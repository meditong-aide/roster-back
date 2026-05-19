"""update-constraint skill — modify scheduling constraints (roster config, shift manage)."""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from agents_v2.skills.registry import register
from agents_v2.tools import constraint_tools


# QA §B1 정책 — 시스템에서 고정 관리하는 정책 field. 변경 시도 시 명시적 거절.
# 운영 정책 위반 시 critical 회귀 (의료 도메인 운영 사고).
_POLICY_LOCKED_FIELDS: dict[str, dict[str, str]] = {
    "max_nig_per_month": {
        "label_ko": "야간 최대 횟수",
        "alternative": "개인별 한도가 필요하시면 `update_monthly_limit` 으로 간호사별 N 한도를 설정할 수 있습니다.",
    },
}


def _reject_policy_locked(updates: dict) -> dict | None:
    """updates 에 정책-고정 field 가 포함되면 즉시 거절 응답 반환. 없으면 None."""
    locked = [f for f in updates if f in _POLICY_LOCKED_FIELDS]
    if not locked:
        return None
    field = locked[0]
    meta = _POLICY_LOCKED_FIELDS[field]
    return {
        "error": "policy_locked",
        "field": field,
        "field_label": meta["label_ko"],
        "message": (
            f"현재 정책상 '{meta['label_ko']}'({field}) 는 "
            f"시스템에서 고정 관리되므로 변경할 수 없습니다."
        ),
        "alternative": meta["alternative"],
    }


@register("update-constraint")
def update_constraint(db: Session, params: dict) -> Any:
    """Update scheduling constraint configuration."""
    group_id = params["group_id"]
    mutation = params.get("mutation", {})
    preview_only = params.get("preview_only", False)

    # Support both nested (mutation.target_field) and flat (field/value) params
    target_field = (
        params.get("field")
        or mutation.get("target_field", "")
    )
    target_value = (
        params.get("value")
        if params.get("value") is not None
        else mutation.get("target_value")
    )

    # Check if this is a shift_manage update (manpower)
    if target_field in ("manpower",) or params.get("nurse_class"):
        return _update_shift_manage(db, group_id, params, mutation, preview_only)

    # Otherwise, update roster config
    updates = {}
    if target_field and target_value is not None:
        updates[target_field] = target_value
    else:
        # Check for direct updates dict
        updates = params.get("updates", {})

    if not updates:
        # Read-only: return current config
        return constraint_tools.get_roster_config(db, group_id)

    # QA §B1 — 정책-고정 field 변경 차단 (preview/apply 모두 거절)
    rejection = _reject_policy_locked(updates)
    if rejection is not None:
        return rejection

    return constraint_tools.update_roster_config(
        db, group_id, updates, preview_only=preview_only,
    )


def _update_shift_manage(db, group_id, params, mutation, preview_only):
    nurse_class = params.get("nurse_class", "RN")
    shift_slot = params.get("shift_slot", 1)
    new_manpower = mutation.get("target_value")

    if new_manpower is None:
        # Read-only
        return constraint_tools.get_shift_manage(db, group_id)

    return constraint_tools.update_shift_manage_manpower(
        db, group_id, nurse_class, shift_slot, new_manpower,
        preview_only=preview_only,
    )
