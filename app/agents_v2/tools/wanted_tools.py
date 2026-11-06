"""Wanted (shift preference request) tools."""

from __future__ import annotations

import functools
import inspect
import logging
from datetime import datetime, date as date_type

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session
from sqlalchemy import desc, func

from db.models import (
    Nurse,
    Shift,
    Wanted,
    WantedRequest,
    NurseShiftRequest,
    NursePairRequest,
    FixedWantedEntry,
    ShiftPreference,
)

logger = logging.getLogger(__name__)


def _wanted_request_month_str(year: int, month: int) -> str:
    """WantedRequest.month is CHAR(7) format 'YYYY-MM'."""
    return f"{year}-{month:02d}"


def _day_in_month(value, year, month) -> tuple[str | None, str | None]:
    """대상 날짜를 'YYYY-MM-DD' 로 맞춘다 → (날짜, 오류 문구). 요청 연월 밖이면 오류.

    ★ 잠금·마감·일자 한도는 요청 연월로 본다 — 다른 달 날짜를 받으면 그 검사를 모두 비켜 가고, 신청 칸에는 월 칸이
      없어 다른 달 칸과 섞인다(11-04 Codex).
    """
    try:
        day = _parse_date(value)
        same_month = isinstance(day, date_type) and (day.year, day.month) == (int(year), int(month))
    except (TypeError, ValueError):
        day, same_month = None, False
    if not same_month:
        return None, f"{value} 은(는) {year}년 {month}월 날짜가 아닙니다."
    return date_type(day.year, day.month, day.day).isoformat(), None


def _month_bounds(month_str: str) -> tuple[date_type, date_type]:
    """'YYYY-MM' → [그 달 1일, 다음 달 1일) — 신청 칸(`nurse_shift_requests`)에는 월 칸이 없어 날짜로 그 달을 고른다."""
    year, month = (int(part) for part in month_str.split("-"))
    start = date_type(year, month, 1)
    return start, (date_type(year + 1, 1, 1) if month == 12 else date_type(year, month + 1, 1))


def get_wanted_status(
    db: Session, group_id: str, year: int, month: int
) -> dict | None:
    """Get the wanted campaign status for a group/month."""
    row = (
        db.query(Wanted)
        .filter(
            Wanted.group_id == group_id,
            Wanted.year == year,
            Wanted.month == month,
        )
        .first()
    )
    if not row:
        return None
    return {
        "group_id": row.group_id,
        "year": row.year,
        "month": row.month,
        "exp_date": str(row.exp_date) if row.exp_date else None,
        "status": row.status,
    }


def get_wanted_submissions(
    db: Session,
    group_id: str,
    year: int,
    month: int,
    *,
    nurse_id: str | None = None,
    submitted_only: bool = True,
    submitted_after: str | None = None,
    submitted_before: str | None = None,
) -> list[dict]:
    """Get shift request detail rows for a group/month.

    Args:
        submitted_only: If True, only return submitted requests. If False, include all.
        submitted_after: ISO date string — only requests submitted on or after this date.
        submitted_before: ISO date string — only requests submitted on or before this date.
    """
    month_str = _wanted_request_month_str(year, month)
    q = (
        db.query(NurseShiftRequest, WantedRequest.is_submitted, WantedRequest.submitted_at)
        .join(WantedRequest, NurseShiftRequest.request_id == WantedRequest.request_id)
        .join(Nurse, NurseShiftRequest.nurse_id == Nurse.nurse_id)
        .filter(WantedRequest.month == month_str)
        .filter(Nurse.group_id == group_id)
    )
    if nurse_id:
        q = q.filter(NurseShiftRequest.nurse_id == nurse_id)
    if submitted_only:
        q = q.filter(WantedRequest.is_submitted == True)
    if submitted_after:
        q = q.filter(WantedRequest.submitted_at >= submitted_after)
    if submitted_before:
        q = q.filter(WantedRequest.submitted_at <= submitted_before)
    rows = q.all()
    return [
        {
            "nurse_id": r[0].nurse_id,
            "request_id": r[0].request_id,
            "shift_date": str(r[0].shift_date) if r[0].shift_date else None,
            "shift": r[0].shift,
            "shifts_table_id": r[0].shifts_table_id,
            "score": r[0].score,
            "comment": r[0].comment,
            "is_submitted": bool(r[1]),
            "submitted_at": str(r[2]) if r[2] else None,
        }
        for r in rows
    ]


