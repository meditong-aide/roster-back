"""고칠 수 있는 첫날(사용자 결정 11-05) — 기본은 오늘까지 고정, 사용자가 날을 정하면 그 앞은 모두 고정.

  · 지난 날(오늘 이전)은 어떤 경우에도 열리지 않는다 · 오늘은 짚은 날이 바로 오늘일 때만 열린다(짚은 날이 오늘보다 앞이면
    오늘도 닫힌다 — 어제 '오늘부터'로 정한 대화를 오늘 이어 써도).
  · 정한 날은 대화 내내 이어 간다 — 새 요청·칸이 바뀌어 다시 열기·적용 뒤 새 버전·재기동 복원.
시험 근무표는 `_two_nurse_ctx`(2026-11 · 4일 · A DODO · B ODOD · 하루 D 1명 · OFF 2개).
"""

from __future__ import annotations

import dataclasses
from datetime import date

import pytest

from agent_kit.conversation import Conversation
from schedule_edit_agent import agent as agent_mod
from schedule_edit_agent import skills as skills_mod
from schedule_edit_agent import store as store_mod
from schedule_edit_agent.grounding import ground_changes
from schedule_edit_agent.skills import _from_open
from services.schedule_edit.requests import (
    BalanceTarget, CountTarget, SetCell, WishTarget, closed_day_text, closed_span_text, edit_from_note, first_open_day,
    held_until,
)
from services.schedule_edit.rules import build_rules
from services.schedule_edit.serialize import requests_from_json, requests_to_json
from services.schedule_edit.solver import propose_edits, verify_candidate
from tests.edit_agent.test_agent import USER, _agent, _logs, _row, call, new_store, say, press
from tests.schedule_edit.test_count import _ward as _night_ward
from tests.schedule_edit.test_solver import BEFORE_MONTH, _two_nurse_ctx

TODAY = date(2026, 11, 2)            # 1일은 지난 날 · 2일은 오늘
LOCKED = "2일까지는 그대로 두기로 해서 1일은 바꾸지 않습니다. 3일부터 고칠 수 있습니다."


def _set(nurse, day, shift):
    return {"kind": "set", "nurse": nurse, "day": day, "shift": shift}


def _ctx(edit_from=None):
    ctx = _two_nurse_ctx()
    ctx.edit_from = edit_from
    return ctx


# ── 판정(first_open_day) ────────────────────────────────────────────────────


@pytest.mark.parametrize("edit_from,today,expected", [
    (None, TODAY, 2),                   # 기본 — 오늘(2일)도 닫힌다
    (None, BEFORE_MONTH, 0),            # 다음 달 근무표 — 아무것도 닫지 않는다(12월을 11월에 고칠 때 1일을 닫지 않게)
    (None, date(2026, 12, 5), 4),       # 지난 달 — 전부 닫힌다
    (3, TODAY, 3),                      # 정한 날(4일)이 오늘보다 뒤 — 그 앞(3일까지) 닫힘
    (1, TODAY, 1),                      # 정한 날이 오늘 — 오늘부터 열린다
    (0, TODAY, 2),                      # 정한 날이 지난 날 — 오늘도 닫힌다(사용자 결정 11-05 · 오늘은 짚은 날이 오늘일 때만)
    (2, BEFORE_MONTH, 2),               # 다음 달 근무표라도 정한 날 앞은 닫는다
])
def test_first_open_day_follows_today_and_the_chosen_day(edit_from, today, expected):
    assert first_open_day(_ctx(edit_from), today) == expected


