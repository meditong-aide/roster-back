"""대화 상태 JSON 왕복(S10a) — 요청 종류마다·후보·결과·세션 전 칸이 JSON 문자열을 거쳐도 그대로인지, 크기 상한, 지문 불일치.

기대값은 손으로 만든 값이다(왕복 전후 동등성 + 손으로 센 크기 조건).
"""

from __future__ import annotations

import json

import pytest

from services.schedule_edit import serialize as ser
from services.schedule_edit.levers import Lever
from services.schedule_edit.precheck import Finding
from services.schedule_edit.requests import (
    AvoidCell, BalanceTarget, CountTarget, CoverTarget, GiveUpWish, GradeFloor, LeaveCell, SetCell, ShapeTarget,
    StaffingPlan, StaffRule, WishTarget,
)
from services.schedule_edit.rules import build_rules
from services.schedule_edit.session import EditSession
from services.schedule_edit.solver import Candidate, CellChange, EditResult
from tests.schedule_edit.test_solver import make_ctx


def _wire(data):
    """저장소를 거친 것처럼 — 문자열로 내렸다 올린다(튜플·집합은 여기서 모양이 바뀐다)."""
    return json.loads(json.dumps(data, ensure_ascii=False))


ALL_REQUESTS = [
    SetCell("A", 3, "O"), AvoidCell("A", 4, frozenset({"N", "E"})), LeaveCell("B", 5, "D"),
    CoverTarget(6, "D", 3, 2), CoverTarget(7, "E", 1),
    CountTarget("A", frozenset({"N"}), 4, 2, 20, "auto", 1, 6, True), CountTarget("B", frozenset({"D", "E"}), 9, 0, 29),
    BalanceTarget(frozenset({"N"}), ("A", "B"), 0, 29, 1, 5), BalanceTarget(frozenset({"O"}), ("A",), 3, 9),
    WishTarget(("A", "B"), 2, 29, 5, True, 1, frozenset({"O"}), (("A", 3), ("B", 9))), WishTarget((), 0, 29),
    ShapeTarget("isolated_off", ("A",), 2, 29, 1, True, True, 1), ShapeTarget("weekend", (), 0, 29),
    StaffingPlan(((StaffRule("all", "D", 3, 4, (1, 2), True), StaffRule("grade2", "N", 1)),
                  (StaffRule("all", "E", 2, None),)), 3, 20, 1, 2, True),
    StaffingPlan(((StaffRule("all", "D", 3),),), 0, 29),
    GradeFloor(2, 1, 3, 20, ("D", "E"), (4, 5), 1, False), GradeFloor(1, 2, 0, 29),
    GiveUpWish((("A", 3), ("B", 8))),
]


def _candidate(n_changes: int, rank: int = 1) -> Candidate:
    nurses = [f"N{k:02d}" for k in range(14)]
    changes = [CellChange(nurses[i % 14], f"김간호{i % 14}", i % 30, "D", "E", "D", "E", vacation=(i % 7 == 0))
               for i in range(n_changes)]
    return Candidate(rank=rank, changes=changes, affected=nurses, optimal=bool(rank % 2), window=(0, 29),
                     counts={n: {"D": (10, 9), "E": (5, 6), "N": (4, 4)} for n in nurses}, span=(2, 29),
                     loss=2 * n_changes + 27, quality=["N 간격이 좁아집니다", "주말 근무가 한쪽으로 몰립니다"])


def _result(n_changes: int = 3) -> EditResult:
    lever = Lever(allow={("OFF수", "A", 3): 1, ("인원부족", "abc123", "D", 4): 2}, lose=["A OFF 1일 줄임"],
                  changes=(None, 5), span=(7, 9), quality=["x"], gain="원티드 31개")
    lever2 = Lever(allow={("N간격", "B"): 1}, lose=[], changes=(8, 5), span=(None, 3))
    return EditResult(status="ok", candidates=[_candidate(n_changes, 1), _candidate(n_changes, 2)],
                      reasons=["이유 하나"], notes=["참고 하나"], blocking=["OFF수"], levers=[lever, lever2],
                      source="annual_leave_swap", extra=["못 넣는 원티드: A 3일"])


def _ctx():
    return make_ctx({"A": "DDOEE", "B": "ODDNN"}, cov_min={"D": 1})


def _fresh(stamp: str = "stamp1") -> EditSession:
    ctx = _ctx()
    return EditSession(schedule_id="s1", ctx=ctx, rules=build_rules(ctx), screen=None, stamp=stamp)


def test_every_request_kind_roundtrips():
    back = ser.requests_from_json(_wire(ser.requests_to_json(ALL_REQUESTS)))
    assert back == ALL_REQUESTS


