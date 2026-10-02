"""앱 알림 발송 대기열(push_outbox) — 넣기 · 꺼내기 · 보내기 · 재시도.

흐름
  업무 코드가 `send_*_push(..., db=db)` 로 `enqueue_push` 를 불러 **업무 저장과 같은 트랜잭션**에
  행을 남긴다 → 서버 안 발송기(`main._push_outbox_dispatcher`)가 몇 초마다 `dispatch_due` 를 불러
  보낼 차례인 행을 하나씩 꺼내 그룹웨어 알림 테이블에 쓴다 → 실패하면 1분·5분·30분 뒤 다시
  시도하고, 그래도 안 되면(또는 24시간이 지나면) failed 로 남긴다.

★ 왜 이렇게 하는가 (2026-10 리뷰·실측)
  예전엔 커밋 뒤 그룹웨어에 바로 썼다. 그 사이 프로세스가 죽거나 그룹웨어가 잠깐 느리면 알림이
  영영 빠졌고 다시 보낼 근거가 없었다(원티드 마감은 이미 closed 라 다음 실행에서도 안 잡힌다).
★★ 그룹웨어 3종 쓰기와 이 행의 'sent' 표시는 **한 트랜잭션**이다(`_deliver`). 두 DB 가 같은 서버·
  같은 로그인이라 한 연결에서 함께 커밋된다. 그래서 중간에 죽으면 전부 되돌아가 다시 보내도
  두 번 들어가지 않고, 커밋됐으면 'sent' 도 함께 남아 다시 보내지 않는다.
  (단계마다 진행 지점을 따로 커밋하던 1차안은 그룹웨어 커밋과 진행 지점 커밋 사이에 죽으면
  같은 단계가 두 번 들어갔다 — Codex 리뷰 2026-10-02.)
"""
import logging
import os
import re
from collections import Counter
from datetime import datetime, timedelta, timezone

from db.client2 import EUN_DB_NAME
from db.models import PushOutbox
from sqlalchemy.orm import Session
from utils.utils import groupware_write_enabled, gw_transaction, gw_write_push, push_enabled

_logger = logging.getLogger(__name__)
_KST = timezone(timedelta(hours=9))

#: n 번째 실패 뒤 다시 시도할 때까지의 간격. 다 쓰면 failed.
RETRY_DELAYS = (timedelta(minutes=1), timedelta(minutes=5), timedelta(minutes=30))
#: 이보다 오래된 알림은 보내지 않는다(하루 지난 '마감' 알림은 혼란만 준다). 서버가 오래 멈췄다
#: 살아나도 마찬가지 — 꺼내기 전에 failed 로 접는다(`expire_stale`).
MAX_AGE = timedelta(hours=24)
#: 'sending' 으로 잡아 둔 채 이만큼 지나면 발송기가 죽은 것으로 보고 되돌린다.
#: 한 건 보내는 데 그룹웨어 문장 상한(30초) × 몇 문장이라 이보다 훨씬 짧다.
STUCK_AFTER = timedelta(minutes=10)


class _OwnershipLost(Exception):
    """보내는 사이 이 행이 더는 'sending' 이 아니다(되돌려져 다른 발송기가 잡았을 수 있다)."""


class _GuardFailed(Exception):
    """보내기 직전 조건(guard)이 깨졌다 — 예: 마감 알림인데 원티드가 다시 열림."""


def _now() -> datetime:
    """DB 시각 컬럼은 KST naive 로 쓴다(다른 테이블과 같은 규약)."""
    return datetime.now(_KST).replace(tzinfo=None)


def enqueue_push(
    db: Session, *, source: str, push_code: str, push_sub_code: str, office_code: str,
    sender_emp_seq_no: str, sender_member_id: str, recipients: list[str], message: str,
    org_message: str | None = None, link_url: str = "", link_code: str = "",
    guard: str | None = None,
) -> PushOutbox | None:
    """보낼 알림 1건을 세션에 넣는다. **커밋은 호출부**가 업무 저장과 함께 한다.

    수신자가 없으면 넣지 않고 None. 수신자는 순서를 지킨 채 중복을 뺀다.
    """
    ids = [str(r) for r in dict.fromkeys(recipients) if r]
    if not ids:
        return None
    now = _now()
    row = PushOutbox(
        source=source, push_code=push_code, push_sub_code=push_sub_code,
        office_code=str(office_code or ""), sender_emp_seq_no=str(sender_emp_seq_no or ""),
        sender_member_id=str(sender_member_id or ""), recipients=",".join(ids),
        recipient_count=len(ids), message=message, org_message=org_message or message,
        link_url=link_url or "", link_code=link_code or "", guard=guard,
        status="pending", attempts=0, next_attempt_at=now, created_at=now,
    )
    db.add(row)
    return row


