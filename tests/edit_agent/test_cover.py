"""그날 인원 요청(11-03 · 사용자 '그날 인원') — '15일 E 한 명 더'·'20일 N 한 명 빼'·'20일 N 에 1등급 넣어'."""

from __future__ import annotations

from schedule_edit_agent.answer import cover_note
from schedule_edit_agent.grounding import ground_changes
from services.schedule_edit.requests import CoverTarget, SetCell, cover_count, request_problem
from services.schedule_edit.rules import coverage_bounds
from services.schedule_edit.serialize import requests_from_json, requests_to_json
from services.schedule_edit.solver import verify_candidate
from tests.edit_agent.test_agent import BEFORE_MONTH, _agent, call, make_ctx, press, say


def _ward():
    """1일 D 는 A 한 명(최소·최대 1). B·C 는 그날 OFF — 한 명 더면 둘 중 하나가 D 로."""
    ctx = make_ctx({"A": "DDOO", "B": "OODD", "C": "ODOD"}, cov_min={"D": 1}, cov_max={"D": 1},
                   config={"not_one_night": False, "isolated_work_hard": False})
    ctx.grade_of.update({"A": 2, "B": 1, "C": 2})
    return ctx


def test_cover_words_become_a_day_headcount_request():
    ctx = _ward()
    out = ground_changes(ctx, [{"kind": "cover", "day": 1, "shift": "D", "change": 1}])
    assert out.requests == [CoverTarget(0, "D", 2)] and not out.asks
    out = ground_changes(ctx, [{"kind": "cover", "day": 1, "shift": "D"}])
    assert out.asks == ["1일 D 는 지금 1명입니다(설정 최소 1명 · 최대 1명). 몇 명으로 바꿀까요?"]
    out = ground_changes(ctx, [{"kind": "cover", "day": 1, "shift": "D", "target": 1}])
    assert out.asks == ["1일 D 는 이미 1명입니다."]
    out = ground_changes(ctx, [{"kind": "cover", "day": 1, "shift": "D", "grade": 1, "change": 1}])
    assert out.requests == [CoverTarget(0, "D", 1, 1)]                 # 1등급까지인 사람 0 → 1
    assert cover_count(ctx, ctx.main, CoverTarget(0, "D", 0, 1)) == 0


def test_grade_without_direction_or_with_words_asks_back():
    """등급만 말하고 넣기·빼기가 없으면 되묻는다 · 숫자 칸에 글이 오면 예외 대신 되묻는다(검토 11-03 LOW 6·7)."""
    ctx = _ward()
    out = ground_changes(ctx, [{"kind": "cover", "day": 1, "shift": "D", "grade": 2}])
    assert out.asks == ["1일 D 는 지금 2등급까지인 사람 1명입니다. 한 명 더 넣을까요, 뺄까요?"] and not out.requests
    out = ground_changes(ctx, [{"kind": "cover", "day": 1, "shift": "D", "grade": "1등급", "change": 1}])
    assert out.asks and "숫자로" in out.asks[0]
    out = ground_changes(ctx, [{"kind": "count", "nurse": "A", "shift": "D", "change": "두 개"}])
    assert out.asks and "숫자로" in out.asks[0]


def test_bounds_of_a_shift_the_day_does_not_set():
    """그날 그 근무 설정이 없으면 넘길 인원 규칙도 없다 — '최대 인원(0명)보다' 같은 안내를 내지 않는다(검토 LOW 4)."""
    ctx = _ward()
    assert coverage_bounds(ctx, 0, "E") == (0, None, None)
    assert cover_note(ctx, CoverTarget(0, "E", 1)) == ""
    assert coverage_bounds(ctx, 0, "D") == (1, 1, None)


def test_cannot_go_below_the_people_locked_on_that_shift():
    """확정 원티드·파견처럼 묶인 칸으로 그 근무를 하는 사람보다 적게는 못 줄인다 — 솔버 대신 이유를 바로(검토 LOW 5)."""
    ctx = _ward()
    ctx.frozen[("A", 0)] = "확정 원티드"
    assert "묶여 있어 0명으로 줄일 수 없습니다" in request_problem(ctx, CoverTarget(0, "D", 0), 0)
    assert request_problem(ctx, CoverTarget(0, "D", 2), 0) is None


