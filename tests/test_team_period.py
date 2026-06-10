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


def test_single_ward_invariant_same_date_removes_other_group(seeded):
    """다른 그룹에 같은 날 시작 구간이 있으면 새 구간이 그걸 제거 → 한 시점 한 병동."""
    db = seeded
    set_team_period(db, nurse_id="n1", group_id="A", valid_from=date(2026, 7, 1), team_id=1)
    # 같은 날 B 로 이동 → A 의 7/1 구간은 모순이므로 제거되어야
    set_team_period(db, nurse_id="n1", group_id="B", valid_from=date(2026, 7, 1), team_id=2)
    rows = (db.query(NurseTeamPeriod)
            .filter(NurseTeamPeriod.nurse_id == "n1",
                    NurseTeamPeriod.valid_from == date(2026, 7, 1)).all())
    groups = {r.group_id for r in rows}
    assert groups == {"B"}, [(r.group_id, r.team_id) for r in rows]
    assert resolve_team_for_roster(db, "n1", "B", 2026, 7) == 2


def test_single_ward_invariant_past_open_period_closed(seeded):
    """다른 그룹의 과거-시작 열린 구간은 새 구간 시작일에 닫힌다(영구 stale 방지)."""
    db = seeded
    set_team_period(db, nurse_id="n1", group_id="A", valid_from=date(2026, 7, 1), team_id=1)
    # 8/1 에 B 로 이동 → A 의 [7/1, None) 은 [7/1, 8/1) 로 닫혀야
    set_team_period(db, nurse_id="n1", group_id="B", valid_from=date(2026, 8, 1), team_id=3)
    a_row = (db.query(NurseTeamPeriod)
             .filter(NurseTeamPeriod.nurse_id == "n1", NurseTeamPeriod.group_id == "A",
                     NurseTeamPeriod.valid_from == date(2026, 7, 1)).first())
    assert a_row.valid_to == date(2026, 8, 1)
    # 7월엔 A=1, 8월엔 B=3 / 8월 A 는 닫혀서 period 없음
    assert resolve_team_for_roster(db, "n1", "A", 2026, 7) == 1
    assert resolve_team_for_roster(db, "n1", "B", 2026, 8) == 3


def test_coerce_team_int_normalizes_mssql_str():
    """MSSQL(pymssql)이 nurses.team_id 를 str '2' 로 돌려줘도 int 로 정규화 →
    period(int)/캐시폴백 타입 섞임 방지(프론트 Map 키 매칭 깨짐 버그 차단)."""
    from services.team_period import _coerce_team_int
    assert _coerce_team_int("2") == 2
    assert _coerce_team_int(" 3 ") == 3
    assert _coerce_team_int(1) == 1
    assert _coerce_team_int(None) is None
    assert _coerce_team_int("") is None
    assert _coerce_team_int("전체") is None


def test_apply_team_ops_writes_period_for_month(seeded):
    """팀 설정 모달(B): year/month 주면 add/remove 를 nurse_team_period(valid_from=1일)로 기록."""
    from db.models import Team
    from services.team_service import apply_team_ops
    db = seeded
    db.add(Team(office_id="o1", group_id="A", team_id=1, team_name="1팀", active=1))
    db.add(Team(office_id="o1", group_id="A", team_id=2, team_name="2팀", active=1))
    db.flush()
    # n1 을 team2 로 add (2026-07) → period team2
    apply_team_ops(db, "o1", "A",
                   [{"team_id": 2, "team_name": "2팀", "add": ["n1"], "remove": []}],
                   year=2026, month=7)
    assert resolve_team_for_roster(db, "n1", "A", 2026, 7) == 2
    # remove → 미지정(None) period
    apply_team_ops(db, "o1", "A",
                   [{"team_id": 2, "team_name": "2팀", "add": [], "remove": ["n1"]}],
                   year=2026, month=7)
    assert resolve_team_for_roster(db, "n1", "A", 2026, 7) is None


def test_apply_team_ops_no_month_skips_period(seeded):
    """year/month 없으면(레거시) period 기록 안 함 — 캐시만 갱신."""
    from db.models import Team
    from services.team_service import apply_team_ops
    db = seeded
    db.add(Team(office_id="o1", group_id="A", team_id=2, team_name="2팀", active=1))
    db.flush()
    apply_team_ops(db, "o1", "A",
                   [{"team_id": 2, "team_name": "2팀", "add": ["n1"], "remove": []}])
    # period 행 없음 → ward-aware 폴백(cache team_id=2)
    assert db.query(NurseTeamPeriod).filter(NurseTeamPeriod.nurse_id == "n1").count() == 0


def test_delete_team_clears_team_period(seeded):
    """팀 삭제(apply_team_ops)는 그 team_id 의 nurse_team_period 도 제거 →
    캐시(team_id=None)와 일관, resolve 가 '없는 팀'을 반환하지 않음(고아 방지)."""
    from db.models import Team
    from services.team_service import apply_team_ops

    db = seeded
    db.add(Team(office_id="o1", group_id="A", team_id=2, team_name="2팀", active=1))
    # n1 을 실제 팀2 멤버로(캐시) + 팀2 period
    db.query(Nurse).filter(Nurse.nurse_id == "n1").update({Nurse.team_id: 2})
    set_team_period(db, nurse_id="n1", group_id="A", valid_from=date(2026, 7, 1), team_id=2)
    db.commit()
    assert resolve_team(db, "n1", "A", date(2026, 7, 10)) == 2

    apply_team_ops(db, office_id="o1", group_id="A", payload=[], delete_team_ids=[2])

    # period 행 제거됨 + 캐시 None + resolve None(ward-aware 폴백→캐시 None)
    assert db.query(NurseTeamPeriod).filter(
        NurseTeamPeriod.group_id == "A", NurseTeamPeriod.team_id == 2
    ).count() == 0
    assert db.query(Nurse).filter(Nurse.nurse_id == "n1").first().team_id is None
    assert resolve_team(db, "n1", "A", date(2026, 7, 10)) is None
