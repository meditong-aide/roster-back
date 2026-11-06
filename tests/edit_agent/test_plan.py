"""여러 날 인원 계획·등급 하한(32장) — 판단이 갈리면 묻고(사용자 지시 11-05), 동의한 만큼만 남기고, 두 단계로 푼다.

시험 근무표(2026-11 · 7일 · 1일 일 ~ 7일 토 → 주말은 1·7일): 다섯 명이 OFF 2개씩. 근무 칸이 25개라
'매일 D2·E2'(28칸)는 산술로 안 되고 '평일 D2·E2 · 토일 D1·E1'(24칸)은 된다.
"""

from __future__ import annotations

import dataclasses
import json

from schedule_edit_agent.answer import answer_for, describe_requests
from schedule_edit_agent.grounding import ground_changes
from schedule_edit_agent import skills as skills_mod
from schedule_edit_agent.skills import GRADE, RESTATE_ASK, SHORT, _patched, consent_key
from services.schedule_edit.levers import lever_allow
from services.schedule_edit.model import add_holds, build_model, free_cells, staffing_relief
from services.schedule_edit.requests import (
    CountTarget, GiveUpWish, GradeFloor, SetCell, StaffingPlan, StaffRule, WishTarget, fixed_in_plan, grade_gaps,
    is_satisfied,
    plan_ambiguous, plan_capacity, plan_label, ro, staffing_bounds, staffing_gaps, staffing_over, wish_targets,
)
from services.schedule_edit.rules import build_rules, measure, new_violations
from services.schedule_edit.serialize import requests_from_json, requests_to_json
from services.schedule_edit.solver import (
    Candidate, EditResult, apply_candidate, cell_change, hold_allowance, probe_plan, propose_edits, quality_notes,
    verify_candidate,
)
from tests.edit_agent.test_agent import _agent, _logs, _row, call, new_store, press, say
from tests.schedule_edit.test_solver import BEFORE_MONTH, make_ctx

LOOSE = {"not_one_night": False, "two_offs_after_two_nig": False, "two_offs_after_three_nig": False,
         "isolated_work_hard": False, "nod_noe": False, "sequential_offs": False}
ROWS = {"A": "DDDOOEE", "B": "EEODDDO", "C": "ODEEODD", "D": "DOODEEE", "E": "EEEOODD"}


def _ward(**kw):
    ctx = make_ctx(dict(ROWS), cov_min={"D": 1, "E": 1}, cov_max={"D": 3, "E": 3}, off_days=2, config=LOOSE, **kw)
    ctx.grade_of = {"A": 1, "B": 2, "C": 1, "D": 2, "E": 2}
    return ctx


def rule(scope, code, low, high=None, exact=None, days=()):
    return StaffRule(scope, code, low, high, tuple(days), exact)


EVERY_DAY = (rule("all", "D", 2), rule("all", "E", 2))
SPLIT = (rule("weekday", "D", 2), rule("weekday", "E", 2), rule("weekend", "D", 1), rule("weekend", "E", 1))


def _plan(*alternatives, **kw):
    return StaffingPlan(tuple(alternatives), 0, 6, **kw)


def _staffing_change(*plans, **kw):
    return {"kind": "staffing", "nurse": "", "plans": [list(p) for p in plans], **kw}


def _line(days, shift, **kw):
    return {"days": days, "shift": shift, **kw}


DAILY = [_line("all", "D", count=2), _line("all", "E", count=2)]
WEEK = [_line("weekday", "D", count=2), _line("weekday", "E", count=2),
        _line("weekend", "D", count=1), _line("weekend", "E", count=1)]


# ── 요청 판정 ────────────────────────────────────────────────────────────────


def test_bounds_follow_scopes_and_later_lines_cover_earlier_ones():
    ctx = _ward()
    plan = _plan(EVERY_DAY + (rule("weekday", "D", 3, 4),))
    bounds = staffing_bounds(ctx, plan)
    assert bounds[0]["D"] == (2, 2) and bounds[6]["D"] == (2, 2)          # 1일(일)·7일(토)은 '매일' 그대로
    assert bounds[1]["D"] == (3, 4) and bounds[1]["E"] == (2, 2)          # 평일은 덧붙인 줄이 덮는다
    assert plan_label(plan.rules()) == "매일 D2·E2 · 평일 D3~4"
    days = staffing_bounds(ctx, _plan((rule("days", "E", 1, days=(2, 3)),)))
    assert sorted(days) == [2, 3]
    assert rule("all", "D", 3, exact=False).bounds() == (3, None)          # '3명 이상' — 최대는 병동 설정


def test_capacity_is_the_arithmetic_check_between_alternatives():
    ctx = _ward()
    assert plan_capacity(ctx, _plan(EVERY_DAY)) == (28, 25)              # 다섯 명 × (7일 − OFF 2)
    assert plan_capacity(ctx, _plan(SPLIT)) == (24, 25)
    # N 만 하는 사람은 월 나이트 상한까지만 근무 가능 칸으로 센다(7B 윤지원 · 산술이 후하게 나왔다)
    night = make_ctx({"A": "NNOONNO", "B": "DDDOODD"}, off_days=2, config=LOOSE)
    night.monthly_limits = {"A": {"n_exact": 4}}
    plan = StaffingPlan(((rule("all", "N", 1),),), 0, 6)
    assert plan_capacity(night, plan)[1] == 5 + 5
    night.n_only = {"A"}
    assert plan_capacity(night, plan)[1] == 4 + 5


def test_vague_counts_are_only_asked_when_the_schedule_has_more_than_that():
    ctx = _ward()
    vague = plan_ambiguous(ctx, _plan(SPLIT))
    # 6일(금) D 3명 · 1·7일(주말) D·E 2명씩 — 그보다 적게 말한 줄만
    assert [(r.code, r.scope, days) for _i, r, days in vague] == [("D", "weekday", [5]), ("D", "weekend", [0, 6]),
                                                                   ("E", "weekend", [0, 6])]
    weekdays = SPLIT[:2]
    assert len(plan_ambiguous(ctx, _plan(weekdays))) == 1
    assert plan_ambiguous(ctx, _plan(weekdays + (rule("weekday", "D", 2, 3),))) == []          # 범위로 덮였다
    assert plan_ambiguous(ctx, _plan((rule("weekday", "D", 2, exact=True),) + SPLIT[1:2])) == []


