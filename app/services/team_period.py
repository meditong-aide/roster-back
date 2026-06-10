"""team 시점 구간 해석/기록 (Phase 1).

`nurse_team_period` 가 진실, `nurses.team_id` 는 현재값 캐시. 모든 화면/생성기는
시점 team 을 이 모듈을 통해서만 읽어 SSOT 를 유지한다.

규칙(docs/TEMPORAL_NURSE_MODEL_DESIGN.md v3):
- 변경 = close-before-open(옛 구간 valid_to 닫고 새 구간 open), 삭제 금지.
- 겹침 금지, gap(미지정) 허용, valid_to=null 은 열린 구간.
- 폴백 ward-aware: 구간 없으면 nurses.group_id==group 일 때만 nurses.team_id, 아니면 None.
"""

from __future__ import annotations

from calendar import monthrange
from datetime import date
from typing import Optional

from sqlalchemy import or_
from sqlalchemy.orm import Session

from db.models import Nurse as NurseModel
from db.models import NurseTeamPeriod


def get_team_period_on(
    db: Session, nurse_id: str, group_id: str, on_date: date
) -> Optional[NurseTeamPeriod]:
    """그 날짜를 덮는 구간(없으면 None). [valid_from, valid_to) 반쪽열림."""
    return (
        db.query(NurseTeamPeriod)
        .filter(
            NurseTeamPeriod.nurse_id == nurse_id,
            NurseTeamPeriod.group_id == group_id,
            NurseTeamPeriod.valid_from <= on_date,
            or_(
                NurseTeamPeriod.valid_to.is_(None),
                NurseTeamPeriod.valid_to > on_date,
            ),
        )
        .order_by(NurseTeamPeriod.valid_from.desc())
        .first()
    )


def _coerce_team_int(value) -> Optional[int]:
    """team_id 를 int 로 정규화(실패 시 None).

    MSSQL(pymssql)이 nurses.team_id 를 문자열 '1'/'2' 로 돌려주는 트랩 방어 —
    period(INT 컬럼)는 int, 캐시 폴백은 str 라 resolve 결과 타입이 섞이면 프론트의
    Map 키(team.team_id 숫자) 매칭이 깨져 '팀 비어 보임' 버그가 난다. 항상 int 로 통일.
    """
    if value is None:
        return None
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _ward_aware_fallback(db: Session, nurse_id: str, group_id: str) -> Optional[int]:
    """구간 없을 때: nurses.group_id==group 일 때만 nurses.team_id, 아니면 None."""
    row = (
        db.query(NurseModel.group_id, NurseModel.team_id)
        .filter(NurseModel.nurse_id == nurse_id)
        .first()
    )
    # MSSQL CHAR 컬럼 트레일링 공백/포맷 차이 방어: strip 후 비교.
    if row and str(row[0] or "").strip() == str(group_id or "").strip():
        return _coerce_team_int(row[1])
    return None


def resolve_team(
    db: Session, nurse_id: str, group_id: str, on_date: date
) -> Optional[int]:
    """그 날짜의 유효 team_id. 구간 우선, 없으면 ward-aware 폴백."""
    p = get_team_period_on(db, nurse_id, group_id, on_date)
    if p is not None:
        return _coerce_team_int(p.team_id)
    return _ward_aware_fallback(db, nurse_id, group_id)


def resolve_team_for_roster(
    db: Session, nurse_id: str, group_id: str, year: int, month: int
) -> Optional[int]:
    """로스터(병동·월)용 단일 team — Phase1(gap만): 월초를 덮는 구간 우선,
    없으면 그 달에 겹치는 첫 구간(중간 합류 대비), 그것도 없으면 ward-aware 폴백.
    """
    m_start = date(year, month, 1)
    m_end = date(year, month, monthrange(year, month)[1])
    periods = (
        db.query(NurseTeamPeriod)
        .filter(
            NurseTeamPeriod.nurse_id == nurse_id,
            NurseTeamPeriod.group_id == group_id,
            NurseTeamPeriod.valid_from <= m_end,
            or_(
                NurseTeamPeriod.valid_to.is_(None),
                NurseTeamPeriod.valid_to > m_start,
            ),
        )
        .order_by(NurseTeamPeriod.valid_from.asc())
        .all()
    )
    if periods:
        covering = [
            p for p in periods
            if p.valid_from <= m_start and (p.valid_to is None or p.valid_to > m_start)
        ]
        return _coerce_team_int((covering[0] if covering else periods[0]).team_id)
    return _ward_aware_fallback(db, nurse_id, group_id)


def set_team_period(
    db: Session,
    *,
    nurse_id: str,
    group_id: str,
    valid_from: date,
    team_id: Optional[int],
    source: str = "edited",
    note: Optional[str] = None,
    commit: bool = True,
) -> NurseTeamPeriod:
    """close-before-open 으로 team 구간을 기록한다.

    - valid_from 과 같은 시작일 구간이 이미 있으면 그 행을 갱신(재설정).
    - 아니면 valid_from 을 덮는 직전 열린/겹침 구간들을 valid_to=valid_from 으로 닫고,
      [valid_from, null) 새 구간을 연다. (삭제 없음 — 완전 타임라인)
    """
    base = db.query(NurseTeamPeriod).filter(
        NurseTeamPeriod.nurse_id == nurse_id,
        NurseTeamPeriod.group_id == group_id,
    )
    same = base.filter(NurseTeamPeriod.valid_from == valid_from).first()
    if same is not None:
        same.team_id = team_id
        same.source = source
        if note is not None:
            same.note = note
        row = same
    else:
        covering = base.filter(
            NurseTeamPeriod.valid_from < valid_from,
            or_(
                NurseTeamPeriod.valid_to.is_(None),
                NurseTeamPeriod.valid_to > valid_from,
            ),
        ).all()
        for c in covering:
            c.valid_to = valid_from
        row = NurseTeamPeriod(
            nurse_id=nurse_id, group_id=group_id, valid_from=valid_from,
            valid_to=None, team_id=team_id, source=source, note=note,
        )
        db.add(row)
    # 단일 병동 불변식: 한 간호사는 한 시점에 한 병동에만 속한다. 새 구간을 열 때
    #   다른 그룹의 열린/겹침 구간(valid_from 이전 시작)을 valid_from 에서 닫고,
    #   같은 날 시작하는 다른 그룹 구간은 모순이므로 제거한다.
    #   (병동이동 시 출발 병동 구간 자동 종료 + 재분배 재실행 시 옛 구간 정리 — group-scoped
    #    close 만으로는 cross-ward stale 구간이 영구히 열려 resolve_team 오염되던 버그 차단.)
    others = (
        db.query(NurseTeamPeriod)
        .filter(
            NurseTeamPeriod.nurse_id == nurse_id,
            NurseTeamPeriod.group_id != group_id,
            NurseTeamPeriod.valid_from <= valid_from,
            or_(
                NurseTeamPeriod.valid_to.is_(None),
                NurseTeamPeriod.valid_to > valid_from,
            ),
        )
        .all()
    )
    for o in others:
        if o.valid_from == valid_from:
            db.delete(o)
        else:
            o.valid_to = valid_from
    if commit:
        db.commit()
        db.refresh(row)
    return row
