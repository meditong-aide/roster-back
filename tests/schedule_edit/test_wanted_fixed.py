"""원티드 칸은 수간호사가 그 칸을 직접 짚을 때만 바꾼다(사용자 지시 11-03).

  · 확정 원티드(고정 칸) — 짚지 않으면 솔버·재확인 어디서도 못 바꾸고, 짚으면 그 칸만 풀어 바꾼다.
  · 지금 반영된 제출 원티드 — 하드로 지키고(짚은 칸만 면제), 넘기자는 레버 카드를 내밀지 않는다.
"""

from __future__ import annotations

from services.schedule_edit.context import _load_ban_n_before, with_released
from services.schedule_edit.levers import LEVER_FAMILIES
from services.schedule_edit.model import requested_cells
from services.schedule_edit.requests import AvoidCell, LeaveCell, SetCell
from services.schedule_edit.rules import WISH, build_rules
from services.schedule_edit.solver import verify_candidate
from tests.schedule_edit.test_solver import BEFORE_MONTH, _two_nurse_ctx, make_ctx, propose


def _changed(res):
    return {(c.nurse_id, c.day) for cand in res.candidates for c in cand.changes}


def test_fixed_wanted_stays_unless_named():
    ctx = _two_nurse_ctx()                     # A DODO · B ODOD · 하루 D 1명 · OFF 2개
    ctx.frozen[("A", 1)] = "확정 원티드"
    res = propose(ctx, [SetCell("A", 0, "O")])
    assert res.status == "ok" and ("A", 1) not in _changed(res)          # 연쇄 변경도 그 칸은 건드리지 않는다

    named = propose(ctx, [SetCell("A", 1, "D")])
    assert named.status == "ok" and ("A", 1) in _changed(named)
    assert "A 2일은 확정 원티드 칸이지만 직접 요청하셔서 바꿉니다." in named.notes
    cand = named.candidates[0]
    rules = build_rules(ctx)
    assert verify_candidate(ctx, rules, cand, [SetCell("A", 1, "D")], today=BEFORE_MONTH) == []
    # 같은 후보라도 그 칸을 짚은 요청이 없으면 적용 직전 재확인이 막는다
    assert any("확정 원티드" in p for p in verify_candidate(ctx, rules, cand, [], today=BEFORE_MONTH))


def test_released_copy_recomputes_the_night_ban_and_leaves_the_original():
    ctx = make_ctx({"A": "DDOD", "B": "ODDO"}, config={"ban_night_before_fixed_wanted_off": True})
    ctx.frozen[("A", 2)] = "확정 원티드"
    _load_ban_n_before(ctx)
    assert ("A", 1) in ctx.ban_n_before                                  # 확정 원티드 OFF 전날 N 금지
    out = with_released(ctx, [("A", 2), ("B", 0)])
    assert ("A", 2) not in out.frozen and ("A", 1) not in out.ban_n_before
    assert ("A", 2) in ctx.frozen and ("A", 1) in ctx.ban_n_before      # 원래 재료는 그대로
    assert with_released(ctx, [("B", 0)]) is ctx                         # 풀 칸이 없으면 사본도 안 만든다


def test_submitted_wanted_is_kept_and_never_offered_as_a_lever():
    ctx = _two_nurse_ctx()
    ctx.wishes = {("A", 1): {"O"}, ("A", 3): {"O"}}                     # A 의 OFF 둘 다 원티드로 받은 칸
    res = propose(ctx, [SetCell("A", 0, "O")])
    assert res.status != "ok"
    assert WISH not in LEVER_FAMILIES
    assert all(key[0] != WISH for lever in res.levers for key in lever.allow)
    assert any(r.startswith("원티드가 반영된 칸(A ") and "직접 말씀해 주세요" in r for r in res.reasons), res.reasons  # 2·4일 대칭
    # 그 원티드 칸을 직접 짚으면 요청이 이긴다
    named = propose(ctx, [SetCell("A", 0, "O"), SetCell("A", 1, "D")])
    assert named.status == "ok" and ("A", 1) in _changed(named)
    assert "A 2일은 원티드가 반영된 칸이지만 직접 요청하셔서 바꿉니다." in named.notes


def test_already_done_cells_in_a_named_range_do_not_release_the_wanted():
    """기간 OFF·'D 넣지 마' 안에 이미 그렇게 된 원티드 칸은 '바꿀 칸'이 아니다 — 풀지도 면제하지도 않는다(검토 11-03 MEDIUM)."""
    ctx = make_ctx({"A": "DDNOD", "B": "DNNOO", "C": "ODDND"}, config={"ban_night_before_fixed_wanted_off": True,
                                                                      "not_one_night": False})
    ctx.frozen[("A", 3)] = "확정 원티드"                                  # A 4일 OFF 확정 원티드 → 3일 N 금지
    _load_ban_n_before(ctx)
    assert ("A", 2) in ctx.ban_n_before
    reqs = [SetCell("A", 3, "O"), SetCell("A", 4, "O")]                 # 'A 4~5일 OFF' — 4일은 이미 OFF
    assert requested_cells(ctx, reqs) == {("A", 4)}
    res = propose(ctx, reqs)
    assert res.status == "ok" and ("A", 3) not in _changed(res)
    assert not any("확정 원티드" in n for n in res.notes)                # 풀지 않았다
    ctx.wishes = {("B", 3): {"O"}}                                      # B 4일 OFF 는 원티드로 받은 칸
    assert requested_cells(ctx, [AvoidCell("B", 3, frozenset({"D"}))]) == set()   # 'B 4일 D 넣지 마' — 이미 O


def test_leave_that_needs_a_wanted_night_changed_asks_to_name_it():
    ctx = make_ctx({"A": "DNNDO", "B": "NNOOD"}, config={"ban_night_before_fixed_off": True, "not_one_night": False})
    ctx.leave_codes = {"연": "연차휴가"}
    ctx.wishes = {("A", 2): {"N"}}                                      # A 3일 N 은 원티드
    res = propose(ctx, [LeaveCell("A", 3, "연")])
    assert res.status == "invalid" and "A 3일 나이트는 원티드가 반영된 칸" in res.reasons[0], res.reasons