def test_fixed_workers_in_the_plan_are_found_and_can_be_left_out_of_the_count():
    ctx = _ward()
    for d in range(ctx.days):
        ctx.frozen[("A", d)] = "고정근무자"
    plan = _plan(SPLIT)
    assert fixed_in_plan(ctx, plan) == [("A", "D"), ("A", "E")]
    base = staffing_gaps(ctx, ctx.main, dataclasses.replace(plan, count_fixed=False))
    assert (1, "D", 2, 1) in base and (1, "D", 2, 2) not in staffing_gaps(ctx, ctx.main, plan)   # A 를 빼면 2일 D 1명


def test_relief_bends_ward_settings_only_as_much_as_asked_and_keeps_grade_rules():
    ctx = _ward()
    ctx.cov_min = [{"D": 2, "E": 1} for _ in range(ctx.days)]
    ctx.grade_min = {"D": {"1": 1}}
    plan = _plan((rule("all", "D", 1), rule("all", "E", 3)))
    relief = staffing_relief(ctx, plan)
    assert relief[("인원최소", None, 0, "D")] == 1 and ("인원최대", None, 0, "E") not in relief   # E 최대 3 안
    # 손풀이 1회차 사고: 최소 인원을 비워 등급 규칙이 사라졌다 — 계획은 넘김만 주고 병동 최소·등급 규칙은 그대로
    assert any(i.key[0] == "등급최소" for i in build_rules(ctx).ineqs)


# ── 엔진 ────────────────────────────────────────────────────────────────────


def test_plan_is_solved_in_two_stages_without_a_change_cap():
    ctx = _ward()
    plan = _plan(SPLIT)
    probe = probe_plan(ctx, [plan], today=BEFORE_MONTH)
    assert probe.status == "ok" and probe.short == [] and probe.hold == {}
    res = propose_edits(ctx, [dataclasses.replace(plan, chosen=0, short=0)], today=BEFORE_MONTH, hint=probe.grid)
    assert res.status == "ok" and len(res.candidates) == 1
    grid = apply_candidate(ctx, res.candidates[0])
    assert staffing_gaps(ctx, grid, plan) == [] and staffing_over(ctx, grid, plan) == []
    assert verify_candidate(ctx, build_rules(ctx), res.candidates[0], [plan], today=BEFORE_MONTH) == []


def test_grade_rules_stay_when_the_plan_lowers_staffing():
    # 하루 D 2명(1등급 1명 포함)인 병동에서 계획이 D 1명으로 낮춘다 — 남는 D 가 1등급이어야 한다(등급 규칙이 살아 있다)
    ctx = make_ctx({"A": "DDDDDOO", "B": "DDDDDOO", "C": "OODDDDD", "D": "OOEEEEE", "E": "EEOOEEE"},
                   cov_min={"D": 2, "E": 1}, cov_max={"D": 3, "E": 3}, config=LOOSE)
    ctx.grade_of = {"A": 1, "B": 2, "C": 1, "D": 2, "E": 2}
    ctx.grade_min = {"D": {"1": 1}}
    plan = _plan((rule("all", "D", 1, exact=True),))
    res = propose_edits(ctx, [plan], today=BEFORE_MONTH)
    assert res.status == "ok"
    grid = apply_candidate(ctx, res.candidates[0])
    for d in range(ctx.days):
        on = [n for n in ctx.nurse_ids if grid[n][d] == "D"]
        assert len(on) == 1 and ctx.grade_of[on[0]] == 1, (d, on)
    # 병동 최소 인원(2명)은 요청한 만큼 넘긴다 — 그 밖의 새 위반은 없다(등급 최소는 그대로 지킨다)
    rules = build_rules(ctx)
    worse = new_violations(measure(ctx, ctx.main, rules), measure(ctx, grid, rules))
    assert {k[0] for k in worse} == {"인원최소"}
    assert verify_candidate(ctx, rules, res.candidates[0], [plan], today=BEFORE_MONTH) == []


def test_grade_floor_fills_every_shift_or_reports_the_gap():
    ctx = _ward()
    floor = GradeFloor(1, 1, 0, 6)
    assert grade_gaps(ctx, ctx.main, floor)[:2] == [(0, "E", 1), (0, "N", 1)]
    with_m = GradeFloor(1, 1, 0, 6, codes=("D", "E"), m_days=(3,))
    assert [(d, c) for d, c, _p in __import__("services.schedule_edit.requests", fromlist=["x"]).grade_slots(
        ctx, with_m) if d == 3] == [(3, "D"), (3, "E"), (3, "M")]
    floor = GradeFloor(1, 1, 0, 6, codes=("D", "E"))
    probe = probe_plan(ctx, [_plan(SPLIT), floor], today=BEFORE_MONTH)
    assert probe.status == "ok" and probe.short == []
    res = propose_edits(ctx, [dataclasses.replace(_plan(SPLIT), chosen=0, short=0),
                              dataclasses.replace(floor, short=sum(x for *_k, x in probe.grade))],
                        today=BEFORE_MONTH, hint=probe.grid)
    grid = apply_candidate(ctx, res.candidates[0])
    assert sum(x for *_k, x in grade_gaps(ctx, grid, floor)) == sum(x for *_k, x in probe.grade)


