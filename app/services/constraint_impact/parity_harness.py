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
