"""하네스 실행 전 backend 확인 — db.client2(가짜)·SessionLocal·하네스 엔진이 sqlite 인가.

conftest 와 모듈 범위 실행 픽스처(`scenario_runs`·`minimal_runs`)가 함께 쓴다.
pytest 는 범위가 넓은 픽스처를 먼저 세우므로, 함수 범위 autouse 확인만으로는 모듈 범위
실행보다 늦다. 그래서 실행 픽스처가 **첫 줄에서 직접** 이 함수를 부른다.

app 모듈을 import 하지 않는다(수집 시점 import 는 가드 밖이다). `sys.modules` 만 본다.
"""

from __future__ import annotations

import sys

import pytest

GUARD_TAG = "[engine_input 가드]"


def backend_of(engine) -> str | None:
    """엔진(또는 None)의 backend 이름. url 이 없으면 None."""
    url = getattr(engine, "url", None)
    return url.get_backend_name() if url is not None else None


def _client2_or_fail():
    """가짜 db.client2 를 돌려준다. 없거나 실 모듈(msdb_manager 존재)이면 pytest.fail."""
    client2 = sys.modules.get("db.client2")
    if client2 is None:
        pytest.fail(f"{GUARD_TAG} db.client2 가 없다 — tests/conftest.py 의 가짜 모듈이 먼저 올라와야 한다")
    if getattr(client2, "msdb_manager", None) is not None:
        pytest.fail(f"{GUARD_TAG} db.client2 가 실 모듈이다(msdb_manager 존재) — 실 MSSQL 경로가 열려 있다")
    return client2


def assert_sqlite_backends(harness_engine) -> None:
    """db.client2(가짜)·SessionLocal bind·하네스 엔진이 sqlite 가 아니면 pytest.fail."""
    client2 = _client2_or_fail()
    session_local = getattr(client2, "SessionLocal", None)
    targets = {
        "db.client2.engine": getattr(client2, "engine", None),
        "db.client2.SessionLocal bind": getattr(session_local, "kw", {}).get("bind"),
        "하네스 engine": harness_engine,
    }
    for name, eng in targets.items():
        backend = backend_of(eng)
        if backend != "sqlite":
            pytest.fail(f"{GUARD_TAG} {name} backend={backend!r} — sqlite 만 허용")