def test_probe_reports_bends_of_held_shape_rules():
    # A 2~5일을 OFF 로 지정하면 연속 OFF(3일 상한)가 지금보다 나빠진다 — 묶은 품질 규칙이라 넘길지 물어야 한다
    ctx = make_ctx(dict(ROWS), cov_min={"D": 0, "E": 0}, config=LOOSE)
    asked = [SetCell("A", d, "O") for d in (1, 2, 3, 4)]
    probe = probe_plan(ctx, [_plan(SPLIT)] + asked, today=BEFORE_MONTH)
    assert probe.status == "ok" and any(k[0] == "연속OFF" for k in probe.hold)
    key = next(k for k in probe.hold if k[0] == "연속OFF")
    rules = build_rules(ctx)
    em = build_model(ctx, rules, asked, free_cells(ctx, range(ctx.days)), 49)
    add_holds(em, rules, {key: probe.hold[key]})          # 동의한 만큼은 봐준다
    assert key in {i.key for i in rules.soft}


def test_wanted_codes_pick_only_off_wishes_and_unreflected_fixed_wanted_cells():
    ctx = _ward()
    ctx.wishes = {("A", 3): {"D"}, ("B", 2): {"D"}, ("C", 1): {"O"}}
    ctx.frozen[("C", 1)] = "확정 원티드"
    ctx.wish_fixed = {("C", 1)}                              # 신청 OFF 인데 근무표가 D(7B 허건 11일)
    assert [(n, d) for n, d, _c in wish_targets(ctx, WishTarget((), 0, 6, codes=frozenset({"O"})))] == [("C", 1)]
    assert len(wish_targets(ctx, WishTarget((), 0, 6))) == 3
    res = propose_edits(ctx, [WishTarget((), 0, 6, at_least=1, codes=frozenset({"O"}))], today=BEFORE_MONTH)
    assert res.status == "ok"
    for cand in res.candidates:
        moved = {(c.nurse_id, c.day): c.after_main for c in cand.changes}
        assert moved[("C", 1)] == "O"                        # 원티드대로만 — 다른 근무로는 바꾸지 않는다


def test_new_requests_round_trip_through_the_record():
    plan = StaffingPlan((EVERY_DAY, SPLIT + (rule("weekday", "D", 2, 3, exact=False),)), 5, 29, chosen=1, short=2,
                        count_fixed=True)
    floor = GradeFloor(1, 1, 5, 29, m_days=(6, 7), short=1, count_fixed=True)
    wish = WishTarget((), 5, 29, at_least=3, codes=frozenset({"O"}))
    assert requests_from_json(requests_to_json([plan, floor, wish])) == [plan, floor, wish]


def _consented(allow, kind):
    return sum(v for k, v in allow.items() if k[0] == kind)


def test_consent_keys_pass_the_allowance_gate():
    short, grade = consent_key(SHORT, "s"), consent_key(GRADE, "s")
    hold = ("품질넘김", "s", "연속OFF", "A", 2, 3)
    assert lever_allow({short: 3, grade: 1, hold: 1, ("전환금지", "A", 1, "ED"): 1, ("연속OFF", "A", 2, 3): 1}) == {
        short: 3, grade: 1, hold: 1}                         # 계획 구분값 없는 품질 규칙 위치는 받지 않는다


# ── 해석 ────────────────────────────────────────────────────────────────────


def test_grounding_reads_plans_ranges_dates_grade_floor_and_off_wishes():
    ctx = _ward()
    out = ground_changes(ctx, [_staffing_change(DAILY, WEEK + [_line("all", "E", min=1, max=2, dates=[3, 4])]),
                               {"kind": "grade_floor", "nurse": "", "grade": 1},
                               {"kind": "wanted", "nurse": "", "shifts": ["OFF"]}])
    assert out.asks == []
    plan, floor, wish = out.requests
    assert plan.alternatives[0] == (rule("all", "D", 2), rule("all", "E", 2))
    assert plan.alternatives[1][-1] == StaffRule("days", "E", 1, 2, (2, 3), False)
    assert (floor.grade, floor.count, floor.codes) == (1, 1, ("D", "E", "N"))
    assert wish.codes == frozenset({"O"})
    assert "몇 명으로" in ground_changes(ctx, [_staffing_change([_line("all", "D")])]).asks[0]


def test_month_total_counts_and_already_true_counts_in_a_bundle():
    ctx = _ward()
    total = {"kind": "count", "nurse": "A", "shift": "OFF", "target": 2, "total": True}
    # 3일까지 닫힌 날에 A 의 OFF 는 없다 — 한 달 2개 = 남은 날 2개(이미 그렇다)
    assert ground_changes(ctx, [total], 3).asks == ["A OFF은(는) 이미 2회입니다."]
    bundle = ground_changes(ctx, [total, _staffing_change(WEEK)], 3)
    assert bundle.asks == [] and bundle.requests[0] == CountTarget("A", frozenset({"O"}), 2, 3, 6, "auto", total=True)
    # 요청 줄은 사용자가 말한 기준(한 달) 그대로 — '2→2회(4~7일)'가 아니라(사용자 결정 11-05)
    assert describe_requests(ctx, bundle.requests[:1]) == "A OFF 한 달 2회"
    moved = dataclasses.replace(bundle.requests[0], target=1)
    assert describe_requests(ctx, [moved]) == "A OFF 한 달 2→1회"
    # 닫힌 날(1~4일)에 이미 OFF 2개 — 한 달 1개로는 못 줄인다
    assert "이미 2회라 한 달 1회로는" in ground_changes(ctx, [{**total, "target": 1}], 5).asks[0]


def test_adding_a_line_patches_every_alternative():
    old = _plan(EVERY_DAY, SPLIT, count_fixed=True)
    new = _plan((rule("weekday", "D", 2, 3, exact=False),))
    merged = _patched(old, new)
    assert [alt[-1] for alt in merged.alternatives] == [rule("weekday", "D", 2, 3, exact=False)] * 2
    assert merged.count_fixed is True and merged.chosen is None


# ── 대화(가짜 LLM) ──────────────────────────────────────────────────────────


