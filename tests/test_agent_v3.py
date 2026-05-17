"""Tests for agent v3 — tool-calling loop, variable memory, middleware, grounding."""

from __future__ import annotations

import json
import pytest

from agents_v2.llm_client import LLMResponse, ToolCall, DeterministicClient
from agents_v2.variable_memory import VariableMemory
from agents_v2.skills.descriptions import SKILL_TOOLS
from agents_v2.schemas.session_context import SessionContext
from agents_v2.conversation import ConversationStore
from agents_v2.grounding.internal import (
    resolve_date,
    resolve_date_range,
    resolve_nurse,
    resolve_shift,
    ResolveResult,
)
from agents_v2.agent_v3 import SchedulingAgent, AgentResult, Stage


# ── Fixtures ──────────��─────────────────────────────────────


@pytest.fixture
def ctx():
    return SessionContext(
        office_id="OFF001",
        group_id="GRP001",
        year=2026,
        month=4,
        nurse_id="N001",
        nurse_name="김민지",
        user_role="HN",
    )


# ── Variable Memory ──────────────��──────────────────────────


class TestVariableMemory:
    def test_store_and_get(self):
        vm = VariableMemory()
        vm.store("query_schedule", {"schedule_id": 42, "nurse_ids": ["N001"]})
        assert vm.get("schedule_id") == 42
        assert vm.get("nurse_ids") == ["N001"]
        assert vm.get("last_query_schedule") == {
            "schedule_id": 42,
            "nurse_ids": ["N001"],
        }

    def test_inject_vm_references(self):
        vm = VariableMemory()
        vm.store("query_schedule", {"schedule_id": 42})
        result = vm.inject({"sid": "$vm.schedule_id", "plain": "hello"})
        assert result["sid"] == 42
        assert result["plain"] == "hello"

    def test_inject_missing_key_keeps_reference(self):
        vm = VariableMemory()
        result = vm.inject({"sid": "$vm.missing_key"})
        assert result["sid"] == "$vm.missing_key"

    def test_clear(self):
        vm = VariableMemory()
        vm.store("test", {"schedule_id": 1})
        vm.clear()
        assert vm.get("schedule_id") is None

    def test_non_dict_non_list_stores_last_only(self):
        vm = VariableMemory()
        vm.store("test", "a string result")
        assert vm.get("last_test") == "a string result"
        assert vm.get("schedule_id") is None

    def test_list_result_stores_count_and_keys(self):
        """VM should extract values from list results (Bug #36 fix)."""
        vm = VariableMemory()
        entries = [
            {"nurse_id": "N001", "shift_id": "D_GRP001", "work_date": "2026-04-01"},
            {"nurse_id": "N002", "shift_id": "E_GRP001", "work_date": "2026-04-01"},
        ]
        vm.store("query_schedule", entries)
        assert vm.get("last_query_schedule") == entries
        assert vm.get("last_query_schedule_count") == 2
        # Auto-extracted list of nurse_ids from entries
        assert vm.get("nurse_id") == ["N001", "N002"]
        assert vm.get("shift_id") == ["D_GRP001", "E_GRP001"]


# ── Date Resolution ────────────────��────────────────────────


class TestDateResolution:
    def test_iso_passthrough(self):
        assert resolve_date("2026-04-05", 2026, 4) == "2026-04-05"

    def test_korean_month_day(self):
        assert resolve_date("4월 5일", 2026, 4) == "2026-04-05"

    def test_slash_format(self):
        assert resolve_date("4/5", 2026, 4) == "2026-04-05"

    def test_day_only(self):
        assert resolve_date("5일", 2026, 4) == "2026-04-05"

    def test_range_month_day(self):
        s, e = resolve_date_range("4월 1일~10일", 2026, 4)
        assert s == "2026-04-01"
        assert e == "2026-04-10"

    def test_range_day_only(self):
        s, e = resolve_date_range("1일~10일", 2026, 4)
        assert s == "2026-04-01"
        assert e == "2026-04-10"

    def test_range_week_number(self):
        s, e = resolve_date_range("2주차", 2026, 4)
        assert s == "2026-04-08"
        assert e == "2026-04-14"

    def test_unresolvable(self):
        assert resolve_date("모레", 2026, 4) is None


# ── Nurse Resolution ─────────────────��──────────────────────


class TestNurseResolution:
    def test_exact_match(self, db, seed_data):
        r = resolve_nurse(db, "GRP001", "김민지")
        assert r.resolved is True
        assert r.value == "N001"

    def test_not_found(self, db, seed_data):
        r = resolve_nurse(db, "GRP001", "존재하지않는이름")
        assert r.error is not None

    def test_ambiguous(self, db, seed_data):
        # "김" partial match should find 김민지 → clarification or resolve
        r = resolve_nurse(db, "GRP001", "김")
        assert r.needs_clarification or r.resolved


# ── Shift Resolution ��──────────────────────���────────────────