def dispatch_due(db: Session, limit: int = 20) -> dict:
    """보낼 차례인 알림을 한 건씩 꺼내 보낸다. 결과 상태별 건수를 돌려준다.

    ★ 한 번에 여러 건을 잡지 않는다 — 묶어 잡으면 뒤쪽 건이 앞 건들 처리 시간만큼 'sending' 에
      머물러, 그룹웨어가 느릴 때 `STUCK_AFTER` 를 넘겨 다른 발송기에 되돌려질 수 있다.
    ★ 보낼 게 없는 평소 주기는 인덱스 조회 1문이다(정리 작업은 `HOUSEKEEPING_EVERY` 마다).
    ★ 운영 DB 인데 발송 게이트가 닫혀 있으면(ENVIRONMENT 누락·오타 등 설정 어긋남) **아무것도
      건드리지 않는다**. 그대로 두면 `_deliver` 가 대기 건을 skipped 로 영구 처리해, 설정을 고쳐도
      되살릴 수 없다(Codex 리뷰 2026-10-02). skipped 는 운영 DB 가 아닐 때(dev·로컬)만 쓴다.
    """
    if groupware_write_enabled() and not push_enabled():
        _log_hold()
        return {"held": 1}
    counts: Counter = Counter()
    expired = _housekeep(db)
    if expired:
        counts["expired"] = expired
    for _ in range(limit):
        row_id = _claim_next(db)
        if row_id is None:
            break
        counts[_process(db, row_id)] += 1
    return dict(counts)


_HOLD_LOG_EVERY = timedelta(minutes=10)
_next_hold_log_at: datetime | None = None


def _log_hold() -> None:
    """발송 보류 상태를 알린다(10초마다 찍으면 로그가 묻히니 10분에 한 번)."""
    global _next_hold_log_at
    now = _now()
    if _next_hold_log_at is not None and now < _next_hold_log_at:
        return
    _next_hold_log_at = now + _HOLD_LOG_EVERY
    _logger.error(
        "[PushOutbox] 운영 DB 인데 발송 게이트가 닫혀 있음(ENVIRONMENT=%r) — 대기 알림을 그대로 두고 "
        "보내지 않는다. ENVIRONMENT=production 으로 고치면 이어서 보낸다.",
        os.getenv("ENVIRONMENT"),
    )


#: 멈춘 발송 되돌리기·오래된 대기 접기 주기. 둘 다 분 단위 기준이라 10초마다 돌 이유가 없다.
HOUSEKEEPING_EVERY = timedelta(minutes=1)
_next_housekeeping_at: datetime | None = None


def _housekeep(db: Session) -> int:
    """정리 작업을 `HOUSEKEEPING_EVERY` 에 한 번만 한다. 접은 대기 건 수를 돌려준다."""
    global _next_housekeeping_at
    now = _now()
    if _next_housekeeping_at is not None and now < _next_housekeeping_at:
        return 0
    _next_housekeeping_at = now + HOUSEKEEPING_EVERY
    reclaim_stuck(db)
    return expire_stale(db)


#: 발송기 조회를 이 인덱스로 고정한다(DDL 주석 참조). 대기 건이 없을 때 이력을 훑지 않게.
_DUE_INDEX = "INDEX(IX_push_outbox_due)"


def _update_due(db: Session, where, values: dict) -> int:
    """`IX_push_outbox_due` 로 **먼저 찾고**, 있을 때만 고친다. 바뀐 행 수.

    ★ UPDATE 대상에는 인덱스 힌트를 못 단다(SQL Server 1069 — 실측). 그래서 힌트 단 SELECT 로
      대상 id 를 찾는다. 평소엔 0건이라 UPDATE 자체를 안 보낸다. 다른 쪽이 잠근 행은 기다리지
      않고 건너뛴다(READPAST — 다음 정리 때 다시 본다). UPDATE 에 같은 조건을 다시 걸어
      그 사이 상태가 바뀐 행은 건드리지 않는다.
    """
    ids = [
        row_id for (row_id,) in db.query(PushOutbox.id)
        .with_hint(PushOutbox, f"WITH (READPAST, {_DUE_INDEX})", "mssql")
        .filter(*where).limit(500)
    ]
    if not ids:
        db.commit()
        return 0
    changed = (
        db.query(PushOutbox).filter(PushOutbox.id.in_(ids), *where)
        .update(values, synchronize_session=False)
    )
    db.commit()
    return changed


def reclaim_stuck(db: Session) -> int:
    """'sending' 으로 잡힌 채 오래된 행을 pending 으로 되돌린다(발송 도중 프로세스가 죽은 경우)."""
    changed = _update_due(
        db, (PushOutbox.status == "sending", PushOutbox.claimed_at < _now() - STUCK_AFTER),
        {PushOutbox.status: "pending"},
    )
    if changed:
        _logger.warning("[PushOutbox] 발송 도중 멈춘 %d건을 다시 대기로 돌림", changed)
    return changed


