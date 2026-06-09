"""MUS → 그래프 bridge: 실제 시퀀스 충돌이 그래프 위에서 진단·추천되는지.

전체 경로: CP-SAT 시퀀스 충돌 → extract_conflict_cores(MUS) → cores_to_conflict_graph
→ 4-노드 그래프(전이금지 제약 + 동시불가 state + 완화 action) → recommend.
"""

from ortools.sat.python import cp_model

from services.cp_sat.hard_assumption import HardAssumptionRegistry, add_hard
from services.ontology_graph.schema import OntologyGraph
from services.ontology_graph.mus_bridge import cores_to_conflict_graph
from services.ontology_graph.recommender import recommend_actions

ALLOWED_KINDS = {"constraint", "domain_object", "state", "action"}
ALLOWED_RELATIONS = {"constrains", "requires", "supplied_by", "reduces",
                     "belongs_to", "pressures", "mitigates", "derived_from"}


def _seq_cores():
    m = cp_model.CpModel()
    reg = HardAssumptionRegistry(m)
    shifts = ["D", "E", "N", "O"]
    x = {(d, s): m.NewBoolVar(f"x_{d}_{s}") for d in range(2) for s in shifts}
    for d in range(2):
        m.Add(sum(x[(d, s)] for s in shifts) == 1)
    m.Add(x[(0, "N")] == 1)
    m.Add(x[(1, "D")] == 1)
    add_hard(m, reg, name="TransitionBan:N->D:day0",
             constraint_expr=(x[(0, "N")] + x[(1, "D")] <= 1),
             meta={"node_id": "transition:N->D:day0", "type": "TransitionBanNode",
                   "label": "전이금지 N→D", "scope": "nurse", "scope_key": "nurse_seq",
                   "pattern": "transition_ban"})
    reg.attach_to_model()
    solver = cp_model.CpSolver()
    solver.Solve(m)
    return reg.extract_conflict_cores(solver, solver_phase="primary")


def test_mus_core_becomes_graph_conflict():
    cores = _seq_cores()
    assert cores, "시퀀스 MUS core 추출 실패(전제)"
    g = cores_to_conflict_graph(OntologyGraph(), cores)
    # 전이금지 제약 노드 + 동시불가 state + 완화 action 생성
    cons = g.nodes_of_kind("constraint")
    states = g.nodes_of_kind("state")
    actions = g.nodes_of_kind("action")
    assert any(c.family == "BoundaryTransitionBan" for c in cons)
    assert any(s.attrs.get("mus") for s in states)
    assert actions


def test_mus_state_pressures_constraint_and_action_mitigates():
    g = cores_to_conflict_graph(OntologyGraph(), _seq_cores())
    tb = next(c for c in g.nodes_of_kind("constraint") if c.family == "BoundaryTransitionBan")
    ins = {e.relation for e in g.in_edges(tb.node_id)}
    assert "pressures" in ins   # 동시불가 state → pressures → 전이금지
    assert "mitigates" in ins   # action → mitigates → 전이금지


def test_recommender_proposes_relaxation_for_sequence_conflict():
    g = cores_to_conflict_graph(OntologyGraph(), _seq_cores())
    actions = recommend_actions(g)
    assert actions, "시퀀스 충돌에 대한 추천 액션 없음"
    assert actions[0].target_family == "BoundaryTransitionBan"


def test_schema_invariant_for_mus_bridge():
    g = cores_to_conflict_graph(OntologyGraph(), _seq_cores())
    kinds = {n.kind for n in g.nodes.values()}
    rels = {e.relation for e in g.edges}
    assert kinds <= ALLOWED_KINDS
    assert rels <= ALLOWED_RELATIONS
