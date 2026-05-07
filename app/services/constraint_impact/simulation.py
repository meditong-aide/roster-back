from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from services.constraint_impact.atoms import AssignmentAtom, DerivedAtom, build_assignment_atoms, make_atom_id
from services.constraint_impact.nurse_state_machine import build_nurse_day_states, simulate_nurse_local_delta
from services.constraint_impact.snapshot import SemanticsSnapshot
from services.constraint_impact.types import SimulationSeverity


@dataclass(slots=True)
class ConstraintEvaluation:
    node_id: str
    valid: bool
    slack: int | float | None
    pressure: int | float | None
    details: dict[str, Any]


@dataclass(slots=True)
class SimulationAction:
    action_id: str
    type: str
    assignments: list[dict[str, Any]]
    source: str
    reason: str | None = None


@dataclass(slots=True)
class SimulationResult:
    action_id: str
    valid_under_current_semantics: bool
    severity: SimulationSeverity
    changed_atom_ids: list[str]
    triggered_forced_atoms: list[DerivedAtom]
    violated_constraints: list[ConstraintEvaluation]
    risky_constraints: list[ConstraintEvaluation]
    causal_chain: list[str]
    notes: list[str]


@dataclass(slots=True)
class CurrentRosterAnalysis:
    valid_under_current_semantics: bool
    atom_count: int
    fixed_atom_count: int
    preceptee_atom_count: int
    coverage_excluded_atom_count: int
    hard_violation_count: int
    risky_constraint_count: int
    violated_constraints: list[ConstraintEvaluation]
    risky_constraints: list[ConstraintEvaluation]
    constraint_mode_summary: dict[str, dict[str, int]]
    preflight_alerts: list[dict[str, Any]]
    notes: list[str]


def _requirements_for_day(snapshot: SemanticsSnapshot, d: int) -> tuple[dict[str, int], dict[str, int] | None]:
    by_day = snapshot.config_payload.get("daily_shift_requirements_by_day")
    req = None
    if isinstance(by_day, list) and d < len(by_day) and isinstance(by_day[d], dict):
        req = by_day[d]
    if req is None:
        req = snapshot.config_payload.get("daily_shift_requirements") or {}
    max_by_day = snapshot.config_payload.get("daily_shift_requirements_max_by_day")
    max_req = None
    if isinstance(max_by_day, list) and d < len(max_by_day) and isinstance(max_by_day[d], dict):
        max_req = max_by_day[d]
    return req, max_req


def _atoms_by_key(atoms: list[AssignmentAtom]) -> dict[tuple[int, int], AssignmentAtom]:
    return {(a.nurse_index, a.day_index): a for a in atoms}


def _apply_action(snapshot: SemanticsSnapshot, atoms: list[AssignmentAtom], action: SimulationAction) -> tuple[list[AssignmentAtom], set[int], list[str]]:
    by_key = _atoms_by_key(atoms)
    changed_nurses: set[int] = set()
    changed_ids: list[str] = []
    nurse_id_to_idx = {n.nurse_id: n.nurse_index for n in snapshot.nurses}
    for item in action.assignments:
        nurse_id = str(item.get("nurse_id"))
        day = int(item.get("day_index", item.get("day", 0)))
        if day > 0 and "day_index" not in item:
            day -= 1
        shift_code = str(item.get("shift_code", item.get("shift"))).strip().upper()
        n_idx = nurse_id_to_idx.get(nurse_id)
        if n_idx is None:
            continue
        prev = by_key.get((n_idx, day))
        coupled_unit_id = prev.coupled_unit_id if prev else None
        counts_to_coverage = shift_code != "O" and (n_idx, day) not in snapshot.coverage_exclude_cells
        by_key[(n_idx, day)] = AssignmentAtom(
            atom_id=make_atom_id(nurse_id, snapshot.year, snapshot.month, day, shift_code),
            nurse_index=n_idx,
            nurse_id=nurse_id,
            day_index=day,
            shift_code=shift_code,
            source="agent_action",
            is_fixed=False,
            is_forced=False,
            counts_to_coverage=counts_to_coverage,
            override_reason=None,
            coupled_unit_id=coupled_unit_id,
            created_by_action_id=action.action_id,
        )
        changed_nurses.add(n_idx)
        changed_ids.append(by_key[(n_idx, day)].atom_id)
    return list(by_key.values()), changed_nurses, changed_ids


