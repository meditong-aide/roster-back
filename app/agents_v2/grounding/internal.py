"""Internal grounding — resolve natural language references to DB IDs.

Used by the middleware pipeline before skill execution.
Each resolver returns a ResolveResult indicating success, clarification needed, or error.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy.orm import Session


@dataclass
class ResolveResult:
    """Result of a grounding resolution attempt."""

    resolved: bool = False
    value: Any = None
    needs_clarification: bool = False
    question: str | None = None
    options: list[str] | None = None
    error: str | None = None

    def to_clarification_dict(self) -> dict:
        return {
            "needs_clarification": True,
            "question": self.question,
            "options": self.options or [],
        }


# ── Nurse Resolution ────────────────────────────────────────


def resolve_nurse(db: Session, group_id: str, name: str) -> ResolveResult:
    """Resolve nurse name → nurse_id. Clarification on ambiguous match."""
    from agents_v2.tools.nurse_tools import search_nurses_by_name

    exact = search_nurses_by_name(db, group_id, name)

    if len(exact) == 1:
        return ResolveResult(resolved=True, value=exact[0]["nurse_id"])

    if len(exact) > 1:
        return ResolveResult(
            needs_clarification=True,
            question=f"'{name}'에 해당하는 간호사가 {len(exact)}명입니다:",
            options=[
                f"{n['name']} (grade {n.get('grade', '?')})"
                for n in exact
            ],
        )

    # No exact match — try partial/fuzzy
    from agents_v2.tools.nurse_tools import get_nurses_in_group

    all_nurses = get_nurses_in_group(db, group_id)
    fuzzy = [n for n in all_nurses if name in n["name"] or n["name"] in name]
    if fuzzy:
        return ResolveResult(
            needs_clarification=True,
            question=f"'{name}'을 찾을 수 없습니다. 혹시:",
            options=[n["name"] for n in fuzzy[:5]],
        )

    return ResolveResult(error=f"'{name}' 간호사를 찾을 수 없습니다.")


# ── Shift Resolution ────────────────────────────────────────

_SHIFT_ALIASES: dict[str, str] = {
    "데이": "D",
    "주간": "D",
    "낮": "D",
    "낮번": "D",
    "이브닝": "E",
    "초번": "E",
    "나이트": "N",
    "야간": "N",
    "밤": "N",
    "오프": "OFF",
    "비번": "OFF",
    "연차": "V",
    "휴가": "V",
    "주휴": "WO",
    "공가": "공가",
    "미드": "M",
}


def resolve_shift(db: Session, group_id: str, name: str) -> ResolveResult:
    """Resolve shift name/alias → shift_id or list of shift_ids.

    Resolution order:
      1. Direct match on name/shift_id → single shift_id
      2. Match via shift_gb category → list of all shift_ids in category
      3. Alias dict match → single shift_id (or list if alias points to category)
      4. Partial/fuzzy match → auto-resolve if 1 candidate, clarify if 2+
      5. Fallback: return full shift list for LLM retry (Layer 2)

    Returns:
      - value: str (single match) or list[str] (category match)
    """
    from agents_v2.tools.shift_tools import read_shift_definitions

    shifts = read_shift_definitions(db, group_id)

    # Step 1: Direct match on shift name or shift_id
    upper_name = name.upper()
    for s in shifts:
        if upper_name == s["name"].upper() or upper_name == s["shift_id"].upper():
            return ResolveResult(resolved=True, value=s["shift_id"])

    # Step 2: Match via shift_gb (category) → return ALL matching shift_ids
    category_matches = [s["shift_id"] for s in shifts if name == s.get("shift_gb")]
    if category_matches:
        if len(category_matches) == 1:
            return ResolveResult(resolved=True, value=category_matches[0])
        return ResolveResult(resolved=True, value=category_matches)

    # Step 3: Alias match
    code = _SHIFT_ALIASES.get(name)
    if code:
        # Try direct match first
        for s in shifts:
            if code.upper() == s["name"].upper():
                return ResolveResult(resolved=True, value=s["shift_id"])
        # Alias might point to a shift_gb category name — try category match
        alias_cat_matches = [s["shift_id"] for s in shifts if code == s.get("shift_gb")]
        if alias_cat_matches:
            if len(alias_cat_matches) == 1:
                return ResolveResult(resolved=True, value=alias_cat_matches[0])
            return ResolveResult(resolved=True, value=alias_cat_matches)

    # Step 4: Partial/fuzzy match (handles abbreviations like "생휴"→"생리휴가")
    # Require at least 2-char overlap to avoid single-character false positives
    candidates = [
        s for s in shifts
        if name in s["name"] or s["name"] in name
    ]
    if len(candidates) == 1:
        return ResolveResult(resolved=True, value=candidates[0]["shift_id"])
    if len(candidates) > 1:
        return ResolveResult(
            needs_clarification=True,
            question=f"'{name}'에 해당하는 시프트가 {len(candidates)}개입니다:",
            options=[
                f"{c['name']} ({c['shift_id']}, {c.get('shift_gb', '')})"
                for c in candidates
            ],
        )

    # Step 5: No match — return available shifts for LLM retry (Layer 2)
    shift_summary = [
        f"{s['name']}({s['shift_id']}, {s.get('shift_gb', '')})"
        for s in shifts
    ]
    return ResolveResult(
        error=f"'{name}' 시프트를 찾을 수 없습니다. 사용 가능한 시프트: {', '.join(shift_summary)}",
    )


# ── Date Resolution ─────────────────────────────────────────


def resolve_date(text: str, year: int, month: int) -> str | None:
    """Resolve natural language date → YYYY-MM-DD."""
    # Already ISO format
    if re.match(r"\d{4}-\d{2}-\d{2}", text):
        return text

    # "4월 5일" or "4월5일"
    m = re.match(r"(\d{1,2})월\s*(\d{1,2})일", text)
    if m:
        return f"{year}-{int(m.group(1)):02d}-{int(m.group(2)):02d}"

    # "4/5" format
    m = re.match(r"(\d{1,2})/(\d{1,2})", text)
    if m:
        return f"{year}-{int(m.group(1)):02d}-{int(m.group(2)):02d}"

    # "5일" (day only, use current month)
    m = re.match(r"(\d{1,2})일", text)
    if m:
        return f"{year}-{month:02d}-{int(m.group(1)):02d}"

    # Relative dates
    today = datetime.now()
    if "오늘" in text:
        return today.strftime("%Y-%m-%d")
    if "내일" in text:
        return (today + timedelta(days=1)).strftime("%Y-%m-%d")
    if "어제" in text:
        return (today - timedelta(days=1)).strftime("%Y-%m-%d")

    # Relative weekday: "이번 주 금요일", "다음 주 월요일"
    weekday_map = {
        "월요일": 0, "화요일": 1, "수요일": 2, "목요일": 3,
        "금요일": 4, "토요일": 5, "일요일": 6,
    }
    for name, wd in weekday_map.items():
        if name in text:
            if "다음" in text or "다음주" in text:
                # Next week's weekday
                monday_next = today + timedelta(days=(7 - today.weekday()))
                target = monday_next + timedelta(days=wd)
            else:
                # This week's weekday (default)
                target = today + timedelta(days=(wd - today.weekday()))
            return target.strftime("%Y-%m-%d")

    return None


def resolve_date_range(
    text: str, year: int, month: int
) -> tuple[str | None, str | None]:
    """Resolve natural language date range → (start, end) ISO dates."""
    # "4월 1일~10일"
    m = re.match(r"(\d{1,2})월\s*(\d{1,2})일\s*[~\-]\s*(\d{1,2})일", text)
    if m:
        mon = int(m.group(1))
        return (
            f"{year}-{mon:02d}-{int(m.group(2)):02d}",
            f"{year}-{mon:02d}-{int(m.group(3)):02d}",
        )

    # "1일~10일" (current month)
    m = re.match(r"(\d{1,2})일\s*[~\-]\s*(\d{1,2})일", text)
    if m:
        return (
            f"{year}-{month:02d}-{int(m.group(1)):02d}",
            f"{year}-{month:02d}-{int(m.group(2)):02d}",
        )

    # "이번 주"
    today = datetime.now()
    if "이번 주" in text or "이번주" in text:
        monday = today - timedelta(days=today.weekday())
        sunday = monday + timedelta(days=6)
        return monday.strftime("%Y-%m-%d"), sunday.strftime("%Y-%m-%d")

    # "다음 주"
    if "다음 주" in text or "다음주" in text:
        monday = today - timedelta(days=today.weekday()) + timedelta(weeks=1)
        sunday = monday + timedelta(days=6)
        return monday.strftime("%Y-%m-%d"), sunday.strftime("%Y-%m-%d")

    # "이번 주말"
    if "주말" in text:
        days_until_sat = (5 - today.weekday()) % 7
        saturday = today + timedelta(days=days_until_sat)
        sunday = saturday + timedelta(days=1)
        return saturday.strftime("%Y-%m-%d"), sunday.strftime("%Y-%m-%d")

    # "N주차"
    m = re.search(r"(\d)주차", text)
    if m:
        week_num = int(m.group(1))
        start_day = (week_num - 1) * 7 + 1
        end_day = min(week_num * 7, 31)
        return (
            f"{year}-{month:02d}-{start_day:02d}",
            f"{year}-{month:02d}-{end_day:02d}",
        )

    return None, None
