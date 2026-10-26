"""engine_input 차단 가드가 실제로 막는지 확인한다(설계서 §7.1 · 사용자 지시 '부하 0').

대상 주소는 실제 호스트가 없는 문서용 대역만 쓴다 — TEST-NET-3(RFC 5737) ·
2001:db8::/32(RFC 3849) · `.invalid`(RFC 2606). 가드가 깨졌을 때도 실서버에 닿지 않게,
드라이버 확인은 127.0.0.1:1(로컬, 열린 포트 없음)로 한다.
"""

from __future__ import annotations

import socket
import sys

import pytest

_EXTERNAL_V4 = ("203.0.113.10", 1433)
_EXTERNAL_V6 = ("2001:db8::10", 1433, 0, 0)
_EXTERNAL_NAME = ("mssql.engine-input.invalid", 1433)
_GUARD = "engine_input 가드"


# ── ① 소켓 ──────────────────────────────────────────────────────────────


def test_socket_connect_external_ipv4_blocked():
    """외부 IPv4 로 connect 하면 RuntimeError."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.2)
        with pytest.raises(RuntimeError, match=_GUARD):
            sock.connect(_EXTERNAL_V4)


def test_socket_connect_ex_external_blocked():
    """connect_ex 도 같은 검사를 거친다(오류 코드로 새지 않는다)."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.2)
        with pytest.raises(RuntimeError, match=_GUARD):
            sock.connect_ex(_EXTERNAL_V4)


@pytest.mark.skipif(not socket.has_ipv6, reason="IPv6 미지원 환경")
def test_socket_connect_external_ipv6_blocked():
    """외부 IPv6 도 막는다."""
    with socket.socket(socket.AF_INET6, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.2)
        with pytest.raises(RuntimeError, match=_GUARD):
            sock.connect(_EXTERNAL_V6)


def test_create_connection_external_blocked():
    """create_connection 은 IP·호스트 이름 모두 DNS 조회 전에 막힌다."""
    for address in (_EXTERNAL_V4, _EXTERNAL_NAME):
        with pytest.raises(RuntimeError, match=_GUARD):
            socket.create_connection(address, timeout=0.2)


def test_dns_lookup_external_name_blocked():
    """바깥 이름의 DNS 조회도 막는다."""
    with pytest.raises(RuntimeError, match=_GUARD):
        socket.getaddrinfo(_EXTERNAL_NAME[0], 1433)
    with pytest.raises(RuntimeError, match=_GUARD):
        socket.gethostbyname(_EXTERNAL_NAME[0])


