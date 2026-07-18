"""bulk-mutation skill — batch modifications on wanted adjustments, schedule entries, etc."""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from agents_v2.skills.registry import register
from agents_v2.tools import schedule_tools, wanted_tools
from agents_v2.tools.nurse_tools import (
    normalize_shift_codes,
    normalize_single_shift_code,
)
from agents_v2.verify import VerifyResult, readback


@register("bulk-mutation")
def bulk_mutation(db: Session, params: dict) -> Any:
    """Perform batch modifications based on scope and mutation params."""
    scope = params.get("scope", "")
    mutation = params.get("mutation", {})
    action = params.get("action") or mutation.get("action", "")
    preview_only = params.get("preview_only", False)

    # B4 (2026-06-01): shift_codes/new_shift_code 정규화 + 모르는 값 즉시 clarification.
    # 기존엔 silent skip → 사용자에겐 "변경 없음" 으로 보이는 dead-end 회귀.
    codes, clar = normalize_shift_codes(params.get("shift_codes"))
    if clar is not None:
        return clar
    if codes is not None:
        params = {**params, "shift_codes": codes}
    new_shift, clar = normalize_single_shift_code(params.get("new_shift_code"))
    if clar is not None:
        return clar
    if new_shift is not None:
        params = {**params, "new_shift_code": new_shift}
    # shift_name (add_shift) 도 단일 코드로 정규화 — _add_wanted_by_date 가 사용.
    sn, clar = normalize_single_shift_code(params.get("shift_name"))
    if clar is not None:
        return clar
    if sn is not None:
        # shift_name 자리에는 정규화된 코드를 넣어 두면 _add_wanted_by_date 가 그대로 통과
        params = {**params, "shift_name": sn}

    if scope == "wanted_adjustment":
        return _mutate_wanted_adjustments(db, params, mutation, preview_only)
    elif scope == "wanted_submissions" and action == "cancel":
        date = params.get("date")
        if date:
            return _delete_wanted_by_date(db, params, preview_only)
        return _cancel_wanted_request(db, params, preview_only)
    elif scope == "wanted_submissions" and action == "update_deadline":
        return _update_wanted_deadline(db, params, preview_only)
    elif scope == "wanted_submissions" and action == "clear_deadline":
        return _clear_wanted_deadline(db, params, preview_only)
    elif scope == "wanted_submissions" and action == "add_shift":
        return _add_wanted_by_date(db, params, preview_only)
    elif scope == "wanted_submissions" and action == "change_shift":
        return _change_wanted_by_date(db, params, preview_only)
    elif scope in ("schedule", "draft_schedule", "published_schedule"):
        return _mutate_schedule_entries(db, params, mutation, preview_only)
    else:
        return {"error": f"Unsupported mutation scope: {scope}"}


def _mutate_wanted_adjustments(db, params, mutation, preview_only):
    group_id = params["group_id"]
    year = params.get("year")
    month = params.get("month")

    # First, get the entries to update
    rows = wanted_tools.get_wanted_adjustments(db, group_id, year, month)

    # Apply filters
    nurse_ids = params.get("nurse_ids")
    if nurse_ids:
        rows = [r for r in rows if r.get("nurse_id") in nurse_ids]

    shift_codes = params.get("shift_codes")
    if shift_codes:
        rows = [r for r in rows if r.get("shift_id") in shift_codes]

    # Apply predicate filter
    predicate = params.get("predicate", {})
    if predicate.get("name") == "off" and predicate.get("arguments", {}).get("shift_ids"):
        off_ids = set(predicate["arguments"]["shift_ids"])
        rows = [r for r in rows if r.get("shift_id") in off_ids]

    entry_ids = [r["entry_id"] for r in rows if "entry_id" in r]

    if not entry_ids:
        return {"affected_count": 0, "message": "No matching entries found"}

    field = mutation.get("target_field", "is_applied")
    value = mutation.get("target_value")

    return wanted_tools.bulk_update_wanted_adjustments(
        db, entry_ids, field, value, group_id=group_id, preview_only=preview_only,
    )


