"""버전 비교·되돌리기(11-04 · 사용자 결정 '통째는 열기·일부는 새 버전') — 통째로 되돌리면 그 버전을 열기만(저장 없음),
사람·기간을 짚으면 그 칸만 그 버전 값으로 바꾸는 안(칸 지정 요청과 같은 길). 버전 행·화면 비교는 가짜로 바꾼다."""

from __future__ import annotations

from datetime import date, datetime
from types import SimpleNamespace

from schedule_edit_agent import skills as skills_mod
from schedule_edit_agent.answer import describe_requests
from schedule_edit_agent.view import blocks_for
from services import schedule_compare_service
from services.schedule_edit import solver as solver_mod
from services.schedule_edit import versions
from services.schedule_edit.requests import LeaveCell, SetCell
from tests.edit_agent.test_agent import BEFORE_MONTH, _agent, call, make_ctx, press, say

LOOSE = {"not_one_night": False, "two_offs_after_two_nig": False, "isolated_work_hard": False}


def _ver(sid, version, name, at):
    return SimpleNamespace(schedule_id=sid, version=version, name=name, status="draft", created_at=at,
                           group_id="g1", year=2026, month=11, dropped=False)


V1 = _ver("v1", 1, "11월 근무표 VER1", datetime(2026, 11, 2, 9, 0))
V2 = _ver("v2", 2, "11월 근무표 VER1 수정본", datetime(2026, 11, 3, 10, 13))
V3 = _ver("s1", 3, "11월 근무표 VER1 수정본 2", datetime(2026, 11, 3, 15, 20))


def _versions(monkeypatch, source="v1"):
    monkeypatch.setattr(versions, "versions_of", lambda db, s: [V1, V2, V3])
    monkeypatch.setattr(versions, "_source_of", lambda db, sid: source)
    monkeypatch.setattr(skills_mod, "_current_schedule", lambda turn: V3)


def test_each_way_of_naming_a_version(monkeypatch):
    _versions(monkeypatch)
    pick = lambda which, **kw: versions.pick_version(None, V3, which, **kw)   # noqa: E731
    assert pick("previous") == (V1, None)                     # 이 버전을 만든 버전(출처) — 번호 바로 앞(VER2)이 아니다
    assert pick("number", number=2) == (V2, None)
    assert pick("number", number=3)[1].startswith("지금 보고 있는 버전이 VER3입니다.")
    assert pick("number", number=9)[1] == "VER9는 없습니다. 이 달 버전: VER1(11-02)·VER2(11-03)·VER3(11-03)"
    assert pick("first") == (V1, None)
    assert pick("latest")[1].startswith("지금 보고 있는 버전이 이 달 가장 최근 버전입니다.")
    assert pick("date", on="2026-11-03") == (V2, None)          # 그날 만든 다른 버전이 하나
    assert pick("date", on="2026-11-05")[1].startswith("11월 5일에 만든 다른 버전이 없습니다.")
    assert pick("name", name="11월 근무표 VER1 수정본") == (V2, None)   # 같은 이름이 먼저('… 수정본 2'가 아니라)
    assert pick("뭔가") == (None, versions.WHICH_ASK)
    monkeypatch.setattr(versions, "_source_of", lambda db, sid: None)
    assert pick("previous") == (V2, None)                     # 출처가 없으면 번호 바로 앞


def test_revert_plan_takes_only_cells_that_can_change():
    ctx = make_ctx({"A": "DDDOD", "B": "OODDE"}, config=LOOSE)
    ctx.leave_codes = {"연": "연차"}
    ctx.frozen[("B", 3)] = "관할 밖(파견·이동·휴직·프리셉티)"
    other = make_ctx({"A": "OEDOD", "B": "DOEON"}, config=LOOSE)
    other.raw["A"][2] = "연"                                   # A 3일 연차(근무일에 넣는 휴가)
    other.vacation.add(("A", 2))
    other.raw["B"][1], other.main["B"][1] = "연", "O"          # B 2일 연차 — 지금 OFF 인 날이라 넣지 않는다
    other.vacation.add(("B", 1))
    other.raw["B"][4] = "E1"                                   # 같은 E 의 하위 코드만 다른 칸
    other.main["B"][4] = "E"
    plan = versions.revert_plan(ctx, other, (), 0, 4, open_from=1)
    assert plan.requests == [SetCell("A", 1, "E"), LeaveCell("A", 2, "연"), SetCell("B", 2, "E")]
    assert (plan.differ, plan.skipped) == (8, {"past": 2, "leave_on_off": 1, "frozen": 1, "subcode": 1})
    other.raw["A"][4] = None                                   # 그 버전에 칸이 없는 날도 화면 비교처럼 센다
    assert versions.revert_plan(ctx, other, ("A",), 0, 4, open_from=1).skipped == {"past": 1, "empty": 1}


