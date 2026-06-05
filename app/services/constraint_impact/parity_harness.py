from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from services.constraint_impact.atoms import AssignmentAtom
from services.constraint_impact.graph_builder import build_primitive_rule_graph
from services.constraint_impact.simulation import analyze_current_roster
from services.constraint_impact.snapshot import SemanticsSnapshot


@dataclass(slots=True)
class ParityMismatch:
    key: str
    category: str  # hard | soft | risk
    evaluator_values: list[str] = field(default_factory=list)
    graph_values: list[str] = field(default_factory=list)
    details: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ParityReport:
    comparable_keys: list[str]
    matched_keys: list[str]
    mismatches: list[ParityMismatch]
    unsupported_evaluator_nodes: list[str]
    unsupported_graph_nodes: list[str]
    evaluator_summary: dict[str, int]
    graph_summary: dict[str, int]


def _state_key(nurse_id: str, day_index: int) -> str:
    return f"{nurse_id}:{day_index}"


def _normalize_evaluator_node(snapshot: SemanticsSnapshot, node_id: str, details: dict[str, Any], *, valid: bool) -> tuple[str | None, str | None]:
    parts = node_id.split(":")
    if node_id.startswith("transition_ban:") and len(parts) >= 4:
        nurse_idx = int(parts[2])
        day = int(parts[3])
        nurse_id = snapshot.nurses[nurse_idx].nurse_id
        trans = parts[1].replace("n_to_d", "N->D").replace("e_to_d", "E->D").replace("n_to_e", "N->E")
        return _state_key(nurse_id, day), f"transition_ban:{trans}"
    if node_id.startswith("consecutive_work:") and len(parts) >= 3:
        nurse_idx = int(parts[1]); day = int(parts[2])
        return _state_key(snapshot.nurses[nurse_idx].nurse_id, day), "consecutive_work_limit"
    if node_id.startswith("consecutive_night:") and len(parts) >= 3:
        nurse_idx = int(parts[1]); day = int(parts[2])
        return _state_key(snapshot.nurses[nurse_idx].nurse_id, day), "consecutive_night_limit"
    if node_id.startswith("recovery_debt:first_day:") and len(parts) >= 4:
        nurse_idx = int(parts[2]); day = int(parts[3])
        return _state_key(snapshot.nurses[nurse_idx].nurse_id, day), "recovery_debt:first_visible_day"
    if node_id.startswith("fatigue_risk:") and len(parts) >= 3 and valid:
        nurse_idx = int(parts[1]); day = int(parts[2])
        return _state_key(snapshot.nurses[nurse_idx].nurse_id, day), "fatigue_risk"
    return None, None


def _collect_evaluator_surface(snapshot: SemanticsSnapshot, current_atoms: list[AssignmentAtom]) -> tuple[dict[str, set[str]], dict[str, set[str]], list[str]]:
    analysis = analyze_current_roster(snapshot=snapshot, current_atoms=current_atoms)
    hard: dict[str, set[str]] = {}
    risk: dict[str, set[str]] = {}
    unsupported: list[str] = []
    for ev in analysis.violated_constraints:
        key, value = _normalize_evaluator_node(snapshot, ev.node_id, ev.details, valid=False)
        if key is None:
            unsupported.append(ev.node_id)
            continue
        hard.setdefault(key, set()).add(value)
    for ev in analysis.risky_constraints:
        key, value = _normalize_evaluator_node(snapshot, ev.node_id, ev.details, valid=True)
        if key is None:
            unsupported.append(ev.node_id)
            continue
        risk.setdefault(key, set()).add(value)
    return hard, risk, unsupported


def _collect_graph_surface(snapshot: SemanticsSnapshot, current_atoms: list[AssignmentAtom]) -> tuple[dict[str, set[str]], dict[str, set[str]], list[str]]:
    graph = build_primitive_rule_graph(snapshot=snapshot, atoms=current_atoms)
    hard: dict[str, set[str]] = {}
    risk: dict[str, set[str]] = {}
    unsupported: list[str] = []
    for key, prop in graph["propagation"].items():
        for h in prop.hard_violations:
            hard.setdefault(key, set()).add(h)
        for r in prop.risk_flags:
            kind = str(r.get("kind") or "risk")
            risk.setdefault(key, set()).add(kind)
        for s in prop.soft_penalties:
            # soft parity는 아직 evaluator overlap surface에 포함하지 않음
            kind = str(s.get("kind") or "soft")
            unsupported.append(f"{key}:{kind}")
    return hard, risk, unsupported


