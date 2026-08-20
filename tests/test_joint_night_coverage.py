"""다인 N-커버리지 결합 — 조인트 frontier DP. "각자는 되는데 같이는 못 채움"."""

from __future__ import annotations

from services.ontology_graph.joint_coverage import (
    detect_joint_night_infeasible,
    joint_night_coverage_feasible,
)
from services.ontology_graph.lagrangian import explain_infeasibility_from_config

_RULES = {"two_offs_after_three_nig": True, "not_one_night": True}


def _cfg(demand_n):
    return dict(_RULES, daily_shift_requirements={"D": 2, "E": 1, "N": demand_n},
               initial_constraints={"forbidden": {}, "forced_off": {}})


def _n_only(k):
    return [{"nurse_id": f"n{i}", "name": f"N{i}", "allowed_shifts": ["N"]} for i in range(k)]


def test_two_night_only_cannot_cover_two_per_day():
    """각자 OK, 같이 X: 2 N전담이 매일 2명 야간을 3연속 규칙상 못 댐."""
    assert joint_night_coverage_feasible(_n_only(2), _cfg(2), 31) is False


def test_two_night_only_can_cover_one_per_day():
    """2명이 번갈아 하루 1명은 커버 가능."""
    assert joint_night_coverage_feasible(_n_only(2), _cfg(1), 31) is True


def test_single_night_only_cannot_cover_daily():
    """1명은 3N2OFF로 매일 못 섬."""
    assert joint_night_coverage_feasible(_n_only(1), _cfg(1), 31) is False


def test_large_pool_inconclusive():
    """N-pool 이 크면(폭발 방지) 판정 유보(None) → 솔버 위임."""
    assert joint_night_coverage_feasible(_n_only(9), _cfg(1), 31) is None


def test_detect_isolates_bottleneck():
    d = detect_joint_night_infeasible(_n_only(2), _cfg(2), 31)
    assert d is not None and d["top_family"] == "night_coverage"
    assert d["demand_n"] == 2 and len(d["culprits"]) >= 1


def test_explain_classifies_coverage_shortage():
    """explain 이 결합-N 을 coverage_shortage/night_coverage 로 분류(probe 스킵 대상)."""
    nurses = _n_only(2) + [{"nurse_id": "gd", "name": "주간", "allowed_shifts": ["D", "E"]}]
    e = explain_infeasibility_from_config(nurses, _cfg(2), 31, year=2026, month=8)
    assert e.classification == "coverage_shortage"
    assert e.top_family == "night_coverage"
    assert "야간" in e.certificate
