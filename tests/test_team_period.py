"""team 시점 구간 해석/기록 (Phase 1).

nurse_team_period 가 진실, nurses.team_id 는 현재값 캐시.
- resolve_team: 구간 우선, 없으면 ward-aware 폴백(nurses.group_id==group 일 때만).
- set_team_period: close-before-open(옛 구간 보존), 과거는 닫힌 구간이 보장.
참조: app/services/team_period.py.
"""

from __future__ import annotations

from datetime import date

import pytest

from db.models import Group, Nurse, Office, NurseTeamPeriod
from services.team_period import (
    resolve_team,
    resolve_team_for_roster,
    set_team_period,
    get_team_period_on,
)


@pytest.fixture
def seeded(db):
    db.add(Office(office_id="o1", office_name="병원"))
    db.add(Group(group_id="A", group_name="A병동", office_id="o1"))
    db.add(Group(group_id="B", group_name="B병동", office_id="o1"))
    # nurses 현재값(캐시): home=A, team_id=1
    db.add(Nurse(nurse_id="n1", account_id="acc_n1", group_id="A", office_id="o1",
                 name="간호1", active=1, team_id=1, is_night_nurse=[]))
    db.flush()
    return db


def test_no_period_ward_aware_fallback(seeded):
    """구간 없으면 nurses(현재값). 단 ward-aware — home(A)만, 다른 병동(B)은 None."""
    db = seeded
    assert resolve_team(db, "n1", "A", date(2026, 7, 10)) == 1   # home → 캐시
    assert resolve_team(db, "n1", "B", date(2026, 7, 10)) is None  # 다른 병동 → None


def test_period_overrides_cache(seeded):
    """그 구간이 있으면 캐시가 아니라 구간 값."""
    db = seeded
    set_team_period(db, nurse_id="n1", group_id="A", valid_from=date(2026, 7, 1), team_id=2)
    assert resolve_team(db, "n1", "A", date(2026, 7, 10)) == 2


def test_close_before_open_preserves_past(seeded):
    """7/15 팀 변경: 옛 구간 닫고 새 구간 — 과거는 닫힌 구간이 보장(freeze 없이)."""
    db = seeded
    set_team_period(db, nurse_id="n1", group_id="A", valid_from=date(2026, 7, 1), team_id=1)
    set_team_period(db, nurse_id="n1", group_id="A", valid_from=date(2026, 7, 15), team_id=2)
    # 과거(7/10)=팀1, 현재/미래(7/20)=팀2
    assert resolve_team(db, "n1", "A", date(2026, 7, 10)) == 1
    assert resolve_team(db, "n1", "A", date(2026, 7, 20)) == 2
    # 옛 구간이 7/15 로 닫혔는지(겹침 없음)
    old = get_team_period_on(db, "n1", "A", date(2026, 7, 10))
    assert old.valid_to == date(2026, 7, 15)
    # 구간 삭제 안 됨(완전 타임라인): 2개 존재
    cnt = db.query(NurseTeamPeriod).filter(NurseTeamPeriod.nurse_id == "n1").count()
    assert cnt == 2


def test_gap_is_unspecified(seeded):
    """구간을 1~12로 닫으면 13~15는 미지정(gap) — 폴백(home 캐시)으로."""
    db = seeded
    set_team_period(db, nurse_id="n1", group_id="A", valid_from=date(2026, 7, 1), team_id=2)
    # 7/12 로 닫음(이후 미지정)
    p = get_team_period_on(db, "n1", "A", date(2026, 7, 1))
    p.valid_to = date(2026, 7, 12)
    db.commit()
    assert resolve_team(db, "n1", "A", date(2026, 7, 10)) == 2   # 구간 내
    assert resolve_team(db, "n1", "A", date(2026, 7, 13)) == 1   # gap → home 캐시 폴백


def test_resolve_for_roster_mid_month_join(seeded):
    """중간 합류(B 7/15~): 월초 구간 없어도 그 달 첫 구간(팀B)을 단일값으로."""
    db = seeded
    set_team_period(db, nurse_id="n1", group_id="B", valid_from=date(2026, 7, 15), team_id=3)
    assert resolve_team_for_roster(db, "n1", "B", 2026, 7) == 3
    # 6월(겹침 없음)엔 B 구간 없음 + home 아님 → None
    assert resolve_team_for_roster(db, "n1", "B", 2026, 6) is None


def test_reset_same_start_updates_in_place(seeded):
    """같은 시작일로 다시 set 하면 새 구간 추가가 아니라 그 구간 갱신."""
    db = seeded
    set_team_period(db, nurse_id="n1", group_id="A", valid_from=date(2026, 7, 1), team_id=2)
    set_team_period(db, nurse_id="n1", group_id="A", valid_from=date(2026, 7, 1), team_id=3)
    cnt = db.query(NurseTeamPeriod).filter(NurseTeamPeriod.nurse_id == "n1").count()
    assert cnt == 1
    assert resolve_team(db, "n1", "A", date(2026, 7, 5)) == 3
