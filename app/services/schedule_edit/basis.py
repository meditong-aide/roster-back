"""근무표 수정 판정 기준 사본 — 생성 당시 입력을 근무표별로 남기고, 수정할 때 그걸로 판정한다.

왜: 수정은 '그 근무표를 만들 때의 기준'으로 판정해야 한다(사용자 지시 11-02). 그런데 일별 인원
(`daily_shift`)·등급 최소(`roster_grade_config`)·팀 최소(`teams`)는 덮어쓰기라 이력이 없고, 간호사
속성·월 한도·원티드·전월 근무도 생성 뒤에 바뀔 수 있다. 그래서 생성 직후 `EditContext` 의 입력 부분을
한 벌 저장해 둔다. 근무 칸 자체는 저장하지 않는다 — 칸은 늘 지금 근무표(그 버전)에서 읽는다.

  · 기록: 생성 경로가 근무 칸을 저장한 직후 `record_edit_basis` 를 부른다(별도 세션 · 실패해도 생성 계속).
  · 승계: [새 버전으로 저장]이 `inherit_edit_basis` 로 원본 사본을 그대로 복사한다.
  · 사용: `context.load_edit_context` 가 사본이 있으면 `apply_basis` 로 채운다.
같이 남기는 참고값: 일자별 배정 인원(세로축)·가로축 분배 지표 — 생성 당시 근무표가 어땠는지.
"""

from __future__ import annotations

import logging
import time
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Dict, List, Optional

from sqlalchemy import inspect as sa_inspect
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from db.models import ScheduleEditBasis
from services.schedule_edit import distribution as dist
from services.schedule_edit.context import (
    EditContext, apply_extra_config, load_cells, load_edit_context, note_unreflected_fixed, refresh_derived,
    refresh_facts,
)

logger = logging.getLogger(__name__)

#: payload 형식 번호 — 칸을 더하거나 뜻을 바꾸면 올린다(옛 사본은 없는 칸을 지금 값으로 채운다)
BASIS_VERSION = 1
TABLE = ScheduleEditBasis.__tablename__
#: 칸 값이 아니라 '입력' 때문에 묶이는 칸 — 사본에 남겨 그대로 다시 묶는다
INPUT_FROZEN_REASONS = {"확정 원티드", "관할 밖(파견·이동·휴직·프리셉티)", "프리셉티 동행",
                        "프리셉티 동행(프리셉터)", "고정근무자"}
_table_ready = False
_checked_at: Optional[float] = None   # 0.0 이면 기동 직후 5분(단조 시계가 작을 때) 확인을 건너뛴다
#: 표가 없을 때 다시 확인하는 간격 — 공용 DB 라 생성·저장마다 카탈로그를 묻지 않는다
_NEGATIVE_TTL = 300.0


def table_ready(db: Session) -> bool:
    """표가 있나 — 있으면 계속, 없으면 5분 동안 캐시한다(DDL 이 나중에 들어와도 재기동 없이 잡힌다)."""
    global _table_ready, _checked_at
    if _table_ready or (_checked_at is not None and time.monotonic() - _checked_at < _NEGATIVE_TTL):
        return _table_ready
    _checked_at = time.monotonic()
    try:
        _table_ready = sa_inspect(db.get_bind()).has_table(TABLE)
    except SQLAlchemyError:
        _table_ready = False
    return _table_ready


# ── 사본 만들기 ─────────────────────────────────────────────────────────────


