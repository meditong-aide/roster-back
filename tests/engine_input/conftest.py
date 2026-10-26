"""tests/engine_input 공용 conftest — 외부 접촉 차단 가드 + 하네스 공용 픽스처.

왜 가드가 먼저인가
  운영 DB 서버는 21개 DB 가 한 인스턴스에 얹힌 공용 온프렘 서버다. 하네스가 연결 하나만
  잘못 열어도 그룹웨어까지 함께 멈출 수 있다. 그래서 이 패키지의 테스트가 돌기 전에
  바깥으로 나가는 길을 전부 막고, 막혔는지를 test_guard.py 가 확인한다.

막는 것 (루프백·AF_UNIX·sqlite 만 통과)
  ① 소켓   socket.socket.connect / connect_ex · socket.create_connection ·
             socket.getaddrinfo / gethostbyname(DNS 조회도 바깥 접촉이다)
  ② AWS     boto3.client · boto3.resource · boto3.Session 생성 +
             botocore BaseClient._make_api_call(import 때 이미 만들어진 클라이언트까지 —
             app/main.py·routers/events.py·routers/roster_create.py 가 모듈 수준에서 만든다)
  ③ DB 드라이버  pymssql.connect(FreeTDS 가 C 소켓을 써 ①을 우회한다) · pymysql 연결
  ④ SQLAlchemy  sqlite 가 아닌 엔진의 연결(Engine.raw_connection) 거부 +
             db.client2.engine·SessionLocal·하네스 엔진 backend 가 sqlite 가 아니면 pytest.fail
             (backend_check.assert_sqlite_backends)

backend 확인이 도는 자리 — 프로덕션 실행보다 반드시 앞
  pytest 는 범위가 넓은 픽스처를 먼저 세운다. 함수 범위 autouse 확인은 모듈 범위 실행
  픽스처(`scenario_runs`·`minimal_runs`)보다 **늦게** 돈다. 그래서 세 자리에서 확인한다.
    (1) package 범위 `_sqlite_backends_verified` — 테이블 DDL(`engine_input_tables`) 앞
    (2) 모듈 범위 실행 픽스처 첫 줄 — 프로덕션 실행 직전(직접 호출)
    (3) 함수 범위 autouse `_require_sqlite_backends` — 매 테스트 앞

범위 규칙
  - 차단 가드는 package 범위 autouse 다. 패키지가 끝나면 MonkeyPatch 가 원상 복구한다
    (다른 디렉터리 테스트에는 영향이 없다).
  - 이 패키지의 픽스처는 package 이하 범위로 둔다. session 범위 픽스처는 가드보다 먼저
    돌아 가드 밖에 놓인다(상위 conftest 의 `engine` 은 sqlite create_all 만 한다).
  - app 모듈은 테스트·픽스처 안에서 import 한다. 수집 시점 import 는 가드 밖이다.

골든 없음 집계(D29) — `pytest_terminal_summary` 가 사유에 `golden_gate.SKIP_PREFIX` 가 든 건너뜀(수집 단계 포함) 수를
  1건 이상일 때만 한 줄로 찍는다(골든은 커밋하지 않는 로컬 전용 · `REQUIRE_GOLDEN=1` 이면 그 건너뜀이 실패로 바뀐다).
  이 conftest 는 engine_input 을 돌릴 때만 올라온다 — `tests/engine_contract` 만 돌리면 D29 건너뜀이 있어도 집계 줄이 없다
  (사유는 `-rs` 로 본다).
"""

from __future__ import annotations

import ipaddress
import socket

import pytest

from .backend_check import GUARD_TAG as _GUARD_TAG
from .backend_check import assert_sqlite_backends
from .golden_gate import SKIP_PREFIX as _D29_SKIP_PREFIX

_AF_UNIX = getattr(socket, "AF_UNIX", None)
_LOOPBACK_NAMES = frozenset({"localhost"})

#: 설계서 §5.3 EI-1 — 상위 conftest `_REQUIRED_TABLES` 에 없어서 이 패키지가 따로 만드는 테이블.
_EXTRA_MODEL_NAMES = ("NurseLeavePeriod", "NurseNightCycle")


class ExternalAccessBlocked(RuntimeError):
    """engine_input 하네스가 바깥(실 DB·AWS·외부망)에 닿으려 했다."""


#: 가드가 막은 시도의 기록(시각 순). app 코드는 `except Exception` 으로 예외를 삼키는 곳이 많아
#: 예외만 보고는 시도를 놓친다. 그래서 막을 때마다 여기 남기고, 시나리오 테스트가 실행 전후
#: 길이를 비교해 '외부 연결 시도 0' 을 확인한다(`guard_log` 픽스처).
BLOCKED_ATTEMPTS: list[str] = []


