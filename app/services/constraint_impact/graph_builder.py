from __future__ import annotations

from collections import defaultdict
from typing import Any

from services.constraint_impact.atoms import AssignmentAtom
from services.constraint_impact.graph_nodes import (
    ConstraintEdge,
    ConstraintNode,
    GraphExpansionState,
    PropagationResult,
    make_expansion_state,
    propagate_layered_constraints,
)
from services.constraint_impact.nurse_state_machine import build_nurse_day_states
from services.constraint_impact.rule_compiler import compile_builtin_rules, compile_personal_rules, merge_rule_bundles
from services.constraint_impact.rule_masks import build_rule_masks
from services.constraint_impact.snapshot import SemanticsSnapshot


def build_constraint_nodes(*, snapshot: SemanticsSnapshot, atoms: list[AssignmentAtom]) -> list[ConstraintNode]:
    nodes: list[ConstraintNode] = []
    atoms_by_key = {(a.nurse_index, a.day_index): a for a in atoms}
    states_by_nurse = build_nurse_day_states(snapshot=snapshot, atoms_by_nurse_day=atoms_by_key)

    for day in sorted({a.day_index for a in atoms}):
        req = snapshot.config_payload.get("daily_shift_requirements_by_day", [])
        day_req = req[day] if isinstance(req, list) and day < len(req) and isinstance(req[day], dict) else snapshot.config_payload.get("daily_shift_requirements") or {}
        max_req_all = snapshot.config_payload.get("daily_shift_requirements_max_by_day", [])
        day_max = max_req_all[day] if isinstance(max_req_all, list) and day < len(max_req_all) and isinstance(max_req_all[day], dict) else {}
        for shift_code, need in (day_req or {}).items():
            if shift_code == "O":
                continue
            nodes.append(
                ConstraintNode(
                    node_id=f"coverage:min:{day}:{shift_code}",
                    family="coverage",
                    mode="enforced",
                    tier="hard",
                    scope={"day": day + 1, "shift": shift_code},
                    operator=">=",
                    target=int(need or 0),
                    related_atom_ids=[a.atom_id for a in atoms if a.day_index == day and a.shift_code == shift_code],
                    explanation_template="day {day} shift {shift} requires at least {target}",
                )
            )
            if shift_code in day_max:
                nodes.append(
                    ConstraintNode(
                        node_id=f"coverage:max:{day}:{shift_code}",
                        family="coverage",
                        mode="enforced",
                        tier="hard",
                        scope={"day": day + 1, "shift": shift_code},
                        operator="<=",
                        target=int(day_max.get(shift_code) or 0),
                        related_atom_ids=[a.atom_id for a in atoms if a.day_index == day and a.shift_code == shift_code],
                        explanation_template="day {day} shift {shift} allows at most {target}",
                    )
                )

    team_cfg = snapshot.config_payload.get("team_min_by_team") or {}
    for team_id, mins in team_cfg.items():
        for day in sorted({a.day_index for a in atoms}):
            for shift_code, need in (mins or {}).items():
                if int(need or 0) <= 0:
                    continue
                related = [a.atom_id for a in atoms if a.day_index == day and a.shift_code == shift_code and any(n.nurse_index == a.nurse_index and str(n.team_id) == str(team_id) for n in snapshot.nurses)]
                nodes.append(ConstraintNode(node_id=f"team_min:{team_id}:{day}:{shift_code}", family="team_min", mode="enforced", tier="hard", scope={"team": team_id, "day": day + 1, "shift": shift_code}, operator=">=", target=int(need), related_atom_ids=related, explanation_template="team {team} shift {shift} minimum {target}"))

    grade_cfg = snapshot.config_payload.get("grade_config") or {}
    constraints_map = (grade_cfg or {}).get("constraints") or (grade_cfg or {}).get("constraints_json") or {}
    for day in sorted({a.day_index for a in atoms}):
        for shift_code, by_grade in (constraints_map or {}).items():
            for grade, target in (by_grade or {}).items():
                nodes.append(ConstraintNode(node_id=f"grade_min:{grade}:{day}:{shift_code}", family="grade_min", mode="soft_fallback" if bool((grade_cfg or {}).get("allow_soft_fallback", False)) else "enforced", tier="soft" if bool((grade_cfg or {}).get("allow_soft_fallback", False)) else "hard", scope={"grade": grade, "day": day + 1, "shift": shift_code}, operator=">=", target=int(target or 0), related_atom_ids=[a.atom_id for a in atoms if a.day_index == day and a.shift_code == shift_code], explanation_template="grade {grade} shift {shift} target {target}"))

    for pf in snapshot.preceptee_facts:
        for day in sorted(pf.follow_days if not pf.full_month_default_follow else snapshot.active_days_by_nurse.get(pf.nurse_index, set())):
            related = [a.atom_id for a in atoms if a.day_index == day and a.nurse_index in {pf.nurse_index, pf.preceptor_index}]
            nodes.append(ConstraintNode(node_id=f"preceptee_sync:{pf.nurse_index}:{day}", family="preceptee_sync", mode="enforced", tier="hard", scope={"nurse": pf.nurse_id, "day": day + 1}, operator="==", target="same_shift", related_atom_ids=related, explanation_template="preceptee sync on day {day}"))

    for nurse_index, nurse_states in states_by_nurse.items():
        for st in nurse_states:
            if st.transition_code:
                nodes.append(ConstraintNode(node_id=f"transition:{nurse_index}:{st.day_index}", family="transition_ban", mode="enforced", tier="hard", scope={"nurse_index": nurse_index, "day": st.day_index + 1}, operator="forbid", target=st.transition_code, related_atom_ids=[a.atom_id for a in atoms if a.nurse_index == nurse_index and a.day_index == st.day_index], explanation_template="forbidden transition {target}", evidence={"notes": st.notes}))
            if st.recovery_debt > 0:
                nodes.append(ConstraintNode(node_id=f"recovery_debt:{nurse_index}:{st.day_index}", family="recovery_2n2o", mode="enforced", tier="hard", scope={"nurse_index": nurse_index, "day": st.day_index + 1}, operator="==", target="O", related_atom_ids=[a.atom_id for a in atoms if a.nurse_index == nurse_index and a.day_index == st.day_index], explanation_template="recovery debt requires O", evidence={"recovery_debt": st.recovery_debt}))
            if st.fatigue_score >= 8.0:
                nodes.append(ConstraintNode(node_id=f"fatigue:{nurse_index}:{st.day_index}", family="consecutive_work", mode="inactive", tier="risk", scope={"nurse_index": nurse_index, "day": st.day_index + 1}, operator=">=", target=8.0, related_atom_ids=[a.atom_id for a in atoms if a.nurse_index == nurse_index and a.day_index == st.day_index], explanation_template="fatigue risk threshold", evidence={"fatigue_score": st.fatigue_score}))

    return nodes


