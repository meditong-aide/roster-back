"""충돌(conflict) generic 진단·수선 + 스키마 불변 증명.

서로 다른 충돌 family(TeamMin×GradeMax, GradeMin×GradeMax, CoverageMin×GradeMax)를
**동일한 detect_conflicts / recommend_conflict_repair** 로 처리 — 케이스별 코드 없음.
"""

from services.ontology_graph.schema import (
    ActionNode, ConstraintNode, OntologyGraph,
)
from services.ontology_graph.conflict import (
    Conflict, actions_resolving_multiple, detect_conflicts,
    recommend_conflict_repair, register_capacity_conflict,
)

ALLOWED_KINDS = {"constraint", "domain_object", "state", "action"}
ALLOWED_RELATIONS = {"constrains", "requires", "supplied_by", "reduces",
                     "belongs_to", "pressures", "mitigates", "derived_from"}


def _conflict_graph(min_family, max_family, resource, floor, ceiling, *, with_actions=True):
    g = OntologyGraph()
    mid = f"min:{resource}"
    xid = f"max:{resource}"
    g.add_node(ConstraintNode(node_id=mid, label=f"{min_family} {resource} ≥{floor}",
                              family=min_family, operator=">=", target=floor))
    g.add_node(ConstraintNode(node_id=xid, label=f"{max_family} {resource} ≤{ceiling}",
                              family=max_family, operator="<=", target=ceiling))
    register_capacity_conflict(g, resource=resource, floor=floor, ceiling=ceiling,
                               min_constraint_id=mid, max_constraint_id=xid)
    if with_actions:
        g.add_node(ActionNode(node_id=f"act:soft:{xid}", label=f"{max_family} soft 전환",
                              action_type="force_soft_mode", target_family=max_family, cost=2.0))
        g.add_edge("mitigates", f"act:soft:{xid}", xid)
        g.add_node(ActionNode(node_id=f"act:disable:{mid}", label=f"{min_family} disable",
                              action_type="disable_module", target_family=min_family, cost=6.0))
        g.add_edge("mitigates", f"act:disable:{mid}", mid)
    return g


# ── 같은 검출기가 여러 충돌 family 를 잡는다 ──────────────────────────────────
def test_same_detector_finds_team_grade_conflict():
    g = _conflict_graph("TeamMin", "GradeMax", "team1_grade1_N", floor=3, ceiling=1)
    cs = detect_conflicts(g)
    assert len(cs) == 1
    assert cs[0].families == ("TeamMin", "GradeMax")
    assert cs[0].gap == 2


def test_same_detector_finds_grade_grade_conflict():
    g = _conflict_graph("GradeMin", "GradeMax", "grade1_N", floor=2, ceiling=0)
    cs = detect_conflicts(g)
    assert len(cs) == 1
    assert cs[0].families == ("GradeMin", "GradeMax")


def test_same_detector_finds_coverage_grade_conflict():
    g = _conflict_graph("CoverageMin", "GradeMax", "N", floor=4, ceiling=2)
    cs = detect_conflicts(g)
    assert len(cs) == 1 and cs[0].gap == 2


def test_no_conflict_when_floor_le_ceiling():
    g = _conflict_graph("TeamMin", "GradeMax", "ok", floor=1, ceiling=3)
    assert detect_conflicts(g) == []


# ── 수선: 두 제약 중 최소변경 쪽을 generic 하게 선택 ─────────────────────────
def test_repair_picks_minimal_action():
    g = _conflict_graph("TeamMin", "GradeMax", "team1_grade1_N", floor=3, ceiling=1)
    rep = recommend_conflict_repair(g, detect_conflicts(g)[0])
    assert rep is not None
    # soft(2.0) < disable(6.0) → soft 선택, 전면 disable 아님
    assert rep.action_id.startswith("act:soft")
    assert rep.relaxed_family == "GradeMax"


# ── 한 액션이 여러 충돌을 동시에 푼다 (그래프의 고유 가치) ───────────────────
def test_one_action_resolves_multiple_conflicts():
    g = OntologyGraph()
    # GradeMax(grade1) 가 두 시프트에서 각각 TeamMin 과 충돌
    for shift in ("N", "D"):
        mid = f"min:team1_g1_{shift}"
        xid = f"max:g1_{shift}"
        g.add_node(ConstraintNode(node_id=mid, label="TeamMin", family="TeamMin",
                                  operator=">=", target=3))
        g.add_node(ConstraintNode(node_id=xid, label="GradeMax", family="GradeMax",
                                  operator="<=", target=1))
        register_capacity_conflict(g, resource=f"g1_{shift}", floor=3, ceiling=1,
                                   min_constraint_id=mid, max_constraint_id=xid)
        # 같은 GradeMax soft 액션이 두 시프트 max 제약을 mitigates
        if "act:soft:gradeMax" not in g.nodes:
            g.add_node(ActionNode(node_id="act:soft:gradeMax", label="GradeMax soft 전환",
                                  action_type="force_soft_mode", target_family="GradeMax", cost=2.0))
        g.add_edge("mitigates", "act:soft:gradeMax", xid)
    conflicts = detect_conflicts(g)
    assert len(conflicts) == 2
    multi = actions_resolving_multiple(g, conflicts)
    assert "act:soft:gradeMax" in multi
    assert len(multi["act:soft:gradeMax"]) == 2   # 한 액션이 2개 충돌 동시 해소


# ── 스키마 불변 증명: 모든 충돌 케이스가 같은 4종/8엣지만 쓴다 ──────────────
def test_schema_invariant_across_conflict_families():
    graphs = [
        _conflict_graph("TeamMin", "GradeMax", "r1", 3, 1),
        _conflict_graph("GradeMin", "GradeMax", "r2", 2, 0),
        _conflict_graph("CoverageMin", "GradeMax", "r3", 4, 2),
    ]
    for g in graphs:
        kinds = {n.kind for n in g.nodes.values()}
        rels = {e.relation for e in g.edges}
        assert kinds <= ALLOWED_KINDS, f"새 노드 종류 등장: {kinds - ALLOWED_KINDS}"
        assert rels <= ALLOWED_RELATIONS, f"새 엣지 종류 등장: {rels - ALLOWED_RELATIONS}"