def expire_stale(db: Session) -> int:
    """만든 지 `MAX_AGE` 가 지난 대기 건을 보내지 않고 failed 로 접는다."""
    changed = _update_due(
        db, (PushOutbox.status == "pending", PushOutbox.created_at < _now() - MAX_AGE),
        {PushOutbox.status: "failed", PushOutbox.last_error: "만든 지 24시간이 지나 보내지 않음"},
    )
    if changed:
        _logger.error("[PushOutbox] 24시간 지난 대기 알림 %d건을 보내지 않고 접음", changed)
    return changed


def _claim_next(db: Session) -> int | None:
    """보낼 차례인 행 하나를 'sending' 으로 잡는다. 없으면 None.

    ★ `UPDLOCK, READPAST` — 여러 프로세스가 동시에 꺼내도 같은 행을 두 번 잡지 않고,
      다른 쪽이 잡고 있는 행은 기다리지 않고 건너뛴다.
    ★ 정렬을 인덱스 순서(next_attempt_at, id)에 맞추고 인덱스를 고정한다. `ORDER BY id` 였을 땐
      대기 건이 없으면 쌓인 이력을 id 순으로 끝까지 훑을 수 있었다(실행 계획 확인 2026-10-02).
    """
    now = _now()
    row = (
        db.query(PushOutbox)
        .with_hint(PushOutbox, f"WITH (UPDLOCK, READPAST, ROWLOCK, {_DUE_INDEX})", "mssql")
        .filter(
            PushOutbox.status == "pending",
            PushOutbox.next_attempt_at <= now,
            PushOutbox.created_at >= now - MAX_AGE,
        )
        .order_by(PushOutbox.next_attempt_at, PushOutbox.id)
        .first()
    )
    if row is None:
        db.commit()
        return None
    row.status, row.claimed_at = "sending", now
    row_id = row.id
    db.commit()
    return row_id


def _process(db: Session, row_id: int) -> str:
    """1건 보내고 결과 상태를 돌려준다. 예외는 재시도로 돌린다."""
    row = db.get(PushOutbox, row_id)
    try:
        return _deliver(db, row)
    except _OwnershipLost:
        db.rollback()
        _logger.warning("[PushOutbox] 보내는 사이 다른 쪽이 가져감 — 쓰지 않고 넘김 id=%s", row_id)
        return "superseded"
    except Exception as exc:
        _logger.warning("[PushOutbox] 발송 실패 id=%s: %s", row_id, exc, exc_info=True)
        db.rollback()
        return _schedule_retry(db, db.get(PushOutbox, row_id), f"{type(exc).__name__}: {exc}")


def _deliver(db: Session, row: PushOutbox) -> str:
    """그룹웨어 알림 3종을 쓰고 이 행을 'sent' 로 바꾼다 — **한 트랜잭션**(모듈 도크스트링 ★★)."""
    if not push_enabled():
        if groupware_write_enabled():   # 운영 DB 인데 게이트가 닫힘 — 보통은 dispatch_due 가 먼저 막는다
            return _finish(db, row, "pending", "발송 게이트 닫힘(설정 어긋남) — 대기로 되돌림")
        return _finish(db, row, "skipped", "운영 외 환경 — 실제 발송하지 않음")
    missing = [name for name, value in (
        ("office_code", row.office_code), ("sender_emp_seq_no", row.sender_emp_seq_no),
        ("sender_member_id", row.sender_member_id),
    ) if not value]
    if missing:
        return _finish(db, row, "failed", f"필수값 없음: {', '.join(missing)}")
    if _now() - row.created_at > MAX_AGE:
        return _finish(db, row, "failed", "만든 지 24시간이 지나 보내지 않음")
    row_id, guard = row.id, row.guard
    payload = dict(
        push_code=row.push_code, push_sub_code=row.push_sub_code, office_code=row.office_code,
        sender_emp_seq_no=row.sender_emp_seq_no, sender_member_id=row.sender_member_id,
        recipients=[r for r in row.recipients.split(",") if r], message=row.message,
        org_message=row.org_message, link_url=row.link_url, link_code=row.link_code,
    )
    db.commit()   # 이 세션의 읽기 트랜잭션을 닫는다 — 아래 연결이 같은 행을 고칠 때 서로 물리지 않게
    try:
        with gw_transaction() as cursor:
            if not _guard_holds(cursor, guard):
                raise _GuardFailed(guard)
            master_idx, device_count = gw_write_push(cursor, **payload)
            _mark_sent(cursor, row_id, master_idx, device_count)
    except _GuardFailed:
        return _finish(db, row, "cancelled", f"보내기 전 조건 불충족({guard}) — 그 사이 상태가 바뀜")
    db.expire(row)
    return "sent"


