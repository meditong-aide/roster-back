"""Agent-facing constraint module/instance read API.

This router exposes the CIG control-layer "search" surface so that an LLM
agent (or UI) can list which constraint modules are active for the current
group and inspect their effective mode + supported adjustment actions.

POST adjustments themselves go through `/roster_create/generate` via
`RosterRequest.constraint_adjustments` — see docs/CONSTRAINT_AGENT_CONTROL_DRAFT.md.
"""

from __future__ import annotations

import calendar
from dataclasses import asdict
from datetime import date
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

from db.client2 import get_db
from db.models import RosterConfig, Team
from routers.auth import get_current_user_from_cookie, require_current_user
from services.group_access import resolve_home_group_id
from schemas.auth_schema import User as UserSchema
from services.constraint_impact.control import (
    find_constraint_instances,
    list_constraint_modules,
)
from services.constraint_impact.snapshot import (
    NurseFact,
    SemanticsSnapshot,
    SolveAttemptMeta,
)


router = APIRouter(tags=["constraint_impact"])


def _fetch_latest_config_for_group(db: Session, group_id: str) -> RosterConfig | None:
    return (
        db.query(RosterConfig)
        .filter(RosterConfig.group_id == group_id)
        .order_by(RosterConfig.created_at.desc())
        .first()
    )


def _fetch_team_min_by_team(db: Session, office_id: str, group_id: str) -> dict[str, dict[str, int]]:
    teams = (
        db.query(Team)
        .filter(Team.office_id == office_id, Team.group_id == group_id, Team.active == 1)
        .all()
    )
    out: dict[str, dict[str, int]] = {}
    for t in teams:
        ms = t.min_shift if isinstance(t.min_shift, dict) else None
        if not ms:
            continue
        cleaned: dict[str, int] = {}
        for k, v in ms.items():
            if k not in ("D", "E", "N", "M"):
                continue
            try:
                iv = int(v)
            except (TypeError, ValueError):
                continue
            if iv > 0:
                cleaned[k] = iv
        if cleaned:
            out[str(t.team_id)] = cleaned
    return out


def _build_listing_snapshot(
    db: Session,
    office_id: str,
    group_id: str,
    year: int,
    month: int,
) -> SemanticsSnapshot:
    """Light snapshot for the read endpoints — no solver, no atoms.

    Populates only `config_payload` + minimal nurse list + `active_days_by_nurse`
    so that `list_constraint_modules` / `find_constraint_instances` produce
    meaningful estimates.
    """
    latest = _fetch_latest_config_for_group(db, group_id)
    config_dict: dict[str, Any] = dict(latest.__dict__) if latest is not None else {}
    config_dict.pop("_sa_instance_state", None)
    team_min_by_team = _fetch_team_min_by_team(db, office_id, group_id)
    if team_min_by_team:
        config_dict["team_min_by_team"] = team_min_by_team
    # Lazy import to avoid circular load
    from services.roster_create_service import _fetch_grade_config_dict
    try:
        gc = _fetch_grade_config_dict(db, office_id, group_id)
        if gc:
            config_dict["grade_config"] = gc
    except Exception:
        pass

    days_in_month = calendar.monthrange(int(year), int(month))[1]

    # Pull active nurses for this group (minimal NurseFact)
    from db.models import Nurse

    nurse_rows = (
        db.query(Nurse)
        .filter(Nurse.group_id == group_id, Nurse.active == 1)
        .all()
    )
    nurses: list[NurseFact] = []
    active_days_by_nurse: dict[int, set[int]] = {}
    nurse_ids: list[str] = []
    for idx, n in enumerate(nurse_rows):
        nurse_id = str(getattr(n, "nurse_id", "") or "")
        if not nurse_id:
            continue
        nurses.append(
            NurseFact(
                nurse_index=idx,
                nurse_id=nurse_id,
                name=str(getattr(n, "name", "") or ""),
                team_id=str(getattr(n, "team_id", "") or "") or None,
                grade=int(getattr(n, "grade", 0) or 0) or None,
                is_weekend_off=bool(getattr(n, "is_weekend_off", 0) or False),
                allowed_shift_codes=set(),
                preceptor_id=None,
                is_inbound=False,
                join_day=0,
                leave_day=days_in_month - 1,
            )
        )
        active_days_by_nurse[idx] = set(range(days_in_month))
        nurse_ids.append(nurse_id)

    snapshot = SemanticsSnapshot(
        year=int(year),
        month=int(month),
        attempt=SolveAttemptMeta(
            attempt_index=0, label="primary", grade_strategy="BASE",
            forced_grade_soft_fallback=False, config_flags={},
        ),
        shift_types=["D", "E", "N", "O", "M"],
        config_payload=config_dict,
        nurse_ids_in_scope=nurse_ids,
        inbound_nurse_ids=[],
        nurses=nurses,
        assignment_windows=[],
        carryover_artifacts=[],
        fixed_cells=[],
        special_fixed_requests=[],
        merged_initial_constraints={},
        join=[0] * len(nurses),
        leave=[days_in_month - 1] * len(nurses),
        active_days_by_nurse=active_days_by_nurse,
        blocked_by_nurse={i: set() for i in range(len(nurses))},
        fixed_wanted_cells=set(),
        fixed_type_by_cell={},
        coverage_exclude_cells=set(),
        vacation_off_cells=set(),
        structural_off_cells=set(),
        forced_off_cap_excluded=set(),
        off_exception_cells=set(),
        off_exception_vacation_cells=set(),
        weekend_days={
            d for d in range(days_in_month)
            if date(int(year), int(month), d + 1).weekday() >= 5
        },
        n_forbid_n=set(),
        preceptee_facts=[],
        preflight_alerts=[],
        mid_feasibility_error=None,
        constraint_modes=[],
    )
    return snapshot


