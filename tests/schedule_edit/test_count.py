"""개수 요청(CountTarget) — '장원영 N 2개만 빼줘'. 어느 날을 바꿀지는 솔버가, 누가 메울지는 transfer 가 정한다.

손으로 만든 4일 근무표: A 는 N 4개, B·C 는 D 4개, 날마다 N 정확히 1명. A 의 N 을 2개 줄이면 그 이틀 N 을
B 나 C 가 메워야 한다(메우지 않으려면 그날 N 인원을 줄여야 한다). 기대값은 손으로 센 값이다.
"""

from __future__ import annotations

from services.schedule_edit.requests import CountTarget, is_satisfied, request_problem
from services.schedule_edit.rules import build_rules
from services.schedule_edit.serialize import requests_from_json, requests_to_json
from services.schedule_edit.model import grid_relief
from services.schedule_edit.solver import apply_candidate, rule_regressions
from tests.schedule_edit.test_solver import make_ctx, propose

LOOSE = {"not_one_night": False, "three_seq_nig": False, "banned_day_after_eve": False,
         "two_offs_after_two_nig": False, "two_offs_after_three_nig": False}


def _ward():
    ctx = make_ctx({"A": "NNNN", "B": "DDDD", "C": "DDDD"}, cov_min={"N": 1}, cov_max={"N": 1}, config=LOOSE)
    ctx.grade_of = {"A": 2, "B": 2, "C": 1}
    ctx.team_of = {"A": 1, "B": 2, "C": 1}
    return ctx


def _n(grid, nid):
    return sum(1 for m in grid[nid] if m == "N")


def _two_less(transfer):
    return CountTarget("A", frozenset({"N"}), 2, 0, 3, transfer)


def test_count_is_met_and_the_gap_is_covered():
    ctx = _ward()
    res = propose(ctx, [_two_less("auto")])
    assert res.status == "ok"
    for cand in res.candidates:
        grid = apply_candidate(ctx, cand)
        assert _n(grid, "A") == 2 and _n(grid, "B") + _n(grid, "C") == 2     # 날마다 N 1명 그대로
        assert is_satisfied(ctx, _two_less("auto"), grid)


def test_transfer_decides_who_covers():
    ctx = _ward()
    grade = apply_candidate(ctx, propose(ctx, [_two_less("same_grade")]).candidates[0])
    assert _n(grade, "B") == 2 and _n(grade, "C") == 0                      # 같은 등급(2) = B
    team = apply_candidate(ctx, propose(ctx, [_two_less("same_team")]).candidates[0])
    assert _n(team, "C") == 2 and _n(team, "B") == 0                         # 같은 팀(1) = C
    fair = apply_candidate(ctx, propose(ctx, [_two_less("fair")]).candidates[0])
    assert max(_n(fair, "B"), _n(fair, "C")) <= 2                            # 줄인 뒤 A(2회)보다 많이 받지 않는다


def test_reduce_coverage_relaxes_only_the_days_the_requester_left():
    ctx = _ward()
    rules = build_rules(ctx)
    res = propose(ctx, [_two_less("reduce_coverage")])
    grid = apply_candidate(ctx, res.candidates[0])
    assert res.candidates[0].changed_count == 2 and _n(grid, "B") + _n(grid, "C") == 0   # 아무도 안 메운다
    left = {d for d in range(4) if grid["A"][d] != "N"}
    relief = grid_relief(ctx, rules, [_two_less("reduce_coverage")], grid)
    assert relief == {("인원최소", None, d, "N"): 1 for d in left}          # 실제로 빠진 날만(검토 M2·N2)
    assert grid_relief(ctx, rules, [_two_less("auto")], grid) == {}       # 그 선택지를 고르지 않으면 없음


def test_more_of_a_shift_takes_it_from_someone_or_adds_coverage():
    # B 의 N 을 하나 늘린다(0→1). 날마다 N 정확히 1명이라 누군가(A)의 N 을 넘겨받거나, 그날 인원을 늘려야 한다.
    ctx = _ward()
    more = CountTarget("B", frozenset({"N"}), 1, 0, 3, "auto")
    grid = apply_candidate(ctx, propose(ctx, [more]).candidates[0])
    assert _n(grid, "B") == 1 and _n(grid, "A") == 3                       # A 에게서 넘겨받음
    add = CountTarget("B", frozenset({"N"}), 1, 0, 3, "add_coverage")
    grid = apply_candidate(ctx, propose(ctx, [add]).candidates[0])
    assert _n(grid, "B") == 1 and _n(grid, "A") == 4                       # 넘겨받지 않고 그날 N 2명
    assert list(grid_relief(ctx, build_rules(ctx), [add], grid).values()) == [1]


def test_more_off_means_someone_covers_the_work():
    # C 의 OFF 를 하나 늘린다 — C 가 놓는 D 는 날마다 D 최소 1명이라 누군가 메워야 한다
    ctx = make_ctx({"A": "DDDD", "B": "OOOO", "C": "DDDD"}, cov_min={"D": 2}, cov_max={"D": 2}, config=LOOSE)
    ctx.grade_of = {"A": 1, "B": 2, "C": 2}
    req = CountTarget("C", frozenset({"O"}), 1, 0, 3, "same_grade")
    grid = apply_candidate(ctx, propose(ctx, [req]).candidates[0])
    assert grid["C"].count("O") == 1 and grid["B"].count("D") == 1 and grid["A"] == list("DDDD")


def test_others_cannot_leave_on_other_days_under_reduce_coverage():
    # A 가 N 을 빼는 날과 다른 날에 B 의 N 이 빠지면, 그날은 완화가 없어 인원최소 위반 — 모델·검증 모두 막는다
    ctx = make_ctx({"A": "NNDD", "B": "DDNN", "C": "DDDD"}, cov_min={"N": 1}, cov_max={"N": 1}, config=LOOSE)
    ctx.grade_of = {"A": 2, "B": 2, "C": 1}
    req = CountTarget("A", frozenset({"N"}), 1, 0, 3, "reduce_coverage")
    rules = build_rules(ctx)
    bad = {"A": list("NDDD"), "B": list("DDDN"), "C": list("DDDD")}   # A 2일 N 빠짐(허용) + B 3일 N 빠짐(불허)
    assert [k for k, _v in rule_regressions(ctx, rules, bad, [req], {})] == [("인원최소", None, 2, "N")]


def test_target_below_fixed_cells_is_refused():
    ctx = _ward()
    ctx.frozen[("A", 0)] = ctx.frozen[("A", 1)] = "확정 원티드"
    problem = request_problem(ctx, CountTarget("A", frozenset({"N"}), 1, 0, 3, "auto"))
    assert problem and "2회는 바꿀 수 없는 칸" in problem


def test_count_request_round_trips_through_json():
    req = [_two_less(None), CountTarget("B", frozenset({"O"}), 1, 2, 3, "fair")]
    assert requests_from_json(requests_to_json(req)) == req


def test_coverage_choice_must_match_the_direction():
    ctx = _ward()
    assert "그날 인원 늘리기" in request_problem(ctx, _two_less("add_coverage"))          # 줄이는 요청에 늘리기
    more = CountTarget("B", frozenset({"N"}), 1, 0, 3, "reduce_coverage")
    assert "그날 인원 줄이기" in request_problem(ctx, more)                              # 늘리는 요청에 줄이기