def get_wanted_adjustments(
    db: Session,
    group_id: str,
    year: int,
    month: int,
    *,
    nurse_id: str | None = None,
    date_range: tuple[str, str] | None = None,
    source_types: list[str] | None = None,
    is_applied: bool | None = None,
) -> list[dict]:
    """Get fixed wanted (adjustment) entries for a group/month.

    Returned rows expose ``source_type`` and ``original_shift_id`` so the caller
    can distinguish nurse-submitted intent from head-nurse interventions:
      - ``original``: nurse-submitted, shift_id is the answer
      - ``modified``: nurse-submitted but head nurse changed code → original_shift_id is the nurse's intent
      - ``added``: head-nurse only, no nurse submission
      - ``weekly_off``: auto-assigned weekly off
    """
    q = db.query(FixedWantedEntry).filter(
        FixedWantedEntry.group_id == group_id,
        FixedWantedEntry.year == year,
        FixedWantedEntry.month == month,
    )
    if nurse_id:
        q = q.filter(FixedWantedEntry.nurse_id == nurse_id)
    if date_range:
        start, end = date_range
        q = q.filter(FixedWantedEntry.shift_date >= start, FixedWantedEntry.shift_date <= end)
    if source_types:
        q = q.filter(FixedWantedEntry.source_type.in_(source_types))
    if is_applied is not None:
        q = q.filter(FixedWantedEntry.is_applied == is_applied)
    rows = q.all()
    return [
        {
            "entry_id": r.id,
            "group_id": r.group_id,
            "nurse_id": r.nurse_id,
            "year": r.year,
            "month": r.month,
            "work_date": str(r.shift_date) if r.shift_date else None,
            "shift_id": r.shift_id,
            "source_type": r.source_type,
            "original_shift_id": r.original_shift_id,
            "is_applied": bool(r.is_applied) if hasattr(r, "is_applied") else True,
            "reason": r.reason,
        }
        for r in rows
    ]


def get_submission_status(
    db: Session, group_id: str, year: int, month: int
) -> dict:
    """Get submission status summary — who submitted, who didn't.

    group_id-scoped single outerjoin: 한 쿼리로 DB-level filter, RBAC 격리.
    """
    from db.models import Nurse

    month_str = _wanted_request_month_str(year, month)
    rows = (
        db.query(Nurse.nurse_id, Nurse.name, WantedRequest.is_submitted)
        .outerjoin(
            WantedRequest,
            (WantedRequest.nurse_id == Nurse.nurse_id)
            & (WantedRequest.month == month_str)
            & (WantedRequest.group_id == group_id)
            & (WantedRequest.is_submitted == True),
        )
        .filter(Nurse.group_id == group_id, Nurse.active == 1)
        .all()
    )
    submitted, not_submitted = [], []
    for nurse_id, name, is_sub in rows:
        bucket = submitted if is_sub else not_submitted
        bucket.append({"nurse_id": nurse_id, "name": name})
    return {
        "total": len(rows),
        "submitted_count": len(submitted),
        "not_submitted_count": len(not_submitted),
        "submitted": submitted,
        "not_submitted": not_submitted,
    }


# 에이전트가 원티드 조정(fixed_wanted_entries)으로 설정 가능한 필드 화이트리스트.
# 의도된 사용 = is_applied(고정원티드 적용/반려, bulk_mutation target_field 기본값).
# reason = FE 에 실제 표시되는 사유(modal-roster-wanted.tsx). 나머지(id/group_id/year/month/
# nurse_id/shift_date/shift_id/source_type/original_shift_id/created_*/updated_*/shifts_table_id)는
# 정체성·스코프·커버리지 직결이라 차단.
_WANTED_ADJ_SETTABLE = frozenset({"is_applied", "reason"})


def _validate_wanted_adj_field(field: str, value) -> str | None:
    """원티드 조정(FixedWantedEntry) 쓰기 검증. 통과 시 None, 실패 시 에러 메시지."""
    if field not in _WANTED_ADJ_SETTABLE:
        return (
            f"'{field}' 은(는) 원티드 조정으로 설정할 수 없는 필드입니다. "
            f"허용: {sorted(_WANTED_ADJ_SETTABLE)}"
        )
    if field == "is_applied" and not isinstance(value, bool):
        if value not in (0, 1, "0", "1", "true", "false", "True", "False"):
            return f"is_applied 는 bool 값이어야 합니다: {value!r}"
    return None


def bulk_update_wanted_adjustments(
    db: Session,
    entry_ids: list[str],
    field: str,
    value,
    *,
    group_id: str,
    preview_only: bool = False,
) -> dict:
    """Bulk update fixed wanted entries. Returns affected count.

    group_id-scoped: 전달된 entry_ids 중 group_id 가 일치하는 것만 적용
    (cross-group mutation 차단).
    """
    err = _validate_wanted_adj_field(field, value)
    if err is not None:
        return {"error": err}
    rows = (
        db.query(FixedWantedEntry)
        .filter(
            FixedWantedEntry.id.in_(entry_ids),
            FixedWantedEntry.group_id == group_id,
        )
        .all()
    )
    if preview_only:
        return {
            "preview": True,
            "affected_count": len(rows),
            "entry_ids": [r.id for r in rows],
        }
    for row in rows:
        setattr(row, field, value)
    db.commit()
    return {
        "preview": False,
        "affected_count": len(rows),
        "entry_ids": [r.id for r in rows],
    }