def _cancel_wanted_request(db, params, preview_only=False):
    nurse_ids = params.get("nurse_ids", [])
    year = params.get("year")
    month = params.get("month")
    group_id = params.get("group_id")

    if not nurse_ids:
        return {"error": "nurse_id required for cancel"}
    if not group_id:
        return {"error": "group_id required for cancel"}

    if preview_only:
        return {
            "preview_only": True,
            "action": "cancel_wanted",
            "nurse_ids": nurse_ids,
            "year": year,
            "month": month,
            "affected_count": len(nurse_ids),
        }

    results = []
    for nid in nurse_ids:
        result = wanted_tools.cancel_wanted_request(db, nid, group_id, year, month)
        results.append(result)

    if len(results) == 1:
        return results[0]
    return {"results": results, "affected_count": len(results)}


def _update_wanted_deadline(db, params, preview_only=False):
    group_id = params["group_id"]
    year = params.get("year")
    month = params.get("month")
    new_deadline = params.get("new_deadline")

    if not new_deadline:
        return {"error": "new_deadline (YYYY-MM-DD) required"}

    # Preview: show current + new deadline
    if preview_only:
        current = wanted_tools.get_wanted_status(db, group_id, year, month)
        if not current:
            return {"error": f"No wanted campaign found for {year}/{month}"}
        return {
            "preview_only": True,
            "action": "update_deadline",
            "current_deadline": current.get("exp_date"),
            "new_deadline": new_deadline,
            "year": year,
            "month": month,
        }

    return wanted_tools.update_wanted_deadline(db, group_id, year, month, new_deadline)


def _clear_wanted_deadline(db, params, preview_only=False):
    """원티드 마감일을 NULL 로 해제. 임의 날짜 추측 차단."""
    group_id = params["group_id"]
    year = params.get("year")
    month = params.get("month")

    if preview_only:
        current = wanted_tools.get_wanted_status(db, group_id, year, month)
        if not current:
            return {"error": f"No wanted campaign found for {year}/{month}"}
        return {
            "preview_only": True,
            "action": "clear_deadline",
            "current_deadline": current.get("exp_date"),
            "new_deadline": None,
            "year": year,
            "month": month,
        }

    return wanted_tools.clear_wanted_deadline(db, group_id, year, month)


def _delete_wanted_by_date(db, params, preview_only=False):
    nurse_ids = params.get("nurse_ids", [])
    if not nurse_ids:
        return {"error": "nurse_id required"}
    return wanted_tools.delete_wanted_by_date(
        db,
        nurse_id=nurse_ids[0],
        group_id=params["group_id"],
        year=params.get("year"),
        month=params.get("month"),
        date=params["date"],
        preview_only=preview_only,
    )


def _add_wanted_by_date(db, params, preview_only=False):
    nurse_ids = params.get("nurse_ids", [])
    if not nurse_ids:
        return {"error": "nurse_id required"}
    shift_code = params.get("shift_codes", [None])[0] if params.get("shift_codes") else params.get("shift_name", "")
    if not shift_code:
        return {"error": "shift required for add_shift"}
    return wanted_tools.add_wanted_by_date(
        db,
        nurse_id=nurse_ids[0],
        group_id=params["group_id"],
        year=params.get("year"),
        month=params.get("month"),
        date=params.get("date", ""),
        shift_code=shift_code,
        comment=params.get("comment", ""),
        preview_only=preview_only,
    )


def _change_wanted_by_date(db, params, preview_only=False):
    nurse_ids = params.get("nurse_ids", [])
    if not nurse_ids:
        return {"error": "nurse_id required"}
    new_shift = params.get("new_shift_code") or (
        params.get("shift_codes", [None])[0] if params.get("shift_codes") else None
    )
    if not new_shift:
        return {"error": "new_shift required for change_shift"}
    return wanted_tools.modify_wanted_by_date(
        db,
        nurse_id=nurse_ids[0],
        group_id=params["group_id"],
        year=params.get("year"),
        month=params.get("month"),
        date=params.get("date", ""),
        new_shift_code=new_shift,
        comment=params.get("comment"),
        preview_only=preview_only,
    )


