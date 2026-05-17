"""Test chat router — no-auth endpoints for testing the agent pipeline.

Provides:
- GET  /agent/test          → Chat UI (Jinja2 template)
- POST /agent/test/chat     → Agent chat (returns full pipeline trace)
- POST /agent/test/setup-db → Initialize SQLite test DB with seed data
- GET  /agent/test/status   → Check DB and LLM status

Usage:
    1. Navigate to http://localhost:8000/agent/test
    2. Select DB mode (test SQLite or production MSSQL)
    3. If using test DB, click "Setup Test DB" button
    4. Start chatting with the agent
"""

from __future__ import annotations

import sys
import types
import logging
from datetime import datetime, date, time
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.templating import Jinja2Templates
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker, Session
from sqlalchemy.ext.compiler import compiles as sa_compiles
from sqlalchemy.dialects.mysql import TINYINT

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/agent/test", tags=["agent_test"])
_TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))

# ── SQLite test DB (lazy init) ─────────────────────────────

_test_engine = None
_test_session_factory = None
_test_db_ready = False


def _ensure_sqlite_compat():
    """Register TINYINT → INTEGER for SQLite."""
    try:
        @sa_compiles(TINYINT, "sqlite")
        def _compile_tinyint(type_, compiler, **kw):
            return "INTEGER"
    except Exception:
        pass  # Already registered


def _get_test_db() -> Session:
    global _test_engine, _test_session_factory
    if _test_engine is None:
        _ensure_sqlite_compat()
        _test_engine = create_engine("sqlite:///:memory:")

        @event.listens_for(_test_engine, "connect")
        def _pragma(dbapi_conn, rec):
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA foreign_keys=OFF")
            cur.close()

        _test_session_factory = sessionmaker(bind=_test_engine)
    return _test_session_factory()


def _get_real_db() -> Session:
    from db.client2 import SessionLocal
    return SessionLocal()


# ── Request / Response models ──────────────────────────────

class TestChatRequest(BaseModel):
    message: str
    year: int = 2026
    month: int = 4
    group_id: str = "GRP001"
    office_id: str = "OFF001"
    user_role: str = "HN"
    nurse_id: str = "N001"
    nurse_name: str = "김민지"
    use_test_db: bool = True
    llm_provider: str = "openai"  # "openai", "anthropic", "deterministic"
    agent_version: str = "v3"
    conversation_id: str | None = None  # multi-turn


class SetupDBRequest(BaseModel):
    pass


# ── Seed data ─────────────────────────────────────────────

