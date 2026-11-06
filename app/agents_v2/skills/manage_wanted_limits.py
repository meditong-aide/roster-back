"""manage-wanted-limits skill — 원티드 일자별 신청제한 설정/조회/정리.

operation:
  set_daily_limit   — 특정일 원티드 신청 최대 개수 설정 (preview/apply). 예 '8월 15일 휴무 3명까지'
  list_over_limit   — wanted_max_requests 초과 제출한 간호사 목록 (read-only)
  delete_excess_off — 특정 간호사의 초과분 OFF 요청 삭제 (preview/apply)

정책:
  - HN/ADM only (mutation 은 middleware._MUTATION_SKILLS).
  - set_daily_limit/delete_excess_off 는 preview→apply.
  - 사용자에게 internal nurse_id 노출 금지. 이름으로 말하기.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from agents_v2.grounding.internal import resolve_date
from agents_v2.skills.registry import register
from services.wanted_service import (
    delete_excess_off_requests,
    get_over_limit_nurses,
    get_wanted_config,
    upsert_wanted_config,
)


def _iso_date(value: Any, year: int, month: int) -> str | None:
    if not value:
        return None
    s = str(value)
    if len(s) >= 10 and s[4] == "-" and s[7] == "-":
        return s[:10]
    return resolve_date(s, year, month)


def _set_daily_limit(db: Session, group_id: str, year: int, month: int, params: dict) -> Any:
    """특정일 원티드 신청 최대 개수 설정. shift_type=None 이면 그 날 전체 신청 제한.

    upsert_wanted_config 는 '월 replace' 라, 현재 월 설정 전부 읽어 merge 후 통째로 보낸다
    (단일 날짜만 보내면 나머지 날짜 제한이 삭제됨).
    """
    iso = _iso_date(params.get("target_date"), year, month)
    if not iso:
        return {"needs_clarification": True,
                "question": "어느 날짜의 원티드 신청 제한을 설정할까요? (예: 8월 15일)", "options": []}
    max_requests = params.get("max_requests")
    if max_requests is None:
        return {"needs_clarification": True,
                "question": f"{iso} 원티드 신청을 최대 몇 개까지 받을까요?", "options": []}
    max_requests = int(max_requests)
    shift_type = params.get("shift_type")  # None = 그 날 전체
    type_label = shift_type or "전체"

    # 현재 월 설정 읽어 dict 리스트로.
    cur = [{"target_date": str(r.target_date), "shift_type": r.shift_type,
            "max_requests": r.max_requests} for r in get_wanted_config(db, group_id, {"year": year, "month": month})]
    old = next((c["max_requests"] for c in cur
                if c["target_date"] == iso and c["shift_type"] == shift_type), None)

    if params.get("preview_only", True):
        return {"preview": True, "operation": "set_daily_limit",
                "summary": {"target_date": iso, "shift_type": type_label, "from": old, "to": max_requests},
                "message": (f"{iso} {type_label} 원티드 신청 제한을 "
                            + (f"{old} → " if old is not None else "")
                            + f"{max_requests}개로 설정합니다. 진행할까요?")}

    # merge: 기존 (date, shift_type) 갱신 or 신규 추가 → 월 전체 통째로 upsert.
    for c in cur:
        if c["target_date"] == iso and c["shift_type"] == shift_type:
            c["max_requests"] = max_requests
            break
    else:
        cur.append({"target_date": iso, "shift_type": shift_type,
                    "max_requests": max_requests, "year": year, "month": month})
    upsert_wanted_config(db, group_id, cur, year=year, month=month)
    return {"ok": True, "operation": "set_daily_limit", "year": year, "month": month,
            "applied": {"target_date": iso, "shift_type": type_label, "max_requests": max_requests},
            "message": f"{iso} {type_label} 원티드 신청을 최대 {max_requests}개로 설정했어요."}


def _summarize_over_limit_message(items: list[dict], year: int, month: int) -> str:
    if not items:
        return f"{year}년 {month}월에 원티드 한도를 넘긴 간호사는 없어요."
    names = [str(i.get("nurse_name") or i.get("name") or i.get("nurse_id")) for i in items]
    head = ", ".join(names[:3])
    extra = f" 외 {len(items) - 3}명" if len(items) > 3 else ""
    return (
        f"{year}년 {month}월 원티드 한도를 넘긴 간호사가 {len(items)}명 있어요: {head}{extra}."
    )


@register("manage-wanted-limits")
def manage_wanted_limits(db: Session, params: dict) -> Any:
    """원티드 한도 관리.

    params:
      operation: list_over_limit | delete_excess_off (필수)
      group_id (필수, RBAC)
      year, month (필수)
      nurse_id (delete_excess_off 필수)
      nurse_name (delete_excess_off — UI 용 라벨, 없으면 nurse_id)
      preview_only (delete_excess_off): 기본 True
    """
    op = (params.get("operation") or "").lower()
    group_id = params.get("group_id")
    year = params.get("year")
    month = params.get("month")

    if not group_id:
        return {"error": "group_id required (RBAC scope)"}
    if year is None or month is None:
        return {"error": "year and month required"}
    if op not in ("set_daily_limit", "list_over_limit", "delete_excess_off"):
        return {
            "needs_clarification": True,
            "question": "원티드 신청제한을 어떻게 처리할까요?",
            "options": [
                "특정일 신청 제한 설정 (set_daily_limit)",
                "한도 초과자 목록 보기 (list_over_limit)",
                "특정 간호사 초과분 정리 (delete_excess_off)",
            ],
        }

    if op == "set_daily_limit":
        return _set_daily_limit(db, group_id, int(year), int(month), params)

    if op == "list_over_limit":
        items = get_over_limit_nurses(db, int(year), int(month), group_id)
        return {
            "operation": "list_over_limit",
            "year": int(year),
            "month": int(month),
            "count": len(items),
            "items": items,
            "message": _summarize_over_limit_message(items, int(year), int(month)),
        }

    # op == "delete_excess_off"
    nurse_id = params.get("nurse_id")
    if not nurse_id:
        return {
            "needs_clarification": True,
            "question": "어떤 간호사의 초과분을 정리할까요? nurse_id 를 알려주세요.",
            "options": [],
        }
    label = params.get("nurse_name") or nurse_id
    preview_only = params.get("preview_only", True)

    if preview_only:
        # 실제 삭제 없이 현재 한도 초과 여부만 안내.
        items = get_over_limit_nurses(db, int(year), int(month), group_id)
        target = next(
            (i for i in items if i.get("nurse_id") == nurse_id), None
        )
        if not target:
            return {
                "preview": True,
                "operation": "delete_excess_off",
                "applied": False,
                "message": (
                    f"{label} 간호사는 {year}년 {month}월 원티드 한도를 초과하지 않았어요. 정리할 것이 없습니다."
                ),
                "_internal": {"nurse_id": nurse_id},
            }
        excess = target.get("excess_count") or target.get("excess") or 0
        return {
            "preview": True,
            "operation": "delete_excess_off",
            "year": int(year),
            "month": int(month),
            "target": {"nurse_name": label, "excess_count": excess},
            "message": (
                f"{label} 간호사의 {year}년 {month}월 원티드 초과분"
                + (f" ({excess}건)" if excess else "")
                + " 을 정리합니다. 진행할까요?"
            ),
            "_internal": {"nurse_id": nurse_id},
        }

    result = delete_excess_off_requests(db, nurse_id, int(year), int(month))
    deleted = result.get("deleted_count") or result.get("count") or 0
    return {
        "preview": False,
        "applied": True,
        "operation": "delete_excess_off",
        "year": int(year),
        "month": int(month),
        "deleted_count": deleted,
        "message": (
            f"{label} 간호사의 {year}년 {month}월 원티드 초과분 {deleted}건을 정리했어요."
            if deleted
            else f"{label} 간호사의 정리할 초과분이 없었어요."
        ),
        "_internal": {"nurse_id": nurse_id, "raw": result},
    }