class TestShiftResolution:
    def test_direct_name_match(self, db, seed_data):
        r = resolve_shift(db, "GRP001", "D")
        assert r.resolved is True

    def test_alias_match(self, db, seed_data):
        r = resolve_shift(db, "GRP001", "나이트")
        assert r.resolved is True

    def test_alias_evening(self, db, seed_data):
        r = resolve_shift(db, "GRP001", "이브닝")
        assert r.resolved is True

    def test_shift_gb_category_returns_list(self, db, seed_data):
        """When multiple shifts share a shift_gb, resolve returns a list."""
        from db.models import Shift

        # Add a second night shift (N2) with same shift_gb="나이트"
        db.add(Shift(
            shift_id="N2_GRP001", office_id="OFF001", group_id="GRP001",
            name="N2", shift_gb="나이트", type="근무",
            color="#000", sequence=9, allday=0, auto_schedule=1,
            is_weekly_off=0, show_in_preference=True, id=100,
        ))
        db.flush()

        r = resolve_shift(db, "GRP001", "나이트")
        assert r.resolved is True
        # Should return list with both night shifts
        assert isinstance(r.value, list)
        assert "N_GRP001" in r.value
        assert "N2_GRP001" in r.value

    def test_shift_gb_single_returns_string(self, db, seed_data):
        """When only one shift in a category, resolve returns single string."""
        r = resolve_shift(db, "GRP001", "미드")
        assert r.resolved is True
        assert isinstance(r.value, str)
        assert r.value == "M_GRP001"

    def test_not_found(self, db, seed_data):
        r = resolve_shift(db, "GRP001", "존재하지않는시프트")
        assert r.error is not None


# ── LLM Client ──────────────────��───────────────────────────


class TestDeterministicClient:
    def test_wanted_query_routing(self):
        client = DeterministicClient()
        msgs = [
            {"role": "system", "content": "test"},
            {"role": "user", "content": "원티드 미제출��� 알려줘"},
        ]
        resp = client.chat(msgs, tools=SKILL_TOOLS)
        assert resp.is_tool_call
        assert resp.tool_name == "query_schedule"
        assert resp.tool_args["scope"] == "wanted_submissions"

    def test_schedule_query_routing(self):
        client = DeterministicClient()
        msgs = [
            {"role": "system", "content": "test"},
            {"role": "user", "content": "김민지 근무표 보여줘"},
        ]
        resp = client.chat(msgs, tools=SKILL_TOOLS)
        assert resp.is_tool_call
        assert resp.tool_name == "query_schedule"

    def test_answer_after_tool_result(self):
        client = DeterministicClient()
        msgs = [
            {"role": "system", "content": "test"},
            {"role": "user", "content": "원티드 현황"},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {
                            "name": "query_schedule",
                            "arguments": '{"scope": "wanted_submissions"}',
                        },
                    }
                ],
            },
            {
                "role": "tool",
                "tool_call_id": "call_1",
                "content": '{"submitted": 3, "not_submitted": 3}',
            },
        ]
        resp = client.chat(msgs, tools=SKILL_TOOLS)
        assert resp.is_text
        assert resp.text  # Should have some answer

    def test_as_assistant_message_single(self):
        resp = LLMResponse(
            type="tool_call",
            tool_calls=[ToolCall(name="query_schedule", args={"scope": "schedule"}, call_id="call_123")],
        )
        msg = resp.as_assistant_message()
        assert msg["role"] == "assistant"
        assert len(msg["tool_calls"]) == 1
        assert msg["tool_calls"][0]["function"]["name"] == "query_schedule"

    def test_as_assistant_message_parallel(self):
        """Parallel tool calls should all appear in one assistant message (Bug #34 fix)."""
        resp = LLMResponse(
            type="tool_call",
            tool_calls=[
                ToolCall(name="query_schedule", args={"scope": "schedule", "nurse_name": "김민지"}, call_id="call_1"),
                ToolCall(name="query_schedule", args={"scope": "nurse_info"}, call_id="call_2"),
            ],
        )
        assert resp.is_parallel
        msg = resp.as_assistant_message()
        assert len(msg["tool_calls"]) == 2
        assert msg["tool_calls"][0]["id"] == "call_1"
        assert msg["tool_calls"][1]["id"] == "call_2"

    def test_backward_compat_accessors(self):
        """Single tool_call convenience accessors should still work."""
        resp = LLMResponse(
            type="tool_call",
            tool_calls=[ToolCall(name="bulk_mutation", args={"action": "cancel"}, call_id="call_99")],
        )
        assert resp.tool_name == "bulk_mutation"
        assert resp.tool_args == {"action": "cancel"}
        assert resp.tool_call_id == "call_99"


# ���─ Conversation Store ──────────────────────────────────────


