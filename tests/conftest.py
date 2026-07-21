"""Test configuration — SQLite in-memory DB with real model data.

Patches db.client2 before models are imported, so all SQLAlchemy
models use the test engine instead of production MSSQL.
"""

from __future__ import annotations

import sys
import types
from datetime import datetime, date, time
from pathlib import Path

import pytest
from sqlalchemy import create_engine, func, event, Integer
from sqlalchemy.orm import sessionmaker, Session
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.dialects.mysql import TINYINT
from sqlalchemy.pool import StaticPool

# ── Add app/ to path FIRST so 'db' package is findable ──────────
APP_DIR = Path(__file__).resolve().parent.parent / "app"
sys.path.insert(0, str(APP_DIR))

# ── Patch db.client2 before anything imports models ──────────────
# We need to pre-create a fake db.client2 module with a SQLite Base
# BEFORE the real db.client2 tries to import pymssql.

# ── Register MySQL TINYINT → SQLite INTEGER mapping ──────
from sqlalchemy.ext.compiler import compiles as sa_compiles

@sa_compiles(TINYINT, "sqlite")
def _compile_tinyint_sqlite(type_, compiler, **kw):
    return "INTEGER"

_TestBase = declarative_base()
_test_engine = create_engine(
    "sqlite:///:memory:",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)

_fake_client2 = types.ModuleType("db.client2")
_fake_client2.Base = _TestBase
_fake_client2.engine = _test_engine
_fake_client2.SessionLocal = sessionmaker(bind=_test_engine)
_fake_client2.get_db = lambda: None
_fake_client2.msdb_manager = None

# Register the fake client2 in sys.modules.
# The real 'db' package directory exists at app/db/, so Python will find
# 'db' as a package. We just intercept 'db.client2' specifically.
sys.modules["db.client2"] = _fake_client2

# Now we can safely import models (they'll use our patched Base)
from db.models import (  # noqa: E402
    Office, Group, Team, Nurse, Shift,
    Schedule, ScheduleEntry, RosterConfig, RosterJob,
    Wanted, WantedRequest, NurseShiftRequest, NursePairRequest, FixedWantedEntry,
    IssuedRoster, ShiftManage, DailyShift, RosterGradeConfig, NurseMonthlyLimit, NurseAssignment,
    NurseTeamPeriod, WeeklyOffSetting,
    NurseGradePeriod, NurseAllowedShiftPeriod, NurseWeekendOffPeriod,
    NursePrecepteePeriod, NurseMutualExclusionPeriod,
    AgentConversation, AgentConversationMessage, AgentUserMemory,
    AgentMemoryAudit, AgentSkillInvocation,
)


# ── SQLite compatibility: TINYINT = INTEGER ──────────────
# SQLite doesn't have TINYINT, but SQLAlchemy handles type mapping.
# Enable FK support for SQLite.
@event.listens_for(_test_engine, "connect")
def _set_sqlite_pragma(dbapi_connection, connection_record):
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA foreign_keys=OFF")  # OFF for easier test data insertion
    cursor.close()


# ── Fixtures ──────────────────────────────────────────────


# Tables required for agent tests (skip models with SQLite-incompatible composite PKs)
_REQUIRED_TABLES = [
    Office.__table__, Group.__table__, Team.__table__, Nurse.__table__,
    Shift.__table__, Schedule.__table__, ScheduleEntry.__table__,
    RosterConfig.__table__, RosterGradeConfig.__table__, RosterJob.__table__,
    Wanted.__table__, WantedRequest.__table__, NurseShiftRequest.__table__,
    NursePairRequest.__table__,
    FixedWantedEntry.__table__, ShiftManage.__table__, DailyShift.__table__, IssuedRoster.__table__,
    NurseMonthlyLimit.__table__, NurseAssignment.__table__, NurseTeamPeriod.__table__,
    WeeklyOffSetting.__table__,
    NurseGradePeriod.__table__, NurseAllowedShiftPeriod.__table__,
    NurseWeekendOffPeriod.__table__, NursePrecepteePeriod.__table__,
    NurseMutualExclusionPeriod.__table__,
    AgentConversation.__table__, AgentConversationMessage.__table__,
    AgentUserMemory.__table__,
    AgentMemoryAudit.__table__,
    AgentSkillInvocation.__table__,
]


def _patch_autoincrement_for_sqlite():
    """SQLite doesn't support autoincrement on composite PKs.
    Disable autoincrement on any composite-PK table before create_all.
    """
    for table in _REQUIRED_TABLES:
        pk_cols = [c for c in table.columns if c.primary_key]
        if len(pk_cols) > 1:
            for col in pk_cols:
                if col.autoincrement is True:
                    col.autoincrement = False