def to_payload(ctx: EditContext) -> Dict[str, Any]:
    return {
        "basis_version": BASIS_VERSION,
        "recorded_at": datetime.now().isoformat(timespec="seconds"),
        "config": _jsonable(ctx.config),
        "frozen_inputs": [[n, d, r] for (n, d), r in sorted(ctx.frozen.items()) if r in INPUT_FROZEN_REASONS],
        "forbidden": [[n, d, sorted(c)] for (n, d), c in sorted(ctx.forbidden.items())],
        "weekend_off": sorted(ctx.weekend_off), "weekend_days": sorted(ctx.weekend_days),
        "cov_min": ctx.cov_min, "cov_max": ctx.cov_max,
        "cov_exclude": sorted([n, d] for n, d in ctx.cov_exclude),
        "grade_of": ctx.grade_of, "grade_min": _jsonable(ctx.grade_min),
        "team_of": ctx.team_of, "team_min": {str(k): v for k, v in ctx.team_min.items()},
        "monthly_limits": {n: {k: (None if v is None else int(v)) for k, v in (lim or {}).items()}
                           for n, lim in ctx.monthly_limits.items()},
        "nurse_ids": list(ctx.nurse_ids),
        "single_n_ok": sorted(ctx.single_n_ok), "n_only": sorted(ctx.n_only), "single_shift": ctx.single_shift,
        "mutex": {n: {"partner_id": i.get("partner_id"), "days": sorted(i.get("days") or ())}
                  for n, i in ctx.mutex.items()},
        "n2n_gap": ctx.n2n_gap, "same_shift_k": ctx.same_shift_k, "isolated_work_hard": ctx.isolated_work_hard,
        "off_bounds": {n: list(b) for n, b in ctx.off_bounds.items()},
        "wishes": [[n, d, sorted(c)] for (n, d), c in sorted(ctx.wishes.items())],
        "tail": ctx.tail,
        "notes": list(ctx.notes),
        "daily_state": _daily_state(ctx),
        "distribution": dist.metrics(ctx, ctx.main),
    }


def _daily_state(ctx: EditContext) -> list:
    """일자별 배정 인원(세로축) — 기록 시점 근무표의 날짜별 D/E/N/M 수."""
    return [{c: sum(1 for n in ctx.nurse_ids if ctx.main[n][d] == c) for c in ("D", "E", "N", "M")}
            for d in range(ctx.days)]


def _jsonable(value):
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(v) for v in value]
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return value


def record_edit_basis(origin: Session, schedule_id: str, generation_notes: Optional[List[str]] = None) -> None:
    """생성 직후 사본을 남긴다. 별도 세션 — 실패해도 생성 경로를 막지 않는다(로그만).

    origin 은 생성에 쓰인 세션. 같은 DB 엔진으로 새 세션을 연다(전역 SessionLocal 을 쓰면 시험이 실제 DB 에 붙는다).
    generation_notes: DB 에 남지 않는 생성 조건(설정 덮어쓰기 등) — 사본은 생성 직후의 DB 값이라
    메모리에서만 쓴 완화는 담기지 않는다. 그 사실을 판정 안내에 남긴다.
    """
    db = Session(bind=origin.get_bind())
    try:
        if not table_ready(db):
            return
        ctx = load_edit_context(db, schedule_id, use_basis=False)
        payload = to_payload(ctx)
        payload["notes"] = list(payload["notes"]) + list(generation_notes or [])
        db.merge(ScheduleEditBasis(schedule_id=schedule_id, source_schedule_id=None,
                                   basis_version=BASIS_VERSION, payload=payload,
                                   created_at=datetime.now()))
        db.commit()
    except Exception:  # noqa: BLE001 — 근무표 생성은 이 기록 때문에 실패하면 안 된다
        db.rollback()
        logger.warning("[schedule_edit] 판정 기준 사본 기록 실패: %s", schedule_id, exc_info=True)
    finally:
        db.close()


def inherit_edit_basis(db: Session, new_schedule_id: str, source_schedule_id: str) -> None:
    """새 버전이 원본 사본을 이어받는다 — 호출한 쪽 트랜잭션 안, 세이브포인트로 감싸 실패해도 무해."""
    if not table_ready(db):
        return
    try:
        with db.begin_nested():
            row = db.get(ScheduleEditBasis, source_schedule_id)
            if row is not None:
                db.merge(ScheduleEditBasis(schedule_id=new_schedule_id, source_schedule_id=source_schedule_id,
                                           basis_version=row.basis_version, payload=row.payload,
                                           created_at=datetime.now()))
    except Exception:  # noqa: BLE001 — 주석대로 '실패해도 무해' 여야 한다(저장 트랜잭션을 깨지 않게)
        logger.warning("[schedule_edit] 판정 기준 사본 승계 실패: %s ← %s",
                       new_schedule_id, source_schedule_id, exc_info=True)


# ── 사본으로 판정하기 ────────────────────────────────────────────────────────


def read_basis(db: Session, schedule_id: str) -> Optional[Dict[str, Any]]:
    """사본 payload. 없거나 형식 번호가 다르면 None(지금 값으로 판정 + 안내)."""
    if not table_ready(db):
        return None
    row = db.get(ScheduleEditBasis, schedule_id)
    if row is None or not row.payload:
        return None
    if int(row.basis_version or 0) != BASIS_VERSION:
        logger.info("[schedule_edit] 사본 형식 %s ≠ %s — 지금 값으로 판정", row.basis_version, BASIS_VERSION)
        return None
    return dict(row.payload)