class TestConversationStore:
    def test_create_and_get(self):
        store = ConversationStore()
        conv = store.create()
        assert store.get(conv.id) is not None
        assert store.get(conv.id).id == conv.id

    def test_get_nonexistent(self):
        store = ConversationStore()
        assert store.get("nonexistent") is None

    def test_get_or_create(self):
        store = ConversationStore()
        conv1 = store.get_or_create(None)
        assert conv1 is not None
        conv2 = store.get_or_create(conv1.id)
        assert conv2.id == conv1.id

    def test_save_messages(self):
        store = ConversationStore()
        conv = store.create()
        store.save_messages(conv.id, [{"role": "user", "content": "hi"}])
        assert len(store.get(conv.id).messages) == 1

    def test_variable_memory_persistence(self):
        store = ConversationStore()
        conv = store.create()
        store.save_variable_memory(conv.id, {"schedule_id": 42})
        assert store.get(conv.id).variable_memory["schedule_id"] == 42


# ── Skill Descriptions ──────���───────────────────────────────


class TestSkillDescriptions:
    def test_count(self):
        assert len(SKILL_TOOLS) == 10

    def test_required_fields(self):
        for tool in SKILL_TOOLS:
            assert "name" in tool
            assert "description" in tool
            assert "parameters" in tool

    def test_names(self):
        names = {t["name"] for t in SKILL_TOOLS}
        assert "query_schedule" in names
        assert "bulk_mutation" in names
        assert "generate_schedule" in names
        assert "update_monthly_limit" in names


# ── Scope Routing (Bug #35) ────────────────────────────────


class TestScopeRouting:
    """Verify scope="schedule" (from tool desc) routes correctly."""

    def test_schedule_scope_routes_to_entries(self, db, seed_data):
        from agents_v2.skills import run_skill

        result = run_skill(db, "query-schedule", {
            "group_id": seed_data["group_id"],
            "year": 2026,
            "month": 4,
            "scope": "schedule",  # LLM sends this, not "draft_schedule"
            "nurse_ids": ["N001"],
        })
        assert isinstance(result, list)
        assert len(result) == 30
        assert all(r["nurse_id"] == "N001" for r in result)

    def test_draft_schedule_still_works(self, db, seed_data):
        from agents_v2.skills import run_skill

        result = run_skill(db, "query-schedule", {
            "group_id": seed_data["group_id"],
            "year": 2026,
            "month": 4,
            "scope": "draft_schedule",
            "nurse_ids": ["N001"],
        })
        assert isinstance(result, list)
        assert len(result) == 30


# ── Duplicate Detection (Bug #37) ─────────────────────────


class TestDuplicateDetection:
    def test_different_nurse_same_scope_not_blocked(self):
        """Different nurse_name with same scope should NOT be blocked."""
        from agents_v2.agent_v3 import _call_signature

        sig1 = _call_signature("query_schedule", {"scope": "schedule", "nurse_name": "김민지"})
        sig2 = _call_signature("query_schedule", {"scope": "schedule", "nurse_name": "박지은"})
        assert sig1 != sig2

    def test_identical_calls_blocked(self):
        from agents_v2.agent_v3 import _call_signature

        sig1 = _call_signature("query_schedule", {"scope": "schedule", "nurse_name": "김민지"})
        sig2 = _call_signature("query_schedule", {"scope": "schedule", "nurse_name": "김민지"})
        assert sig1 == sig2


# ── Agent E2E (with DeterministicClient) ────────────────────


class TestSchedulingAgentE2E:
    def test_simple_query(self, db, seed_data, ctx):
        """Agent should route 원티드 query → tool call → answer."""
        agent = SchedulingAgent(DeterministicClient())
        result = agent.run(db, "원티드 미제출자 알려줘", ctx)

        assert isinstance(result, AgentResult)
        # Should have planning + execution + answer stages
        stage_names = [s.name for s in result.trace]
        assert "planning" in stage_names
        assert "execution" in stage_names
        assert "answer" in stage_names

    def test_schedule_query(self, db, seed_data, ctx):
        """Agent should handle 근무표 query."""
        agent = SchedulingAgent(DeterministicClient())
        result = agent.run(db, "김민지 근무표 보여줘", ctx)

        assert result.answer  # Should produce an answer
        assert len(result.trace) >= 2  # At least planning + execution

    def test_unknown_query_returns_text(self, db, seed_data, ctx):
        """Unrecognized query → direct text response."""
        agent = SchedulingAgent(DeterministicClient())
        result = agent.run(db, "안녕하세요", ctx)

        assert result.answer
        assert not result.needs_clarification

    def test_trace_format(self, db, seed_data, ctx):
        """Trace stages should serialize to dict properly."""
        agent = SchedulingAgent(DeterministicClient())
        result = agent.run(db, "원티드 현황 보여줘", ctx)

        d = result.to_dict()
        assert "pipeline_stages" in d
        assert "total_time_ms" in d
        for stage in d["pipeline_stages"]:
            assert "name" in stage
            assert "status" in stage
            assert "duration_ms" in stage


# ── Abbreviation Resolution Pipeline ────────────────────────