def test_candidate_roundtrips_every_field():
    cand = _candidate(5)
    back = ser.candidate_from_json(_wire(ser.candidate_to_json(cand)))
    assert back == cand
    assert isinstance(back.window, tuple) and isinstance(back.span, tuple)
    assert back.counts["N00"]["D"] == (10, 9)


def test_result_roundtrips_every_field():
    res = _result()
    back = ser.result_from_json(_wire(ser.result_to_json(res)))
    assert back == res
    assert back.levers[0].allow == {("OFF수", "A", 3): 1, ("인원부족", "abc123", "D", 4): 2}
    assert back.levers[1].changes == (8, 5) and back.levers[1].span == (None, 3)
    assert back.source == "annual_leave_swap" and back.extra == ["못 넣는 원티드: A 3일"]


def test_session_state_roundtrips_all_stored_fields():
    s = _fresh()
    s.requests = list(ALL_REQUESTS)
    s.allow = {("OFF수", "A", 3): 2}
    s.avoid_people = [frozenset({"A", "B"}), frozenset({"B"})]
    s.confirmed = {"c1", "c2"}
    s.asking = [Finding("f1", "묻는 문장", "setting", {("OFF수", "A", 3): 1}), Finding("f2", "또", "quality")]
    s.held = (list(ALL_REQUESTS[:3]), _result(), [frozenset({"A"})])
    s.shown_notes = {"전월 근무표가 없어…"}
    s.last = _result()
    s.ctx.edit_from = 5
    data = _wire(ser.session_state_to_json(s))
    assert data["edit_from"] == 5 and data["candidates_dropped"] is False
    back = ser.session_state_from_json(data, rebuild=lambda sid: _fresh())
    assert back.schedule_id == "s1" and back.stamp == "stamp1"
    assert back.requests == s.requests and back.allow == s.allow and back.avoid_people == s.avoid_people
    assert back.confirmed == s.confirmed and back.asking == s.asking and back.shown_notes == s.shown_notes
    assert back.held == (s.held[0], None, s.held[2]) and back.last == s.last   # held 결과는 늘 버린다
    assert back.ctx.edit_from == 5 and back.candidates_dropped is False


def test_edit_from_argument_wins_and_none_is_kept():
    s = _fresh()
    assert ser.session_state_to_json(s)["edit_from"] is None
    assert ser.session_state_to_json(s, edit_from=7)["edit_from"] == 7
    back = ser.session_state_from_json(_wire(ser.session_state_to_json(s)), rebuild=lambda sid: _fresh())
    assert back.ctx.edit_from is None


def test_last_keeps_at_most_three_candidates():
    s = _fresh()
    res = _result(2)
    res.candidates = [_candidate(2, r) for r in range(1, 6)]
    s.last = res
    back = ser.session_state_from_json(_wire(ser.session_state_to_json(s)), rebuild=lambda sid: _fresh())
    assert [c.rank for c in back.last.candidates] == [1, 2, 3]


def test_held_result_is_always_dropped_even_with_same_stamp():
    s = _fresh()
    s.held = (list(ALL_REQUESTS[:2]), _result(), [frozenset({"A"})])
    back = ser.session_state_from_json(_wire(ser.session_state_to_json(s)), rebuild=lambda sid: _fresh())
    assert back.held == (list(ALL_REQUESTS[:2]), None, [frozenset({"A"})])


def test_restored_last_candidate_passes_apply_identity_check():
    """apply_to_new_version 첫 줄 `any(c is candidate for c in session.last.candidates)` 와 같은 비교."""
    s = _fresh()
    s.last = _result()
    back = ser.session_state_from_json(_wire(ser.session_state_to_json(s)), rebuild=lambda sid: _fresh())
    cand = back.last.candidates[1]
    assert back.last is not None and any(c is cand for c in back.last.candidates)
    assert not any(c is cand for c in s.last.candidates)      # 원래 세션의 객체와는 다르다(복원본 안에서만 통한다)


def test_stamp_mismatch_drops_only_candidates():
    s = _fresh("old")
    s.requests = list(ALL_REQUESTS)
    s.allow = {("OFF수", "A", 3): 2}
    s.avoid_people = [frozenset({"A"})]
    s.confirmed = {"c1"}
    s.asking = [Finding("f1", "묻는 문장", "setting")]
    s.held = (list(ALL_REQUESTS[:2]), _result(), [frozenset({"A"})])
    s.shown_notes = {"문구"}
    s.last = _result()
    s.ctx.edit_from = 3
    back = ser.session_state_from_json(_wire(ser.session_state_to_json(s)), rebuild=lambda sid: _fresh("new"))
    assert back.stamp == "new"
    assert back.last is None and back.held[1] is None
    assert back.held[0] == list(ALL_REQUESTS[:2]) and back.held[2] == [frozenset({"A"})]
    assert back.requests == s.requests and back.allow == s.allow and back.avoid_people == s.avoid_people
    assert back.confirmed == {"c1"} and back.asking == s.asking and back.shown_notes == {"문구"}
    assert back.ctx.edit_from == 3 and back.candidates_dropped is False


