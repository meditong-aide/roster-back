"""포커스 인과경로 서브그래프 → PNG (networkx + matplotlib, headless).

전체 그래프(576 노드)는 못 보므로, 한 케이스의 인과경로만 추출해 그린다:
  [domain objects] ──constrains── constraint ──requires──▶ state(shortage)
                                       ▲                        ▲
                              mitigates│              pressures │  reduces
                                    action               (state)──◀── leave/wanted_off
"""

from __future__ import annotations

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import networkx as nx  # noqa: E402

_KIND_COLOR = {
    "constraint_hard": "#e74c3c",
    "constraint_soft": "#f39c12",
    "domain_object": "#3498db",
    "state": "#e67e22",
    "action": "#2ecc71",
}
_REL_COLOR = {
    "pressures": "#c0392b", "mitigates": "#27ae60", "requires": "#7f8c8d",
    "constrains": "#2980b9", "reduces": "#8e44ad", "belongs_to": "#95a5a6",
}


def _node_color(node) -> str:
    if node.kind == "constraint":
        return _KIND_COLOR["constraint_soft" if node.severity == "soft" else "constraint_hard"]
    return _KIND_COLOR.get(node.kind, "#bdc3c7")


def _short_label(node) -> str:
    if node.kind == "state":
        sh = node.evidence.get("shortage")
        pen = node.evidence.get("penalty")
        v = f"부족 {sh}" if sh else (f"penalty {pen}" if pen is not None else "")
        return f"[상태]\n{node.label}\n{v}".strip()
    if node.kind == "action":
        return f"[액션]\n{node.label}"
    if node.kind == "constraint":
        tag = "소프트" if node.severity == "soft" else "하드"
        return f"[{tag}제약]\n{node.label}"
    return f"[{node.object_type}]\n{node.label}"


def focused_subgraph(graph, target_family: str, top_action_id: str | None,
                     *, max_constraints=2, max_objs=3, max_leaves=2) -> set[str]:
    """주입 family 의 인과경로 노드 id 집합."""
    keep: set[str] = set()
    # 1) 해당 family 의 압력받는 제약
    fam_constraints = [n.node_id for n in graph.nodes_of_kind("constraint")
                       if n.family == target_family and graph.in_edges(n.node_id, "pressures")]
    fam_constraints = fam_constraints[:max_constraints]
    keep.update(fam_constraints)
    # 2) 그 제약을 압박하는 shortage state + reduce 한 leave
    for cid in fam_constraints:
        for e in graph.in_edges(cid, "pressures"):
            keep.add(e.source_id)
            for re_ in graph.in_edges(e.source_id, "reduces")[:max_leaves]:
                keep.add(re_.source_id)
        for e in graph.out_edges(cid, "constrains"):
            keep.add(e.target_id)
        for e in graph.out_edges(cid, "requires"):
            keep.add(e.source_id)  # constraint self already in keep; add state
            keep.add(e.target_id)
    # 3) 추천 액션
    if top_action_id and top_action_id in graph.nodes:
        keep.add(top_action_id)
    # objects 수 제한
    objs = [n for n in keep if graph.nodes[n].kind == "domain_object"]
    if len(objs) > max_objs:
        for extra in objs[max_objs:]:
            keep.discard(extra)
    return keep


def render(graph, keep: set[str], path: str, *, title: str) -> None:
    G = nx.DiGraph()
    for nid in keep:
        G.add_node(nid)
    edge_labels = {}
    for e in graph.edges:
        if e.source_id in keep and e.target_id in keep:
            G.add_edge(e.source_id, e.target_id, relation=e.relation)
            edge_labels[(e.source_id, e.target_id)] = e.relation

    if G.number_of_nodes() == 0:
        return
    pos = nx.spring_layout(G, k=2.2, iterations=80, seed=7)
    fig, ax = plt.subplots(figsize=(11, 7.5))
    colors = [_node_color(graph.nodes[n]) for n in G.nodes]
    labels = {n: _short_label(graph.nodes[n]) for n in G.nodes}
    nx.draw_networkx_nodes(G, pos, node_color=colors, node_size=2600, alpha=0.92, ax=ax)
    for rel in set(nx.get_edge_attributes(G, "relation").values()):
        edl = [(u, v) for u, v, d in G.edges(data=True) if d["relation"] == rel]
        nx.draw_networkx_edges(G, pos, edgelist=edl, edge_color=_REL_COLOR.get(rel, "#555"),
                               width=2.0, arrowsize=22, ax=ax,
                               connectionstyle="arc3,rad=0.06")
    nx.draw_networkx_labels(G, pos, labels, font_size=8,
                            font_family="AppleGothic", ax=ax)
    nx.draw_networkx_edge_labels(G, pos, edge_labels, font_size=7.5,
                                 font_family="AppleGothic", ax=ax,
                                 bbox=dict(boxstyle="round,pad=0.15", fc="white", ec="none", alpha=0.7))
    ax.set_title(title, fontsize=12, fontfamily="AppleGothic")
    ax.axis("off")
    # 범례
    import matplotlib.patches as mp
    leg = [mp.Patch(color=_KIND_COLOR["constraint_hard"], label="하드제약"),
           mp.Patch(color=_KIND_COLOR["constraint_soft"], label="소프트제약"),
           mp.Patch(color=_KIND_COLOR["state"], label="상태(부족/penalty)"),
           mp.Patch(color=_KIND_COLOR["domain_object"], label="도메인객체"),
           mp.Patch(color=_KIND_COLOR["action"], label="완화액션")]
    ax.legend(handles=leg, loc="lower left", fontsize=8, prop={"family": "AppleGothic"})
    fig.tight_layout()
    fig.savefig(path, dpi=130, bbox_inches="tight")
    plt.close(fig)