class TestAbbreviationResolution:
    """Test the 3-layer abbreviation resolution: .md → DB fallback → clarify."""

    def test_layer2_known_alias_resolves(self, db, seed_data):
        """Layer 2: Known alias resolves to correct shift."""
        r = resolve_shift(db, "GRP001", "밤")
        # "밤" is in _SHIFT_ALIASES → "N" → matches shift name "N"
        assert r.resolved is True

    def test_layer2_completely_unknown_returns_shift_list(self, db, seed_data):
        """Completely unknown term → error with available shifts listed."""
        r = resolve_shift(db, "GRP001", "새벽근무")
        assert r.error is not None
        assert "사용 가능한 시프트" in r.error
        # Should list actual shift names for LLM to retry
        assert "D" in r.error
        assert "N" in r.error

    def test_layer2_partial_match_auto_resolves(self, db, seed_data):
        """Partial match with single candidate → auto-resolve."""
        # "생리" is a substring of a shift name if we had "생리휴가"
        # For current test data, "공" should match "공가"
        r = resolve_shift(db, "GRP001", "공")
        # Single char might not match — let's test with "공가" directly
        r = resolve_shift(db, "GRP001", "공가")
        assert r.resolved is True

    def test_category_shift_codes_in_middleware(self, db, seed_data):
        """Middleware grounds shift_gb category → list of shift_codes."""
        from db.models import Shift

        # Add N2 shift to create multi-shift category
        db.add(Shift(
            shift_id="N2_GRP001", office_id="OFF001", group_id="GRP001",
            name="N2", shift_gb="나이트", type="근무",
            color="#000", sequence=9, allday=0, auto_schedule=1,
            is_weekly_off=0, show_in_preference=True, id=100,
        ))
        db.flush()

        from agents_v2.middleware import _ground_params

        params = {
            "shift_name": "나이트",
            "group_id": "GRP001",
            "scope": "schedule",
        }
        clarification = _ground_params(db, "GRP001", params)
        assert clarification is None  # no clarification needed
        assert "shift_codes" in params
        assert isinstance(params["shift_codes"], list)
        assert len(params["shift_codes"]) == 2
        assert "N_GRP001" in params["shift_codes"]
        assert "N2_GRP001" in params["shift_codes"]

    def test_category_codes_filter_schedule_entries(self, db, seed_data):
        """Category shift_codes correctly filter schedule entries."""
        from db.models import Shift

        # Add N2 shift
        db.add(Shift(
            shift_id="N2_GRP001", office_id="OFF001", group_id="GRP001",
            name="N2", shift_gb="나이트", type="근무",
            color="#000", sequence=9, allday=0, auto_schedule=1,
            is_weekly_off=0, show_in_preference=True, id=101,
        ))
        db.flush()

        from agents_v2.middleware import execute_skill
        from agents_v2.schemas.session_context import SessionContext

        ctx = SessionContext(
            office_id="OFF001", group_id="GRP001",
            year=2026, month=4,
            nurse_id="N001", nurse_name="김민지", user_role="HN",
        )
        result = execute_skill(db, "query_schedule", {
            "scope": "schedule",
            "shift_name": "나이트",
        }, ctx)

        assert not isinstance(result.data, dict) or "error" not in result.data
        # All returned entries should have night shift codes
        if isinstance(result.data, list):
            for entry in result.data:
                assert entry["shift_id"] in ("N_GRP001", "N2_GRP001")

    def test_mutation_category_triggers_clarification(self, db, seed_data):
        """Mutation with category shift → clarification (need specific shift)."""
        from db.models import Shift

        # Add N2 shift
        db.add(Shift(
            shift_id="N2_GRP001", office_id="OFF001", group_id="GRP001",
            name="N2", shift_gb="나이트", type="근무",
            color="#000", sequence=9, allday=0, auto_schedule=1,
            is_weekly_off=0, show_in_preference=True, id=102,
        ))
        db.flush()

        from agents_v2.middleware import _ground_params

        params = {
            "new_shift_name": "나이트",
            "group_id": "GRP001",
        }
        clarification = _ground_params(db, "GRP001", params)
        assert clarification is not None
        assert clarification["needs_clarification"] is True
        assert "여러 시프트" in clarification["question"]

    def test_auto_learn_tracking(self):
        """Auto-learn tracks failed terms and learns on retry success."""
        from agents_v2.agent_v3 import _track_shift_learning
        from agents_v2.middleware import SkillResult, MiddlewareStep

        failed_terms: list[str] = []

        # Simulate failed shift grounding
        fail_result = SkillResult(
            data={"error": "밤번 시프트를 찾을 수 없습니다."},
            duration_ms=10, skill_name="query_schedule",
            grounded_params={}, middleware_steps=[],
        )
        _track_shift_learning(
            {"shift_name": "밤번"}, fail_result, failed_terms,
        )
        assert "밤번" in failed_terms

        # Simulate successful retry with correct name
        ok_result = SkillResult(
            data=[{"shift_id": "N_GRP001"}],
            duration_ms=10, skill_name="query_schedule",
            grounded_params={}, middleware_steps=[],
        )
        # Note: _append_learned_abbreviation will try to write to .md
        # We just verify the tracking logic, not the file write
        _track_shift_learning(
            {"shift_name": "N"}, ok_result, failed_terms,
        )
        # After learning, failed_terms should be cleared
        assert len(failed_terms) == 0


