"""Unified ontology graph schema.

The research target (docs/UNIFIED_ONTOLOGY_GRAPH_SCHEMA.md) models a constraint
optimisation problem with exactly four first-class node kinds and a fixed set of
typed, directed edges. This module is the single canonical schema — it supersedes
``constraint_impact/graph_nodes.py`` and the node concepts scattered across
``semantics/ontology.yaml``.

Node kinds (연구계획 1:1):
  - constraint     : 제약 함수 (coverage / team_min / grade_* / transition_ban / ...)
  - domain_object  : 도메인 객체 (nurse / day / shift / ward / team / grade / leave / wanted_off)
  - state          : 수요·공급·부하 상태 (supply_demand / skill_supply / nurse_load)
  - action         : 완화 action (force_soft_mode / disable_module / set_threshold / ...)

Edges are directed and their (tail kind -> head kind) pairing is enforced by the
schema so the graph cannot drift back into an untyped blob.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

# Reuse the existing vocabularies so the new graph stays meaning-compatible with
# the solver-side constraint families and modes already in the codebase.
from services.constraint_impact.types import ConstraintFamily, ConstraintMode

# ── Node taxonomy ──────────────────────────────────────────────────────────
NodeKind = Literal["constraint", "domain_object", "state", "action"]

DomainObjectType = Literal[
    "nurse",
    "day",
    "shift",
    "ward",
    "team",
    "grade",
    "leave",       # 연차/휴가
    "wanted_off",  # 희망 OFF
]

StateType = Literal[
    "supply_demand",  # day×shift: required vs available vs shortage
    "skill_supply",   # skill/grade eligible supply on a day×shift
    "nurse_load",     # per-nurse cumulative load (consec work/night, fatigue, recovery debt)
    "soft_penalty",   # 소프트 제약 위반량 (편차/고립근무/패턴 등) — 체크용 상태
]

# Aligned with constraint_impact/control.py actions + ontology.yaml treatments.
ActionType = Literal[
    "force_soft_mode",
    "disable_module",
    "set_threshold",
    "narrow_scope",
    "data_correction_required",
]

Severity = Literal["hard", "soft", "risk", "none"]


# ── Nodes ──────────────────────────────────────────────────────────────────
@dataclass(slots=True)
class Node:
    """Common base. Every node carries a stable id, a human label, free-form
    ``attrs`` (typed per kind by convention) and ``evidence`` (runtime numbers).

    ``kind`` is set authoritatively by each subclass's ``__post_init__``; the
    default here only exists so subclasses can add their own defaulted fields.
    ``Node`` itself is a base and is not meant to be instantiated directly."""

    node_id: str
    label: str
    kind: NodeKind = "constraint"
    attrs: dict[str, Any] = field(default_factory=dict)
    evidence: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ConstraintNode(Node):
    family: ConstraintFamily | str = "coverage"
    mode: ConstraintMode = "enforced"
    severity: Severity = "hard"
    operator: str = ">="          # >= | <= | == | forbid | require
    target: Any = None

    def __post_init__(self) -> None:
        self.kind = "constraint"


@dataclass(slots=True)
class DomainObjectNode(Node):
    object_type: DomainObjectType = "nurse"

    def __post_init__(self) -> None:
        self.kind = "domain_object"


@dataclass(slots=True)
class StateNode(Node):
    state_type: StateType = "supply_demand"
    severity: Severity = "none"

    def __post_init__(self) -> None:
        self.kind = "state"


@dataclass(slots=True)
class ActionNode(Node):
    action_type: ActionType = "set_threshold"
    target_family: ConstraintFamily | str | None = None
    config_key: str | None = None
    direction: str | None = None       # enable | disable | increase | decrease | clear | remove_key | manual
    cost: float = 1.0
    reversible: bool = True
    auto_applicable: bool = False

    def __post_init__(self) -> None:
        self.kind = "action"


# ── Edges ──────────────────────────────────────────────────────────────────
EdgeRelation = Literal[
    "constrains",    # constraint     -> domain_object   (제약이 객체에 적용)
    "requires",      # constraint     -> state            (제약이 수요를 만든다)
    "supplied_by",   # state          -> domain_object    (가용 공급원: 간호사)
    "reduces",       # domain_object  -> state            (연차·희망OFF가 가용 감소)
    "belongs_to",    # domain_object  -> domain_object    (간호사→팀/등급)
    "pressures",     # state          -> constraint       (부족이 제약 위반 압력)
    "mitigates",     # action         -> constraint/state (완화가 충돌 해소)
    "derived_from",  # state          -> state            (상태 파생)
]

# (tail kind, head kind) legality per relation. This is the schema governance:
# an edge whose endpoints do not match is rejected at add time.
_EDGE_ENDPOINT_RULES: dict[str, set[tuple[NodeKind, NodeKind]]] = {
    "constrains": {("constraint", "domain_object")},
    "requires": {("constraint", "state")},
    "supplied_by": {("state", "domain_object")},
    "reduces": {("domain_object", "state")},
    "belongs_to": {("domain_object", "domain_object")},
    "pressures": {("state", "constraint")},
    "mitigates": {("action", "constraint"), ("action", "state")},
    "derived_from": {("state", "state")},
}


@dataclass(slots=True)
class Edge:
    edge_id: str
    relation: EdgeRelation
    source_id: str
    target_id: str
    evidence: dict[str, Any] = field(default_factory=dict)


class GraphValidationError(ValueError):
    """Raised when an edge violates the schema (unknown node or illegal endpoints)."""


# ── Container ───────────────────────────────────────────────────────────────
@dataclass(slots=True)
class OntologyGraph:
    nodes: dict[str, Node] = field(default_factory=dict)
    edges: list[Edge] = field(default_factory=list)

    # -- node ops --
    def add_node(self, node: Node) -> Node:
        if node.node_id in self.nodes:
            existing = self.nodes[node.node_id]
            if existing.kind != node.kind:
                raise GraphValidationError(
                    f"node id {node.node_id!r} re-added with kind {node.kind!r} "
                    f"(was {existing.kind!r})"
                )
            return existing
        self.nodes[node.node_id] = node
        return node

    def get(self, node_id: str) -> Node | None:
        return self.nodes.get(node_id)

    def nodes_of_kind(self, kind: NodeKind) -> list[Node]:
        return [n for n in self.nodes.values() if n.kind == kind]

    # -- edge ops --
    def add_edge(
        self,
        relation: EdgeRelation,
        source_id: str,
        target_id: str,
        *,
        evidence: dict[str, Any] | None = None,
        edge_id: str | None = None,
    ) -> Edge:
        src = self.nodes.get(source_id)
        dst = self.nodes.get(target_id)
        if src is None:
            raise GraphValidationError(f"edge {relation}: unknown source {source_id!r}")
        if dst is None:
            raise GraphValidationError(f"edge {relation}: unknown target {target_id!r}")
        allowed = _EDGE_ENDPOINT_RULES.get(relation)
        if allowed is None:
            raise GraphValidationError(f"unknown relation {relation!r}")
        if (src.kind, dst.kind) not in allowed:
            raise GraphValidationError(
                f"relation {relation!r} illegal for {src.kind!r} -> {dst.kind!r}; "
                f"allowed: {sorted(allowed)}"
            )
        edge = Edge(
            edge_id=edge_id or f"{relation}:{source_id}->{target_id}",
            relation=relation,
            source_id=source_id,
            target_id=target_id,
            evidence=evidence or {},
        )
        self.edges.append(edge)
        return edge

    def out_edges(self, node_id: str, relation: EdgeRelation | None = None) -> list[Edge]:
        return [
            e for e in self.edges
            if e.source_id == node_id and (relation is None or e.relation == relation)
        ]

    def in_edges(self, node_id: str, relation: EdgeRelation | None = None) -> list[Edge]:
        return [
            e for e in self.edges
            if e.target_id == node_id and (relation is None or e.relation == relation)
        ]

    # -- summary --
    def stats(self) -> dict[str, Any]:
        kind_counts: dict[str, int] = {}
        for n in self.nodes.values():
            kind_counts[n.kind] = kind_counts.get(n.kind, 0) + 1
        rel_counts: dict[str, int] = {}
        for e in self.edges:
            rel_counts[e.relation] = rel_counts.get(e.relation, 0) + 1
        return {
            "nodes": len(self.nodes),
            "edges": len(self.edges),
            "by_kind": kind_counts,
            "by_relation": rel_counts,
        }
