"""병동이동 × 발행된 달 가드 — 쓰기·미리보기·재분배가 **같은 판정**을 쓴다(dev-7 · N83).

★ 왜 막는가 — 발행본은 간호사들에게 이미 배포된 확정 근무표다. 발행본 조회는 살아 있는 병동이동을
  출발 병동 표와 **받는 병동 표 양쪽에** 덧씌운다. 원장(`nurse_assignment`)만 바꾸면 배포된 그 달 표가
  바뀐다. 그래서 출발·도착 병동 **둘 다**의 발행된 달을 본다(사용자 결정 Q48, 2026-10-08).
★ 과거월 자체는 막지 않는다(사용자 결정 2026-08-26) — 입력 누락을 뒤늦게 소급 정정하는 일이 실제로 있다.
  막는 것은 **발행된 달**뿐이다. 발행을 취소하면 다시 이동할 수 있다.
★ 연월은 `meta_json` 이 정본이다. `year`/`month` 컬럼은 nullable 이고 정본 조회
  (`get_issued_roster_snapshot_service`)도 meta 만 본다. meta 를 읽을 수 없는 활성 발행본이 있으면
  어느 달인지 모르므로 막는다(fail-closed, 사용자 결정 Q6).
★ 쓰기는 마감·철회와 같은 달 잠금(`lock_month_publish`)을 잡은 **뒤에** 판정한다. 마감은 스냅샷을 만들 때
  배정을 읽으므로, '판정(발행 없음) → 마감 → 이동 커밋' 순서가 되면 이동이 빠진 발행본이 남는다.
  그 잠금은 호출자의 쓰기를 flush 하기 **전에** 잡는다(잠금 → 행 — 마감과 교착하지 않게, `lock_transfer_months`).
★ 판정·문구 조회는 실제 쓰기의 권한·병원 경계 검사를 통과한 범위만 받는다 — 다른 병원 병동 이름·발행 월이
  409 에 실리지 않게(선행 판정·미리보기·재분배 모두 호출부가 거른다).
★ 파견은 대상이 아니다(급한 파견은 발행 뒤에 잡히는 일이 흔하다 — 기존 결정 유지).
"""
from __future__ import annotations

import calendar
import json
import logging
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any, Callable, Iterable, List, Optional, Tuple

from fastapi import HTTPException
from sqlalchemy import and_, or_
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

TRANSFER_REASON = "병동이동"
#: 판정 대상인 '유효' 상태 — 진행·완료. 완료 처리(active→completed)는 기간이 그대로라 판정하지 않는다.
_LIVE_STATUSES = frozenset({"active", "completed"})

CODE_BLOCKED = "ISSUED_ROSTER_BLOCKS_TRANSFER"
CODE_UNKNOWN = "ISSUED_ROSTER_MONTH_UNKNOWN"
CODE_UNAVAILABLE = "ISSUED_ROSTER_CHECK_UNAVAILABLE"
_MSG_UNAVAILABLE = (
    "발행된 근무표 여부를 확인하지 못해 병동이동을 적용하지 않았습니다. 잠시 후 다시 시도해 주세요."
)
_ONE_DAY = timedelta(days=1)

#: 양 끝을 포함하는 날짜 구간. 끝이 None 이면 열린 끝(종료일 없는 병동이동).
Span = Tuple[date, Optional[date]]


@dataclass(frozen=True)
class TransferState:
    """판정 입력 — 배정 한 건의 모양. end 는 실제 종료 경계(병동이동은 expected_end_date)."""

    reason: Optional[str]
    status: Optional[str]
    source: Optional[str]
    target: Optional[str]
    start: Optional[date]
    end: Optional[date]

    @property
    def live(self) -> bool:
        """판정 대상인 '유효 병동이동'인가 — 사유가 병동이동이고 진행·완료 상태."""
        return (self.reason == TRANSFER_REASON and self.status in _LIVE_STATUSES
                and self.start is not None)

    @property
    def span(self) -> Span:
        return (self.start, self.end)


def state_of_row(row: Any) -> TransferState:
    """배정 행(ORM·응답 모델)을 판정 입력으로. 종료 경계는 `_effective_end_date` 와 같은 규칙."""
    reason = getattr(row, "reason", None)
    expected = getattr(row, "expected_end_date", None)
    end = expected if reason == TRANSFER_REASON else (getattr(row, "end_date", None) or expected)
    return TransferState(reason, getattr(row, "status", None), getattr(row, "source_group_id", None),
                         getattr(row, "target_group_id", None), getattr(row, "start_date", None), end)


