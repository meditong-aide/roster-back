"""연차·휴가 칸 넣기와 조회(11-03 · 사용자 '연차·휴가 칸 + 조회') — 가짜 LLM · 손 근무표."""

from __future__ import annotations

from schedule_edit_agent.grounding import resolve_leave
from services.schedule_edit.apply import roster_payload
from services.schedule_edit.solver import verify_candidate
from tests.edit_agent.test_agent import BEFORE_MONTH, _agent, call, make_ctx, say

LEAVE = {"연": "연차", "공": "공가", "생": "생휴"}


def _ward():
    """A 가 1일 D — 연차로 빠지면 그날 D 1명을 C 가 메운다(C 는 모두 OFF · OFF 수 설정 없음)."""
    ctx = make_ctx({"A": "DODO", "B": "ODOD", "C": "OOOO"}, cov_min={"D": 1}, config={"not_one_night": False})
    ctx.leave_codes = dict(LEAVE)
    return ctx


def test_leave_names_resolve_to_the_ward_codes():
    ctx = _ward()
    assert resolve_leave(ctx, "연차") == ("연", None)
    assert resolve_leave(ctx, "공") == ("공", None)
    code, ask = resolve_leave(ctx, "휴가")
    assert code is None and "연(연차)" in ask                          # 여럿이면 되묻는다
    ctx.leave_codes["휴"] = "휴가"
    assert resolve_leave(ctx, "휴가") == ("휴", None)                  # 이름이 정확히 같은 코드가 있으면 그것
    code, ask = resolve_leave(ctx, "출장")
    assert code is None and "없습니다" in ask
    ctx.leave_codes = {}
    assert "휴가·공가 근무 코드가 없습니다" in resolve_leave(ctx, "연차")[1]


def test_leave_on_a_work_day_is_filled_by_someone_else(world):
    world.ctxs["s1"] = _ward
    agent, _ = _agent(call("propose_edit", changes=[{"kind": "leave", "nurse": "A", "day": 1, "leave": "연차"}]))
    reply = say(agent, world.conv, "A 1일 연차 넣어줘")
    assert reply.preview["status"] == "ok", reply.answer
    assert reply.answer.startswith("A 1일 연차 — 바꿀 방법")
    session = world.conv.state
    cand = session.last.candidates[0]
    leave = [c for c in cand.changes if (c.nurse_id, c.day) == ("A", 0)]
    assert leave and leave[0].after == "연" and leave[0].vacation and leave[0].after_main == "O"
    assert any(c.nurse_id == "C" and c.day == 0 and c.after_main == "D" for c in cand.changes)   # 빠진 D 를 메운다
    assert verify_candidate(session.ctx, session.rules, cand, session.requests, today=BEFORE_MONTH) == []
    rows = {r["nurse_id"]: r["schedule"] for r in roster_payload(session, cand)}
    assert rows["A"][0] == "연"


def test_leave_is_not_put_on_a_day_off(world):
    world.ctxs["s1"] = _ward
    agent, _ = _agent(call("propose_edit", changes=[{"kind": "leave", "nurse": "A", "day": 2, "leave": "연차"}]),
                      call("propose_edit", changes=[{"kind": "leave", "nurse": "C", "day": 1, "to_day": 4,
                                                     "leave": "연차"}]))
    reply = say(agent, world.conv, "A 2일 연차")
    assert reply.preview["status"] == "invalid" and "이미 쉬는 날이라 연차를 넣지 않습니다" in reply.answer
    assert "모두 쉬는 날이라" in say(agent, world.conv, "C 1~4일 연차").answer      # 기간이 전부 쉬는 날


def test_lookup_answers_from_the_saved_schedule(world):
    world.ctxs["s1"] = _ward
    agent, _ = _agent(call("lookup", kind="day_shift", day=1, shift="D"),
                      call("lookup", kind="person_count", nurse="A", shift="OFF"),
                      call("lookup", kind="person_day", nurse="B", day=2),
                      call("lookup", kind="person_day", nurse="Z", day=2))
    assert say(agent, world.conv, "1일 데이 누구야").answer == "1일 D 1명: A. (지금 저장된 근무표 기준)"
    assert say(agent, world.conv, "A OFF 몇 개야").answer.startswith("A 11월 OFF 2회입니다")
    assert say(agent, world.conv, "B 2일 뭐야").answer == "B 2일은 D입니다(지금 저장된 근무표 기준)."
    assert "'Z' 간호사가 없습니다" in say(agent, world.conv, "Z 2일 뭐야").answer
    assert world.conv.state.requests == []                           # 조회는 요청을 바꾸지 않는다