def build_constraint_edges(*, snapshot: SemanticsSnapshot, atoms: list[AssignmentAtom], nodes: list[ConstraintNode]) -> list[ConstraintEdge]:
    node_ids_by_atom: dict[str, list[str]] = defaultdict(list)
    for node in nodes:
        for atom_id in node.related_atom_ids:
            node_ids_by_atom[atom_id].append(node.node_id)
    edges: list[ConstraintEdge] = []
    seen = set()
    for atom_id, node_ids in node_ids_by_atom.items():
        for src in node_ids:
            for dst in node_ids:
                if src == dst:
                    continue
                key = (src, dst, atom_id)
                if key in seen:
                    continue
                seen.add(key)
                edges.append(ConstraintEdge(edge_id=f"edge:{src}->{dst}:{atom_id}", source_node_id=src, target_node_id=dst, relation="depends_on", evidence={"atom_id": atom_id}))
    return edges


def index_nodes_by_atom(nodes: list[ConstraintNode]) -> dict[str, list[str]]:
    out: dict[str, list[str]] = defaultdict(list)
    for node in nodes:
        for atom_id in node.related_atom_ids:
            out[atom_id].append(node.node_id)
    return dict(out)


def build_graph_expansion_states(*, snapshot: SemanticsSnapshot, atoms: list[AssignmentAtom]) -> list[GraphExpansionState]:
    atoms_by_key = {(a.nurse_index, a.day_index): a for a in atoms}
    states_by_nurse = build_nurse_day_states(snapshot=snapshot, atoms_by_nurse_day=atoms_by_key)
    out: list[GraphExpansionState] = []
    for nurse_index, states in states_by_nurse.items():
        for st in states:
            out.append(make_expansion_state(snapshot, nurse_index, st.day_index, st))
    return out


def propagate_graph_states(*, snapshot: SemanticsSnapshot, states: list[GraphExpansionState]) -> dict[str, PropagationResult]:
    return {f"{s.nurse_id}:{s.day_index}": propagate_layered_constraints(snapshot=snapshot, state=s) for s in states}


def build_primitive_rule_graph(*, snapshot: SemanticsSnapshot, atoms: list[AssignmentAtom], personal_rules: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    builtin = compile_builtin_rules(snapshot=snapshot)
    personal = compile_personal_rules(snapshot=snapshot, user_rules=personal_rules or [])
    bundle = merge_rule_bundles(bundles=[builtin, personal])
    masks = build_rule_masks(snapshot=snapshot, bundle=bundle)
    nodes = build_constraint_nodes(snapshot=snapshot, atoms=atoms)
    edges = build_constraint_edges(snapshot=snapshot, atoms=atoms, nodes=nodes)
    states = build_graph_expansion_states(snapshot=snapshot, atoms=atoms)
    propagation = propagate_graph_states(snapshot=snapshot, states=states)
    return {
        "rule_bundle": bundle,
        "rule_masks": masks,
        "nodes": nodes,
        "edges": edges,
        "states": states,
        "propagation": propagation,
    }