def _seed_test_db(db: Session):
    """Seed test DB with sample hospital ward data.

    Mirrors the conftest.py seed_data fixture — uses correct model fields.
    """
    from db.models import (
        Office, Group, Team, Nurse, Shift,
        Schedule, ScheduleEntry, RosterConfig,
        Wanted, WantedRequest, NurseShiftRequest, FixedWantedEntry,
        IssuedRoster, ShiftManage, RosterGradeConfig,
    )

    # Tables
    required_tables = [
        Office.__table__, Group.__table__, Team.__table__, Nurse.__table__,
        Shift.__table__, Schedule.__table__, ScheduleEntry.__table__,
        RosterConfig.__table__, RosterGradeConfig.__table__,
        Wanted.__table__, WantedRequest.__table__, NurseShiftRequest.__table__,
        FixedWantedEntry.__table__, ShiftManage.__table__, IssuedRoster.__table__,
    ]

    # Patch autoincrement for composite PKs
    for table in required_tables:
        pk_cols = [c for c in table.columns if c.primary_key]
        if len(pk_cols) > 1:
            for col in pk_cols:
                if col.autoincrement is True:
                    col.autoincrement = False

    from db.client2 import Base
    Base.metadata.create_all(bind=_test_engine, tables=required_tables)

    # Office & Group
    db.add(Office(office_id="OFF001", office_name="테스트병원"))
    db.flush()
    db.add(Group(group_id="GRP001", office_id="OFF001", group_name="9병동"))
    db.flush()

    # Teams
    db.add_all([
        Team(office_id="OFF001", group_id="GRP001", team_id=1, team_name="A팀"),
        Team(office_id="OFF001", group_id="GRP001", team_id=2, team_name="B팀"),
    ])
    db.flush()

    # Nurses
    nurses_data = [
        ("N001", "김민지", 4, 10, 1, "HN"),
        ("N002", "박지은", 3, 7, 1, None),
        ("N003", "이수정", 2, 3, 2, None),
        ("N004", "정다은", 1, 1, 2, None),
        ("N005", "최유진", 3, 5, 1, None),
        ("N006", "한서연", 2, 2, 2, None),
    ]
    for nid, name, grade, exp, team_id, hn_auth in nurses_data:
        db.add(Nurse(
            nurse_id=nid, group_id="GRP001", office_id="OFF001",
            account_id=f"acc_{nid}", name=name, grade=grade, experience=exp,
            role="RN", team_id=team_id, is_head_nurse=(hn_auth == "HN"),
            hn_auth=hn_auth, active=1, sequence=int(nid[1:]),
            joining_date=datetime(2025, 1, 1),
            is_weekend_off=False, work_shifts=["D", "E", "N"],
            is_night_nurse=[], fixed_shift=None, enable_aide=True,
        ))
    db.flush()

    # Shifts
    shifts_data = [
        ("D_GRP001", "D", "데이", "데이", "근무", time(7, 0), time(15, 0), 1, 0, "D", True),
        ("E_GRP001", "E", "이브닝", "이브닝", "근무", time(15, 0), time(23, 0), 2, 0, "E", True),
        ("N_GRP001", "N", "나이트", "나이트", "근무", time(23, 0), time(7, 0), 3, 0, "N", True),
        ("OFF_GRP001", "OFF", "오프", "오프", "오프", None, None, 4, 0, "OFF", False),
        ("V_GRP001", "V", "연차", "오프", "휴가", None, None, 5, 0, "V", False),
        ("G_GRP001", "공가", "공가", "오프", "공가", None, None, 6, 0, None, False),
        ("M_GRP001", "M", "미드", "데이", "근무", time(10, 0), time(18, 0), 7, 0, "M", True),
        ("WO_GRP001", "WO", "주휴", "오프", "오프", None, None, 8, 1, None, False),
    ]
    for i, (sid, name, shift_gb, _gb2, stype, st, et, seq, is_wo, default, show_pref) in enumerate(shifts_data):
        db.add(Shift(
            shift_id=sid, office_id="OFF001", group_id="GRP001",
            name=name, shift_gb=shift_gb, type=stype,
            start_time=st, end_time=et, color="#000",
            sequence=seq, allday=0, auto_schedule=1,
            is_weekly_off=is_wo, default_shift=default,
            show_in_preference=show_pref, id=i + 1,
        ))
    db.flush()

    # Roster Config
    db.add(RosterConfig(
        config_id=1, config_version="v1", office_id="OFF001", group_id="GRP001",
        day_req=2, eve_req=2, nig_req=2, min_exp_per_shift=2,
        req_exp_nurses=1, two_offs_per_week=True, max_nig_per_month=7,
        three_seq_nig=False, two_offs_after_three_nig=True,
        two_offs_after_two_nig=False, banned_day_after_eve=True,
        max_conseq_work=5, off_days=8, shift_priority=0.5,
        weekend_shift_ratio=0.5, patient_amount=30,
        sequential_offs=True, even_nights=True, nod_noe=True,
        not_one_night=False, use_mid=False, preceptor_gauge=5,
        preceptee_on=True, preceptee_shift_count=True,
        weekly_off_group=False, team_balance_enable=True,
        team_balance_gauge=5, team_balance_mode="balanced",
        off_placement_mode=0, fixed_wanted_use_yn=True,
        show_level=True, show_preceptor=True,
    ))
    db.flush()

    # Schedule (April 2026)
    db.add(Schedule(
        schedule_id="SCH202604V1", office_id="OFF001", group_id="GRP001",
        year=2026, month=4, version=1,
        config_id=1, created_by="acc_N001", status="completed",
        dropped=False, name="4월 근무표 v1",
    ))
    db.flush()

    # Schedule entries (6 nurses × 30 days, rotating pattern)
    shift_ids = ["D_GRP001", "E_GRP001", "N_GRP001", "OFF_GRP001", "D_GRP001", "E_GRP001"]
    nurse_ids = ["N001", "N002", "N003", "N004", "N005", "N006"]
    entry_count = 0
    for day in range(1, 31):
        for i, nid in enumerate(nurse_ids):
            idx = (day + i) % len(shift_ids)
            sid = shift_ids[idx]
            if nid == "N004" and 10 <= day <= 13:
                sid = "N_GRP001"
            if nid == "N001" and day % 7 == 0:
                sid = "OFF_GRP001"
            entry_count += 1
            db.add(ScheduleEntry(
                entry_id=f"E{entry_count:05d}",
                schedule_id="SCH202604V1",
                nurse_id=nid,
                work_date=datetime(2026, 4, day),
                shift_id=sid,
            ))
    db.flush()

    # Wanted campaign
    db.add(Wanted(
        group_id="GRP001", year=2026, month=4,
        exp_date=datetime(2026, 3, 25), status="requested",
    ))
    db.flush()

    # Wanted requests (N001 submitted, N002 submitted, N003 not submitted)
    for nurse_id, is_sub in [("N001", True), ("N002", True), ("N003", False)]:
        db.add(WantedRequest(
            nurse_id=nurse_id, request_id=1, month="2026-04",
            group_id="GRP001",
            is_submitted=is_sub,
            submitted_at=datetime(2026, 3, 20) if is_sub else None,
        ))
    db.flush()

    # Nurse shift requests (detail rows)
    for nurse_id in ["N001", "N002"]:
        for day, shift in [(5, "D"), (10, "N"), (15, "E")]:
            db.add(NurseShiftRequest(
                nurse_id=nurse_id, request_id=1,
                detailed_request_id=day,
                shift_date=date(2026, 4, day),
                group_id="GRP001",
                shift=shift, score=1.0,
            ))
    db.flush()

    # Fixed wanted entries (adjustments)
    adj_entries = [
        ("N001", date(2026, 4, 5), "D_GRP001", True, "original"),
        ("N001", date(2026, 4, 10), "N_GRP001", True, "original"),
        ("N002", date(2026, 4, 15), "OFF_GRP001", False, "modified"),
        ("N003", date(2026, 4, 20), "V_GRP001", True, "added"),
    ]
    for nid, sdate, sid, applied, source in adj_entries:
        db.add(FixedWantedEntry(
            group_id="GRP001", year=2026, month=4,
            nurse_id=nid, shift_date=sdate, shift_id=sid,
            is_applied=applied, source_type=source,
        ))
    db.flush()

    # Shift manage
    for slot, (main, mp) in enumerate([("D", 2), ("E", 2), ("N", 2)], start=1):
        db.add(ShiftManage(
            office_id="OFF001", group_id="GRP001",
            nurse_class="RN", shift_slot=slot,
            main_code=f"{main}_GRP001",
            codes=[f"{main}_GRP001"], manpower=mp,
        ))
    db.flush()

    db.commit()
    return {"status": "seeded", "nurses": len(nurse_ids), "entries": entry_count}


