"""간호사 attribute의 시점-effective 값 lookup 헬퍼.

핵심 룰:
- 활성 `NurseAssignment` row가 as_of 시점에 효력 있으면 그 `target_*` 값 우선
- 그렇지 않으면 `Nurse` 테이블 값 폴백

활성 조건:
    status='active' AND start_date <= as_of AND (end_date IS NULL OR as_of <= end_date)

여러 row 활성 시: 가장 최근 `start_date` 우선 (Phase 1.4에서 kind 도입 후 더 정밀화 예정).

참조: docs/NURSE_GROUP_CHANGE_MODEL.md §1, §9.
"""
from __future__ import annotations

from datetime import date
from typing import Any, Iterable

from sqlalchemy.orm import Session

from db.models import Nurse, NurseAssignment


# attr 이름 → NurseAssignment의 target_* 컬럼 매핑.
# kind/payload 컬럼 추가 전이라 기존 target_* 컬럼만 사용 (Phase 1.4 이후 payload JSON 병용 예정).
#
# 권위 있는 출처: roster_create_service.py:4665~4671 inbound override 블록과 1:1 일치.
# 엔진이 실제로 각 target_* 를 어느 Nurse 속성에 주입하는지를 그대로 반영한다.
#   d['team_id']           = target_team_id
#   d['grade']             = target_grade
#   d['weekly_off_enabled']= target_weekly_off_enabled
#   d['weekly_off_type']   = target_weekly_off_type      (런타임 주입 속성, 실컬럼 아님)
#   d['weekly_off_weekday']= target_weekly_off_weekday
#   d['is_night_nurse']    = target_shift_types           (허용 시프트 코드 리스트, []=전부 허용)
#   d['fixed_shift']       = target_fixed_shift
#
# 의도적으로 제외된 것:
#   - group_id: override 아님. 병동이동은 flush가 Nurse.group_id를 직접 바꾸고,
#               파견은 본가 group_id 유지(엔진에 inbound로 추가). 별도 메커니즘.
#   - is_weekend_off: target_is_weekend_off 컬럼 자체가 없음 → 항상 Nurse 폴백.
#   - wanted_max_requests: target 컬럼은 있으나 inbound override가 주입하지 않음(엔진 미사용).
_ATTR_TO_TARGET_COL: dict[str, str] = {
    "team_id": "target_team_id",
    "grade": "target_grade",
    "weekly_off_enabled": "target_weekly_off_enabled",
    "weekly_off_type": "target_weekly_off_type",
    "weekly_off_weekday": "target_weekly_off_weekday",
    "is_night_nurse": "target_shift_types",
    "fixed_shift": "target_fixed_shift",
}

# Nurse 값만 보는 attr (NurseAssignment override 대상 아님 → 항상 폴백)
_NURSE_ONLY_ATTRS: set[str] = {
    "group_id",          # 병동이동 flush가 직접 변경 / 파견은 본가 유지
    "is_weekend_off",    # target 컬럼 없음
    "active",
    "name",
    "account_id",
    "experience_years",
    "office_id",
    "original_group_id",
}


def _is_unset_override(val: Any) -> bool:
    """target_* 값이 "미설정"인지 판정 — 폴백 여부 결정용.

    None과 빈 컨테이너([], "", {})는 미설정 → Nurse 폴백.
    단 0/False는 의미 있는 값(예: target_weekly_off_enabled=0 = 주말휴무 OFF)이라 설정으로 본다.

    배경: target_shift_types 컬럼 기본값이 `[]`(default=list)라, 빈 리스트는
    "모든 시프트 금지"가 아니라 "override 없음"을 뜻한다. 엔진도 `(... or [])` 패턴으로
    None/[]를 동일하게 "제약 없음"으로 취급한다 (roster_create_service.py:3205).
    """
    if val is None:
        return True
    if isinstance(val, (list, dict, str)) and len(val) == 0:
        return True
    return False