def apply_basis(db: Session, ctx: EditContext, payload: Dict[str, Any]) -> None:
    """사본 값으로 판정 재료를 채운다. 칸·근무 정의·이름은 지금 근무표에서 읽는다."""
    ctx.config = dict(payload.get("config") or {})
    apply_extra_config(db, ctx)
    ctx.use_mid = bool(ctx.config.get("use_mid"))
    shifts = load_cells(db, ctx)
    _apply_cells_rules(ctx, payload)
    _apply_people(ctx, payload)
    _apply_sequence(ctx, payload)
    # 기준은 사본, 사실은 지금 값도 — 그 뒤 새로 잡힌 파견·휴직·확정 원티드 칸은 막는다(검토 M3)
    rows = refresh_facts(db, ctx)
    _freeze_unknown(ctx, payload)
    _apply_wishes(ctx, payload)
    note_unreflected_fixed(db, ctx, shifts, rows)
    refresh_derived(ctx)
    ctx.notes.extend(payload.get("notes") or [])


def _apply_wishes(ctx: EditContext, payload: Dict[str, Any]) -> None:
    """원티드는 묶인 칸을 다 정한 뒤에 — 바꿀 수 없는 칸의 원티드는 보호할 필요가 없다."""
    ctx.wishes = {(n, d): set(c) for n, d, c in payload.get("wishes") or []
                  if n in ctx.main and ctx.is_free(n, d)}


def _freeze_unknown(ctx: EditContext, payload: Dict[str, Any]) -> None:
    """사본 뒤 명단에 들어온 사람은 개인 규칙(허용 근무형·고정근무 등)을 몰라 줄을 통째로 묶는다(검토 H1)."""
    known = set(payload.get("nurse_ids") or payload.get("grade_of") or [])
    unknown = [n for n in ctx.nurse_ids if n not in known]
    for nid in unknown:
        for day in range(ctx.days):
            ctx.frozen.setdefault((nid, day), "판정 기준 사본에 없는 간호사")
    if unknown:
        ctx.notes.append(f"근무표를 만든 뒤 명단에 들어온 {len(unknown)}명은 개인 규칙을 몰라 바꾸지 않습니다.")


def _apply_cells_rules(ctx: EditContext, payload: Dict[str, Any]) -> None:
    for n, d, reason in payload.get("frozen_inputs") or []:
        if n in ctx.main and 0 <= d < ctx.days:
            ctx.frozen[(n, d)] = reason
    ctx.forbidden = {(n, d): set(c) for n, d, c in payload.get("forbidden") or []}
    ctx.cov_min = list(payload.get("cov_min") or [])
    ctx.cov_max = list(payload.get("cov_max") or [])
    ctx.cov_exclude = {(n, d) for n, d in payload.get("cov_exclude") or []}


def _apply_people(ctx: EditContext, payload: Dict[str, Any]) -> None:
    ctx.weekend_off = set(payload.get("weekend_off") or [])
    ctx.weekend_days = set(payload.get("weekend_days") or [])
    ctx.grade_of = dict(payload.get("grade_of") or {})
    ctx.grade_min = dict(payload.get("grade_min") or {})
    ctx.team_of = dict(payload.get("team_of") or {})
    ctx.team_min = {int(k): v for k, v in (payload.get("team_min") or {}).items()}
    ctx.monthly_limits = dict(payload.get("monthly_limits") or {})
    ctx.single_n_ok = set(payload.get("single_n_ok") or [])
    ctx.n_only = set(payload.get("n_only") or [])
    ctx.single_shift = dict(payload.get("single_shift") or {})
    ctx.mutex = {n: {"partner_id": i.get("partner_id"), "days": set(i.get("days") or [])}
                 for n, i in (payload.get("mutex") or {}).items()}
    ctx.off_bounds = {n: tuple(b) for n, b in (payload.get("off_bounds") or {}).items()}


def _apply_sequence(ctx: EditContext, payload: Dict[str, Any]) -> None:
    ctx.n2n_gap = int(payload.get("n2n_gap") or 0)
    ctx.same_shift_k = int(payload.get("same_shift_k") or 0)
    ctx.isolated_work_hard = bool(payload.get("isolated_work_hard"))
    ctx.tail = {n: list(seq) for n, seq in (payload.get("tail") or {}).items()}
