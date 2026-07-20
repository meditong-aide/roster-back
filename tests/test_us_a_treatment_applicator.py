"""US-A1~A5 검증 — Treatment apply orchestrator (Verified Resolution Phase 4).

핵심 invariants:
  1. treatment_id list 가 auto/manual/unresolved 정확히 분류
  2. apply 멱등 — 같은 입력 두 번 → 같은 output
  3. rollback snapshot 이 deepcopy (caller 가 patched_config 수정해도 snapshot 불변)
  4. data_correction_required treatment 는 auto 적용 X, manual_required 로 분리
  5. violation_delta 계산 정확 (cause_id 별 before/after)
  6. new_causes 감지 — 부작용 식별
  7. EvidenceNode status: FEASIBLE / PARTIAL / INFEASIBLE 정확
"""

from __future__ import annotations

import copy
import pytest

from services.treatment_applicator import (
    ApplyPlan,
    ApplyResult,
    apply_treatments,
    build_apply_plan,
    build_evidence_from_apply,
    compute_violation_delta,
    detect_new_causes,
    rollback_config,
)
from services.semantics.ontology import get_default_ontology


# ─────────────────────────────────────────────────────────────────────────
# build_apply_plan
# ─────────────────────────────────────────────────────────────────────────
def test_build_apply_plan_auto_only():
    plan = build_apply_plan([
        "treatment:soft:grade_max",
        "treatment:disable:team_min",
    ])
    assert len(plan.auto_adjustments) == 2
    assert plan.manual_required == []
    assert plan.unresolved_treatment_ids == []
    families = {a.family for a in plan.auto_adjustments}
    assert "GradeMax" in families
    assert "TeamMin" in families


def test_build_apply_plan_manual_only():
    plan = build_apply_plan(["treatment:data:fix_config", "treatment:data:reduce_fixed"])
    assert plan.auto_adjustments == []
    assert plan.manual_required == ["treatment:data:fix_config", "treatment:data:reduce_fixed"]
    assert plan.unresolved_treatment_ids == []


def test_build_apply_plan_mixed():
    plan = build_apply_plan([
        "treatment:soft:grade_max",       # auto
        "treatment:data:fix_config",       # manual
        "treatment:unknown:xyz",           # unresolved
    ])
    assert len(plan.auto_adjustments) == 1
    assert plan.manual_required == ["treatment:data:fix_config"]
    assert plan.unresolved_treatment_ids == ["treatment:unknown:xyz"]


def test_build_apply_plan_empty():
    plan = build_apply_plan([])
    assert plan.auto_adjustments == []
    assert plan.manual_required == []
    assert plan.unresolved_treatment_ids == []


# ─────────────────────────────────────────────────────────────────────────
# apply_treatments — 멱등 + rollback snapshot
# ─────────────────────────────────────────────────────────────────────────
def test_apply_treatments_idempotent():
    cfg = {"team_min_by_team": {1: {"D": 2}}}
    r1 = apply_treatments(["treatment:soft:team_min"], cfg)
    r2 = apply_treatments(["treatment:soft:team_min"], cfg)
    # 같은 입력 → 같은 patched_config (적용된 결과)
    assert r1.patched_config["team_min_soft_fallback"] == r2.patched_config["team_min_soft_fallback"]


def test_apply_treatments_does_not_mutate_input():
    cfg = {"team_min_by_team": {1: {"D": 2}}, "x": 1}
    cfg_clone = copy.deepcopy(cfg)
    apply_treatments(["treatment:disable:team_min"], cfg)
    assert cfg == cfg_clone, "input config 가 mutate 됨"


def test_apply_treatments_snapshot_isolates_from_patched():
    cfg = {"team_min_by_team": {1: {"D": 2}}}
    r = apply_treatments(["treatment:disable:team_min"], cfg)
    # patched 와 snapshot 이 독립 (mutate 해도 snapshot 안 변함)
    r.patched_config["_extra"] = "modified"
    assert "_extra" not in r.original_config_snapshot


def test_apply_treatments_force_soft_team_min_sets_flag():
    cfg = {"team_min_by_team": {1: {"D": 2}}}
    r = apply_treatments(["treatment:soft:team_min"], cfg)
    assert r.patched_config.get("team_min_soft_fallback") is True


def test_apply_treatments_disable_team_min_clears_dict():
    cfg = {"team_min_by_team": {1: {"D": 2}, 2: {"E": 1}}}
    r = apply_treatments(["treatment:disable:team_min"], cfg)
    assert r.patched_config.get("team_min_by_team") == {}