def update_wanted_deadline(
    db: Session, group_id: str, year: int, month: int, new_deadline: str
) -> dict:
    """Update wanted campaign deadline."""
    from datetime import datetime as dt

    row = (
        db.query(Wanted)
        .filter(
            Wanted.group_id == group_id,
            Wanted.year == year,
            Wanted.month == month,
        )
        .first()
    )
    if not row:
        return {"error": "Wanted campaign not found"}
    old_deadline = str(row.exp_date) if row.exp_date else None
    # Parse string to datetime for DB compatibility (SQLite requires datetime objects)
    try:
        row.exp_date = dt.strptime(new_deadline, "%Y-%m-%d")
    except (ValueError, TypeError):
        row.exp_date = new_deadline
    db.commit()
    return {
        "group_id": group_id,
        "year": year,
        "month": month,
        "old_deadline": old_deadline,
        "new_deadline": new_deadline,
    }


def clear_wanted_deadline(
    db: Session, group_id: str, year: int, month: int
) -> dict:
    """Clear wanted campaign deadline (set exp_date to NULL).

    DB 컬럼 자체가 nullable 이므로 마감일 해제는 정상 운영 경로.
    "마감일 없애줘 / 제거 / 삭제" 같은 의도에 대응한다.
    """
    row = (
        db.query(Wanted)
        .filter(
            Wanted.group_id == group_id,
            Wanted.year == year,
            Wanted.month == month,
        )
        .first()
    )
    if not row:
        return {"error": "Wanted campaign not found"}
    old_deadline = str(row.exp_date) if row.exp_date else None
    row.exp_date = None
    db.commit()
    return {
        "group_id": group_id,
        "year": year,
        "month": month,
        "old_deadline": old_deadline,
        "new_deadline": None,
    }


def _release_lock_on_error(fn):
    """쓰기 도구(실행)를 savepoint 로 감싼다 — 예외면 이 도구가 쓴 것만 되돌리고 병동·월 잠금을 푼 뒤 다시 던진다.

    ★ 그냥 올려 보내면 잠금이 턴 끝 세션 저장(LLM 답 생성 뒤)까지 남아, 같은 병동·월 화면 제출이 10초 기다린 뒤
      409 를 받는다. 덜 만든 초안·복사 칸이 그 커밋에 섞일 수도 있다(11-04 점검).
    ★ 세션 rollback 은 쓰지 않는다 — 같은 턴 감사 기록까지 날아간다. 커밋(`_unlock`)으로 잠금을 풀고 그 기록을
      확정한다(MSSQL 은 savepoint 롤백으로 그 뒤에 잡은 잠금이 풀리지만 확대·변환된 잠금은 남는다).
      미리보기(preview_only=True)는 잠그지도 쓰지도 않아 감싸지 않는다.
    ★ 예외: 교착(1205)이면 서버가 이미 턴 트랜잭션 전체를 되돌렸다(감사 기록도 사라졌다) — 세션만 서버에 맞추고
      1205 를 다시 던진다(`_resync_lost_transaction`).
    ★ 전제: 도구 안 commit 은 **마지막 쓰기**다(`_submit_draft`·철회의 commit·`_unlock`). commit 하면 savepoint 도
      닫히므로, 그 뒤에 쓰고 예외가 나면 되돌릴 것이 없고 잠금 풀기 commit 이 그 쓰기까지 확정한다.
    """
    has_preview = "preview_only" in inspect.signature(fn).parameters

    @functools.wraps(fn)
    def wrapper(db: Session, *args, **kwargs):
        if has_preview and kwargs.get("preview_only", True):
            return fn(db, *args, **kwargs)
        savepoint = db.begin_nested()
        try:
            result = fn(db, *args, **kwargs)
        except Exception as exc:
            reraise = _server_rolled_back(exc)
            if reraise is not None:
                _resync_lost_transaction(db, fn.__name__)
                if reraise is exc:
                    raise
                raise reraise from None
            try:
                # ★ is_active 로 고르지 않는다 — 도구 안 commit 의 flush 가 실패하면 savepoint 는 비활성으로 세션에
                #   남는다. 그대로 두면 잠금 풀기(commit)가 PendingRollbackError 로 실패하고, 턴 끝 rollback 에 앞선
                #   감사 기록까지 사라진다(11-04 Codex 재현). 세션에 남아 있으면 비활성이어도 되돌려 닫는다.
                if _savepoint_open(db, savepoint):
                    savepoint.rollback()
                _unlock(db)
            except SQLAlchemyError:
                # 연결이 끊긴 경우 등 — 턴 끝 정리에 맡기고 원래 예외를 올린다.
                logger.warning("[aide.wanted] 오류 뒤 잠금 풀기 실패: %s", fn.__name__, exc_info=True)
            raise
        if _savepoint_open(db, savepoint):   # 도구가 커밋하지 않고 끝난 경로(잠금을 못 잡은 경우 등)
            if savepoint.is_active:
                savepoint.commit()
            else:
                savepoint.rollback()          # 삼켜진 flush 오류 — 세션을 다시 쓸 수 있게 닫는다
        return result

    return wrapper


