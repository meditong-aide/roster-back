"""반영 안 된 원티드 넣기(11-03 · 사용자 결정 '원티드 반영') — 규칙을 지키며 가장 많이, 그다음 가장 적게 바꾸기."""

from __future__ import annotations

from schedule_edit_agent import skills as skills_mod
from schedule_edit_agent.grounding import ground_changes
from schedule_edit_agent.view import _lever_cards
from services.schedule_edit.requests import WishTarget, is_satisfied, wish_targets
from services.schedule_edit.serialize import requests_from_json, requests_to_json
from services.schedule_edit.solver import max_wish_count
from tests.edit_agent.test_agent import BEFORE_MONTH, _agent, call, make_ctx, say

LOOSE = {"not_one_night": False, "two_offs_after_two_nig": False, "isolated_work_hard": False}


def _ward():
    """A 1일 D 원티드는 전월 말 N 뒤라 N→D 전환 금지(하드락)로 못 넣고, B 2일 D 원티드는 넣을 수 있다. C 3일 OFF 는 이미 반영."""
    ctx = make_ctx({"A": "ODOD", "B": "DODO", "C": "DDOO"}, cov_min={"D": 1}, config=LOOSE)
    ctx.tail = {"A": ["O", "N", "N"], "B": ["O", "O", "O"], "C": ["O", "O", "O"]}
    ctx.wishes = {("A", 0): {"D"}, ("B", 1): {"D"}, ("C", 2): {"O"}}
    ctx.leave_codes = {"연": "연차"}
    ctx.wish_skipped = [("A", 1, "code", "연"), ("B", 2, "code", "연")]
    return ctx


def test_targets_are_the_unreflected_wishes_and_the_most_that_fit_is_found():
    ctx = _ward()
    req = WishTarget((), 0, ctx.days - 1)
    assert [(n, d) for n, d, _c in wish_targets(ctx, req)] == [("A", 0), ("B", 1)]   # C 3일은 이미 반영
    best, proven, total, grid = max_wish_count(ctx, [req], today=BEFORE_MONTH)
    assert (best, proven, total) == (1, True, 2) and grid["B"][1] == "D"
    assert not is_satisfied(ctx, req) and is_satisfied(ctx, WishTarget((), 0, 3, at_least=1), grid)
    only_b = ground_changes(ctx, [{"kind": "wanted", "nurse": "B", "day": 2, "to_day": 3}]).requests
    assert only_b == [WishTarget(("B",), 1, 2, ranged=True)]
    assert requests_from_json(requests_to_json(only_b)) == only_b


def test_agent_puts_in_what_fits_and_says_why_the_rest_cannot(world):
    world.ctxs["s1"] = _ward
    agent, _ = _agent(call("propose_edit", changes=[{"kind": "wanted", "nurse": ""}]))
    reply = say(agent, world.conv, "반영 안 된 원티드 넣어줘", BEFORE_MONTH)
    assert reply.preview["status"] == "ok", reply.answer
    assert reply.answer.startswith("원티드 반영(전원 · 미반영 2개) — 바꿀 방법")       # 기간을 말하지 않으면 날짜 없음
    assert "미반영 2개 중 1개를 넣습니다." in reply.answer
    assert "못 넣는 원티드 1개:\n· A 1일을 D로 하면 N→D 전환 금지에 걸립니다" in reply.answer
    assert "휴가 원티드 2개는" in reply.answer                   # 휴가 코드 원티드는 따로
    assert reply.preview["levers"] == []                         # 하드락은 넘길 수 없다 — '하나 더' 카드 없음
    assert world.conv.state.requests[0].at_least == 1            # 찾은 수를 확정 조건으로(적용 직전 재확인도 같은 기준)


def test_nothing_left_to_put_in(world):
    def done():
        ctx = _ward()
        ctx.wishes = {("C", 2): {"O"}}
        return ctx
    world.ctxs["s1"] = done
    agent, _ = _agent(call("propose_edit", changes=[{"kind": "wanted", "nurse": ""}]))
    reply = say(agent, world.conv, "원티드 반영해줘", BEFORE_MONTH)
    assert reply.answer.startswith("바꿀 수 있는 칸의 원티드는 모두 반영되어 있습니다.")


def test_one_more_lever_reads_in_wishes_and_the_count_is_found_again_each_time(world):
    card = _lever_cards([{"lever_id": "x", "lose": ["8일 N 최소 근무 인원보다 1명 적게"], "gain": "원티드 16개",
                          "changes": {"now": None, "after": 49}, "span_days": {"now": None, "after": 22},
                          "quality": []}])["items"][0]
    assert card["lines"][0]["text"] == "이 설정을 이번만 넘기시면 원티드 16개를 넣을 수 있습니다(49칸 바꿈)"
    # 넣을 수는 늘 다시 찾는다 — 앞에서 정한 수가 남아 있으면 넘기기·레버·고쳐 말하기 뒤에 '방법 없음'이 된다(검토 11-03 M1)
    world.ctxs["s1"] = _ward
    agent, _ = _agent(call("propose_edit", changes=[{"kind": "wanted", "nurse": ""}]))
    say(agent, world.conv, "원티드 반영해줘", BEFORE_MONTH)
    session = world.conv.state
    stale = [WishTarget((), 0, 3, at_least=5)]
    turn = skills_mod.Turn(db=None, user=None, conversation_id="c", turn_no=2, session=session, today=BEFORE_MONTH,
                           deadline=float("inf"))
    again, info = skills_mod._resolve_wish(turn, stale)
    assert again[0].at_least == 1 and info["best"] == 1


def test_skipped_wishes_count_only_inside_the_request():
    from schedule_edit_agent.answer import wish_skipped_note
    ctx = _ward()
    ctx.leave_codes = {"연": "연차"}
    ctx.wish_skipped = [("A", 0, "code", "연"), ("B", 3, "code", "연"), ("C", 1, "locked", "D"), ("A", 2, "code", "X")]
    assert wish_skipped_note(ctx, WishTarget((), 1, 3)) == (
        "휴가 원티드 1개는 '○○ ○일 연차 넣어줘'처럼 따로 말씀해 주세요. 근무·OFF·휴가가 아닌 원티드 1개는 대상이 아닙니다. "
        "바꿀 수 없는 칸(파견·프리셉티 등)의 원티드 1개는 대상이 아닙니다.")
    assert wish_skipped_note(ctx, WishTarget(("B",), 0, 3)) == "휴가 원티드 1개는 '○○ ○일 연차 넣어줘'처럼 따로 말씀해 주세요."
