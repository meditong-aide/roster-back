"""월 단위 시점 속성 해석 (Phase 1: team).

`nurse_month_profile` 가 진실, `nurses` 는 현재값 캐시. 그 달 행이 있으면 그 값을,
없으면 nurses(현재값)로 폴백한다(델타만 저장). 모든 화면/생성기는 시점 team 을
이 모듈을 통해서만 읽어 SSOT 를 유지한다.
참조: docs/TEMPORAL_NURSE_MODEL_DESIGN.md §4.1·§4.6.
"""

from __future__ import annotations

from typing import Optional

from sqlalchemy.orm import Session

from db.models import Nurse as NurseModel
from db.models import NurseMonthProfile


def get_month_profile(
    db: Session, nurse_id: str, year: int, month: int
) -> Optional[NurseMonthProfile]:
    """그 달의 프로필 행(없으면 None)."""
    return (
        db.query(NurseMonthProfile)
        .filter(
            NurseMonthProfile.nurse_id == nurse_id,
            NurseMonthProfile.year == year,
            NurseMonthProfile.month == month,
        )
        .first()
    )


def resolve_team_as_of(
    db: Session,
    nurse_id: str,
    year: int,
    month: int,
    *,
    fallback_team_id: Optional[int] = None,
) -> Optional[int]:
    """그 달의 유효 team_id. profile 행 있으면 그 값, 없으면 nurses(현재값) 폴백.

    fallback_team_id 를 주면 nurses 재조회를 생략한다(이미 nurse 객체를 들고 있을 때).
    """
    prof = get_month_profile(db, nurse_id, year, month)
    if prof is not None:
        return prof.team_id
    if fallback_team_id is not None:
        return fallback_team_id
    nurse = (
        db.query(NurseModel.team_id)
        .filter(NurseModel.nurse_id == nurse_id)
        .first()
    )
    return nurse[0] if nurse else None


def upsert_month_profile(
    db: Session,
    *,
    nurse_id: str,
    year: int,
    month: int,
    group_id: str,
    team_id: Optional[int] = None,
    grade: Optional[int] = None,
    shift_rule=None,
    weekend_off: Optional[int] = None,
    source: str = "edited",
    note: Optional[str] = None,
    if_absent_only: bool = False,
    commit: bool = True,
) -> NurseMonthProfile:
    """그 달 프로필 upsert.

    if_absent_only=True 면 이미 행이 있을 때 덮어쓰지 않는다(freeze 멱등용).
    값 인자가 None 이면 해당 컬럼은 건드리지 않는다(부분 갱신).
    """
    prof = get_month_profile(db, nurse_id, year, month)
    if prof is not None:
        if if_absent_only:
            return prof
        prof.group_id = group_id
        if team_id is not None:
            prof.team_id = team_id
        if grade is not None:
            prof.grade = grade
        if shift_rule is not None:
            prof.shift_rule = shift_rule
        if weekend_off is not None:
            prof.weekend_off = weekend_off
        prof.source = source
        if note is not None:
            prof.note = note
    else:
        prof = NurseMonthProfile(
            nurse_id=nurse_id, year=year, month=month, group_id=group_id,
            team_id=team_id, grade=grade, shift_rule=shift_rule,
            weekend_off=weekend_off, source=source, note=note,
        )
        db.add(prof)
    if commit:
        db.commit()
        db.refresh(prof)
    return prof
