"""후보를 본 뒤 규칙을 묻는 말(11-03 · 사용자 '안건을 알려 준 상태에서 그레이드는 다 지켜져? 등을 물으면 안내가 나가야').

후보는 원래 어긋나 있던 곳을 '더 나빠지지 않게'만 지키므로, 답은 '모두 지킴'으로 뭉뚱그리지 않고 새로 넘긴 곳 ·
원래 어긋나 있던 곳 · 풀린 곳을 나눠 말한다.
"""

from __future__ import annotations

from schedule_edit_agent.view import blocks_for
from services.schedule_edit import solver as S
from services.schedule_edit.requests import SetCell
from services.schedule_edit.rule_check import TOPICS, check_candidate
from tests.edit_agent.test_agent import BEFORE_MONTH, _agent, call, make_ctx, say

LOOSE = {"not_one_night": False, "isolated_work_hard": False}


def _ward(grade_one=("A", "B")):
    """1등급(A·B)이 매일 D 에 1명 있어야 한다. 3일 D 는 원래 2등급 C 뿐이라 이미 어긋나 있다."""
    ctx = make_ctx({"A": "DOOD", "B": "ODOO", "C": "OODO"}, cov_min={"D": 1}, cov_max={"D": 1}, config=LOOSE)
    ctx.grade_of.update({n: (1 if n in grade_one else 2) for n in ("A", "B", "C")})
    ctx.grade_min = {"D": {1: 1}}
    return ctx


def test_grade_question_after_candidates_names_what_was_already_off(world):
    world.ctxs["s1"] = _ward
    agent, _ = _agent(call("propose_edit", changes=[{"kind": "set", "nurse": "A", "day": 1, "shift": "OFF"}]),
                      call("check_rules", topic="grade"),
                      call("check_rules", topic="grade", rank=1),
                      call("check_rules", topic="grade", rank=9))
    assert say(agent, world.conv, "A 1일 오프", BEFORE_MONTH).preview["status"] == "ok"
    reply = say(agent, world.conv, "그레이드는 다 지켜져?", BEFORE_MONTH)
    assert reply.answer.startswith("등급별 최소 인원: ")
    assert "새로 어기는 곳이 없습니다" in reply.answer
    assert "원래 근무표에서 어긋나 있던 1곳은 그대로입니다(더 나빠지지 않음) — 3일 D 1등급까지 최소 인원보다 1명 적음." \
        in reply.answer
    # 묻고 나서도 후보 카드는 그대로 보인다(문장만 온 턴은 보던 결과를 다시 그린다)
    assert blocks_for(reply, world.conv.state)[-1]["type"] == "cards"
    assert world.conv.state.requests and world.conv.state.last.status == "ok"     # 요청·후보를 바꾸지 않는다
    one = say(agent, world.conv, "1안은?", BEFORE_MONTH)
    assert one.answer.startswith("등급별 최소 인원: 1안은 새로 어기는 곳이 없습니다.") and one.focus == 1
    assert say(agent, world.conv, "9안은?", BEFORE_MONTH).answer.startswith("후보는 ")


def test_without_candidates_the_saved_schedule_is_checked(world):
    world.ctxs["s1"] = _ward
    agent, _ = _agent(call("check_rules", topic="grade"), call("check_rules", topic="mutex"))
    assert say(agent, world.conv, "지금 그레이드 다 지켜져?", BEFORE_MONTH).answer == (
        "등급별 최소 인원: 지금 저장된 근무표에서 어긋난 곳 1곳 — 3일 D 1등급까지 최소 인원보다 1명 적음.")
    assert say(agent, world.conv, "상호배제는?", BEFORE_MONTH).answer == (
        "이 근무표 설정에는 상호배제 규칙이 없어 따질 것이 없습니다.")


def test_a_setting_passed_on_purpose_is_said_as_passed():
    # 1등급이 A 뿐이면 A 1일 OFF 는 1등급을 비워야 한다 — 그 설정을 이번만 넘긴(레버) 후보는 '이번만 넘기는 곳'으로 말한다
    ctx = _ward(grade_one=("A",))
    allow = {("등급최소", None, 0, "D≤1"): 1}
    result = S.propose_edits(ctx, [SetCell("A", 0, "O")], today=BEFORE_MONTH, allow=allow)
    assert result.status == "ok"
    row = check_candidate(ctx, None, result.candidates[0], [SetCell("A", 0, "O")], TOPICS["grade"][1])
    assert row.new == ["1일 D 1등급까지 최소 인원보다 1명 적게"]
    assert row.kept == ["2일 D 1등급까지 최소 인원보다 1명 적음", "3일 D 1등급까지 최소 인원보다 1명 적음"]   # 원래 B·C 뿐


def test_shape_and_everything_questions(world):
    world.ctxs["s1"] = _ward
    agent, _ = _agent(call("propose_edit", changes=[{"kind": "set", "nurse": "A", "day": 1, "shift": "OFF"}]),
                      call("check_rules", topic="all"), call("check_rules", topic="shape"))
    say(agent, world.conv, "A 1일 오프", BEFORE_MONTH)
    every = say(agent, world.conv, "다 지켜져?", BEFORE_MONTH).answer
    assert every.startswith("규칙(하드락·병동 설정): ") and "3일 D 1등급까지 최소 인원보다 1명 적음" in every
    shape = say(agent, world.conv, "근무 모양은?", BEFORE_MONTH).answer
    assert shape.startswith("근무 모양(규칙 위반은 아님):\n· 1안은 나빠지는 곳이 없습니다.")
