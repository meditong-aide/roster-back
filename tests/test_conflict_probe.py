"""Unit tests for conflict_probe — ranking, scenario matching, probe plan."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "app"
for p in (str(ROOT), str(APP)):
    if p not in sys.path:
        sys.path.insert(0, p)

from services.constraint_impact.conflict_probe import (  # noqa: E402
    ConflictProbeReport,
    MatchedScenario,
    ProbeStep,
    RankedCandidate,
    build_conflict_probe_report,
    build_probe_plan,
    match_known_conflict_scenarios,
    rank_relaxation_candidates,
)
from services.constraint_impact.solver_emit import EmittedConstraint  # noqa: E402


def _emit(family: str, mode: str = "enforced", **scope) -> EmittedConstraint:
    return EmittedConstraint(family=family, scope=dict(scope), target="x", mode=mode)


def test_ranking_excludes_bypassed_records():
    records = [
        _emit("TeamMin", team="1", day=1),
        _emit("TeamMin", mode="bypassed_by_fixed", team="1", day=2),
    ]
    ranked = rank_relaxation_candidates(emit_records=records)
    assert len(ranked) == 1
    assert ranked[0].family == "TeamMin"
    assert ranked[0].emit_count == 1  # bypass excluded


def test_ranking_orders_by_relaxation_priority_then_scope():
    records = [
        _emit("TeamMin", team="1", day=1),       # priority=2 high explosion
        _emit("OffCap", nurse_index=0),           # priority=5 low explosion
        _emit("GradeMax", grade=1, day=1),        # priority=2 high explosion
    ]
    ranked = rank_relaxation_candidates(emit_records=records)
    families = [c.family for c in ranked]
    # priority=2 들이 priority=5 보다 위에
    assert families.index("TeamMin") < families.index("OffCap")
    assert families.index("GradeMax") < families.index("OffCap")
    # OffCap 점수는 가장 낮음 (= 풀기 가장 어려움 = 후보로 비추천)
    assert ranked[-1].family == "OffCap"


def test_scenario_matching_full_confidence():
    records = [
        _emit("TeamMin", team="1"),
        _emit("GradeMax", grade=1),
    ]
    matched = match_known_conflict_scenarios(emit_records=records)
    ids = {m.scenario_id for m in matched}
    assert "TEAM_MIN_VS_GRADE_MAX_INTERSECTION" in ids
    full = next(m for m in matched if m.scenario_id == "TEAM_MIN_VS_GRADE_MAX_INTERSECTION")
    assert full.confidence == 1.0


def test_scenario_matching_partial_confidence_filtered():
    # 한 family 만 등장 → confidence 0.5 → default min_confidence 와 동일하게 매칭됨
    records = [_emit("TeamMin", team="1")]
    matched = match_known_conflict_scenarios(emit_records=records)
    # TEAM_MIN_VS_GRADE_MAX_INTERSECTION (TeamMin only) → 0.5
    # ALLOWED_SHIFT_MASK_VS_TEAM_MIN (TeamMin only) → 0.5
    # NIGHT_RECOVERY_VS_COVERAGE_MIN (TeamMin in 3) → ~0.33 → 제외
    ids = {m.scenario_id for m in matched}
    assert "TEAM_MIN_VS_GRADE_MAX_INTERSECTION" in ids
    # 모두 confidence ≥ 0.5
    assert all(m.confidence >= 0.5 for m in matched)


def test_scenario_matching_threshold_filters():
    records = [_emit("TeamMin", team="1")]
    matched = match_known_conflict_scenarios(emit_records=records, min_confidence=0.99)
    assert matched == []


def test_probe_plan_uses_disable_module_action():
    candidates = [
        RankedCandidate(
            family="TeamMin", score=4.0, relaxation_priority=2,
            scope_explosion="high", emit_count=10, matched_scenario_ids=["X"],
        ),
        RankedCandidate(
            family="GradeMax", score=3.5, relaxation_priority=2,
            scope_explosion="high", emit_count=5, matched_scenario_ids=[],
        ),
    ]
    plan = build_probe_plan(ranked_candidates=candidates)
    assert len(plan) == 2
    assert plan[0].order == 1
    assert plan[0].family == "TeamMin"
    assert plan[0].action["family"] == "TeamMin"
    assert plan[0].action["action"] == "disable_module"
    assert "X" in plan[0].rationale


def test_probe_plan_respects_max_steps():
    candidates = [
        RankedCandidate(family=f, score=1.0, relaxation_priority=3,
                        scope_explosion="medium", emit_count=1)
        for f in ("A", "B", "C", "D", "E", "F", "G")
    ]
    plan = build_probe_plan(ranked_candidates=candidates, max_steps=3)
    assert len(plan) == 3


def test_full_probe_report_combines_all_layers():
    records = [
        _emit("TeamMin", team="1"),
        _emit("GradeMax", grade=1),
        _emit("OffCap", nurse_index=0),
    ]
    rep = build_conflict_probe_report(emit_records=records)
    assert isinstance(rep, ConflictProbeReport)
    # ranked 가 모든 enforced family 포함
    fams = {c.family for c in rep.ranked_candidates}
    assert {"TeamMin", "GradeMax", "OffCap"} == fams
    # scenario 매칭 결과가 ranked 에 reflect 됨
    team_min_cand = next(c for c in rep.ranked_candidates if c.family == "TeamMin")
    assert "TEAM_MIN_VS_GRADE_MAX_INTERSECTION" in team_min_cand.matched_scenario_ids
    # plan 첫 step 이 가장 높은 score family
    assert rep.probe_plan[0].family == rep.ranked_candidates[0].family


def test_empty_emit_returns_notes():
    rep = build_conflict_probe_report(emit_records=[])
    assert rep.ranked_candidates == []
    assert rep.matched_scenarios == []
    assert rep.probe_plan == []
    assert any("no emit records" in n for n in rep.notes)


def test_ranked_candidate_sample_records_capped():
    records = [_emit("TeamMin", team="1", day=i) for i in range(20)]
    ranked = rank_relaxation_candidates(emit_records=records, sample_per_family=3)
    assert len(ranked) == 1
    assert len(ranked[0].sample_records) == 3
    assert ranked[0].emit_count == 20