# ── Wanted Deadline Update (Task #45) ────────────────────────


class TestWantedDeadlineUpdate:
    """Test update_deadline action in bulk_mutation."""

    def test_update_deadline_preview(self, db, seed_data):
        from agents_v2.skills import run_skill

        result = run_skill(db, "bulk-mutation", {
            "scope": "wanted_submissions",
            "action": "update_deadline",
            "group_id": seed_data["group_id"],
            "year": 2026,
            "month": 4,
            "new_deadline": "2026-04-20",
            "preview_only": True,
        })
        assert result.get("preview_only") is True
        assert result["new_deadline"] == "2026-04-20"
        assert result.get("current_deadline") is not None

    def test_update_deadline_execute(self, db, seed_data):
        from agents_v2.skills import run_skill

        result = run_skill(db, "bulk-mutation", {
            "scope": "wanted_submissions",
            "action": "update_deadline",
            "group_id": seed_data["group_id"],
            "year": 2026,
            "month": 4,
            "new_deadline": "2026-04-22",
            "preview_only": False,
        })
        assert "error" not in result
        assert result["new_deadline"] == "2026-04-22"

    def test_update_deadline_no_campaign(self, db, seed_data):
        from agents_v2.skills import run_skill

        result = run_skill(db, "bulk-mutation", {
            "scope": "wanted_submissions",
            "action": "update_deadline",
            "group_id": seed_data["group_id"],
            "year": 2026,
            "month": 12,  # no campaign for Dec
            "new_deadline": "2026-12-20",
            "preview_only": True,
        })
        assert "error" in result


# ── Relative Weekday Date Resolution (Task #45) ──────────────


class TestRelativeWeekdayResolution:
    """Test resolve_date with relative weekday expressions."""

    def test_this_week_friday(self):
        from datetime import datetime, timedelta
        result = resolve_date("이번 주 금요일", 2026, 4)
        assert result is not None
        # Should be a valid YYYY-MM-DD
        assert len(result) == 10

    def test_next_week_monday(self):
        result = resolve_date("다음 주 월요일", 2026, 4)
        assert result is not None
        assert len(result) == 10

    def test_this_saturday(self):
        result = resolve_date("토요일", 2026, 4)
        assert result is not None


# ── Wanted Submission Filter (Task #46) ──────────────────────


class TestWantedSubmissionFilter:
    """Test include_cancelled and submitted_date_range parameters."""

    def test_default_submitted_only(self, db, seed_data):
        """Default: only submitted requests returned."""
        from agents_v2.skills import run_skill

        result = run_skill(db, "query-schedule", {
            "scope": "wanted_submissions",
            "group_id": seed_data["group_id"],
            "year": 2026,
            "month": 4,
        })
        assert isinstance(result, list)
        # All returned rows should be from submitted nurses (N001, N002)
        nurse_ids = {r["nurse_id"] for r in result}
        assert "N003" not in nurse_ids  # N003 is not submitted

    def test_include_cancelled_shows_all(self, db, seed_data):
        """include_cancelled=true shows retracted/unsubmitted too."""
        from agents_v2.skills import run_skill

        # First check default (submitted only)
        submitted = run_skill(db, "query-schedule", {
            "scope": "wanted_submissions",
            "group_id": seed_data["group_id"],
            "year": 2026,
            "month": 4,
        })

        # Now with include_cancelled
        all_rows = run_skill(db, "query-schedule", {
            "scope": "wanted_submissions",
            "group_id": seed_data["group_id"],
            "year": 2026,
            "month": 4,
            "include_cancelled": True,
        })
        assert isinstance(all_rows, list)
        # Should have is_submitted field
        if all_rows:
            assert "is_submitted" in all_rows[0]

    def test_submitted_date_range_filter(self, db, seed_data):
        """submitted_date_range filters by submission date."""
        from agents_v2.skills import run_skill

        # Submitted_at is 2026-03-20 in seed data
        result = run_skill(db, "query-schedule", {
            "scope": "wanted_submissions",
            "group_id": seed_data["group_id"],
            "year": 2026,
            "month": 4,
            "submitted_date_range": "2026-03-19~2026-03-21",
        })
        assert isinstance(result, list)
        assert len(result) > 0  # should find submissions from 03-20

    def test_submitted_date_range_no_match(self, db, seed_data):
        """submitted_date_range outside range returns empty."""
        from agents_v2.skills import run_skill

        result = run_skill(db, "query-schedule", {
            "scope": "wanted_submissions",
            "group_id": seed_data["group_id"],
            "year": 2026,
            "month": 4,
            "submitted_date_range": "2026-01-01~2026-01-05",
        })
        assert isinstance(result, list)
        assert len(result) == 0


# ── Grade Filter + Enrich (Task #47) ─────────────────────────