def _evaluate_coverage(snapshot: SemanticsSnapshot, atoms_by_key: dict[tuple[int, int], AssignmentAtom], changed_days: set[int]) -> list[ConstraintEvaluation]:
    evaluations: list[ConstraintEvaluation] = []
    for d in sorted(changed_days):
        req_map, max_map = _requirements_for_day(snapshot, d)
        for code in [c for c in snapshot.shift_types if c != "O"]:
            need = int((req_map or {}).get(code, 0) or 0)
            assigned = sum(1 for atom in atoms_by_key.values() if atom.day_index == d and atom.shift_code == code and atom.counts_to_coverage)
            slack = assigned - need
            evaluations.append(ConstraintEvaluation(node_id=f"coverage:min:{d}:{code}", valid=slack >= 0, slack=slack, pressure=max(0, -slack), details={"day": d + 1, "shift": code, "need": need, "assigned": assigned}))
            if isinstance(max_map, dict):
                max_need = int((max_map or {}).get(code, 0) or 0)
                if max_need > 0:
                    slack_max = max_need - assigned
                    evaluations.append(ConstraintEvaluation(node_id=f"coverage:max:{d}:{code}", valid=slack_max >= 0, slack=slack_max, pressure=max(0, -slack_max), details={"day": d + 1, "shift": code, "max_need": max_need, "assigned": assigned}))
    return evaluations


def _evaluate_preceptee(snapshot: SemanticsSnapshot, atoms_by_key: dict[tuple[int, int], AssignmentAtom], changed_nurses: set[int], changed_days: set[int]) -> list[ConstraintEvaluation]:
    evaluations: list[ConstraintEvaluation] = []
    for pf in snapshot.preceptee_facts:
        if pf.nurse_index not in changed_nurses and (pf.preceptor_index is None or pf.preceptor_index not in changed_nurses):
            continue
        if pf.preceptor_index is None:
            continue
        for d in sorted(changed_days):
            if pf.follow_enabled and not pf.full_month_default_follow and d not in pf.follow_days:
                continue
            pte = atoms_by_key.get((pf.nurse_index, d))
            pre = atoms_by_key.get((pf.preceptor_index, d))
            if pte is None or pre is None:
                continue
            valid = pte.shift_code == pre.shift_code or d in pf.fixed_wanted_override_days
            evaluations.append(ConstraintEvaluation(node_id=f"preceptee_sync:{pf.nurse_index}:{d}", valid=valid, slack=0 if valid else -1, pressure=0 if valid else 1, details={"day": d + 1, "preceptee_shift": pte.shift_code, "preceptor_shift": pre.shift_code, "override": d in pf.fixed_wanted_override_days}))
    return evaluations


def _evaluate_team_min(snapshot: SemanticsSnapshot, atoms_by_key: dict[tuple[int, int], AssignmentAtom], changed_days: set[int]) -> list[ConstraintEvaluation]:
    team_cfg = snapshot.config_payload.get("team_min_by_team") or {}
    if not team_cfg or snapshot.config_payload.get("grade_strategy") not in {"TEAM", "COMBINED"}:
        return []
    nurse_by_team: dict[str, list[int]] = {}
    for nurse in snapshot.nurses:
        if nurse.team_id:
            nurse_by_team.setdefault(nurse.team_id, []).append(nurse.nurse_index)
    evaluations: list[ConstraintEvaluation] = []
    for team_id, mins in team_cfg.items():
        members = nurse_by_team.get(str(team_id), [])
        for d in sorted(changed_days):
            active = [n for n in members if d in snapshot.active_days_by_nurse.get(n, set())]
            for code, min_val in (mins or {}).items():
                min_need = int(min_val or 0)
                if min_need <= 0:
                    continue
                assigned = sum(1 for n in active if (n, d) in atoms_by_key and atoms_by_key[(n, d)].shift_code == code)
                slack = assigned - min_need
                evaluations.append(ConstraintEvaluation(node_id=f"team_min:{team_id}:{d}:{code}", valid=slack >= 0, slack=slack, pressure=max(0, -slack), details={"day": d + 1, "team": team_id, "shift": code, "need": min_need, "assigned": assigned, "active": len(active)}))
    return evaluations