def test_closed_day_text_says_why_it_is_closed():
    ctx = _ctx()
    assert closed_day_text(ctx, 0, 2, TODAY) == "1일은 이미 지난 날짜라 바꿀 수 없습니다. 3일부터 고칠 수 있습니다."
    assert closed_day_text(ctx, 1, 2, TODAY) == ("오늘(2일)은 이미 시작된 근무일이라 바꾸지 않습니다. "
                                                 "오늘도 고치려면 '오늘부터 고쳐줘'라고 하시면 됩니다.")
    ctx.edit_from = 3
    assert closed_day_text(ctx, 1, 3, TODAY) == "3일까지는 그대로 두기로 해서 2일은 바꾸지 않습니다. 4일부터 고칠 수 있습니다."
    assert closed_day_text(ctx, 0, 3, TODAY).startswith("1일은 이미 지난 날짜라")      # 지난 날은 지난 날이라고
    assert closed_day_text(ctx, 0, 3).startswith("3일까지는 그대로 두기로 해서")      # 오늘을 모르면 정한 날로


def test_a_chosen_day_that_is_today_or_earlier_is_not_called_kept():
    # 정한 날이 오늘이면 닫힌 날은 지난 날뿐 — '그대로 두기로 한 날'이라 하지 않는다(검토 11-05 L2)
    ctx = _ctx(1)
    assert held_until(ctx, 1, TODAY) is None and held_until(ctx, 1) == 1          # 오늘을 모르면 정한 날로
    assert closed_day_text(ctx, 0, 1, TODAY).startswith("1일은 이미 지난 날짜라")
    assert closed_span_text(ctx, 0, 1, TODAY) == "그 기간은 이미 지난 날이라 바꿀 수 없습니다. 2일부터 고칠 수 있습니다."
    assert edit_from_note(ctx, 1, TODAY) == "오늘(2일)부터 고칩니다."
    assert closed_span_text(_ctx(), 1, 2, TODAY) == ("그 기간은 지난 날·오늘이라 바꾸지 않습니다. 3일부터 고칠 수 있습니다. "
                                                     "오늘도 고치려면 '오늘부터 고쳐줘'라고 하시면 됩니다.")


def test_apply_recheck_refuses_a_candidate_that_touches_days_before_the_chosen_day():
    # 후보를 찾은 뒤 '2일까지는 그대로'로 정했다 — 1일을 바꾸는 후보는 적용 직전 재확인이 막는다
    ctx = _ctx()
    cand = propose_edits(ctx, [SetCell("A", 0, "O")], today=BEFORE_MONTH).candidates[0]
    ctx.edit_from = 2
    problems = verify_candidate(ctx, build_rules(ctx), cand, [SetCell("A", 0, "O")], today=BEFORE_MONTH)
    assert problems[0] == LOCKED


def test_grounding_says_a_period_before_the_chosen_day_is_kept():
    ctx = _ctx(2)
    grounded = ground_changes(ctx, [{"kind": "wanted", "nurse": "", "day": 1, "to_day": 2}], first_open_day(ctx, BEFORE_MONTH))
    assert grounded.asks == ["2일까지는 그대로 두기로 해서 그 기간은 바꾸지 않습니다. 3일부터 고칠 수 있습니다."]


# ── 대화 ────────────────────────────────────────────────────────────────────


def test_choosing_the_day_alone_sets_it_and_asks_what_to_change(world):
    agent, llm = _agent(call("propose_edit", changes=[], edit_from_day=3), call("reply_only", kind="status"))
    reply = say(agent, world.conv, "2일까지는 고정해")
    assert reply.answer == "3일부터 고칩니다(2일까지는 그대로 둡니다). 바꿀 내용을 말씀해 주세요."
    assert [e.event for e in world.events] == ["message", "lock", "clarify"]
    assert world.events[1].payload == {"edit_from_day": 3, "year": 2026, "month": 11}
    assert world.conv.state.ctx.edit_from == 2 and world.conv.state.requests == []
    say(agent, world.conv, "지금 어떻게 돼 있어?")
    assert "고칠 수 있는 날: 3일부터(사용자가 정함 — 2일까지 그대로)" in llm.seen[1][-2]["content"]