@dataclass(frozen=True)
class GuardScope:
    """판정 범위 — 이 병동들의 발행된 달 중 이 구간들에 닿는 달을 막는다. 비면 판정하지 않는다."""

    groups: Tuple[str, ...] = ()
    spans: Tuple[Span, ...] = ()

    def __bool__(self) -> bool:
        return bool(self.groups and self.spans)

    @classmethod
    def of(cls, groups: Iterable[Optional[str]], spans: Iterable[Span]) -> "GuardScope":
        """병동 중복·빈 값을 걸러 범위를 만든다."""
        return cls(tuple(sorted({str(g) for g in groups if g})), tuple(spans))


def transfer_guard_scope(old: Optional[TransferState], new: Optional[TransferState]) -> GuardScope:
    """변경 전후 상태로 판정 범위를 만든다(생성·수정·취소·미리보기 공통).

    - 생성·되살리기(무효→유효): 새 기간 전체 · 출발·도착 병동.
    - 취소(유효→무효): 옛 기간 전체 · 출발·도착 병동. 취소도 발행된 달 표를 바꾼다(사용자 결정 2026-10-08).
      취소 API·수정의 status=cancelled·PATCH cancel·에이전트 취소가 모두 이 규칙을 탄다.
    - 수정(유효→유효): 병동이 같으면 **실제로 바뀐 날짜**(대칭차)만, 병동이 바뀌면 옛·새 기간 전체.
      값이 같으면(메모·target_*·사유 재전송) 빈 범위다 — 요청에 필드가 있는지가 아니라 값으로 본다.
    """
    was = old is not None and old.live
    now = new is not None and new.live
    if now and not was:
        return GuardScope.of((new.source, new.target), [new.span])
    if was and not now:
        return GuardScope.of((old.source, old.target), [old.span])
    if not (was and now):
        return GuardScope()
    if (old.source, old.target) != (new.source, new.target):
        return GuardScope.of((old.source, old.target, new.source, new.target), [old.span, new.span])
    return GuardScope.of((new.source, new.target), _span_symdiff(old.span, new.span))


def _ends_on_or_after(end: Optional[date], day: date) -> bool:
    """끝(None=열린 끝)이 day 이후까지 이어지는가."""
    return end is None or end >= day


def _span_symdiff(old: Span, new: Span) -> List[Span]:
    """두 구간의 대칭차(한쪽에만 있는 날)를 덮는 구간들. 겹치지 않으면 두 구간 전체."""
    (a1, b1), (a2, b2) = old, new
    if not (_ends_on_or_after(b1, a2) and _ends_on_or_after(b2, a1)):
        return [old, new]
    out: List[Span] = []
    if a1 != a2:
        out.append((min(a1, a2), max(a1, a2) - _ONE_DAY))
    if b1 != b2:
        if b1 is None or b2 is None:
            out.append(((b1 or b2) + _ONE_DAY, None))
        else:
            out.append((min(b1, b2) + _ONE_DAY, max(b1, b2)))
    return out


def _month_touches(year: int, month: int, spans: Iterable[Span]) -> bool:
    """그 달 [1일, 말일]이 구간 하나라도 건드리는가."""
    first = date(year, month, 1)
    last = date(year, month, calendar.monthrange(year, month)[1])
    return any(start <= last and _ends_on_or_after(end, first) for start, end in spans)


def json_dict(value: Any) -> dict:
    """JSON 컬럼 값을 dict 로. 문자열이면 파싱하고, dict 가 아니면 빈 dict."""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (ValueError, TypeError):
            return {}
    return value if isinstance(value, dict) else {}


def snapshot_year_month(meta: Any) -> Optional[Tuple[int, int]]:
    """발행본 meta_json 의 (연, 월). 못 읽으면 None.

    정본 조회(`meta.get('year') == year`)와 같은 기준 — 둘 다 정수(bool 제외)이고 월이 1~12 일 때만 인정한다.
    문자열 '2026' 은 정본 조회가 그 달 발행본으로 찾지 못하는 값이라 '읽을 수 없음'으로 본다.
    """
    d = json_dict(meta)
    y, m = d.get("year"), d.get("month")
    if not all(isinstance(v, int) and not isinstance(v, bool) for v in (y, m)):
        return None
    return (y, m) if date.min.year <= y <= date.max.year and 1 <= m <= 12 else None


