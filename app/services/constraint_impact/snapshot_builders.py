from __future__ import annotations

from copy import deepcopy
from datetime import timedelta
from typing import Any

from services.constraint_impact.snapshot import (
    AssignmentWindowFact,
    CarryoverStateArtifact,
    ConstraintModeFact,
    FixedCellFact,
    NurseFact,
    PrecepteeFact,
    PreflightAlertFact,
    SemanticsSnapshot,
    SolveAttemptMeta,
)
from services.cp_sat.allowed_shift_types import normalize_allowed_shift_codes


def _active_days_map(join: list[int], leave: list[int], blocked_by_nurse: dict[int, set[int]], num_days: int) -> dict[int, set[int]]:
    out: dict[int, set[int]] = {}
    for n, (j, l) in enumerate(zip(join, leave)):
        blocked = blocked_by_nurse.get(n, set())
        out[n] = {d for d in range(j, min(l, num_days - 1) + 1) if 0 <= d < num_days and d not in blocked}
    return out


def _preceptor_id_from_period(rs, idx: int) -> str | None:
    """period SSOT(preceptee_preceptor_idx: 프리셉티 idx→프리셉터 idx)에서 프리셉터 nurse_id.

    캐시(nurses.preceptor_id) 미사용 — 온톨로지 관계도 period 유래로 일원화.
    """
    pmap = getattr(rs, "preceptee_preceptor_idx", {}) or {}
    p = pmap.get(idx)
    if p is None or not (0 <= p < len(rs.nurses)):
        return None
    pn = rs.nurses[p]
    return str(getattr(pn, "db_id", getattr(pn, "nurse_id", p)))


def _collect_nurse_facts(rs, join: list[int], leave: list[int]) -> list[NurseFact]:
    use_mid = bool(getattr(rs.config, "use_mid", False))
    facts: list[NurseFact] = []
    for idx, nurse in enumerate(rs.nurses):
        allowed = normalize_allowed_shift_codes(getattr(nurse, "allowed_shifts", None), use_mid=use_mid) or set(rs.config.shift_types)
        grade_val = getattr(nurse, "grade", None)
        try:
            grade_val = int(grade_val) if grade_val is not None else None
        except Exception:
            grade_val = None
        facts.append(
            NurseFact(
                nurse_index=idx,
                nurse_id=str(getattr(nurse, "db_id", getattr(nurse, "nurse_id", idx))),
                name=str(getattr(nurse, "name", "?")),
                team_id=None if getattr(nurse, "team_id", None) in (None, "", 0) else str(getattr(nurse, "team_id")),
                grade=grade_val,
                # TODO(weekend-period): period as-of 로 전환 필요(호출측 db 주입)
                is_weekend_off=bool(getattr(nurse, "is_weekend_off", False)),
                allowed_shift_codes=set(allowed),
                preceptor_id=_preceptor_id_from_period(rs, idx),
                is_inbound=bool(getattr(nurse, "is_inbound", False)),
                join_day=join[idx],
                leave_day=leave[idx],
            )
        )
    return facts


def _collect_fixed_cells(snapshot_year: int, snapshot_month: int, rs, fixed_type_by_cell: dict[tuple[int, int], str | None], fixed_wanted_cells: set[tuple[int, int]], coverage_exclude_cells: set[tuple[int, int]]) -> list[FixedCellFact]:
    shift_id_to_main = getattr(rs, "shift_id_to_main", {}) or {}
    nurse_lookup = {idx: str(getattr(n, "db_id", getattr(n, "nurse_id", idx))) for idx, n in enumerate(rs.nurses)}
    out: list[FixedCellFact] = []
    for cell in getattr(rs, "fixed_cells", []) or []:
        n = int(cell.get("nurse_index"))
        d = int(cell.get("day_index"))
        raw = str(cell.get("shift") or "").strip().upper()
        main = shift_id_to_main.get(raw, raw)
        if main in {"OFF", "주"}:
            main = "O"
        counts_to_coverage = main not in {"O", "OFF", "주"} and (n, d) not in coverage_exclude_cells
        out.append(
            FixedCellFact(
                nurse_index=n,
                nurse_id=nurse_lookup.get(n, str(n)),
                day_index=d,
                shift_code_raw=raw,
                shift_code_main=main,
                shift_type=fixed_type_by_cell.get((n, d)) or cell.get("shift_type"),
                fixed_source="fixed_wanted" if (n, d) in fixed_wanted_cells else str(cell.get("fixed_source") or "manual_fixed"),
                counts_to_coverage=counts_to_coverage,
            )
        )
    return out