def test_reverting_the_whole_version_only_opens_it(world, monkeypatch):
    _versions(monkeypatch)
    agent, _ = _agent(call("revert_version", which="previous"))
    reply = say(agent, world.conv, "방금 거 취소해줘", BEFORE_MONTH)
    assert reply.effects == [{"type": "switch_version", "opened": True, "schedule_id": "v1", "version": 1,
                              "status": "draft", "name": "11월 근무표 VER1"}]
    assert reply.answer.startswith("VER1(11월 근무표 VER1)을 엽니다. 지금 보던 VER3(11월 근무표 VER1 수정본 2)는 그대로")
    assert world.conv.context["schedule_id"] == "v1"           # 대화도 그 버전으로(저장한 것은 없다)
    notice = blocks_for(reply, None)[0]
    assert notice["title"] == "VER1을 열었습니다" and "저장" not in notice["title"]
    assert [e.event for e in world.events if e.event in ("open", "apply")] == ["open"]


def test_reverting_some_cells_goes_the_usual_way(world, monkeypatch):
    def ward():
        return make_ctx({"A": "DODO", "B": "ODOD"}, cov_min={"D": 1}, config=LOOSE)

    def old(db, ctx, schedule_id):
        return make_ctx({"A": "ODOD", "B": "DODO"}, cov_min={"D": 1}, config=LOOSE)
    _versions(monkeypatch)
    monkeypatch.setattr(versions, "load_version_cells", old)
    world.ctxs["s1"] = ward
    agent, _ = _agent(call("revert_version", which="number", number=1, nurses=["A"]))
    reply = say(agent, world.conv, "A만 VER1처럼 해줘", BEFORE_MONTH)
    assert reply.answer.startswith("VER1(11월 근무표 VER1)에서 A 칸 4개를 가져옵니다.\n"), reply.answer
    assert reply.preview["status"] == "ok"                     # 매일 D 1명 — B 도 따라 바뀌어야 한다
    assert {(c["nurse_id"], c["day"]) for c in reply.preview["candidates"][0]["changes"]} >= {("A", 1), ("B", 1)}
    assert world.conv.state.requests == [SetCell("A", d, c) for d, c in enumerate("ODOD")]
    assert not reply.effects                                   # 적용 전까지 저장하지 않는다


def test_nothing_to_take_back_says_why(world, monkeypatch):
    _versions(monkeypatch)
    monkeypatch.setattr(versions, "load_version_cells",
                        lambda db, ctx, sid: make_ctx({"A": "OODO", "B": "ODOD"}, config=LOOSE))
    agent, _ = _agent(call("revert_version", which="first", nurses=["A"], day=1, to_day=1))
    reply = say(agent, world.conv, "A 1일 처음 버전처럼", date(2026, 11, 2))     # 1일은 지난 날
    assert reply.answer == ("VER1(11월 근무표 VER1)과 A · 1일에서 되돌릴 칸이 없습니다 — 다른 칸 1개는 모두 그대로 둬야 하는 "
                            "칸입니다(지난 날 1칸).")


def test_compare_shows_the_cells_and_opens_on_the_button(world, monkeypatch):
    _versions(monkeypatch)
    data = {"cells": [{"nurse_id": "A", "nurse_name": "A", "date": "2026-11-01", "from": {"shift_id": "O"},
                       "to": {"shift_id": "D"}},
                      {"nurse_id": "B", "nurse_name": "B", "date": "2026-11-01", "from": {"shift_id": "D"},
                       "to": {"shift_id": "O"}}],
            "added_nurses": [{"nurse_id": "C"}], "removed_nurses": []}
    monkeypatch.setattr(schedule_compare_service, "compare_schedules", lambda db, src, dst: data)
    agent, _ = _agent(call("compare_versions", which="number", number=1), call("compare_versions", which="number", number=1))
    reply = say(agent, world.conv, "VER1이랑 뭐가 달라?")
    assert reply.answer == ("VER1(11월 근무표 VER1) → 지금 버전 VER3(11월 근무표 VER1 수정본 2): 다른 칸 2개 · 2명\n"
                            "· A 1칸: 1일 OFF→D\n· B 1칸: 1일 D→OFF\n"
                            "명단: 지금 버전에만 있는 사람 1명 · VER1에만 있는 사람 0명")
    text, cards = blocks_for(reply, world.conv.state)
    assert text["text"].split("\n") == [reply.answer.split("\n")[0], reply.answer.split("\n")[-1]]
    card = cards["items"][0]
    assert card["title"] == "VER1(11월 근무표 VER1)과 다른 칸 2개"
    # 표 미리보기는 지금 값 → 그 버전 값(검토 11-04 HIGH: 거꾸로 넣어 표에 지금 값이 그대로 보였다)
    assert card["overlay"]["cells"][0] == {"nurse_id": "A", "day": 1, "before": "D", "after": "O"}
    assert card["details"]["items"][0] == "A 1일 지금 D · VER1 O"
    data["cells"].append({"nurse_id": "B", "nurse_name": "B", "date": "2026-11-02", "from": None, "to": {"shift_id": "E"}})
    again = blocks_for(say(agent, world.conv, "VER1이랑 뭐가 달라?"), world.conv.state)[1]["items"][0]
    assert again["title"].endswith("다른 칸 3개") and again["details"]["items"][-1] == "B 2일 지금 E · VER1 빈 칸"
    assert len(again["overlay"]["cells"]) == 2                 # 빈 칸은 표 미리보기에서만 뺀다(화면 스키마 after 는 문자열)
    button = card["actions"][0]["input"]
    assert (card["actions"][0]["label"], button["action"]) == ("VER1 열기", "open_version")
    opened = press(agent, world.conv, "open_version", button["params"])
    from schedule_edit_agent.answer import compare_answer
    assert compare_answer("VER1", "VER3", data, "Z", "장원영") == "VER1 → 지금 버전 VER3: 장원영 칸이 모두 같습니다."
    assert opened.effects[0]["opened"] and opened.effects[0]["schedule_id"] == "v1"


