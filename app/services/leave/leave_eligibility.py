"""휴가 자동부여 대상자 시점 판정 (`nurse_leave_period`).

보건휴가·수면OFF 의 per-nurse 예외를 3-state 로 담는다.

    NULL = 자동판정(성별·N전담·임신 등 규칙대로)   ← 행이 없을 때와 동일
    0    = 제외(규칙상 적격이어도 주지 않음)
    1    = 강제포함(규칙상 부적격이어도 준다)

★ **행이 0개면 도입 전 동작 그대로다.** 백필하지 않는다 — 예외만 행을 만든다.

★ 판정 단위가 '월' 인 이유 — 보건휴가는 월 1개가 원칙이고 수면OFF 도 월 단위로 집계된다.
  구간이 월 중간에 시작·종료할 수 있으므로 **그 달에 걸친 모든 구간**을 본다.
  0 과 1 이 한 달에 함께 걸리면 **0(제외)이 이긴다** — 잘못 준 것보다 안 준 쪽이 복구 가능하다.

설계: docs/leave_auto_assignment_design.md §6 Step6
"""
from __future__ import annotations

import logging
from datetime import date
from typing import Any, Optional

from sqlalchemy.orm import Session

from db.models import NurseLeavePeriod
from services.nurse_period_resolver import fetch_periods, upsert_period

logger = logging.getLogger(__name__)

# 한 row 가 담는 값 컬럼 전체. upsert 시 명시하지 않은 형제는 직전 구간에서 승계한다.
LEAVE_VALUE_COLS = ("health_leave_eligible", "sleep_off_eligible", "pregnant")


def month_bounds(year: int, month: int) -> tuple[date, date]:
    """[월초, 다음달초) 반열림 — fetch_periods 규약과 같다."""
    return date(year, month, 1), date(year + (month == 12), (month % 12) + 1, 1)


def fetch_leave_flags(db: Session, nurse_ids, year: int, month: int) -> dict[str, dict]:
    """대상월에 걸친 구간을 간호사별로 접어 3-state 플래그로 만든다. 1쿼리.

    Returns:
        {nurse_id: {"health_leave_eligible": bool|None,
                    "sleep_off_eligible": bool|None,
                    "pregnant": bool|None}}
        행이 없는 간호사는 키 자체가 없다(= 전부 자동판정).
    """
    ids = [str(n) for n in (nurse_ids or []) if n]
    if not ids:
        return {}
    start, end = month_bounds(year, month)
    try:
        rows_map = fetch_periods(db, NurseLeavePeriod, ids, start, end)
    except Exception as exc:
        # ★ **테이블 부재만** 삼킨다 — 배포 순서가 뒤집혀(코드 먼저·DDL 나중) 근무표 생성
        #   전체가 죽는 것을 막기 위한 창구다. 권한·커넥션 오류까지 삼키면 설정이
        #   조용히 무시된 채 "왜 안 되지" 로 남는다.
        if not _is_missing_table(exc):
            raise
        # ★ 실패한 쿼리로 트랜잭션이 오염된 채 생성이 이어지면 뒤 쿼리가 연쇄로 죽는다.
        db.rollback()
        logger.warning(
            "[LeavePeriod] nurse_leave_period 미생성 — 전원 자동판정으로 진행. "
            "DDL(scripts/leave_auto_assignment_step6_nurse_leave_period.sql) 적용 필요."
        )
        return {}

    out: dict[str, dict] = {}
    for nid, rows in (rows_map or {}).items():
        folded = {c: _fold(rows, c) for c in LEAVE_VALUE_COLS}
        if any(v is not None for v in folded.values()):
            out[str(nid)] = folded
    return out


def _is_missing_table(exc: Exception) -> bool:
    """"테이블이 없다" 인가. MSSQL 208 / SQLite(테스트) 둘 다 본다."""
    msg = str(exc)
    if NurseLeavePeriod.__tablename__ not in msg:
        return False
    return "Invalid object name" in msg or "no such table" in msg


def _fold(rows: list, attr: str) -> Optional[bool]:
    """그 달에 걸친 구간들의 값을 하나로 접는다. 0 이 하나라도 있으면 0(제외 우선)."""
    seen: list[bool] = []
    for r in rows or []:
        v = getattr(r, attr, None)
        if v is not None:
            seen.append(bool(v))
    if not seen:
        return None
    return False not in seen          # 0 이 하나라도 있으면 제외가 이긴다


def is_health_leave_eligible(flags: Optional[dict], auto: bool) -> bool:
    """보건휴가 최종 판정. `flags` 는 fetch_leave_flags 의 해당 간호사 항목(없으면 None).

    Args:
        auto: 규칙 기반 자동판정 결과(성별·N전담·활성 등).
    """
    if not flags:
        return auto
    explicit = flags.get("health_leave_eligible")
    if explicit is not None:
        return bool(explicit)
    # 명시값이 없으면 임신 사실이 자동판정을 덮는다(임신 중 보건휴가 미부여 — 실측 규칙).
    if flags.get("pregnant") is True:
        return False
    return auto


def is_sleep_off_eligible(flags: Optional[dict], auto: bool = True) -> bool:
    """수면OFF 최종 판정. 임신은 관여하지 않는다(N 근무 자체가 배제 사유라 별개)."""
    if not flags:
        return auto
    explicit = flags.get("sleep_off_eligible")
    return auto if explicit is None else bool(explicit)


def upsert_leave_period(db: Session, nurse_id: str, valid_from: date, *,
                        source: str = "edited", **values: Any):
    """휴가 대상 구간 변경(close-before-open). **carry 를 호출부에 맡기지 않는다.**

    ★ 이 함수가 유일한 쓰기 창구다. `nurse_allowed_shift_period` 에서 났던 사고
      (형제 컬럼 승계 누락 → 값 소실)를 구조적으로 막기 위해, 명시하지 않은 형제
      컬럼을 **여기서 항상** 직전 구간에서 승계한다. 호출부가 carry_attrs 를 적을
      일이 없으므로 빠뜨릴 수도 없다.

    Args:
        values: LEAVE_VALUE_COLS 중 바꿀 것만. 예) pregnant=True
                None 을 명시하면 "자동판정으로 되돌린다"는 뜻이다(승계 대상 아님).
    """
    unknown = set(values) - set(LEAVE_VALUE_COLS)
    if unknown:
        raise ValueError(f"알 수 없는 컬럼: {sorted(unknown)}")
    if not values:
        raise ValueError("변경할 값이 없다")

    # ★ 2번째 이후 컬럼은 방금 연 구간을 **제자리 갱신**한다(같은 valid_from).
    #   upsert_period 는 내부에서 flush 하고 `cur.valid_from == valid_from` 이면 setattr 만
    #   하므로, 먼저 쓴 형제 컬럼이 유지된다(resolver:142-151 — allowed→fixed 와 같은 패턴).
    carry = [c for c in LEAVE_VALUE_COLS if c not in values]
    last = None
    for attr, val in values.items():
        last = upsert_period(
            db, NurseLeavePeriod, str(nurse_id), valid_from, attr, val,
            source=source, carry_attrs=carry,
        )
    return last
