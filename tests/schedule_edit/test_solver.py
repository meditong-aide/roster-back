"""schedule_edit 솔버 핵심 — DB 없이 손으로 만든 작은 근무표로 본다.

기대값은 규칙을 손으로 따라가 센 값이다(구현 출력을 복사하지 않았다).
"""

from __future__ import annotations

from datetime import date

import pytest

from services.schedule_edit import solver as solver_mod
from services.schedule_edit.context import EditContext
from services.schedule_edit.requests import AvoidCell, SetCell, swap
from services.schedule_edit.rules import build_rules, measure
from services.schedule_edit.solver import apply_candidate, propose_edits

#: 시험 근무표(2026-11)보다 앞선 '오늘' — 모든 날을 바꿀 수 있다. 실제 오늘 날짜에 시험이 흔들리지 않게 고정.
BEFORE_MONTH = date(2026, 10, 31)


def propose(ctx, requests, **kw):
    return propose_edits(ctx, requests, today=kw.pop("today", BEFORE_MONTH), **kw)

BASE_CONFIG = {
    "max_conseq_work": 5, "three_seq_nig": True, "not_one_night": True,
    "banned_day_after_eve": True, "two_offs_after_two_nig": True, "two_offs_after_three_nig": True,
    "max_nig_per_month": 15, "off_first": False,
}


def make_ctx(rows: dict, *, cov_min=None, cov_max=None, off_days=None, config=None) -> EditContext:
    """rows = {이름: 'DONE…'} — 한 글자가 하루. 이름을 nurse_id 로 그대로 쓴다."""
    days = len(next(iter(rows.values())))
    ctx = EditContext(schedule_id="s1", group_id="g1", office_id="o1", year=2026, month=11, days=days)
    ctx.config = {**BASE_CONFIG, **(config or {})}
    ctx.rep_code = {c: c for c in ("D", "E", "N", "M", "O")}
    for nid, row in rows.items():
        ctx.nurse_ids.append(nid)
        ctx.names[nid] = nid
        ctx.main[nid] = list(row)
        ctx.raw[nid] = list(row)
        ctx.raw_ids[nid] = [None] * days
        if off_days is not None:
            ctx.off_bounds[nid] = (off_days, off_days)
    ctx.cov_min = [dict(cov_min or {}) for _ in range(days)]
    ctx.cov_max = [dict(cov_max or {}) for _ in range(days)]
    return ctx


def kinds(ctx: EditContext) -> set:
    return {k[0] for k in measure(ctx, ctx.main, build_rules(ctx))}


# ── 규칙(검증기) ────────────────────────────────────────────────────────────


@pytest.mark.parametrize("row,expected", [
    ("DNODD", {"1N"}),               # 2일 N 단독
    ("NNDOO", {"2N후OFF", "전환금지"}),  # NN 뒤 3일 D: 회복 OFF 아님 + N→D
    ("EDOOO", {"전환금지"}),           # E→D
    ("DDDDDDO", {"연속근무"}),         # 6연속
    ("NNNNOOO", {"연속나이트", "3N후OFF"}),  # 4연속 N — 1~3일 NNN 뒤 4일이 OFF 가 아니다
    ("DDOODDO", set()),
])
def test_measure_flags_sequence_rules(row, expected):
    assert kinds(make_ctx({"A": row})) == expected


def test_measure_uses_previous_month_tail():
    # 전월 말 N N 뒤 1일 D → 회복 OFF 위반 + N→D. 월 안만 보면 깨끗해 보인다.
    ctx = make_ctx({"A": "DOODO"})
    ctx.tail = {"A": ["O", "O", "O", "O", "N", "N"]}
    assert kinds(ctx) == {"2N후OFF", "전환금지"}


def test_single_night_exemption_for_monthly_cap_one():
    ctx = make_ctx({"A": "DNODD"})
    ctx.single_n_ok = {"A"}
    assert kinds(ctx) == set()


def test_coverage_min_and_max():
    ctx = make_ctx({"A": "DD", "B": "DO"}, cov_min={"D": 2}, cov_max={"D": 2})
    v = measure(ctx, ctx.main, build_rules(ctx))
    assert v == {("인원최소", None, 1, "D"): 1}   # 2일 D 1명(최소 2)


# ── 후보 ────────────────────────────────────────────────────────────────────


def _two_nurse_ctx():
    """하루 D 정확히 1명, E·N 금지(요구 0), OFF 2개 고정."""
    return make_ctx({"A": "DODO", "B": "ODOD"}, cov_min={"D": 1, "E": 0, "N": 0},
                    cov_max={"D": 1}, off_days=2, config={"not_one_night": False})