def _evaluate_grade(snapshot: SemanticsSnapshot, atoms_by_key: dict[tuple[int, int], AssignmentAtom], changed_days: set[int]) -> list[ConstraintEvaluation]:
    strategy = snapshot.config_payload.get("grade_strategy")
    grade_cfg = snapshot.config_payload.get("grade_config") or {}
    constraints_map = (grade_cfg or {}).get("constraints") or (grade_cfg or {}).get("constraints_json") or {}
    if strategy not in {"GRADE", "COMBINED"} or not constraints_map:
        return []
    evals: list[ConstraintEvaluation] = []
    nurses_by_grade: dict[int, set[int]] = {}
    for nurse in snapshot.nurses:
        if nurse.grade is not None:
            nurses_by_grade.setdefault(int(nurse.grade), set()).add(nurse.nurse_index)
    for d in sorted(changed_days):
        req_map, _ = _requirements_for_day(snapshot, d)
        for shift_code, grade_map in constraints_map.items():
            if shift_code not in {"D", "E", "N", "M"}:
                continue
            need = int((req_map or {}).get(shift_code, 0) or 0)
            for grade_key, ratio_or_val in (grade_map or {}).items():
                try:
                    grade = int(grade_key)
                except Exception:
                    continue
                target = int(ratio_or_val or 0)
                if target <= 0 or need <= 0:
                    continue
                assigned = sum(1 for n in nurses_by_grade.get(grade, set()) if (n, d) in atoms_by_key and atoms_by_key[(n, d)].shift_code == shift_code)
                slack = assigned - target
                evals.append(ConstraintEvaluation(node_id=f"grade_min:{grade}:{d}:{shift_code}", valid=slack >= 0, slack=slack, pressure=max(0, -slack), details={"day": d + 1, "grade": grade, "shift": shift_code, "need": target, "assigned": assigned}))
    return evals


def _evaluate_nurse_local(snapshot: SemanticsSnapshot, atoms_by_key: dict[tuple[int, int], AssignmentAtom], changed_nurses: set[int]) -> list[ConstraintEvaluation]:
    evals: list[ConstraintEvaluation] = []
    max_work = int(snapshot.config_payload.get("max_consecutive_work_days") or 0)
    max_nights = int(snapshot.config_payload.get("max_consecutive_nights") or 0)
    max_month_night = int(snapshot.config_payload.get("max_night_shifts_per_month") or 0)
    states_by_nurse = build_nurse_day_states(snapshot=snapshot, atoms_by_nurse_day=atoms_by_key)
    for n in changed_nurses:
        nurse_states = states_by_nurse.get(n, [])
        if max_work > 0:
            for st in nurse_states:
                if st.consecutive_work > max_work:
                    evals.append(ConstraintEvaluation(node_id=f"consecutive_work:{n}:{st.day_index}", valid=False, slack=max_work - st.consecutive_work, pressure=st.consecutive_work - max_work, details={"day": st.day_index + 1, "consecutive_work": st.consecutive_work, "limit": max_work, "notes": st.notes}))
        if max_nights > 0:
            for st in nurse_states:
                if st.consecutive_nights > max_nights:
                    evals.append(ConstraintEvaluation(node_id=f"consecutive_night:{n}:{st.day_index}", valid=False, slack=max_nights - st.consecutive_nights, pressure=st.consecutive_nights - max_nights, details={"day": st.day_index + 1, "consecutive_nights": st.consecutive_nights, "limit": max_nights, "notes": st.notes}))
        if max_month_night > 0:
            monthly_n = sum(1 for st in nurse_states if st.assigned_shift == "N")
            evals.append(ConstraintEvaluation(node_id=f"monthly_night_cap:{n}", valid=monthly_n <= max_month_night, slack=max_month_night - monthly_n, pressure=max(0, monthly_n - max_month_night), details={"nurse_index": n, "assigned_nights": monthly_n, "limit": max_month_night}))
    return evals


