"""Constraint / config tools — read and update roster generation settings."""

from __future__ import annotations

from sqlalchemy.orm import Session

from db.models import RosterConfig, RosterGradeConfig, ShiftManage


# 에이전트가 update-constraint 로 설정 가능한 RosterConfig 필드 화이트리스트.
# ★스코프/식별/타임스탬프 컬럼(config_id/office_id/group_id/version/created_at/updated_at)은
#   의도적으로 제외 — 에이전트/온톨로지가 프리셋 정체성·테넌트 스코프를 손상시키지 못하게.
# ★신규 정책 필드는 여기에 명시적으로 추가해야만 설정 가능(fail-safe: 무가드 자유 setattr 방지).
# 하드락 토글(banned_day_after_eve/three_seq_nig/not_one_night 등)은 HN 정당 설정이라 허용.
_AGENT_SETTABLE_FIELDS = frozenset({
    "day_req", "eve_req", "nig_req", "min_exp_per_shift", "req_exp_nurses",
    "two_offs_per_week", "max_nig_per_month", "three_seq_nig",
    "two_offs_after_three_nig", "two_offs_after_two_nig", "banned_day_after_eve",
    "max_conseq_work", "off_days", "sequential_offs",
    "nod_noe", "not_one_night", "use_mid", "preceptee_on", "preceptee_shift_count",
    "weekly_off_group", "fixed_wanted_use_yn",
    "show_level", "show_preceptor", "off_first", "off_swap_enabled",
    "config_name", "config_memo",
})
# ★제외(상수-live): shift_priority(prod 전량 0.8)·ban_night_before_fixed_off(prod 전량 True·
#   FE 미노출·constraint_impact probe 전용 레버) — 운영에서 아무도 안 바꾸는 솔버값이라
#   에이전트가 건드릴 이유가 없어 화이트리스트에서 제외(값 고정 유지). 컬럼 DROP은 별건(DDL/FE/probe).
# 숫자 필드 sane 범위(비정상값·타입오류 차단). 정책(하드락)이 아니라 데이터 무결성용.
_NUMERIC_BOUNDS: dict[str, tuple[float, float]] = {
    "day_req": (0, 50), "eve_req": (0, 50), "nig_req": (0, 50),
    "min_exp_per_shift": (0, 50), "req_exp_nurses": (0, 50),
    "max_nig_per_month": (0, 31), "max_conseq_work": (1, 7), "off_days": (0, 31),
}


def _validate_config_field(field: str, new_val) -> str | None:
    """에이전트 config 쓰기 검증. 통과 시 None, 실패 시 에러 메시지."""
    if field not in _AGENT_SETTABLE_FIELDS:
        return f"'{field}' 은(는) 에이전트로 설정할 수 없는 필드입니다(식별/스코프 컬럼 등)."
    bounds = _NUMERIC_BOUNDS.get(field)
    if bounds is not None and new_val is not None:
        try:
            nv = float(new_val)
        except (TypeError, ValueError):
            return f"'{field}' 은(는) 숫자여야 합니다: {new_val!r}"
        lo, hi = bounds
        if nv < lo or nv > hi:
            return f"'{field}' 값이 허용범위 [{lo}, {hi}] 를 벗어났습니다: {new_val}"
    return None


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
        err = _validate_config_field(field, new_val)
        if err is not None:
            return {"error": err, "allowed": sorted(_AGENT_SETTABLE_FIELDS)}
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
        "sequential_offs": bool(row.sequential_offs) if row.sequential_offs is not None else None,
        "nod_noe": bool(row.nod_noe) if row.nod_noe is not None else None,
        "not_one_night": bool(row.not_one_night),
        "use_mid": bool(row.use_mid),
        "preceptee_on": bool(row.preceptee_on),
        "preceptee_shift_count": bool(row.preceptee_shift_count),
        "weekly_off_group": bool(row.weekly_off_group) if row.weekly_off_group is not None else None,
        "fixed_wanted_use_yn": bool(row.fixed_wanted_use_yn),
        "show_level": bool(row.show_level),
        "show_preceptor": bool(row.show_preceptor),
    }
