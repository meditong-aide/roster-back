"""manage-grade skill — 등급(역량 grade)별 근무 정책 조회/수정.

확정 스코프 (사용자 결정):
  - constraints        : shift별 grade 최소 인원
  - constraints_max    : shift별 grade 최대 인원(anti-pair)
  - allow_soft_fallback: 일자별 grade 제약 hard↔soft 토글
  - grade_names        : grade 번호 ↔ 표시 이름

설계 메모:
  - UX 규칙: 사용자에게 grade 번호(id)·JSON 원형을 노출하지 않는다. 입출력은 grade '이름' 기반.
    grade 위계가 없으므로 "맨 위 등급" 같은 순서 표현은 해석하지 않고 재질의한다.
  - 재사용: services.grade_service 의 get/upsert (검증 _validate_constraints 포함).
  - preview: upsert_grade_config_service 는 즉시 commit 하므로 preview_only 단계에선 호출하지 않고
    현재값 대비 변경 요약만 반환한다 (constraint_tools 패턴과 동일). 확인(confirm) 시
    agent_v3._execute_approval 이 preview_only=False 로 재호출 → 실제 upsert.
  - 부분 갱신: upsert 는 constraints/constraints_max 를 전체 교체하므로, 스킬이
    read-modify-write 로 현재 dict 를 로드 → 해당 (shift, grade) 셀만 patch → 전체 dict 전달.
"""

from __future__ import annotations

import copy
import re
from typing import Any

from sqlalchemy.orm import Session

from agents_v2.skills._shift_category import CATEGORY_LABEL as _CATEGORY_LABEL
from agents_v2.skills._shift_category import resolve_shift_category as _resolve_shift_category
from agents_v2.skills.registry import register
from agents_v2.tools.nurse_tools import get_nurses_in_group
from schemas.grade_schema import GradeConfigUpsert
from services.grade_service import (
    _validate_constraints,
    get_grade_config_service,
    upsert_grade_config_service,
)

_APPLY_NOTE = "다음 근무표 생성부터 반영됩니다."


@register("manage-grade")
def manage_grade(db: Session, params: dict) -> Any:
    """등급별 근무 정책 조회/수정. operation 에 따라 분기."""
    operation = str(params.get("operation") or "read").lower()
    group_id = params["group_id"]
    office_id = params.get("office_id")
    user_id = params.get("acting_user_id")
    preview_only = params.get("preview_only", True)

    if operation == "read":
        return _read(db, group_id)
    if operation == "set_requirement":
        return _set_requirement(
            db, group_id, office_id, user_id, params, preview_only
        )
    if operation == "set_soft_fallback":
        return _set_soft_fallback(
            db, group_id, office_id, user_id, params, preview_only
        )
    if operation == "set_grade_name":
        return _set_grade_name(
            db, group_id, office_id, user_id, params, preview_only
        )
    return {"error": f"지원하지 않는 operation: {operation}"}


# ── read ─────────────────────────────────────────────────────


def _read(db: Session, group_id: str) -> dict:
    resp = get_grade_config_service(db, group_id)
    _, num_to_name = _grade_name_index(resp.grade_names)

    min_reqs = [
        {"shift": _CATEGORY_LABEL.get(s, s), "grade": _grade_label(int(g), num_to_name), "min_count": int(c)}
        for s, gmap in (resp.constraints or {}).items()
        for g, c in (gmap or {}).items()
        if _is_int(g)
    ]
    max_reqs = [
        {"shift": _CATEGORY_LABEL.get(s, s), "grade": _grade_label(int(g), num_to_name), "max_count": int(c)}
        for s, gmap in (resp.constraints_max or {}).items()
        for g, c in (gmap or {}).items()
        if _is_int(g) and int(c) >= 0
    ]
    counts = _roster_grade_counts(db, group_id)
    known = sorted(_known_grades(db, group_id, resp))
    grades = [_grade_label(n, num_to_name) for n in known]
    distribution = [
        {"grade": _grade_label(n, num_to_name), "nurse_count": counts.get(n, 0)}
        for n in known
    ]
    return {
        "grades": grades,
        "grade_distribution": distribution,
        "min_requirements": min_reqs,
        "max_requirements": max_reqs,
        "soft_fallback_enabled": bool(resp.allow_soft_fallback),
        "soft_fallback_label": (
            "완화(soft) — grade 정원을 못 채워도 근무표를 생성합니다."
            if resp.allow_soft_fallback
            else "엄격(hard) — grade 정원을 반드시 지킵니다."
        ),
        "names_configured": bool(_grade_name_index(resp.grade_names)[0]),
    }