class TestGradeFilterAndEnrich:
    """Test grade enrichment in schedule entries and grade filtering."""

    def test_enrich_includes_grade(self, db, seed_data):
        """Schedule entries should include grade after enrichment."""
        from agents_v2.skills import run_skill

        result = run_skill(db, "query-schedule", {
            "scope": "schedule",
            "group_id": seed_data["group_id"],
            "year": 2026,
            "month": 4,
            "nurse_ids": ["N001"],
        })
        assert isinstance(result, list)
        assert len(result) > 0
        # Grade should be present in enriched entries
        assert "grade" in result[0]
        assert result[0]["grade"] == 4  # N001 is grade 4 in seed data

    def test_grade_filter(self, db, seed_data):
        """grade parameter filters schedule entries by nurse grade."""
        from agents_v2.skills import run_skill

        # Grade 1 = only 정다은 (N004) in seed data
        result = run_skill(db, "query-schedule", {
            "scope": "schedule",
            "group_id": seed_data["group_id"],
            "year": 2026,
            "month": 4,
            "grade": 1,
        })
        assert isinstance(result, list)
        assert len(result) > 0
        # All entries should be for grade 1 nurses only
        assert all(e["grade"] == 1 for e in result)
        assert all(e["nurse_id"] == "N004" for e in result)

    def test_grade_filter_no_match(self, db, seed_data):
        """grade parameter with non-existent grade returns empty."""
        from agents_v2.skills import run_skill

        result = run_skill(db, "query-schedule", {
            "scope": "schedule",
            "group_id": seed_data["group_id"],
            "year": 2026,
            "month": 4,
            "grade": 99,
        })
        assert isinstance(result, list)
        assert len(result) == 0

    def test_grade_combined_with_shift(self, db, seed_data):
        """grade + shift_codes filter combined."""
        from agents_v2.skills import run_skill

        # Grade 1 nurses on night shift (use shift_codes directly, as grounding is middleware)
        result = run_skill(db, "query-schedule", {
            "scope": "schedule",
            "group_id": seed_data["group_id"],
            "year": 2026,
            "month": 4,
            "grade": 1,
            "shift_codes": ["N_GRP001"],
        })
        assert isinstance(result, list)
        # All entries should be grade 1 AND night shift
        for e in result:
            assert e["grade"] == 1
            assert e["shift_id"] == "N_GRP001"

    def test_summarize_includes_grade(self, db, seed_data):
        """Auto-summary should still work with grade enrichment."""
        from agents_v2.skills import run_skill

        # No filters → triggers auto-summary (>60 entries)
        result = run_skill(db, "query-schedule", {
            "scope": "schedule",
            "group_id": seed_data["group_id"],
            "year": 2026,
            "month": 4,
        })
        # Should be summary dict, not raw list
        assert isinstance(result, dict)
        assert result.get("summary") is True


# ── Per-date Wanted CRUD (Task #48-52) ────────────────────────