def test_the_chosen_day_carries_into_the_next_request_until_overwritten(world):
    agent, _ = _agent(call("propose_edit", changes=[], edit_from_day=3),
                      call("propose_edit", changes=[_set("A", 3, "OFF")]),
                      call("propose_edit", changes=[_set("A", 1, "OFF")]),
                      call("propose_edit", changes=[_set("A", 1, "OFF")], edit_from_day=1))
    say(agent, world.conv, "2일까지는 고정해")
    later = say(agent, world.conv, "A 3일 오프")
    assert later.preview["status"] == "ok"
    assert all(ch["day"] >= 3 for c in later.preview["candidates"] for ch in c["changes"])   # 화면 day 는 1부터
    assert "그대로 둡니다" not in later.answer                    # 기준일 안내는 정한 턴에 이미 했다
    refused = say(agent, world.conv, "A 1일 오프")
    assert refused.preview["status"] == "invalid" and LOCKED in refused.answer
    opened = say(agent, world.conv, "1일부터 고쳐줘, A 1일 오프")        # 다른 날을 말하면 덮어쓴다
    assert opened.preview["status"] == "ok" and world.conv.state.ctx.edit_from == 0
    assert [e.payload["edit_from_day"] for e in world.events if e.event == "lock"] == [3, 1]


def test_choosing_the_day_with_a_change_notes_it_once(world):
    agent, _ = _agent(call("propose_edit", changes=[_set("A", 3, "OFF")], edit_from_day=3),
                      call("propose_edit", changes=[_set("A", 4, "OFF")]))
    first = say(agent, world.conv, "2일까지는 그대로 두고 A 3일 오프")
    assert first.preview["status"] == "ok" and "참고: 3일부터 고칩니다(2일까지는 그대로 둡니다)." in first.answer
    second = say(agent, world.conv, "A 4일 오프")
    assert "2일까지는 그대로 둡니다" not in second.answer


def test_today_is_closed_by_default_and_opens_when_asked(world):
    agent, llm = _agent(call("propose_edit", changes=[_set("B", 2, "OFF")]),
                        call("propose_edit", changes=[_set("B", 2, "OFF")], edit_from_day=2))
    closed = say(agent, world.conv, "B 2일 오프", TODAY)
    assert closed.preview["status"] == "invalid"
    assert ("오늘(2일)은 이미 시작된 근무일이라 바꾸지 않습니다. 오늘도 고치려면 '오늘부터 고쳐줘'라고 하시면 됩니다."
            in closed.answer)
    assert "고칠 수 있는 날: 3일부터(오늘까지 고정)" in llm.seen[0][-2]["content"]
    opened = say(agent, world.conv, "오늘부터 고쳐줘, B 2일 오프", TODAY)
    assert opened.preview["status"] == "ok"
    assert all(ch["day"] >= 2 for c in opened.preview["candidates"] for ch in c["changes"])


def test_today_from_yesterday_closes_today_after_midnight(world):
    # 2일에 '오늘부터'(2일부터)로 정했다 — 3일에 이어 쓰면 3일(오늘)은 닫힌다
    agent, llm = _agent(call("propose_edit", changes=[], edit_from_day=2),
                        call("propose_edit", changes=[_set("A", 3, "OFF")]))
    assert say(agent, world.conv, "오늘부터 고쳐줘", TODAY).answer == "오늘(2일)부터 고칩니다. 바꿀 내용을 말씀해 주세요."
    assert first_open_day(world.conv.state.ctx, TODAY) == 1
    next_day = date(2026, 11, 3)
    reply = say(agent, world.conv, "A 3일 오프", next_day)
    assert reply.preview["status"] == "invalid"
    assert "\n오늘(3일)은 이미 시작된 근무일이라 바꾸지 않습니다. 오늘도 고치려면 '오늘부터 고쳐줘'라고 하시면 됩니다." in reply.answer
    # 날이 바뀌어 안내가 달라졌다 — 정한 2일이 지난 날이 된 사실을 한 번 알린다
    assert "참고: 2일은 지난 날이라 바꿀 수 없어 4일부터 고칩니다." in reply.answer
    assert "고칠 수 있는 날: 4일부터(오늘까지 고정 · 사용자가 정한 2일은 이미 지남)" in llm.seen[1][-2]["content"]