def _fixed_ward():
    ctx = _ward()
    for d in range(ctx.days):
        ctx.frozen[("A", d)] = "고정근무자"
    return ctx


def test_conversation_asks_fixed_workers_then_counts_and_explains_the_skipped_plan(world):
    world.ctxs["s1"] = _fixed_ward
    agent, _ = _agent(call("propose_edit", changes=[_staffing_change(DAILY, WEEK)]))
    first = say(agent, world.conv, "그외 2.2 안되면 주말 1.1")
    assert first.preview["status"] == "choose_plan" and first.preview["kind"] == "fixed"
    assert "고정 근무자도 하루 인원에 셀까요? (A(고정 D·E))" in first.answer
    assert world.conv.state.last is None                          # 묻는 동안 낸 안이 없다
    second = press(agent, world.conv, "choose_staffing", {"choice_id": "count_fixed"})
    assert second.preview["status"] == "choose_plan" and second.preview["kind"] == "exact"
    assert "앞 계획은 안 돼 다음 계획으로 갑니다: 매일 D2·E2은 필요 28칸 > 근무 가능 25칸" in second.answer
    assert "평일 D 2명 — 지금 그보다 많은 날 1일(6일)" in second.answer
    done = press(agent, world.conv, "choose_staffing", {"choice_id": "exact"})
    assert done.preview["status"] == "ok"
    assert "쓴 계획: 평일 D2·E2 · 토일 D1·E1 · 모자라는 칸 없음" in done.answer
    assert "사람별 바뀐 칸:" in done.answer and "날마다 인원(D·E):" in done.answer
    plan = next(r for r in world.conv.state.requests if isinstance(r, StaffingPlan))
    assert (plan.count_fixed, plan.chosen, plan.short) == (True, 1, 0)
    assert [e.payload.get("choice_id") for e in world.events if e.event == "choose"] == ["count_fixed", "exact"]


def test_shortage_on_the_last_plan_is_asked_before_any_such_candidate(world):
    world.ctxs["s1"] = _ward
    agent, _ = _agent(call("propose_edit", changes=[_staffing_change(DAILY)]))
    asked = say(agent, world.conv, "매일 2.2")
    assert asked.preview["status"] == "confirm" and asked.preview["restate"] is True
    assert "매일 D2·E2은 필요 28칸 > 근무 가능 25칸" in asked.answer
    assert world.conv.state.last is None and world.conv.state.asking
    then = press(agent, world.conv, "confirm_request", {})
    assert then.preview["status"] == "choose_plan" and then.preview["kind"] == "exact"   # 6일 D 3명 — 산술은 최소만 본다
    done = press(agent, world.conv, "choose_staffing", {"choice_id": "exact"})
    assert done.preview["status"] == "ok" and "모자라는 칸 3:" in done.answer
    assert _consented(world.conv.state.allow, SHORT) == 3          # 산술로 모자라는 수만(28 - 25 · 재검토 LOW-1)


def test_an_arithmetic_consent_is_asked_again_when_the_probe_finds_more(world, monkeypatch):
    # 산술 동의는 모자라는 3칸만 — 풀어 보니 5칸이면 다시 묻는다(결정은 사용자에게 · 재검토 LOW-1)
    world.ctxs["s1"] = _ward
    monkeypatch.setattr(skills_mod, "probe_plan", _fake_probe({2: 5}))
    monkeypatch.setattr(skills_mod, "_give_up_for_short", lambda *a, **k: None)
    agent, _ = _agent(call("propose_edit", changes=[_staffing_change(DAILY)]))
    asked = say(agent, world.conv, "매일 2.2")
    assert asked.preview["status"] == "confirm" and "필요 28칸 > 근무 가능 25칸" in asked.answer
    then = press(agent, world.conv, "confirm_request", {})
    assert _consented(world.conv.state.allow, SHORT) == 3
    if then.preview["status"] == "choose_plan":
        then = press(agent, world.conv, "choose_staffing", {"choice_id": "exact"})
    assert then.preview["status"] == "confirm" and "5칸이 모자랍니다" in then.answer
    assert world.conv.state.last is None
    press(agent, world.conv, "confirm_request", {})
    assert _consented(world.conv.state.allow, SHORT) == 5


def test_restating_staffing_leaves_the_schedule_alone(world):
    world.ctxs["s1"] = _ward
    agent, _ = _agent(call("propose_edit", changes=[_staffing_change(DAILY)]))
    say(agent, world.conv, "매일 2.2")
    out = press(agent, world.conv, "cancel_request", {"restate": True})
    assert out.answer == RESTATE_ASK and world.conv.state.requests == [] and not world.conv.state.asking


def test_grade_shortage_is_asked_and_then_left_as_agreed(world):
    world.ctxs["s1"] = lambda: _with_grades(_ward(), {"A": 2, "C": 2, "B": 1})   # 1등급은 B 하나
    agent, _ = _agent(call("propose_edit", changes=[_staffing_change(WEEK),
                                                    {"kind": "grade_floor", "nurse": "", "grade": 1,
                                                     "shifts": ["D", "E"]}]))
    asked = say(agent, world.conv, "평일 2.2 주말 1.1 · 근무조마다 1등급")
    if asked.preview["status"] == "choose_plan":              # 6일 D 3명 — 정확히로
        asked = press(agent, world.conv, "choose_staffing", {"choice_id": "exact"})
    assert asked.preview["status"] == "confirm" and "등급 하한이 모자라는 근무조" in asked.answer
    done = press(agent, world.conv, "confirm_request", {})
    assert done.preview["status"] == "ok" and "모자라는 근무조" in done.answer
    floor = next(r for r in world.conv.state.requests if isinstance(r, GradeFloor))
    assert floor.short == _consented(world.conv.state.allow, GRADE) > 0


def _with_grades(ctx, grades):
    ctx.grade_of.update(grades)
    return ctx