@pytest.fixture(scope="session")
def engine():
    """Create only required tables once per test session."""
    _patch_autoincrement_for_sqlite()
    _TestBase.metadata.create_all(bind=_test_engine, tables=_REQUIRED_TABLES)
    yield _test_engine
    _TestBase.metadata.drop_all(bind=_test_engine, tables=_REQUIRED_TABLES)


@pytest.fixture
def db(engine) -> Session:
    """Provide a transactional DB session that rolls back after each test."""
    connection = engine.connect()
    transaction = connection.begin()
    session = Session(bind=connection)

    yield session

    session.close()
    transaction.rollback()
    connection.close()


@pytest.fixture
def seed_data(db: Session) -> dict:
    """Seed a complete hospital ward dataset for E2E testing.

    Creates:
    - 1 office, 1 group, 2 teams
    - 6 nurses (김민지, 박지은, 이수정, 정다은, 최유진, 한서연) with varying grades
    - 8 shift definitions (D, E, N, OFF, V, 연차, 공가, M)
    - 1 schedule with 30 days × 6 nurses = 180 entries
    - Wanted campaign with submissions and adjustments
    - Roster config with constraints
    """
    # ── Office & Group ──
    office = Office(office_id="OFF001", office_name="테스트병원")
    db.add(office)
    db.flush()

    group = Group(group_id="GRP001", office_id="OFF001", group_name="9병동")
    db.add(group)
    db.flush()

    # ── Teams ──
    team_a = Team(office_id="OFF001", group_id="GRP001", team_id=1, team_name="A팀")
    team_b = Team(office_id="OFF001", group_id="GRP001", team_id=2, team_name="B팀")
    db.add_all([team_a, team_b])
    db.flush()

    # ── Nurses ──
    nurses_data = [
        ("N001", "김민지", 4, 10, 1, "HN", False),
        ("N002", "박지은", 3, 7, 1, None, False),
        ("N003", "이수정", 2, 3, 2, None, False),
        ("N004", "정다은", 1, 1, 2, None, True),   # 신규
        ("N005", "최유진", 3, 5, 1, None, False),
        ("N006", "한서연", 2, 2, 2, None, True),    # 신규, preceptor=N002
    ]
    nurses = {}
    for nid, name, grade, exp, team_id, hn_auth, is_new in nurses_data:
        nurse = Nurse(
            nurse_id=nid,
            group_id="GRP001",
            office_id="OFF001",
            account_id=f"acc_{nid}",
            name=name,
            grade=grade,
            experience=exp,
            role="RN",
            team_id=team_id,
            is_head_nurse=(hn_auth == "HN"),
            hn_auth=hn_auth,
            active=1,
            sequence=int(nid[1:]),
            joining_date=datetime(2025, 1, 1) if not is_new else datetime(2026, 3, 1),
            preceptor_id="N002" if nid == "N006" else None,
            is_weekend_off=False,
            work_shifts=["D", "E", "N"],
            allowed_shifts=[],
            fixed_shift=None,
            enable_aide=True,
        )
        db.add(nurse)
        nurses[nid] = nurse
    db.flush()

    # ── Shifts ──
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
    shifts = {}
    for i, (sid, name, shift_gb, _gb2, stype, st, et, seq, is_wo, default, show_pref) in enumerate(shifts_data):
        shift = Shift(
            shift_id=sid,
            office_id="OFF001",
            group_id="GRP001",
            name=name,
            shift_gb=shift_gb,
            type=stype,
            start_time=st,
            end_time=et,
            color="#000",
            sequence=seq,
            allday=0,
            auto_schedule=1,
            is_weekly_off=is_wo,
            default_shift=default,
            show_in_preference=show_pref,
            id=i + 1,
        )
        db.add(shift)
        shifts[name] = shift
    db.flush()

    # ── Roster Config ──
    config = RosterConfig(
        config_id=1,
        office_id="OFF001",
        group_id="GRP001",
        day_req=2,
        eve_req=2,
        nig_req=2,
        min_exp_per_shift=2,
        req_exp_nurses=1,
        two_offs_per_week=True,
        max_nig_per_month=7,
        three_seq_nig=False,
        two_offs_after_three_nig=True,
        two_offs_after_two_nig=False,
        banned_day_after_eve=True,
        max_conseq_work=5,
        off_days=8,
        shift_priority=0.5,
        sequential_offs=True,
        nod_noe=True,
        not_one_night=False,
        use_mid=False,
        preceptee_on=True,
        preceptee_shift_count=True,
        weekly_off_group=False,
        fixed_wanted_use_yn=True,
        show_level=True,
        show_preceptor=True,
    )
    db.add(config)
    db.flush()

    # ── Schedule (April 2026) ──
    schedule = Schedule(
        schedule_id="SCH202604V1",
        office_id="OFF001",
        group_id="GRP001",
        year=2026,
        month=4,
        version=1,
        config_id=1,
        created_by="acc_N001",
        status="completed",
        dropped=False,
        name="4월 근무표 v1",
    )
    db.add(schedule)
    db.flush()

    # ── Schedule Entries (30 days × 6 nurses) ──
    # Assign shifts in a rotating pattern
    shift_ids = ["D_GRP001", "E_GRP001", "N_GRP001", "OFF_GRP001", "D_GRP001", "E_GRP001"]
    entry_count = 0
    for day in range(1, 31):
        for i, nid in enumerate(["N001", "N002", "N003", "N004", "N005", "N006"]):
            # Rotate shifts, ensure some nights for N003/N004
            idx = (day + i) % len(shift_ids)
            sid = shift_ids[idx]
            # Give N004 consecutive nights on days 10-13 (for pattern detection test)
            if nid == "N004" and 10 <= day <= 13:
                sid = "N_GRP001"
            # Give N001 more OFF for fairness test
            if nid == "N001" and day % 7 == 0:
                sid = "OFF_GRP001"
            entry_count += 1
            entry = ScheduleEntry(
                entry_id=f"E{entry_count:05d}",
                schedule_id="SCH202604V1",
                nurse_id=nid,
                work_date=datetime(2026, 4, day),
                shift_id=sid,
            )
            db.add(entry)
    db.flush()

    # ── Wanted Campaign (April 2026) ──
    wanted = Wanted(
        group_id="GRP001",
        year=2026,
        month=4,
        exp_date=datetime(2026, 3, 25),
        status="requested",
    )
    db.add(wanted)
    db.flush()

    # ── Wanted Requests ──
    # N001 (김민지) submitted, N002 (박지은) submitted, N003 not submitted
    for nurse_id, is_sub in [("N001", True), ("N002", True), ("N003", False)]:
        wr = WantedRequest(
            nurse_id=nurse_id,
            request_id=1,
            month="2026-04",
            group_id="GRP001",
            is_submitted=is_sub,
            submitted_at=datetime(2026, 3, 20) if is_sub else None,
        )
        db.add(wr)
    db.flush()

    # ── Nurse Shift Requests (detail rows for submitted nurses) ──
    for nurse_id in ["N001", "N002"]:
        for day, shift in [(5, "D"), (10, "N"), (15, "E")]:
            nsr = NurseShiftRequest(
                nurse_id=nurse_id,
                request_id=1,
                detailed_request_id=day,
                shift_date=date(2026, 4, day),
                group_id="GRP001",
                shift=shift,
                score=1.0,
            )
            db.add(nsr)
    db.flush()

    # ── Nurse Pair Requests (pair preferences for N001) ──
    pair1 = NursePairRequest(
        nurse_id="N001",
        request_id=1,
        month="2026-04",
        detailed_request_id=1,
        target_id="N002",
        group_id="GRP001",
        score=1.5,
        partial_request="같이 근무 선호",
    )
    pair2 = NursePairRequest(
        nurse_id="N001",
        request_id=1,
        month="2026-04",
        detailed_request_id=2,
        target_id="N004",
        group_id="GRP001",
        score=-1.0,
        partial_request="비선호",
    )
    db.add_all([pair1, pair2])
    db.flush()

    # ── Fixed Wanted Entries (adjustments) ──
    adj_entries = [
        ("N001", date(2026, 4, 5), "D_GRP001", True, "original"),
        ("N001", date(2026, 4, 10), "N_GRP001", True, "original"),
        ("N002", date(2026, 4, 15), "OFF_GRP001", False, "modified"),  # is_applied=False
        ("N003", date(2026, 4, 20), "V_GRP001", True, "added"),
    ]
    for nid, sdate, sid, applied, source in adj_entries:
        fwe = FixedWantedEntry(
            group_id="GRP001",
            year=2026,
            month=4,
            nurse_id=nid,
            shift_date=sdate,
            shift_id=sid,
            is_applied=applied,
            source_type=source,
        )
        db.add(fwe)
    db.flush()

    # ── ShiftManage ──
    for slot, (main, mp) in enumerate(
        [("D", 2), ("E", 2), ("N", 2)], start=1
    ):
        sm = ShiftManage(
            office_id="OFF001",
            group_id="GRP001",
            nurse_class="RN",
            shift_slot=slot,
            main_code=f"{main}_GRP001",
            codes=[f"{main}_GRP001"],
            manpower=mp,
        )
        db.add(sm)
    db.flush()

    db.commit()

    return {
        "office_id": "OFF001",
        "group_id": "GRP001",
        "year": 2026,
        "month": 4,
        "schedule_id": "SCH202604V1",
        "nurse_ids": ["N001", "N002", "N003", "N004", "N005", "N006"],
        "nurse_names": {
            "N001": "김민지", "N002": "박지은", "N003": "이수정",
            "N004": "정다은", "N005": "최유진", "N006": "한서연",
        },
    }