def test_a_day_before_today_says_those_days_are_past_and_keeps_today_closed(world):
    agent, _ = _agent(call("propose_edit", changes=[], edit_from_day=1))
    reply = say(agent, world.conv, "1일부터 고쳐줘", date(2026, 11, 3))
    assert reply.answer == ("1·2일은 지난 날이라 바꿀 수 없어 4일부터 고칩니다. 오늘도 고치려면 '오늘부터 고쳐줘'라고 하시면 됩니다. "
                            "바꿀 내용을 말씀해 주세요.")
    assert first_open_day(world.conv.state.ctx, date(2026, 11, 3)) == 3


def test_choosing_the_day_alone_reproposes_the_earlier_request(world):
    # 앞 요청이 있으면 날만 정한 말도 새 첫날로 다시 찾는다 — 1일을 바꾸던 옛 후보가 남지 않는다(검토 11-05 M2)
    agent, _ = _agent(call("propose_edit", changes=[_set("A", 1, "OFF")]), call("propose_edit", changes=[], edit_from_day=3))
    assert say(agent, world.conv, "A 1일 오프").preview["status"] == "ok"
    reply = say(agent, world.conv, "2일까지는 고정해")
    assert reply.preview["status"] == "invalid" and reply.preview["candidates"] == [] and LOCKED in reply.answer
    assert world.conv.state.last.status == "invalid" and world.events[-1].event == "propose"


def test_choosing_the_day_alone_matches_revising_with_it(world):
    said = call("propose_edit", changes=[_set("A", 3, "OFF")])
    agent, _ = _agent(said, call("propose_edit", changes=[], edit_from_day=3))
    say(agent, world.conv, "A 3일 오프")
    alone = say(agent, world.conv, "2일까지는 고정해")
    other = world.store.start(USER.account_id, {"schedule_id": "s1"})
    agent2, _ = _agent(said, call("revise_edit", edit_from_day=3))
    say(agent2, other, "A 3일 오프")
    revised = say(agent2, other, "그리고 2일까지는 그대로 둬")
    assert alone.preview["status"] == "ok" and (alone.answer, alone.preview) == (revised.answer, revised.preview)


def test_a_count_already_over_its_target_on_closed_days_keeps_them_and_says_so(world):
    # A N 4개를 1개로 — 3일부터로 정하면 1·2일에 이미 N 2개라 목표를 못 지킨다. 거절 대신 바꿀 수 있는 몫을 0개로(검토 L1)
    world.ctxs["s1"] = _night_ward
    agent, _ = _agent(call("propose_edit", changes=[{"kind": "count", "nurse": "A", "shift": "N", "target": 1,
                                                     "transfer": "auto"}]),
                      call("propose_edit", changes=[], edit_from_day=3))
    say(agent, world.conv, "A N 1개로")
    reply = say(agent, world.conv, "2일까지는 고정해")
    assert reply.preview["status"] == "ok"
    assert "참고: A N은(는) 고치지 않는 날(1·2일)에 이미 2개가 있어 그만큼은 남습니다." in reply.answer
    assert world.conv.state.requests == [CountTarget("A", frozenset({"N"}), 0, 2, 3, "auto", asked_first=0, asked_target=1)]


def test_a_single_period_request_left_wholly_closed_is_refused_with_a_reason(world):
    # 기간 요청 하나만 남았는데 그 기간이 모두 닫힌 날 — 빈 결과(이미 그렇게 됨)가 아니라 거절 문장(검토 L3)
    agent, _ = _agent(call("propose_edit", changes=[{"kind": "wanted", "nurse": "", "day": 1, "to_day": 2}]),
                      call("revise_edit", edit_from_day=3))
    say(agent, world.conv, "1~2일 원티드 넣어줘")
    reply = say(agent, world.conv, "2일까지는 그대로 둬")
    assert reply.preview["status"] == "invalid" and reply.preview["candidates"] == []
    assert "2일까지는 그대로 두기로 해서 그 기간은 바꾸지 않습니다. 3일부터 고칠 수 있습니다." in reply.answer


