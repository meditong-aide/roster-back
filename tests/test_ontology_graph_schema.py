"""Unified ontology graph schema — 4종 노드 + 엣지 거버넌스 회귀 테스트.

설계: docs/UNIFIED_ONTOLOGY_GRAPH_SCHEMA.md
"""

import pytest

from services.ontology_graph import (
    ActionNode,
    ConstraintNode,
    DomainObjectNode,
    GraphValidationError,
    OntologyGraph,
    StateNode,
)


def _research_plan_path() -> OntologyGraph:
    """연구계획 대표 인과경로:
    wanted_off --reduces--> supply_demand --pressures--> coverage_min
    + action --mitigates--> coverage_min
    """
    g = OntologyGraph()
    g.add_node(DomainObjectNode(node_id="nurse:X", label="간호사X", object_type="nurse"))
    g.add_node(DomainObjectNode(node_id="wanted_off:X:d12", label="희망OFF X day12", object_type="wanted_off"))
    g.add_node(
        StateNode(
            node_id="state:supply:d12:N",
            label="day12 N 수요/가용",
            state_type="supply_demand",
            severity="hard",
            evidence={"required": 3, "available": 2, "shortage": 1},
        )
    )
    g.add_node(
        ConstraintNode(
            node_id="cov:min:d12:N", label="day12 N 최소인원",
            family="coverage", operator=">=", target=3, severity="hard",
        )
    )
    g.add_node(
        ActionNode(
            node_id="act:thr:dailyN:d12", label="일별 N 요구 -1",
            action_type="set_threshold", target_family="coverage",
            config_key="daily_shift_requirements", direction="decrease", cost=2.0,
        )
    )
    g.add_edge("reduces", "wanted_off:X:d12", "state:supply:d12:N")
    g.add_edge("supplied_by", "state:supply:d12:N", "nurse:X")
    g.add_edge("pressures", "state:supply:d12:N", "cov:min:d12:N")
    g.add_edge("mitigates", "act:thr:dailyN:d12", "cov:min:d12:N")
    return g


def test_four_node_kinds_set_by_post_init():
    g = _research_plan_path()
    assert g.get("nurse:X").kind == "domain_object"
    assert g.get("state:supply:d12:N").kind == "state"
    assert g.get("cov:min:d12:N").kind == "constraint"
    assert g.get("act:thr:dailyN:d12").kind == "action"


def test_stats_counts_all_four_kinds():
    stats = _research_plan_path().stats()
    assert stats["nodes"] == 5
    assert stats["edges"] == 4
    assert stats["by_kind"] == {"domain_object": 2, "state": 1, "constraint": 1, "action": 1}


def test_causal_path_is_traversable():
    g = _research_plan_path()
    # 무엇이 coverage 제약을 압박하는가 → shortage state
    pressed_by = [e.source_id for e in g.in_edges("cov:min:d12:N", "pressures")]
    assert pressed_by == ["state:supply:d12:N"]
    # 무엇이 그 제약을 완화하는가 → action
    mitigated_by = [e.source_id for e in g.in_edges("cov:min:d12:N", "mitigates")]
    assert mitigated_by == ["act:thr:dailyN:d12"]
    # 희망OFF가 가용을 줄이는 경로
    reduces = [e.target_id for e in g.out_edges("wanted_off:X:d12", "reduces")]
    assert reduces == ["state:supply:d12:N"]


def test_governance_rejects_illegal_endpoint_kinds():
    g = _research_plan_path()
    # domain_object 는 constraint 를 mitigate 할 수 없음 (action 만 가능)
    with pytest.raises(GraphValidationError):
        g.add_edge("mitigates", "nurse:X", "cov:min:d12:N")
    # state 가 domain_object 를 pressures 할 수 없음 (head 는 constraint 여야)
    with pytest.raises(GraphValidationError):
        g.add_edge("pressures", "state:supply:d12:N", "nurse:X")


def test_governance_rejects_unknown_nodes():
    g = _research_plan_path()
    with pytest.raises(GraphValidationError):
        g.add_edge("pressures", "state:supply:d12:N", "no:such:node")
    with pytest.raises(GraphValidationError):
        g.add_edge("reduces", "ghost", "state:supply:d12:N")


def test_duplicate_node_id_same_kind_is_idempotent():
    g = OntologyGraph()
    a = g.add_node(ConstraintNode(node_id="c1", label="첫번째", family="coverage"))
    b = g.add_node(ConstraintNode(node_id="c1", label="중복", family="team_min"))
    assert a is b  # 기존 노드 반환, 덮어쓰지 않음
    assert g.stats()["nodes"] == 1


def test_duplicate_node_id_different_kind_raises():
    g = OntologyGraph()
    g.add_node(ConstraintNode(node_id="x", label="제약"))
    with pytest.raises(GraphValidationError):
        g.add_node(StateNode(node_id="x", label="상태"))
