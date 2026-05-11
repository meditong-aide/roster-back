from __future__ import annotations

from dataclasses import dataclass, field

from services.constraint_impact.snapshot import SemanticsSnapshot
from services.constraint_impact.types import AtomSource


@dataclass(slots=True)
class AssignmentAtom:
    atom_id: str
    nurse_index: int
    nurse_id: str
    day_index: int
    shift_code: str
    source: AtomSource
    is_fixed: bool
    is_forced: bool
    counts_to_coverage: bool
    override_reason: str | None
    coupled_unit_id: str | None
    created_by_action_id: str | None = None


@dataclass(slots=True)
class DerivedAtom:
    atom_id: str
    family: str
    nurse_index: int
    day_index: int
    shift_code: str
    caused_by: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def make_atom_id(nurse_id: str, year: int, month: int, day_index: int, shift_code: str) -> str:
    return f"assign:{nurse_id}:{year:04d}-{month:02d}-{day_index + 1:02d}:{shift_code}"


def _fixed_cell_map(snapshot: SemanticsSnapshot) -> dict[tuple[int, int], AssignmentAtom]:
    out: dict[tuple[int, int], AssignmentAtom] = {}
    for fc in snapshot.fixed_cells:
        source: AtomSource = "manual_fixed"
        override_reason = None
        if str(fc.fixed_source).lower() == "fixed_wanted":
            source = "fixed_wanted"
            override_reason = "fixed_wanted"
        elif fc.shift_type in {"휴가", "공가"}:
            source = "special_fixed"
        elif fc.shift_code_main == "O":
            source = "weekly_off"
        out[(fc.nurse_index, fc.day_index)] = AssignmentAtom(
            atom_id=make_atom_id(fc.nurse_id, snapshot.year, snapshot.month, fc.day_index, fc.shift_code_main),
            nurse_index=fc.nurse_index,
            nurse_id=fc.nurse_id,
            day_index=fc.day_index,
            shift_code=fc.shift_code_main,
            source=source,
            is_fixed=True,
            is_forced=False,
            counts_to_coverage=fc.counts_to_coverage,
            override_reason=override_reason,
            coupled_unit_id=None,
        )
    return out


def build_assignment_atoms(snapshot: SemanticsSnapshot, roster) -> list[AssignmentAtom]:
    fixed_map = _fixed_cell_map(snapshot)
    shift_types = snapshot.shift_types
    preceptee_by_nurse = {pf.nurse_index: pf for pf in snapshot.preceptee_facts}
    atoms: list[AssignmentAtom] = []
    for nurse in snapshot.nurses:
        unit_id = f"preceptor_unit:{preceptee_by_nurse[nurse.nurse_index].preceptor_id}" if nurse.nurse_index in preceptee_by_nurse and preceptee_by_nurse[nurse.nurse_index].preceptor_id else None
        active_days = snapshot.active_days_by_nurse.get(nurse.nurse_index, set())
        for d in sorted(active_days):
            fixed_atom = fixed_map.get((nurse.nurse_index, d))
            if fixed_atom is not None:
                fixed_atom.coupled_unit_id = unit_id
                atoms.append(fixed_atom)
                continue
            assigned_idx = None
            for s_idx, val in enumerate(roster[nurse.nurse_index, d]):
                if int(val) == 1:
                    assigned_idx = s_idx
                    break
            if assigned_idx is None:
                continue
            shift_code = shift_types[assigned_idx]
            pf = preceptee_by_nurse.get(nurse.nurse_index)
            is_synced = bool(pf and pf.follow_enabled and (pf.full_month_default_follow or d in pf.follow_days))
            counts_to_coverage = (nurse.nurse_index, d) not in snapshot.coverage_exclude_cells and not (shift_code == "O")
            if pf and not pf.counts_to_coverage:
                counts_to_coverage = False if shift_code in {"D", "E", "N", "M"} else counts_to_coverage
            atoms.append(
                AssignmentAtom(
                    atom_id=make_atom_id(nurse.nurse_id, snapshot.year, snapshot.month, d, shift_code),
                    nurse_index=nurse.nurse_index,
                    nurse_id=nurse.nurse_id,
                    day_index=d,
                    shift_code=shift_code,
                    source="preceptee_sync" if is_synced else "solver",
                    is_fixed=False,
                    is_forced=False,
                    counts_to_coverage=counts_to_coverage,
                    override_reason=None,
                    coupled_unit_id=unit_id,
                )
            )
    return atoms
