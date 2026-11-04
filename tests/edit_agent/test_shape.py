"""근무 모양 고치기(11-04 · 사용자 결정 '근무 모양 고치기') — 하루짜리 OFF·퐁당퐁당·N-O-D 순서는 규칙을 지키며 줄일 수
있는 만큼, 연속 근무·주말 근무는 정한 수 이하로. 시험 근무표는 2026-11(1일=일요일)."""

from __future__ import annotations

from datetime import date

from schedule_edit_agent.grounding import SHAPE_ASK, ground_changes
from schedule_edit_agent.skills import UNSUPPORTED_TOPICS
from services.schedule_edit.requests import ShapeTarget, is_satisfied, shape_count, shape_values, weekend_days
from services.schedule_edit.serialize import requests_from_json, requests_to_json
from services.schedule_edit.solver import min_shape_count
from tests.edit_agent.test_agent import BEFORE_MONTH, _agent, call, make_ctx, say

LOOSE = {"not_one_night": False, "two_offs_after_two_nig": False, "isolated_work_hard": False}


def _ward():
    """하루짜리 OFF 4개(A 2일 · B 3·9일 · C 5일) · 퐁당퐁당 1개(B 8일) · N-O-D 없음 · 연속 근무 A 3일·B·C 2일 ·
    주말(1·7·8일) 근무 A 2·B 2·C 1."""
    return make_ctx({"A": "DODDDOODDO", "B": "DDODDOODOD", "C": "OODDODDOOD"}, cov_min={"D": 1}, config=LOOSE)


def _stuck():
    """A 는 OFF 가 꼭 1개이고 연속 근무는 3일까지 — OFF 를 어디에 두어도 하루짜리 OFF 다(맨 앞·뒤면 4일 연속).
    C 의 하루짜리 OFF(2일)는 3일을 OFF 로 바꾸면 없어진다."""
    ctx = make_ctx({"A": "DDODD", "C": "DODDD"}, config={**LOOSE, "max_conseq_work": 3})
    ctx.off_bounds = {"A": (1, 1)}
    return ctx


def _shape(kind, nurses=(), at_most=None, said=False):
    return ShapeTarget(kind, tuple(nurses), 0, 9, at_most, said=said)


def test_counts_each_shape_on_the_month():
    ctx = _ward()
    assert [shape_count(ctx, ctx.main, _shape(k)) for k in ("isolated_off", "isolated_work", "pattern")] == [4, 1, 0]
    assert shape_values(ctx, ctx.main, _shape("work_run")) == {"A": 3, "B": 2, "C": 2}
    assert weekend_days(ctx, _shape("weekend")) == [0, 6, 7]
    assert shape_values(ctx, ctx.main, _shape("weekend")) == {"A": 2, "B": 2, "C": 1}
    assert not is_satisfied(ctx, _shape("isolated_off")) and is_satisfied(ctx, _shape("pattern"))
    assert is_satisfied(ctx, _shape("work_run", at_most=3)) and not is_satisfied(ctx, _shape("work_run", at_most=2))
    reqs = [_shape("isolated_off", at_most=2, said=True), ShapeTarget("weekend", ("C",), 2, 5, 1, ranged=True)]
    assert requests_from_json(requests_to_json(reqs)) == reqs


def test_grounding_fills_the_limit_only_for_run_and_weekend():
    ctx = _ward()

    def ground(**change):
        return ground_changes(ctx, [{"kind": "shape", "nurse": "", **change}])

    assert ground(shape="").asks == [SHAPE_ASK]
    assert ground(shape="isolated_off").requests == [_shape("isolated_off")]     # 줄일 수는 스킬이 찾는다
    assert ground(shape="work_run", nurse="A").requests == [_shape("work_run", ("A",), 2)]   # 가장 긴 것 − 1
    assert ground(shape="work_run", nurse="A", target=4).requests == [_shape("work_run", ("A",), 4, said=True)]
    assert ground(shape="isolated_off", target=2).requests == [_shape("isolated_off", (), 2, said=True)]
    # 주말은 병동 평균(5/3 올림 = 2)까지 — 짚은 사람이 이미 평균 이하면 지금 − 1
    assert ground(shape="weekend", nurse="A").requests == [_shape("weekend", ("A",), 1)]
    assert ground(shape="weekend", nurse="C").requests == [_shape("weekend", ("C",), 0)]
    assert "숫자로" in ground(shape="work_run", target="많이").asks[0]


