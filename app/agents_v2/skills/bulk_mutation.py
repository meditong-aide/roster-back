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

    # source_type 스코프 — "전원 승인"(nurse 필터 없는 is_applied) 시 주휴(weekly_off)·
    # HN 추가분(added)까지 과승인되는 것 방지. per-nurse 필터도 명시 source_types 도 없으면
    # 승인 대상을 **간호사 제출분(original/modified)** 으로 한정. (개별 대상 지정 시엔 그대로.)
    source_types = params.get("source_types")
    is_approve = mutation.get("target_field", "is_applied") == "is_applied"
    if not source_types and not nurse_ids and is_approve:
        source_types = ["original", "modified"]
    if source_types:
        st = set(source_types)
        rows = [r for r in rows if r.get("source_type") in st]

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

    # 마감본은 에이전트가 고치지 않는다(int-0b) — 미리보기에서부터 막아 '미리보기 OK → 승인 실패'를 없앤다.
    # 여기서는 판정만 한다(잠금 없이). 잠금 뒤 재판정은 실제로 쓰는 update_schedule_entry 가 한다 —
    # 여기서 잠그면 쓰지 않고 돌아가는 길에서 잠금이 턴 끝까지 남는다.
    issued = schedule_tools.issued_overwrite_error(db, schedule_id, lock=False)
    if issued:
        return issued

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

    return _update_entries_all_or_nothing(db, entries, new_shift, group_id)


class _CellWriteAbort(Exception):
    def __init__(self, result: dict):
        super().__init__("cell write aborted")
        self.result = result


def _update_entries_all_or_nothing(db, entries: list[dict], new_shift: str, group_id: str) -> dict:
    """여러 칸을 한 번에 반영하거나 하나도 반영하지 않는다(int-0b).

    칸마다 커밋하면 사이사이 근무표 행 잠금이 풀려 그 틈에 화면 마감이 끼어들고, 중간에 멈추면
    앞 칸만 바뀐 채 남았다. 칸 쓰기 함수의 commit 을 flush 로 돌려 SAVEPOINT 안에서 모으고 마지막에
    한 번 커밋한다(첫 칸의 잠금이 끝까지 유지된다). 실패하면 이 묶음만 되감고 트랜잭션을 끝내 잠금을 푼다.
    바깥에 이미 원자 커밋(배치 승인·DAG)이 있으면 그 커밋 규약을 그대로 따른다.
    """
    real_commit = db.commit
    db.commit = db.flush  # type: ignore[method-assign]
    try:
        with db.begin_nested():
            results = []
            for e in entries:
                result = schedule_tools.update_schedule_entry(db, e["entry_id"], new_shift, group_id)
                if isinstance(result, dict) and "error" in result:
                    raise _CellWriteAbort(result)
                results.append(result)
    except _CellWriteAbort as abort:
        db.commit = real_commit  # type: ignore[method-assign]
        db.commit()  # 되감은 뒤 트랜잭션을 끝내 잠금을 푼다(감사 기록 등 앞선 flush 는 함께 확정)
        return {**abort.result, "rolled_back": True, "applied_count": 0}
    finally:
        db.commit = real_commit  # type: ignore[method-assign]
    db.commit()
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
