"""골든 하네스 실행기 — 생성 서비스 함수를 직접 부르고 L2 뒤 GoldenStop 으로 멈춘다(설계서 §7.1).

진입점
  `services.roster_create_service.generate_roster_service`(:5150) — 동기 라우터
  `/roster_create/generate`(routers/roster_create.py:326)가 부르는 바로 그 서비스 함수다.
  추가 인자(treatment_ids·config_override·release 4종)는 라우터와 **같은 식**으로 req 에서 꺼낸다
  (`router_kwargs`, routers/roster_create.py:326-334). 비동기(SQS 전송)·worker·Lambda·
  main.app(TestClient)은 쓰지 않는다.
트랜잭션(흔적 0)
  하네스 연결 하나(상위 conftest 의 StaticPool 연결)에 바깥 트랜잭션을 연다. 시드 세션·실행
  세션은 `join_transaction_mode="create_savepoint"` 로 붙여, 서비스의 commit/rollback 은
  SAVEPOINT 에만 닿는다. 끝에 바깥 트랜잭션을 롤백한다(시나리오 DDL 포함).
  - pysqlite 는 DML 전까지 BEGIN 을 내지 않는다 → 시작하자마자 빈 DELETE 로 트랜잭션을 연다.
  - 실행 세션은 운영 SessionLocal(db/client2.py:230)과 같게 autoflush=False 다.
  - 시드 세션은 닫고 **새 세션**으로 실행한다 — 운영처럼 빈 identity map 에서 시작한다.
시나리오 패치
  `Scenario.patches` 는 실행 동안만 거는 MonkeyPatch 함수다(env·모듈 준비). 끝나면 원상 복구된다.
이중 실행 갈고리(`after_stop`, EI-9c — EI9_SPEC.md §2.5)
  `run_production`/`run_with_output` 에 선택 인자 `after_stop` 을 주면, 생성 서비스 호출이
  끝난 뒤(GoldenStop 포함·바깥 롤백 전) **같은 연결의 새 세션**을 열어
  `after_stop(session, req, user, kwargs, capture)` 를 부른다. 이 시점 DB 는 프로덕션 첫 시도
  시점(release·flush·materialize commit 반영, 생성 상태 기록 전)이다 — 대조 조건 ②. `req` 는
  `_invoke` 가 만든 **같은 객체**(materialize 뒤 `req.config_id`)이고, 쿼리 카운터는 이미 꺼져
  있다(`capture.queries.armed = False`, RunResult 는 이미 만들어진 뒤라 이후 호출이 골든 기록을
  바꾸지 않는다). 기본값 `None` 이면 이 자리를 아예 거치지 않아 지금과 바이트 같다(골든 무변경).
"""

from __future__ import annotations

import contextlib
import io
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import pytest
from sqlalchemy.orm import Session

from .canonical import layers_digest
from .capture import Capture, GoldenStop, SeparateSessionBlocked, SolveBlocked
from .clock import install_fixed_clock
from .ladder import RecorderFailure
from .mssql_shim import FIXED_NOW, install_mssql_shim

#: pysqlite 가 바깥 트랜잭션을 실제로 열게 하는 빈 DML(행에 닿지 않는다).
_OPEN_TRANSACTION_SQL = "DELETE FROM roster_config WHERE 1 = 0"
#: 라우터가 req 에서 꺼내 서비스에 넘기는 추가 인자(routers/roster_create.py:328-333).
ROUTER_FORWARDED = ("treatment_ids", "config_override", "weekend_off_release",
                    "monthly_limit_release", "banned_wanted_release", "allowed_shift_add")


@dataclass(frozen=True)
class Scenario:
    """합성 시나리오 하나 — 시드·요청·호출자·추가 인자·시나리오 DDL·패치·멈춤 지점."""

    name: str
    seed: Callable[[Session], None]
    make_request: Callable[[], object]
    make_user: Callable[[], object]
    ddl: tuple[str, ...] = ()
    call_kwargs: dict = field(default_factory=dict)
    patches: tuple[Callable[[pytest.MonkeyPatch], None], ...] = ()
    stop_at: str = "L2"
    description: str = ""


