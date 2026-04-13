"""End-to-end tests for the scheduling agent pipeline.

Tests the FULL pipeline: concept_typer → grounding → canonicalize → plan → execute → verify
with real SQLAlchemy models and real DB data (SQLite in-memory).

Based on the 23 example queries from the design specification.
"""

from __future__ import annotations

import pytest
from sqlalchemy.orm import Session

from agents_v2.agent import SchedulingAgent
from agents_v2.schemas.agent_response import AgentResponse

# ═══════════════════════════════════════════════════════════
# Layer 1: Tool-level tests (verify tools work against real DB)
# ═══════════════════════════════════════════════════════════


class TestToolLayer:
    """Verify tool functions produce correct results from real DB data."""

    def test_read_shift_definitions(self, db, seed_data):
        from agents_v2.tools.shift_tools import read_shift_definitions

        shifts = read_shift_definitions(db, seed_data["group_id"])
        assert len(shifts) == 8
        names = {s["name"] for s in shifts}
        assert {"D", "E", "N", "OFF", "V"}.issubset(names)
        # Check working vs non-working
        working = [s for s in shifts if s["is_working"]]
        assert len(working) == 4  # D, E, N, M

    def test_search_nurses_by_name(self, db, seed_data):
        from agents_v2.tools.nurse_tools import search_nurses_by_name

        results = search_nurses_by_name(db, seed_data["group_id"], "김민지")
        assert len(results) == 1
        assert results[0]["nurse_id"] == "N001"
        assert results[0]["grade"] == 4

    def test_get_nurses_in_group(self, db, seed_data):
        from agents_v2.tools.nurse_tools import get_nurses_in_group

        nurses = get_nurses_in_group(db, seed_data["group_id"])
        assert len(nurses) == 6

    def test_filter_nurses_by_grade(self, db, seed_data):
        from agents_v2.tools.nurse_tools import filter_nurses

        grade1 = filter_nurses(db, seed_data["group_id"], grade=1)
        assert len(grade1) == 1
        assert grade1[0]["name"] == "정다은"

    def test_resolve_target_schedule(self, db, seed_data):
        from agents_v2.tools.schedule_tools import resolve_target_schedule, get_schedule_versions

        # First verify versions exist
        versions = get_schedule_versions(db, seed_data["group_id"], 2026, 4)
        assert len(versions) >= 1, f"Expected schedule versions, got: {versions}"

        meta = resolve_target_schedule(db, seed_data["group_id"], 2026, 4)
        assert meta is not None, "resolve_target_schedule returned None"
        assert meta["schedule_id"] == "SCH202604V1"
        assert meta["year"] == 2026
        assert meta["month"] == 4

    def test_get_schedule_entries(self, db, seed_data):
        from agents_v2.tools.schedule_tools import get_schedule_entries

        entries = get_schedule_entries(db, seed_data["schedule_id"])
        assert len(entries) == 180  # 30 days × 6 nurses

    def test_get_schedule_entries_filtered_by_nurse(self, db, seed_data):
        from agents_v2.tools.schedule_tools import get_schedule_entries

        entries = get_schedule_entries(db, seed_data["schedule_id"], nurse_ids=["N001"])
        assert len(entries) == 30  # 30 days
        assert all(e["nurse_id"] == "N001" for e in entries)

    def test_get_schedule_entries_filtered_by_date(self, db, seed_data):
        from agents_v2.tools.schedule_tools import get_schedule_entries
        from datetime import datetime

        # SQLite stores datetime objects, so filter with datetime
        entries = get_schedule_entries(db, seed_data["schedule_id"], date_str=datetime(2026, 4, 15))
        assert len(entries) == 6  # all 6 nurses on that date

    def test_find_schedule_entry(self, db, seed_data):
        from agents_v2.tools.schedule_tools import find_schedule_entry
        from datetime import datetime

        entry = find_schedule_entry(db, seed_data["schedule_id"], "N001", datetime(2026, 4, 1))
        assert entry is not None
        assert entry["nurse_id"] == "N001"

    def test_get_wanted_status(self, db, seed_data):
        from agents_v2.tools.wanted_tools import get_wanted_status

        status = get_wanted_status(db, seed_data["group_id"], 2026, 4)
        assert status is not None
        assert status["status"] == "requested"

    def test_get_submission_status(self, db, seed_data):
        from agents_v2.tools.wanted_tools import get_submission_status

        status = get_submission_status(db, seed_data["group_id"], 2026, 4)
        assert status["total"] == 6
        assert status["submitted_count"] == 2  # N001, N002
        assert status["not_submitted_count"] == 4

    def test_get_wanted_adjustments(self, db, seed_data):
        from agents_v2.tools.wanted_tools import get_wanted_adjustments

        adjustments = get_wanted_adjustments(db, seed_data["group_id"], 2026, 4)
        assert len(adjustments) == 4
        # Check one not-applied entry
        not_applied = [a for a in adjustments if not a["is_applied"]]
        assert len(not_applied) == 1
        assert not_applied[0]["nurse_id"] == "N002"

    def test_get_roster_config(self, db, seed_data):
        from agents_v2.tools.constraint_tools import get_roster_config

        config = get_roster_config(db, seed_data["group_id"])
        assert config is not None
        assert config["day_req"] == 2
        assert config["max_nig_per_month"] == 7
        assert config["even_nights"] is True

    def test_count_shifts_per_nurse(self, db, seed_data):
        from agents_v2.tools.analysis_tools import count_shifts_per_nurse

        counts = count_shifts_per_nurse(db, seed_data["schedule_id"])
        assert len(counts) > 0
        # Each nurse should have some entries
        nurse_ids = {c["nurse_id"] for c in counts}
        assert len(nurse_ids) == 6

    def test_detect_consecutive_nights(self, db, seed_data):
        from agents_v2.tools.analysis_tools import detect_consecutive_pattern

        # N004 has consecutive nights on days 10-13 (4 consecutive)
        streaks = detect_consecutive_pattern(
            db, seed_data["schedule_id"], "N_GRP001", min_streak=3
        )
        assert len(streaks) >= 1
        n004_streak = [s for s in streaks if s["nurse_id"] == "N004"]
        assert len(n004_streak) >= 1
        assert n004_streak[0]["streak_length"] >= 3

    def test_shift_count_variance(self, db, seed_data):
        from agents_v2.tools.analysis_tools import shift_count_variance

        stats = shift_count_variance(db, seed_data["schedule_id"], "N_GRP001")
        assert "variance" in stats
        assert "min" in stats
        assert "max" in stats
        assert stats["min"] >= 0

    def test_daily_headcount(self, db, seed_data):
        from agents_v2.tools.analysis_tools import daily_headcount

        hc = daily_headcount(db, seed_data["schedule_id"], seed_data["group_id"])
        assert len(hc) > 0
        # Each date-shift combo has a count
        assert all("count" in h for h in hc)


