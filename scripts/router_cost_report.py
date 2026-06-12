"""Router 비용 리포트 — 카테고리별 프롬프트 절감 측정 (오프라인, 결정론).

측정 대상:
  1) system prompt 의 '## 사용 가능한 도구' 텍스트 섹션 char 수 (전체 vs scoped)
  2) chat(tools=) 로 가는 JSON 스키마 char 수 (전체 vs scoped)
  3) 라우터 분류 호출이 추가하는 프롬프트 char 수 (_CLASSIFY_SYSTEM)

토큰은 한국어 혼합 기준 대략 chars/3.5 로 추정(절대값 아닌 비교용).

실행:  uv run python scripts/router_cost_report.py
"""

from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

from agents_v2.harness.prompt_builder import _build_tool_descriptions_section  # noqa: E402
from agents_v2.router import (  # noqa: E402
    ALL_TOOL_NAMES,
    CATEGORY_TOOLS,
    _CLASSIFY_SYSTEM,
    resolve_tools,
)
from agents_v2.skills.descriptions import SKILL_TOOLS  # noqa: E402

_CHARS_PER_TOKEN = 3.5
_BY_NAME = {t["name"]: t for t in SKILL_TOOLS}


def _est_tokens(chars: int) -> int:
    return round(chars / _CHARS_PER_TOKEN)


def _schema_chars(tool_names: list[str]) -> int:
    """chat(tools=) 로 전달되는 JSON 스키마 크기."""
    subset = [_BY_NAME[n] for n in tool_names]
    return len(json.dumps(subset, ensure_ascii=False))


def _text_chars(tool_names: list[str] | None) -> int:
    return len(_build_tool_descriptions_section(tool_names))


def main() -> None:
    full_text = _text_chars(None)
    full_schema = _schema_chars(ALL_TOOL_NAMES)
    full_total = full_text + full_schema
    classify_chars = len(_CLASSIFY_SYSTEM)

    print("=" * 78)
    print("ROUTER COST REPORT — '## 사용 가능한 도구' 텍스트 + tools= JSON 스키마")
    print("=" * 78)
    print(f"전체(14 tool): text={full_text}c  schema={full_schema}c  "
          f"합계={full_total}c (~{_est_tokens(full_total)} tok)")
    print(f"라우터 분류 프롬프트(_CLASSIFY_SYSTEM): {classify_chars}c "
          f"(~{_est_tokens(classify_chars)} tok)  ← 턴당 추가")
    print("-" * 78)
    print(f"{'category':<16}{'n':>3} {'text':>7} {'schema':>8} "
          f"{'scoped':>8} {'절감%':>7} {'순절감(분류차감)':>16}")
    print("-" * 78)

    rows = []
    for cat in CATEGORY_TOOLS:
        names = resolve_tools([cat])
        t = _text_chars(names)
        s = _schema_chars(names)
        tot = t + s
        saved = full_total - tot
        net = saved - classify_chars  # 라우터 추가 비용 차감
        pct = 100.0 * saved / full_total
        rows.append((cat, len(names), t, s, tot, pct, net))
        print(f"{cat:<16}{len(names):>3} {t:>7} {s:>8} {tot:>8} "
              f"{pct:>6.1f}% {net:>16}")

    print("-" * 78)
    avg_scoped = sum(r[4] for r in rows) / len(rows)
    avg_saved_pct = 100.0 * (full_total - avg_scoped) / full_total
    avg_net = sum(r[6] for r in rows) / len(rows)
    print(f"평균 scoped 합계: {avg_scoped:.0f}c  | 평균 절감: {avg_saved_pct:.1f}%  | "
          f"평균 순절감(분류 차감): {avg_net:.0f}c (~{_est_tokens(avg_net)} tok)")
    print("주의: 절감은 tool 섹션+스키마에 한정. DOMAIN_KNOWLEDGE 등 나머지 프롬프트는 불변.")


if __name__ == "__main__":
    main()