def _savepoint_open(db: Session, savepoint) -> bool:
    """savepoint 가 아직 세션의 트랜잭션 체인에 있는지(비활성 포함). 도구가 커밋했으면 이미 닫혀 False."""
    tx = db.get_nested_transaction()
    while tx is not None:
        if tx is savepoint:
            return True
        tx = tx.parent
    return False


#: 서버가 트랜잭션을 통째로 되돌렸다는 MSSQL 오류 — 1205 교착 희생, 그 뒤 savepoint 롤백·커밋이 받는 3903·3902.
_SERVER_TX_LOST = (1205, 3902, 3903)


def _sql_error_number(exc: BaseException) -> int | None:
    args = getattr(getattr(exc, "orig", None), "args", None) or ()
    return args[0] if args and isinstance(args[0], int) else None


def _server_rolled_back(exc: BaseException) -> BaseException | None:
    """서버가 트랜잭션을 통째로 되돌렸으면 다시 던질 예외, 아니면 None.

    ★ 도구 안 commit 의 flush 가 1205 를 받으면 SQLAlchemy 의 savepoint 정리(ROLLBACK TRANSACTION)가 3903 으로
      실패하고, 그 오류가 1205 를 가린다(11-04 점검). DB 오류면 사슬을 따라가 1205 를 찾아 그것을 던진다.
    ★ 전제: 도구는 DB 오류를 잡아 같은 세션에서 다시 시도하지 않는다. 그런 도구가 생기면 사슬에 남은 옛 1205 때문에
      살아 있는 트랜잭션까지 되돌린다.
    """
    chain, cur = [], exc
    while cur is not None and len(chain) < 10:
        chain.append(cur)
        cur = cur.__cause__ or cur.__context__
    numbers = [_sql_error_number(e) for e in chain]
    if not any(n in _SERVER_TX_LOST for n in numbers):
        return None
    if isinstance(exc, SQLAlchemyError) and 1205 in numbers:
        return chain[numbers.index(1205)]
    return exc


def _resync_lost_transaction(db: Session, name: str) -> None:
    """서버가 되돌린 트랜잭션에 세션을 맞춘다 — 남은 savepoint 마다 ROLLBACK TRANSACTION 이 3903 으로 실패하며
    한 겹씩 닫히므로, 루트(pymssql rollback 은 3903 을 삼키고 BEGIN TRAN)에 닿을 때까지 거듭 부른다.

    ★ 맞추지 않으면 서버는 트랜잭션 밖이라, 다음 쓰기(실패 감사 기록 등)가 문장마다 바로 확정되고 잠금 SELECT 의
      UPDLOCK 이 문장 끝에 풀리며 턴 끝 commit 이 3902 로 실패한다(11-04 점검).
    """
    logger.error("[aide.wanted] 서버가 트랜잭션을 되돌림(교착 등) — 같은 턴 앞선 기록도 사라짐: %s", name, exc_info=True)
    last_error = None
    for _ in range(16):     # 실패 한 번에 살아 있는 savepoint 한 겹 — 지금 가장 깊은 경로는 3겹(루트 포함)
        try:
            db.rollback()
            return
        except SQLAlchemyError as err:
            last_error = err
    logger.warning("[aide.wanted] 교착 뒤 세션 맞추기 실패: %s", name, exc_info=last_error)


@_release_lock_on_error
def cancel_wanted_request(
    db: Session, nurse_id: str, group_id: str, year: int, month: int, request_id: int | None = None,
    *, self_rules: bool = False,
) -> dict:
    """Cancel (retract) a wanted request submission.

    수간호사·관리자 경로는 group_id 필터로 cross-group mutation 차단 (defense-in-depth).
    병동·월 잠금 뒤에 읽는다 · self_rules=True(간호사 본인)면 화면 철회처럼 마감 뒤에는 막는다(`_lock_for_write`).
    """
    month_str = _wanted_request_month_str(year, month)
    blocked = _lock_for_write(db, group_id, year, month, self_rules=self_rules)
    if blocked:
        return {"error": blocked}
    q = db.query(WantedRequest).filter(
        WantedRequest.nurse_id == nurse_id,
        WantedRequest.month == month_str,
        WantedRequest.is_submitted == True,
    )
    # ★ 간호사 본인 철회는 화면 철회(`retract_submission_service`)처럼 병동을 보지 않고 그 달 제출본을 모두 내린다 —
    #   병동을 옮기기 전에 낸 제출본이 남으면 화면 저장이 '이미 제출'로 계속 막힌다(11-04 검증). 수간호사·관리자는 이 병동만.
    if not self_rules:
        q = q.filter(WantedRequest.group_id == group_id)
    if request_id:
        q = q.filter(WantedRequest.request_id == request_id)
    rows = q.order_by(desc(WantedRequest.submitted_at)).all()
    if not rows:
        return _write_error(db, "No submitted request found")
    row = rows[0]
    # 요청 번호를 안 주면 그 달 제출본을 모두 내린다 — 이전 제출본이 철회 뒤 되살아나지 않게(`_submit_draft` 참고).
    for r in rows:
        r.is_submitted = False
        r.submitted_at = None
    db.commit()
    return {
        "nurse_id": nurse_id,
        "request_id": row.request_id,
        "month_str": month_str,
        "status": "retracted",
    }