def test_off_request_needs_four_cells():
    # A 1일 OFF → B 가 1일 D(인원 1). A 는 OFF 3개가 되니 다른 날 하나 근무, 그날 B 가 쉰다 → 4칸
    ctx = _two_nurse_ctx()
    res = propose(ctx, [SetCell("A", 0, "O")])
    assert res.status == "ok"
    best = res.candidates[0]
    assert best.changed_count == 4
    assert best.optimal
    grid = apply_candidate(ctx, best)
    assert grid["A"][0] == "O" and grid["B"][0] == "D"
    assert measure(ctx, grid, build_rules(ctx)) == {}


def test_alternative_candidates_change_different_people_or_cells():
    ctx = _two_nurse_ctx()
    res = propose(ctx, [SetCell("A", 0, "O")])
    # 두 사람뿐이라 '바뀐 사람 집합'이 다른 대안은 없다 → 1개
    assert len(res.candidates) == 1


def test_alternatives_use_other_people():
    # B·C 가 똑같이 생겼다 → B 로 메우는 안과 C 로 메우는 안 둘 다 4칸. 바뀐 사람 집합이 달라야 한다.
    ctx = make_ctx({"A": "DODO", "B": "ODOD", "C": "ODOD"}, cov_min={"D": 1, "E": 0, "N": 0},
                   cov_max={"D": 2}, off_days=2, config={"not_one_night": False})
    res = propose(ctx, [SetCell("A", 0, "O")])
    people = [frozenset(c.affected) for c in res.candidates]
    assert len(people) >= 2 and len(set(people)) == len(people)
    assert all(c.loss <= res.candidates[0].loss + solver_mod.EXTRA_LOSS for c in res.candidates)
    for c in res.candidates:
        assert measure(ctx, apply_candidate(ctx, c), build_rules(ctx)) == {}


def test_swap_also_rebalances_off_count():
    ctx = _two_nurse_ctx()
    res = propose(ctx, swap(ctx, "A", "B", 0))
    assert res.status == "ok"
    # A·B 1일 교환만으로는 OFF 수가 바뀐다(A 3, B 1) → 다른 날도 교환해야 한다: 4칸
    assert res.candidates[0].changed_count == 4


def test_already_satisfied_and_invalid():
    ctx = _two_nurse_ctx()
    assert propose(ctx, [SetCell("A", 0, "D")]).status == "already"
    ctx.frozen[("A", 1)] = "확정 원티드"
    res = propose(ctx, [SetCell("A", 1, "D")])
    assert res.status == "invalid"
    assert "확정 원티드" in res.reasons[0]


def test_existing_violation_may_stay_but_no_new_one():
    # A 는 이미 2일에 1N 이 있다(기준선 위반). 다른 칸을 바꿔도 그 1N 은 남아도 되지만 새 1N 은 안 된다.
    ctx = make_ctx({"A": "DNODDOO", "B": "ODDODDO"}, config={"isolated_work_hard": False})
    before = measure(ctx, ctx.main, build_rules(ctx))
    assert ("1N", "A", 1, None) in before
    res = propose(ctx, [SetCell("B", 5, "N")])
    # B 6일 N 단독은 1N → 뒤(7일)도 N 이어야 한다. A 의 기존 1N 은 그대로 둬도 된다.
    assert res.status == "ok"
    grid = apply_candidate(ctx, res.candidates[0])
    assert grid["B"][5] == "N" and grid["B"][6] == "N"
    assert grid["A"] == list("DNODDOO")


def test_last_day_single_night_cannot_borrow_earlier_day():
    # 7일(말일) N 하나만 넣으면 1N(말일은 다음 달을 못 본다 — 엔진과 같다). 6일을 N 으로 바꾸면 되지만
    # 6일은 요청한 날보다 앞이라 못 바꾼다 → 불가(하드락이라 넘김 안내도 없다).
    ctx = make_ctx({"A": "DNODDOO", "B": "ODDODDO"}, config={"isolated_work_hard": False})
    res = propose(ctx, [SetCell("B", 6, "N")])
    assert res.status == "infeasible"
    assert res.blocking == ["1N"]
    assert "B 7일을 N으로 하면 나이트 단독(1N) 금지에 걸립니다(6일 D" in res.reasons[1]


