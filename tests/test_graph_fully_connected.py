"""3개 갭 마감 검증: day 1급 노드 / soft 엣지 연결 / leave·wanted_off reduces.
+ 통합 그래프에 고립(엣지 0) 노드가 없는지(소프트 포함) 확인.
"""

from services.ontology_graph.builder import UnifiedGraphInput, build_unified_graph
from services.ontology_graph.soft_penalty import augment_soft_constraints, compute_soft_penalties


def _full_graph():
    nurses = [{"nurse_id": "a", "grade": 1, "team_id": "1"},
              {"nurse_id": "b", "grade": 2, "team_id": "1"},
              {"nurse_id": "c", "grade": 1, "team_id": "2"}]
    req = {d: {"D": 1, "E": 1, "N": 1} for d in range(3)}
    inp = UnifiedGraphInput(
        nurses=nurses, num_days=3, work_shifts=["D", "E", "N"], requirements_by_day=req,
        team_min={"1": {"D": 1}}, grade_max={"N": {"1": 1}},
        leaves=[{"nurse_id": "a", "day": 0}],
        wanted_offs=[{"nurse_id": "b", "day": 1}],
    )
    g = build_unified_graph(inp)
    roster = {"a": ["N", "O", "D"], "b": ["O", "D", "O"], "c": ["N", "N", "N"]}
    augment_soft_constraints(g, compute_soft_penalties(roster, work_shifts=["D", "E", "N"]),
                             work_shifts=["D", "E", "N"])
    return g


# ── Gap 1: day 1급 노드 ───────────────────────────────────────────────────
def test_day_nodes_exist_and_connected():
    g = _full_graph()
    days = [n for n in g.nodes_of_kind("domain_object") if n.object_type == "day"]
    assert len(days) == 3
    linked = {e.source_id for e in g.edges} | {e.target_id for e in g.edges}
    for d in days:
        assert d.node_id in linked, f"day 노드 {d.node_id} 고립"
    # constraint --constrains--> day 엣지 존재
    assert any(e.relation == "constrains" and e.target_id.startswith("day:") for e in g.edges)


# ── Gap 2: soft 제약 엣지 연결 ────────────────────────────────────────────
def test_soft_constraints_connected():
    g = _full_graph()
    soft = [n for n in g.nodes_of_kind("constraint") if n.severity == "soft"]
    assert soft
    for sc in soft:
        ins = g.in_edges(sc.node_id)
        rels = {e.relation for e in ins}
        assert "pressures" in rels, f"{sc.node_id}: penalty→pressures 없음"
        assert "mitigates" in rels, f"{sc.node_id}: action→mitigates 없음"


def test_soft_penalty_states_and_actions_present():
    g = _full_graph()
    pen_states = [n for n in g.nodes_of_kind("state") if n.state_type == "soft_penalty"]
    soft_actions = [n for n in g.nodes_of_kind("action") if n.attrs.get("soft")]
    assert pen_states and soft_actions


# ── Gap 3: leave / wanted_off → reduces ───────────────────────────────────
def test_leave_and_wanted_off_reduce_supply():
    g = _full_graph()
    reduces = [e for e in g.edges if e.relation == "reduces"]
    assert reduces, "reduces 엣지 없음"
    srcs = {g.nodes[e.source_id].object_type for e in reduces}
    assert "leave" in srcs and "wanted_off" in srcs
    for e in reduces:
        assert g.nodes[e.target_id].state_type == "supply_demand"


# ── 전체: 고립 노드 0 ──────────────────────────────────────────────────────
def test_no_isolated_nodes():
    g = _full_graph()
    linked = {e.source_id for e in g.edges} | {e.target_id for e in g.edges}
    isolated = [nid for nid in g.nodes if nid not in linked]
    assert isolated == [], f"고립 노드 존재: {isolated}"


# ── soft penalty 계산 정확성 ──────────────────────────────────────────────
def test_compute_soft_penalties_values():
    roster = {
        "a": ["N", "O", "D"],     # nod_noe(N-O-D) +1, N=1
        "b": ["O", "D", "O"],     # isolated_work(O-D-O) +1, N=0
        "c": ["D", "O", "D"],     # isolated_off(D-O-D) +1, N=0
        "d": ["N", "N", "N"],     # N=3
    }
    pen = compute_soft_penalties(roster, work_shifts=["D", "E", "N"])
    assert pen["nod_noe"] == 1            # a: N-O-D
    assert pen["isolated_work"] == 1      # b: O-D-O
    assert pen["isolated_off"] == 2       # a: N-O-D (O 고립), c: D-O-D
    assert pen["night_deviation"] == 3    # max N(3, d) - min N(0, b/c)
