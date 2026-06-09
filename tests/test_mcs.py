"""MCS — MUS 의 쌍대. '무엇을 풀면 feasible 한지' 검증된 최소집합.

핵심: 같은 충돌에서 MUS 는 동시불가 '집합'(≥2)을 주지만, MCS 는 '풀어야 할 최소
제약'(보통 1개)을 주고 재실행으로 feasible 을 확인한다 → 더 정확·실행가능.
"""

from ortools.sat.python import cp_model

from services.cp_sat.hard_assumption import HardAssumptionRegistry, add_hard
from services.cp_sat.mcs import find_mcs


def _contradiction_model():
    """x>=5 ∧ x<=3 모순 + 무고한 제약 2개(y>=1, z<=10)."""
    m = cp_model.CpModel()
    reg = HardAssumptionRegistry(m)
    x = m.NewIntVar(0, 10, "x")
    y = m.NewIntVar(0, 10, "y")
    z = m.NewIntVar(0, 10, "z")
    add_hard(m, reg, name="A:x_ge_5", constraint_expr=x >= 5,
             meta={"node_id": "a", "pattern": "lower", "label": "x≥5"})
    add_hard(m, reg, name="B:x_le_3", constraint_expr=x <= 3,
             meta={"node_id": "b", "pattern": "upper", "label": "x≤3"})
    add_hard(m, reg, name="C:y_ge_1", constraint_expr=y >= 1,
             meta={"node_id": "c", "pattern": "innocent", "label": "y≥1"})
    add_hard(m, reg, name="D:z_le_10", constraint_expr=z <= 10,
             meta={"node_id": "d", "pattern": "innocent", "label": "z≤10"})
    return m, reg


def test_mcs_returns_single_constraint_not_whole_conflict():
    m, reg = _contradiction_model()
    res = find_mcs(m, reg, time_limit=5)
    assert res is not None
    # MCS = 1개만 (A 또는 B) — 전체 충돌집합이 아님
    assert len(res.relaxed) == 1
    assert res.relaxed[0] in ("A:x_ge_5", "B:x_le_3")
    # 무고한 제약은 절대 완화 대상 아님
    assert "C:y_ge_1" not in res.relaxed
    assert "D:z_le_10" not in res.relaxed


def test_mcs_is_verified_feasible():
    m, reg = _contradiction_model()
    res = find_mcs(m, reg, time_limit=5)
    assert res.verified_feasible is True   # 완화 적용 후 재실행 feasible 확인됨


def test_mcs_cost_prefers_cheaper_relaxation():
    """B 를 더 싸게 두면 MCS 가 B 를 고른다 (최소비용 완화)."""
    m, reg = _contradiction_model()
    cost = {"A:x_ge_5": 10.0, "B:x_le_3": 1.0}
    res = find_mcs(m, reg, cost=lambda n: cost.get(n, 5.0), time_limit=5)
    assert res.relaxed == ["B:x_le_3"]


def test_mcs_vs_mus_precision():
    """같은 모델: MUS 는 ≥2 멤버(집합), MCS 는 1 (수선점)."""
    m, reg = _contradiction_model()
    reg.attach_to_model()
    solver = cp_model.CpSolver()
    assert solver.Solve(m) == cp_model.INFEASIBLE
    mus = reg.extract_conflict_cores(solver)
    mus_members = sum(len(c["members"]) for c in mus)
    m2, reg2 = _contradiction_model()
    mcs = find_mcs(m2, reg2, time_limit=5)
    assert mus_members >= 2          # MUS: 동시불가 집합
    assert len(mcs.relaxed) == 1     # MCS: 풀어야 할 최소 1개


def test_mcs_sequence_conflict():
    """시퀀스 충돌(전이금지)도 MCS 가 그 제약 1개를 수선점으로 짚는다."""
    m = cp_model.CpModel()
    reg = HardAssumptionRegistry(m)
    shifts = ["D", "E", "N", "O"]
    x = {(d, s): m.NewBoolVar(f"x_{d}_{s}") for d in range(2) for s in shifts}
    for d in range(2):
        m.Add(sum(x[(d, s)] for s in shifts) == 1)
    m.Add(x[(0, "N")] == 1)
    m.Add(x[(1, "D")] == 1)
    add_hard(m, reg, name="TransitionBanN2D",
             constraint_expr=(x[(0, "N")] + x[(1, "D")] <= 1),
             meta={"node_id": "tb", "type": "TransitionBanNode", "pattern": "transition_ban",
                   "label": "전이금지 N→D"})
    res = find_mcs(m, reg, time_limit=5)
    assert res.relaxed == ["TransitionBanN2D"]
    assert res.verified_feasible is True
    assert res.relaxed_meta[0]["pattern"] == "transition_ban"


def test_feasible_model_has_empty_mcs():
    m = cp_model.CpModel()
    reg = HardAssumptionRegistry(m)
    x = m.NewIntVar(0, 10, "x")
    add_hard(m, reg, name="A", constraint_expr=x >= 2, meta={"node_id": "a"})
    res = find_mcs(m, reg, time_limit=5)
    assert res.relaxed == []
    assert res.verified_feasible is True


def test_mcs_to_graph_bridge():
    """MCS → 4-노드 그래프: verified state + 완화 제약 + 액션, 스키마 불변."""
    from services.ontology_graph.schema import OntologyGraph
    from services.ontology_graph.mus_bridge import mcs_to_graph
    from services.ontology_graph.recommender import recommend_actions
    m, reg = _contradiction_model()
    res = find_mcs(m, reg, cost=lambda n: {"A:x_ge_5": 10.0, "B:x_le_3": 1.0}.get(n, 5.0), time_limit=5)
    g = mcs_to_graph(OntologyGraph(), res)
    # verified state 존재 + 완화 제약 노드 + 액션
    assert any(s.attrs.get("mcs") and s.attrs.get("verified") for s in g.nodes_of_kind("state"))
    assert g.nodes_of_kind("constraint")
    actions = recommend_actions(g)
    assert actions  # MCS 수선점이 추천 액션으로 도출
    # 스키마 불변
    kinds = {n.kind for n in g.nodes.values()}
    rels = {e.relation for e in g.edges}
    assert kinds <= {"constraint", "domain_object", "state", "action"}
    assert rels <= {"constrains", "requires", "supplied_by", "reduces", "belongs_to",
                    "pressures", "mitigates", "derived_from"}