def test_loopback_and_unix_sockets_allowed():
    """루프백 TCP 와 AF_UNIX 는 통과한다(가드가 로컬까지 막지는 않는다)."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
        server.bind(("127.0.0.1", 0))
        server.listen(8)
        server.settimeout(1)
        port = server.getsockname()[1]
        for host in ("127.0.0.1", "localhost"):
            with socket.create_connection((host, port), timeout=1):
                accepted, _ = server.accept()
                accepted.close()
    left, right = socket.socketpair()
    with left, right:
        left.sendall(b"ok")
        assert right.recv(2) == b"ok"


# ── ② AWS ───────────────────────────────────────────────────────────────


def test_boto3_client_resource_session_blocked():
    """boto3.client·resource·Session 생성이 RuntimeError."""
    import boto3

    with pytest.raises(RuntimeError, match=_GUARD):
        boto3.client("sqs", region_name="ap-northeast-2")
    with pytest.raises(RuntimeError, match=_GUARD):
        boto3.resource("s3", region_name="ap-northeast-2")
    with pytest.raises(RuntimeError, match=_GUARD):
        boto3.Session(region_name="ap-northeast-2")


def test_botocore_api_call_on_precreated_client_blocked(monkeypatch):
    """가드 전에 만들어진 클라이언트(모듈 수준 생성)도 API 호출에서 막힌다."""
    import botocore.session

    # 실행 env 의 `AWS_PROFILE=`(빈 값)은 botocore 가 '이름이 빈 프로필'로 읽는다 — 이 테스트에서만 뺀다.
    monkeypatch.delenv("AWS_PROFILE", raising=False)
    client = botocore.session.get_session().create_client(
        "sqs", region_name="ap-northeast-2",
        aws_access_key_id="testing", aws_secret_access_key="testing",
    )
    with pytest.raises(RuntimeError, match="_make_api_call"):
        client.list_queues()


# ── ③ DB 드라이버 ───────────────────────────────────────────────────────


def test_pymssql_connect_blocked():
    """pymssql 은 C 소켓이라 ①을 우회한다 — 드라이버 입구에서 막는다."""
    import pymssql

    with pytest.raises(RuntimeError, match="pymssql"):
        pymssql.connect(server="127.0.0.1", port="1", user="x", password="x",
                        login_timeout=1)


def test_pymysql_connect_blocked():
    """pymysql 연결도 드라이버 입구에서 막는다."""
    import pymysql

    with pytest.raises(RuntimeError, match="pymysql"):
        pymysql.connect(host="127.0.0.1", port=1, user="x", password="x",
                        connect_timeout=1)


# ── ④ SQLAlchemy ────────────────────────────────────────────────────────


@pytest.mark.parametrize("url", [
    "mssql+pymssql://u:p@127.0.0.1:1/x",
    "mysql+pymysql://u:p@127.0.0.1:1/x",
])
def test_sqlalchemy_non_sqlite_engine_blocked(url):
    """sqlite 가 아닌 엔진은 DBAPI 연결 전에 막힌다."""
    from sqlalchemy import create_engine

    eng = create_engine(url)
    try:
        with pytest.raises(RuntimeError, match="SQLAlchemy"):
            eng.connect()
    finally:
        eng.dispose()


def test_harness_engines_are_sqlite(engine):
    """db.client2.engine·SessionLocal·하네스 엔진이 모두 sqlite 이고 실제로 쓸 수 있다."""
    from sqlalchemy import text

    client2 = sys.modules["db.client2"]
    # 전체 실행에서는 tests/test_real_llm_*.py 가 수집 시점에 `db.client2` 를 다른 sqlite 가짜로
    # 바꿔 끼운다(그 가짜엔 msdb_manager 속성 자체가 없다). 뜻은 '실 MSSQL 관리자가 없다'이다.
    assert getattr(client2, "msdb_manager", None) is None
    assert client2.engine.url.get_backend_name() == "sqlite"
    assert client2.SessionLocal.kw["bind"].url.get_backend_name() == "sqlite"
    assert engine.url.get_backend_name() == "sqlite"
    with engine.connect() as conn:
        assert conn.execute(text("SELECT 1")).scalar() == 1


def test_backend_check_fails_on_non_sqlite(sqlite_backend_check):
    """backend 검사는 sqlite 가 아닌 엔진을 받으면 pytest.fail 을 낸다."""
    from sqlalchemy import create_engine

    eng = create_engine("mssql+pymssql://u:p@127.0.0.1:1/x")
    try:
        with pytest.raises(pytest.fail.Exception, match="sqlite 만 허용"):
            sqlite_backend_check(eng)
    finally:
        eng.dispose()


def _fake_client2(engine, *, msdb_manager=None, bind=None):
    """db.client2 대역 — 확인 함수가 보는 속성(msdb_manager·engine·SessionLocal.kw)만 갖는다."""
    from types import SimpleNamespace

    session_local = SimpleNamespace(kw={"bind": bind if bind is not None else engine})
    return SimpleNamespace(msdb_manager=msdb_manager, engine=engine, SessionLocal=session_local)


def test_backend_check_fails_on_real_client2(monkeypatch, engine, sqlite_backend_check):
    """db.client2 에 실 MSSQL 관리자(msdb_manager)가 있으면 pytest.fail."""
    monkeypatch.setitem(sys.modules, "db.client2", _fake_client2(engine, msdb_manager=object()))
    with pytest.raises(pytest.fail.Exception, match="msdb_manager"):
        sqlite_backend_check(engine)


def test_backend_check_fails_on_non_sqlite_session_bind(monkeypatch, engine, sqlite_backend_check):
    """SessionLocal 이 sqlite 가 아닌 엔진에 묶여 있으면 pytest.fail(연결은 열지 않는다)."""
    from sqlalchemy import create_engine

    mssql = create_engine("mssql+pymssql://u:p@127.0.0.1:1/x")
    try:
        monkeypatch.setitem(sys.modules, "db.client2", _fake_client2(engine, bind=mssql))
        with pytest.raises(pytest.fail.Exception, match="SessionLocal bind"):
            sqlite_backend_check(engine)
    finally:
        mssql.dispose()


def test_extra_tables_created(engine_input_tables, engine):
    """상위 conftest 에 없는 테이블(보건휴가·나이트 주기)이 하네스 엔진에 있다."""
    from sqlalchemy import inspect

    names = set(inspect(engine).get_table_names())
    assert {t.name for t in engine_input_tables} <= names
    assert {"nurse_leave_period", "nurse_night_cycle"} <= names
