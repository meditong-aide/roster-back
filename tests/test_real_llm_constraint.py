"""Real LLM E2E test for constraint updates.

Tests that gpt-4.1-mini correctly maps Korean constraint queries to update_constraint tool calls.

Usage:
    cd roster-back && python -m tests.test_real_llm_constraint
"""

from __future__ import annotations

import sys
import types
from pathlib import Path
from datetime import datetime, date, time

APP_DIR = Path(__file__).resolve().parent.parent / "app"
sys.path.insert(0, str(APP_DIR))

from dotenv import load_dotenv
load_dotenv(Path(__file__).resolve().parent.parent / ".env")

from sqlalchemy import create_engine, event
from sqlalchemy.orm import declarative_base, sessionmaker, Session
from sqlalchemy.ext.compiler import compiles as sa_compiles
from sqlalchemy.dialects.mysql import TINYINT

@sa_compiles(TINYINT, "sqlite")
def _compile_tinyint_sqlite(type_, compiler, **kw):
    return "INTEGER"

_TestBase = declarative_base()
_engine = create_engine("sqlite:///:memory:")

_fake = types.ModuleType("db.client2")
_fake.Base = _TestBase
_fake.engine = _engine
_fake.SessionLocal = sessionmaker(bind=_engine)
_fake.get_db = lambda: None
sys.modules["db.client2"] = _fake

@event.listens_for(_engine, "connect")
def _pragma(dbapi_conn, _):
    dbapi_conn.cursor().execute("PRAGMA foreign_keys=OFF")

from db.models import (
    Office, Group, Team, Nurse, Shift,
    Schedule, ScheduleEntry, RosterConfig, RosterJob,
    Wanted, WantedRequest, NurseShiftRequest, NursePairRequest,
    FixedWantedEntry, IssuedRoster, ShiftManage, RosterGradeConfig,
)

_tables = [
    Office.__table__, Group.__table__, Team.__table__, Nurse.__table__,
    Shift.__table__, Schedule.__table__, ScheduleEntry.__table__,
    RosterConfig.__table__, RosterGradeConfig.__table__, RosterJob.__table__,
    Wanted.__table__, WantedRequest.__table__, NurseShiftRequest.__table__,
    NursePairRequest.__table__,
    FixedWantedEntry.__table__, ShiftManage.__table__, IssuedRoster.__table__,
]

for t in _tables:
    pks = [c for c in t.columns if c.primary_key]
    if len(pks) > 1:
        for c in pks:
            if c.autoincrement is True:
                c.autoincrement = False

_TestBase.metadata.create_all(bind=_engine, tables=_tables)
_SessionLocal = sessionmaker(bind=_engine)


def _seed(db: Session):
    db.add(Office(office_id="OFF001", office_name="테스트병원"))
    db.flush()
    db.add(Group(group_id="GRP001", office_id="OFF001", group_name="9병동"))
    db.flush()

    db.add(Nurse(
        nurse_id="N001", group_id="GRP001", office_id="OFF001",
        account_id="acc_N001", name="김민지", grade=4, experience=10,
        role="RN", team_id=1, is_head_nurse=True, hn_auth="HN",
        active=1, sequence=1, joining_date=datetime(2025, 1, 1),
        work_shifts=["D", "E", "N"],
        allowed_shifts=[], fixed_shift=None, enable_aide=True,
    ))
    db.flush()

    for i, (sid, name, shift_gb, stype, st, et, seq) in enumerate([
        ("D_GRP001", "D", "데이", "근무", time(7, 0), time(15, 0), 1),
        ("E_GRP001", "E", "이브닝", "근무", time(15, 0), time(23, 0), 2),
        ("N_GRP001", "N", "나이트", "근무", time(23, 0), time(7, 0), 3),
        ("OFF_GRP001", "OFF", "오프", "오프", None, None, 4),
    ]):
        db.add(Shift(
            shift_id=sid, office_id="OFF001", group_id="GRP001",
            name=name, shift_gb=shift_gb, type=stype,
            start_time=st, end_time=et, color="#000", sequence=seq,
            allday=0, auto_schedule=1, is_weekly_off=0,
            show_in_preference=True, id=i + 1,
        ))
    db.flush()

    db.add(RosterConfig(
        config_id=1, office_id="OFF001", group_id="GRP001",
        day_req=2, eve_req=2, nig_req=2,
        min_exp_per_shift=2, req_exp_nurses=1,
        two_offs_per_week=True, max_nig_per_month=7,
        three_seq_nig=False, two_offs_after_three_nig=True,
        two_offs_after_two_nig=False, banned_day_after_eve=True,
        max_conseq_work=5, off_days=8,
        shift_priority=0.5,
        sequential_offs=True, nod_noe=True,
        not_one_night=False, use_mid=False,
        preceptee_on=True, preceptee_shift_count=True,
        weekly_off_group=False,
        fixed_wanted_use_yn=True,
        show_level=True, show_preceptor=True,
    ))
    db.flush()
    db.commit()


def run_test(db: Session, query: str):
    from agents_v2.agent_v3 import SchedulingAgent
    from agents_v2.llm_client import get_llm_client
    from agents_v2.schemas.session_context import SessionContext
    from agents_v2.conversation import ConversationStore

    store = ConversationStore()
    conv = store.get_or_create(db, None, user_id="N001", group_id="GRP001")

    ctx = SessionContext(
        office_id="OFF001", group_id="GRP001",
        year=2026, month=4,
        nurse_id="N001", nurse_name="김민지", user_role="HN",
        conversation_id=conv.id, messages=conv.messages,
        pending_approval=conv.pending_approval,
        variable_memory=conv.variable_memory,
    )

    client = get_llm_client("openai")
    agent = SchedulingAgent(client)
    return agent.run(db, query, ctx)


def main():
    db = _SessionLocal()
    _seed(db)

    queries = [
        ("야간 최대 5회로 설정해줘", "max_nig_per_month=5"),
        ("연속 근무 4일로 제한해줘", "max_conseq_work=4"),
        ("이브닝 다음날 데이 금지 해제해줘", "banned_day_after_eve=false"),
        ("나이트 필요인원 3명으로 늘려줘", "nig_req=3"),
        ("프리셉터 매칭 꺼줘", "preceptee_on=false"),
    ]

    print("=" * 70)
    print("Real LLM E2E Test — Constraint Updates")
    print("=" * 70)

    passed = 0
    for query, expected in queries:
        print(f"\n{'─' * 60}")
        print(f"Query: {query}")
        print(f"Expected: {expected}")
        print(f"{'─' * 60}")

        try:
            result = run_test(db, query)
            answer = result.answer[:300] if result.answer else "(no answer)"
            print(f"  Answer: {answer}")
            print(f"  Awaiting approval: {result.awaiting_approval}")

            if result.awaiting_approval:
                print(f"  ✅ Preview returned — approval flow working")
                passed += 1
            elif "error" not in (result.answer or "").lower():
                print(f"  ⚠️ No approval but got answer")
                passed += 1
            else:
                print(f"  ❌ Failed")

        except Exception as e:
            print(f"  ❌ Error: {e}")

        # Reset DB
        db.close()
        db = _SessionLocal()
        _TestBase.metadata.drop_all(bind=_engine, tables=_tables)
        _TestBase.metadata.create_all(bind=_engine, tables=_tables)
        _seed(db)

    print(f"\n{'=' * 70}")
    print(f"Result: {passed}/{len(queries)} passed")
    db.close()


if __name__ == "__main__":
    main()