def test_a_day_outside_the_month_is_asked_back(world):
    agent, _ = _agent(call("propose_edit", changes=[], edit_from_day=9))
    reply = say(agent, world.conv, "8일까지는 고정해")
    assert reply.answer == "고칠 수 있는 첫날을 11월 안의 날짜로 알려 주세요. 예) '6일부터 고쳐줘'"
    assert world.conv.state.ctx.edit_from is None and "lock" not in [e.event for e in world.events]


def test_revising_with_a_new_day_moves_the_earlier_request_after_it(world):
    agent, _ = _agent(call("propose_edit", changes=[_set("A", 3, "OFF")]),
                      call("revise_edit", edit_from_day=3))
    say(agent, world.conv, "A 3일 오프")
    again = say(agent, world.conv, "그리고 2일까지는 그대로 둬")
    assert again.preview["status"] == "ok" and "참고: 3일부터 고칩니다(2일까지는 그대로 둡니다)." in again.answer
    assert all(ch["day"] >= 3 for c in again.preview["candidates"] for ch in c["changes"])


def test_removing_the_last_request_while_choosing_the_day_still_says_the_day(world):
    # 남은 요청이 없다는 말만으로는 날이 정해졌는지 모른다(검토 11-05 LOW-3)
    agent, _ = _agent(call("propose_edit", changes=[_set("A", 3, "OFF")]),
                      call("revise_edit", remove=[{"nurse": "A", "day": 3}], edit_from_day=3))
    say(agent, world.conv, "A 3일 오프")
    again = say(agent, world.conv, "A 3일 건은 취소하고 2일까지는 그대로 둬")
    assert again.answer == ("3일부터 고칩니다(2일까지는 그대로 둡니다).\n"
                            "남은 변경 요청이 없습니다. 무엇을 바꿀지 말씀해 주세요.")
    assert world.conv.state.ctx.edit_from == 2


def test_keeping_a_closed_day_is_not_a_request(world):
    # '오늘은 건드리지 마' — 오늘은 원래 닫혀 있다. 그대로 두기 칸을 넣으면 닫힌 날 요청이 돼 요청 전체가 거절됐다
    agent, _ = _agent(call("propose_edit", changes=[_set("A", 3, "OFF")]), call("revise_edit", keep=[{"day": 2}]))
    say(agent, world.conv, "A 3일 오프", TODAY)
    kept = say(agent, world.conv, "2일은 건드리지 마", TODAY)
    assert kept.preview["status"] == "ok" and world.conv.state.requests == [SetCell("A", 2, "O")]


def test_earlier_requests_follow_the_new_first_day():
    ctx = _ctx()
    count = CountTarget("A", frozenset({"D"}), 1, 0, 3)            # A D 2개(1·3일) → 1개
    wish = WishTarget((), 0, 3)
    balance = BalanceTarget(frozenset({"D"}), ("A", "B"), 0, 3)
    moved, facts = _from_open(ctx, [count, wish, balance, SetCell("A", 0, "D"), SetCell("A", 0, "O")], 2)
    # 개수는 닫힌 날(1일 D 1개)을 빼 합을 지킨다 · '그대로 두기'(A 1일 D)는 닫힌 날이라 뺀다 · 닫힌 날 칸 요청은 남겨 이유를 말하게
    assert moved == [CountTarget("A", frozenset({"D"}), 0, 2, 3, asked_first=0, asked_target=1), WishTarget((), 2, 3),
                     BalanceTarget(frozenset({"D"}), ("A", "B"), 2, 3), SetCell("A", 0, "O")]
    assert facts == []
    # 닫힌 날에 이미 목표보다 많으면 바꿀 수 있는 몫을 0 으로 깎고 사실을 알린다
    moved, facts = _from_open(ctx, [CountTarget("A", frozenset({"D"}), 0, 0, 3)], 2)
    assert moved == [CountTarget("A", frozenset({"D"}), 0, 2, 3, asked_first=0, asked_target=0)]
    assert facts == ["A D은(는) 고치지 않는 날(1·2일)에 이미 1개가 있어 그만큼은 남습니다."]