def test_hard_lock_blocks_and_is_explained():
    # 1일 N 이 고정(확정 원티드)인데 2일 D 를 요청 → N→D 는 하드락. 어느 칸 때문인지 짚어야 한다.
    ctx = make_ctx({"A": "NOOOO"}, config={"not_one_night": False})
    ctx.frozen[("A", 0)] = "확정 원티드"
    res = propose(ctx, [SetCell("A", 1, "D")])
    assert res.status == "infeasible"
    assert res.blocking == ["전환금지"]
    assert "A 2일을 D로 하면 N→D 전환 금지에 걸립니다(1일 N" in res.reasons[1]


@pytest.mark.parametrize("row,raw0,code,expected", [
    # M 은 전날 D·OFF·M 일 때만 — 1일 E 고정 뒤 2일 M
    ("EOOOO", "E", "M", "A 2일을 M으로 하면 M 전날 E·N 금지(M 은 전날 D·OFF·M 일 때만)에 걸립니다(1일 E"),
    # M→D 금지 — 병동이 MID 를 D2 로 쓰면 화면 코드와 대표코드를 함께 보인다
    ("MOOOO", "D2", "D", "A 2일을 D로 하면 M→D 전환 금지에 걸립니다(1일 D2(M)"),
])
def test_mid_transition_conflicts_name_the_mid_rule(row, raw0, code, expected):
    ctx = make_ctx({"A": row}, config={"not_one_night": False, "use_mid": True})
    ctx.use_mid = True
    ctx.raw["A"][0] = raw0
    ctx.frozen[("A", 0)] = "확정 원티드"
    res = propose(ctx, [SetCell("A", 1, code)])
    assert res.status == "infeasible" and res.blocking == ["전환금지"]
    assert expected in res.reasons[1]


def test_change_cap_is_reported_with_settings_to_bend(monkeypatch):
    # 규칙을 다 지키려면 4칸이 필요한데 상한이 2칸이다. 2칸 이내에서는 어떻게 해도 넘김 2개가 필요하다:
    # A 만 OFF(1칸) → 1일 D 미달 + A OFF 3개 / B 도 바꿈(2칸) → A OFF 3개 + B OFF 1개 등.
    monkeypatch.setattr(solver_mod, "MAX_CHANGES", 2)
    ctx = _two_nurse_ctx()
    res = propose(ctx, [SetCell("A", 0, "O")])
    assert res.status == "infeasible"
    assert "2칸 이내" in res.reasons[0]
    assert res.blocking and set(res.blocking) <= {"인원최소", "인원최대", "OFF수"}
    bent = [r for r in res.reasons if "넘기면" in r]
    assert len(bent) == 1 and bent[0].startswith("다음 설정을 함께 넘기면")   # 한 해가 함께 넘긴 묶음 — 한 문장
    assert bent[0].count(" · ") >= 1


def test_setting_bend_is_explained():
    # A 의 OFF 2개(3·4일)는 주휴라 못 바꾼다. 1일을 OFF 로 하면 OFF 3개 — 돌려줄 OFF 가 없어
    # OFF 수 상한을 1 넘겨야만 된다(인원 규칙 없음).
    ctx = make_ctx({"A": "DDOO"}, off_days=2, config={"not_one_night": False})
    ctx.frozen[("A", 2)] = ctx.frozen[("A", 3)] = "주휴·특수 휴무"
    res = propose(ctx, [SetCell("A", 0, "O")])
    assert res.status == "infeasible"
    assert res.blocking == ["OFF수"]
    assert any("상한을 1 넘김" in r for r in res.reasons)
    assert "다음 설정을 넘기면 20칸 이내로 가능합니다: A 개인 OFF 수 상한을 1 넘김" in res.reasons


def test_missing_cells_are_never_filled():
    # 3·4일 칸이 없는 사람(월 중 퇴사 등). 그 칸을 채우면 없던 근무가 생긴다 — 바꿀 수 없는 칸이어야 한다.
    from services.schedule_edit.context import _freeze_missing
    ctx = _two_nurse_ctx()
    ctx.raw["B"][2] = ctx.raw["B"][3] = None
    ctx.main["B"][2] = ctx.main["B"][3] = None
    _freeze_missing(ctx)
    assert ctx.frozen[("B", 2)] == "근무표에 칸 없음"
    assert propose(ctx, [SetCell("B", 3, "D")]).status == "invalid"
    res = propose(ctx, [SetCell("A", 0, "O")])
    for cand in res.candidates:
        assert all(ch.day < 2 or ch.nurse_id != "B" for ch in cand.changes)