# ── Endpoints ──────────────────────────────────────────────

@router.get("", response_class=HTMLResponse)
async def test_chat_page(request: Request):
    """Serve the test chat UI."""
    return templates.TemplateResponse("agent_test_chat.html", {"request": request})


@router.post("/chat")
def test_chat(req: TestChatRequest):
    """Run agent pipeline with full debug trace. Supports v2 and v3."""
    db = _get_test_db() if req.use_test_db else _get_real_db()

    try:
        return _run_v3(db, req)
    except Exception as e:
        logger.error("Test chat error: %s", e, exc_info=True)
        return {"error": str(e), "pipeline_stages": [], "total_time_ms": 0}
    finally:
        db.close()


def _run_v3(db, req: TestChatRequest) -> dict:
    """v3 pipeline (Routine-aware tool-calling loop)."""
    from agents_v2.agent_v3 import SchedulingAgent
    from agents_v2.conversation import conversation_store
    from agents_v2.llm_client import get_llm_client
    from agents_v2.schemas.session_context import SessionContext

    # Conversation state
    conv = conversation_store.get_or_create(req.conversation_id)

    # Session context
    ctx = SessionContext(
        office_id=req.office_id,
        group_id=req.group_id,
        year=req.year,
        month=req.month,
        nurse_id=req.nurse_id,
        nurse_name=req.nurse_name,
        user_role=req.user_role,
        conversation_id=conv.id,
        messages=conv.messages,
        pending_approval=conv.pending_approval,
        variable_memory=conv.variable_memory,
    )

    # Agent
    client = get_llm_client(req.llm_provider)
    agent = SchedulingAgent(client)
    result = agent.run(db, req.message, ctx)

    # Persist conversation state
    conversation_store.save_messages(conv.id, result.messages)
    conversation_store.save_variable_memory(conv.id, result.variable_memory)
    if result.awaiting_approval:
        conversation_store.set_pending_approval(conv.id, result.preview)
    else:
        conversation_store.set_pending_approval(conv.id, None)

    # Response
    resp = result.to_dict()
    resp["conversation_id"] = conv.id
    resp["agent_version"] = "v3"
    return resp


