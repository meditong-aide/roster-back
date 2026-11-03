"""수정 도우미의 생성 후처리(자동 연차전환) — 서비스 함수는 가짜로 바꾸고, 미리 돌리기·카드·이유 문장·적용 재료만 본다."""

from __future__ import annotations

from datetime import date
from types import SimpleNamespace

import pytest

from schedule_edit_agent import skills as skills_mod
from schedule_edit_agent.answer import status_text
from schedule_edit_agent.view import blocks_for, step_label
from services.schedule_edit import postprocess as post_mod
from services.schedule_edit.apply import roster_payload
from services.schedule_edit.solver import verify_candidate
from tests.edit_agent.test_agent import _agent, call, make_ctx, press, say

TODAY = date(2026, 11, 2)      # 11월 1일은 지난 날
SWAP = call("run_postprocess", kind="annual_leave_swap")


def _ward():
    return make_ctx({"A": "OODOD", "B": "DOODO"}, cov_min={"D": 1}, off_days=2, config={"not_one_night": False})


@pytest.fixture
def swap(world, monkeypatch):
    """서비스가 바꿀 칸·report 를 정해 두는 가짜 — A 1일(지난 날)·A 4일·B 5일을 연차로."""
    world.ctxs["s1"] = _ward
    state = {"cells": [("A", 0), ("A", 3), ("B", 4)],
             "report": dict(skip=None, target_shift="연", baseline=2, two_offs_per_week=True, converted=3,
                            skipped_n_only=1, kept_off_for_week_floor=2, excess_total=6, protected=1)}

    def fake(db, schedule, generated, config, req, report=None):
        report.update(state["report"])
        for nid, day in state["cells"]:
            generated[nid][day] = "연"
        return generated

    monkeypatch.setattr(post_mod, "postprocess_off_swap", fake)
    monkeypatch.setattr(post_mod, "load_schedule_config",
                        lambda db, sid: (SimpleNamespace(schedule_id=sid), SimpleNamespace()))
    return state


def test_swap_previews_one_card_and_holds_past_days(world, swap):
    agent, _ = _agent(SWAP)
    reply = say(agent, world.conv, "남는 OFF 연차로 돌려줘", TODAY)
    assert reply.trail == "new" and reply.preview["source"] == "annual_leave_swap"
    cand = reply.preview["candidates"][0]
    assert [(c["nurse_id"], c["day"], c["before"], c["after"]) for c in cand["changes"]] == \
        [("A", 4, "O", "연"), ("B", 5, "O", "연")]                     # 1일은 지난 날이라 뺐다
    assert cand["counts"]["A"]["OFF"] == [3, 2]
    assert "OFF 2칸을 연차로 바꿉니다(2명)" in reply.answer
    for why in ("주 2회 휴일 하한", "회복 OFF·확정 원티드", "N 전담 1명", "지난 날·바꿀 수 없는 칸 1칸", "연차 잔여는 보지 않습니다"):
        assert why in reply.answer
    assert world.events[-1].event == "propose" and world.events[-1].payload["postprocess"] == "annual_leave_swap"

    card = blocks_for(reply, world.conv.state)[-1]["items"][0]
    assert card["title"].startswith("자동 연차전환 · 2칸")
    assert [a["input"]["action"] for a in card["actions"]] == ["apply_edit"]   # 다듬기·넘기기는 없다
    assert card["overlay"]["cells"][0] == {"nurse_id": "A", "day": 4, "before": "O", "after": "연"}

    session = world.conv.state
    assert session.requests == [] and status_text(session).startswith("지금 요청: 자동 연차전환.")
    assert step_label("apply_edit", {"rank": 1}, session) == "자동 연차전환 적용"
    # 연차도 쉬는 날이라 규칙 판정은 그대로 — 적용 직전 재확인을 통과하고, 저장 요청에는 '연' 이 그대로 들어간다
    last = session.last.candidates[0]
    assert verify_candidate(session.ctx, session.rules, last, session.requests, today=TODAY) == []
    rows = {r["nurse_id"]: r["schedule"] for r in roster_payload(session, last)}
    assert rows == {"A": ["O", "O", "D", "연", "D"], "B": ["D", "O", "O", "D", "연"]}


def test_nothing_to_convert_says_why_from_the_service_numbers(world, swap):
    swap["cells"] = []
    swap["report"] = dict(skip=None, target_shift="연", baseline=8, two_offs_per_week=True, converted=0,
                          skipped_n_only=1, kept_off_for_week_floor=8, excess_total=8, protected=0)
    agent, _ = _agent(SWAP, SWAP, SWAP)
    reply = say(agent, world.conv, "자동 연차전환 해줘", TODAY)
    assert reply.preview is None and world.conv.state.last is None
    assert reply.answer == ("자동 연차전환(OFF 기준 8개 · 바꿀 코드 '연')을 돌려 봤지만 바꿀 칸이 없습니다. "
                            "기준을 넘는 OFF 8칸은 주 2회 휴일 하한을 지키려면 그대로 둬야 합니다 · N 전담 1명은 바꾸지 않습니다.")
    swap["report"] = {"skip": "disabled"}
    assert "자동 연차전환이 꺼져 있습니다" in say(agent, world.conv, "연차 전환 써줘", TODAY).answer
    swap["report"] = dict(skip=None, target_shift="연", baseline=8, excess_total=0)
    assert "기준을 넘는 OFF 가 없어" in say(agent, world.conv, "연차 전환 다시", TODAY).answer


def test_apply_saves_the_converted_cells_as_a_new_version(world, swap, monkeypatch):
    seen = {}

    def fake_apply(db, session, cand, user, **kw):
        seen["cells"] = [(c.nurse_id, c.day, c.after) for c in cand.changes]
        return {"new_schedule_id": "s2", "new_version": 2, "new_name": "11월 수정본"}

    monkeypatch.setattr(skills_mod, "apply_to_new_version", fake_apply)
    agent, _ = _agent(SWAP)
    say(agent, world.conv, "자동 연차전환 해줘", TODAY)
    applied = press(agent, world.conv, "apply_edit", {"rank": 1}, TODAY)
    assert seen["cells"] == [("A", 3, "연"), ("B", 4, "연")]
    assert applied.effects[0]["schedule_id"] == "s2" and world.conv.context == {"schedule_id": "s2"}


def test_conversion_below_the_off_floor_is_refused_before_apply(world, swap):
    # 기준 OFF 2 · A 의 순수 OFF 3 — 두 칸을 연차로 바꾸면 1개가 되어 하한 아래(Codex 11-03: 대표코드 O 만 보면 놓친다)
    swap["cells"] = [("A", 1), ("A", 3)]
    agent, _ = _agent(SWAP)
    say(agent, world.conv, "자동 연차전환 해줘", TODAY)
    session = world.conv.state
    problems = verify_candidate(session.ctx, session.rules, session.last.candidates[0], session.requests, today=TODAY)
    assert any("OFF수" in p and "하한" in p for p in problems)


def test_postprocess_card_only_takes_apply(world, swap):
    # 화면엔 다듬기·넘기기가 없지만 서버로 직접 와도 빈 요청으로 솔버를 돌리지 않는다(검토 M1)
    agent, _ = _agent(SWAP)
    say(agent, world.conv, "자동 연차전환 해줘", TODAY)
    for action in ("keep_candidate", "reject_candidate"):
        reply = press(agent, world.conv, action, {"rank": 1}, TODAY)
        assert reply.failed and "'적용'만 할 수 있습니다" in reply.answer
    assert world.conv.state.last.source == "annual_leave_swap"           # 미리 본 안은 그대로