def _parse_date(val) -> date_type:
    """Convert string or datetime to date object (SQLite compatibility)."""
    if isinstance(val, date_type):
        return val
    if isinstance(val, str):
        return datetime.strptime(val, "%Y-%m-%d").date()
    return val


# ── Per-date wanted CRUD ──────────────────────────────────


def _get_latest_submitted_request(
    db: Session, nurse_id: str, group_id: str, month_str: str
) -> WantedRequest | None:
    """Find the latest submitted WantedRequest for a nurse+month.

    group_id-scoped: cross-group read 차단 (RBAC defense-in-depth).
    """
    return (
        db.query(WantedRequest)
        .filter(
            WantedRequest.nurse_id == nurse_id,
            WantedRequest.group_id == group_id,
            WantedRequest.month == month_str,
            WantedRequest.is_submitted == True,
        )
        .order_by(desc(WantedRequest.submitted_at))
        .first()
    )


def _create_draft_with_copy(
    db: Session,
    nurse_id: str,
    month_str: str,
    old_request_id: int | None,
    *,
    group_id: str,
    exclude_date: str | None = None,
    modify_date: str | None = None,
    modify_shift: str | None = None,
    modify_shifts_table_id: int | None = None,
    modify_comment: str | None = None,
    add_date: str | None = None,
    add_shift: str | None = None,
    add_shifts_table_id: int | None = None,
    add_comment: str | None = None,
) -> tuple[WantedRequest, int]:
    """Create a new draft WantedRequest and copy shift+pair data with modifications.

    Returns (new_draft, shift_count).
    """
    # 1. New request_id = MAX + 1
    max_rid = (
        db.query(func.max(WantedRequest.request_id))
        .filter(
            WantedRequest.nurse_id == nurse_id,
            WantedRequest.month == month_str,
        )
        .scalar()
    ) or 0
    new_request_id = max_rid + 1

    # 2. Create draft WantedRequest
    draft = WantedRequest(
        nurse_id=nurse_id,
        request_id=new_request_id,
        month=month_str,
        group_id=group_id,
        request="",
        is_submitted=False,
        created_at=datetime.now(),
        submitted_at=None,
    )
    db.add(draft)

    # 3. Copy NurseShiftRequest rows from old request_id (with modifications)
    #    ★ request_id 는 간호사·**월마다** 1부터 다시 센다(`_next_request_id`) — 이 달 날짜만 복사한다. 월을 안 걸면
    #      다른 달의 같은 번호 칸까지 새 제출본에 딸려 와 편집할 때마다 쌓인다(11-04 확인).
    detailed_id = 1
    if old_request_id is not None:
        month_start, month_end = _month_bounds(month_str)
        old_rows = (
            db.query(NurseShiftRequest)
            .filter(
                NurseShiftRequest.nurse_id == nurse_id,
                NurseShiftRequest.request_id == old_request_id,
                NurseShiftRequest.shift_date >= month_start,
                NurseShiftRequest.shift_date < month_end,
            )
            .all()
        )
        for row in old_rows:
            date_str = str(row.shift_date) if row.shift_date else None
            # Skip the excluded date (delete case)
            if exclude_date and date_str == exclude_date:
                continue
            # Modify the target date (modify case)
            shift = row.shift
            s_table_id = row.shifts_table_id
            comment = row.comment
            if modify_date and date_str == modify_date:
                shift = modify_shift or shift
                s_table_id = modify_shifts_table_id if modify_shifts_table_id is not None else s_table_id
                if modify_comment is not None:
                    comment = modify_comment
            db.add(NurseShiftRequest(
                nurse_id=nurse_id,
                request_id=new_request_id,
                detailed_request_id=detailed_id,
                shift_date=row.shift_date,
                group_id=group_id,
                shift=shift,
                shifts_table_id=s_table_id,
                score=row.score,
                partial_request=row.partial_request,
                comment=comment,
            ))
            detailed_id += 1

    # 4. Add new date (add case)
    if add_date and add_shift:
        parsed_date = _parse_date(add_date)
        db.add(NurseShiftRequest(
            nurse_id=nurse_id,
            request_id=new_request_id,
            detailed_request_id=detailed_id,
            shift_date=parsed_date,
            group_id=group_id,
            shift=add_shift,
            shifts_table_id=add_shifts_table_id,
            score=1.0,
            partial_request="",
            comment=add_comment or "",
        ))
        detailed_id += 1

    # 5. Copy NursePairRequest from old request_id
    if old_request_id is not None:
        old_pairs = (
            db.query(NursePairRequest)
            .filter(
                NursePairRequest.nurse_id == nurse_id,
                NursePairRequest.request_id == old_request_id,
                NursePairRequest.month == month_str,
            )
            .all()
        )
        pair_id = 1
        for p in old_pairs:
            db.add(NursePairRequest(
                nurse_id=nurse_id,
                request_id=new_request_id,
                month=month_str,
                detailed_request_id=pair_id,
                target_id=p.target_id,
                group_id=group_id,
                score=p.score,
                partial_request=p.partial_request,
            ))
            pair_id += 1

    db.flush()
    return draft, detailed_id - 1