def test_smallest_count_that_keeps_the_rules_is_found():
    ctx = _stuck()
    best, proven, now, grid = min_shape_count(ctx, [ShapeTarget("isolated_off", (), 0, 4)], today=BEFORE_MONTH)
    assert (best, proven, now) == (1, True, 2) and grid["C"][2] == "O"


def test_agent_removes_every_isolated_off_it_can(world):
    world.ctxs["s1"] = _ward
    agent, _ = _agent(call("propose_edit", changes=[{"kind": "shape", "shape": "isolated_off", "nurse": ""}]))
    reply = say(agent, world.conv, "하루짜리 OFF 없애줘", BEFORE_MONTH)
    assert reply.preview["status"] == "ok", reply.answer
    assert reply.answer.startswith("하루짜리 OFF 줄이기(전원 · 지금 4개) — 바꿀 방법")
    assert "하루짜리 OFF 4개 → 0개" in reply.answer and "남은" not in reply.answer
    assert world.conv.state.requests == [_shape("isolated_off", at_most=0)]    # 찾은 수를 확정 조건으로


def test_agent_says_why_the_rest_stays(world):
    world.ctxs["s1"] = _stuck
    agent, _ = _agent(call("propose_edit", changes=[{"kind": "shape", "shape": "isolated_off", "nurse": ""}]))
    reply = say(agent, world.conv, "하루짜리 OFF 없애줘", BEFORE_MONTH)
    assert reply.preview["status"] == "ok", reply.answer
    assert "하루짜리 OFF 2개 → 1개" in reply.answer
    assert reply.answer.endswith("남은 하루짜리 OFF 1개는 규칙을 모두 지키며 바꾸는 칸 40개 안에서는 더 줄일 방법이 없습니다.")
    assert world.conv.state.requests[0].at_most == 1


def test_none_can_go_offers_a_setting_to_pass(world):
    def only_a():
        ctx = make_ctx({"A": "DDODD"}, config={**LOOSE, "max_conseq_work": 3})
        ctx.off_bounds = {"A": (1, 1)}
        return ctx
    world.ctxs["s1"] = only_a
    agent, _ = _agent(call("propose_edit", changes=[{"kind": "shape", "shape": "isolated_off", "nurse": "A"}]))
    reply = say(agent, world.conv, "A 하루짜리 OFF 없애줘", BEFORE_MONTH)
    assert reply.preview["status"] == "infeasible", reply.answer
    assert reply.answer.startswith("하루짜리 OFF 줄이기(A · 지금 1개):")
    assert [lv["lose"] for lv in reply.preview["levers"]] == [["A OFF 수 상한보다 1개 많게"]]   # 하드락은 카드로 안 나온다


def test_nothing_to_fix_and_already_within_the_limit(world):
    agent, _ = _agent(call("propose_edit", changes=[{"kind": "shape", "shape": "pattern", "nurse": ""}]),
                      call("propose_edit", changes=[{"kind": "shape", "shape": "work_run", "nurse": "A", "target": 5}]))
    world.ctxs["s1"] = _ward
    assert say(agent, world.conv, "N-O-D 없애줘").answer.startswith(
        "바꿀 수 있는 칸에는 고칠 곳이 없습니다 — N-O-D·N-O-E·E-O-D 순서 0개(전원).")
    assert say(agent, world.conv, "A 연속 근무 5일까지만").answer.startswith(
        "A 연속 근무 5일 이하로(지금 최대 3일) — 이미 그렇게 되어 있습니다.")


def test_run_and_weekend_answers_show_before_and_after(world):
    world.ctxs["s1"] = _ward
    agent, _ = _agent(call("propose_edit", changes=[{"kind": "shape", "shape": "work_run", "nurse": "A"}]),
                      call("propose_edit", changes=[{"kind": "shape", "shape": "weekend", "nurse": "C"}]))
    run = say(agent, world.conv, "A 연속 근무 너무 길어")
    assert run.answer.startswith("A 연속 근무 2일 이하로(지금 최대 3일) — 바꿀 방법") and "가장 긴 연속 근무 3일 → 2일" in run.answer
    weekend = say(agent, world.conv, "C 주말 근무 빼줘")
    assert weekend.answer.startswith("C 주말 근무 0회로(지금 최대 1회) — 바꿀 방법") and "주말 근무: C 1→0회" in weekend.answer