def _collect_preceptee_facts(rs, active_days_by_nurse: dict[int, set[int]], coverage_exclude_cells: set[tuple[int, int]]) -> list[PrecepteeFact]:
    """프리셉티 fact = nurse_preceptee_period(SSOT) 유래 엔진 맵에서 생성(캐시 preceptor_id 안 봄).

    rs.preceptee_follow_days: {preceptee_idx: set(follow day_idx)} — 그 달 겹치는 구간.
    rs.preceptee_preceptor_idx: {preceptee_idx: preceptor_idx} — WHO.
    days 가 비면 그 달 프리셉티 아님(제외). period 는 항상 명시적 days → full_month_default=False.
    """
    follow_days_raw = getattr(rs, "preceptee_follow_days", {}) or {}
    preceptor_idx_map = getattr(rs, "preceptee_preceptor_idx", {}) or {}
    preceptee_shift_count = bool(getattr(rs.config, "preceptee_shift_count", True))
    follow_enabled = bool(getattr(rs.config, "preceptee_on", False))
    pte_fw = getattr(rs, "_preceptee_fixed_wanted_map", {}) or {}
    out: list[PrecepteeFact] = []
    for idx, days in follow_days_raw.items():
        follow_days = set(days or set())
        if not follow_days:
            continue  # 그 달 프리셉티 아님(종료/미겹침)
        if not (0 <= idx < len(rs.nurses)):
            continue
        nurse = rs.nurses[idx]
        p_idx = preceptor_idx_map.get(idx)
        preceptor_id = None
        if p_idx is not None and 0 <= p_idx < len(rs.nurses):
            pn = rs.nurses[p_idx]
            preceptor_id = str(getattr(pn, "db_id", getattr(pn, "nurse_id", p_idx)))
        override_days = {d for (n, d), _ in pte_fw.items() if n == idx}
        counts_to_coverage = preceptee_shift_count and not any((idx, d) in coverage_exclude_cells for d in active_days_by_nurse.get(idx, set()))
        out.append(
            PrecepteeFact(
                nurse_index=idx,
                nurse_id=str(getattr(nurse, "db_id", getattr(nurse, "nurse_id", idx))),
                preceptor_index=p_idx,
                preceptor_id=preceptor_id,
                follow_enabled=follow_enabled,
                follow_days=follow_days,
                full_month_default_follow=False,  # period SSOT: 항상 명시적 days
                counts_to_coverage=counts_to_coverage,
                fixed_wanted_override_days=override_days,
            )
        )
    return out


def _collect_assignment_window_facts(rs, active_days_by_nurse: dict[int, set[int]]) -> list[AssignmentWindowFact]:
    raw_windows = deepcopy(getattr(rs, "_constraint_impact_assignment_windows", []) or [])
    if not raw_windows:
        return []
    out: list[AssignmentWindowFact] = []
    for row in raw_windows:
        nurse_id = str(row.get("nurse_id") or "")
        if not nurse_id:
            continue
        active_days = {int(d) for d in (row.get("active_day_indices") or []) if 0 <= int(d) < rs.num_days}
        inactive_days = {int(d) for d in (row.get("inactive_day_indices") or []) if 0 <= int(d) < rs.num_days}
        allowed_shift_codes = {str(c).strip().upper() for c in (row.get("allowed_shift_codes") or []) if str(c).strip()}
        out.append(
            AssignmentWindowFact(
                nurse_id=nurse_id,
                direction=str(row.get("direction") or "inbound"),
                source_group_id=None if row.get("source_group_id") in (None, "") else str(row.get("source_group_id")),
                target_group_id=None if row.get("target_group_id") in (None, "") else str(row.get("target_group_id")),
                reason=str(row.get("reason") or "assignment"),
                active_day_indices=active_days,
                inactive_day_indices=inactive_days,
                allowed_shift_codes=allowed_shift_codes,
                carries_state=bool(row.get("carries_state", True)),
                counts_to_coverage=bool(row.get("counts_to_coverage", True)),
                metadata=deepcopy(row.get("metadata") or {}),
            )
        )
    return out