# ── 이어 가기(다시 열기·적용·복원) ──────────────────────────────────────────


def test_moving_the_chosen_day_earlier_brings_the_original_requests_back():
    # 정한 날을 뒤로 정했다가 다시 당기면 줄였던 기간·목표가 원래대로(Codex 11-05) — 늘 원래 요청에서 다시 맞춘다
    ctx = _ctx()
    count = CountTarget("A", frozenset({"D"}), 1, 0, 3)            # A D 2개(1·3일) → 1개
    said = WishTarget((), 1, 3, ranged=True)                        # '2~4일 원티드'
    implied = WishTarget((), 0, 3)                                  # 기간을 말하지 않음
    later, _ = _from_open(ctx, [count, said, implied], 3)
    assert later == [CountTarget("A", frozenset({"D"}), 0, 3, 3, asked_first=0, asked_target=1),
                     WishTarget((), 3, 3, ranged=True, asked_first=1), WishTarget((), 3, 3)]
    back, _ = _from_open(ctx, later, 1)                             # 2일부터로 당김 — 말한 기간 첫날(2일)까지만 되살린다
    assert back == [CountTarget("A", frozenset({"D"}), 0, 1, 3, asked_first=0, asked_target=1),
                    WishTarget((), 1, 3, ranged=True), WishTarget((), 1, 3)]
    whole, _ = _from_open(ctx, back, 0)                             # 처음으로 — 줄이기 전 요청 그대로
    assert whole == [count, said, implied]
    # 기록에 실려 재기동 뒤에도 되살릴 수 있다
    assert requests_from_json(requests_to_json(later)) == later


def test_moving_the_chosen_day_earlier_in_conversation_restores_the_count(world):
    agent, _ = _agent(call("propose_edit", changes=[{"kind": "count", "nurse": "A", "shift": "D", "change": -1}]),
                      call("revise_edit", edit_from_day=4), call("revise_edit", edit_from_day=1))
    say(agent, world.conv, "A D 하나 빼줘", BEFORE_MONTH)
    first = world.conv.state.requests[0]
    say(agent, world.conv, "3일까지는 그대로 둬", BEFORE_MONTH)
    assert world.conv.state.requests[0].first_day == 3
    say(agent, world.conv, "아니 처음부터 고쳐줘", BEFORE_MONTH)
    assert world.conv.state.requests[0] == dataclasses.replace(first, transfer=world.conv.state.requests[0].transfer)


def test_the_chosen_day_survives_reopening_and_moving_to_the_new_version(world, monkeypatch):
    stale = {"on": False}
    monkeypatch.setattr(agent_mod, "is_stale", lambda db, s: stale["on"])
    saved = {"new_schedule_id": "s2", "new_version": 2, "new_name": "11월 수정본"}
    monkeypatch.setattr(skills_mod, "apply_to_new_version", lambda *a, **kw: saved)
    agent, _ = _agent(call("propose_edit", changes=[], edit_from_day=3),
                      call("propose_edit", changes=[_set("A", 3, "OFF")]),
                      call("propose_edit", changes=[_set("A", 1, "OFF")]))
    say(agent, world.conv, "2일까지는 고정해")
    first = world.conv.state
    stale["on"] = True                                                       # 그사이 칸이 바뀌어 다시 연다
    say(agent, world.conv, "A 3일 오프")
    assert world.conv.state is not first and world.conv.state.ctx.edit_from == 2     # 다시 연 판정 재료에도
    stale["on"] = False
    press(agent, world.conv, "apply_edit", {"rank": 1})
    assert world.conv.context == {"schedule_id": "s2"} and world.conv.state is None
    after = say(agent, world.conv, "A 1일 오프")
    assert world.conv.state.schedule_id == "s2" and world.conv.state.ctx.edit_from == 2
    assert LOCKED in after.answer