# ═══════════════════════════════════════════════════════════
# Layer 2: Grounding tests (verify grounders resolve correctly)
# ═══════════════════════════════════════════════════════════


class TestGroundingLayer:
    """Verify grounders resolve surface expressions to canonical values via real DB."""

    def test_ground_lexical_D(self, db, seed_data):
        from agents_v2.grounding.lexical_grounder import ground_lexical

        result = ground_lexical(db, seed_data["group_id"], "D")
        assert result.is_grounded
        assert result.grounded_value["shift_id"] == "D_GRP001"
        assert result.grounded_value["name"] == "D"

    def test_ground_lexical_OFF(self, db, seed_data):
        from agents_v2.grounding.lexical_grounder import ground_lexical

        result = ground_lexical(db, seed_data["group_id"], "OFF")
        assert result.is_grounded
        assert result.grounded_value["shift_id"] == "OFF_GRP001"

    def test_ground_lexical_V(self, db, seed_data):
        from agents_v2.grounding.lexical_grounder import ground_lexical

        result = ground_lexical(db, seed_data["group_id"], "V")
        assert result.is_grounded
        assert result.grounded_value["name"] == "V"

    def test_ground_temporal_morning(self, db, seed_data):
        from agents_v2.grounding.temporal_grounder import ground_temporal

        result = ground_temporal(db, seed_data["group_id"], "아침 근무")
        assert result.is_grounded
        # Should match D (07:00) and possibly M (10:00)
        val = result.grounded_value
        if isinstance(val, list):
            shift_ids = {v["shift_id"] for v in val}
            assert "D_GRP001" in shift_ids
        else:
            assert val["shift_id"] == "D_GRP001"

    def test_ground_temporal_night(self, db, seed_data):
        from agents_v2.grounding.temporal_grounder import ground_temporal

        result = ground_temporal(db, seed_data["group_id"], "야간")
        assert result.is_grounded
        val = result.grounded_value
        if isinstance(val, list):
            shift_ids = {v["shift_id"] for v in val}
            assert "N_GRP001" in shift_ids
        else:
            assert val["shift_id"] == "N_GRP001"

    def test_ground_temporal_evening(self, db, seed_data):
        from agents_v2.grounding.temporal_grounder import ground_temporal

        result = ground_temporal(db, seed_data["group_id"], "이브닝")
        assert result.is_grounded

    def test_ground_entity_nurse_by_name(self, db, seed_data):
        from agents_v2.grounding.entity_grounder import ground_entity

        result = ground_entity(db, seed_data["group_id"], "김민지")
        assert result.is_grounded
        assert result.grounded_value["nurse_id"] == "N001"
        assert result.grounded_value["name"] == "김민지"

    def test_ground_entity_nurse_ambiguous(self, db, seed_data):
        """If searching with partial name that matches multiple, should be ambiguous or grounded."""
        from agents_v2.grounding.entity_grounder import ground_entity

        # "지" matches both 김민지 and 박지은
        result = ground_entity(db, seed_data["group_id"], "지")
        # Should either be ambiguous (multiple matches) or grounded (if exact enough)
        assert result.status.value in ("grounded", "ambiguous")

    def test_ground_entity_team(self, db, seed_data):
        from agents_v2.grounding.entity_grounder import ground_entity

        result = ground_entity(db, seed_data["group_id"], "1팀", entity_type_hint="team")
        assert result.is_grounded
        assert result.grounded_value["team_id"] == 1
        assert result.grounded_value["member_count"] == 3  # N001, N002, N005

    def test_ground_status_off(self, db, seed_data):
        from agents_v2.grounding.status_grounder import ground_status

        result = ground_status(db, seed_data["group_id"], "쉬는사람")
        assert result.is_grounded
        assert result.grounded_value["predicate"] == "off"
        assert len(result.grounded_value["shift_ids"]) > 0

    def test_ground_status_submitted(self, db, seed_data):
        from agents_v2.grounding.status_grounder import ground_status

        result = ground_status(db, seed_data["group_id"], "제출")
        assert result.is_grounded
        assert result.grounded_value["predicate"] == "submitted"

    def test_ground_workflow_unapply(self, db, seed_data):
        from agents_v2.grounding.workflow_grounder import ground_workflow

        result = ground_workflow(db, seed_data["group_id"], "적용 안되게")
        assert result.is_grounded
        chain = result.grounded_value["action_chain"]
        assert chain[0]["action"] == "set_field"
        assert chain[0]["value"] is False

    def test_ground_workflow_cancel(self, db, seed_data):
        from agents_v2.grounding.workflow_grounder import ground_workflow

        result = ground_workflow(db, seed_data["group_id"], "취소해줘")
        assert result.is_grounded
        assert result.grounded_value["action_chain"][0]["action"] == "cancel"

    def test_ground_dispatch(self, db, seed_data):
        from agents_v2.grounding.dispatcher import dispatch_grounding
        from agents_v2.schemas.grounded_concept import ConceptType

        result = dispatch_grounding(db, seed_data["group_id"], ConceptType.LEXICAL, "N")
        assert result.is_grounded
        assert result.grounded_value["shift_id"] == "N_GRP001"


