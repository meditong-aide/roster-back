"""Tests for tool and skill layers against real DB data (SQLite in-memory)."""

from __future__ import annotations

import pytest
from sqlalchemy.orm import Session

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

        entry = find_schedule_entry(db, seed_data["schedule_id"], "N001", datetime(2026, 4, 1), seed_data["group_id"])
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
# Layer 2: Skill tests (verify skills execute against real DB)
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

    def test_query_wanted_campaign(self, db, seed_data):
        from agents_v2.skills import run_skill

        result = run_skill(db, "query-schedule", {
            "group_id": seed_data["group_id"],
            "year": 2026,
            "month": 4,
            "scope": "wanted_campaign",
        })
        assert result["campaign_exists"] is True
        assert result["status"] == "requested"
        assert result["exp_date"] is not None
        assert result["exp_date"].startswith("2026-03-25")
        assert result["is_open"] is False  # 2026-03-25 마감, 현재 시점은 그 이후

    def test_query_wanted_campaign_absent(self, db, seed_data):
        from agents_v2.skills import run_skill

        result = run_skill(db, "query-schedule", {
            "group_id": seed_data["group_id"],
            "year": 2099,
            "month": 12,
            "scope": "wanted_campaign",
        })
        assert result["campaign_exists"] is False
        assert "원티드 캠페인" in result["message"]

    def test_query_wanted_campaign_missing_year_month(self, db, seed_data):
        from agents_v2.skills import run_skill

        result = run_skill(db, "query-schedule", {
            "group_id": seed_data["group_id"],
            "scope": "wanted_campaign",
        })
        assert "error" in result

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

    def test_bulk_mutation_clear_deadline_preview(self, db, seed_data):
        """clear_deadline preview: 현재 마감일 표시 + new_deadline=None."""
        from agents_v2.skills import run_skill

        result = run_skill(db, "bulk-mutation", {
            "group_id": seed_data["group_id"],
            "year": 2026,
            "month": 4,
            "scope": "wanted_submissions",
            "action": "clear_deadline",
            "preview_only": True,
        })
        assert result["preview_only"] is True
        assert result["action"] == "clear_deadline"
        assert result["new_deadline"] is None
        assert result["current_deadline"] is not None  # seed: 2026-03-25

    def test_bulk_mutation_clear_deadline_apply(self, db, seed_data):
        """clear_deadline 적용: DB exp_date 가 NULL 로 바뀐다."""
        from agents_v2.skills import run_skill
        from agents_v2.tools.wanted_tools import get_wanted_status

        result = run_skill(db, "bulk-mutation", {
            "group_id": seed_data["group_id"],
            "year": 2026,
            "month": 4,
            "scope": "wanted_submissions",
            "action": "clear_deadline",
            "preview_only": False,
        })
        assert result["new_deadline"] is None
        assert result["old_deadline"] is not None

        # DB 상태 직접 확인
        status = get_wanted_status(db, seed_data["group_id"], 2026, 4)
        assert status["exp_date"] is None

    def test_bulk_mutation_clear_deadline_missing_campaign(self, db, seed_data):
        """캠페인 부재 시 error."""
        from agents_v2.skills import run_skill

        result = run_skill(db, "bulk-mutation", {
            "group_id": seed_data["group_id"],
            "year": 2099,
            "month": 12,
            "scope": "wanted_submissions",
            "action": "clear_deadline",
            "preview_only": True,
        })
        assert "error" in result

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

    def test_bulk_mutation_schedule_scope_from_llm(self, db, seed_data):
        """LLM sends scope='schedule' + new_shift_code (flat params, no mutation dict)."""
        from agents_v2.skills import run_skill
        from datetime import datetime

        result = run_skill(db, "bulk-mutation", {
            "group_id": seed_data["group_id"],
            "year": 2026,
            "month": 4,
            "scope": "schedule",  # LLM sends this
            "schedule_id": seed_data["schedule_id"],
            "nurse_ids": ["N002"],
            "date": datetime(2026, 4, 2),
            "new_shift_code": "N_GRP001",  # From middleware grounding
            "preview_only": True,
        })
        assert result.get("preview") is True
        assert result.get("new_shift_id") == "N_GRP001"

    def test_bulk_mutation_schedule_scope_execute(self, db, seed_data):
        """LLM-style flat params actually execute the shift change."""
        from agents_v2.skills import run_skill
        from datetime import datetime

        result = run_skill(db, "bulk-mutation", {
            "group_id": seed_data["group_id"],
            "year": 2026,
            "month": 4,
            "scope": "schedule",
            "schedule_id": seed_data["schedule_id"],
            "nurse_ids": ["N003"],
            "date": datetime(2026, 4, 3),
            "new_shift_code": "OFF_GRP001",
            "preview_only": False,
        })
        assert "old_shift_id" in result
        assert result["new_shift_id"] == "OFF_GRP001"

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