# ── 판정 ────────────────────────────────────────────────────────────────


def _is_loopback_host(host) -> bool:
    """host 가 루프백(127.0.0.0/8 · ::1 · localhost)이면 참이다."""
    if isinstance(host, bytes):
        host = host.decode("ascii", "replace")
    if not isinstance(host, str):
        return False
    name = host.strip().strip("[]").split("%", 1)[0].lower()
    if name in _LOOPBACK_NAMES:
        return True
    try:
        return ipaddress.ip_address(name).is_loopback
    except ValueError:
        return False


def _blocked(what: str, target=None) -> ExternalAccessBlocked:
    """차단 예외를 만들고 시도를 기록한다(메시지에 무엇을 어디로 하려 했는지 남긴다)."""
    suffix = f": {target!r}" if target is not None else ""
    message = f"{_GUARD_TAG} {what} 차단{suffix} — 루프백·AF_UNIX·sqlite 만 허용"
    BLOCKED_ATTEMPTS.append(message)
    return ExternalAccessBlocked(message)


def _check_socket_target(family, address, what: str) -> None:
    """소켓 연결 대상이 루프백·AF_UNIX 가 아니면 ExternalAccessBlocked 를 낸다."""
    if _AF_UNIX is not None and family == _AF_UNIX:
        return
    if isinstance(address, tuple) and address and _is_loopback_host(address[0]):
        return
    raise _blocked(what, address)


# ── ① 소켓 ──────────────────────────────────────────────────────────────


def _guard_socket_method(original, what: str):
    """socket.socket 의 connect 류 메서드를 대상 검사로 감싼다."""
    def guarded(self, address, *args, **kwargs):
        _check_socket_target(self.family, address, what)
        return original(self, address, *args, **kwargs)
    guarded.__name__ = getattr(original, "__name__", what)
    return guarded


def _guard_create_connection(original):
    """socket.create_connection 을 DNS 조회 전에 대상 검사로 감싼다."""
    def guarded(address, *args, **kwargs):
        _check_socket_target(socket.AF_INET, address, "socket.create_connection")
        return original(address, *args, **kwargs)
    return guarded


def _guard_resolver(original, what: str):
    """getaddrinfo·gethostbyname — 루프백이 아닌 이름 조회(DNS)를 막는다."""
    def guarded(host, *args, **kwargs):
        if host not in (None, "", b"") and not _is_loopback_host(host):
            raise _blocked(what, host)
        return original(host, *args, **kwargs)
    return guarded


def _install_socket_guard(mp: pytest.MonkeyPatch) -> None:
    """① 소켓 계층 차단."""
    for name in ("connect", "connect_ex"):
        original = getattr(socket.socket, name)
        mp.setattr(socket.socket, name, _guard_socket_method(original, f"socket.{name}"))
    mp.setattr(socket, "create_connection", _guard_create_connection(socket.create_connection))
    mp.setattr(socket, "getaddrinfo", _guard_resolver(socket.getaddrinfo, "socket.getaddrinfo"))
    mp.setattr(socket, "gethostbyname", _guard_resolver(socket.gethostbyname, "socket.gethostbyname"))


# ── ② AWS ───────────────────────────────────────────────────────────────


def _raiser(what: str):
    """부르면 곧바로 차단 예외를 내는 대역 함수를 만든다."""
    def blocked(*_args, **_kwargs):
        raise _blocked(what)
    blocked.__name__ = f"blocked_{what.replace('.', '_')}"
    return blocked


def _blocked_session_class(base: type) -> type:
    """boto3.Session 대역 — isinstance 는 살리고 생성만 막는다."""
    class BlockedSession(base):
        def __init__(self, *_args, **_kwargs):  # noqa: D401 — 생성 차단
            raise _blocked("boto3.Session")
    return BlockedSession


def _install_aws_guard(mp: pytest.MonkeyPatch) -> None:
    """② boto3 생성 3종 + botocore API 호출 차단."""
    import boto3
    import botocore.client

    mp.setattr(boto3, "client", _raiser("boto3.client"))
    mp.setattr(boto3, "resource", _raiser("boto3.resource"))
    mp.setattr(boto3, "Session", _blocked_session_class(boto3.session.Session))
    mp.setattr(botocore.client.BaseClient, "_make_api_call",
               _raiser("botocore BaseClient._make_api_call"))


# ── ③ DB 드라이버 ───────────────────────────────────────────────────────