@dataclass
class IssuedVerdict:
    """판정 결과. blocked=(병동, 연, 월) · unknown=(병동, 발행본 번호) — 연월을 읽을 수 없는 활성 발행본."""

    blocked: List[Tuple[str, int, int]] = field(default_factory=list)
    unknown: List[Tuple[str, Any]] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.blocked and not self.unknown

    def bad_groups(self) -> set:
        """막힌 병동 — 발행된 달이 걸리거나 연월을 읽을 수 없는 발행본이 있는 병동."""
        return {g for g, _, _ in self.blocked} | {g for g, _ in self.unknown}

    def merge(self, other: "IssuedVerdict") -> "IssuedVerdict":
        """두 판정을 합친다(여러 op 를 한 번에 거절할 때)."""
        return IssuedVerdict(sorted(set(self.blocked) | set(other.blocked)),
                             sorted(set(self.unknown) | set(other.unknown), key=str))


def _read_active_snapshots(db: Session, gids: Iterable[str]) -> List[Tuple[Any, str, Any]]:
    """활성 발행본 (번호, 병동, meta_json). 연월 컬럼으로 거르지 않는다(nullable · 정본은 meta)."""
    from db.models import IssuedRosterSnapshot as Snap

    rows = (
        db.query(Snap.snapshot_id, Snap.group_id, Snap.meta_json)
        .filter(Snap.group_id.in_(sorted(gids)), Snap.is_active_issued == True)  # noqa: E712
        .all()
    )
    return [(sid, str(gid), meta) for sid, gid, meta in rows]


def _verdict_from_rows(rows: List[Tuple[Any, str, Any]], scope: GuardScope) -> IssuedVerdict:
    """조회한 발행본을 범위에 비춰 판정한다. 연월을 못 읽으면 기간과 무관하게 unknown(fail-closed)."""
    groups = set(scope.groups)
    blocked, unknown = set(), set()
    for sid, gid, meta in rows:
        if gid not in groups:
            continue
        ym = snapshot_year_month(meta)
        if ym is None:
            unknown.add((gid, sid))
        elif _month_touches(ym[0], ym[1], scope.spans):
            blocked.add((gid, ym[0], ym[1]))
    return IssuedVerdict(sorted(blocked), sorted(unknown, key=str))


def _unavailable(exc: Exception) -> HTTPException:
    """확인 실패(503) — 로그를 남기고 사용자 문구로 돌려준다."""
    logger.error("[IssuedGuard] 발행본 확인 실패: %s", exc, exc_info=True)
    return HTTPException(status_code=503, detail={"code": CODE_UNAVAILABLE, "message": _MSG_UNAVAILABLE})


def _rewind(db: Session, savepoint: Any) -> None:
    """savepoint 를 되감는다. 되감기 자체가 실패하면(서버가 트랜잭션을 이미 날림 — 교착 희생 등) 전체 rollback.

    ★ SQLAlchemy 는 savepoint 되감기가 실패해도 그 savepoint 만 닫고 바깥 트랜잭션을 ACTIVE 로 둔다 —
      `db.is_active` 로는 서버 쪽 트랜잭션 소실을 알 수 없다. 되감기 실패로 판정한다(재분배 `_discard_attempt` 와 같은 규칙).
    """
    if savepoint is not None:
        try:
            savepoint.rollback()
            return
        except SQLAlchemyError:
            logger.warning("[IssuedGuard] savepoint 되감기 실패 — 트랜잭션 소실로 보고 전체 rollback", exc_info=True)
    db.rollback()


def _in_savepoint(db: Session, work: Callable[[], Any]) -> Any:
    """savepoint 안에서 work 를 돌린다. 실패하면 savepoint 를 되감고(`_rewind`) 예외를 그대로 다시 낸다.

    호출자가 먼저 flush 해야 한다 — savepoint 를 잡을 때의 flush 실패를 work 의 실패로 오인하지 않게.
    """
    savepoint = None
    try:
        savepoint = db.begin_nested()
        result = work()
        savepoint.commit()
    except Exception:  # noqa: BLE001 — 되감은 뒤 그대로 다시 낸다
        _rewind(db, savepoint)
        raise
    return result


def _guarded(db: Session, work: Callable[[], Any]) -> Any:
    """savepoint 안에서 조회를 돌린다. DB 오류면 savepoint 만 되감고 503 — 삼키지 않는다.

    ★ 먼저 flush 한다 — savepoint 를 잡을 때의 flush 실패(호출자 작업의 오류)를 '확인 실패'로 오인하지 않게.
    ★ savepoint 덕에 호출자의 미커밋 작업은 산다. 되감기마저 실패하면(트랜잭션 소실) 전체 rollback 뒤 503.
    """
    db.flush()
    try:
        return _in_savepoint(db, work)
    except SQLAlchemyError as exc:
        raise _unavailable(exc) from exc