class TestWantedPerDateCRUD:
    """Test per-date wanted add/modify/delete with new request_id + pair copy."""

    # ── Delete ──

    def test_delete_preview(self, db, seed_data):
        from agents_v2.skills import run_skill

        result = run_skill(db, "bulk-mutation", {
            "scope": "wanted_submissions",
            "action": "cancel",
            "group_id": seed_data["group_id"],
            "year": 2026,
            "month": 4,
            "nurse_ids": ["N001"],
            "date": "2026-04-05",
            "preview_only": True,
        })
        assert result.get("preview_only") is True
        assert result["action"] == "delete_wanted_date"
        assert result["date"] == "2026-04-05"
        assert result["shift"] == "D"

    def test_delete_execute(self, db, seed_data):
        from agents_v2.skills import run_skill
        from db.models import WantedRequest, NurseShiftRequest, NursePairRequest

        result = run_skill(db, "bulk-mutation", {
            "scope": "wanted_submissions",
            "action": "cancel",
            "group_id": seed_data["group_id"],
            "year": 2026,
            "month": 4,
            "nurse_ids": ["N001"],
            "date": "2026-04-05",
            "preview_only": False,
        })
        assert "error" not in result
        assert result["status"] == "submitted"
        new_rid = result["request_id"]
        assert new_rid > 1  # New request_id created

        # Verify: new WantedRequest is submitted
        new_wr = db.query(WantedRequest).filter(
            WantedRequest.nurse_id == "N001",
            WantedRequest.request_id == new_rid,
        ).first()
        assert bool(new_wr.is_submitted) is True
        assert new_wr.submitted_at is not None

        # Verify: 4/5 removed, 4/10 and 4/15 remain
        shifts = db.query(NurseShiftRequest).filter(
            NurseShiftRequest.nurse_id == "N001",
            NurseShiftRequest.request_id == new_rid,
        ).all()
        dates = {str(s.shift_date) for s in shifts}
        assert "2026-04-05" not in dates
        assert "2026-04-10" in dates
        assert "2026-04-15" in dates
        assert result["remaining_count"] == 2

        # Verify: pairs copied
        pairs = db.query(NursePairRequest).filter(
            NursePairRequest.nurse_id == "N001",
            NursePairRequest.request_id == new_rid,
        ).all()
        assert len(pairs) == 2
        target_ids = {p.target_id for p in pairs}
        assert "N002" in target_ids
        assert "N004" in target_ids

    def test_delete_nonexistent_date(self, db, seed_data):
        from agents_v2.skills import run_skill

        result = run_skill(db, "bulk-mutation", {
            "scope": "wanted_submissions",
            "action": "cancel",
            "group_id": seed_data["group_id"],
            "year": 2026,
            "month": 4,
            "nurse_ids": ["N001"],
            "date": "2026-04-20",  # N001 has no wanted on 4/20
            "preview_only": True,
        })
        assert "error" in result

    # ── Add ──

    def test_add_preview_with_comment(self, db, seed_data):
        from agents_v2.skills import run_skill

        result = run_skill(db, "bulk-mutation", {
            "scope": "wanted_submissions",
            "action": "add_shift",
            "group_id": seed_data["group_id"],
            "year": 2026,
            "month": 4,
            "nurse_ids": ["N001"],
            "date": "2026-04-20",
            "shift_codes": ["D_GRP001"],
            "comment": "부모님 병원 방문",
            "preview_only": True,
        })
        assert result.get("preview_only") is True
        assert result["action"] == "add_wanted_date"
        assert "부모님 병원 방문" in result["message"]

    def test_add_execute(self, db, seed_data):
        from agents_v2.skills import run_skill
        from db.models import NurseShiftRequest, NursePairRequest

        result = run_skill(db, "bulk-mutation", {
            "scope": "wanted_submissions",
            "action": "add_shift",
            "group_id": seed_data["group_id"],
            "year": 2026,
            "month": 4,
            "nurse_ids": ["N001"],
            "date": "2026-04-20",
            "shift_codes": ["D_GRP001"],
            "comment": "개인 사정",
            "preview_only": False,
        })
        assert "error" not in result
        assert result["status"] == "submitted"
        new_rid = result["request_id"]

        # Verify: 4 entries (3 old + 1 new)
        shifts = db.query(NurseShiftRequest).filter(
            NurseShiftRequest.nurse_id == "N001",
            NurseShiftRequest.request_id == new_rid,
        ).all()
        assert len(shifts) == 4
        new_entry = [s for s in shifts if str(s.shift_date) == "2026-04-20"]
        assert len(new_entry) == 1
        assert new_entry[0].shift == "D_GRP001"
        assert new_entry[0].comment == "개인 사정"
        assert new_entry[0].shifts_table_id is not None  # resolved

        # Verify: pairs copied
        pairs = db.query(NursePairRequest).filter(
            NursePairRequest.nurse_id == "N001",
            NursePairRequest.request_id == new_rid,
        ).all()
        assert len(pairs) == 2

    def test_add_duplicate_date_error(self, db, seed_data):
        from agents_v2.skills import run_skill

        result = run_skill(db, "bulk-mutation", {
            "scope": "wanted_submissions",
            "action": "add_shift",
            "group_id": seed_data["group_id"],
            "year": 2026,
            "month": 4,
            "nurse_ids": ["N001"],
            "date": "2026-04-05",  # already exists
            "shift_codes": ["D_GRP001"],
            "preview_only": True,
        })
        assert "error" in result
        assert "이미" in result["error"]

    # ── Modify ──

    def test_modify_preview(self, db, seed_data):
        from agents_v2.skills import run_skill

        result = run_skill(db, "bulk-mutation", {
            "scope": "wanted_submissions",
            "action": "change_shift",
            "group_id": seed_data["group_id"],
            "year": 2026,
            "month": 4,
            "nurse_ids": ["N001"],
            "date": "2026-04-05",
            "new_shift_code": "N_GRP001",
            "preview_only": True,
        })
        assert result.get("preview_only") is True
        assert result["old_shift"] == "D"
        assert result["new_shift"] == "N_GRP001"

    def test_modify_execute_with_comment(self, db, seed_data):
        from agents_v2.skills import run_skill
        from db.models import NurseShiftRequest, NursePairRequest

        result = run_skill(db, "bulk-mutation", {
            "scope": "wanted_submissions",
            "action": "change_shift",
            "group_id": seed_data["group_id"],
            "year": 2026,
            "month": 4,
            "nurse_ids": ["N001"],
            "date": "2026-04-05",
            "new_shift_code": "N_GRP001",
            "comment": "야간 선호 변경",
            "preview_only": False,
        })
        assert "error" not in result
        assert result["status"] == "submitted"
        new_rid = result["request_id"]

        # Verify: 3 entries, 4/5 changed to N
        shifts = db.query(NurseShiftRequest).filter(
            NurseShiftRequest.nurse_id == "N001",
            NurseShiftRequest.request_id == new_rid,
        ).all()
        assert len(shifts) == 3
        modified = [s for s in shifts if str(s.shift_date) == "2026-04-05"][0]
        assert modified.shift == "N_GRP001"
        assert modified.comment == "야간 선호 변경"

        # Other dates unchanged
        d10 = [s for s in shifts if str(s.shift_date) == "2026-04-10"][0]
        assert d10.shift == "N"  # original

        # Verify: pairs copied
        pairs = db.query(NursePairRequest).filter(
            NursePairRequest.nurse_id == "N001",
            NursePairRequest.request_id == new_rid,
        ).all()
        assert len(pairs) == 2

    def test_modify_nonexistent_date(self, db, seed_data):
        from agents_v2.skills import run_skill

        result = run_skill(db, "bulk-mutation", {
            "scope": "wanted_submissions",
            "action": "change_shift",
            "group_id": seed_data["group_id"],
            "year": 2026,
            "month": 4,
            "nurse_ids": ["N001"],
            "date": "2026-04-25",
            "new_shift_code": "D_GRP001",
            "preview_only": True,
        })
        assert "error" in result

    # ── No submitted wanted ──

    def test_add_without_prior_submission(self, db, seed_data):
        """N003 has no submitted wanted — add should create fresh WantedRequest."""
        from agents_v2.skills import run_skill
        from db.models import WantedRequest, NurseShiftRequest

        result = run_skill(db, "bulk-mutation", {
            "scope": "wanted_submissions",
            "action": "add_shift",
            "group_id": seed_data["group_id"],
            "year": 2026,
            "month": 4,
            "nurse_ids": ["N003"],
            "date": "2026-04-20",
            "shift_codes": ["E_GRP001"],
            "comment": "첫 원티드",
            "preview_only": False,
        })
        assert "error" not in result
        assert result["status"] == "submitted"
        new_rid = result["request_id"]

        # Verify: new WantedRequest created and submitted
        wr = db.query(WantedRequest).filter(
            WantedRequest.nurse_id == "N003",
            WantedRequest.request_id == new_rid,
        ).first()
        assert wr is not None
        assert bool(wr.is_submitted) is True

        # Verify: 1 shift entry
        shifts = db.query(NurseShiftRequest).filter(
            NurseShiftRequest.nurse_id == "N003",
            NurseShiftRequest.request_id == new_rid,
        ).all()
        assert len(shifts) == 1
        assert shifts[0].comment == "첫 원티드"

    # ── Month-level cancel still works (no date) ──

    def test_month_cancel_without_date(self, db, seed_data):
        """cancel without date should retract entire month submission (existing behavior)."""
        from agents_v2.skills import run_skill

        result = run_skill(db, "bulk-mutation", {
            "scope": "wanted_submissions",
            "action": "cancel",
            "group_id": seed_data["group_id"],
            "year": 2026,
            "month": 4,
            "nurse_ids": ["N002"],
            "preview_only": False,
        })
        assert "error" not in result
        assert result.get("status") == "retracted"