@router.get("/constraint_impact/modules")
def get_modules(
    year: int = Query(..., ge=2000, le=3000),
    month: int = Query(..., ge=1, le=12),
    current_user: UserSchema = Depends(require_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """List ontology constraint modules with effective mode + supported actions."""
    try:
        snapshot = _build_listing_snapshot(
            db, current_user.office_id, resolve_home_group_id(db, current_user), year, month
        )
        modules = list_constraint_modules(snapshot)
        grouped: dict[str, list[dict[str, Any]]] = {}
        for m in modules:
            grouped.setdefault(m.parent or "Other", []).append(asdict(m))
        return {
            "year": year,
            "month": month,
            "group_id": resolve_home_group_id(db, current_user),
            "module_count": len(modules),
            "modules": [asdict(m) for m in modules],
            "grouped_by_parent": grouped,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"list modules failed: {e}")


class _AdjustmentPreviewBody(BaseModel):
    year: int
    month: int
    constraint_adjustments: list[dict[str, Any]]


@router.post("/constraint_impact/preview_adjustments")
def preview_adjustments(
    body: _AdjustmentPreviewBody,
    current_user: UserSchema = Depends(require_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Dry-run apply adjustments and return the post-apply config diff.

    Useful for an agent to inspect what its proposed adjustments would
    actually change in the solver input config, without running CP-SAT.
    """
    try:
        from services.constraint_impact.control import apply_adjustments_to_config

        snapshot = _build_listing_snapshot(
            db, current_user.office_id, resolve_home_group_id(db, current_user), body.year, body.month
        )
        before = dict(snapshot.config_payload)
        after = apply_adjustments_to_config(before, body.constraint_adjustments)
        diff = {}
        watched = (
            "team_min_by_team", "team_min_soft_fallback", "team_handoff_soft_fallback",
            "ban_n_to_d", "ban_e_to_d", "ban_n_to_e",
            "max_consecutive_work_days", "max_consecutive_nights",
            "two_offs_after_two_nig", "two_offs_after_three_nig",
            "_force_grade_max_soft_fallback", "_force_grade_min_soft_fallback",
            "grade_config",
        )
        for k in watched:
            if before.get(k) != after.get(k):
                diff[k] = {"before": before.get(k), "after": after.get(k)}
        return {
            "applied": after.get("_applied_constraint_adjustments") or [],
            "diff": diff,
            "diff_count": len(diff),
        }
    except ValueError as ve:
        raise HTTPException(status_code=400, detail=str(ve))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"preview failed: {e}")


@router.get("/constraint_impact/instances")
def get_instances(
    year: int = Query(..., ge=2000, le=3000),
    month: int = Query(..., ge=1, le=12),
    family: str | None = Query(default=None),
    team: str | None = Query(default=None),
    day: int | None = Query(default=None),
    shift: str | None = Query(default=None),
    grade: int | None = Query(default=None),
    current_user: UserSchema = Depends(require_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Search constraint instances by family + scope filter."""
    try:
        snapshot = _build_listing_snapshot(
            db, current_user.office_id, resolve_home_group_id(db, current_user), year, month
        )
        scope_filter: dict[str, Any] = {}
        if team is not None:
            scope_filter["team"] = team
        if day is not None:
            scope_filter["day"] = day
        if shift is not None:
            scope_filter["shift"] = shift
        if grade is not None:
            scope_filter["grade"] = grade
        instances = find_constraint_instances(
            snapshot, family=family, scope_filter=scope_filter or None
        )
        return {
            "year": year,
            "month": month,
            "family": family,
            "scope_filter": scope_filter,
            "instance_count": len(instances),
            "instances": [asdict(i) for i in instances],
        }
    except ValueError as ve:
        raise HTTPException(status_code=400, detail=str(ve))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"find instances failed: {e}")
