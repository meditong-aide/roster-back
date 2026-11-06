"""S1(11-06) — 근무표 수정 도우미 경로를 AIDE 표면 스위치(surface) 한 곳에서 붙인다.

- 기본 설정: 수정 도우미 공통 경로(/api/agents/*)·AIDE 대화·테스트 화면 전부 안 붙음(whoami 만).
- `SCHEDULE_EDIT_AGENT_ENABLED=1` 이면 /api/agents/{agent_id}/* 가 붙고, AIDE 대화는 여전히 안 붙음.
- 운영 모양(ENVIRONMENT=production)에서 테스트 화면 스위치를 켜도 테스트 화면은 안 붙음(N66).
- 새 수정 대화 id 는 `se:` 접두 — AIDE 대화 저장소와 이름 공간을 나눈다.
실 LLM·실 DB 없음.
"""
from __future__ import annotations

from agent_kit import base as kit_base
from agent_kit.base import any_enabled
from agents_v2 import surface
from fastapi import FastAPI

_SWITCHES = ("AIDE_AGENT_CHAT", "AIDE_AGENT_TEST_ROUTER", "AIDE_AGENT_USAGE_VIEW",
             "SCHEDULE_EDIT_AGENT_ENABLED", "ENVIRONMENT", "EUN_DB_NAME")
_KIT_TURN = "/api/agents/{agent_id}/turn"
_SEND = "/api/agent/chat/send"
_WHOAMI = "/api/agent/chat/whoami"


def _paths(monkeypatch, **env) -> set[str]:
    for name in _SWITCHES:
        monkeypatch.delenv(name, raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    app = FastAPI()
    surface.mount_agent_routes(app)
    return {getattr(r, "path", "") for r in app.routes}


def _kit(paths: set[str]) -> set[str]:
    return {p for p in paths if p.startswith("/api/agents/")}


def _test_page(paths: set[str]) -> bool:
    return any(p.startswith("/agent/test") for p in paths)


def test_default_mounts_no_agent_surface(monkeypatch):
    paths = _paths(monkeypatch)
    assert _WHOAMI in paths
    assert not _kit(paths)
    assert _SEND not in paths and not _test_page(paths)


def test_schedule_edit_switch_mounts_kit_routes_only(monkeypatch):
    paths = _paths(monkeypatch, SCHEDULE_EDIT_AGENT_ENABLED="1")
    assert _KIT_TURN in paths
    assert "/api/agents/{agent_id}/availability" in paths
    assert _SEND not in paths and not _test_page(paths)


def test_switch_value_other_than_one_stays_off(monkeypatch):
    for value in ("true", "0", "", "yes"):
        assert not _kit(_paths(monkeypatch, SCHEDULE_EDIT_AGENT_ENABLED=value))


def test_production_never_mounts_test_page(monkeypatch):
    paths = _paths(monkeypatch, ENVIRONMENT="production", AIDE_AGENT_TEST_ROUTER="1",
                   SCHEDULE_EDIT_AGENT_ENABLED="1")
    assert not _test_page(paths)
    assert _KIT_TURN in paths


def test_any_enabled_follows_registered_agents(monkeypatch):
    monkeypatch.delenv("SCHEDULE_EDIT_AGENT_ENABLED", raising=False)
    surface.kit_agents_enabled()  # 등록 보장
    assert "schedule-edit" in kit_base.AGENTS
    assert any_enabled() is False
    monkeypatch.setenv("SCHEDULE_EDIT_AGENT_ENABLED", "1")
    assert any_enabled() is True


def test_new_schedule_edit_conversation_id_has_se_prefix(monkeypatch):
    surface.kit_agents_enabled()
    agent = kit_base.get_agent("schedule-edit")
    conv = agent.store.start("acc_test_prefix", {"schedule_id": "s1"})
    try:
        assert conv.id.startswith("se:")
        assert len(conv.id) <= 64  # schedule_edit_log.conversation_id VARCHAR(64)
    finally:
        agent.store.checkin(conv)
        agent.store.close(conv.id)