def _mutate_schedule_entries(db, params, mutation, preview_only):
    group_id = params["group_id"]
    year = params.get("year")
    month = params.get("month")
    nurse_ids = params.get("nurse_ids")
    date = params.get("date")
    # LLM sends flat params (new_shift_code via grounding), tests use nested mutation
    new_shift = (
        params.get("new_shift_code")
        or mutation.get("target_value")
    )

    # Resolve schedule
    schedule_id = params.get("schedule_id")
    if not schedule_id:
        meta = schedule_tools.resolve_target_schedule(db, group_id, year, month)
        if not meta:
            return {"error": f"No schedule found for {year}/{month}"}
        schedule_id = meta["schedule_id"]

    # Single entry update
    if nurse_ids and len(nurse_ids) == 1 and date and new_shift:
        entry = schedule_tools.find_schedule_entry(db, schedule_id, nurse_ids[0], date, group_id)
        if not entry:
            return {"error": f"No entry found for nurse {nurse_ids[0]} on {date}"}
        if preview_only:
            return {
                "preview": True,
                "entry": entry,
                "new_shift_id": new_shift,
            }
        return schedule_tools.update_schedule_entry(db, entry["entry_id"], new_shift, group_id)

    # Bulk: get all matching entries and update
    entries = schedule_tools.get_schedule_entries(
        db, schedule_id,
        nurse_ids=nurse_ids or None,
        date_str=date,
    )

    shift_codes = params.get("shift_codes")
    if shift_codes:
        entries = [e for e in entries if e.get("shift_id") in shift_codes]

    if preview_only:
        return {
            "preview": True,
            "affected_count": len(entries),
            "entries": entries[:20],
        }

    if not new_shift:
        return {"error": "target shift value required for bulk update"}

    results = []
    for e in entries:
        result = schedule_tools.update_schedule_entry(db, e["entry_id"], new_shift, group_id)
        results.append(result)

    return {"affected_count": len(results), "results": results}


# ── L1 read-back 검증 (근무표 셀 변경) ────────────────────────
# 스킬이 '셀을 new_shift 로 바꿨다'(new_shift_id 주장)고 보고하면, 같은 스케줄 resolve 경로로
# 셀을 되읽어 shift_id 가 실제로 그 값인지 대조. '조용한 거짓완료'(보고했으나 미반영) 차단.
# 정합성 최우선 설계:
#   - 스킬 자신의 주장(결과의 new_shift_id)을 진실값과 대조 → 값 재해석 없음(불일치 원천 제거).
#   - 스킬과 동일한 resolve_target_schedule + find_schedule_entry 경로로 되읽음.
#   - false-fail-safe: 되읽기 성공했는데 값이 다를 때만 실패. 못 읽으면(애매) 통과.
# 원티드/deadline 스코프는 방향·필드가 달라 1차 범위 밖(통과).
def _bool_or_none(v: Any) -> bool | None:
    """승인/거부 값 해석. bool/int/명확한 문자열만, 애매하면 None(검증 스킵)."""
    if isinstance(v, bool):
        return v
    if isinstance(v, int):
        return bool(v)
    if isinstance(v, str):
        s = v.strip().lower()
        if s in ("true", "1", "yes", "y", "t"):
            return True
        if s in ("false", "0", "no", "n", "f"):
            return False
    return None


@readback("bulk_mutation")
def _verify_bulk_mutation(db: Session, params: dict, result: Any) -> VerifyResult:
    scope = params.get("scope", "")
    action = params.get("action") or (params.get("mutation") or {}).get("action", "")
    if scope == "wanted_adjustment":
        return _verify_wanted_adjustments(db, params, result)
    if scope == "wanted_submissions" and action == "cancel":
        return _verify_wanted_cancel(db, params, result)
    if scope in ("schedule", "draft_schedule", "published_schedule"):
        return _verify_schedule_cells(db, params, result)
    return VerifyResult(True)