# ── set_requirement (min / max) ──────────────────────────────


def _set_requirement(
    db: Session, group_id: str, office_id: str | None, user_id: str | None,
    params: dict, preview_only: bool,
) -> dict:
    resp = get_grade_config_service(db, group_id)
    use_mid = bool(resp.use_mid)

    code = _resolve_shift_category(params.get("shift_name"), use_mid)
    if code is None:
        return _shift_clarification(params.get("shift_name"), use_mid)

    name_to_num, num_to_name = _grade_name_index(resp.grade_names)
    num = _resolve_grade(params.get("grade_name"), name_to_num)
    if num is None:
        return _grade_clarification(db, group_id, params.get("grade_name"), resp)

    min_count = _as_int_or_none(params.get("min_count"))
    max_count = _as_int_or_none(params.get("max_count"))
    if min_count is None and max_count is None:
        return {
            "needs_clarification": True,
            "question": "최소 인원과 최대 인원 중 무엇을 설정할까요? (예: 최소 2명 / 최대 1명)",
            "options": [],
        }

    grade_label = _grade_label(num, num_to_name)
    shift_label = _CATEGORY_LABEL.get(code, code)
    changes: list[dict] = []
    payload_kwargs: dict[str, Any] = {}

    if min_count is not None:
        new_min = copy.deepcopy(resp.constraints or {})
        gmap = new_min.setdefault(code, {})
        before = _pop_grade(gmap, num)
        gmap[str(num)] = max(0, min_count)
        err = _safe_validate(new_min, use_mid, allow_negative=False)
        if err:
            return err
        payload_kwargs["constraints"] = new_min
        changes.append({
            "type": "최소 인원", "shift": shift_label, "grade": grade_label,
            "before": before, "after": max(0, min_count),
        })

    if max_count is not None:
        new_max = copy.deepcopy(resp.constraints_max or {})
        gmap = new_max.setdefault(code, {})
        before = _pop_grade(gmap, num)
        # 음수 = 제한 없음 (schema 규약). 사용자 "최대 제한 없애줘" → max_count=-1.
        gmap[str(num)] = int(max_count)
        err = _safe_validate(new_max, use_mid, allow_negative=True)
        if err:
            return err
        payload_kwargs["constraints_max"] = new_max
        changes.append({
            "type": "최대 인원", "shift": shift_label, "grade": grade_label,
            "before": before,
            "after": ("제한 없음" if int(max_count) < 0 else int(max_count)),
        })

    if preview_only:
        return {"preview": True, "changes": changes, "note": _APPLY_NOTE}

    # 명시적 사용자 지정 → grade-1-only 정규화 우회 (skip_min_normalization=True).
    upsert_grade_config_service(
        db, office_id, group_id, GradeConfigUpsert(**payload_kwargs), user_id or "",
        skip_min_normalization=True,
    )
    return {"preview": False, "applied": True, "changes": changes, "note": _APPLY_NOTE}


# ── set_soft_fallback ────────────────────────────────────────