# ═══════════════════════════════════════════════════════════
# Layer 3: Skill tests (verify skills execute against real DB)
# ═══════════════════════════════════════════════════════════


class TestSkillLayer:
    """Verify skills produce correct results using real DB data."""

    def test_query_schedule_entries(self, db, seed_data):
        from agents_v2.skills import run_skill

        result = run_skill(db, "query-schedule", {
            "group_id": seed_data["group_id"],
            "year": 2026,
            "month": 4,
            "scope": "draft_schedule",
            "nurse_ids": ["N001"],
            "schedule_id": seed_data["schedule_id"],
        })
        assert isinstance(result, list)
        assert len(result) == 30
        assert all(r["nurse_id"] == "N001" for r in result)

    def test_query_wanted_submissions(self, db, seed_data):
        from agents_v2.skills import run_skill

        result = run_skill(db, "query-schedule", {
            "group_id": seed_data["group_id"],
            "year": 2026,
            "month": 4,
            "scope": "wanted_submissions",
            "operation": "count",
        })
        assert result["submitted_count"] == 2
        assert result["not_submitted_count"] == 4

    def test_query_wanted_adjustments(self, db, seed_data):
        from agents_v2.skills import run_skill

        result = run_skill(db, "query-schedule", {
            "group_id": seed_data["group_id"],
            "year": 2026,
            "month": 4,
            "scope": "wanted_adjustment",
        })
        assert isinstance(result, list)
        assert len(result) == 4

    def test_query_shift_definitions(self, db, seed_data):
        from agents_v2.skills import run_skill

        result = run_skill(db, "query-schedule", {
            "group_id": seed_data["group_id"],
            "scope": "shift_definitions",
        })
        assert len(result) == 8

    def test_query_constraint_config(self, db, seed_data):
        from agents_v2.skills import run_skill

        result = run_skill(db, "query-schedule", {
            "group_id": seed_data["group_id"],
            "scope": "constraint_config",
        })
        assert result["day_req"] == 2

    def test_bulk_mutation_wanted_adjustment(self, db, seed_data):
        from agents_v2.skills import run_skill

        # Preview mode first
        result = run_skill(db, "bulk-mutation", {
            "group_id": seed_data["group_id"],
            "year": 2026,
            "month": 4,
            "scope": "wanted_adjustment",
            "nurse_ids": ["N002"],
            "mutation": {
                "action": "set_field",
                "target_field": "is_applied",
                "target_value": True,
            },
            "preview_only": True,
        })
        assert result["preview"] is True
        assert result["affected_count"] == 1  # N002's not-applied entry

    def test_bulk_mutation_schedule_entry(self, db, seed_data):
        from agents_v2.skills import run_skill
        from datetime import datetime

        result = run_skill(db, "bulk-mutation", {
            "group_id": seed_data["group_id"],
            "year": 2026,
            "month": 4,
            "scope": "draft_schedule",
            "schedule_id": seed_data["schedule_id"],
            "nurse_ids": ["N001"],
            "date": datetime(2026, 4, 1),
            "mutation": {
                "action": "update_entry",
                "target_value": "E_GRP001",
            },
        })
        assert "old_shift_id" in result
        assert result["new_shift_id"] == "E_GRP001"

    def test_validate_schedule(self, db, seed_data):
        from agents_v2.skills import run_skill

        result = run_skill(db, "validate-schedule", {
            "group_id": seed_data["group_id"],
            "year": 2026,
            "month": 4,
            "schedule_id": seed_data["schedule_id"],
        })
        assert "violations" in result
        assert "violation_count" in result
        # N004 has 4 consecutive nights — should trigger violation
        night_violations = [
            v for v in result["violations"]
            if v["type"] == "consecutive_night_exceeded"
        ]
        assert len(night_violations) >= 1

    def test_recommend_candidates(self, db, seed_data):
        from agents_v2.skills import run_skill
        from agents_v2.tools.schedule_tools import get_schedule_entries
        from datetime import datetime

        # First verify which nurses have OFF on a specific date
        day7_entries = get_schedule_entries(db, seed_data["schedule_id"], date_str=datetime(2026, 4, 7))
        off_on_day7 = [e["nurse_id"] for e in day7_entries if "OFF" in e["shift_id"]]

        result = run_skill(db, "recommend-candidates", {
            "group_id": seed_data["group_id"],
            "year": 2026,
            "month": 4,
            "schedule_id": seed_data["schedule_id"],
            "date": datetime(2026, 4, 7),
            "shift_codes": ["D_GRP001"],
        })
        assert "candidates" in result
        assert result["candidate_count"] == len(result["candidates"])
        # All returned candidates should be nurses who are off that day
        if off_on_day7:
            candidate_ids = {c["nurse_id"] for c in result["candidates"]}
            assert candidate_ids.issubset(set(off_on_day7))

    def test_analyze_report_schedule(self, db, seed_data):
        from agents_v2.skills import run_skill

        result = run_skill(db, "analyze-report", {
            "group_id": seed_data["group_id"],
            "year": 2026,
            "month": 4,
            "scope": "draft_schedule",
            "operation": "summarize",
            "schedule_id": seed_data["schedule_id"],
        })
        assert "per_nurse_counts" in result
        assert "variance_reports" in result
        assert "daily_headcount" in result

    def test_repair_schedule(self, db, seed_data):
        from agents_v2.skills import run_skill

        result = run_skill(db, "repair-schedule", {
            "group_id": seed_data["group_id"],
            "year": 2026,
            "month": 4,
            "schedule_id": seed_data["schedule_id"],
        })
        assert "suggestions" in result
        assert isinstance(result["suggestions"], list)


