"""제약 충돌(conflict) — generic 진단 + 수선. 스키마 불변.

핵심: 충돌은 새 노드 종류가 아니다. "하한(min) 제약과 상한(max) 제약이 같은 자원
state 를 공유하는데 floor > ceiling" 이면, 두 제약은 단독으론 멀쩡해도 함께는
만족 불가다. 이 구조는 기존 4종 노드 + 8종 엣지로 표현된다:

    min_constraint ──requires──▶ capacity_state ◀──requires── max_constraint
                                      │ pressures(둘 다)
                                      ▼
                            (floor > ceiling 이면 충돌)
    action ──mitigates──▶ (min 또는 max) constraint   ← 어느 쪽이든 풀면 해소

detect_conflicts 는 **family 무관**하다. TeamMin×GradeMax, GradeMin×GradeMax,
CoverageMin×GradeMax 가 전부 같은 검출기로 잡힌다 — 케이스별 코드 없음.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from services.ontology_graph.schema import ConstraintNode, OntologyGraph, StateNode


@dataclass
class Conflict:
    state_id: str
    resource: str
    floor: int                 # 하한(min 제약 요구)
    ceiling: int               # 상한(max 제약 허용)
    min_constraint: str
    max_constraint: str
    families: tuple[str, str]
    gap: int = field(init=False)

    def __post_init__(self):
        self.gap = self.floor - self.ceiling


def register_capacity_conflict(
    graph: OntologyGraph,
    *,
    resource: str,
    floor: int,
    ceiling: int,
    min_constraint_id: str,
    max_constraint_id: str,
) -> str:
    """두 제약이 공유하는 자원 capacity state 를 만들고 requires/pressures 로 잇는다.

    스키마 불변: state(기존 kind), requires/pressures(기존 엣지)만 사용.
    """
    sid = f"state:capacity:{resource}"
    if sid not in graph.nodes:
        graph.add_node(StateNode(
            node_id=sid, label=f"{resource} 자원(하한{floor}/상한{ceiling})",
            state_type="skill_supply",
            severity="hard" if floor > ceiling else "none",
            attrs={"resource": resource},
            evidence={"floor": floor, "ceiling": ceiling, "shortage": max(0, floor - ceiling)}))
    # 두 제약 모두 이 자원을 requires (하한·상한). 충돌이면 둘 다 pressures.
    graph.add_edge("requires", min_constraint_id, sid)
    graph.add_edge("requires", max_constraint_id, sid)
    if floor > ceiling:
        graph.add_edge("pressures", sid, min_constraint_id)
        graph.add_edge("pressures", sid, max_constraint_id)
    return sid


def detect_conflicts(graph: OntologyGraph) -> list[Conflict]:
    """floor > ceiling 인 capacity state 를 충돌로 보고한다 (family 무관 generic)."""
    out: list[Conflict] = []
    for st in graph.nodes_of_kind("state"):
        floor = st.evidence.get("floor")
        ceiling = st.evidence.get("ceiling")
        if floor is None or ceiling is None or floor <= ceiling:
            continue
        # 이 state 를 requires 하는 제약들 → min 쪽(>=)과 max 쪽(<=) 식별
        reqs = [graph.nodes[e.source_id] for e in graph.in_edges(st.node_id, "requires")
                if graph.nodes[e.source_id].kind == "constraint"]
        min_c = next((c for c in reqs if c.operator == ">="), None)
        max_c = next((c for c in reqs if c.operator == "<="), None)
        if not (min_c and max_c):
            continue
        out.append(Conflict(
            state_id=st.node_id, resource=st.attrs.get("resource", st.node_id),
            floor=int(floor), ceiling=int(ceiling),
            min_constraint=min_c.node_id, max_constraint=max_c.node_id,
            families=(min_c.family, max_c.family)))
    return out


def detect_convergent_conflicts(graph: OntologyGraph) -> list[str]:
    """서로 다른 family 출처의 pressures 가 2개 이상 들어오는 제약 노드 = 수렴 충돌.
    (generic graph query — 케이스별 코드 없음)"""
    out = []
    for c in graph.nodes_of_kind("constraint"):
        src_families = set()
        for e in graph.in_edges(c.node_id, "pressures"):
            s = graph.nodes[e.source_id]
            src_families.add(s.attrs.get("resource") or s.node_id)
        if len(src_families) >= 2:
            out.append(c.node_id)
    return out


@dataclass
class ConflictRepair:
    conflict: Conflict
    action_id: str
    action_label: str
    relaxed_constraint: str
    relaxed_family: str
    note: str


def recommend_conflict_repair(graph: OntologyGraph, conflict: Conflict) -> ConflictRepair | None:
    """충돌의 두 제약 중 어느 쪽이든 푸는 action 을 generic 하게 고른다(최소변경 우선).
    동일 recommend traversal 재사용: 제약을 mitigates 하는 action 들 중 최저 cost."""
    candidates = []
    for cid, fam in ((conflict.min_constraint, conflict.families[0]),
                     (conflict.max_constraint, conflict.families[1])):
        for e in graph.in_edges(cid, "mitigates"):
            act = graph.nodes[e.source_id]
            if act.kind == "action":
                candidates.append((float(act.cost), act, cid, fam))
    if not candidates:
        return None
    candidates.sort(key=lambda x: x[0])
    _, act, cid, fam = candidates[0]
    return ConflictRepair(conflict=conflict, action_id=act.node_id, action_label=act.label,
                          relaxed_constraint=cid, relaxed_family=fam,
                          note=f"{fam} 완화로 floor>ceiling 해소")


def actions_resolving_multiple(graph: OntologyGraph, conflicts: list[Conflict]) -> dict[str, list[str]]:
    """한 action 이 여러 충돌을 동시에 푸는지 — action_id → 풀리는 conflict resource 목록."""
    cover: dict[str, set[str]] = {}
    for cf in conflicts:
        for cid in (cf.min_constraint, cf.max_constraint):
            for e in graph.in_edges(cid, "mitigates"):
                if graph.nodes[e.source_id].kind == "action":
                    cover.setdefault(e.source_id, set()).add(cf.resource)
    return {a: sorted(rs) for a, rs in cover.items() if len(rs) >= 2}
