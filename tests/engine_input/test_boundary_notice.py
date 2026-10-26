"""생성 결과 안내 — 작성 중인 상대 병동 근무표를 기준으로 경계·상한을 맞춘 경우(EI-11 11w-2 · D34 · D38 · 11w-n D39).

D38: 상대 병동 근무표를 읽어 경계·상한을 건 자리 전부 — 출발 경계(G3) · 파견 합류·복귀 꼬리(11r-b) · 월초 2~6일 합류·복귀의 상대 전월
꼬리(G7) · 1일 복귀 꼬리(G4) · 1일 합류 꼬리(월경계 보충) · 두 병동 나이트 합산(#6 · D33) — 에서 그 근무표가 작성 중(마감 전)이면
병동·달마다 한 번 `boundary_other_ward_reference` 항목을 싣는다. 마감본이면 싣지 않는다.
D39: 그 항목은 성공 응답의 `boundary_notices` 칸에 안내가 있을 때만 싣는다(빈 리스트로 싣지 않는다). `weekly_off_warnings` 에는 하나도
없다 — 워커 Slack 품질 요약(`utils/roster_quality.summarize`)이 그 칸 개수를 '주간 OFF 경고'로 세기 때문이다.

방법: 성공 모드 기록기(`stop_at='post'` — 엔진 대신 결정론 근무표 · 후처리까지)로 생성 서비스를 끝까지 부르고, 서비스를 감싸 응답을
받는다. 파견 알림 준비(`_enqueue_assignment_created_push`)는 별도 세션을 열어 하네스가 막으므로 이 시험에서만 끈다(안내와 무관).
"""

from __future__ import annotations

import dataclasses
from collections.abc import Callable

import pytest

from .boundary_cases import (
    CFG,
    BoundaryCase,
    depart_n07,
    return_day1,
    scenario_for,
    tail_cells,
    transfer_out_n08,
    x01_in,
)
from .harness import run_with_output
from .ladder import drain_live_graph_dirs
from .scenario_ladder import post_env

NOTICE = "boundary_other_ward_reference"


def _capture_response(holder: dict):
    def patch(mp):
        import services.roster_create_service as rcs

        original = rcs.generate_roster_service

        def wrapper(*args, **kwargs):
            holder["response"] = original(*args, **kwargs)
            return holder["response"]
        mp.setattr(rcs, "generate_roster_service", wrapper)
        mp.setattr(rcs, "_enqueue_assignment_created_push", lambda *_a, **_k: None)
    return patch


def _response(engine, case: BoundaryCase) -> dict:
    """성공 모드로 끝까지 — 생성 서비스의 응답."""
    holder: dict = {}
    base = scenario_for(case)
    scenario = dataclasses.replace(base, stop_at="post", patches=base.patches + (post_env, _capture_response(holder)))
    try:
        rec = run_with_output(engine, scenario).result
    finally:
        drain_live_graph_dirs()   # test_ladder.py 의 live graph 흔적 단언을 지킨다(성공 모드도 임시 폴더를 쓴다)
    assert rec.outcome == "returned", (rec.outcome, rec.error)
    return holder["response"]


def _notices(engine, case: BoundaryCase) -> list[dict]:
    """응답의 안내 칸(D39 — `boundary_notices` · 안내가 없으면 칸도 없다). 주휴 경고 칸에는 안내가 하나도 없다."""
    response = _response(engine, case)
    assert [w for w in response["weekly_off_warnings"] if w.get("type") == NOTICE] == [], response["weekly_off_warnings"]
    notices = response.get("boundary_notices")
    assert notices is None or (notices and all(n.get("type") == NOTICE for n in notices)), notices
    return notices or []


def _x01_all_month(status_nov: str = "issued", **over) -> BoundaryCase:
    """X01 이 11월 내내(1~30일) G1 에 파견 와 있다 — 월중 경계가 없다(합류는 월경계 · 원 병동 복귀는 다음 달)."""
    return BoundaryCase(name="in1-30", focus="X01", boundaries=(("after", 0),), assignments=[x01_in(1, 30)],
                        config=CFG["Y"], g2_nov_status=status_nov, **over)