def test_mutex_days_are_merged_per_pair():
    # 양방향 저장(a→b · b→a)의 날짜가 달라도 둘 다 걸려야 한다
    ctx = make_ctx({"A": "DDO", "B": "DOD"}, config={"not_one_night": False})
    ctx.mutex = {"A": {"partner_id": "B", "days": {0}}, "B": {"partner_id": "A", "days": {2}}}
    keys = {k for k in measure(ctx, {"A": list("DDD"), "B": list("DOD")}, build_rules(ctx))
            if k[0] == "상호배제"}
    assert keys == {("상호배제", "A", 0, "D"), ("상호배제", "A", 2, "D")}


def test_chain_never_reaches_days_before_the_request():
    # A 3일 OFF. 1·2일로 되돌려 맞추는 게 아니라 3일 뒤로만 연쇄해야 한다(앞 날짜는 이미 정해진 근무).
    ctx = _two_nurse_ctx()
    res = propose(ctx, [SetCell("A", 2, "O")])
    assert res.status == "ok"
    for cand in res.candidates:
        assert all(ch.day >= 2 for ch in cand.changes)
    assert res.candidates[0].changed_count == 4   # A3 근무·B3 OFF·B2 근무 + 요청 칸


def test_past_days_are_closed():
    ctx = _two_nurse_ctx()
    today = date(2026, 11, 3)                       # 1·2일은 이미 지났다
    res = propose(ctx, [SetCell("A", 1, "D")], today=today)
    assert res.status == "invalid" and "지난 날짜" in res.reasons[0]
    res = propose(ctx, [SetCell("A", 2, "O")], today=today)
    assert all(ch.day >= 2 for c in res.candidates for ch in c.changes)


@pytest.mark.parametrize("today,expected", [
    (date(2026, 10, 15), 0), (date(2026, 11, 1), 0), (date(2026, 11, 3), 2), (date(2026, 12, 5), 4),
])
def test_first_open_day(today, expected):
    from services.schedule_edit.requests import first_open_day
    assert first_open_day(_two_nurse_ctx(), today) == expected


def test_granted_wish_is_kept_even_if_another_nurse_is_equally_cheap():
    # B 와 C 는 똑같이 생겨 누가 A 1일 D 를 메워도 4칸이다. B 는 1일 OFF 를 원티드로 받아 둔 상태 →
    # B 의 원티드를 깨지 않는 C 쪽만 나와야 한다.
    ctx = make_ctx({"A": "DODO", "B": "ODOD", "C": "ODOD"}, cov_min={"D": 1, "E": 0, "N": 0},
                   cov_max={"D": 2}, off_days=2, config={"not_one_night": False})
    ctx.wishes[("B", 0)] = {"O"}
    res = propose(ctx, [SetCell("A", 0, "O")])
    assert res.status == "ok"
    for cand in res.candidates:
        assert not any(ch.nurse_id == "B" and ch.day == 0 for ch in cand.changes)


def test_head_nurse_request_overrides_the_wish_on_that_cell():
    ctx = _two_nurse_ctx()
    ctx.wishes[("A", 0)] = {"D"}          # A 는 1일 D 를 원해 받았지만, 수간호사가 그 칸을 OFF 로 바꾸라 한다
    res = propose(ctx, [SetCell("A", 0, "O")])
    assert res.status == "ok"


def test_unavoidable_quality_loss_is_reported_on_the_card():
    # 바뀌는 칸 수가 품질보다 먼저다: 2일 하나만 OFF 로 바꾸면(1칸) 'D O D' 고립 OFF 가 생긴다.
    # 1일도 OFF 로 바꾸면 고립이 아니지만 2칸이라 고르지 않는다 — 대신 카드에 적는다.
    ctx = make_ctx({"A": "DDDDO"}, config={"not_one_night": False, "banned_day_after_eve": True})
    res = propose(ctx, [AvoidCell("A", 1, frozenset({"D"}))])
    assert res.status == "ok"
    best = res.candidates[0]
    assert best.changed_count == 1
    assert best.quality == ["A 2일 근무 사이 OFF 하루"]


def test_measure_soft_patterns():
    from services.schedule_edit.rules import measure_soft
    ctx = make_ctx({"A": "NODDOD"})
    keys = {(k[0], k[2], k[3]) for k in measure_soft(ctx, ctx.main, build_rules(ctx))}
    assert ("패턴", 0, "N-O-D") in keys and ("고립OFF", 4, None) in keys


def test_avoid_cell_picks_any_other_code():
    ctx = make_ctx({"A": "DDOO", "B": "OODD"}, cov_min={"D": 0, "E": 0, "N": 0},
                   cov_max={"D": 2, "E": 2}, config={"not_one_night": False})
    res = propose(ctx, [AvoidCell("A", 1, frozenset({"D"}))])
    assert res.status == "ok"
    assert res.candidates[0].changed_count == 1