# ── 검토 반영(11-03) ────────────────────────────────────────────────────────


def _night_ward(**config):
    """A 2일 N · 3일 D — 3일 휴가면 '휴가 전날 N 금지'(설정)로 2일 N 도 빠져야 한다."""
    ctx = make_ctx({"A": "ONDD", "B": "DODO", "C": "OOOO"}, cov_min={"D": 1},
                   config={"not_one_night": False, **config})
    ctx.leave_codes = dict(LEAVE)
    return ctx


def test_leave_also_clears_the_night_before_when_the_ward_bans_it(world):
    world.ctxs["s1"] = lambda: _night_ward(ban_night_before_fixed_off=True)
    agent, _ = _agent(call("propose_edit", changes=[{"kind": "leave", "nurse": "A", "day": 3, "leave": "연차"}]))
    reply = say(agent, world.conv, "A 3일 연차")
    assert reply.preview["status"] == "ok", reply.answer
    session = world.conv.state
    cand = session.last.candidates[0]
    assert any(c.nurse_id == "A" and c.day == 1 and c.before_main == "N" for c in cand.changes)   # 2일 N 이 빠진다
    assert verify_candidate(session.ctx, session.rules, cand, session.requests, today=BEFORE_MONTH) == []


def test_leave_is_refused_when_the_night_before_cannot_change(world):
    def fixed():
        ctx = _night_ward(ban_night_before_fixed_off=True)
        ctx.frozen[("A", 1)] = "확정 원티드"
        return ctx
    world.ctxs["s1"] = fixed
    agent, _ = _agent(call("propose_edit", changes=[{"kind": "leave", "nurse": "A", "day": 3, "leave": "연차"}]))
    reply = say(agent, world.conv, "A 3일 연차")
    assert reply.preview["status"] == "invalid" and "2일 나이트를 바꿀 수 없어" in reply.answer


def test_period_leave_skips_fixed_and_off_days():
    from schedule_edit_agent.grounding import ground_changes
    ctx = _ward()
    ctx.frozen[("A", 2)] = "확정 원티드"                                # A 3일(D) 은 고정
    out = ground_changes(ctx, [{"kind": "leave", "nurse": "A", "day": 1, "to_day": 4, "leave": "연차"}], 0)
    assert [(r.nurse_id, r.day) for r in out.requests] == [("A", 0)] and not out.asks


def test_leave_check_requires_the_leave_code_in_the_candidate(world):
    world.ctxs["s1"] = _ward
    agent, _ = _agent(call("propose_edit", changes=[{"kind": "leave", "nurse": "A", "day": 1, "leave": "연차"}]))
    say(agent, world.conv, "A 1일 연차")
    session = world.conv.state
    cand = session.last.candidates[0]
    for c in cand.changes:
        if (c.nurse_id, c.day) == ("A", 0):
            c.after = "O"                                               # 휴가 코드가 빠진 후보
    problems = verify_candidate(session.ctx, session.rules, cand, session.requests, today=BEFORE_MONTH)
    assert any("요청 미충족" in p for p in problems)


def test_partner_counts_leave_out_vacation():
    from services.schedule_edit.requests import count_cells
    ctx = make_ctx({"A": "OOOD", "B": "OOOD"})
    ctx.vacation |= {("B", 0), ("B", 1)}                                # B 의 1·2일은 연차
    assert count_cells(ctx, ctx.main, "B", frozenset({"O"}), range(4)) == 1
    assert count_cells(ctx, ctx.main, "B", frozenset({"D"}), range(4)) == 1


def test_adding_a_request_on_the_same_cell_replaces_the_old_one(world):
    world.ctxs["s1"] = _ward
    agent, _ = _agent(call("propose_edit", changes=[{"kind": "set", "nurse": "A", "day": 1, "shift": "E"}]),
                      call("revise_edit", add=[{"kind": "leave", "nurse": "A", "day": 1, "leave": "연차"}]))
    say(agent, world.conv, "A 1일 E로")
    say(agent, world.conv, "아니 그날 연차로")
    reqs = world.conv.state.requests
    assert [(type(r).__name__, r.day) for r in reqs] == [("LeaveCell", 0)]