@router.post("/setup-db")
def setup_test_db():
    """Initialize SQLite test DB with seed data."""
    global _test_db_ready, _test_engine

    db = _get_test_db()
    try:
        from db.models import (
            Office, Group, Team, Nurse, Shift,
            Schedule, ScheduleEntry, RosterConfig, RosterJob,
            Wanted, WantedRequest, NurseShiftRequest, FixedWantedEntry,
            IssuedRoster, ShiftManage, RosterGradeConfig,
        )
        _tables = [
            Office.__table__, Group.__table__, Team.__table__, Nurse.__table__,
            Shift.__table__, Schedule.__table__, ScheduleEntry.__table__,
            RosterConfig.__table__, RosterGradeConfig.__table__, RosterJob.__table__,
            Wanted.__table__, WantedRequest.__table__, NurseShiftRequest.__table__,
            FixedWantedEntry.__table__, ShiftManage.__table__, IssuedRoster.__table__,
        ]
        # Patch autoincrement for SQLite composite PKs
        for table in _tables:
            pk_cols = [c for c in table.columns if c.primary_key]
            if len(pk_cols) > 1:
                for col in pk_cols:
                    if col.autoincrement is True:
                        col.autoincrement = False

        # Drop and recreate for a clean slate
        from db.models import Base
        Base.metadata.drop_all(bind=_test_engine, tables=_tables)
        Base.metadata.create_all(bind=_test_engine, tables=_tables)

        result = _seed_test_db(db)
        _test_db_ready = True
        return {"status": "seeded", **result}
    except Exception as e:
        logger.error("Setup DB error: %s", e, exc_info=True)
        return {"status": "error", "message": str(e)}
    finally:
        db.close()


@router.get("/status")
def test_status():
    """Check DB and LLM availability."""
    import os

    return {
        "test_db_ready": _test_db_ready,
        "openai_configured": bool(os.getenv("OPENAI_API_KEY")),
        "anthropic_configured": bool(os.getenv("ANTHROPIC_API_KEY")),
        "available_providers": (
            ["deterministic"]
            + (["openai"] if os.getenv("OPENAI_API_KEY") else [])
            + (["anthropic"] if os.getenv("ANTHROPIC_API_KEY") else [])
        ),
    }


# ── Identity lookup endpoints ─────────────────────────────


@router.get("/offices")
def list_offices(use_test_db: bool = True):
    """List all offices."""
    from db.models import Office
    db = _get_test_db() if use_test_db else _get_real_db()
    try:
        rows = db.query(Office).all()
        return [
            {"office_id": r.office_id, "office_name": r.office_name}
            for r in rows
        ]
    except Exception:
        return []
    finally:
        db.close()


@router.get("/groups")
def list_groups(office_id: str, use_test_db: bool = True):
    """List groups for an office."""
    from db.models import Group
    db = _get_test_db() if use_test_db else _get_real_db()
    try:
        rows = (
            db.query(Group)
            .filter(Group.office_id == office_id)
            .all()
        )
        return [
            {"group_id": r.group_id, "group_name": r.group_name}
            for r in rows
        ]
    except Exception:
        return []
    finally:
        db.close()


@router.get("/nurses")
def list_nurses(group_id: str, use_test_db: bool = True):
    """List active nurses in a group (for identity selection)."""
    from db.models import Nurse
    db = _get_test_db() if use_test_db else _get_real_db()
    try:
        rows = (
            db.query(Nurse)
            .filter(Nurse.group_id == group_id, Nurse.active == 1)
            .order_by(Nurse.sequence)
            .all()
        )
        return [
            {
                "nurse_id": r.nurse_id,
                "name": r.name,
                "grade": r.grade,
                "is_head_nurse": bool(r.is_head_nurse),
                "hn_auth": r.hn_auth,
                "role": r.role,
                "team_id": r.team_id,
            }
            for r in rows
        ]
    except Exception:
        return []
    finally:
        db.close()
