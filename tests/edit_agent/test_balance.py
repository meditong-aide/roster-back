"""여러 사람의 근무 수 고르게 맞추기(11-03 · 사용자 '그 근무 가능한 전원') — 'N 근무 수 고르게 맞춰줘'·'OFF 다 9개로'."""

from __future__ import annotations

from schedule_edit_agent.grounding import ground_changes
from services.schedule_edit.requests import BalanceTarget, balance_counts, balance_spread, request_problem
from services.schedule_edit.rules import build_rules
from services.schedule_edit.serialize import requests_from_json, requests_to_json
from services.schedule_edit.solver import apply_candidate, min_balance_spread, propose_edits, verify_candidate
from tests.edit_agent.test_agent import BEFORE_MONTH, _agent, call, make_ctx, say

LOOSE = {"not_one_night": False, "isolated_work_hard": False, "two_offs_after_two_nig": False,
         "two_offs_after_three_nig": False, "three_seq_nig": False}


def _ward():
    """하루 N 1명. N 이 A 4·B 2·C 0 으로 몰려 있다(합 6 → 셋이 2개씩이면 차이 0)."""
    return make_ctx({"A": "NNOONN", "B": "OONNOO", "C": "DODODO"}, cov_min={"N": 1}, config=LOOSE)


def test_balance_words_cover_everyone_who_can_do_the_shift():
    ctx = _ward()
    out = ground_changes(ctx, [{"kind": "balance", "shift": "N"}])
    assert out.requests == [BalanceTarget(frozenset({"N"}), ("A", "B", "C"), 0, 5)] and not out.asks
    ctx.n_only.add("A")                                     # N 전담은 원래 N 이 많다 — 대상에서 뺀다
    ctx.forbidden.update({("C", d): {"N"} for d in range(6)})   # N 을 할 수 없는 사람도 뺀다
    assert ground_changes(ctx, [{"kind": "balance", "shift": "N"}]).requests[0].members == ("B",)
    assert "두 명보다 적습니다" in request_problem(ctx, BalanceTarget(frozenset({"N"}), ("B",), 0, 5), 0)
    asks = ground_changes(ctx, [{"kind": "balance", "shift": "N", "target": "많이"}]).asks
    assert asks and "숫자로" in asks[0]


def test_smallest_spread_is_found_then_the_fewest_changes():
    ctx = _ward()
    req = BalanceTarget(frozenset({"N"}), ("A", "B", "C"), 0, 5)
    assert balance_spread(ctx, ctx.main, req) == 4
    spread, proven = min_balance_spread(ctx, [req], today=BEFORE_MONTH)
    assert (spread, proven) == (0, True)
    fixed = BalanceTarget(req.codes, req.members, 0, 5, spread=0)
    res = propose_edits(ctx, [fixed], today=BEFORE_MONTH)
    assert res.status == "ok", res.reasons
    cand = res.candidates[0]
    assert set(balance_counts(ctx, apply_candidate(ctx, cand), fixed).values()) == {2}
    assert verify_candidate(ctx, build_rules(ctx), cand, [fixed], today=BEFORE_MONTH) == []


def test_agent_answers_with_before_and_after_range(world):
    world.ctxs["s1"] = _ward
    agent, _ = _agent(call("propose_edit", changes=[{"kind": "balance", "shift": "N"}]))
    reply = say(agent, world.conv, "N 근무 수 고르게 맞춰줘", BEFORE_MONTH)
    assert reply.preview["status"] == "ok", reply.answer
    assert reply.answer.startswith("N 근무 수 고르게 0~4회(차이 4) → 차이 0 이하 · 3명 — 바꿀 방법")
    assert "맞춘 뒤 2~2회(차이 0)." in reply.answer
    assert reply.request_line.startswith("N 근무 수 고르게")
    session = world.conv.state
    assert session.requests[0].spread == 0                 # 찾은 차이를 확정 조건으로 — 적용 직전 재확인도 같은 기준
    assert verify_candidate(session.ctx, session.rules, session.last.candidates[0], session.requests,
                            today=BEFORE_MONTH) == []


def test_same_count_for_everyone_and_records():
    ctx = _ward()
    every = ground_changes(ctx, [{"kind": "balance", "shift": "N", "target": 2}]).requests[0]
    assert every.target == 2
    res = propose_edits(ctx, [every], today=BEFORE_MONTH)
    assert res.status == "ok" and set(balance_counts(ctx, apply_candidate(ctx, res.candidates[0]), every).values()) == {2}
    # 바꿀 수 없는 칸 때문에 못 맞추는 수는 미리 거절
    ctx.frozen.update({("A", d): "관할 밖(파견·이동·휴직·프리셉티)" for d in range(6)})
    assert "맞출 수 없습니다" in request_problem(ctx, every, 0)
    assert requests_from_json(requests_to_json([every])) == [every]


def test_rules_keep_the_spread_above_the_arithmetic_best_and_the_answer_says_why(world):
    # C 는 6일만 바꿀 수 있어 N 이 많아야 1개 — 산술로는 셋이 2개씩(차이 0)이지만 규칙 안 최선은 차이 2(3·2·1 또는 2·3·1)
    def ward():
        ctx = _ward()
        ctx.frozen.update({("C", d): "주휴·특수 휴무" for d in range(5)})
        return ctx
    world.ctxs["s1"] = ward
    agent, _ = _agent(call("propose_edit", changes=[{"kind": "balance", "shift": "N"}]))
    reply = say(agent, world.conv, "N 근무 수 고르게 맞춰줘", BEFORE_MONTH)
    assert reply.preview["status"] == "ok", reply.answer
    assert world.conv.state.requests[0].spread == 2
    assert ("참고: 산술로는 차이 0까지 맞출 수 있지만, 지금 규칙을 모두 지키며 바꾸는 칸 40개 안에서는 차이 2보다 줄일 "
            "방법이 없습니다.") in reply.answer