def get_active_assignment(
    db: Session,
    nurse_id: str,
    as_of: date,
) -> NurseAssignment | None:
    """as_of 시점에 활성인 `NurseAssignment` row 1건 반환.

    여러 활성 row가 있으면 가장 최근 `start_date` 우선.
    """
    return (
        db.query(NurseAssignment)
        .filter(
            NurseAssignment.nurse_id == nurse_id,
            NurseAssignment.status == "active",
            NurseAssignment.start_date <= as_of,
            (
                (NurseAssignment.end_date.is_(None))
                | (NurseAssignment.end_date >= as_of)
            ),
        )
        .order_by(NurseAssignment.start_date.desc())
        .first()
    )


def get_active_assignments_batch(
    db: Session,
    nurse_ids: Iterable[str],
    as_of: date,
) -> dict[str, NurseAssignment]:
    """여러 nurse의 활성 assignment를 한 번에 조회 (N+1 방지).

    각 nurse_id에 대해 가장 최근 start_date 활성 row 1건만 dict로 반환.
    """
    nurse_ids = list(nurse_ids)
    if not nurse_ids:
        return {}
    rows = (
        db.query(NurseAssignment)
        .filter(
            NurseAssignment.nurse_id.in_(nurse_ids),
            NurseAssignment.status == "active",
            NurseAssignment.start_date <= as_of,
            (
                (NurseAssignment.end_date.is_(None))
                | (NurseAssignment.end_date >= as_of)
            ),
        )
        .order_by(NurseAssignment.start_date.desc())
        .all()
    )
    out: dict[str, NurseAssignment] = {}
    for r in rows:
        # 같은 nurse_id의 첫 row(가장 최근 start_date)만 유지
        if r.nurse_id not in out:
            out[r.nurse_id] = r
    return out


def get_nurse_effective_attr(
    db: Session,
    nurse: Nurse,
    attr: str,
    as_of: date,
    *,
    _assignment_cache: NurseAssignment | None | object = ...,  # 내부 캐싱용
) -> Any:
    """nurse의 attr 값을 as_of 시점 기준으로 반환.

    1. NurseAssignment 활성 row가 있고 매핑된 target_* 값이 None이 아니면 → 그 값
    2. 아니면 Nurse 테이블 attr 폴백

    Args:
        _assignment_cache: 호출자가 미리 조회한 NurseAssignment를 전달하면 재조회 안 함.
                          명시적 None = "활성 assignment 없음을 안다" (조회 생략).
    """
    if attr in _ATTR_TO_TARGET_COL:
        if _assignment_cache is ...:
            assignment = get_active_assignment(db, nurse.nurse_id, as_of)
        else:
            assignment = _assignment_cache  # type: ignore[assignment]
        if assignment is not None:
            target_col = _ATTR_TO_TARGET_COL[attr]
            val = getattr(assignment, target_col, None)
            if not _is_unset_override(val):
                return val
    return getattr(nurse, attr, None)


def apply_effective_attrs_to_nurse(
    db: Session,
    nurse: Nurse,
    as_of: date,
    *,
    assignment: NurseAssignment | None | object = ...,
    attrs: Iterable[str] | None = None,
) -> Nurse:
    """nurse 객체의 attribute들을 as_of 시점의 effective 값으로 in-place 갱신.

    엔진 입력 빌더(Phase 1.2)에서 사용: 솔버에 넘기기 전 한 번 호출하면
    솔버 본체는 `nurse.x`를 그대로 read해도 시점-effective 값을 봄.

    Args:
        assignment: 미리 조회된 활성 assignment (호출자가 batch로 가져와 넘기면 효율적).
                    ... = 직접 조회, None = 활성 없음.
        attrs: 적용할 attribute 화이트리스트. None이면 _ATTR_TO_TARGET_COL의 모든 키.

    Returns:
        in-place 갱신된 nurse (편의용 반환).
    """
    if assignment is ...:
        assignment = get_active_assignment(db, nurse.nurse_id, as_of)
    if assignment is None:
        return nurse  # 활성 없음 → 폴백 그대로

    targets = attrs if attrs is not None else _ATTR_TO_TARGET_COL.keys()
    for attr in targets:
        if attr not in _ATTR_TO_TARGET_COL:
            continue
        target_col = _ATTR_TO_TARGET_COL[attr]
        val = getattr(assignment, target_col, None)
        if not _is_unset_override(val):
            setattr(nurse, attr, val)
    return nurse
