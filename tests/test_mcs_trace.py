"""Deletion-based MCS 추적기 — 복합 충돌을 선형 재solve로 조건 단위 추적."""

from __future__ import annotations

from services.ontology_graph.mcs_trace import minimal_correction_set, trace_conflict


def test_mcs_finds_minimal_composite_set_linear():
    """feasible 하려면 {A,B} 둘 다 완화 필요 → MCS={A,B}, 재solve 선형(N+1)."""
    items = ["A", "B", "C", "D", "E"]
    calls = {"n": 0}

    def resolve(relaxed):
        calls["n"] += 1
        return {"A", "B"} <= set(relaxed)      # A,B 둘 다 완화돼야 feasible

    res = minimal_correction_set(resolve, items)
    assert res is not None
    min_set, solves = res
    assert set(min_set) == {"A", "B"}          # 최소 수선집합 정확
    assert solves == len(items) + 1            # 선형: 6회 (2^5=32 아님)
    assert calls["n"] == solves


def test_mcs_none_when_relax_all_still_infeasible():
    """전부 완화해도 infeasible → None(모델 밖 원인)."""
    def resolve(relaxed):
        return False
    assert minimal_correction_set(resolve, ["A", "B"]) is None


def test_trace_conflict_family_then_instance_drilldown():
    """family MCS(weekend_off, monthly_limit) → 각 family 안 culprit 간호사까지 추적."""
    families = ["coverage", "weekend_off", "monthly_limit", "transition", "off_budget"]

    # feasible 하려면 weekend_off + monthly_limit 를 풀어야 함 (2n2off/transition 무관)
    def family_resolve(relaxed):
        return {"weekend_off", "monthly_limit"} <= set(relaxed)

    # family 별 instance: weekend_off 는 김수선만, monthly_limit 도 김수선만 범인
    instances = {"weekend_off": ["김수선", "이영희", "박철수"],
                 "monthly_limit": ["김수선", "정민수"]}

    def instance_resolve(family, relaxed_instances):
        if family == "weekend_off":
            return "김수선" in relaxed_instances       # 김수선만 풀면 됨
        if family == "monthly_limit":
            return "김수선" in relaxed_instances
        return True

    tr = trace_conflict(family_resolve, families,
                        instance_resolve=instance_resolve,
                        instances_by_family=instances)
    assert set(tr.families) == {"weekend_off", "monthly_limit"}   # 얽힌 family
    assert tr.instances_by_family["weekend_off"] == ["김수선"]     # culprit 간호사
    assert tr.instances_by_family["monthly_limit"] == ["김수선"]
    assert tr.feasible_when_all_relaxed
    # 선형: family 6 + 각 family instance drill (3+1, 2+1) = 6+4+3 = 13
    assert tr.solve_count <= 15
    assert "김수선" in tr.certificate and "weekend_off" in tr.certificate


def test_trace_conflict_reports_outside_model_when_unrelaxable():
    """전체 완화로도 안 풀리면 '모델 밖 원인' 안내."""
    tr = trace_conflict(lambda relaxed: False, ["a", "b"])
    assert tr.families == []
    assert not tr.feasible_when_all_relaxed
    assert "모델 밖" in tr.certificate
