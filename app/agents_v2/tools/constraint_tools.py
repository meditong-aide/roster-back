"""Constraint / config tools — read and update roster generation settings."""

from __future__ import annotations

from sqlalchemy.orm import Session

from db.models import RosterConfig, RosterGradeConfig, ShiftManage


def get_roster_config(db: Session, group_id: str) -> dict | None:
    """Get the latest roster config for a group."""
    row = (
        db.query(RosterConfig)
        .filter(RosterConfig.group_id == group_id)
        .order_by(RosterConfig.config_id.desc())
        .first()
    )
    if not row:
        return None
    return _config_dict(row)


def get_roster_config_by_id(db: Session, config_id: int) -> dict | None:
    """Get a specific roster config by ID."""
    row = db.query(RosterConfig).filter(RosterConfig.config_id == config_id).first()
    if not row:
        return None
    return _config_dict(row)


def update_roster_config(
    db: Session,
    group_id: str,
    updates: dict,
    *,
    preview_only: bool = False,
) -> dict:
    """Update roster config fields. Returns old/new values for changed fields."""
    row = (
        db.query(RosterConfig)
        .filter(RosterConfig.group_id == group_id)
        .order_by(RosterConfig.config_id.desc())
        .first()
    )
    if not row:
        return {"error": "RosterConfig not found for group"}

    changes = {}
    for field, new_val in updates.items():
        if not hasattr(row, field):
            return {"error": f"Unknown config field: {field}"}
        old_val = getattr(row, field)
        if old_val != new_val:
            changes[field] = {"old": old_val, "new": new_val}

    if preview_only:
        return {"preview": True, "config_id": row.config_id, "changes": changes}

    for field, delta in changes.items():
        setattr(row, field, delta["new"])
    db.commit()
    db.refresh(row)
    return {"preview": False, "config_id": row.config_id, "changes": changes}


def get_grade_config(db: Session, group_id: str) -> dict | None:
    """Get grade-based constraint config for a group."""
    row = (
        db.query(RosterGradeConfig)
        .filter(RosterGradeConfig.group_id == group_id)
        .first()
    )
    if not row:
        return None
    return {
        "config_id": row.config_id,
        "office_id": row.office_id,
        "group_id": row.group_id,
        "null_grade_policy": row.null_grade_policy,
        "constraints_json": row.constraints_json,
        "use_dynamic_scaling": bool(row.use_dynamic_scaling),
        "allow_soft_fallback": bool(getattr(row, "allow_soft_fallback", False)),
        "updated_at": str(row.updated_at) if row.updated_at else None,
    }


def get_shift_manage(db: Session, group_id: str) -> list[dict]:
    """Get shift slot manpower requirements for a group."""
    rows = (
        db.query(ShiftManage)
        .filter(ShiftManage.group_id == group_id)
        .order_by(ShiftManage.nurse_class, ShiftManage.shift_slot)
        .all()
    )
    return [
        {
            "office_id": r.office_id,
            "group_id": r.group_id,
            "nurse_class": r.nurse_class,
            "shift_slot": r.shift_slot,
            "main_code": r.main_code,
            "codes": r.codes,
            "manpower": r.manpower,
        }
        for r in rows
    ]


def update_shift_manage_manpower(
    db: Session,
    group_id: str,
    nurse_class: str,
    shift_slot: int,
    new_manpower: int,
    *,
    preview_only: bool = False,
) -> dict:
    """Update manpower requirement for a specific shift slot."""
    # 중복행이 남아있을 수 있으므로 매칭되는 모든 행을 갱신한다(.first() 면 1행만 바뀌어
    # 로더의 최대 id 채택값과 stale 불일치 발생). old 값은 로더가 읽는 최대 id 행 기준.
    rows = (
        db.query(ShiftManage)
        .filter(
            ShiftManage.group_id == group_id,
            ShiftManage.nurse_class == nurse_class,
            ShiftManage.shift_slot == shift_slot,
        )
        .order_by(ShiftManage.id.asc())
        .all()
    )
    if not rows:
        return {"error": "ShiftManage entry not found"}
    old_manpower = rows[-1].manpower
    if preview_only:
        return {
            "preview": True,
            "nurse_class": nurse_class,
            "shift_slot": shift_slot,
            "old_manpower": old_manpower,
            "new_manpower": new_manpower,
        }
    for row in rows:
        row.manpower = new_manpower
    db.commit()
    return {
        "preview": False,
        "nurse_class": nurse_class,
        "shift_slot": shift_slot,
        "old_manpower": old_manpower,
        "new_manpower": new_manpower,
    }


# ── private ──────────────────────────────────────────────


def _config_dict(row: RosterConfig) -> dict:
    return {
        "config_id": row.config_id,
        "config_version": row.config_version,
        "office_id": row.office_id,
        "group_id": row.group_id,
        "day_req": row.day_req,
        "eve_req": row.eve_req,
        "nig_req": row.nig_req,
        "min_exp_per_shift": row.min_exp_per_shift,
        "req_exp_nurses": row.req_exp_nurses,
        "two_offs_per_week": bool(row.two_offs_per_week) if row.two_offs_per_week is not None else None,
        "max_nig_per_month": row.max_nig_per_month,
        "three_seq_nig": bool(row.three_seq_nig) if row.three_seq_nig is not None else None,
        "two_offs_after_three_nig": bool(row.two_offs_after_three_nig) if row.two_offs_after_three_nig is not None else None,
        "two_offs_after_two_nig": bool(row.two_offs_after_two_nig) if row.two_offs_after_two_nig is not None else None,
        "banned_day_after_eve": bool(row.banned_day_after_eve) if row.banned_day_after_eve is not None else None,
        "max_conseq_work": row.max_conseq_work,
        "off_days": row.off_days,
        "shift_priority": row.shift_priority,
        "weekend_shift_ratio": row.weekend_shift_ratio,
        "sequential_offs": bool(row.sequential_offs) if row.sequential_offs is not None else None,
        "even_nights": bool(row.even_nights) if row.even_nights is not None else None,
        "nod_noe": bool(row.nod_noe) if row.nod_noe is not None else None,
        "not_one_night": bool(row.not_one_night),
        "use_mid": bool(row.use_mid),
        "preceptor_gauge": row.preceptor_gauge,
        "preceptee_on": bool(row.preceptee_on),
        "preceptee_shift_count": bool(row.preceptee_shift_count),
        "weekly_off_group": bool(row.weekly_off_group) if row.weekly_off_group is not None else None,
        "team_balance_enable": bool(row.team_balance_enable),
        "team_balance_gauge": row.team_balance_gauge,
        "team_balance_mode": row.team_balance_mode,
        "off_placement_mode": row.off_placement_mode,
        "fixed_wanted_use_yn": bool(row.fixed_wanted_use_yn),
        "show_level": bool(row.show_level),
        "show_preceptor": bool(row.show_preceptor),
    }