def _resolve_shifts_table_id(db: Session, group_id: str, shift_code: str) -> int | None:
    """Map shift_id → shifts.id for a group.

    ★ 대표 행은 목록 조회와 같은 (sequence ASC, id ASC) **첫 행**이다
      (정본 `shift_service_mssql.SHIFT_LIST_ORDER`). `shifts` 에 UNIQUE 가 없어
      같은 (group, shift_id) 행이 여럿일 수 있고, 정렬 없는 `.first()` 는 어느 행이
      올지 보장되지 않는다 — 반환값이 `nurse_shift_requests.shifts_table_id` 로
      저장되므로 흔들리면 화면과 저장값이 갈린다.
      (office_id 는 걸지 않는다 — `group_id` 가 `groups` 의 PK 라 이미 오피스 단위로 유일하다.)
    """
    row = (
        db.query(Shift.id)
        .filter(Shift.group_id == group_id, Shift.shift_id == shift_code)
        .order_by(Shift.sequence.asc(), Shift.id.asc())
        .first()
    )
    return row[0] if row else None


def _lock_for_write(db: Session, group_id: str, year: int, month: int, *, self_rules: bool) -> str | None:
    """원티드 쓰기 전 병동·월 잠금 — 간호사 본인(self_rules)이면 화면 제출처럼 마감도 본다.

    ★ 최신 제출본은 잠근 **뒤에** 읽는다. 잠금 전에 읽으면 기다리는 사이 커밋된 편집·철회를 못 보고 옛 제출본을
      복사해 그 변경을 덮는다(11-04 Codex). 그래서 수간호사·관리자 편집도 같은 잠금에 줄을 선다(마감·한도는
      보지 않는다 — 관리보드 직접 입력처럼 한도 예외, 10-27 결정 ④).
    ★ 행을 쓰기 **전에** 잠근다(`lock_wanted_month` 교착 설명). 막아야 하면 사용자에게 보일 문구(잠금은 풀어 둔다),
      아니면 None.
    """
    from services.preferences_service import (
        PreferenceConflictError,
        _is_wanted_closed,
        _load_wanted_window,
    )
    from services.wanted_daily_quota_service import lock_wanted_month

    try:
        lock_wanted_month(db, group_id, year, month, source="AIDE")
    except PreferenceConflictError as exc:
        return str(exc)
    if not self_rules:
        return None
    wanted = _load_wanted_window(db, group_id, year, month)
    reason = None
    if wanted is None:
        reason = f"{year}년 {month}월 원티드 요청이 아직 생성되지 않았습니다."
    elif _is_wanted_closed(wanted):
        reason = f"{year}년 {month}월 원티드가 마감되었습니다."
    if reason:
        _unlock(db)
    return reason


def _unlock(db: Session) -> None:
    """잠근 뒤 쓰지 않고 끝나는 경로 — 커밋으로 병동·월 잠금만 푼다(이 도구는 아직 아무것도 쓰지 않았다).

    ★ rollback 으로 풀지 않는다 — AIDE 한 턴은 끝에 한 번 커밋해서, 앞선 스킬 감사 기록(flush 만 된 행)까지 날아간다.
    """
    db.commit()


def _write_error(db: Session, message: str, *, locked: bool = True) -> dict:
    """도구 오류 응답 — 실행 경로(잠근 뒤)면 잠금을 풀고 돌려준다."""
    if locked:
        _unlock(db)
    return {"error": message}


