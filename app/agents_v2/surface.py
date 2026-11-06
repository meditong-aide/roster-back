"""에이전트 HTTP 표면 스위치(int-3a, 2026-10-08).

이 브랜치는 끝나면 dev 로 일괄 머지된다. 머지 뒤 기본 설정에서 에이전트 표면이 새로 열리지
않도록 마운트를 여기 한 곳에서 정한다. 값은 '1' 만 켬이고, 미설정·빈 문자열·그 밖의 값은 꺼짐이다.

- 대화(send·reset): ``AIDE_AGENT_CHAT=1`` 일 때만.
- 테스트 화면(``/agent/test/*``): ``AIDE_AGENT_TEST_ROUTER=1`` 이고, 운영 환경
  (``ENVIRONMENT=production|prod``)도 운영 roster DB(``EUN_DB_NAME=eun_roster``)도 아닐 때만.
  이 화면은 ``use_test_db=false`` 면 무인증으로 실 DB 를 읽는다(N66).
- 사용량 화면: ``AIDE_AGENT_USAGE_VIEW=1`` 일 때만(int-0b).
- whoami 는 늘 둔다. 위젯은 응답의 ``agent_enabled`` 로 자기를 숨겨야 한다(10-08 결정, 프론트 front-1).
- 배포 서버의 ``.env`` 는 배포 워크플로(deploy-ecr.yml heredoc)가 매번 새로 쓰므로, dev·prod 에서 켤 값은
  워크플로에 넣는다(설계 ops-1·Q62). 지금 워크플로에는 이 값들이 없다 = 기본 꺼짐.
"""

from __future__ import annotations

import os

from fastapi import FastAPI

_ON = "1"
_PRODUCTION_ENVS = frozenset({"production", "prod"})
_PRODUCTION_ROSTER_DB = "eun_roster"


def _flag(name: str) -> bool:
    return os.getenv(name, "").strip() == _ON


def chat_enabled() -> bool:
    """에이전트 대화(send·reset) 마운트 여부."""
    return _flag("AIDE_AGENT_CHAT")


def usage_view_enabled() -> bool:
    """사용량 화면 마운트 여부."""
    return _flag("AIDE_AGENT_USAGE_VIEW")


def _production_like() -> bool:
    env = os.getenv("ENVIRONMENT", "dev").strip().lower()
    db_name = os.getenv("EUN_DB_NAME", "").strip().lower()
    return env in _PRODUCTION_ENVS or db_name == _PRODUCTION_ROSTER_DB


def debug_page_enabled() -> bool:
    """테스트 화면 마운트 여부 — 켜는 값이 있어도 운영 환경·운영 DB 면 꺼짐."""
    return _flag("AIDE_AGENT_TEST_ROUTER") and not _production_like()


def mount_agent_routes(app: FastAPI) -> None:
    """에이전트 라우터를 스위치대로 붙인다. whoami 는 늘 붙인다."""
    from agents_v2.chat_router import router as chat_router
    from agents_v2.chat_router import whoami_router
    from agents_v2.test_chat_router import router as test_router
    from agents_v2.usage_router import router as usage_router

    app.include_router(whoami_router)
    if chat_enabled():
        app.include_router(chat_router)
    if debug_page_enabled():
        app.include_router(test_router)
    if usage_view_enabled():
        app.include_router(usage_router)