def test_apply_treatments_manual_not_applied():
    cfg = {"team_min_by_team": {1: {"D": 2}}}
    r = apply_treatments(["treatment:data:fix_config"], cfg)
    # manual treatment 는 config 안 건드림
    assert r.patched_config.get("team_min_by_team") == {1: {"D": 2}}
    assert "treatment:data:fix_config" in r.manual_required
    assert r.applied_treatment_ids == []


def test_apply_treatments_mixed_partial_apply():
    cfg = {"team_min_by_team": {1: {"D": 2}}}
    r = apply_treatments([
        "treatment:soft:team_min",     # auto
        "treatment:data:fix_config",   # manual
        "nonexistent",                  # unresolved
    ], cfg)
    assert r.applied_treatment_ids == ["treatment:soft:team_min"]
    assert r.manual_required == ["treatment:data:fix_config"]
    assert r.unresolved_treatment_ids == ["nonexistent"]
    assert r.patched_config.get("team_min_soft_fallback") is True


# ─────────────────────────────────────────────────────────────────────────
# rollback
# ─────────────────────────────────────────────────────────────────────────
def test_rollback_returns_snapshot():
    cfg = {"team_min_by_team": {1: {"D": 2}}, "key": "value"}
    r = apply_treatments(["treatment:disable:team_min"], cfg)
    restored = rollback_config(r)
    assert restored == cfg


def test_rollback_is_idempotent():
    cfg = {"k": 1}
    r = apply_treatments([], cfg)
    a = rollback_config(r)
    b = rollback_config(r)
    assert a == b == cfg


# ─────────────────────────────────────────────────────────────────────────
# compute_violation_delta
# ─────────────────────────────────────────────────────────────────────────
def test_violation_delta_full_resolution():
    before = [{"reason_code": "CAPACITY_TOTAL_SHORTAGE"}, {"reason_code": "TEAM_MIN_EXCEEDS_GLOBAL_NEED"}]
    after = []
    d = compute_violation_delta(before, after)
    assert d["CAPACITY_TOTAL_SHORTAGE"] == {"before": 1, "after": 0}
    assert d["TEAM_MIN_EXCEEDS_GLOBAL_NEED"] == {"before": 1, "after": 0}


def test_violation_delta_partial():
    before = [{"reason_code": "CAPACITY_TOTAL_SHORTAGE"}, {"reason_code": "GRADE_MAX_SUM_BELOW_NEED"}]
    after = [{"reason_code": "GRADE_MAX_SUM_BELOW_NEED"}]
    d = compute_violation_delta(before, after)
    assert d["CAPACITY_TOTAL_SHORTAGE"]["after"] == 0
    assert d["GRADE_MAX_SUM_BELOW_NEED"]["after"] == 1


def test_violation_delta_counts_duplicate_codes():
    before = [{"reason_code": "TEAM_MIN_EXCEEDS_TEAM_SIZE"}, {"reason_code": "TEAM_MIN_EXCEEDS_TEAM_SIZE"}]
    after = [{"reason_code": "TEAM_MIN_EXCEEDS_TEAM_SIZE"}]
    d = compute_violation_delta(before, after)
    assert d["TEAM_MIN_EXCEEDS_TEAM_SIZE"] == {"before": 2, "after": 1}


# ─────────────────────────────────────────────────────────────────────────
# detect_new_causes
# ─────────────────────────────────────────────────────────────────────────
def test_detect_new_causes_finds_side_effect():
    before = [{"reason_code": "GRADE_MAX_SUM_BELOW_NEED"}]
    after = [{"reason_code": "TEAM_MIN_EXCEEDS_GLOBAL_NEED"}]
    new = detect_new_causes(before, after)
    assert len(new) == 1
    assert new[0]["reason_code"] == "TEAM_MIN_EXCEEDS_GLOBAL_NEED"


def test_detect_new_causes_returns_empty_when_subset():
    before = [{"reason_code": "A"}, {"reason_code": "B"}]
    after = [{"reason_code": "A"}]
    new = detect_new_causes(before, after)
    assert new == []


# ─────────────────────────────────────────────────────────────────────────
# build_evidence_from_apply
# ─────────────────────────────────────────────────────────────────────────
def test_evidence_verified_when_feasible_and_all_resolved():
    apply_r = apply_treatments(["treatment:soft:grade_max"], {})
    ev = build_evidence_from_apply(
        apply_result=apply_r,
        before_causes=[{"reason_code": "GRADE_MAX_SUM_BELOW_NEED"}],
        after_causes=[],
        solver_status="FEASIBLE",
        witness_schedule_id="sched_42",
    )
    assert ev["status"] == "FEASIBLE"
    assert ev["verified"] is True
    assert ev["witness_schedule_id"] == "sched_42"
    assert ev["new_causes"] == []