def _collect_carryover_artifacts(rs) -> list[CarryoverStateArtifact]:
    raw_items = deepcopy(getattr(rs, "_constraint_impact_carryover_artifacts", []) or [])
    out: list[CarryoverStateArtifact] = []
    for row in raw_items:
        nurse_id = str(row.get("nurse_id") or "")
        if not nurse_id:
            continue
        out.append(
            CarryoverStateArtifact(
                nurse_id=nurse_id,
                direction=str(row.get("direction") or "inbound"),
                boundary_day_index=int(row.get("boundary_day_index", 0)),
                reference_group_id=None if row.get("reference_group_id") in (None, "") else str(row.get("reference_group_id")),
                selected_schedule_id=None if row.get("selected_schedule_id") in (None, "") else str(row.get("selected_schedule_id")),
                selected_schedule_basis=str(row.get("selected_schedule_basis") or "blank"),
                carries_state=bool(row.get("carries_state", True)),
                tail_sequence=[str(x) for x in (row.get("tail_sequence") or [])],
                metrics=deepcopy(row.get("metrics") or {}),
                metadata=deepcopy(row.get("metadata") or {}),
            )
        )
    return out


def _wrap_preflight_alerts(alerts: list[str], mid_error: str | None) -> list[PreflightAlertFact]:
    out = [
        PreflightAlertFact(source="feasibility_alerts", severity="warning", code="preflight_alert", message=str(msg))
        for msg in (alerts or [])
    ]
    if mid_error:
        out.append(PreflightAlertFact(source="mid_feasibility", severity="blocking", code="mid_feasibility", message=mid_error))
    return out


def _base_constraint_modes(rs, snapshot: SemanticsSnapshot) -> list[ConstraintModeFact]:
    cfg = rs.config
    grade_strategy = str(getattr(rs, "grade_strategy", "COMBINED") or "COMBINED").upper()
    out: list[ConstraintModeFact] = []
    if bool(getattr(cfg, "ban_n_to_d", True)):
        out.append(ConstraintModeFact("transition_ban", "ban_n_to_d", "hard", "enforced", "app/services/cp_sat_basic.py", "N→D 금지 활성"))
    if bool(getattr(cfg, "ban_e_to_d", True)):
        out.append(ConstraintModeFact("transition_ban", "ban_e_to_d", "hard", "enforced", "app/services/cp_sat_basic.py", "E→D 금지 활성"))
    if bool(getattr(cfg, "ban_n_to_e", True)):
        out.append(ConstraintModeFact("transition_ban", "ban_n_to_e", "hard", "enforced", "app/services/cp_sat_basic.py", "N→E 금지 활성"))
    out.append(ConstraintModeFact("consecutive_work", "max_consecutive_work", "hard", "enforced", "app/services/cp_sat_basic.py", "K+1 창에서 OFF≥1, fixed_wanted 우회 불가"))
    if bool(getattr(cfg, "two_offs_after_two_nig", False)):
        out.append(ConstraintModeFact("recovery_2n2o", "two_offs_after_two_nig", "hard", "enforced", "app/services/cp_sat_basic.py", "2N 후 2OFF 활성"))
    if bool(getattr(cfg, "two_offs_after_three_nig", False)):
        out.append(ConstraintModeFact("recovery_3n2o", "two_offs_after_three_nig", "hard", "enforced", "app/services/cp_sat_basic.py", "3N 후 2OFF 활성"))
    if bool(getattr(cfg, "not_one_night", False)):
        out.append(ConstraintModeFact("consecutive_night", "not_one_night", "hard", "enforced", "app/services/cp_sat_basic.py", "1N 금지 활성"))
    if bool(getattr(cfg, "weekend_off_only_enable", True)):
        out.append(ConstraintModeFact("weekend_only", "weekend_off_only", "hard", "enforced", "app/services/cp_sat_basic.py", "주말휴무 hard 활성"))
    team_mode = "soft_fallback" if bool(getattr(cfg, "team_min_soft_fallback", False)) else "enforced"
    if grade_strategy in ("TEAM", "COMBINED") and bool(getattr(cfg, "team_min_by_team", {}) or {}):
        out.append(ConstraintModeFact("team_min", "team_min", "soft" if team_mode == "soft_fallback" else "hard", team_mode, "app/services/constraints/team_constraints.py", "team_min 전략 활성"))
    grade_cfg = getattr(rs, "grade_config", None) or {}
    allow_soft_grade = bool((grade_cfg or {}).get("allow_soft_fallback", False))
    if grade_strategy in ("GRADE", "COMBINED") and bool((grade_cfg or {}).get("constraints") or (grade_cfg or {}).get("constraints_json")):
        out.append(ConstraintModeFact("grade_min", "grade_min", "soft" if allow_soft_grade else "hard", "soft_fallback" if allow_soft_grade else "enforced", "app/services/constraints/grade_constraints.py", "grade min 활성"))
        out.append(ConstraintModeFact("grade_max", "grade_max", "soft" if allow_soft_grade else "hard", "soft_fallback" if allow_soft_grade else "enforced", "app/services/constraints/grade_constraints.py", "grade max 활성"))
    if grade_strategy == "COMBINED" and bool(getattr(cfg, "team_handoff_policy_by_team", {}) or {}):
        allow_soft = bool(getattr(cfg, "team_handoff_soft_fallback", True))
        out.append(ConstraintModeFact("handoff", "team_grade_handoff", "soft" if allow_soft else "hard", "soft_fallback" if allow_soft else "enforced", "app/services/constraints/team_grade_handoff_constraints.py", "team×grade handoff 활성"))
    for raw in getattr(rs, "_constraint_impact_constraint_modes", []) or []:
        try:
            out.append(ConstraintModeFact(**raw))
        except Exception:
            continue
    return out