def _self_quota_block(
    db: Session, nurse_id: str, group_id: str, year: int, month: int, request_id: int,
) -> dict | None:
    """간호사 본인 제출의 일자 한도 — 넘치면 화면 409 와 같은 내용을 오류로 돌려준다(되돌리기는 `_draft_within_quota`)."""
    from services.preferences_service import PreferenceQuotaError
    from services.wanted_daily_quota_service import assert_daily_quota

    try:
        assert_daily_quota(db, nurse_id=nurse_id, group_id=group_id, year=year, month=month,
                           request_id=request_id)
    except PreferenceQuotaError as exc:
        return {"error": str(exc), "code": exc.code, "detail": exc.detail}
    return None


def _draft_within_quota(
    db: Session, nurse_id: str, group_id: str, year: int, month: int, old_request_id: int | None,
    *, self_rules: bool, **changes,
) -> tuple[WantedRequest | None, int, dict | None]:
    """초안을 만들고, 간호사 본인이면 일자 한도를 본다. 넘치면 이 초안만 되돌리고 잠금을 푼 뒤 오류를 돌려준다.

    ★ 초안은 savepoint 안에서 만든다 — 세션 rollback 은 같은 AIDE 턴의 감사 기록까지 날린다(`_unlock`).
    """
    month_str = _wanted_request_month_str(year, month)
    savepoint = db.begin_nested()
    draft, count = _create_draft_with_copy(db, nurse_id, month_str, old_request_id, group_id=group_id, **changes)
    over = _self_quota_block(db, nurse_id, group_id, year, month, draft.request_id) if self_rules else None
    if over:
        savepoint.rollback()
        _unlock(db)
        return None, 0, over
    savepoint.commit()
    return draft, count, None


def _submit_draft(db: Session, draft: WantedRequest) -> None:
    """Mark a draft as submitted — 같은 달 이전 제출본은 내린다(제출본은 간호사·달마다 1건).

    ★ 예전엔 이전 제출본을 남겨 두어, 철회(최신 1건만 내림) 뒤 그 제출본이 다시 '최신'이 되어 일자 한도
      자리를 되살렸다(11-04 검토). 제출 게이트처럼 병동은 보지 않는다.
    """
    for row in db.query(WantedRequest).filter(
        WantedRequest.nurse_id == draft.nurse_id,
        WantedRequest.month == draft.month,
        WantedRequest.is_submitted == True,
        WantedRequest.request_id != draft.request_id,
    ).all():
        row.is_submitted = False
        row.submitted_at = None
    draft.is_submitted = True
    draft.submitted_at = datetime.now()
    db.commit()


@_release_lock_on_error
def delete_wanted_by_date(
    db: Session, nurse_id: str, group_id: str, year: int, month: int, date: str,
    *, preview_only: bool = True, self_rules: bool = False,
) -> dict:
    """Delete a specific date from wanted submissions.

    실행이면 병동·월 잠금 뒤에 읽는다(`_lock_for_write`) · self_rules=True(간호사 본인)면 마감 확인을 거친다
    (빼기만 하므로 일자 한도는 보지 않는다).
    """
    date, bad_date = _day_in_month(date, year, month)
    if bad_date:
        return {"error": bad_date}
    month_str = _wanted_request_month_str(year, month)
    locked = not preview_only
    if locked:
        blocked = _lock_for_write(db, group_id, year, month, self_rules=self_rules)
        if blocked:
            return {"error": blocked}
    old_wr = _get_latest_submitted_request(db, nurse_id, group_id, month_str)
    if not old_wr:
        return _write_error(db, "제출된 원티드가 없습니다.", locked=locked)

    # Check the target date exists
    target = (
        db.query(NurseShiftRequest)
        .filter(
            NurseShiftRequest.nurse_id == nurse_id,
            NurseShiftRequest.request_id == old_wr.request_id,
            NurseShiftRequest.shift_date == _parse_date(date),
        )
        .first()
    )
    if not target:
        return _write_error(db, f"{date}에 해당하는 원티드가 없습니다.", locked=locked)

    if preview_only:
        return {
            "preview_only": True,
            "action": "delete_wanted_date",
            "nurse_id": nurse_id,
            "date": date,
            "shift": target.shift,
            "comment": target.comment or "",
            "message": f"{date} {target.shift} 원티드를 취소합니다.",
        }

    draft, count = _create_draft_with_copy(
        db, nurse_id, month_str, old_wr.request_id,
        group_id=group_id,
        exclude_date=date,
    )
    _submit_draft(db, draft)
    return {
        "action": "delete_wanted_date",
        "nurse_id": nurse_id,
        "request_id": draft.request_id,
        "date": date,
        "deleted_shift": target.shift,
        "remaining_count": count,
        "status": "submitted",
    }