def test_choice_questions_survive_a_restart(world, monkeypatch):
    plan = _plan(EVERY_DAY, SPLIT)
    rows = [_row("message", 1, {"text": "그외 2.2", "tool": "propose_edit"}),
            _row("propose", 1, {"requests": requests_to_json([plan]), "allow": [], "avoid": [],
                                "result": {"status": "choose_plan", "kind": "fixed"}, "answer": "셀까요?"})]
    _logs(monkeypatch, rows)
    world.ctxs["s1"] = _fixed_ward
    store = new_store()
    conv = store.checkout(None, "c-plan", "acc_hn")
    store.checkin(conv)
    agent, _ = _agent()
    out = press(agent, conv, "choose_staffing", {"choice_id": "exclude_fixed"})
    assert out.preview["status"] in ("choose_plan", "confirm", "ok")
    assert next(r for r in conv.state.requests if isinstance(r, StaffingPlan)).count_fixed is False


def test_staffing_buttons_refuse_when_nothing_is_pending(world):
    world.ctxs["s1"] = _ward
    agent, _ = _agent()
    out = press(agent, world.conv, "choose_staffing", {"choice_id": "exact"})
    assert out.failed and "고를 인원 계획이 없습니다" in out.answer


def test_satisfied_counts_in_a_plan_bundle_do_not_ask_how_to_fill(world):
    world.ctxs["s1"] = _ward
    agent, _ = _agent(call("propose_edit", changes=[
        {"kind": "count", "nurse": "A", "shift": "OFF", "target": 2, "total": True}, _staffing_change(WEEK)]))
    out = say(agent, world.conv, "A 총 오프 2개, 평일 2.2 주말 1.1")
    assert out.preview["status"] != "choose_transfer"
    count = next(r for r in world.conv.state.requests if isinstance(r, CountTarget))
    assert count.transfer == "auto" and is_satisfied(world.conv.state.ctx, count)


def test_bending_a_held_shape_rule_is_asked_like_a_lever(world):
    # A 2~5일 OFF 를 함께 요청하면 연속 OFF(3일 상한)가 지금보다 나빠진다 — 풀지 않고 무엇을 넘기는지 보이고 묻는다
    world.ctxs["s1"] = lambda: make_ctx(dict(ROWS), cov_min={"D": 1, "E": 1}, cov_max={"D": 3, "E": 3}, config=LOOSE)
    agent, _ = _agent(call("propose_edit", changes=[{"kind": "off_period", "nurse": "A", "day": 2, "to_day": 5},
                                                    _staffing_change(WEEK)]))
    out = say(agent, world.conv, "A 2~5일 오프, 평일 2.2 주말 1.1")
    if out.preview["status"] == "choose_plan":
        out = press(agent, world.conv, "choose_staffing", {"choice_id": "at_least"})
    assert out.preview["status"] == "confirm" and "A 2일부터 연속 OFF 3일 넘음(+1)" in out.answer
    assert world.conv.state.last is None
    done = press(agent, world.conv, "confirm_request", {})
    assert done.preview["status"] == "ok"
    assert any(k[0] == "품질넘김" and k[2] == "연속OFF" for k in world.conv.state.allow)


def test_a_held_shape_consent_survives_a_restart_and_solves_again(world, monkeypatch):
    # 품질 넘김 동의는 기록(JSON)에 [열쇠 목록, 양]으로 남는다 — 되살린 위치가 모델 열쇠(ineq.key)와 같은 튜플이라
    # 재기동 뒤 같은 요청으로 다시 찾아도 묻지 않고 그 계획에 동의한 만큼만 넘긴다(재검토 LOW-2)
    world.ctxs["s1"] = lambda: make_ctx(dict(ROWS), cov_min={"D": 1, "E": 1}, cov_max={"D": 3, "E": 3}, config=LOOSE)
    agent, _ = _agent(call("propose_edit", changes=[{"kind": "off_period", "nurse": "A", "day": 2, "to_day": 5},
                                                    _staffing_change(WEEK)]))
    out = say(agent, world.conv, "A 2~5일 오프, 평일 2.2 주말 1.1")
    if out.preview["status"] == "choose_plan":
        press(agent, world.conv, "choose_staffing", {"choice_id": "at_least"})
    assert press(agent, world.conv, "confirm_request", {}).preview["status"] == "ok"
    given = dict(world.conv.state.allow)
    rows = [_row(e.event, e.turn_no or 1, json.loads(json.dumps(e.payload))) for e in world.events]
    _logs(monkeypatch, rows)
    store = new_store()
    conv = store.checkout(None, "c-hold", "acc_hn")
    store.checkin(conv)
    again = press(_agent()[0], conv, "retry_request", {})
    assert again.preview["status"] == "ok" and not conv.state.asking
    assert conv.state.allow == given
    held = hold_allowance(conv.state.allow, conv.state.requests)
    keys = {ineq.key for ineq in conv.state.rules.soft}
    assert held and all(isinstance(k, tuple) for k in held) and set(held) <= keys
    assert any(k[0] == "연속OFF" for k in held)


def test_an_unproven_probe_does_not_claim_the_shortage_is_needed(world, monkeypatch):
    # 시간 안에 끝까지 못 풀었으면 '모자랍니다'로 단정하지 않는다(작업자 2 실측: 0 인 등급 부족을 1로 보고 물었다)
    from schedule_edit_agent import skills as skills_mod
    from services.schedule_edit.solver import PlanProbe
    world.ctxs["s1"] = _ward
    monkeypatch.setattr(skills_mod, "probe_plan", lambda *a, **k: PlanProbe("ok", short=[(2, "D", 1)], proven=False))
    agent, _ = _agent(call("propose_edit", changes=[_staffing_change([_line("weekday", "D", min=1, max=3)])]))
    out = say(agent, world.conv, "평일 D 1~3명")
    assert out.preview["status"] == "confirm"
    assert "시간 안에 찾은 방법으로는 1칸이 모자랍니다 — 3일 D. 더 줄일 수 있는지 끝까지 확인하지 못했습니다." in out.answer


