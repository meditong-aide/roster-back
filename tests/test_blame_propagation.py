"""Blame propagation scorer 테스트 — 결정론·다중홉·설명가능·그룹 롤업.

검증 관점: (1) 수렴충돌 증폭, (2) derived_from 다중홉 근본원인 귀속,
(3) reduces 로 공급 감소 주체(누구) 지목, (4) constrains 로 그룹 롤업,
(5) 결정론, (6) 사이클 fallback, (7) builder 통합(monthly→daily derived_from emit).
"""

from __future__ import annotations

import pytest

from services.ontology_graph.blame import ALPHA, W_HARD, W_REL, score_blame
from services.ontology_graph.schema import (
    ConstraintNode,
    DomainObjectNode,
    OntologyGraph,
    StateNode,
)


def _state(g, nid, shortage, hard=True):
    g.add_node(StateNode(node_id=nid, label=nid, state_type="supply_demand",
                         severity="hard" if hard else "none",
                         evidence={"shortage": shortage}))


def _constraint(g, nid, family="CoverageMin", operator=">="):
    g.add_node(ConstraintNode(node_id=nid, label=nid, family=family, operator=operator))


def _obj(g, nid, object_type):
    g.add_node(DomainObjectNode(node_id=nid, label=nid, object_type=object_type))


def test_convergent_states_amplify_shared_constraint():
    """서로 다른 부족이 한 제약에 수렴하면 그 제약 blame 이 합산 증폭된다."""
    g = OntologyGraph()
    _state(g, "sA", 3)
    _state(g, "sB", 2)
    _constraint(g, "C")
    g.add_edge("pressures", "sA", "C")
    g.add_edge("pressures", "sB", "C")

    r = score_blame(g)
    assert r.scores["C"] == pytest.approx(ALPHA * W_REL["pressures"] * (3 * W_HARD + 2 * W_HARD))
    assert r.top_constraints[0].node_id == "C"
    assert len(r.top_constraints[0].reasons) == 2   # 두 seed 로 정확히 분해


def test_derived_from_routes_blame_to_root():
    """daily(파생) → monthly(근본) derived_from 로 근본원인이 상류에 누적된다(다중홉)."""
    g = OntologyGraph()
    _state(g, "daily", 2)
    _state(g, "monthly", 1)
    g.add_edge("derived_from", "daily", "monthly")

    r = score_blame(g)
    expected = 1 * W_HARD + ALPHA * W_REL["derived_from"] * (2 * W_HARD)
    assert r.scores["monthly"] == pytest.approx(expected)
    # 설명력: monthly blame 분해에 daily seed 가 잡혀야
    assert "daily" in r.contrib["monthly"]


def test_reduces_blames_supply_removing_object():
    """공급을 깎는 주체(희망OFF 등)를 '누구' 축에서 근본원인으로 지목."""
    g = OntologyGraph()
    _state(g, "S", 4)
    _obj(g, "wo", "wanted_off")
    g.add_edge("reduces", "wo", "S")   # object → state

    r = score_blame(g)
    assert r.scores["wo"] == pytest.approx(ALPHA * W_REL["reduces_inv"] * (4 * W_HARD))
    assert r.top_objects and r.top_objects[0].node_id == "wo"


def test_constrains_rolls_blame_up_to_group():
    """constrains(constraint→team)로 그룹(팀/등급) 문제 랭킹이 나온다."""
    g = OntologyGraph()
    _state(g, "S", 2)
    _constraint(g, "tm", family="TeamMin")
    _obj(g, "team:T", "team")
    g.add_edge("pressures", "S", "tm")
    g.add_edge("constrains", "tm", "team:T")

    r = score_blame(g)
    c_blame = ALPHA * W_REL["pressures"] * (2 * W_HARD)
    assert r.scores["tm"] == pytest.approx(c_blame)
    assert r.scores["team:T"] == pytest.approx(ALPHA * W_REL["constrains"] * c_blame)
    assert r.top_groups and r.top_groups[0].node_id == "team:T"


def test_hard_outweighs_soft_seed():
    """같은 shortage 라도 hard 가 soft 보다 W_HARD 배 무겁다."""
    g = OntologyGraph()
    _state(g, "h", 1, hard=True)
    _state(g, "s", 1, hard=False)
    r = score_blame(g)
    assert r.scores["h"] == pytest.approx(W_HARD)
    assert r.scores["s"] == pytest.approx(1.0)


def test_deterministic():
    g = OntologyGraph()
    _state(g, "sA", 3)
    _state(g, "sB", 2)
    _constraint(g, "C")
    g.add_edge("pressures", "sA", "C")
    g.add_edge("pressures", "sB", "C")
    assert score_blame(g).scores == score_blame(g).scores


def test_cycle_falls_back_and_still_scores():
    """무순환 위배(derived_from 사이클) 시 반복 fallback 으로도 점수는 나온다."""
    g = OntologyGraph()
    _state(g, "a", 1)
    _state(g, "b", 1)
    g.add_edge("derived_from", "a", "b")
    g.add_edge("derived_from", "b", "a")

    r = score_blame(g)
    assert r.converged is False
    assert r.scores["a"] > 0 and r.scores["b"] > 0


def test_integration_monthly_derived_from_emitted_and_scored():
    """builder 가 monthly→daily derived_from 을 emit 하고 스코어러가 끝까지 돈다."""
    from services.ontology_graph.builder import UnifiedGraphInput, build_unified_graph
    from services.ontology_graph.supply_demand import (
        NurseSupply,
        compute_monthly_supply_demand,
    )

    DAYS = 5
    ids = [f"n{i}" for i in range(20)]
    gi = UnifiedGraphInput(
        nurses=[{"nurse_id": i, "grade": 1, "team_id": None} for i in ids],
        num_days=DAYS, work_shifts=["D", "E", "N"],
        requirements_by_day={d: {"D": 6, "E": 6, "N": 6} for d in range(DAYS)})
    sup = [NurseSupply(nurse_id=i, eligible_by_day={d: {"D", "E", "N"} for d in range(DAYS)})
           for i in ids]
    mon = compute_monthly_supply_demand(
        sup, {"D": 6 * DAYS, "E": 6 * DAYS, "N": 6 * DAYS},
        workdays_by_nurse={i: 2 for i in ids})   # 근무가능일 2일 → 월 capacity 부족

    g = build_unified_graph(gi, monthly_supply_demand=mon)
    assert "derived_from" in g.stats()["by_relation"]

    r = score_blame(g)
    assert r.converged
    # 월별 근본원인 state 가 blame 을 받아 랭킹에 오른다
    assert any(s.node_id.startswith("state:monthly_supply_demand") for s in r.ranked)