def _install_db_driver_guard(mp: pytest.MonkeyPatch) -> None:
    """③ pymssql(C 소켓이라 ①을 우회) · pymysql 연결 차단."""
    import pymssql
    import pymysql
    import pymysql.connections

    mp.setattr(pymssql, "connect", _raiser("pymssql.connect"))
    if hasattr(pymssql, "_mssql"):
        mp.setattr(pymssql._mssql, "connect", _raiser("pymssql._mssql.connect"))
    mp.setattr(pymysql.connections.Connection, "connect",
               _raiser("pymysql Connection.connect"))


# ── ④ SQLAlchemy ────────────────────────────────────────────────────────


def _guard_raw_connection(original):
    """sqlite 가 아닌 엔진은 DBAPI 연결을 열기 전에 막는다."""
    def raw_connection(self):
        if self.dialect.name != "sqlite":
            raise _blocked(f"SQLAlchemy 연결(backend={self.dialect.name})", str(self.url))
        return original(self)
    return raw_connection


def _install_sqlalchemy_guard(mp: pytest.MonkeyPatch) -> None:
    """④ Engine.raw_connection 을 sqlite 전용으로 묶는다."""
    from sqlalchemy.engine.base import Engine

    mp.setattr(Engine, "raw_connection", _guard_raw_connection(Engine.raw_connection))


# ── 픽스처 ──────────────────────────────────────────────────────────────


@pytest.fixture(scope="package", autouse=True)
def _block_external_access():
    """이 패키지 전체에 외부 접촉 차단을 건다(패키지가 끝나면 원상 복구)."""
    with pytest.MonkeyPatch.context() as mp:
        _install_socket_guard(mp)
        _install_aws_guard(mp)
        _install_db_driver_guard(mp)
        _install_sqlalchemy_guard(mp)
        yield


@pytest.fixture(scope="package")
def _sqlite_backends_verified(_block_external_access, engine):
    """package 범위 backend 확인 — 이 패키지의 DDL·실행 픽스처는 이것 뒤에 선다."""
    assert_sqlite_backends(engine)


@pytest.fixture(autouse=True)
def _require_sqlite_backends(_sqlite_backends_verified, engine):
    """매 테스트 전에 db.client2·하네스 엔진이 sqlite 인지 확인한다.

    함수 범위라 모듈 범위 실행 픽스처보다 늦다 — 실행 앞 확인은 (1)·(2) 가 맡는다.
    """
    assert_sqlite_backends(engine)


@pytest.fixture(scope="package")
def guard_log() -> list[str]:
    """가드가 막은 시도 기록(공유 리스트). 테스트는 실행 전 길이를 잡아 두고 뒤에 비교한다."""
    return BLOCKED_ATTEMPTS


@pytest.fixture
def sqlite_backend_check():
    """backend 검사 함수 자체를 돌려준다(검사가 실패를 내는지 test_guard 가 확인한다)."""
    return assert_sqlite_backends


@pytest.fixture(scope="package")
def engine_input_tables(_sqlite_backends_verified, engine):
    """상위 conftest 에 없는 테이블을 같은 엔진에 만든다(이미 있으면 건너뜀).

    끝나면 **이 픽스처가 만든 것만** 지운다 — 다른 디렉터리 테스트의 스키마 전제를
    바꾸지 않기 위해서다.
    """
    import db.models as models
    from sqlalchemy import inspect

    tables = [getattr(models, name).__table__ for name in _EXTRA_MODEL_NAMES]
    existing = set(inspect(engine).get_table_names())
    created = [t for t in tables if t.name not in existing]
    tables[0].metadata.create_all(bind=engine, tables=created, checkfirst=True)
    yield tables
    tables[0].metadata.drop_all(bind=engine, tables=created, checkfirst=True)


# ── 골든 없음 집계(D29) ─────────────────────────────────────────────────


def _skip_reason_text(report) -> str:
    """건너뜀 보고의 사유 문자열 — 테스트·수집 보고 모두 `longrepr` 가 `(경로, 줄, 사유)` 튜플이다."""
    longrepr = report.longrepr
    if isinstance(longrepr, tuple) and len(longrepr) == 3:
        return str(longrepr[2])
    return str(longrepr)


def pytest_terminal_summary(terminalreporter):
    """골든이 없어 D29 사유로 건너뛴 수를 한 줄로 알린다(0건이면 아무것도 찍지 않는다)."""
    skipped = terminalreporter.stats.get("skipped", [])
    count = sum(1 for report in skipped if _D29_SKIP_PREFIX in _skip_reason_text(report))
    if count:
        terminalreporter.write_line(
            f"골든 로컬 전용(D29): 골든이 없어 {count}건 건너뜀 — REQUIRE_GOLDEN=1 이면 실패로 바뀐다")
