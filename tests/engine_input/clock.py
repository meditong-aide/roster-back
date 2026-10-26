"""골든 하네스 시각 고정 — 설계서 §7.1 '시각 고정'(freezegun 은 의존성에 없다).

생성 경로(`generate_roster_service` → 첫 시도 직전)에서 '지금'을 읽는 지점을 코드에서 찾아
그 자리만 바꾼다. 모듈 전역 `date`/`datetime` 을 바꾸는 곳은 대역 클래스를 쓴다.
  - 대역의 생성자·클래스메서드는 **진짜 date/datetime** 을 돌려준다(`__new__` 위임).
  - `isinstance(x, 대역)` 은 진짜 클래스로 판정한다(메타클래스) — 바꾼 모듈의 타입 판정이 그대로다.

고정하는 지점(경로:줄 — 49f14bd 기준)
  - assignment_service.py:1789 `flush_pending_transfers` 의 `date.today()`(생성마다 지남)
    · 같은 모듈 :1725·:1743 `_apply_target_profile_reset`(병동이동 발효 때)
  - nurse_period_resolver.py:24 `_today()`(release 경로 `upsert_period` 캐시 투영 :177)
  - preceptee_period.py:25 · mutual_exclusion_period.py:21 `_today()`(기간 쓰기 경로)
  - roster_create_service.py:529 `_build_generate_snapshot` 의 `datetime.now()`(차단·실패 기록)
  - SQL `func.now()` — 모델 기본값·onupdate(db/models.py 57곳)·쿼리. SQLite 는 CURRENT_TIMESTAMP
    (UTC 벽시계)로 컴파일한다 → mssql_shim 이 실행 직전 `GETDATE()` 로 재작성한다(여기서 패치 안 함).
    ★ 컴파일러(SQLiteCompiler.visit_now_func)를 바꾸는 안은 버렸다 — ORM INSERT/UPDATE 는 연결의
      compiled_cache 옵션이 아니라 매퍼 자체 캐시를 써서, GETDATE() 로 컴파일된 문장이 하네스
      밖 테스트로 샜다(test_capture 의 복구 테스트가 잡았다).
  - SQL `GETDATE()` — mssql_shim 사용자 함수(고정 시각).
고정하지 않는 것(캡처·순서에 닿지 않음)
  - push_outbox_service.py:50 `_now()`(병동이동 발효 알림 대기열 시각) — 이 모듈은 가짜
    `db.client2` 에 `EUN_DB_NAME` 이 없어 테스트 환경에서 import 되지 않는다(:24). 그래서 여기서
    고정하지 않고, 병동이동 시나리오 S6 만 실행 동안 모듈을 올리면서 함께 고정한다
    (scenario_defs.enable_push_outbox). 알림 행 시각은 캡처 층에 없다.
  - roster_create_service.py:8315 근무표 id `uuid4` · :6571 shadow run_id(env 꺼짐이면 미사용)
  - cp_sat_basic.py:2053·:2291 solve 시드 — L2 뒤라 닿지 않는다 · `Timer` 의 time.time(출력만)
  - holiday_pack.py:16 `this_year`(import 시각, 쓰는 곳 없음)
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date as _RealDate
from datetime import datetime as _RealDateTime

import pytest

from .mssql_shim import FIXED_NOW

FIXED_TODAY = FIXED_NOW.date()


class _RealTypeMeta(type):
    """isinstance·issubclass 를 진짜 클래스(`_real`)로 판정하는 메타클래스."""

    def __instancecheck__(cls, obj):
        return isinstance(obj, cls._real)

    def __subclasscheck__(cls, sub):
        return issubclass(sub, cls._real)


def make_fixed_date(today: _RealDate) -> type:
    """`today()` 만 고정한 date 대역 클래스를 만든다."""

    class FixedDate(_RealDate, metaclass=_RealTypeMeta):
        _real = _RealDate

        def __new__(cls, *args, **kwargs):
            return _RealDate(*args, **kwargs)

        @classmethod
        def today(cls):
            return _RealDate(today.year, today.month, today.day)

    return FixedDate


def make_fixed_datetime(now: _RealDateTime) -> type:
    """`now()`·`today()`·`utcnow()` 를 고정한 datetime 대역 클래스를 만든다(naive 로컬 = now)."""

    class FixedDateTime(_RealDateTime, metaclass=_RealTypeMeta):
        _real = _RealDateTime

        def __new__(cls, *args, **kwargs):
            return _RealDateTime(*args, **kwargs)

        @classmethod
        def now(cls, tz=None):
            return now if tz is None else now.replace(tzinfo=tz)

        @classmethod
        def today(cls):
            return now

        @classmethod
        def utcnow(cls):
            return now

    return FixedDateTime


@dataclass(frozen=True)
class ClockPoint:
    """고정한 지점 하나 — 모듈·속성·근거(경로:줄)."""

    module: str
    attr: str
    where: str


CLOCK_POINTS: tuple[ClockPoint, ...] = (
    ClockPoint("services.assignment_service", "date", "assignment_service.py:1789·:1725·:1743"),
    ClockPoint("services.nurse_period_resolver", "_today", "nurse_period_resolver.py:24(:177)"),
    ClockPoint("services.preceptee_period", "_today", "preceptee_period.py:25"),
    ClockPoint("services.mutual_exclusion_period", "_today", "mutual_exclusion_period.py:21"),
    ClockPoint("services.roster_create_service", "datetime", "roster_create_service.py:529"),
)


def _replacement(point: ClockPoint, now: _RealDateTime):
    """지점별 대역 값."""
    today = now.date()
    if point.attr == "date":
        return make_fixed_date(today)
    if point.attr == "datetime":
        return make_fixed_datetime(now)
    if point.attr == "_today":
        return lambda: today
    raise ValueError(f"모르는 시각 지점: {point}")


def install_fixed_clock(mp: pytest.MonkeyPatch, now: _RealDateTime = FIXED_NOW) -> tuple[ClockPoint, ...]:
    """CLOCK_POINTS 를 고정 시각으로 바꾼다(mp 가 끝나면 원상 복구). 바꾼 지점을 돌려준다."""
    import importlib

    for point in CLOCK_POINTS:
        module = importlib.import_module(point.module)
        mp.setattr(module, point.attr, _replacement(point, now))
    return CLOCK_POINTS