@_release_lock_on_error
def add_wanted_by_date(
    db: Session, nurse_id: str, group_id: str,
    year: int, month: int, date: str, shift_code: str,
    *, comment: str = "", preview_only: bool = True, self_rules: bool = False,
) -> dict:
    """Add a new date to wanted submissions.

    실행이면 병동·월 잠금 뒤에 읽는다(`_lock_for_write`) · self_rules=True(간호사 본인)면 화면 제출과 같은
    마감·일자 한도를 거친다.
    """
    date, bad_date = _day_in_month(date, year, month)
    if bad_date:
        return {"error": bad_date}
    month_str = _wanted_request_month_str(year, month)
    locked = not preview_only
    if locked:
        blocked = _lock_for_write(db, group_id, year, month, self_rules=self_rules)
        if blocked:
            return {"error": blocked}
    old_wr = _get_latest_submitted_request(db, nurse_id, group_id, month_str)
    old_rid = old_wr.request_id if old_wr else None

    # Check duplicate
    if old_rid is not None:
        dup = (
            db.query(NurseShiftRequest)
            .filter(
                NurseShiftRequest.nurse_id == nurse_id,
                NurseShiftRequest.request_id == old_rid,
                NurseShiftRequest.shift_date == _parse_date(date),
            )
            .first()
        )
        if dup:
            return _write_error(
                db, f"{date}에 이미 {dup.shift} 원티드가 있습니다. 수정을 원하시면 변경 요청을 해주세요.", locked=locked,
            )

    shifts_table_id = _resolve_shifts_table_id(db, group_id, shift_code)

    if preview_only:
        msg = f"{date}에 {shift_code} 원티드를 추가합니다."
        if comment:
            msg = f"{date}에 '{comment}' 사유로 {shift_code} 원티드를 추가합니다."
        return {
            "preview_only": True,
            "action": "add_wanted_date",
            "nurse_id": nurse_id,
            "date": date,
            "shift": shift_code,
            "comment": comment,
            "message": msg,
        }

    draft, count, over = _draft_within_quota(
        db, nurse_id, group_id, year, month, old_rid, self_rules=self_rules,
        add_date=date,
        add_shift=shift_code,
        add_shifts_table_id=shifts_table_id,
        add_comment=comment,
    )
    if over:
        return over
    _submit_draft(db, draft)
    return {
        "action": "add_wanted_date",
        "nurse_id": nurse_id,
        "request_id": draft.request_id,
        "date": date,
        "shift": shift_code,
        "comment": comment,
        "total_count": count,
        "status": "submitted",
    }


@_release_lock_on_error
def modify_wanted_by_date(
    db: Session, nurse_id: str, group_id: str,
    year: int, month: int, date: str, new_shift_code: str,
    *, comment: str | None = None, preview_only: bool = True, self_rules: bool = False,
) -> dict:
    """Modify the shift of an existing wanted date.

    실행이면 병동·월 잠금 뒤에 읽는다(`_lock_for_write`) · self_rules=True(간호사 본인)면 화면 제출과 같은
    마감·일자 한도를 거친다.
    """
    date, bad_date = _day_in_month(date, year, month)
    if bad_date:
        return {"error": bad_date}
    month_str = _wanted_request_month_str(year, month)
    locked = not preview_only
    if locked:
        blocked = _lock_for_write(db, group_id, year, month, self_rules=self_rules)
        if blocked:
            return {"error": blocked}
    old_wr = _get_latest_submitted_request(db, nurse_id, group_id, month_str)
    if not old_wr:
        return _write_error(db, "제출된 원티드가 없습니다.", locked=locked)

    target = (
        db.query(NurseShiftRequest)
        .filter(
            NurseShiftRequest.nurse_id == nurse_id,
            NurseShiftRequest.request_id == old_wr.request_id,
            NurseShiftRequest.shift_date == _parse_date(date),
        )
        .first()
    )
    if not target:
        return _write_error(db, f"{date}에 해당하는 원티드가 없습니다.", locked=locked)

    old_shift = target.shift
    new_shifts_table_id = _resolve_shifts_table_id(db, group_id, new_shift_code)

    if preview_only:
        msg = f"{date} 원티드를 {old_shift} → {new_shift_code}로 변경합니다."
        if comment:
            msg = f"{date} 원티드를 '{comment}' 사유로 {old_shift} → {new_shift_code}로 변경합니다."
        return {
            "preview_only": True,
            "action": "modify_wanted_date",
            "nurse_id": nurse_id,
            "date": date,
            "old_shift": old_shift,
            "new_shift": new_shift_code,
            "comment": comment or "",
            "message": msg,
        }

    draft, count, over = _draft_within_quota(
        db, nurse_id, group_id, year, month, old_wr.request_id, self_rules=self_rules,
        modify_date=date,
        modify_shift=new_shift_code,
        modify_shifts_table_id=new_shifts_table_id,
        modify_comment=comment,
    )
    if over:
        return over
    _submit_draft(db, draft)
    return {
        "action": "modify_wanted_date",
        "nurse_id": nurse_id,
        "request_id": draft.request_id,
        "date": date,
        "old_shift": old_shift,
        "new_shift": new_shift_code,
        "comment": comment or "",
        "total_count": count,
        "status": "submitted",
    }