# ═══════════════════════════════════════════════════════════
# Layer 4: Full pipeline E2E tests (real example queries)
# ═══════════════════════════════════════════════════════════


class TestFullPipeline:
    """Full pipeline E2E tests using the user's 23 example queries.

    Each test exercises: concept_typer → grounding → canonicalize → plan → execute → verify
    """

    def _run(self, db, llm, session_context, message: str) -> AgentResponse:
        agent = SchedulingAgent(llm_call=llm)
        return agent.run(db, message, session_context)

    # ── Wanted queries (examples 1-6) ──

    def test_w1_wanted_submissions_list(self, db, llm, seed_data, session_context):
        """이번 달 9병동 원티드 신청 내역"""
        resp = self._run(db, llm, session_context, "이번 달 원티드 신청 내역 보여줘")
        assert resp.error is None or resp.needs_clarification
        # Should return data or ask for clarification

    def test_w2_wanted_by_nurse(self, db, llm, seed_data, session_context):
        """김민지 4월 원티드 조회"""
        resp = self._run(db, llm, session_context, "김민지 4월 원티드 조회해줘")
        assert resp.error is None or resp.needs_clarification

    def test_w3_night_wanted(self, db, llm, seed_data, session_context):
        """야간근무 원티드만 확인"""
        resp = self._run(db, llm, session_context, "야간 원티드만 보여줘")
        assert resp.error is None or resp.needs_clarification

    def test_w5_wanted_submission_count(self, db, llm, seed_data, session_context):
        """원티드 제출 현황"""
        resp = self._run(db, llm, session_context, "원티드 제출 현황 알려줘")
        assert resp.error is None or resp.needs_clarification

    # ── Schedule queries (examples 1-6) ──

    def test_s1_full_schedule(self, db, llm, seed_data, session_context):
        """전체 근무표"""
        resp = self._run(db, llm, session_context, "전체 근무표 보여줘")
        assert resp.error is None or resp.needs_clarification
        if resp.data is not None:
            assert isinstance(resp.data, list)

    def test_s2_nurse_schedule(self, db, llm, seed_data, session_context):
        """김민지 근무 조회"""
        resp = self._run(db, llm, session_context, "김민지 근무 조회해줘")
        assert resp.error is None or resp.needs_clarification

    def test_s3_night_workers(self, db, llm, seed_data, session_context):
        """야간 근무자 보여줘"""
        resp = self._run(db, llm, session_context, "야간 근무자 보여줘")
        assert resp.error is None or resp.needs_clarification

    # ── Mutation queries (examples 7-10) ──

    def test_w7_cancel_wanted(self, db, llm, seed_data, session_context):
        """내 원티드 취소"""
        resp = self._run(db, llm, session_context, "내 원티드 취소해줘")
        # Should either execute or ask for clarification
        assert resp.error is None or resp.needs_clarification

    def test_s9_change_shift(self, db, llm, seed_data, session_context):
        """김민지 D→E 변경"""
        resp = self._run(db, llm, session_context, "김민지 D E 변경해줘")
        assert resp.error is None or resp.needs_clarification

    # ── Analysis queries (examples 7, 11) ──

    def test_s7_consecutive_nights(self, db, llm, seed_data, session_context):
        """연속 3일 이상 나이트 사람"""
        resp = self._run(db, llm, session_context, "연속 3일 이상 나이트 근무자 보여줘")
        assert resp.error is None or resp.needs_clarification