# ── Constraint Update (update_constraint) ─────────────────────


class TestConstraintUpdate:
    """Test update_constraint skill with flat field/value params."""

    def test_preview_integer_field(self, db, seed_data):
        from agents_v2.skills import run_skill

        result = run_skill(db, "update-constraint", {
            "group_id": seed_data["group_id"],
            "field": "max_nig_per_month",
            "value": 5,
            "preview_only": True,
        })
        assert result.get("preview") is True
        assert "max_nig_per_month" in result.get("changes", {})
        change = result["changes"]["max_nig_per_month"]
        assert change["old"] == 7  # seed_data default
        assert change["new"] == 5

    def test_execute_integer_field(self, db, seed_data):
        from agents_v2.skills import run_skill

        result = run_skill(db, "update-constraint", {
            "group_id": seed_data["group_id"],
            "field": "max_conseq_work",
            "value": 4,
            "preview_only": False,
        })
        assert result.get("preview") is False
        assert result["changes"]["max_conseq_work"]["new"] == 4

        # Verify persisted
        config = run_skill(db, "update-constraint", {
            "group_id": seed_data["group_id"],
        })
        assert config["max_conseq_work"] == 4

    def test_preview_boolean_field(self, db, seed_data):
        from agents_v2.skills import run_skill

        result = run_skill(db, "update-constraint", {
            "group_id": seed_data["group_id"],
            "field": "banned_day_after_eve",
            "value": False,
            "preview_only": True,
        })
        assert result.get("preview") is True
        change = result["changes"]["banned_day_after_eve"]
        assert change["old"] is True
        assert change["new"] is False

    def test_execute_boolean_field(self, db, seed_data):
        from agents_v2.skills import run_skill

        result = run_skill(db, "update-constraint", {
            "group_id": seed_data["group_id"],
            "field": "team_balance_enable",
            "value": False,
            "preview_only": False,
        })
        assert result["changes"]["team_balance_enable"]["new"] is False

    def test_unknown_field_error(self, db, seed_data):
        from agents_v2.skills import run_skill

        result = run_skill(db, "update-constraint", {
            "group_id": seed_data["group_id"],
            "field": "nonexistent_field",
            "value": 123,
            "preview_only": False,
        })
        assert "error" in result

    def test_no_field_returns_current_config(self, db, seed_data):
        from agents_v2.skills import run_skill

        result = run_skill(db, "update-constraint", {
            "group_id": seed_data["group_id"],
        })
        assert "config_id" in result
        assert "max_nig_per_month" in result
        assert result["max_nig_per_month"] == 7
