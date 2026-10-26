"""두 병동 나이트 합(#6 · D33)은 상대 병동마다 그 병동 배정 날짜만 센다 — EI-13 12w(Codex 3차 P2).

간호사가 같은 달 두 병동(G2 1~4일 · G3 10~13일)으로 따로 파견되면 이 병동 차단일은 두 기간의 합집합이다. 상대 병동 근무표를 차단일
전체로 세면 배정이 바뀌기 전에 지어진 근무표에 남은 기간 밖 칸(G2 의 10~13일 · G3 의 1~4일)까지 세어 나이트 8 이 16 이 되고, 이
병동 몫(15 − 16 → 0)이 사라져 정확 횟수가 있는 간호사는 거짓 해 없음이 난다. 병동마다 그 병동 배정 날짜(같은 병동 배정이 여럿이면
합집합)만 센다 — 실제 파견 기간 안의 나이트는 그대로 센다(하드락 완화 없음).
"""

from __future__ import annotations

from datetime import date
from types import SimpleNamespace

import pytest

from .boundary_cases import CFG, BoundaryCase, _schedule_seed, d, n07_out, run_stage1, tail_cells
from .scenario_defs import _assignment, _rows
from .test_boundary_fixed_d32 import _run
from .ward import GROUP_ID, OTHER_GROUP_ID, stamp

G3 = "G3"
#: 1~4일 · 10~13일 나이트 8 개 — 두 상대 병동 근무표에 다 남아 있다. 한 병동의 실제 파견 날은 그 가운데 4 일뿐이다.
NIGHTS = {**tail_cells("NNNN", 4), **tail_cells("NNNN", 13)}


def _with_g3(cells: dict, exact: dict | None = None):
    """세 번째 병동 G3(11월 근무표 · 작성 중 · 칸 `cells`)과 이 병동 11월 월 한도 행(`exact` — 간호사 → 나이트 정확 횟수)을 더한다."""
    def mutate(ward) -> None:
        ward.other_groups.append(G3)
        ward.extras.append(_schedule_seed("g3nov0000001", G3, 11, "draft", cells, "g3n"))
        if exact:
            ward.extras.append(_rows("NurseMonthlyLimit", [
                {"nurse_id": nid, "group_id": GROUP_ID, "year": 2026, "month": 11, "n_exact": n, **stamp()}
                for nid, n in exact.items()]))
    return mutate


def _dispatches(name: str, second_ward: str, g2: dict) -> BoundaryCase:
    """N07 이 11/1~4 G2 · 11/10~13 `second_ward` 로 파견 간다(설정 X — 월 나이트 상한 15)."""
    return BoundaryCase(name=name, focus="N07", boundaries=(("after", 4), ("after", 13)),
                        assignments=[n07_out(d(11, 1), d(11, 4)),
                                     _assignment("N07", GROUP_ID, second_ward, d(11, 10), d(11, 13), "파견", target_grade=1,
                                                 target_shift_types=["D", "E", "N"])],
                        g2_nov={"N07": g2}, config=CFG["X"], g2_nurses=())


def _nurse(layer: dict, key: str, nid: str = "N07") -> dict:
    return next(n for n in layer[key] if str(n["nurse_id"]) == nid)


def test_two_ward_dispatches_count_each_wards_own_days(engine, engine_input_tables):
    """G2·G3 근무표에 N07 나이트가 16 칸 남아 있어도 각 병동 실제 파견 날의 나이트 8 만 센다 → 이 병동 몫 15 − 8 = 7(L0·L1)."""
    l0, l1, rec = _run(engine, _dispatches("n6-two-wards-X", G3, NIGHTS), mutate=_with_g3({"N07": NIGHTS}))
    assert l0 and l1, (rec.outcome, rec.path, rec.error, rec.http_detail)
    assert (_nurse(l0, "nurses_dict").get("n_max"), _nurse(l1, "nurses_data").get("n_max")) == (7, 7)


def test_two_ward_dispatches_no_false_infeasible(engine, engine_input_tables, monkeypatch):
    """N07 월 나이트 정확 횟수 3 — 16 으로 세면 몫 0 이라 [3, 0] 이 되어 해가 없다(거짓). 8 로 세면 몫 7 안이라 그대로 두고 해가 있다."""
    _l0, l1, rec = _run(engine, _dispatches("n6-two-wards-exact3-X", G3, NIGHTS), mutate=_with_g3({"N07": NIGHTS}, {"N07": 3}))
    assert l1, (rec.outcome, rec.path, rec.error, rec.http_detail)
    nurse = _nurse(l1, "nurses_data")
    st = run_stage1(monkeypatch, l1)
    assert st.status in ("OPTIMAL", "FEASIBLE"), (st.status, {k: nurse.get(k) for k in ("n_min", "n_exact", "n_max")})
    assert (nurse.get("n_exact"), nurse.get("n_max")) == (3, None), nurse


