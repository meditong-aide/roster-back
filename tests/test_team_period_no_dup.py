"""set_team_period 완전중복 행 방지(within-txn no-flush 버그).

버그: 프로덕션 SessionLocal 은 autoflush=False. apply_team_ops 가 한 txn 에서 같은
nurse 를 두 번 처리하면(payload 중복 item / 이동을 두 op 로 표현 등) 두 번째
set_team_period 의 same 쿼리가 첫 INSERT(미flush)를 못 봐 동일 행을 또 INSERT
→ (nurse,group,valid_from) 동일한 완전중복 행. 수정: set_team_period 시작에 db.flush().

테스트는 prod 와 동일하게 autoflush 를 끄고(no_autoflush) 같은 시점 두 번 기록한다.
"""
from __future__ import annotations

from datetime import date

import pytest

from db.models import Office, Group, Nurse, NurseTeamPeriod
from services.team_period import set_team_period


@pytest.fixture
def seeded(db):
    db.add(Office(office_id="o1", office_name="병원"))
    db.add(Group(group_id="A", group_name="A병동", office_id="o1"))
    db.add(Nurse(nurse_id="n1", account_id="acc_n1", group_id="A", office_id="o1", name="n1",
                 active=1, allowed_shifts=[], grade=1))
    db.flush()
    return db


def _rows(db):
    return (db.query(NurseTeamPeriod)
            .filter(NurseTeamPeriod.nurse_id == "n1")
            .order_by(NurseTeamPeriod.valid_from.asc()).all())


def test_same_timepoint_twice_in_txn_no_duplicate(seeded):
    """autoflush=False 환경에서 같은 (nurse,group,valid_from) 두 번 기록 → 1행."""
    db = seeded
    vf = date(2026, 7, 1)
    with db.no_autoflush:                      # prod SessionLocal(autoflush=False) 재현
        set_team_period(db, nurse_id="n1", group_id="A", valid_from=vf,
                        team_id=3, source="team_setting", commit=False)
        set_team_period(db, nurse_id="n1", group_id="A", valid_from=vf,
                        team_id=3, source="team_setting", commit=False)
    db.flush()
    rows = _rows(db)
    assert len(rows) == 1, f"중복행 발생: {[(r.valid_from, r.valid_to, r.team_id) for r in rows]}"
    assert rows[0].team_id == 3
    assert rows[0].valid_to is None