def test_evidence_partial_when_feasible_but_some_remain():
    apply_r = apply_treatments(["treatment:soft:grade_max"], {})
    ev = build_evidence_from_apply(
        apply_result=apply_r,
        before_causes=[{"reason_code": "GRADE_MAX_SUM_BELOW_NEED"}, {"reason_code": "TEAM_MIN_EXCEEDS_GLOBAL_NEED"}],
        after_causes=[{"reason_code": "TEAM_MIN_EXCEEDS_GLOBAL_NEED"}],
        solver_status="FEASIBLE",
        witness_schedule_id="sched_43",
    )
    assert ev["status"] == "PARTIAL"
    assert ev["verified"] is False  # 한 cause 잔존
    delta = ev["violation_delta"]
    assert delta["GRADE_MAX_SUM_BELOW_NEED"]["after"] == 0
    assert delta["TEAM_MIN_EXCEEDS_GLOBAL_NEED"]["after"] == 1


def test_evidence_infeasible_when_solver_fails():
    apply_r = apply_treatments(["treatment:soft:grade_max"], {})
    ev = build_evidence_from_apply(
        apply_result=apply_r,
        before_causes=[{"reason_code": "GRADE_MAX_SUM_BELOW_NEED"}],
        after_causes=[{"reason_code": "GRADE_MAX_SUM_BELOW_NEED"}],
        solver_status="INFEASIBLE",
        witness_schedule_id=None,
    )
    assert ev["status"] == "INFEASIBLE"
    assert ev["verified"] is False
    assert ev["witness_schedule_id"] is None


def test_evidence_detects_new_cause_as_side_effect():
    apply_r = apply_treatments(["treatment:soft:grade_max"], {})
    ev = build_evidence_from_apply(
        apply_result=apply_r,
        before_causes=[{"reason_code": "GRADE_MAX_SUM_BELOW_NEED"}],
        after_causes=[{"reason_code": "TEAM_MIN_EXCEEDS_GLOBAL_NEED"}],
        solver_status="FEASIBLE",
        witness_schedule_id="sched_44",
    )
    # GRADE_MAX 해소됐지만 TEAM_MIN 새로 등장 → PARTIAL
    assert ev["status"] == "PARTIAL"
    assert len(ev["new_causes"]) == 1
    assert ev["new_causes"][0]["reason_code"] == "TEAM_MIN_EXCEEDS_GLOBAL_NEED"


def test_evidence_includes_manual_required_when_present():
    apply_r = apply_treatments(
        ["treatment:soft:grade_max", "treatment:data:fix_config"],
        {},
    )
    ev = build_evidence_from_apply(
        apply_result=apply_r,
        before_causes=[],
        after_causes=[],
        solver_status="FEASIBLE",
        witness_schedule_id="sched_45",
    )
    assert "treatment:data:fix_config" in ev["manual_required_treatment_ids"]


def test_evidence_does_not_leak_cause_keys():
    """US-1 invariant 유지: evidence 안에 cause/reason_code/symptom 키 없음."""
    apply_r = apply_treatments(["treatment:soft:grade_max"], {})
    ev = build_evidence_from_apply(
        apply_result=apply_r,
        before_causes=[],
        after_causes=[],
        solver_status="FEASIBLE",
        witness_schedule_id="sched_46",
    )
    # new_causes 자체는 evidence 안에 있지만 evidence 의 cause/reason_code TOP-LEVEL 키 아님
    forbidden_top_level = {"reason_code", "cause_id", "symptom", "cause"}
    for k in forbidden_top_level:
        assert k not in ev, f"forbidden key {k} in evidence top level"
    # 단 new_causes 는 list[dict] — 각 항목에 reason_code 가 있을 수 있음
    # 그건 cause data 자체이므로 OK (evidence 의 "관찰 결과" 일부)


# ─────────────────────────────────────────────────────────────────────────
# 에이전트 친화 — 멱등 + 부분 적용
# ─────────────────────────────────────────────────────────────────────────
def test_apply_then_rollback_then_apply_again_yields_same_result():
    cfg = {"team_min_by_team": {1: {"D": 2}}}
    r1 = apply_treatments(["treatment:soft:team_min"], cfg)
    restored = rollback_config(r1)
    r2 = apply_treatments(["treatment:soft:team_min"], restored)
    assert r1.patched_config == r2.patched_config
