from __future__ import annotations

from dataclasses import dataclass, field

from services.constraint_impact.atoms import AssignmentAtom, DerivedAtom
from services.constraint_impact.snapshot import CarryoverStateArtifact, SemanticsSnapshot


@dataclass(slots=True)
class NurseDayState:
    nurse_index: int
    day_index: int
    assigned_shift: str | None
    previous_shift: str | None
    transition_code: str | None
    consecutive_work: int
    consecutive_nights: int
    recovery_off_required_after_day: int
    fatigue_score: float
    recovery_debt: float
    weekend_only_active: bool
    n_forbidden: bool
    off_count_nonvac_so_far: int
    notes: list[str] = field(default_factory=list)


@dataclass(slots=True)
class NurseStateTransition:
    nurse_index: int
    from_day: int
    to_day: int
    trigger: str
    produced_atoms: list[DerivedAtom] = field(default_factory=list)
    blocked_constraints: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def _derive_required_recovery_offs(consecutive_night_tail: int) -> int:
    if consecutive_night_tail >= 3:
        return 2
    if consecutive_night_tail >= 2:
        return 2
    return 0


def _seed_fatigue_score(*, consecutive_work: int, consecutive_nights: int, previous_shift: str | None) -> float:
    score = float(consecutive_work)
    score += float(consecutive_nights) * 2.0
    if previous_shift == "N":
        score += 1.5
    elif previous_shift == "E":
        score += 0.5
    return score


def _artifact_base_state(artifact: CarryoverStateArtifact | None) -> tuple[int, int, int, str | None, float, float, list[str]]:
    if artifact is None or not artifact.carries_state or artifact.selected_schedule_basis == "blank":
        return 0, 0, 0, None, 0.0, 0.0, [] if artifact is None else [f"carryover={artifact.selected_schedule_basis}:blank_state"]
    metrics = artifact.metrics or {}
    consec_work = int(metrics.get("consecutive_work_tail", 0) or 0)
    consec_n = int(metrics.get("consecutive_night_tail", 0) or 0)
    off_count = int(metrics.get("consecutive_off_tail", 0) or 0)
    offs_after_tail_nights = int(metrics.get("offs_after_tail_nights", 0) or 0)
    previous_shift = metrics.get("last_day_shift")
    if previous_shift in (None, "") and artifact.tail_sequence:
        previous_shift = artifact.tail_sequence[-1]
    previous_shift = str(previous_shift).strip().upper() if previous_shift not in (None, "") else None
    recovery_debt = float(max(0, _derive_required_recovery_offs(consec_n) - offs_after_tail_nights))
    fatigue_score = _seed_fatigue_score(
        consecutive_work=consec_work,
        consecutive_nights=consec_n,
        previous_shift=previous_shift,
    )
    notes = [
        f"carryover={artifact.selected_schedule_basis}",
        f"tail={' '.join(artifact.tail_sequence or [])}",
        f"recovery_debt={recovery_debt}",
        f"fatigue_score={fatigue_score}",
    ]
    return consec_work, consec_n, off_count, previous_shift, fatigue_score, recovery_debt, notes


def _carryover_map_for_nurse(snapshot: SemanticsSnapshot, nurse_id: str) -> dict[int, CarryoverStateArtifact]:
    return {
        int(a.boundary_day_index): a
        for a in (snapshot.carryover_artifacts or [])
        if a.nurse_id == nurse_id
    }