# ── LLM Test Adapter ──────────────────────────────────────


class DeterministicLLM:
    """Deterministic LLM adapter for testing.

    Returns predictable JSON responses based on input patterns.
    This is NOT a mock of our code — it's a test double for the
    external LLM service, which is standard practice for E2E tests.
    """

    def __call__(self, system_prompt: str, user_prompt: str) -> str:
        """Route to appropriate response based on user_prompt content."""
        import json

        # Concept typing requests
        if "Extract typed fragments" in user_prompt:
            return self._classify(user_prompt)

        # Canonicalization requests
        if "Determine scope, subject, and operation" in user_prompt:
            return self._canonicalize(user_prompt)

        # Answer generation
        if "자연스럽고 간결한 한국어 답변" in user_prompt:
            return "테스트 결과입니다."

        return "{}"

    def _classify(self, prompt: str) -> str:
        import json

        # Extract the user message from the prompt
        msg = ""
        if 'User message: "' in prompt:
            msg = prompt.split('User message: "')[1].split('"')[0]

        fragments = []

        # Entity detection
        names = ["김민지", "박지은", "이수정", "정다은", "최유진", "한서연"]
        for name in names:
            if name in msg:
                fragments.append({"text": name, "type": "entity"})

        # Temporal detection
        temporal_tokens = {
            "야간": "temporal", "나이트": "temporal", "아침": "temporal",
            "이브닝": "temporal", "주말": "temporal", "다음주": "temporal",
        }
        for token, ttype in temporal_tokens.items():
            if token in msg:
                fragments.append({"text": token, "type": ttype})

        # Lexical detection
        lexical_tokens = ["D", "E", "N", "OFF", "V", "연차", "조정판", "확정본"]
        for token in lexical_tokens:
            if token in msg and not any(f["text"] == token for f in fragments):
                fragments.append({"text": token, "type": "lexical"})

        # Status detection
        status_tokens = ["쉬는", "근무자", "휴가자", "미제출", "제출", "신규"]
        for token in status_tokens:
            if token in msg:
                fragments.append({"text": token + ("사람" if "사람" in msg and token == "쉬는" else ""), "type": "status"})

        # Workflow detection
        workflow_tokens = {
            "적용 안": "적용 안되게", "변경": "변경해줘", "수정": "수정",
            "취소": "취소해줘", "조회": "조회", "보여": "보여줘",
            "생성": "생성해", "재생성": "재생성",
        }
        for trigger, text in workflow_tokens.items():
            if trigger in msg:
                fragments.append({"text": text, "type": "workflow"})

        # If nothing matched, treat the whole thing as workflow
        if not fragments:
            fragments.append({"text": msg, "type": "workflow"})

        return json.dumps(fragments, ensure_ascii=False)

    def _canonicalize(self, prompt: str) -> str:
        import json

        # Parse the grounded concepts from the prompt
        lower = prompt.lower()

        # Determine scope
        scope = "draft_schedule"
        if "wanted" in lower or "원티드" in lower:
            scope = "wanted_submissions"
        if "adjustment" in lower or "조정" in lower:
            scope = "wanted_adjustment"
        if "constraint" in lower or "config" in lower:
            scope = "constraint_config"
        if "nurse_info" in lower or "nurse_attr" in lower:
            scope = "nurse_info"

        # Determine operation
        operation = "list"
        if "cancel" in lower or "취소" in lower:
            operation = "cancel"
        if "set_field" in lower or "update" in lower or "변경" in lower or "수정" in lower:
            operation = "bulk_update"
        if "generate" in lower or "생성" in lower:
            operation = "generate"
        if "count" in lower or "몇" in lower:
            operation = "count"

        # Determine subject
        subject = "schedule_row"
        if "nurse" in lower or "person" in lower:
            subject = "person"
        if "request" in lower or "wanted" in lower:
            subject = "request_row"

        return json.dumps({
            "scope": scope,
            "subject": subject,
            "operation": operation,
        }, ensure_ascii=False)


@pytest.fixture
def llm():
    """Provide a deterministic LLM adapter."""
    return DeterministicLLM()


@pytest.fixture
def session_context(seed_data):
    """Standard session context for tests."""
    return {
        "group_id": seed_data["group_id"],
        "office_id": seed_data["office_id"],
        "year": seed_data["year"],
        "month": seed_data["month"],
        "user_role": "HN",
        "nurse_id": "N001",
    }