def _config_payload(rs) -> dict[str, Any]:
    cfg = rs.config
    keys = [
        "preceptee_on", "preceptee_shift_count", "use_mid", "off_first", "weekend_off_only_enable",
        "team_min_soft_fallback", "team_handoff_soft_fallback", "two_offs_after_two_nig",
        "two_offs_after_three_nig", "not_one_night", "ban_n_to_d", "ban_e_to_d", "ban_n_to_e",
        "max_consecutive_work_days", "max_consecutive_nights", "max_night_shifts_per_month", "off_days",
        "daily_shift_requirements", "daily_shift_requirements_by_day", "daily_shift_requirements_max_by_day",
        "team_min_by_team", "team_handoff_policy_by_team",
    ]
    out = {}
    for key in keys:
        out[key] = deepcopy(getattr(cfg, key, None))
    out["grade_strategy"] = str(getattr(rs, "grade_strategy", "COMBINED") or "COMBINED")
    out["grade_config"] = deepcopy(getattr(rs, "grade_config", None))
    return out


def build_semantics_snapshot_from_roster_system(rs, *, year: int | None = None, month: int | None = None) -> SemanticsSnapshot:
    target_month = getattr(rs, "target_month")
    snap_year = int(year or target_month.year)
    snap_month = int(month or target_month.month)
    join = list(getattr(rs, "_constraint_impact_join", []))
    leave = list(getattr(rs, "_constraint_impact_leave", []))
    if not join or not leave:
        join = [0 for _ in rs.nurses]
        leave = [rs.num_days - 1 for _ in rs.nurses]
    blocked_by_nurse = deepcopy(getattr(rs, "blocked_by_nurse", {}) or {})
    _raw_active_days = deepcopy(getattr(rs, "_constraint_impact_active_days", None) or _active_days_map(join, leave, blocked_by_nurse, rs.num_days))
    active_days_by_nurse = {
        int(n): {int(d) for d in (days or set()) if 0 <= int(d) < rs.num_days}
        for n, days in _raw_active_days.items()
    }
    fixed_wanted_cells = set(getattr(rs, "_constraint_impact_fixed_wanted_cells", set()) or set())
    fixed_type_by_cell = dict(getattr(rs, "_constraint_impact_fixed_type_by_cell", {}) or {})
    coverage_exclude_cells = set(getattr(rs, "coverage_exclude_cells", set()) or set())
    vacation_off_cells = set(getattr(rs, "_constraint_impact_vacation_off_cells", set()) or set())
    structural_off_cells = set(getattr(rs, "_constraint_impact_structural_off_cells", set()) or set())
    forced_off_cap_excluded = set(getattr(rs, "_constraint_impact_forced_off_cap_excluded", set()) or set())
    off_exception_cells = set(getattr(rs.config, "off_exception_cells", []) or [])
    off_exception_vacation_cells = set(getattr(rs.config, "off_exception_vacation_cells", []) or [])
    weekend_days = {int(d) for d in (getattr(rs, "_constraint_impact_weekend_days", set()) or set()) if 0 <= int(d) < rs.num_days}
    nurse_facts = _collect_nurse_facts(rs, join, leave)
    attempt_raw = deepcopy(getattr(rs, "_constraint_impact_attempt_meta", {}) or {})
    attempt = SolveAttemptMeta(
        attempt_index=int(attempt_raw.get("attempt_index", 0)),
        label=attempt_raw.get("label", "primary"),
        grade_strategy=str(getattr(rs, "grade_strategy", "COMBINED") or "COMBINED"),
        forced_grade_soft_fallback=bool(attempt_raw.get("forced_grade_soft_fallback", False)),
        config_flags=attempt_raw.get("config_flags", {}),
    )
    preflight_msgs = list(getattr(rs, "_constraint_impact_preflight_alerts", []) or [])
    mid_error = getattr(rs, "_constraint_impact_mid_feasibility_error", None)
    snapshot = SemanticsSnapshot(
        year=snap_year,
        month=snap_month,
        attempt=attempt,
        shift_types=list(rs.config.shift_types),
        config_payload=_config_payload(rs),
        nurse_ids_in_scope=[nf.nurse_id for nf in nurse_facts],
        inbound_nurse_ids=[nf.nurse_id for nf in nurse_facts if nf.is_inbound],
        nurses=nurse_facts,
        assignment_windows=_collect_assignment_window_facts(rs, active_days_by_nurse),
        carryover_artifacts=_collect_carryover_artifacts(rs),
        fixed_cells=_collect_fixed_cells(snap_year, snap_month, rs, fixed_type_by_cell, fixed_wanted_cells, coverage_exclude_cells),
        special_fixed_requests=deepcopy(getattr(rs, "_constraint_impact_special_fixed_requests", []) or []),
        merged_initial_constraints=deepcopy(getattr(rs, "_constraint_impact_merged_initial_constraints", {}) or {}),
        join=join,
        leave=leave,
        active_days_by_nurse=active_days_by_nurse,
        blocked_by_nurse=blocked_by_nurse,
        fixed_wanted_cells=fixed_wanted_cells,
        fixed_type_by_cell=fixed_type_by_cell,
        coverage_exclude_cells=coverage_exclude_cells,
        vacation_off_cells=vacation_off_cells,
        structural_off_cells=structural_off_cells,
        forced_off_cap_excluded=forced_off_cap_excluded,
        off_exception_cells=off_exception_cells,
        off_exception_vacation_cells=off_exception_vacation_cells,
        weekend_days=weekend_days,
        n_forbid_n=set(getattr(rs, "_constraint_impact_n_forbid_n", set()) or set()),
        preceptee_facts=_collect_preceptee_facts(rs, active_days_by_nurse, coverage_exclude_cells),
        preflight_alerts=_wrap_preflight_alerts(preflight_msgs, mid_error),
        mid_feasibility_error=mid_error,
        constraint_modes=[],
    )
    snapshot.constraint_modes = _base_constraint_modes(rs, snapshot)
    return snapshot


def build_semantics_snapshot_from_active_path(*, roster_system, year: int | None = None, month: int | None = None) -> SemanticsSnapshot:
    return build_semantics_snapshot_from_roster_system(roster_system, year=year, month=month)