def test_revise_keeps_other_shift_headcounts_and_replaces_the_same_one(world):
    """같은 날 다른 근무의 인원 요청은 남기고, 같은 날·근무·등급이면 새 것으로 바꾼다(검토 MEDIUM 1)."""
    world.ctxs["s1"] = _ward
    agent, _ = _agent(call("propose_edit", changes=[{"kind": "cover", "day": 1, "shift": "D", "change": 1}]),
                      call("revise_edit", add=[{"kind": "cover", "day": 1, "shift": "E", "change": 1}]),
                      call("revise_edit", add=[{"kind": "cover", "day": 1, "shift": "D", "target": 3}]))
    say(agent, world.conv, "1일 데이 한 명 더")
    say(agent, world.conv, "1일 이브닝도 한 명 더")
    assert world.conv.state.requests == [CoverTarget(0, "D", 2), CoverTarget(0, "E", 1)]
    say(agent, world.conv, "1일 데이는 3명으로")
    assert world.conv.state.requests == [CoverTarget(0, "E", 1), CoverTarget(0, "D", 3)]


def test_revise_keep_or_remove_the_whole_day_drops_its_headcount(world):
    """'1일은 그대로 둬'·'1일 요청 빼' — 그날 인원 요청도 같이 빠진다(남기면 모순·뺄 길이 없었다 · 검토 MEDIUM 2·3)."""
    world.ctxs["s1"] = _ward
    agent, _ = _agent(call("propose_edit", changes=[{"kind": "cover", "day": 1, "shift": "D", "change": 1}]),
                      call("revise_edit", keep=[{"day": 1}]),
                      call("propose_edit", changes=[{"kind": "cover", "day": 1, "shift": "D", "change": 1},
                                                    {"kind": "set", "nurse": "C", "day": 4, "shift": "OFF"}]),
                      call("revise_edit", remove=[{"day": 1}]))
    say(agent, world.conv, "1일 데이 한 명 더")
    assert "남은 변경 요청이 없습니다" in say(agent, world.conv, "1일은 그대로 둬").answer
    say(agent, world.conv, "1일 데이 한 명 더, C 4일 오프")
    say(agent, world.conv, "1일 건은 빼")
    assert world.conv.state.requests == [SetCell("C", 3, "O")]


def test_one_more_on_a_day_moves_someone_in_and_names_the_setting_it_passes(world):
    world.ctxs["s1"] = _ward
    agent, _ = _agent(call("propose_edit", changes=[{"kind": "cover", "day": 1, "shift": "D", "change": 1}]))
    asked = say(agent, world.conv, "1일 데이 한 명 더")
    # 최대 인원을 넘기는 것은 요청 자체가 정한다 — 풀기 전에 먼저 묻는다(사용자 지시 11-03)
    assert asked.preview["status"] == "confirm"
    assert "· 1일 D 1명 → 2명 — 최대 근무 인원(1명)보다 1명 많음" in asked.answer
    reply = press(agent, world.conv, "confirm_request", {})
    assert reply.preview["status"] == "ok", reply.answer
    assert reply.answer.startswith("1일 D 1→2명 — 바꿀 방법")
    assert "요청대로 1일 D를 최대 인원(1명)보다 1명 많이 둡니다(이번만 · 병동 설정은 그대로)." in reply.answer
    session = world.conv.state
    cand = session.last.candidates[0]
    assert any(c.day == 0 and c.after_main == "D" and c.nurse_id in ("B", "C") for c in cand.changes)
    assert verify_candidate(session.ctx, session.rules, cand, session.requests, today=BEFORE_MONTH) == []
    assert asked.request_line == "1일 D 1→2명"                          # 흔적에도 해석한 요청(말 턴)


def test_one_less_on_a_day(world):
    world.ctxs["s1"] = lambda: make_ctx({"A": "DDOO", "B": "ODDO", "C": "OODD"}, cov_min={"D": 1},
                                        config={"not_one_night": False, "isolated_work_hard": False})
    agent, _ = _agent(call("propose_edit", changes=[{"kind": "cover", "day": 2, "shift": "D", "change": -1}]))
    reply = say(agent, world.conv, "2일 데이 한 명 빼")
    assert reply.preview["status"] == "ok", reply.answer
    cand = world.conv.state.last.candidates[0]
    assert any(c.day == 1 and c.before_main == "D" and c.after_main != "D" for c in cand.changes)


def test_cover_requests_survive_the_record():
    reqs = [CoverTarget(14, "E", 4), CoverTarget(19, "N", 2, 1)]
    assert requests_from_json(requests_to_json(reqs)) == reqs