# ── 막혔을 때 — 원티드 포기 · 넘길 설정(사용자 결정 11-05) ─────────────────────────


def _wish_ward():
    ctx = _ward()
    ctx.wishes = {("A", 2): {"O"}, ("B", 1): {"E"}}         # A 3일 OFF 신청(반영 안 됨) · B 2일 E 신청(반영됨)
    return ctx


def test_a_wanted_off_that_cannot_fit_is_offered_to_give_up(world):
    world.ctxs["s1"] = _wish_ward
    agent, _ = _agent(call("propose_edit", changes=[{"kind": "set", "nurse": "A", "day": 3, "shift": "D"},
                                                    _staffing_change([_line("weekday", "D", min=2, max=3)]),
                                                    {"kind": "wanted", "nurse": "", "shifts": ["OFF"]}]))
    blocked = say(agent, world.conv, "A 3일 D, 평일 D 2~3명, 신청 오프 유지")
    assert blocked.preview["status"] == "confirm" and "풀 방법이 없습니다" in blocked.answer
    assert "원티드 1칸을 포기하면 찾을 수 있습니다: A 3일 OFF" in blocked.answer
    assert world.conv.state.last is not None and world.conv.state.last.status == "infeasible"   # 레버를 고를 수 있게
    done = press(agent, world.conv, "confirm_request", {})
    assert done.preview["status"] == "ok" and "포기한 원티드 1칸: A 3일" in done.answer
    wish = next(r for r in world.conv.state.requests if isinstance(r, WishTarget))
    assert wish.skip == (("A", 2),) and wish.at_least == 0


def test_a_kept_wish_blocking_the_plan_is_released_only_when_picked(world):
    # B 2일 E 는 반영된 원티드 — B 의 E 를 한 달 0개로 하려면 그 원티드를 포기해야 한다(짚을 때만 푼다 · [RD-29])
    world.ctxs["s1"] = _wish_ward
    agent, _ = _agent(call("propose_edit", changes=[
        {"kind": "count", "nurse": "B", "shift": "E", "target": 0, "total": True, "transfer": "auto"},
        _staffing_change([_line("weekday", "D", min=1, max=3)])]))
    blocked = say(agent, world.conv, "B 이브닝 없게, 평일 D 1~3명")
    assert blocked.preview["status"] == "confirm" and "B 2일 E" in blocked.answer
    done = press(agent, world.conv, "confirm_request", {})
    assert done.preview["status"] == "ok"
    assert any(isinstance(r, GiveUpWish) and r.cells == (("B", 1),) for r in world.conv.state.requests)
    moved = {(c["nurse_id"], c["day"]): c["after"] for c in done.preview["candidates"][0]["changes"]}
    assert moved[("B", 2)] != "E"                             # 화면 day 는 1부터


def test_plan_counts_ask_how_to_fill_without_probing_and_without_coverage_options(world):
    world.ctxs["s1"] = _ward
    agent, _ = _agent(call("propose_edit", changes=[{"kind": "count", "nurse": "A", "shift": "D", "change": -1},
                                                    _staffing_change([_line("weekday", "D", min=1, max=3)])]))
    asked = say(agent, world.conv, "A D 하나 빼고 평일 D 1~3명")
    assert asked.preview["status"] == "choose_transfer" and asked.preview["probed"] is False
    ids = [c["choice_id"] for c in asked.preview["choices"]]
    assert "reduce_coverage" not in ids and "add_coverage" not in ids and "auto" in ids
    refused = press(agent, world.conv, "choose_transfer", {"choice_id": "reduce_coverage"})
    assert refused.failed
    done = press(agent, world.conv, "choose_transfer", {"choice_id": "auto"})
    assert done.preview["status"] in ("ok", "choose_plan", "confirm")
    assert next(r for r in world.conv.state.requests if isinstance(r, CountTarget)).transfer == "auto"


# ── 검토 H1·H2·M1 · 모자랄 때 원티드 포기(사용자 결정 11-05) ───────────────────────


def test_an_unset_shortage_allowance_means_none_in_the_engine():
    # 남겨도 되는 모자람을 정하지 않은 계획은 0 — 동의 없이 부족 안이 나오지 않는다(검토 H1)
    res = propose_edits(_ward(), [_plan(EVERY_DAY)], today=BEFORE_MONTH)
    assert res.status == "infeasible" and not res.candidates


def test_a_timed_out_probe_ends_without_a_shortage_plan(world, monkeypatch):
    from services.schedule_edit.solver import PlanProbe
    world.ctxs["s1"] = _ward
    monkeypatch.setattr(skills_mod, "probe_plan", lambda *a, **k: PlanProbe("timeout"))
    agent, _ = _agent(call("propose_edit", changes=[_staffing_change([_line("weekday", "D", min=1, max=3)])]))
    out = say(agent, world.conv, "평일 D 1~3명")
    assert out.preview["status"] == "timeout" and out.preview["candidates"] == []
    assert "시간 안에 확인하지 못했습니다" in out.answer and world.conv.state.requests


def _fake_probe(shorts):
    """계획마다 정해 둔 모자람을 돌려주는 가짜 미리 풀기 — 원티드를 포기하면(GiveUpWish·skip) 0."""
    from services.schedule_edit.solver import PlanProbe

    def probe(ctx, trial, **kw):
        if kw.get("release_wishes"):
            return PlanProbe("ok", wish_lost=[("C", 0, frozenset({"O"}))])
        gave = any(isinstance(r, GiveUpWish) or (isinstance(r, WishTarget) and r.skip) for r in trial)
        plan = next(r for r in trial if isinstance(r, StaffingPlan))
        n = 0 if gave else shorts[plan.rules()[0].low]
        return PlanProbe("ok", short=[(1, "D", n)] if n else [], proven=True)
    return probe