def judge_many(db: Session, scopes: List[GuardScope]) -> List[IssuedVerdict]:
    """여러 범위를 **조회 1회**로 판정한다(잠금 없음 — 미리보기·선행 판정용). 조회 실패는 503."""
    gids = {g for s in scopes if s for g in s.groups}
    if not gids:
        return [IssuedVerdict() for _ in scopes]
    rows = _guarded(db, lambda: _read_active_snapshots(db, gids))
    return [_verdict_from_rows(rows, s) if s else IssuedVerdict() for s in scopes]


def judge_issued_months(db: Session, scope: GuardScope) -> IssuedVerdict:
    """한 범위를 잠금 없이 판정한다(미리보기·재분배·선행 판정). 쓰기는 `assert_transfer_not_on_issued`."""
    return judge_many(db, [scope])[0]


def _ym_from(model: Any, day: date):
    """(연, 월) >= day 의 달 — 인덱스를 탈 수 있는 or_/and_ 꼴."""
    return or_(model.year > day.year, and_(model.year == day.year, model.month >= day.month))


def _ym_until(model: Any, day: date):
    """(연, 월) <= day 의 달."""
    return or_(model.year < day.year, and_(model.year == day.year, model.month <= day.month))


def _publish_months_to_lock(db: Session, scope: GuardScope) -> List[Tuple[str, int, int]]:
    """범위에 닿는 (병동, 연, 월) 중 **근무표가 있는 달**(정렬). 마감은 그 달 근무표가 있어야 할 수 있다."""
    from db.models import Schedule

    ends = [end for _, end in scope.spans]
    conds = [Schedule.group_id.in_(list(scope.groups)),
             _ym_from(Schedule, min(start for start, _ in scope.spans))]
    if all(end is not None for end in ends):
        conds.append(_ym_until(Schedule, max(ends)))
    rows = db.query(Schedule.group_id, Schedule.year, Schedule.month).filter(*conds).distinct().all()
    return sorted({(str(g), int(y), int(m)) for g, y, m in rows
                   if y is not None and m is not None and _month_touches(int(y), int(m), scope.spans)})


def _lock_publish_months(db: Session, scope: GuardScope) -> None:
    """마감·철회와 같은 달 잠금을 정렬 순서로 잡는다(교착 방지). 시간 초과는 기존 409 그대로."""
    from services import schedule_versioning

    for gid, year, month in _publish_months_to_lock(db, scope):
        schedule_versioning.lock_month_publish(db, gid, year, month)


def lock_transfer_months(db: Session, scope: GuardScope) -> None:
    """범위에 닿는 마감 달 잠금을 **호출자의 쓰기를 flush 하기 전에** 잡는다(잠금 → 행).

    ★ 마감은 그 달 잠금을 쥔 채 그 병동 간호사·팀을 읽는다(RCSI 꺼짐). 호출자가 바꾼 행이 먼저 flush 돼 잠긴 채
      마감 달 잠금을 기다리면 서로 기다린다 — team_period 의 '잠금 → 행' 과 같은 이유. 그래서 savepoint(잡을 때
      flush 한다) 없이, autoflush 도 끄고 잡는다. 같은 트랜잭션이 다시 잡으면 기다리지 않는다(재진입).
    ★ savepoint 가 없어 DB 오류 뒤 트랜잭션 상태를 장담할 수 없다 — 전체 rollback 뒤 503. 시간 초과 409·잠금 오류 500 은 그대로.
    """
    if not scope:
        return
    try:
        with db.no_autoflush:
            _lock_publish_months(db, scope)
    except SQLAlchemyError as exc:
        db.rollback()
        raise _unavailable(exc) from exc


def assert_transfer_not_on_issued(db: Session, scope: GuardScope) -> None:
    """쓰기 정본 — 마감 달 잠금을 잡은 **뒤에** 판정한다. 막히면 409, 연월 판정 불가 409, 조회 실패 503.

    create·update·cancel 에서 어떤 쓰기보다 먼저 부른다(잠금 순서: 배정 행 → 마감 달 → 팀 구간).
    잠금은 flush 전에(`lock_transfer_months`), 발행본 조회는 flush 뒤 savepoint 안에서(`_guarded`) 한다.
    """
    if not scope:
        return
    lock_transfer_months(db, scope)
    rows = _guarded(db, lambda: _read_active_snapshots(db, scope.groups))
    raise_for_verdict(db, _verdict_from_rows(rows, scope))