def test_saying_it_again_for_one_person_replaces_the_ward_request(world):
    world.ctxs["s1"] = _ward
    agent, _ = _agent(call("propose_edit", changes=[{"kind": "shape", "shape": "isolated_off", "nurse": ""}]),
                      call("revise_edit", add=[{"kind": "shape", "shape": "isolated_off", "nurse": "A"}]))
    say(agent, world.conv, "하루짜리 OFF 없애줘")
    reply = say(agent, world.conv, "A 것만")
    assert reply.answer.startswith("하루짜리 OFF 줄이기(A · 지금 1개)")
    assert world.conv.state.requests == [_shape("isolated_off", ("A",), 0)]


def test_shapes_are_no_longer_answered_as_unsupported():
    assert set(UNSUPPORTED_TOPICS) == {"postprocess"}          # 버전 되돌리기도 11-04 부터 지원


def test_a_said_count_is_kept_not_searched_again(world):
    # '하루짜리 OFF 2개까지만' — 말한 수를 그대로 쓴다(전엔 가장 작은 수로 덮어써 칸이 늘었다 · 검토 11-04 M1)
    world.ctxs["s1"] = _ward
    agent, _ = _agent(call("propose_edit", changes=[{"kind": "shape", "shape": "isolated_off", "nurse": "", "target": 2}]),
                      call("propose_edit", changes=[{"kind": "shape", "shape": "isolated_off", "nurse": "", "target": 5}]))
    reply = say(agent, world.conv, "하루짜리 OFF 2개까지만")
    assert reply.answer.startswith("하루짜리 OFF 2개 이하로(전원 · 지금 4개) — 바꿀 방법"), reply.answer
    assert world.conv.state.requests == [_shape("isolated_off", at_most=2, said=True)] and "남은" not in reply.answer
    assert say(agent, world.conv, "5개까지만").answer.startswith(
        "하루짜리 OFF 5개 이하로(전원 · 지금 4개) — 이미 그렇게 되어 있습니다.")


def test_two_counted_shapes_are_each_brought_down(world):
    # 한 식을 덮어써 퐁당퐁당만 줄이고 '하루짜리 OFF 0개 증명'이라 했다(검토 11-04 M2)
    ctx = _ward()
    off, alone = _shape("isolated_off"), _shape("isolated_work")
    best, proven, now, grid = min_shape_count(ctx, [off, alone], target=off, today=BEFORE_MONTH)
    assert (best, proven, now) == (0, True, 4) and shape_count(ctx, grid, off) == 0
    assert shape_count(ctx, grid, alone) <= 1                          # 아직 정하지 않은 쪽은 늘지만 않게
    world.ctxs["s1"] = _ward
    agent, _ = _agent(call("propose_edit", changes=[{"kind": "shape", "shape": "isolated_off", "nurse": ""},
                                                    {"kind": "shape", "shape": "isolated_work", "nurse": ""}]))
    reply = say(agent, world.conv, "하루짜리 OFF랑 퐁당퐁당 없애줘")
    assert reply.preview["status"] == "ok", reply.answer
    assert "하루짜리 OFF 4개 → 0개" in reply.answer and "하루 근무(퐁당퐁당) 1개 → 0개" in reply.answer
    assert [r.at_most for r in world.conv.state.requests] == [0, 0]


