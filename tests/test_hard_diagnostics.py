from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Mapping, Sequence
from pathlib import Path
import sys
import importlib

import numpy as np
from numpy.typing import NDArray

try:
    from app.services.cp_sat.hard_diagnostics import collect_hard_diagnostics
except ModuleNotFoundError:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))
    collect_hard_diagnostics = importlib.import_module(
        "services.cp_sat.hard_diagnostics"
    ).collect_hard_diagnostics


@dataclass
class DummyNurse:
    nurse_id: str
    name: str
    is_weekend_off: bool = False
    preceptor_id: str | None = None
    db_id: str | None = None


@dataclass
class DummyConfig:
    shift_types: list[str]
    num_shifts: int
    global_monthly_off_days: int = 0
    standard_personal_off_days: int = 0
    max_extra_off_days: int = 0
    weekend_off_only_enable: bool = True
    preceptee_on: bool = False
    daily_shift_requirements: dict[str, int] | None = None
    daily_shift_requirements_by_day: list[dict[str, int]] | None = None


class DummyRosterSystem:
    def __init__(
        self,
        roster: NDArray[np.int_],
        violations: Sequence[Mapping[str, object]],
        weekend_off_nurse_indexes: set[int] | None = None,
    ):
        weekend_off_nurse_indexes = set(weekend_off_nurse_indexes or set())
        self.roster = roster
        self.num_days = roster.shape[1]
        self.nurses = [
            DummyNurse("n1", "Nurse1", is_weekend_off=0 in weekend_off_nurse_indexes, db_id="n1"),
            DummyNurse("n2", "Nurse2", is_weekend_off=1 in weekend_off_nurse_indexes, db_id="n2"),
        ]
        self.config = DummyConfig(shift_types=["D", "E", "N", "O"], num_shifts=4)
        self.target_month = date(2026, 11, 1)
        self.initial_forbidden: dict[tuple[int, int], set[str]] = {}
        self.weekly_off_by_idx: dict[int, list[int]] = {}
        self.fixed_cells: list[dict[str, object]] = []
        self._diag_vacation_off_cells: set[tuple[int, int]] = set()
        self._diag_weekly_off_by_idx: dict[int, list[int]] = {}
        self._diag_weekend_days: set[int] = set()
        self._violations = [dict(v) for v in violations]

    def _find_violations(self) -> list[dict[str, object]]:
        return list(self._violations)


def test_collect_hard_diagnostics_detects_expanded_constraints_beyond_legacy():
    roster = np.zeros((2, 2, 4), dtype=int)
    roster[0, 0, 3] = 1
    roster[0, 1, 3] = 1
    roster[0, 1, 0] = 1
    roster[1, 0, 0] = 1
    roster[1, 1, 0] = 1

    rs = DummyRosterSystem(roster=roster, violations=[])
    result = collect_hard_diagnostics(rs)

    assert result.legacy_hard_count == 0
    assert result.expanded_hard_count > 0
    assert result.expanded_by_type.get("exactly_one", 0) >= 1
    assert result.expanded_by_type.get("off_max", 0) >= 1
    assert "exactly_one" in result.mismatch_by_type


def test_collect_hard_diagnostics_keeps_legacy_counts():
    roster = np.zeros((2, 2, 4), dtype=int)
    roster[0, 0, 0] = 1
    roster[0, 1, 0] = 1
    roster[1, 0, 0] = 1
    roster[1, 1, 0] = 1

    legacy = [{"type": "shift_requirement", "day": 0, "shift": "D", "required": 2, "actual": 1}]
    rs = DummyRosterSystem(roster=roster, violations=legacy)

    result = collect_hard_diagnostics(rs)

    assert result.legacy_hard_count == 1
    assert result.legacy_by_type.get("shift_requirement") == 1
    assert result.expanded_by_type.get("shift_requirement") == 1


def test_collect_hard_diagnostics_structural_coverage_hint():
    roster = np.zeros((2, 2, 4), dtype=int)
    roster[0, 0, 3] = 1
    roster[0, 1, 0] = 1
    roster[1, 0, 0] = 1
    roster[1, 1, 0] = 1

    rs = DummyRosterSystem(roster=roster, violations=[])
    rs.config.daily_shift_requirements = {"D": 2}
    rs.fixed_cells = [{"nurse_index": 0, "day_index": 0, "shift": "O"}]

    result = collect_hard_diagnostics(rs)

    assert len(result.structural_coverage_hints) >= 1
    top = result.structural_coverage_hints[0]
    assert int(top.get("day", 0)) == 1
    assert int(top.get("deficit", 0)) >= 1


def test_collect_hard_diagnostics_off_partition_counts():
    roster = np.zeros((2, 3, 4), dtype=int)
    roster[0, 0, 3] = 1
    roster[0, 1, 3] = 1
    roster[0, 2, 0] = 1
    roster[1, 0, 3] = 1
    roster[1, 1, 0] = 1
    roster[1, 2, 0] = 1

    rs = DummyRosterSystem(roster=roster, violations=[])
    rs.config.max_extra_off_days = 3
    rs._diag_vacation_off_cells = {(0, 0)}
    rs._diag_weekly_off_by_idx = {0: [1]}
    rs._diag_weekend_days = {2}

    result = collect_hard_diagnostics(rs)

    assert result.off_partition_counts.get("V") == 1
    assert result.off_partition_counts.get("Wo") == 1
    assert result.off_partition_counts.get("O") == 1
    assert result.off_partition_counts.get("off_total") == 3
    assert result.off_regulation_counts.get("off_partition_mismatch") == 0
    assert result.global_error_indicator.is_feasible is True


def test_collect_hard_diagnostics_weekend_off_regulation_and_global_indicator():
    roster = np.zeros((2, 2, 4), dtype=int)
    roster[0, 0, 0] = 1
    roster[0, 1, 3] = 1
    roster[1, 0, 0] = 1
    roster[1, 1, 0] = 1

    rs = DummyRosterSystem(roster=roster, violations=[], weekend_off_nurse_indexes={0})
    rs._diag_weekend_days = {0}
    result = collect_hard_diagnostics(rs)

    assert result.off_regulation_counts.get("weekend_off_missing_weekend_o") == 1
    assert result.off_regulation_counts.get("weekend_off_weekday_natural_o") == 1
    assert result.expanded_by_type.get("weekend_off_missing_weekend_o", 0) >= 1
    assert result.expanded_by_type.get("weekend_off_weekday_natural_o", 0) >= 1
    assert result.global_error_indicator.is_feasible is False
    assert any("Weekend-off policy" in issue for issue in result.global_error_indicator.primary_issues)


def test_collect_hard_diagnostics_global_indicator_recommendations_for_initial_forbidden_and_off_max():
    roster = np.zeros((2, 2, 4), dtype=int)
    roster[0, 0, 0] = 1
    roster[0, 1, 3] = 1
    roster[1, 0, 0] = 1
    roster[1, 1, 0] = 1

    rs = DummyRosterSystem(roster=roster, violations=[])
    rs.initial_forbidden = {(0, 0): {"D"}}
    rs.config.global_monthly_off_days = 0
    rs.config.standard_personal_off_days = 0
    rs.config.max_extra_off_days = 0

    result = collect_hard_diagnostics(rs)

    assert result.expanded_by_type.get("initial_forbidden", 0) >= 1
    assert result.expanded_by_type.get("off_max", 0) >= 1
    assert result.global_error_indicator.is_feasible is False
    joined_recs = "\n".join(result.global_error_indicator.recommendations)
    assert "forbidden" in joined_recs.lower()
    assert "max_extra_off_days" in joined_recs