def _group_names(db: Session, gids: Iterable[str]) -> dict:
    """막힌 병동의 이름(화면 문구용). 이름 조회가 실패하면 병동 ID 로 보인다(판정은 이미 끝났다).

    되감기마저 실패하면(트랜잭션 소실) `_rewind` 가 전체 rollback 한다 — 판정 경로와 같은 규칙.
    """
    from db.models import Group

    db.flush()
    try:
        rows = _in_savepoint(db, lambda: db.query(Group.group_id, Group.group_name)
                             .filter(Group.group_id.in_(sorted(gids))).all())
    except SQLAlchemyError:
        logger.warning("[IssuedGuard] 병동 이름 조회 실패 — ID 로 표시", exc_info=True)
        return {}
    return {str(g): n for g, n in rows if n}


def _blocked_detail(verdict: IssuedVerdict, names: dict) -> dict:
    """발행된 달에 걸린 경우의 응답(사용자 화면용 문구 + 막힌 달 목록)."""
    months = [{"group_id": g, "group_name": names.get(g) or g, "year": y, "month": m}
              for g, y, m in verdict.blocked]
    shown = ", ".join(f"{x['group_name']} {x['year']}년 {x['month']}월" for x in months[:3])
    more = f" 외 {len(months) - 3}건" if len(months) > 3 else ""
    message = (f"이미 발행된 근무표가 있어 병동이동을 적용할 수 없습니다 ({shown}{more}). "
               "발행을 취소한 뒤 다시 시도하거나, 발행본이 없는 달부터 이동해 주세요.")
    return {"code": CODE_BLOCKED, "message": message, "months": months}


def _unknown_detail(verdict: IssuedVerdict, names: dict) -> dict:
    """연월을 읽을 수 없는 활성 발행본이 있는 경우의 응답 — 재시도로 안 풀리는 데이터 결함이라 409."""
    gid, sid = verdict.unknown[0]
    more = f" 외 {len(verdict.unknown) - 1}건" if len(verdict.unknown) > 1 else ""
    message = (f"{names.get(gid) or gid} 발행본(번호 {sid}){more}의 연월을 확인할 수 없어 병동이동을 막았습니다. "
               "발행본 확인이 필요합니다 — 그 발행본을 철회한 뒤 다시 마감하거나 관리자에게 문의해 주세요.")
    snapshots = [{"group_id": g, "group_name": names.get(g) or g, "snapshot_id": s}
                 for g, s in verdict.unknown]
    return {"code": CODE_UNKNOWN, "message": message, "months": [], "snapshots": snapshots}


def verdict_detail(db: Session, verdict: IssuedVerdict) -> Optional[dict]:
    """판정 결과를 응답 detail({code, message, months})로. 통과면 None. 확정된 막힘을 먼저 보인다."""
    if verdict.ok:
        return None
    names = _group_names(db, verdict.bad_groups())
    if verdict.unknown:
        logger.error("[IssuedGuard] 연월을 읽을 수 없는 활성 발행본: %s", verdict.unknown)
    if verdict.blocked:
        return _blocked_detail(verdict, names)
    return _unknown_detail(verdict, names)


def raise_for_verdict(db: Session, verdict: IssuedVerdict) -> None:
    """막힌 판정이면 409 를 낸다. 통과면 아무것도 하지 않는다."""
    detail = verdict_detail(db, verdict)
    if detail is not None:
        raise HTTPException(status_code=409, detail=detail)


def preview_guard(db: Session, scope: GuardScope) -> dict:
    """미리보기용 판정 — 쓰기와 같은 범위·판정(잠금 없음). 예외를 내지 않는다(미리보기 계약).

    판정 불가·조회 실패도 blocked=True 로 돌려준다(fail-closed). 형태: {blocked, code, message, months}.
    """
    try:
        detail = verdict_detail(db, judge_issued_months(db, scope))
    except HTTPException as exc:          # 조회 실패(503) — 확인 못 했으니 막힌 것으로 보인다
        detail = dict(exc.detail) if isinstance(exc.detail, dict) else {"message": str(exc.detail)}
    if detail is None:
        return {"blocked": False, "code": None, "message": None, "months": []}
    return {"blocked": True, "code": detail.get("code"), "message": detail.get("message"),
            "months": detail.get("months") or []}
