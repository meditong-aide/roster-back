"""후보 아래 '설정을 이번만 넘기시면 몇 칸 → 몇 칸' 레버 카드(11-03 · 사용자 제안 — 줄어들 때만)."""

from __future__ import annotations

from schedule_edit_agent.view import blocks_for
from tests.edit_agent.test_agent import BEFORE_MONTH, _agent, call, say
from tests.schedule_edit.test_shortcuts import _long_ward


def test_panel_shows_the_candidates_then_how_a_setting_would_cut_them(world):
    world.ctxs["s1"] = _long_ward
    agent, _ = _agent(call("propose_edit", changes=[{"kind": "set", "nurse": "A", "day": 2, "shift": "OFF"}]))
    reply = say(agent, world.conv, "A 2일 오프", BEFORE_MONTH)
    assert reply.preview["status"] == "ok"
    assert "설정 하나를 이번만 넘기시면 5칸 대신 2칸만 바꾸는 방법도 아래에 있습니다." in reply.answer
    blocks = blocks_for(reply, world.conv.state)
    candidates, levers = blocks[-2], blocks[-1]
    assert candidates["selectable"] and not levers["selectable"]
    first = next(c for c in levers["items"] if c["title"] == "2일 E 최소 근무 인원보다 1명 적게")
    assert first["lines"][0]["text"] == "이 설정을 이번만 넘기시면 바꾸는 칸이 5칸 → 2칸으로 줄어듭니다"
    assert first["actions"][0]["input"]["action"] == "choose_lever"