def test_consents_belong_to_the_plan_they_were_given_for(world, monkeypatch):
    # 5칸 동의 → 새 계획에서 4칸 모자람 → 다시 묻는다(검토 H2 — 동의가 대화 전체로 쌓여 다음 계획에 쓰였다)
    world.ctxs["s1"] = _ward
    monkeypatch.setattr(skills_mod, "probe_plan", _fake_probe({1: 5, 2: 4}))
    monkeypatch.setattr(skills_mod, "_give_up_for_short", lambda *a, **k: None)
    agent, _ = _agent(call("propose_edit", changes=[_staffing_change([_line("weekday", "D", min=1, max=3)])]),
                      call("propose_edit", changes=[_staffing_change([_line("weekday", "D", min=2, max=3)])]))
    first = say(agent, world.conv, "평일 D 1~3명")
    assert first.preview["status"] == "confirm" and "5칸이 모자랍니다" in first.answer
    press(agent, world.conv, "confirm_request", {})
    assert _consented(world.conv.state.allow, SHORT) == 5
    second = say(agent, world.conv, "평일 D 2~3명")
    assert second.preview["status"] == "confirm" and "4칸이 모자랍니다" in second.answer


def test_shortage_question_offers_giving_up_a_wish_for_zero_shortage(world, monkeypatch):
    world.ctxs["s1"] = _wish_ward
    monkeypatch.setattr(skills_mod, "probe_plan", _fake_probe({2: 1}))
    agent, _ = _agent(call("propose_edit", changes=[_staffing_change([_line("weekday", "D", min=2, max=3)])]))
    asked = say(agent, world.conv, "평일 D 2~3명")
    assert asked.preview["status"] == "confirm"
    assert [c["label"] for c in asked.preview["choices"]] == ["모자라는 칸 남기고 찾기", "원티드 포기하고 찾기"]
    assert "원티드 1칸을 포기하면 모자람 0: C 1일 OFF" in asked.answer
    give_up = asked.preview["choices"][1]["id"]
    press(agent, world.conv, "confirm_request", {"pick": give_up})
    allow = world.conv.state.allow
    assert _consented(allow, "원티드포기") == 1 and _consented(allow, SHORT) == 0    # 고른 쪽만 받아들인다
    assert any(isinstance(r, GiveUpWish) and r.cells == (("C", 0),) for r in world.conv.state.requests)


def test_apply_recheck_speaks_in_requests_not_dataclasses():
    ctx = _ward()
    res = propose_edits(ctx, [SetCell("A", 3, "D")], today=BEFORE_MONTH)
    cand = res.candidates[0]
    cand.changes = [c for c in cand.changes if (c.nurse_id, c.day) != ("A", 3)]
    problems = verify_candidate(ctx, build_rules(ctx), cand, [SetCell("A", 3, "D")], today=BEFORE_MONTH)
    assert "요청 미충족: A 4일 D" in problems                 # 검토 M1: 'SetCell(nurse_id=…)' 가 그대로 나갔다


def _night_answer(rows, changes, requests):
    ctx = make_ctx(rows, config=LOOSE)
    cand = Candidate(1, [cell_change(ctx, n, d, c) for n, d, c in changes], sorted({n for n, _d, _c in changes}),
                     True, (0, ctx.days - 1), span=(0, 1))
    cand.quality = quality_notes(ctx, build_rules(ctx), apply_candidate(ctx, cand))
    return answer_for(ctx, requests, EditResult("ok", candidates=[cand]), notes=[]), cand.quality


def test_nights_given_up_by_a_count_request_are_said_as_redistribution():
    # 개수 요청으로 빠진 나이트를 다른 간호사가 나눠 받아 차이가 커지면 그렇게 밝힌다 — '근무 모양'이라 부르지 않는다.
    # 나머지 분배 지표는 기존 문구에서 '분배'로(사용자 결정 11-05). A 의 N 2개를 B·C 가 하나씩 받는다 — N 2·2·2 → 0·3·3
    answer, quality = _night_answer({"A": "NNOODDO", "B": "DONNODD", "C": "ODOONND"},
                                    [("A", 0, "D"), ("A", 1, "D"), ("B", 0, "N"), ("C", 1, "N")],
                                    [CountTarget("A", frozenset({"N"}), 0, 0, 6, total=True)])
    assert "사람마다 나이트 수 차이 0→3" in quality and "D·E 쏠림 점수 1→2" in quality
    assert "A N 0회로 빠진 나이트 2개를 다른 간호사가 나눠 받아 사람마다 나이트 수 차이가 0→3으로 커졌습니다." in answer
    assert "1안은 규칙을 모두 지키지만 분배가 나빠지는 곳이 있습니다(예: D·E 쏠림 점수 1→2)." in answer
    assert "근무 모양" not in answer


def test_a_night_spread_not_from_a_count_request_keeps_the_old_line_as_distribution():
    # 개수 요청이 나이트를 빼지 않았으면(이미 0 · 요청과 무관) 나눠 받았다고 하지 않는다 — 기존 문구를 '분배'로
    answer, quality = _night_answer({"A": "DDOODDO", "B": "DONNODD", "C": "ODOONND"},
                                    [("B", 0, "N"), ("B", 1, "N"), ("B", 5, "O")],
                                    [CountTarget("A", frozenset({"N"}), 0, 0, 6, total=True), SetCell("B", 0, "N")])
    assert quality and quality[0].startswith("사람마다 나이트 수 차이")
    assert "나눠 받아" not in answer
    assert f"1안은 규칙을 모두 지키지만 분배가 나빠지는 곳이 있습니다(예: {quality[0]})." in answer


#: 나이트가 하루 1명씩 — 7일만 0명이다(하루 N 1명 계획이면 그날 하나가 모자란다)
NIGHTS = {"A": "NNOODDO", "B": "DONNODD", "C": "ODOONND"}
ONE_NIGHT = _plan((rule("all", "N", 1),), chosen=0)