#: 이름 → (상태를 받아 재료를 짓는 함수, 안내가 걸리는 상대 근무표 달, 그 안내에 반드시 있는 쓰임).
TARGETS: dict[str, tuple[Callable[[str], BoundaryCase], int, str]] = {
    # 출발 경계(G3) — 병동이동 out(상대 병동 같은 달)
    "departure": (lambda s: dataclasses.replace(transfer_out_n08(16, "DDE", "Y"), g2_nov_status=s), 11, "departure"),
    # 파견 합류 꼬리(11r-b)
    "join": (lambda s: BoundaryCase(name="in20", focus="X01", boundaries=(("after", 19),), assignments=[x01_in(20, 25)],
                                    g2_nov={"X01": tail_cells("OOOONN", 19)}, config=CFG["Y"], g2_nov_status=s), 11, "join"),
    # 파견 복귀 꼬리(11r-b)
    "return": (lambda s: dataclasses.replace(depart_n07("DDODD", "Y"), g2_nov_status=s), 11, "return"),
    # 월초 3일 합류의 상대 전월 꼬리(G7) — 같은 달 G2 근무표는 마감본이라 안내는 전월(10월) 몫만
    "g7": (lambda s: BoundaryCase(name="in3", focus="X01", boundaries=(("after", 2),), assignments=[x01_in(3, 25)],
                                  g2_oct={"X01": tail_cells("OOOOON", 31)}, g2_nov={"X01": tail_cells("NN", 2)},
                                  config=CFG["Y"], g2_nov_status="issued", g2_oct_status=s), 10, "join"),
    # 1일 복귀 꼬리(G4)
    "return_day1": (lambda s: dataclasses.replace(return_day1("OOOONN", "Y"), g2_oct_status=s), 10, "return_day1"),
    # 1일 합류 꼬리(월경계 보충 `_inbound_source_map`)
    "join_day1": (lambda s: _x01_all_month(g2_oct={"X01": tail_cells("OOOONN", 31)}, g2_oct_status=s), 10, "join_day1"),
    # 두 병동 나이트 합산(#6) — X01 은 11월 내내 이 병동이라 경계는 없고 같은 달 G2 근무표를 #6 만 읽는다
    "night_sum": (lambda s: _x01_all_month(s, g2_nov={"X01": {1: "O"}}), 11, "night_sum"),
}


@pytest.mark.parametrize("name", list(TARGETS))
def test_draft_other_ward_schedule_gets_one_notice(name, engine, engine_input_tables):
    """작성 중 근무표 → 그 병동·달 안내 1건(쓰임이 여럿이어도 1건) · 문구에 병동 이름과 달 · 쓰임에 그 자리."""
    build, month, kind = TARGETS[name]
    notices = _notices(engine, build("draft"))
    assert [(n["group_id"], n["year"], n["month"]) for n in notices] == [("G2", 2026, month)], notices
    notice = notices[0]
    assert kind in {u["kind"] for u in notice["uses"]}, notice
    assert notice["schedule_status"] == "draft" and f"합성병동G2 {month}월 근무표(작성 중" in notice["detail"], notice
    assert len(notice["uses"]) == len({(u["kind"], u["nurse_id"]) for u in notice["uses"]}), notice


@pytest.mark.parametrize("name", list(TARGETS))
def test_issued_other_ward_schedule_gets_no_notice(name, engine, engine_input_tables):
    """마감본이면 안내하지 않는다(D38)."""
    build, _month, _kind = TARGETS[name]
    assert _notices(engine, build("issued")) == []


def test_notice_leaves_weekly_off_count_and_slack_summary_alone(engine, engine_input_tables):
    """안내가 붙어도(작성 중) 주휴 경고 개수와 워커 Slack 품질 요약의 '주간 OFF' 지표·줄은 안내 없는 응답(마감본)과 같다(D39 —
    `roster_quality.summarize` 는 `weekly_off_warnings` 만 세고 `boundary_notices` 는 세지 않는다)."""
    from utils.roster_quality import summarize

    build, _month, _kind = TARGETS["departure"]
    draft, issued = _response(engine, build("draft")), _response(engine, build("issued"))
    assert len(draft.get("boundary_notices") or []) == 1 and "boundary_notices" not in issued
    assert len(draft["weekly_off_warnings"]) == len(issued["weekly_off_warnings"])
    with_notice, without = summarize(draft), summarize(issued)
    assert with_notice["metrics"].get("weekly_off") == without["metrics"].get("weekly_off")
    assert [s for s in with_notice["lines"] if "주간 OFF" in s] == [s for s in without["lines"] if "주간 OFF" in s]


def test_two_draft_months_get_one_notice_each(engine, engine_input_tables):
    """전월(G7 꼬리)과 같은 달(합류·출발·#6) 근무표가 둘 다 작성 중이면 달마다 1건씩 — 같은 병동·달 중복은 없다."""
    build, _month, _kind = TARGETS["g7"]
    notices = _notices(engine, dataclasses.replace(build("draft"), g2_nov_status="draft"))
    assert sorted((n["group_id"], n["month"]) for n in notices) == [("G2", 10), ("G2", 11)], notices
    by_month = {n["month"]: {u["kind"] for u in n["uses"]} for n in notices}
    assert {"join", "join_day1"} <= by_month[10] and {"join", "departure", "night_sum"} <= by_month[11], by_month


def test_mid_month_joiner_prev_month_tail_gets_no_notice(engine, engine_input_tables):
    """월중 합류(11/20) 간호사도 월경계가 상대 병동 전월 꼬리를 읽지만 월초가 모두 차단일이라 생성에 쓰이지 않는다 — 전월 근무표가
    작성 중이어도 안내하지 않는다(같은 달 G2 는 마감본)."""
    build, _month, _kind = TARGETS["join"]
    case = dataclasses.replace(build("issued"), g2_oct={"X01": tail_cells("DDDDDN", 31)}, g2_oct_status="draft")
    assert _notices(engine, case) == []


def test_no_other_ward_schedule_no_notice(engine, engine_input_tables):
    """상대 병동 근무표가 없으면 읽은 것이 없어 안내도 없다(출발 경계도 걸지 않는다 — D34)."""
    assert _notices(engine, dataclasses.replace(depart_n07("DDODD", "Y"), g2_nov={})) == []