def test_many_cells_are_described_per_person_and_get_room_to_move():
    ctx = make_ctx({"A": "DDDDDOO", "B": "OODDDDD"}, config=LOOSE)
    reqs = [SetCell("A", d, "O") for d in range(5)] + [SetCell("B", d, "D") for d in (0, 1)]
    assert describe_requests(ctx, reqs) == "A 칸 5개, B 칸 2개 지정"
    assert describe_requests(ctx, reqs[:2]) == "A 1일 OFF, A 2일 OFF"            # 적으면 칸마다 그대로
    assert solver_mod._max_changes(reqs[:5]) == solver_mod.MAX_CHANGES             # 평소 요청은 상한 그대로
    assert solver_mod._max_changes(reqs * 3) == 21 + solver_mod.CELL_EXTRA_CHANGES


def test_reverting_into_a_hard_lock_is_worked_around_or_blocked(world, monkeypatch):
    # 그 버전은 2일 N · 3일 D. 2일만 되돌리면 N→D(하드락) — 3일을 바꿀 수 있으면 함께 바꿔 피하고, 묶여 있으면 막는다
    _versions(monkeypatch)
    monkeypatch.setattr(versions, "load_version_cells",
                        lambda db, ctx, sid: make_ctx({"A": "ONDO", "B": "DDOD"}, config=LOOSE))
    world.ctxs["s1"] = lambda: make_ctx({"A": "OODO", "B": "DDOD"}, config=LOOSE)
    agent, _ = _agent(call("revert_version", which="number", number=1, nurses=["A"]),
                      call("revert_version", which="number", number=1, nurses=["A"]))
    reply = say(agent, world.conv, "A만 VER1처럼")
    first = reply.preview["candidates"][0]["changes"]
    assert {(c["day"], c["after"]) for c in first if c["nurse_id"] == "A"} >= {(2, "N")}
    assert (3, "D") not in {(c["day"], c["after"]) for c in first} and any(c["day"] == 3 for c in first)

    def tied():
        ctx = make_ctx({"A": "OODO", "B": "DDOD"}, config=LOOSE)
        ctx.frozen[("A", 2)] = "관할 밖(파견·이동·휴직·프리셉티)"
        return ctx
    world.ctxs["s1"] = tied
    world.conv.state = None
    blocked = say(agent, world.conv, "A만 VER1처럼")
    assert "하드락" in blocked.answer and not (blocked.preview or {}).get("levers"), blocked.answer
    assert not (blocked.preview or {}).get("candidates")


def test_too_many_cells_ask_to_narrow(world, monkeypatch):
    _versions(monkeypatch)
    monkeypatch.setattr(versions, "REVERT_MAX_CELLS", 2)
    monkeypatch.setattr(versions, "load_version_cells",
                        lambda db, ctx, sid: make_ctx({"A": "ODOD", "B": "DODO"}, config=LOOSE))
    agent, _ = _agent(call("revert_version", which="number", number=1, day=1, to_day=4))
    reply = say(agent, world.conv, "1일부터 4일까지 VER1로")
    assert reply.answer.startswith("VER1(11월 근무표 VER1)에서 가져올 칸이 8개라 한 번에 찾기엔 많습니다.")


def test_particles_follow_the_last_sound():
    from services.schedule_edit.requests import eul, eun, gwa
    assert [eul("VER1"), eul("VER2"), eun("VER3"), eun("VER4"), gwa("VER1(11월 근무표 VER1)"), gwa("VER2")] == [
        "VER1을", "VER2를", "VER3은", "VER4는", "VER1(11월 근무표 VER1)과", "VER2와"]
    assert [eul("연차"), eul("OFF")] == ["연차를", "OFF을(를)"]