def build_nurse_day_states(*, snapshot: SemanticsSnapshot, atoms_by_nurse_day: dict[tuple[int, int], AssignmentAtom]) -> dict[int, list[NurseDayState]]:
    states: dict[int, list[NurseDayState]] = {}
    for nurse in snapshot.nurses:
        day_states: list[NurseDayState] = []
        consec_work = 0
        consec_n = 0
        off_count = 0
        previous_shift: str | None = None
        fatigue_score = 0.0
        recovery_debt = 0.0
        carry_map = _carryover_map_for_nurse(snapshot, nurse.nurse_id)
        prev_active_day: int | None = None
        for d in sorted(snapshot.active_days_by_nurse.get(nurse.nurse_index, set())):
            notes: list[str] = []
            is_reentry_boundary = prev_active_day is None or d != prev_active_day + 1
            if is_reentry_boundary:
                artifact = carry_map.get(d)
                consec_work, consec_n, off_count, previous_shift, fatigue_score, recovery_debt, carry_notes = _artifact_base_state(artifact)
                notes.extend(carry_notes)
                if artifact is None and prev_active_day is not None:
                    notes.append("gap_without_carryover:reset_state")
            atom = atoms_by_nurse_day.get((nurse.nurse_index, d))
            shift = atom.shift_code if atom else None
            transition_code = f"{previous_shift}->{shift}" if previous_shift and shift else None
            if shift and shift != "O":
                consec_work += 1
            else:
                consec_work = 0
            if shift == "N":
                consec_n += 1
            else:
                consec_n = 0
            if shift == "O" and (nurse.nurse_index, d) not in snapshot.vacation_off_cells:
                off_count += 1
                recovery_debt = max(0.0, recovery_debt - 1.0)
                fatigue_score = max(0.0, fatigue_score - 1.5)
            elif shift and shift != "O":
                fatigue_score += 1.0
                if shift == "N":
                    fatigue_score += 1.5
                if transition_code in {"N->D", "N->E"}:
                    fatigue_score += 2.0
                elif transition_code == "E->D":
                    fatigue_score += 1.0
            recovery = 2 if consec_n >= 3 else 2 if consec_n >= 2 else 0
            day_states.append(
                NurseDayState(
                    nurse_index=nurse.nurse_index,
                    day_index=d,
                    assigned_shift=shift,
                    previous_shift=previous_shift,
                    transition_code=transition_code,
                    consecutive_work=consec_work,
                    consecutive_nights=consec_n,
                    recovery_off_required_after_day=recovery,
                    fatigue_score=fatigue_score,
                    recovery_debt=recovery_debt,
                    weekend_only_active=nurse.is_weekend_off,
                    n_forbidden=nurse.nurse_index in snapshot.n_forbid_n,
                    off_count_nonvac_so_far=off_count,
                    notes=notes,
                )
            )
            previous_shift = shift
            prev_active_day = d
        states[nurse.nurse_index] = day_states
    return states


def simulate_nurse_local_delta(*, snapshot: SemanticsSnapshot, nurse_index: int, changed_days: set[int], atoms_by_nurse_day: dict[tuple[int, int], AssignmentAtom]) -> tuple[list[NurseStateTransition], list[DerivedAtom]]:
    transitions: list[NurseStateTransition] = []
    derived_atoms: list[DerivedAtom] = []
    by_day = {st.day_index: st for st in build_nurse_day_states(snapshot=snapshot, atoms_by_nurse_day=atoms_by_nurse_day).get(nurse_index, [])}
    if not changed_days:
        return transitions, derived_atoms
    for d in sorted(changed_days):
        st = by_day.get(d)
        if st is None:
            continue
        if st.assigned_shift == "N" and st.consecutive_nights >= 2:
            for off_day in (d + 1, d + 2):
                derived_atoms.append(
                    DerivedAtom(
                        atom_id=f"derived:{nurse_index}:{off_day}:O:recovery",
                        family="forced_off",
                        nurse_index=nurse_index,
                        day_index=off_day,
                        shift_code="O",
                        caused_by=[f"night_block_end:{nurse_index}:{d}"],
                        notes=["2N/3N 이후 회복 OFF 필요"],
                    )
                )
            transitions.append(
                NurseStateTransition(
                    nurse_index=nurse_index,
                    from_day=d,
                    to_day=min(d + 2, snapshot.leave[nurse_index]),
                    trigger="night_recovery",
                    produced_atoms=derived_atoms[-2:],
                )
            )
    return transitions, derived_atoms