def _verify_wanted_cancel(db: Session, params: dict, result: Any) -> VerifyResult:
    # 원티드 취소(retract) = WantedRequest.is_submitted → False. 부재 확인:
    # 결과의 request_id 를 되읽어 여전히 제출상태(is_submitted=True)면 취소 미반영.
    items = result.get("results") if isinstance(result.get("results"), list) else [result]
    from db.models import WantedRequest
    for it in items:
        if not isinstance(it, dict) or "error" in it:
            continue
        rid = it.get("request_id")
        if rid is None:
            continue
        row = (
            db.query(WantedRequest)
            .filter(
                WantedRequest.request_id == rid,
                WantedRequest.group_id == params.get("group_id"),
            )
            .first()
        )
        if row is None:
            continue  # 못 읽으면 애매 → 통과(오탐 방지)
        if bool(getattr(row, "is_submitted", False)):  # 여전히 제출됨 → 취소 미반영
            return VerifyResult(
                False, f"원티드 취소가 반영되지 않았습니다 (request={rid}, 여전히 제출됨)."
            )
    return VerifyResult(True)


def _verify_wanted_adjustments(db: Session, params: dict, result: Any) -> VerifyResult:
    # 원티드 일괄 승인/거부(is_applied)만. 실제 적용(preview 아님)만 검증.
    # 결과의 entry_ids(실제 적용된 id)를 되읽어 is_applied 가 요청값인지 대조.
    if result.get("preview"):
        return VerifyResult(True)
    entry_ids = result.get("entry_ids")
    if not entry_ids:
        return VerifyResult(True)
    mutation = params.get("mutation") or {}
    if mutation.get("target_field", "is_applied") != "is_applied":
        return VerifyResult(True)  # 1차: 승인/거부만
    want = _bool_or_none(mutation.get("target_value"))
    if want is None:
        return VerifyResult(True)  # 값 해석 불가 → 스킵(오탐 방지)

    from db.models import FixedWantedEntry
    rows = (
        db.query(FixedWantedEntry)
        .filter(
            FixedWantedEntry.id.in_(entry_ids),
            FixedWantedEntry.group_id == params.get("group_id"),
        )
        .all()
    )
    for row in rows:
        # bool 비교(강제변환 안전) — 요청한 승인/거부 상태와 실제가 다르면 미반영.
        if bool(getattr(row, "is_applied", None)) != want:
            return VerifyResult(
                False,
                f"원티드 {'승인' if want else '거부'}이 반영되지 않았습니다 (entry={row.id}).",
            )
    return VerifyResult(True)


def _verify_schedule_cells(db: Session, params: dict, result: Any) -> VerifyResult:
    # 적용 결과 아이템(단일 평탄화 / 다중 results). 셀 변경을 '주장'한 것만.
    items = result.get("results") if isinstance(result.get("results"), list) else [result]
    claims = [
        it for it in items
        if isinstance(it, dict) and it.get("entry_id")
        and it.get("new_shift_id") is not None and "error" not in it
    ]
    if not claims:
        return VerifyResult(True)

    # 스킬과 동일하게 schedule_id 재해석(못 찾으면 애초에 실행됐을 리 없음 → 통과).
    group_id = params.get("group_id")
    schedule_id = params.get("schedule_id")
    if not schedule_id:
        meta = schedule_tools.resolve_target_schedule(
            db, group_id, params.get("year"), params.get("month")
        )
        if not meta:
            return VerifyResult(True)
        schedule_id = meta["schedule_id"]

    for it in claims:
        nid, wdate, want = it.get("nurse_id"), it.get("work_date"), it.get("new_shift_id")
        if not (nid and wdate):
            continue
        fresh = schedule_tools.find_schedule_entry(db, schedule_id, nid, wdate, group_id)
        if not isinstance(fresh, dict) or "error" in fresh:
            continue  # 못 읽으면 애매 → 통과(오탐 방지)
        if fresh.get("shift_id") != want:
            return VerifyResult(
                False,
                f"근무표 변경이 반영되지 않았습니다 "
                f"(nurse={nid}, {wdate}: 기대={want}, 실제={fresh.get('shift_id')}).",
            )
    return VerifyResult(True)