def test_none_last_and_held_roundtrip():
    back = ser.session_state_from_json(_wire(ser.session_state_to_json(_fresh())), rebuild=lambda sid: _fresh())
    assert back.last is None and back.held is None and back.requests == []


def test_size_of_116_cell_candidates_fits_limit():
    """7B 규모 — 바뀐 칸 116개짜리 후보 3개. 실측 바이트는 -s 로 보인다."""
    s = _fresh()
    res = EditResult(status="ok", candidates=[_candidate(116, r) for r in (1, 2, 3)], levers=_result().levers)
    s.last = res
    s.requests = list(ALL_REQUESTS)
    data = ser.session_state_to_json(s)
    size = len(json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
    print(f"\n[실측] 후보 3개 x 116칸 상태 JSON = {size} 바이트")
    assert size <= ser.SESSION_JSON_LIMIT
    assert len(data["last"]["candidates"]) == 3 and data["candidates_dropped"] is False


def _big_session():
    s = _fresh()
    s.requests = [SetCell("A", 3, "O")]
    s.last = EditResult(status="ok", candidates=[_candidate(116, r) for r in (1, 2, 3)], notes=["참고"],
                        levers=_result().levers)
    s.held = ([SetCell("A", 1, "O")], _result(116), [])
    return s


def _size(data):
    return len(json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


def test_stage1_over_limit_drops_candidates_only(monkeypatch):
    s = _big_session()
    s.last.levers = []
    s.held = ([SetCell("A", 1, "O")], EditResult(status="ok", candidates=[_candidate(116)]), [])
    monkeypatch.setattr(ser, "SESSION_JSON_LIMIT", 6000)
    data = _wire(ser.session_state_to_json(s))
    assert data["last"]["candidates"] == [] and data["held"]["result"]["candidates"] == []
    assert data["last"]["notes"] == ["참고"] and data["candidates_dropped"] is True and data["levers_dropped"] is False
    assert _size(data) <= 6000
    back = ser.session_state_from_json(data, rebuild=lambda sid: _fresh())
    assert back.candidates_dropped is True and back.levers_dropped is False
    assert back.last.candidates == [] and back.requests == [SetCell("A", 3, "O")]


def test_stage2_levers_dropped_when_still_over(monkeypatch):
    s = _big_session()
    probe = ser.session_state_to_json(s)
    probe["last"]["candidates"] = []
    probe["held"]["result"]["candidates"] = []
    limit = _size(probe) - 10      # 후보만 빼서는 모자라고 레버까지 빼면 들어가는 상한
    monkeypatch.setattr(ser, "SESSION_JSON_LIMIT", limit)
    data = _wire(ser.session_state_to_json(s))
    assert data["candidates_dropped"] is True and data["levers_dropped"] is True
    assert data["last"]["levers"] == [] and data["held"]["result"]["levers"] == []
    assert _size(data) <= limit
    back = ser.session_state_from_json(data, rebuild=lambda sid: _fresh())
    assert back.levers_dropped is True and back.last.levers == []


def test_stage3_raises_when_nothing_left_to_drop(monkeypatch):
    monkeypatch.setattr(ser, "SESSION_JSON_LIMIT", 100)
    with pytest.raises(ser.SessionStateTooLarge):
        ser.session_state_to_json(_big_session())
    assert issubclass(ser.SessionStateTooLarge, ValueError)


def test_flags_stay_false_when_nothing_was_dropped(monkeypatch):
    """상한을 넘었어도 뺄 후보·레버가 원래 없으면 표시는 False(그리고 예외)."""
    s = _fresh()
    s.requests = list(ALL_REQUESTS)
    s.last = EditResult(status="infeasible", reasons=["이유"])
    monkeypatch.setattr(ser, "SESSION_JSON_LIMIT", 100)
    with pytest.raises(ser.SessionStateTooLarge):
        ser.session_state_to_json(s)
    monkeypatch.setattr(ser, "SESSION_JSON_LIMIT", 10_000_000)
    data = ser.session_state_to_json(s)
    assert data["candidates_dropped"] is False and data["levers_dropped"] is False


def test_under_limit_keeps_candidates(monkeypatch):
    monkeypatch.setattr(ser, "SESSION_JSON_LIMIT", 10_000_000)
    s = _fresh()
    s.last = _result(4)
    assert ser.session_state_to_json(s)["candidates_dropped"] is False