def test_the_chosen_day_is_not_used_for_another_month(world):
    def december():
        ctx = _two_nurse_ctx()
        ctx.month = 12
        return ctx

    world.ctxs["s3"] = december
    agent, _ = _agent(call("propose_edit", changes=[], edit_from_day=3), call("reply_only", kind="status"))
    say(agent, world.conv, "2일까지는 고정해")
    world.conv.move_to({"schedule_id": "s3"})                               # 같은 대화로 다른 달 근무표
    say(agent, world.conv, "지금 어떻게 돼 있어?")
    assert world.conv.state.ctx.edit_from is None


def test_restore_brings_the_chosen_day_back_across_versions(world, monkeypatch):
    req = [{"kind": "set", "nurse_id": "A", "day": 3, "code": "O"}]
    rows = [_row("message", 1, {"text": "2일까지는 고정해", "tool": "propose_edit"}),
            _row("lock", 1, {"edit_from_day": 3, "year": 2026, "month": 11}),
            _row("clarify", 1, {"answer": "3일부터 고칩니다(2일까지는 그대로 둡니다). 바꿀 내용을 말씀해 주세요."}),
            _row("propose", 2, {"requests": req, "allow": [], "avoid": []}),
            _row("apply", 3, {}, new_schedule_id="s2")]
    _logs(monkeypatch, rows)
    store = new_store()
    conv = store.checkout(None, "c-restored", USER.account_id)
    store.checkin(conv)
    assert conv.context == {"schedule_id": "s2"} and conv.carry == {"edit_from": (2026, 11, 2)}
    assert conv.restored["requests"] == []                                  # 적용 뒤라 요청은 비우고 기준일만
    agent, _ = _agent(call("propose_edit", changes=[_set("A", 1, "OFF")]))
    reply = say(agent, conv, "A 1일 오프")
    assert conv.state.ctx.edit_from == 2 and LOCKED in reply.answer


def test_restore_reads_the_last_lock_alone_when_the_conversation_is_longer_than_the_window(monkeypatch):
    # 최근 READ_LIMIT 건만 읽는다 — 그 안에 lock 이 없고 창이 꽉 찼을 때만 마지막 lock 한 건을 따로(검토 L4)
    asked = []
    monkeypatch.setattr(store_mod, "READ_LIMIT", 2)
    monkeypatch.setattr(store_mod, "read_last_lock", lambda db, cid: asked.append(cid) or
                        _row("lock", 1, {"edit_from_day": 4, "year": 2026, "month": 11}))
    _logs(monkeypatch, [_row("message", 9, {"text": "x", "reply": "y"}), _row("message", 10, {"text": "z", "reply": "w"})])
    assert store_mod.restore(None, "c1").carry == {"edit_from": (2026, 11, 3)} and asked == ["c1"]
    _logs(monkeypatch, [_row("message", 1, {"text": "x", "reply": "y"})])         # 창 안에 다 들어왔다 — lock 이 없는 대화
    assert store_mod.restore(None, "c1").carry == {} and asked == ["c1"]


def test_moving_the_target_keeps_carry():
    conv = Conversation(id="c", agent="a", owner="o", context={"schedule_id": "s1"}, created_at=0.0, touched_at=0.0)
    conv.state, conv.carry["edit_from"] = object(), (2026, 11, 2)
    conv.move_to({"schedule_id": "s2"})
    assert conv.state is None and conv.carry == {"edit_from": (2026, 11, 2)}