def test_same_ward_dispatches_are_unioned(engine, engine_input_tables):
    """같은 병동(G2) 배정 둘(1~4일 · 10~13일)은 합집합 — 두 기간 나이트 8 을 다 센다(몫 7 · 완화 없음). 기간 밖 20~23일(이 병동이
    배정하는 날) 칸은 세지 않는다."""
    g2 = {**NIGHTS, **tail_cells("NNNN", 23)}
    l0, l1, rec = _run(engine, _dispatches("n6-same-ward-twice-X", OTHER_GROUP_ID, g2))
    assert l0 and l1, (rec.outcome, rec.path, rec.error, rec.http_detail)
    assert (_nurse(l0, "nurses_dict").get("n_max"), _nurse(l1, "nurses_data").get("n_max")) == (7, 7)


# ── 순수 함수 단위(기대값은 손으로 적었다) ─────────────────────────────────────────────


def _a(src, dst, start: date, end: date | None, reason: str = "파견", nid: str = "N07") -> SimpleNamespace:
    return SimpleNamespace(nurse_id=nid, source_group_id=src, target_group_id=dst, start_date=start, end_date=None,
                           expected_end_date=end, reason=reason)


def _days(*spans) -> set[int]:
    """(첫날, 끝날)(1부터) 구간들 → 0부터 날 집합."""
    return {day - 1 for lo, hi in spans for day in range(lo, hi + 1)}


@pytest.mark.parametrize("assignments,blocked,ward,expected", [
    # 두 병동 따로 — 병동마다 그 병동 배정 날짜만(Codex 예)
    ([_a("G1", "G2", d(11, 1), d(11, 4)), _a("G1", "G3", d(11, 10), d(11, 13))], _days((1, 4), (10, 13)), "G2",
     _days((1, 4))),
    ([_a("G1", "G2", d(11, 1), d(11, 4)), _a("G1", "G3", d(11, 10), d(11, 13))], _days((1, 4), (10, 13)), "G3",
     _days((10, 13))),
    # 같은 병동 배정 둘 — 합집합
    ([_a("G1", "G2", d(11, 1), d(11, 4)), _a("G1", "G2", d(11, 10), d(11, 13))], _days((1, 4), (10, 13)), "G2",
     _days((1, 4), (10, 13))),
    # 합류(그 병동 → 이 병동 20~25일) — 이 병동에 없는 날 전부가 원 병동 날
    ([_a("G2", "G1", d(11, 20), d(11, 25))], _days((1, 19), (26, 30)), "G2", _days((1, 19), (26, 30))),
    # 휴직(상대 병동 없음)·다른 간호사 배정은 그 병동 날이 아니다
    ([_a("G1", "G2", d(11, 1), d(11, 4)), _a("G1", None, d(11, 21), None, "휴직"), _a("G1", "G2", d(11, 10), d(11, 13), nid="N08")],
     _days((1, 4), (21, 30)), "G2", _days((1, 4))),
    # 병동이동 출발(종료일 없음 → 월말까지) · 전월에 시작한 파견(월초부터)
    ([_a("G1", "G2", d(11, 16), None, "병동이동")], _days((16, 30)), "G2", _days((16, 30))),
    ([_a("G1", "G2", d(10, 25), d(11, 3))], _days((1, 3)), "G2", _days((1, 3))),
    # 차단일 밖(이 병동이 배정하는 날)은 빼서 두 번 세지 않는다
    ([_a("G1", "G2", d(11, 1), d(11, 4))], _days((1, 2)), "G2", _days((1, 2))),
])
def test_other_ward_days_table(assignments, blocked, ward, expected):
    """상대 병동 `ward` 에 있는 날(0부터) — 이 병동 → 그 병동 배정 기간의 합집합 · 그 병동 → 이 병동 합류면 차단일 전부 · 늘 차단일 안."""
    from services.roster_create_service import _other_ward_days

    assert _other_ward_days(assignments, "N07", ward, "G1", date(2026, 11, 1), 30, blocked) == expected
