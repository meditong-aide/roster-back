"""autonomy_mode auto tier — auto_safe mutation 만 승인 없이 자동 실행.

HITL 3-tier(auto/notify/block). 기본 manual = 모든 mutation 승인. auto 여도 스킬이
auto_safe=True 로 명시 opt-in 한 것만 자동(안전 결정은 스킬별로).
"""

from __future__ import annotations

from agents_v2.skills.manifest import SKILL_SPECS, skill
from agents_v2.skills.registry import SKILL_REGISTRY
from tests.agent_qa.harness import AgentTestSession, ScriptedClient, ScriptedResponse

_NAME = "autonomy_test_skill"
_SCHEMA = {"name": _NAME, "parameters": {"type": "object", "properties": {}}}


def _register(auto_safe: bool):
    @skill(
        _NAME, _SCHEMA, mutation=True, hn_only=False,
        auto_safe=auto_safe, postcondition=lambda d: True,
    )
    def _h(db, params):
        if params.get("preview_only"):
            return {"preview": True, "summary": "테스트 변경"}
        return {"ok": True, "committed": True}


def _cleanup():
    SKILL_SPECS.pop(_NAME, None)
    SKILL_REGISTRY.pop(_NAME, None)


def _session(db) -> AgentTestSession:
    return AgentTestSession(db, client=ScriptedClient([
        ScriptedResponse(tool_calls=[{"name": _NAME, "args": {"preview_only": True}}]),
    ]))


def test_auto_mode_autosafe_commits_without_approval(db, seed_data):
    _register(auto_safe=True)
    try:
        sess = _session(db)
        sess.ctx.autonomy_mode = "auto"
        res = sess.send("변경해줘")
        assert res.awaiting_approval is False, "auto+auto_safe 인데 승인 대기함"
        assert "자동 실행" in res.answer
        assert any(s.name == "auto_execution" for s in res.trace)
    finally:
        _cleanup()


def test_auto_mode_not_autosafe_still_gated(db, seed_data):
    _register(auto_safe=False)
    try:
        sess = _session(db)
        sess.ctx.autonomy_mode = "auto"
        res = sess.send("변경해줘")
        # auto_safe=False → auto 모드여도 여전히 승인 필요
        assert res.awaiting_approval is True
        assert not any(s.name == "auto_execution" for s in res.trace)
    finally:
        _cleanup()


def test_manual_mode_always_gated_even_if_autosafe(db, seed_data):
    _register(auto_safe=True)  # auto_safe 여도 manual 이면 승인(모드가 우선)
    try:
        sess = _session(db)  # ctx.autonomy_mode 기본 "manual"
        res = sess.send("변경해줘")
        assert res.awaiting_approval is True
        assert not any(s.name == "auto_execution" for s in res.trace)
    finally:
        _cleanup()