def test_nights_added_by_the_plan_are_said_as_the_plan_s_doing():
    # 인원 계획이 나이트 전체 수를 늘려 차이가 커지면 계획 때문이라고 밝히고, '분배가 나빠지는 곳'에서는 뺀다(사용자 결정 11-06)
    answer, quality = _night_answer(NIGHTS, [("C", 6, "N")], [ONE_NIGHT])
    assert "사람마다 나이트 수 차이 0→1" in quality
    assert "하루 N 인원을 늘려 늘어난 나이트 1개를 나눠 받아 사람마다 나이트 수 차이가 0→1로 커졌습니다." in answer
    assert answer.count("사람마다 나이트 수 차이") == 1 and "다른 간호사가" not in answer


def test_a_count_request_and_the_plan_together_are_both_said():
    # 개수 요청으로 빠진 몫과 계획으로 늘어난 몫이 함께면 사실대로 함께 — A 의 N 2개 + 7일 N 1개를 B·C 가 받는다
    answer, _q = _night_answer(NIGHTS, [("A", 0, "D"), ("A", 1, "D"), ("B", 0, "N"), ("C", 1, "N"), ("C", 6, "N")],
                               [CountTarget("A", frozenset({"N"}), 0, 0, 6, total=True), ONE_NIGHT])
    assert ("A N 0회로 빠진 나이트 2개와 하루 N 인원을 늘려 늘어난 나이트 1개를 다른 간호사가 나눠 받아 "
            "사람마다 나이트 수 차이가 0→4로 커졌습니다.") in answer
    assert answer.count("사람마다 나이트 수 차이") == 1


def test_a_plan_that_did_not_raise_the_total_is_not_blamed():
    # 계획 최소를 이미 채운 근무표(7일도 N 1명)에서 사람끼리 나이트를 옮겨 차이가 커지면 계획 탓이 아니다 — 기존 문구로
    answer, quality = _night_answer({**NIGHTS, "C": "ODOONNN"}, [("A", 1, "D"), ("C", 1, "N")], [ONE_NIGHT])
    assert quality and quality[0].startswith("사람마다 나이트 수 차이")
    assert "인원을 늘려" not in answer and f"분배가 나빠지는 곳이 있습니다(예: {quality[0]})" in answer


def test_a_raising_count_request_is_not_counted_as_the_plan_s_share():
    # 계획 몫은 계획 부족이 있던 날(7일)의 증가분만 — 'B N 4회'로 는 2개는 개수 요청 쪽으로 따로 밝힌다(재검토 LOW-1:
    # 전에는 전체 증가 3개를 모두 '하루 N 인원을 늘려 늘어난 나이트'로 셌다)
    answer, _q = _night_answer(NIGHTS, [("B", 0, "N"), ("B", 5, "N"), ("C", 6, "N")],
                               [CountTarget("B", frozenset({"N"}), 4, 0, 6, total=True), ONE_NIGHT])
    assert ("하루 N 인원을 늘려 늘어난 나이트 1개를 나눠 받고 B N 4회로 나이트 2개가 늘어 "
            "사람마다 나이트 수 차이가 0→2로 커졌습니다.") in answer
    assert answer.count("사람마다 나이트 수 차이") == 1


def test_the_plan_s_share_never_exceeds_the_real_increase():
    # 2일(N 2명)에서 하나를 빼고 4·7일(N 0명)에 하나씩 넣으면 모자랐던 날 증가는 2지만 그 달 순증가는 1 —
    # '늘어난 나이트 2개'라고 하지 않는다(검토 11-06 LOW)
    answer, quality = _night_answer({"A": "NNOODDO", "B": "DNNOODD", "C": "OODONND"},
                                    [("B", 1, "D"), ("C", 3, "N"), ("A", 6, "N")], [ONE_NIGHT])
    assert "사람마다 나이트 수 차이 0→2" in quality
    assert "하루 N 인원을 늘려 늘어난 나이트 1개를 나눠 받아 사람마다 나이트 수 차이가 0→2로 커졌습니다." in answer


def test_a_plan_that_lowers_staffing_says_the_offs_it_added():
    # OFF 계획 문장 — 계획 최대(D 1명)를 넘던 날 D 를 줄여 늘어난 OFF 를 C 가 받는다(사용자 결정 11-06). 넘지 않던 날은 세지 않는다
    plan = _plan((rule("all", "D", 0, 1),), chosen=0)
    answer, quality = _night_answer({"A": "DDOODDO", "B": "DONNODD", "C": "ODOONND"}, [("C", 1, "O"), ("C", 6, "O")],
                                    [plan])
    assert "사람마다 OFF 수 차이 1→3" in quality
    assert "하루 인원을 줄여 늘어난 OFF 2개를 나눠 받아 사람마다 OFF 수 차이가 1→3으로 커졌습니다." in answer
    assert answer.count("사람마다 OFF 수 차이") == 1


def test_offs_given_up_by_a_count_request_use_the_same_sentence():
    # OFF 도 같은 틀 — A OFF 3→1 로 빠진 OFF 2개를 B·C 가 받아 OFF 수 차이가 커진다(휴가 칸은 세지 않는다)
    answer, quality = _night_answer({"A": "DDOODDO", "B": "DONNODD", "C": "ODOONND"},
                                    [("A", 2, "D"), ("A", 3, "D"), ("B", 5, "O"), ("C", 6, "O")],
                                    [CountTarget("A", frozenset({"O"}), 1, 0, 6, total=True)])
    assert "사람마다 OFF 수 차이 1→3" in quality
    assert "A OFF 1회로 빠진 OFF 2개를 다른 간호사가 나눠 받아 사람마다 OFF 수 차이가 1→3으로 커졌습니다." in answer
    assert answer.count("사람마다 OFF 수 차이") == 1


def test_ro_particle_follows_the_last_sound():
    assert [ro(w) for w in ("0회", "6→8", "2→3", "2→5", "A N 3회(3~30일)")] == [
        "0회로", "6→8로", "2→3으로", "2→5로", "A N 3회(3~30일)로"]
