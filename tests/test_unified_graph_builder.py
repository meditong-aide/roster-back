"""US-003 — 통합 4-노드 그래프 빌더 검증.

합성 입력(가용<수요 = 하드위반)에서 4종 노드가 모두 생성되고,
shortage state --pressures--> constraint --(action mitigates)--> action
경로가 traversable 한지 확인. 이 경로가 US-004 action 추천의 토대.
"""

from services.ontology_graph.builder import UnifiedGraphInput, build_unified_graph


def _infeasible_input():
    # 간호사 2명, day0 에 N 3명 요구 (가용 2 < 3 → shortage 1)
    return UnifiedGraphInput(
        nurses=[
            {"nurse_id": "a", "grade": 1, "team_id": "T1"},
            {"nurse_id": "b", "grade": 2, "team_id": "T1"},
        ],
        num_days=1,
        work_shifts=["D", "E", "N"],
        requirements_by_day={0: {"N": 3}},
        team_min={"T1": {"N": 1}},
        grade_max={"N": {"1": 0}},
    )


def test_all_four_node_kinds_present():
    g = build_unified_graph(_infeasible_input())
    kinds = g.stats()["by_kind"]
    assert kinds.get("constraint", 0) > 0
    assert kinds.get("domain_object", 0) > 0
    assert kinds.get("state", 0) > 0
    assert kinds.get("action", 0) > 0


def test_hard_and_soft_constraints_both_present():
    g = build_unified_graph(_infeasible_input())
    consts = g.nodes_of_kind("constraint")
    severities = {c.severity for c in consts}
    assert "hard" in severities
    assert "soft" in severities          # soft 카탈로그(US-002)도 합류


def test_domain_objects_cover_expected_types():
    g = build_unified_graph(_infeasible_input())
    types = {n.object_type for n in g.nodes_of_kind("domain_object")}
    assert {"nurse", "team", "grade", "shift"}.issubset(types)


def test_supply_demand_state_has_shortage():
    g = build_unified_graph(_infeasible_input())
    states = g.nodes_of_kind("state")
    assert any(s.evidence.get("shortage", 0) > 0 for s in states)


def test_pressures_path_state_to_constraint():
    g = build_unified_graph(_infeasible_input())
    # shortage state 가 적어도 하나의 하드 제약을 pressures
    pressures = [e for e in g.edges if e.relation == "pressures"]
    assert pressures, "shortage → constraint pressures 엣지 없음"
    for e in pressures:
        assert g.nodes[e.source_id].kind == "state"
        assert g.nodes[e.target_id].kind == "constraint"


def test_mitigates_path_action_to_constraint():
    g = build_unified_graph(_infeasible_input())
    mit = [e for e in g.edges if e.relation == "mitigates"]
    assert mit, "action → constraint mitigates 엣지 없음"
    for e in mit:
        assert g.nodes[e.source_id].kind == "action"
        assert g.nodes[e.target_id].kind == "constraint"


def test_full_traversal_shortage_to_action():
    """연구 핵심 경로: shortage → pressures → constraint → (mitigates 역방향) → action."""
    g = build_unified_graph(_infeasible_input())
    bottleneck_states = [s for s in g.nodes_of_kind("state") if s.evidence.get("shortage", 0) > 0]
    assert bottleneck_states
    reached_action = False
    for st in bottleneck_states:
        for pe in g.out_edges(st.node_id, "pressures"):
            cons = pe.target_id
            for me in g.in_edges(cons, "mitigates"):
                if g.nodes[me.source_id].kind == "action":
                    reached_action = True
    assert reached_action, "shortage→constraint→action 완전 경로 미연결"


def test_edge_governance_holds():
    # 빌더가 생성한 모든 엣지가 스키마 거버넌스를 통과해 존재(예외 없이 빌드 완료)
    g = build_unified_graph(_infeasible_input())
    rels = set(g.stats()["by_relation"].keys())
    assert {"requires", "pressures", "mitigates", "belongs_to", "constrains"}.issubset(rels)
