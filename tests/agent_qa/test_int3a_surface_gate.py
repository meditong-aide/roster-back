"""int-3a — 에이전트 HTTP 표면 스위치와 일반 간호사 미제공(2026-10-08).

이 브랜치는 끝나면 dev 로 일괄 머지된다. 머지 뒤 기본 설정에서 에이전트 대화·테스트 화면·사용량
화면이 새로 열리지 않아야 하고, 일반 간호사는 에이전트를 쓸 수 없어야 한다(서버 거절 + whoami).
실 LLM·실 DB 없음.
"""
from __future__ import annotations

import asyncio

import pytest
from agents_v2 import chat_router, surface
from db.models import Nurse
from fastapi import FastAPI

from tests.agent_qa.harness import make_auth_user

_SWITCHES = ("AIDE_AGENT_CHAT", "AIDE_AGENT_TEST_ROUTER", "AIDE_AGENT_USAGE_VIEW")
_SEND = "/api/agent/chat/send"
_RESET = "/api/agent/chat/reset"
_WHOAMI = "/api/agent/chat/whoami"


def _paths(monkeypatch, **env) -> set[str]:
    for name in _SWITCHES + ("ENVIRONMENT", "EUN_DB_NAME"):
        monkeypatch.delenv(name, raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    app = FastAPI()
    surface.mount_agent_routes(app)
    return {getattr(r, "path", "") for r in app.routes}


def _has_test_page(paths: set[str]) -> bool:
    return any(p.startswith("/agent/test") for p in paths)


def _has_usage(paths: set[str]) -> bool:
    return any(p.startswith("/api/agent/usage") for p in paths)


# ── 마운트 스위치 ─────────────────────────────────────────────


def test_default_mounts_only_whoami(monkeypatch):
    paths = _paths(monkeypatch)
    assert _WHOAMI in paths
    assert _SEND not in paths and _RESET not in paths
    assert not _has_test_page(paths) and not _has_usage(paths)


def test_prod_like_settings_mount_only_whoami(monkeypatch):
    paths = _paths(monkeypatch, ENVIRONMENT="production", EUN_DB_NAME="eun_roster")
    assert _WHOAMI in paths
    assert _SEND not in paths and not _has_test_page(paths) and not _has_usage(paths)


def test_chat_switch_mounts_send_and_reset(monkeypatch):
    paths = _paths(monkeypatch, AIDE_AGENT_CHAT="1")
    assert {_SEND, _RESET, _WHOAMI} <= paths


@pytest.mark.parametrize("value", ["", "0", "true", "yes", "on"])
def test_only_one_turns_a_switch_on(monkeypatch, value):
    paths = _paths(monkeypatch, AIDE_AGENT_CHAT=value, AIDE_AGENT_USAGE_VIEW=value)
    assert _SEND not in paths and not _has_usage(paths)


def test_test_page_mounts_on_dev_db(monkeypatch):
    paths = _paths(monkeypatch, AIDE_AGENT_TEST_ROUTER="1",
                   ENVIRONMENT="development", EUN_DB_NAME="eun_roster_dev")
    assert _has_test_page(paths)


@pytest.mark.parametrize("env", [
    {"ENVIRONMENT": "production", "EUN_DB_NAME": "eun_roster_dev"},
    {"ENVIRONMENT": "prod", "EUN_DB_NAME": "eun_roster_dev"},
    {"ENVIRONMENT": "development", "EUN_DB_NAME": "eun_roster"},
    {"ENVIRONMENT": "Production ", "EUN_DB_NAME": ""},
])
def test_test_page_never_mounts_in_production(monkeypatch, env):
    """테스트 화면은 무인증 실 DB 조회 경로라(N66) 켜는 값이 있어도 운영 환경·운영 DB 면 막는다."""
    paths = _paths(monkeypatch, AIDE_AGENT_TEST_ROUTER="1", **env)
    assert not _has_test_page(paths)


def test_usage_switch_mounts_usage(monkeypatch):
    assert _has_usage(_paths(monkeypatch, AIDE_AGENT_USAGE_VIEW="1"))


def test_main_app_mounts_through_surface():
    """실제 앱(main)이 마운트를 surface 한 곳에 맡긴다.

    마운트 결과 자체는 위 `_paths()` 테스트가 본다. main 은 import 때 routers 의 load_dotenv 가
    로컬 .env 를 다시 읽고 모듈 캐시에 남으므로, main.app 의 라우트로 env 별 결과를 재면 로컬
    설정·실행 순서에 따라 흔들린다. 여기서는 배선만 확인한다.
    """
    import main

    paths = {getattr(r, "path", "") for r in main.app.routes}
    assert main.mount_agent_routes is surface.mount_agent_routes
    assert _WHOAMI in paths  # whoami 는 surface 에서만 붙는다


# ── whoami agent_enabled ─────────────────────────────────────


def _whoami(db, user):
    return chat_router.whoami(current_user=user, db=db)


def test_whoami_disabled_when_chat_off(monkeypatch, db, seed_data):
    monkeypatch.delenv("AIDE_AGENT_CHAT", raising=False)
    assert _whoami(db, make_auth_user()).agent_enabled is False


def test_whoami_enabled_for_head_nurse(monkeypatch, db, seed_data):
    monkeypatch.setenv("AIDE_AGENT_CHAT", "1")
    assert _whoami(db, make_auth_user()).agent_enabled is True


def test_whoami_disabled_for_staff_nurse(monkeypatch, db, seed_data):
    monkeypatch.setenv("AIDE_AGENT_CHAT", "1")
    user = make_auth_user("N002", "박지은", role="NURSE")
    assert _whoami(db, user).agent_enabled is False


def test_whoami_enabled_for_group_manager_without_head_nurse_flag(monkeypatch, db, seed_data):
    """그룹관리자(hn_auth='HN')는 is_head_nurse 가 아니어도 수간호사와 같은 단계(10-07 결정)."""
    monkeypatch.setenv("AIDE_AGENT_CHAT", "1")
    nurse = db.query(Nurse).filter(Nurse.nurse_id == "N003").one()
    nurse.hn_auth, nurse.is_head_nurse = "HN", False
    db.flush()
    user = make_auth_user("N003", "이수정", role="NURSE")
    assert _whoami(db, user).agent_enabled is True


def test_whoami_enabled_for_admin(monkeypatch, db, seed_data):
    monkeypatch.setenv("AIDE_AGENT_CHAT", "1")
    user = make_auth_user("ADM01", "관리자", role="ADM")
    assert _whoami(db, user).agent_enabled is True


def test_whoami_uses_db_not_token(monkeypatch, db, seed_data):
    """토큰이 수간호사라고 해도 DB 에서 보직이 풀렸으면 쓸 수 없다."""
    monkeypatch.setenv("AIDE_AGENT_CHAT", "1")
    nurse = db.query(Nurse).filter(Nurse.nurse_id == "N001").one()
    nurse.hn_auth, nurse.is_head_nurse = None, False
    db.flush()
    assert _whoami(db, make_auth_user()).agent_enabled is False


# ── send·reset 서버 거절 ─────────────────────────────────────


def _forbid_agent_and_store(monkeypatch):
    def _boom():
        raise AssertionError("일반 간호사 요청이 에이전트·대화 저장소에 닿았다")

    monkeypatch.setattr(chat_router, "_get_agent", _boom)
    monkeypatch.setattr(chat_router, "_get_store", _boom)


def test_send_refuses_staff_nurse_before_agent(monkeypatch, db, seed_data):
    _forbid_agent_and_store(monkeypatch)
    user = make_auth_user("N002", "박지은", role="NURSE")
    req = chat_router.ChatRequest(message="이번 달 내 근무 알려줘", conversation_id="c-1")
    res = asyncio.run(chat_router.send_message(req, current_user=user, db=db))
    assert res.answer == chat_router._NOT_PROVIDED_MSG
    assert res.conversation_id == "c-1"
    assert res.awaiting_approval is False and res.ui_actions == []


def test_reset_refuses_staff_nurse(monkeypatch, db, seed_data):
    _forbid_agent_and_store(monkeypatch)
    user = make_auth_user("N002", "박지은", role="NURSE")
    assert asyncio.run(chat_router.reset_conversation("c-1", current_user=user, db=db)) is None


class _Reached(Exception):
    """판정을 통과해 대화 저장소까지 닿았다는 표시."""


def _reach_store(monkeypatch):
    def _store():
        raise _Reached

    monkeypatch.setattr(chat_router, "_get_store", _store)


def _manager_users(db):
    nurse = db.query(Nurse).filter(Nurse.nurse_id == "N003").one()
    nurse.hn_auth, nurse.is_head_nurse = "HN", False  # 그룹관리자(수간호사 플래그 없음)
    db.flush()
    return [
        make_auth_user(),  # 수간호사 N001
        make_auth_user("N003", "이수정", role="NURSE"),
        make_auth_user("ADM01", "관리자", role="ADM"),
    ]


def test_send_lets_managers_through(monkeypatch, db, seed_data):
    """수간호사·그룹관리자·ADM 의 send 는 거절문 없이 대화 저장소까지 간다."""
    _reach_store(monkeypatch)
    req = chat_router.ChatRequest(message="이번 달 근무표 보여줘", conversation_id="c-2")
    for user in _manager_users(db):
        with pytest.raises(_Reached):
            asyncio.run(chat_router.send_message(req, current_user=user, db=db))


def test_reset_lets_managers_through(monkeypatch, db, seed_data):
    _reach_store(monkeypatch)
    for user in _manager_users(db):
        with pytest.raises(_Reached):
            asyncio.run(chat_router.reset_conversation("c-2", current_user=user, db=db))