def test_cells_that_cannot_change_do_not_make_the_limit_impossible():
    ctx = _ward()
    ctx.frozen[("C", 6)] = "관할 밖(파견·이동·휴직·프리셉티)"        # C 7일(토) D — 바꿀 수 없는 주말 근무
    ctx.frozen.update({("A", d): "관할 밖(파견·이동·휴직·프리셉티)" for d in (2, 3, 4)})   # A 3~5일 DDD 통째로 묶임
    no_weekend = ShapeTarget("weekend", (), 0, 9, 0)
    grid = {n: list(row) for n, row in ctx.main.items()}
    for n, d in (("A", 0), ("A", 7), ("B", 0), ("B", 7)):
        grid[n][d] = "O"
    assert is_satisfied(ctx, no_weekend, grid)                         # C 는 바꿀 수 없는 1회까지
    assert ground_changes(ctx, [{"kind": "shape", "shape": "weekend", "nurse": "C"}]).asks == [
        "그 기간의 주말 근무는 모두 바꿀 수 없는 칸(원티드 고정·파견 등)입니다."]
    assert shape_values(ctx, ctx.main, _shape("work_run", ("A",)))["A"] == 2   # 묶인 3연속은 고칠 수 없어 세지 않는다
    # 인원 기준이 없으면 '주말 0회'가 풀린다 — C 의 묶인 주말 근무 1회 때문에 통째로 불가가 되지 않는다
    free = make_ctx(dict(ctx.main), config=LOOSE)
    free.frozen.update(ctx.frozen)
    _best, _proven, _now, found = min_shape_count(free, [no_weekend, _shape("isolated_off")], today=BEFORE_MONTH)
    assert found is not None and is_satisfied(free, no_weekend, found) and found["C"][6] == "D"



def test_asking_about_shapes_counts_them_like_the_fix_does(world):
    """6차 평가(11-04) — 'N-O-D 같은 거 있어?'(후보 없음)에 셈 없이 '안을 찾은 뒤 알려 드립니다'로만 답하던 것."""
    world.ctxs["s1"] = _ward
    agent, _ = _agent(call("check_rules", topic="shape"), call("check_rules", topic="shape"),
                      call("propose_edit", changes=[{"kind": "shape", "shape": "isolated_off", "nurse": ""}]))
    answer = say(agent, world.conv, "N-O-D 같은 거 있어?", BEFORE_MONTH).answer
    assert answer == ("근무 모양(바꿀 수 있는 칸 기준): 하루짜리 OFF 4개(A 2일, B 3일, C 5일 외 1개) · "
                      "하루 근무(퐁당퐁당) 1개(B 8일) · N-O-D·N-O-E·E-O-D 순서 0개 · 가장 긴 연속 근무 3일(A) · "
                      "가장 많은 주말 근무 2회(A 외 1명).\n고치려면 '하루짜리 OFF 없애줘'·'퐁당퐁당 없애줘'처럼 말씀해 주세요.")
    # 달 중간이면 바꿀 수 있는 날부터 — 이어서 고쳐 달라고 할 때의 '지금 n개'와 같은 수
    today = date(2026, 11, 4)
    later = say(agent, world.conv, "하루짜리 OFF 있어?", today).answer
    assert later.startswith("근무 모양(바꿀 수 있는 칸 기준 · 4일부터): 하루짜리 OFF 2개(C 5일, B 9일) · ")
    fix = say(agent, world.conv, "하루짜리 OFF 없애줘", today).answer
    assert fix.startswith("하루짜리 OFF 줄이기(전원 · 지금 2개)")


def test_a_past_month_does_not_name_a_day_that_is_not_there(world):
    """지난 달이면 '31일부터'처럼 없는 날을 말하지 않는다(검토 11-04 M1)."""
    world.ctxs["s1"] = _ward
    agent, _ = _agent(call("check_rules", topic="shape"))
    past = say(agent, world.conv, "근무 모양 어때?", date(2026, 12, 15)).answer
    assert past == "이 달은 바꿀 수 있는 날이 남아 있지 않아 고칠 근무 모양을 세지 않습니다."


def test_the_fix_example_names_the_order_that_is_there(world):
    """N-O-D·N-O-E·E-O-D 는 한 종류로 센다 — 예시는 실제로 나온 순서(E-O-D) 이름으로."""
    world.ctxs["s1"] = lambda: make_ctx({"A": "DEODDOODDO", "B": "DDDODDDODD"}, config=LOOSE)
    agent, _ = _agent(call("check_rules", topic="shape"))
    answer = say(agent, world.conv, "근무 순서 이상한 거 있어?", BEFORE_MONTH).answer
    assert "N-O-D·N-O-E·E-O-D 순서 1개(A 2~4일)" in answer and "'E-O-D 없애줘'" in answer, answer
