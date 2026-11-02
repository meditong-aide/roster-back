"""후보가 화면에 새 위반을 만드는지 — 화면 판정과 엔진 기준이 갈리는 곳.

기대값은 화면 코드(useRosterValidation.ts)를 손으로 따라가 센 값이다.
"""

from __future__ import annotations

from services.schedule_edit.rules import build_rules, measure
from services.schedule_edit.screen_check import ScreenBaseline, screen_regressions
from services.schedule_edit.screen_rules import ScreenInputs, ShiftDef, screen_violations
from tests.schedule_edit.test_solver import make_ctx

SHIFTS = [
    ShiftDef(id=1, shift_id="D", default_shift="D", shift_gb="데이", type="근무", sequence=1),
    ShiftDef(id=2, shift_id="E", default_shift="E", shift_gb="이브닝", type="근무", sequence=2),
    ShiftDef(id=3, shift_id="N", default_shift="N", shift_gb="나이트", type="근무", sequence=3),
    ShiftDef(id=4, shift_id="O", default_shift="O", shift_gb="오프", type="휴무", sequence=4),
]


def _baseline(cells: dict, **inputs) -> ScreenBaseline:
    ids = {n: [{"D": 1, "E": 2, "N": 3, "O": 4}[c] for c in row] for n, row in cells.items()}
    rows = {n: list(row) for n, row in cells.items()}
    scr = ScreenInputs(year=2026, month=11, shifts=SHIFTS, use_mid=False, **inputs)
    return ScreenBaseline(inputs=scr, cells=rows, cell_ids=ids,
                          before=screen_violations(rows, ids, scr), id_of={"D": 1, "E": 2, "N": 3, "O": 4})


def test_screen_two_nights_rule_is_stricter_than_engine():
    # 2N 뒤 OFF 만 켜면 화면은 NNN 의 첫 NN 뒤(3일)가 N 이라 위반으로 본다. 엔진은 NNN 끝(3일) 뒤만 본다.
    ctx = make_ctx({"A": "NNNOOO"}, config={"two_offs_after_three_nig": False})
    kinds = {k[0] for k in measure(ctx, ctx.main, build_rules(ctx))}
    assert kinds == {"2N후OFF(화면)"}


def test_screen_two_nights_rule_off_when_three_nights_rule_on():
    ctx = make_ctx({"A": "NNNOOO"})
    assert measure(ctx, ctx.main, build_rules(ctx)) == {}


def test_regression_new_shortage_day():
    base = _baseline({"A": "DD", "B": "DO"}, manpower_monthly={"D": 2})
    days = {v.day for v in base.before}                 # 화면은 그 달 30일을 다 센다(3일부터는 칸 없음 = 0명)
    assert 1 in days and 0 not in days                  # 2일 D 1명(필요 2), 1일은 2명
    worse = screen_regressions(base, [("B", 0, "O")])   # 1일도 1명이 된다
    assert [(v.type, v.day) for v in worse] == [("manpower_shortage", 0)]


def test_regression_same_day_gets_worse():
    base = _baseline({"A": "DD", "B": "DO"}, manpower_monthly={"D": 2})
    worse = screen_regressions(base, [("A", 1, "O")])   # 2일 미달 1 → 2명
    assert [(v.type, v.day, v.actual) for v in worse] == [("manpower_shortage", 1, 0)]


def test_no_regression_when_screen_stays_same():
    base = _baseline({"A": "DD", "B": "DO"}, manpower_monthly={"D": 2})
    assert screen_regressions(base, [("B", 1, "E")]) == []   # E 는 검사 대상 아님
