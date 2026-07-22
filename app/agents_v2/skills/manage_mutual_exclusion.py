"""manage-mutual-exclusion skill — 두 간호사 상호배제(같은 근무 회피) 설정/해제.

set_mutual_exclusion 은 **1:1**(한 간호사당 파트너 1명, 양방향 대칭). 파트너 바꾸면 옛 것 해제.
⚠️ 3명+ 그룹 전원 배타는 백엔드 미지원(그룹 배타 없음) — 페어 단위만.

정책: HN/ADM only. preview→apply. 이름→nurse_id 는 스킬 내부 그라운딩.
"""

from __future__ import annotations

from datetime import date as _date
from typing import Any

from sqlalchemy.orm import Session

from agents_v2.grounding.internal import resolve_nurse
from agents_v2.skills.registry import register
from services.mutual_exclusion_period import set_mutual_exclusion


def _valid_from(params: dict) -> _date:
    y, m = params.get("year"), params.get("month")
    if y and m:
        return _date(int(y), int(m), 1)  # 대상월 1일 발효(월 단위 정책)
    return _date.today()


def _resolve_or_clarify(db, group_id, name):
    """(nurse_id, None) 또는 (None, clarify/error dict)."""
    r = resolve_nurse(db, group_id, name)
    if getattr(r, "resolved", False):
        return r.value, None
    if getattr(r, "needs_clarification", False):
        return None, {"needs_clarification": True, "question": r.question, "options": r.options or []}
    return None, {"error": getattr(r, "error", None) or f"'{name}' 간호사를 찾을 수 없습니다."}


@register("manage-mutual-exclusion")
def manage_mutual_exclusion(db: Session, params: dict) -> Any:
    """상호배제 설정/해제.

    params: operation(set|release), nurse_name, partner_name(set), group_id/office_id/year/month(ctx), preview_only
    """
    op = (params.get("operation") or "set").lower()
    group_id = params.get("group_id")
    office_id = params.get("office_id")
    if not group_id:
        return {"error": "group_id required (RBAC scope)"}

    name = params.get("nurse_name")
    if not name:
        return {"needs_clarification": True, "question": "누구의 상호배제를 설정/해제할까요?", "options": []}
    nurse_id, err = _resolve_or_clarify(db, group_id, name)
    if err:
        return err

    if op == "release":
        if params.get("preview_only", True):
            return {"preview": True, "operation": "release_mutual_exclusion",
                    "summary": {"nurse_name": name},
                    "message": f"{name} 간호사의 상호배제를 해제합니다. 진행할까요?"}
        res = set_mutual_exclusion(db, nurse_id=nurse_id, partner_id=None,
                                   office_id=office_id, valid_from=_valid_from(params))
        return {"ok": True, "operation": "release_mutual_exclusion", "closed": res.get("closed"),
                "applied": {"nurse_name": name},
                "message": f"{name} 간호사의 상호배제를 해제했어요."}

    # op == set — 파트너 필요
    pname = params.get("partner_name")
    if not pname:
        return {"needs_clarification": True, "question": f"{name} 간호사를 누구와 상호배제할까요?", "options": []}
    partner_id, perr = _resolve_or_clarify(db, group_id, pname)
    if perr:
        return perr
    if nurse_id == partner_id:
        return {"error": "같은 간호사끼리는 상호배제할 수 없습니다."}

    if params.get("preview_only", True):
        return {"preview": True, "operation": "set_mutual_exclusion",
                "summary": {"nurse_name": name, "partner_name": pname},
                "message": (f"{name} 간호사와 {pname} 간호사를 상호배제(같은 근무 회피)로 "
                            "설정합니다. 진행할까요?")}
    res = set_mutual_exclusion(db, nurse_id=nurse_id, partner_id=partner_id,
                               office_id=office_id, valid_from=_valid_from(params))
    return {"ok": True, "operation": "set_mutual_exclusion", "opened": res.get("opened"),
            "applied": {"nurse_name": name, "partner_name": pname},
            "message": f"{name} 간호사와 {pname} 간호사를 상호배제로 설정했어요."}