@dataclass
class RunResult:
    """프로덕션 경로 1회 실행 결과."""

    scenario: str
    outcome: str
    path: str
    layers: dict
    digest: str
    queries: dict
    solve_calls: int
    separate_sessions: int
    l2_calls_outside_l1: int
    error: str | None = None
    http_detail: object = None
    #: EI-10c — precheck 의 엔진 하드락 조회 횟수(골든 기록에는 넣지 않는다 — test_precheck_hard_lock_harness).
    precheck_hard_lock_calls: int = 0


@dataclass
class CapturedRun:
    """실행 결과 + 그동안의 표준출력·WARNING 이상 로그."""

    result: RunResult
    stdout: str
    logs: list[str]


#: EI-9c `after_stop` 갈고리의 모양 — `(session, req, user, router_kwargs, capture)`.
AfterStop = Callable[[Session, Any, Any, dict, Capture], None]


# ── 실행 ────────────────────────────────────────────────────────────────


def run_production(
    engine, scenario: Scenario, *, now: datetime = FIXED_NOW,
    after_stop: AfterStop | None = None,
) -> RunResult:
    """shim·시각 고정을 건 하네스 연결에서 시나리오를 시드하고 생성 서비스를 1회 부른다.

    `after_stop` 은 §2.5 그대로다 — 기본값 `None` 이면 호출되지 않아 지금과 바이트 같다.
    """
    with pytest.MonkeyPatch.context() as mp:
        shim = install_mssql_shim(engine, fixed_now=now)
        try:
            install_fixed_clock(mp, now)
            for patch in scenario.patches:
                patch(mp)
            return _run_in_transaction(engine, scenario, mp, after_stop=after_stop)
        finally:
            shim.uninstall()


class _LogCollector(logging.Handler):
    """WARNING 이상 로그를 모은다."""

    def __init__(self):
        super().__init__(level=logging.WARNING)
        self.records: list[str] = []

    def emit(self, record):
        self.records.append(f"{record.levelname} {record.name}: {record.getMessage()}")


def run_with_output(
    engine, scenario: Scenario, *, after_stop: AfterStop | None = None,
) -> CapturedRun:
    """run_production 을 표준출력·WARNING 로그를 모으며 돌린다."""
    collector = _LogCollector()
    root = logging.getLogger()
    root.addHandler(collector)
    buffer = io.StringIO()
    try:
        with contextlib.redirect_stdout(buffer):
            result = run_production(engine, scenario, after_stop=after_stop)
    finally:
        root.removeHandler(collector)
    return CapturedRun(result=result, stdout=buffer.getvalue(), logs=collector.records)


def _run_in_transaction(
    engine, scenario: Scenario, mp: pytest.MonkeyPatch, *,
    after_stop: AfterStop | None = None,
) -> RunResult:
    """바깥 트랜잭션 안에서 DDL → 시드 → 캡처 설치 → 호출 → (있으면) `after_stop`. 끝나면 전부 롤백한다.

    `after_stop` 은 `_invoke` 가 돌아온 뒤(생성 서비스가 끝난 뒤) · 바깥 롤백 전에, 같은 연결의
    새 세션에서 부른다(§2.5). `RunResult` 는 `_invoke` 안에서 이미 만들어졌으므로 이 호출이
    골든 기록(쿼리 카운터는 이미 꺼진 뒤)을 바꾸지 않는다.
    """
    conn = engine.connect()
    outer = conn.begin()
    capture = Capture(stop_at=scenario.stop_at)
    try:
        conn.exec_driver_sql(_OPEN_TRANSACTION_SQL)
        for ddl in scenario.ddl:
            conn.exec_driver_sql(ddl)
        _seed(conn, scenario)
        capture.install(mp, engine)
        result, req, user, kwargs = _invoke(conn, scenario, capture)
        if after_stop is not None:
            _run_after_stop(conn, after_stop, req, user, kwargs, capture)
        return result
    finally:
        capture.queries.detach()
        outer.rollback()
        conn.close()