def _evaluate_transition_bans(snapshot: SemanticsSnapshot, atoms_by_key: dict[tuple[int, int], AssignmentAtom], changed_nurses: set[int]) -> list[ConstraintEvaluation]:
    evals: list[ConstraintEvaluation] = []
    states_by_nurse = build_nurse_day_states(snapshot=snapshot, atoms_by_nurse_day=atoms_by_key)
    ban_n_to_d = bool(snapshot.config_payload.get("ban_n_to_d", True))
    ban_e_to_d = bool(snapshot.config_payload.get("ban_e_to_d", True))
    ban_n_to_e = bool(snapshot.config_payload.get("ban_n_to_e", True))
    for n in changed_nurses:
        for st in states_by_nurse.get(n, []):
            if not st.transition_code:
                continue
            if st.transition_code == "N->D" and ban_n_to_d:
                evals.append(ConstraintEvaluation(node_id=f"transition_ban:n_to_d:{n}:{st.day_index}", valid=False, slack=-1, pressure=1, details={"day": st.day_index + 1, "transition": st.transition_code, "notes": st.notes}))
            if st.transition_code == "E->D" and ban_e_to_d:
                evals.append(ConstraintEvaluation(node_id=f"transition_ban:e_to_d:{n}:{st.day_index}", valid=False, slack=-1, pressure=1, details={"day": st.day_index + 1, "transition": st.transition_code, "notes": st.notes}))
            if st.transition_code == "N->E" and ban_n_to_e:
                evals.append(ConstraintEvaluation(node_id=f"transition_ban:n_to_e:{n}:{st.day_index}", valid=False, slack=-1, pressure=1, details={"day": st.day_index + 1, "transition": st.transition_code, "notes": st.notes}))
    return evals


def _evaluate_recovery_debt(snapshot: SemanticsSnapshot, atoms_by_key: dict[tuple[int, int], AssignmentAtom], changed_nurses: set[int]) -> list[ConstraintEvaluation]:
    evals: list[ConstraintEvaluation] = []
    states_by_nurse = build_nurse_day_states(snapshot=snapshot, atoms_by_nurse_day=atoms_by_key)
    for n in changed_nurses:
        nurse_states = states_by_nurse.get(n, [])
        if not nurse_states:
            continue
        first_state = nurse_states[0]
        if first_state.recovery_debt > 0 and first_state.assigned_shift != "O":
            evals.append(
                ConstraintEvaluation(
                    node_id=f"recovery_debt:first_day:{n}:{first_state.day_index}",
                    valid=False,
                    slack=-first_state.recovery_debt,
                    pressure=first_state.recovery_debt,
                    details={
                        "day": first_state.day_index + 1,
                        "assigned_shift": first_state.assigned_shift,
                        "recovery_debt": first_state.recovery_debt,
                        "notes": first_state.notes,
                    },
                )
            )
    return evals


def _evaluate_fatigue_risk(snapshot: SemanticsSnapshot, atoms_by_key: dict[tuple[int, int], AssignmentAtom], changed_nurses: set[int]) -> list[ConstraintEvaluation]:
    evals: list[ConstraintEvaluation] = []
    states_by_nurse = build_nurse_day_states(snapshot=snapshot, atoms_by_nurse_day=atoms_by_key)
    threshold = 8.0
    for n in changed_nurses:
        for st in states_by_nurse.get(n, []):
            if st.fatigue_score >= threshold:
                evals.append(
                    ConstraintEvaluation(
                        node_id=f"fatigue_risk:{n}:{st.day_index}",
                        valid=True,
                        slack=0.0,
                        pressure=st.fatigue_score,
                        details={
                            "day": st.day_index + 1,
                            "fatigue_score": st.fatigue_score,
                            "transition": st.transition_code,
                            "notes": st.notes,
                        },
                    )
                )
    return evals