def compare_graph_and_evaluator(*, snapshot: SemanticsSnapshot, current_atoms: list[AssignmentAtom]) -> ParityReport:
    evaluator_hard, evaluator_risk, evaluator_unsupported = _collect_evaluator_surface(snapshot, current_atoms)
    graph_hard, graph_risk, graph_unsupported = _collect_graph_surface(snapshot, current_atoms)

    comparable_keys = sorted(set(evaluator_hard) | set(evaluator_risk) | set(graph_hard) | set(graph_risk))
    matched: list[str] = []
    mismatches: list[ParityMismatch] = []
    for key in comparable_keys:
        eh = sorted(evaluator_hard.get(key, set()))
        gh = sorted(graph_hard.get(key, set()))
        if eh != gh:
            mismatches.append(ParityMismatch(key=key, category="hard", evaluator_values=eh, graph_values=gh))
            continue
        er = sorted(evaluator_risk.get(key, set()))
        gr = sorted(graph_risk.get(key, set()))
        if er != gr:
            mismatches.append(ParityMismatch(key=key, category="risk", evaluator_values=er, graph_values=gr))
            continue
        matched.append(key)

    return ParityReport(
        comparable_keys=comparable_keys,
        matched_keys=matched,
        mismatches=mismatches,
        unsupported_evaluator_nodes=sorted(set(evaluator_unsupported)),
        unsupported_graph_nodes=sorted(set(graph_unsupported)),
        evaluator_summary={"hard_keys": len(evaluator_hard), "risk_keys": len(evaluator_risk)},
        graph_summary={"hard_keys": len(graph_hard), "risk_keys": len(graph_risk)},
    )


# ---- Phase B parity: solver-emitted vs snapshot-derived (graph) -------------------


@dataclass(slots=True)
class SolverGraphParityReport:
    family: str
    matched: list[str] = field(default_factory=list)
    solver_only: list[str] = field(default_factory=list)
    graph_only: list[str] = field(default_factory=list)
    mode_mismatches: list[dict[str, Any]] = field(default_factory=list)
    solver_count: int = 0
    graph_count: int = 0


def _solver_emitted_key(rec) -> str:
    """Stable key for an EmittedConstraint of the BoundaryTransitionBan family."""
    sc = rec.scope or {}
    return f"transition:{sc.get('nurse_index')}:{sc.get('day')}:{sc.get('transition')}"


def _graph_node_key_for_transition_ban(node) -> str | None:
    """Stable key for a graph ConstraintNode of family transition_ban."""
    if str(node.family) != "transition_ban":
        return None
    sc = node.scope or {}
    return f"transition:{sc.get('nurse_index')}:{sc.get('day')}:{node.target}"


def compare_solver_emitted_vs_graph_derived(
    *,
    snapshot,
    solver_emitted: list,
    family: str = "BoundaryTransitionBan",
    graph_family_alias: str = "transition_ban",
) -> SolverGraphParityReport:
    """For one family, compare what the solver actually emitted vs what the
    snapshot-derived graph predicts. v1 supports BoundaryTransitionBan only —
    extend the per-family key extractors as more families get the emit treatment."""
    from services.constraint_impact.atoms import build_assignment_atoms
    from services.constraint_impact.graph_builder import build_constraint_nodes

    if family != "BoundaryTransitionBan":
        return SolverGraphParityReport(family=family)

    solver_records = [r for r in (solver_emitted or []) if getattr(r, "family", None) == family]
    solver_keys: dict[str, Any] = {_solver_emitted_key(r): r for r in solver_records}

    atoms = []
    rs = getattr(snapshot, "_rs_handle", None)
    if rs is not None:
        try:
            atoms = build_assignment_atoms(snapshot, rs.roster)
        except Exception:
            atoms = []
    nodes = build_constraint_nodes(snapshot=snapshot, atoms=atoms)
    graph_keys: dict[str, Any] = {}
    for n in nodes:
        k = _graph_node_key_for_transition_ban(n)
        if k:
            graph_keys[k] = n

    matched: list[str] = []
    mode_mismatches: list[dict[str, Any]] = []
    for k, srec in solver_keys.items():
        if k in graph_keys:
            matched.append(k)
            gnode_mode = str(graph_keys[k].mode)
            if gnode_mode != srec.mode:
                mode_mismatches.append(
                    {"key": k, "solver_mode": srec.mode, "graph_mode": gnode_mode}
                )
    solver_only = sorted(set(solver_keys) - set(graph_keys))
    graph_only = sorted(set(graph_keys) - set(solver_keys))

    return SolverGraphParityReport(
        family=family,
        matched=sorted(matched),
        solver_only=solver_only,
        graph_only=graph_only,
        mode_mismatches=mode_mismatches,
        solver_count=len(solver_records),
        graph_count=len(graph_keys),
    )