def _set_soft_fallback(
    db: Session, group_id: str, office_id: str | None, user_id: str | None,
    params: dict, preview_only: bool,
) -> dict:
    enabled = params.get("soft_enabled")
    if not isinstance(enabled, bool):
        return {
            "needs_clarification": True,
            "question": "grade 제약을 완화(soft)할까요, 엄격(hard)하게 유지할까요?",
            "options": ["완화(soft)", "엄격(hard)"],
        }
    resp = get_grade_config_service(db, group_id)
    before = bool(resp.allow_soft_fallback)
    change = {
        "type": "grade 제약 완화",
        "before": "완화(soft)" if before else "엄격(hard)",
        "after": "완화(soft)" if enabled else "엄격(hard)",
    }
    if before == enabled:
        return {"preview": False, "applied": False, "no_change": True, "changes": [change]}

    if preview_only:
        return {"preview": True, "changes": [change], "note": _APPLY_NOTE}

    upsert_grade_config_service(
        db, office_id, group_id,
        GradeConfigUpsert(allow_soft_fallback=enabled), user_id or "",
    )
    return {"preview": False, "applied": True, "changes": [change], "note": _APPLY_NOTE}


# ── set_grade_name ───────────────────────────────────────────


def _set_grade_name(
    db: Session, group_id: str, office_id: str | None, user_id: str | None,
    params: dict, preview_only: bool,
) -> dict:
    new_name = params.get("new_name")
    if not new_name or not str(new_name).strip():
        return {
            "needs_clarification": True,
            "question": "어떤 이름으로 설정할까요?",
            "options": [],
        }
    resp = get_grade_config_service(db, group_id)
    name_to_num, num_to_name = _grade_name_index(resp.grade_names)
    num = _resolve_grade(params.get("target_grade_name"), name_to_num)
    if num is None:
        return _grade_clarification(db, group_id, params.get("target_grade_name"), resp)

    # 기존 이름 보존 + target 갱신 (빈 이름은 버려 read 시 재유도)
    new_names = {str(k): v for k, v in num_to_name.items() if v and str(v).strip()}
    before = new_names.get(str(num))
    new_names[str(num)] = str(new_name).strip()
    change = {"type": "등급 이름", "before": before, "after": str(new_name).strip()}

    if preview_only:
        return {"preview": True, "changes": [change], "note": "표시 이름만 바뀌며 근무표 생성에는 영향이 없습니다."}

    upsert_grade_config_service(
        db, office_id, group_id,
        GradeConfigUpsert(grade_names=new_names), user_id or "",
    )
    return {"preview": False, "applied": True, "changes": [change]}


# ── helpers ──────────────────────────────────────────────────


def _shift_clarification(name: Any, use_mid: bool) -> dict:
    options = ["데이", "이브닝", "나이트"] + (["미드"] if use_mid else [])
    return {
        "needs_clarification": True,
        "question": f"'{name}' 근무를 인식하지 못했습니다. 어떤 근무인가요?",
        "options": options,
    }


def _grade_name_index(grade_names: Any) -> tuple[dict[str, int], dict[int, str]]:
    """grade_names → (이름→번호, 번호→이름). 빈 이름은 이름→번호에서 제외."""
    name_to_num: dict[str, int] = {}
    num_to_name: dict[int, str] = {}
    if isinstance(grade_names, dict):
        for k, v in grade_names.items():
            if not _is_int(k):
                continue
            num = int(k)
            name = str(v).strip() if v is not None else ""
            num_to_name[num] = name
            if name:
                name_to_num[name.lower()] = num
    return name_to_num, num_to_name


def _resolve_grade(grade_name: Any, name_to_num: dict[str, int]) -> int | None:
    """사용자가 말한 등급 표현 → grade 번호. 못 찾으면 None (→ 재질의).

    위계가 없으므로 '맨 위/최고/제일 높은' 같은 순서 표현은 해석하지 않는다.
    """
    if grade_name is None:
        return None
    s = str(grade_name).strip()
    # 옵션 라벨을 그대로 되돌려준 경우(예 "주니어 (5명)", "3등급 (2명)") 괄호 표기 제거.
    s = re.sub(r"\s*\(.*?\)\s*$", "", s).strip()
    if not s:
        return None
    if re.fullmatch(r"\d+", s):
        return int(s)
    # "2등급" / "2급" / "2레벨" 형태의 숫자 추출
    m = re.fullmatch(r"(\d+)\s*(등급|급|레벨)", s)
    if m:
        return int(m.group(1))
    # 이름 매칭 ("시니어", "주니어" 등). 접미사 제거 후 재시도.
    key = s.lower()
    if key in name_to_num:
        return name_to_num[key]
    stripped = re.sub(r"\s*(등급|급|레벨)$", "", s).strip().lower()
    return name_to_num.get(stripped)