def _run_after_stop(
    conn, after_stop: AfterStop, req: Any, user: Any, kwargs: dict, capture: Capture,
) -> None:
    """같은 연결의 새 세션(§2.5 — `join_transaction_mode=\"create_savepoint\"`)에서 `after_stop` 을 부른다."""
    session = Session(bind=conn, autoflush=False, join_transaction_mode="create_savepoint")
    try:
        after_stop(session, req, user, kwargs, capture)
    finally:
        session.close()


def _seed(conn, scenario: Scenario) -> None:
    """시드 세션으로 시나리오 행을 넣고(SAVEPOINT commit) 닫는다."""
    session = Session(bind=conn, join_transaction_mode="create_savepoint")
    try:
        scenario.seed(session)
        session.commit()
    finally:
        session.close()


def router_kwargs(req) -> dict:
    """라우터와 같은 식으로 req 에서 추가 인자를 꺼낸다(값이 비면 None)."""
    return {name: (getattr(req, name, None) or None) for name in ROUTER_FORWARDED}


def _invoke(conn, scenario: Scenario, capture: Capture) -> tuple[RunResult, Any, Any, dict]:
    """새 세션으로 생성 서비스를 부르고, (결과, req, user, kwargs) 를 돌려준다.

    `req`·`user`·`kwargs` 는 `_run_after_stop`(EI-9c `after_stop`) 가 쓴다 — 그 쪽이 없으면
    (기본값) 아무도 안 읽는 부가 반환값일 뿐이라 기존 동작은 바뀌지 않는다.
    """
    from services.roster_create_service import generate_roster_service

    req = scenario.make_request()
    user = scenario.make_user()
    kwargs = {**router_kwargs(req), **scenario.call_kwargs}
    db = Session(bind=conn, autoflush=False, join_transaction_mode="create_savepoint")
    capture.queries.armed = True
    try:
        outcome, error, http_detail = _call(generate_roster_service, req, user, db, kwargs)
    finally:
        capture.queries.armed = False
        db.close()
    path = classify_path(outcome, capture.queries.marks)
    result = RunResult(
        scenario=scenario.name, outcome=outcome, path=path, layers=capture.layers,
        digest=layers_digest(capture.layers), queries=capture.queries.summary(path),
        solve_calls=capture.solve_calls, separate_sessions=capture.separate_sessions,
        l2_calls_outside_l1=capture.l2_calls_outside_l1, error=error, http_detail=http_detail,
        precheck_hard_lock_calls=capture.precheck_hard_lock_calls)
    return result, req, user, kwargs


def _call(service, req, user, db, call_kwargs: dict) -> tuple[str, str | None, object]:
    """서비스를 불러 (결과 종류, 오류 문자열, HTTP detail) 을 돌려준다."""
    from fastapi import HTTPException

    try:
        service(req, user, db, **call_kwargs)
    except GoldenStop:
        return "golden_stop", None, None
    except SolveBlocked as exc:
        return "solve_blocked", str(exc), None
    except SeparateSessionBlocked as exc:
        return "separate_session_blocked", str(exc), None
    except RecorderFailure as exc:
        return "recorder_failure", str(exc), None
    except HTTPException as exc:
        return "http_exception", f"HTTP {exc.status_code}", exc.detail
    except Exception as exc:  # noqa: BLE001 — 하네스는 실패 종류를 기록만 한다
        return "exception", f"{type(exc).__name__}: {exc}", None
    return "returned", None, None


def classify_path(outcome: str, marks: dict[str, int]) -> str:
    """경로 분류(§7.1 쿼리 카운터의 '경로별').

    첫 시도까지 · 사다리 모드 전체(엔진을 지난 뒤 UNRECOVERABLE) · 후처리까지(정상 반환) ·
    나이트 차단 · precheck 차단 · 오류.
    """
    if outcome == "golden_stop" and "L1" in marks:
        return "first_attempt"
    if outcome == "http_exception" and "L1" in marks:
        return "ladder_unrecoverable"
    if outcome == "returned" and "L1" in marks:
        return "post_complete"
    if outcome == "http_exception" and "L0p" in marks:
        return "night_block"
    if outcome == "http_exception" and "L0" in marks:
        return "precheck_block"
    return "error"