def simulate_action(*, snapshot: SemanticsSnapshot, current_atoms: list[AssignmentAtom], action: SimulationAction) -> SimulationResult:
    updated_atoms, changed_nurses, changed_atom_ids = _apply_action(snapshot, current_atoms, action)
    atoms_by_key = _atoms_by_key(updated_atoms)
    changed_days = {a.day_index for a in updated_atoms if a.created_by_action_id == action.action_id}
    triggered: list[DerivedAtom] = []
    causal_chain: list[str] = []
    for n in changed_nurses:
        transitions, derived = simulate_nurse_local_delta(snapshot=snapshot, nurse_index=n, changed_days=changed_days, atoms_by_nurse_day=atoms_by_key)
        triggered.extend(derived)
        for tr in transitions:
            causal_chain.append(f"nurse={n} day={tr.from_day + 1} trigger={tr.trigger}")
    evaluations = []
    evaluations.extend(_evaluate_coverage(snapshot, atoms_by_key, changed_days))
    evaluations.extend(_evaluate_preceptee(snapshot, atoms_by_key, changed_nurses, changed_days))
    evaluations.extend(_evaluate_team_min(snapshot, atoms_by_key, changed_days))
    evaluations.extend(_evaluate_grade(snapshot, atoms_by_key, changed_days))
    evaluations.extend(_evaluate_nurse_local(snapshot, atoms_by_key, changed_nurses))
    evaluations.extend(_evaluate_transition_bans(snapshot, atoms_by_key, changed_nurses))
    evaluations.extend(_evaluate_recovery_debt(snapshot, atoms_by_key, changed_nurses))
    evaluations.extend(_evaluate_fatigue_risk(snapshot, atoms_by_key, changed_nurses))
    violations = [ev for ev in evaluations if not ev.valid]
    risky = [ev for ev in evaluations if ev.valid and ev.slack is not None and isinstance(ev.slack, (int, float)) and ev.slack == 0]
    severity: SimulationSeverity = "ok"
    if violations:
        severity = "hard_violation"
    elif risky:
        severity = "warning"
    return SimulationResult(
        action_id=action.action_id,
        valid_under_current_semantics=not violations,
        severity=severity,
        changed_atom_ids=changed_atom_ids,
        triggered_forced_atoms=triggered,
        violated_constraints=violations,
        risky_constraints=risky,
        causal_chain=causal_chain,
        notes=[],
    )


def build_current_atoms_from_roster_system(snapshot: SemanticsSnapshot, rs) -> list[AssignmentAtom]:
    return build_assignment_atoms(snapshot, rs.roster)


def analyze_current_roster(*, snapshot: SemanticsSnapshot, current_atoms: list[AssignmentAtom]) -> CurrentRosterAnalysis:
    atoms_by_key = _atoms_by_key(current_atoms)
    changed_days = set()
    for days in snapshot.active_days_by_nurse.values():
        changed_days.update(days)
    changed_nurses = set(snapshot.active_days_by_nurse.keys())
    evaluations: list[ConstraintEvaluation] = []
    evaluations.extend(_evaluate_coverage(snapshot, atoms_by_key, changed_days))
    evaluations.extend(_evaluate_preceptee(snapshot, atoms_by_key, changed_nurses, changed_days))
    evaluations.extend(_evaluate_team_min(snapshot, atoms_by_key, changed_days))
    evaluations.extend(_evaluate_grade(snapshot, atoms_by_key, changed_days))
    evaluations.extend(_evaluate_nurse_local(snapshot, atoms_by_key, changed_nurses))
    evaluations.extend(_evaluate_transition_bans(snapshot, atoms_by_key, changed_nurses))
    evaluations.extend(_evaluate_recovery_debt(snapshot, atoms_by_key, changed_nurses))
    evaluations.extend(_evaluate_fatigue_risk(snapshot, atoms_by_key, changed_nurses))
    violations = [ev for ev in evaluations if not ev.valid]
    risky = [ev for ev in evaluations if ev.valid and ev.slack is not None and isinstance(ev.slack, (int, float)) and ev.slack == 0]
    mode_summary: dict[str, dict[str, int]] = {}
    for fact in snapshot.constraint_modes:
        fam = mode_summary.setdefault(fact.family, {})
        fam[fact.effective_mode] = fam.get(fact.effective_mode, 0) + 1
    preflight_payload = [
        {"source": a.source, "severity": a.severity, "code": a.code, "message": a.message}
        for a in snapshot.preflight_alerts
    ]
    return CurrentRosterAnalysis(
        valid_under_current_semantics=not violations,
        atom_count=len(current_atoms),
        fixed_atom_count=sum(1 for a in current_atoms if a.is_fixed),
        preceptee_atom_count=sum(1 for a in current_atoms if a.coupled_unit_id is not None),
        coverage_excluded_atom_count=sum(1 for a in current_atoms if not a.counts_to_coverage),
        hard_violation_count=len(violations),
        risky_constraint_count=len(risky),
        violated_constraints=violations,
        risky_constraints=risky,
        constraint_mode_summary=mode_summary,
        preflight_alerts=preflight_payload,
        notes=[],
    )