def _grade_clarification(db: Session, group_id: str, grade_name: Any, resp: Any) -> dict:
    """등급 해석 실패 시 재질의. 후보는 설정 + **실제 간호사 명부**의 등급에서 뽑고,
    각 등급의 간호사 수를 함께 보여줘 사용자가 고르기 쉽게 한다.
    """
    _, num_to_name = _grade_name_index(resp.grade_names)
    counts = _roster_grade_counts(db, group_id)
    known = sorted(_known_grades(db, group_id, resp))
    options = [_grade_option_label(n, num_to_name, counts) for n in known]
    has_names = any(num_to_name.get(n) for n in known)
    if has_names:
        question = f"'{grade_name}' 등급을 찾지 못했습니다. 어떤 등급인가요?"
    else:
        question = (
            f"'{grade_name}' 등급을 찾지 못했습니다. 아직 등급 이름이 설정돼 있지 않습니다. "
            "아래 등급 중에서 골라 주시거나, 먼저 등급 이름을 정해 주세요."
        )
    return {"needs_clarification": True, "question": question, "options": options}


def _known_grades(db: Session, group_id: str, resp: Any) -> set[int]:
    """grade_names + constraints + constraints_max + **실제 간호사 명부**의 grade 합집합."""
    grades: set[int] = set()
    _, num_to_name = _grade_name_index(resp.grade_names)
    grades.update(num_to_name.keys())
    for cmap in (resp.constraints or {}, resp.constraints_max or {}):
        for gmap in cmap.values():
            for g in (gmap or {}).keys():
                if _is_int(g):
                    grades.add(int(g))
    grades.update(_roster_grade_counts(db, group_id).keys())
    return grades


def _roster_grade_counts(db: Session, group_id: str) -> dict[int, int]:
    """그룹 active 간호사들의 grade 별 인원수. 조회 실패 시 빈 dict."""
    counts: dict[int, int] = {}
    try:
        for n in get_nurses_in_group(db, group_id):
            g = n.get("grade")
            if _is_int(g):
                counts[int(g)] = counts.get(int(g), 0) + 1
    except Exception:  # noqa: BLE001 — 명부 조회 실패가 스킬을 막지 않도록
        return {}
    return counts


def _grade_option_label(num: int, num_to_name: dict[int, str], counts: dict[int, int]) -> str:
    name = num_to_name.get(num)
    base = name.strip() if name and name.strip() else f"{num}등급"
    cnt = counts.get(num)
    return f"{base} ({cnt}명)" if cnt else base


def _grade_label(num: int, num_to_name: dict[int, str]) -> str:
    name = num_to_name.get(num)
    return name if name and name.strip() else f"{num}등급"


def _pop_grade(gmap: dict, num: int) -> Any:
    """gmap 에서 num(문자/정수 키 혼재 가능)을 찾아 제거하고 이전값 반환."""
    before = None
    for k in list(gmap.keys()):
        if _is_int(k) and int(k) == num:
            before = gmap.pop(k)
    return before


def _safe_validate(constraints: dict, use_mid: bool, allow_negative: bool) -> dict | None:
    try:
        _validate_constraints(constraints, use_mid=use_mid, allow_negative_as_unset=allow_negative)
    except ValueError as e:
        return {"error": str(e)}
    return None


def _is_int(v: Any) -> bool:
    try:
        int(v)
        return True
    except (TypeError, ValueError):
        return False


def _as_int_or_none(v: Any) -> int | None:
    if v is None:
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None