def _roster_table(name: str) -> str:
    """그룹웨어 연결에서 roster 테이블을 부를 3부 이름. DB 이름은 환경변수에서만 온다.

    ★ 하드코딩 금지 — 그룹웨어 연결은 운영 eun_gw 에 붙으므로, `eun_roster` 를 박으면
      개발 서버가 운영 테이블을 고친다.
    """
    if not EUN_DB_NAME or not re.fullmatch(r"[A-Za-z0-9_]+", EUN_DB_NAME):
        raise RuntimeError(f"EUN_DB_NAME 이 올바르지 않습니다: {EUN_DB_NAME!r}")
    return f"[{EUN_DB_NAME}].dbo.{name}"


def _mark_sent(cursor, row_id: int, master_idx: int, device_count: int) -> None:
    """그룹웨어 쓰기와 같은 트랜잭션에서 'sent' 로 바꾼다. 이미 'sending' 이 아니면 전부 되돌린다."""
    cursor.execute(
        f"SET NOCOUNT ON; "
        f"UPDATE {_roster_table('push_outbox')} SET status = 'sent', sent_at = %s, master_idx = %s, "
        f"device_count = %s, last_error = NULL WHERE id = %s AND status = 'sending'; "
        f"SELECT @@ROWCOUNT;",
        (_now(), master_idx, device_count, row_id),
    )
    changed = cursor.fetchone()
    if not changed or changed[0] != 1:
        raise _OwnershipLost(row_id)


def _guard_holds(cursor, guard: str | None) -> bool:
    """보내기 직전 조건을 **발송 트랜잭션 안에서** 확인한다. 모르는 종류는 막지 않는다.

    ★ `wanted_closed` 는 그 원티드 행을 `UPDLOCK` 으로 잡은 채 커밋까지 간다. 그래서 확인과
      발송 사이에 마감일 변경이 원티드를 다시 열 수 없다(`wanted_service._lock_wanted_row` 가
      같은 잠금을 기다린다). 따로 확인하고 커밋한 뒤 보내던 1차안은 그 틈에 다시 열린 원티드에
      '마감' 알림이 갔다(Codex 리뷰 2026-10-02). 읽기만 하는 조회는 막지 않는다(U 는 S 와 공존).
    """
    if not guard:
        return True
    kind, _, rest = guard.partition(":")
    if kind == "wanted_closed":
        group_id, year, month = rest.rsplit(":", 2)
        cursor.execute(
            f"SELECT status FROM {_roster_table('wanted')} WITH (UPDLOCK, ROWLOCK) "
            f"WHERE group_id = %s AND year = %s AND month = %s",
            (group_id, int(year), int(month)),
        )
        found = cursor.fetchone()
        return bool(found) and found[0] == "closed"
    if kind == "roster_snapshot_active":
        # 재마감 변경 확인 알림 — 보내기 전에 그 마감 스냅샷이 아직 활성이어야 한다. 그 사이 철회되거나
        #   다시 마감됐으면 "확인해 주세요" 를 눌러도 409 라 보낼 이유가 없다(Codex 11회차).
        #   스냅샷 행을 UPDLOCK 으로 잡아 확인과 발송 사이에 철회·재마감이 끼어들지 못하게 한다.
        cursor.execute(
            f"SELECT is_active_issued FROM {_roster_table('issued_roster_snapshot')} WITH (UPDLOCK, ROWLOCK) "
            f"WHERE snapshot_id = %s",
            (int(rest),),
        )
        found = cursor.fetchone()
        return bool(found) and bool(found[0])
    _logger.warning("[PushOutbox] 알 수 없는 guard 종류 — 막지 않음: %s", guard)
    return True


def _finish(db: Session, row: PushOutbox, status: str, error: str | None) -> str:
    row.status = status
    row.last_error = (error or None) and error[:1000]
    db.commit()
    return status


def _schedule_retry(db: Session, row: PushOutbox, error: str) -> str:
    """실패 1회를 기록하고 다음 시도 시각을 잡는다. 재시도를 다 썼거나 너무 오래됐으면 failed.

    ★ 'sending' 이 아니면 손대지 않는다 — 커밋 응답만 끊긴 경우 실제로는 'sent' 로 남아 있다.
    """
    if row.status != "sending":
        return row.status
    now = _now()
    row.attempts = (row.attempts or 0) + 1
    row.last_error = error[:1000]
    if row.attempts > len(RETRY_DELAYS) or now - row.created_at > MAX_AGE:
        row.status = "failed"
        _logger.error("[PushOutbox] 재시도 소진 — 발송 포기 id=%s source=%s: %s", row.id, row.source, error)
    else:
        row.status = "pending"
        row.next_attempt_at = now + RETRY_DELAYS[row.attempts - 1]
    db.commit()
    return row.status