# ═══════════════════════════════════════════════════════════
# Layer 5: Harness integration tests
# ═══════════════════════════════════════════════════════════


class TestHarnessIntegration:
    """Verify .aide harness loads correctly and integrates with the pipeline."""

    def test_agents_prompt_loads(self, session_context):
        from agents_v2.harness.agents_loader import load_agents_prompt

        prompt = load_agents_prompt(
            year=session_context["year"],
            month=session_context["month"],
            group_id=session_context["group_id"],
            user_role=session_context["user_role"],
        )
        assert "2026" in prompt
        assert "GRP001" in prompt

    def test_topic_selection(self):
        from agents_v2.harness.topic_selector import select_topics
        from agents_v2.schemas.grounded_concept import ConceptType

        topics = select_topics([ConceptType.WORKFLOW])
        assert "workflow-patterns" in topics

    def test_skill_listing(self):
        from agents_v2.harness.skill_matcher import list_skills

        skills = list_skills()
        names = {s.name for s in skills}
        assert "query-schedule" in names
        assert "bulk-mutation" in names
        assert len(skills) == 9

    def test_skill_load(self):
        from agents_v2.harness.skill_matcher import load_skill

        content = load_skill("query-schedule")
        assert content is not None
        assert "query-schedule" in content

    def test_system_prompt_assembly(self, llm, session_context):
        agent = SchedulingAgent(llm_call=llm)
        prompt = agent.build_system_prompt(session_context)
        assert "2026" in prompt
        assert "query-schedule" in prompt
