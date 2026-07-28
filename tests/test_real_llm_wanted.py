"""Real LLM E2E test for per-date wanted CRUD.

Tests that gpt-4.1-mini correctly produces tool calls for Korean wanted queries.
Uses SQLite in-memory DB with seed data.

Usage:
    cd roster-back && python -m tests.test_real_llm_wanted
"""

from __future__ import annotations

import sys
import os
import types
from pathlib import Path
from datetime import datetime, date, time

# ── Setup path and env ──
APP_DIR = Path(__file__).resolve().parent.parent / "app"
sys.path.insert(0, str(APP_DIR))

from dotenv import load_dotenv
load_dotenv(Path(__file__).resolve().parent.parent / ".env")

# ── Stub db.client2 ──
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

# ── Import models and seed ──
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

# Patch autoincrement for composite PKs
for t in _tables:
    pks = [c for c in t.columns if c.primary_key]
    if len(pks) > 1:
        for c in pks:
            if c.autoincrement is True:
                c.autoincrement = False

_TestBase.metadata.create_all(bind=_engine, tables=_tables)
_SessionLocal = sessionmaker(bind=_engine)


def _seed(db: Session):
    """Seed test data (same as conftest)."""
    db.add(Office(office_id="OFF001", office_name="테스트병원"))
    db.flush()
    db.add(Group(group_id="GRP001", office_id="OFF001", group_name="9병동"))
    db.flush()

    nurses_data = [
        ("N001", "김민지", 4, 10, 1, "HN", False),
        ("N002", "박지은", 3, 7, 1, None, False),
        ("N003", "이수정", 2, 3, 2, None, False),
    ]
    for nid, name, grade, exp, tid, hn, is_new in nurses_data:
        db.add(Nurse(
            nurse_id=nid, group_id="GRP001", office_id="OFF001",
            account_id=f"acc_{nid}", name=name, grade=grade, experience=exp,
            role="RN", team_id=tid, is_head_nurse=(hn == "HN"), hn_auth=hn,
            active=1, sequence=int(nid[1:]),
            joining_date=datetime(2025, 1, 1) if not is_new else datetime(2026, 3, 1),
            work_shifts=["D", "E", "N"],
            allowed_shifts=[], fixed_shift=None, enable_aide=True,
        ))
    db.flush()

    shifts_data = [
        ("D_GRP001", "D", "데이", "근무", time(7, 0), time(15, 0), 1, True),
        ("E_GRP001", "E", "이브닝", "근무", time(15, 0), time(23, 0), 2, True),
        ("N_GRP001", "N", "나이트", "근무", time(23, 0), time(7, 0), 3, True),
        ("OFF_GRP001", "OFF", "오프", "오프", None, None, 4, False),
        ("V_GRP001", "V", "연차", "휴가", None, None, 5, False),
    ]
    for i, (sid, name, shift_gb, stype, st, et, seq, show) in enumerate(shifts_data):
        db.add(Shift(
            shift_id=sid, office_id="OFF001", group_id="GRP001",
            name=name, shift_gb=shift_gb, type=stype,
            start_time=st, end_time=et, color="#000", sequence=seq,
            allday=0, auto_schedule=1, is_weekly_off=0,
            default_shift=name[0] if name != "오프" else "OFF",
            show_in_preference=show, id=i + 1,
        ))
    db.flush()

    # Wanted campaign
    db.add(Wanted(group_id="GRP001", year=2026, month=5,
                  exp_date=datetime(2026, 5, 20), status="requested"))
    db.flush()

    # N001 submitted wanted for May
    db.add(WantedRequest(
        nurse_id="N001", request_id=1, month="2026-05", group_id="GRP001",
        is_submitted=True, submitted_at=datetime(2026, 4, 10),
    ))
    db.flush()

    for day, shift in [(3, "D"), (10, "N"), (15, "E")]:
        db.add(NurseShiftRequest(
            nurse_id="N001", request_id=1, detailed_request_id=day,
            shift_date=date(2026, 5, day), group_id="GRP001",
            shift=shift, score=1.0,
        ))

    # Pair data for N001
    db.add(NursePairRequest(
        nurse_id="N001", request_id=1, month="2026-05",
        detailed_request_id=1, target_id="N002", group_id="GRP001",
        score=1.5,
        partial_request="같이 근무 선호",
    ))
    db.flush()
    db.commit()


def run_test(db: Session, query: str, nurse_id: str = "N001", nurse_name: str = "김민지"):
    """Run a single query through real LLM agent."""
    from agents_v2.agent_v3 import SchedulingAgent
    from agents_v2.llm_client import get_llm_client
    from agents_v2.schemas.session_context import SessionContext
    from agents_v2.conversation import ConversationStore

    store = ConversationStore()
    conv = store.get_or_create(db, None, user_id=nurse_id, group_id="GRP001")

    ctx = SessionContext(
        office_id="OFF001",
        group_id="GRP001",
        year=2026,
        month=5,
        nurse_id=nurse_id,
        nurse_name=nurse_name,
        user_role="RN",
        conversation_id=conv.id,
        messages=conv.messages,
        pending_approval=conv.pending_approval,
        variable_memory=conv.variable_memory,
    )

    client = get_llm_client("openai")
    agent = SchedulingAgent(client)
    result = agent.run(db, query, ctx)
    return result


def main():
    db = _SessionLocal()
    _seed(db)

    queries = [
        ("내가 올린 5월 3일 원티드 취소해줘", "cancel 날짜별"),
        ("5월 20일에 부모님 병원 방문 사유로 연차 원티드 추가해줘", "add + comment"),
        ("5월 10일 원티드 N을 D로 바꿔줘", "modify shift"),
    ]

    print("=" * 70)
    print("Real LLM E2E Test — Per-date Wanted CRUD")
    print("=" * 70)

    for query, label in queries:
        print(f"\n{'─' * 60}")
        print(f"[{label}] Query: {query}")
        print(f"{'─' * 60}")

        try:
            result = run_test(db, query)
            print(f"  Answer: {result.answer[:300] if result.answer else '(no answer)'}")
            print(f"  Stages: {len(result.trace)}")
            for stage in result.trace:
                s = stage if isinstance(stage, dict) else stage.__dict__ if hasattr(stage, '__dict__') else {}
                print(f"    - {s.get('stage', '?')}: skill={s.get('skill', '')}, tool={s.get('tool_name', '')}")
                if s.get("params"):
                    print(f"      params: {s['params']}")
                if s.get("result"):
                    r = s["result"]
                    if isinstance(r, dict):
                        keys = {k: v for k, v in r.items() if k in ("action", "preview_only", "error", "message", "status")}
                        print(f"      result: {keys}")
            print(f"  Awaiting approval: {result.awaiting_approval}")

            if result.awaiting_approval:
                print(f"  ✅ Preview returned — approval flow working")
            else:
                print(f"  ⚠️ No approval requested")

        except Exception as e:
            print(f"  ❌ Error: {e}")

        # Reset DB for next query (re-seed)
        db.close()
        db = _SessionLocal()
        # Drop and recreate for clean state
        _TestBase.metadata.drop_all(bind=_engine, tables=_tables)
        _TestBase.metadata.create_all(bind=_engine, tables=_tables)
        _seed(db)

    print(f"\n{'=' * 70}")
    print("Done.")
    db.close()


if __name__ == "__main__":
    main()
