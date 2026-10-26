"""확정 칸 조정 시험(EI-11 11w-3 · G2 · D32).

D32 — 확정 원티드·특수 근무(사용자가 직접 확정한 고정 칸)는 '그 칸만' 하드락 면제다. 엔진이 스스로 고르는 칸은 하드락을 지킨다
(못 받은 회복 OFF 는 다음 가능한 날로 옮긴다). 해가 없으면 완화하지 않고 실패로 알린다.

  - 엔진 시험: 11q 하네스(`boundary_cases.build_ward`·`run_stage1`)로 stage1 모델을 풀어 D32 판정기(`boundary_d32`)로 본다 —
    ① stage1 배정에 D32 위반 0(또는 INFEASIBLE) ② '경계만의 위반 이어짐'(확정 칸은 그대로 두고 엔진 칸만 바꾼 것)의 최소형 전부를
    모델에 강제하면 INFEASIBLE ③ 해가 있어야 하는 사례는 해가 있다. ②가 '완화 0'을 고정한다(위반 이어짐을 강제하면 해 없음).
  - 순수 함수 단위: `owed_offs`·`trail_state`·`reconcile_forward`/`reconcile_backward` 표 · `_fixed_main_by_nurse` 코드 정규화 ·
    D32 판정기 손 계산 표. 기대값은 손으로 적었다(생성 코드를 쓰지 않는다).
"""

from __future__ import annotations

import dataclasses
import itertools
from datetime import time
from functools import partial
from types import SimpleNamespace

import pytest
from services.engine_input.boundary_fixed import (
    BoundaryRules,
    night_extension_limit,
    owed_offs,
    reconcile_backward,
    reconcile_forward,
    trail_state,
)

from .boundary_cases import (
    CFG,
    NDAYS,
    OCT_DAYS,
    PREV_OCT,
    BoundaryCase,
    BoundaryGap,
    build_ward,
    d,
    l1_cells,
    main_code,
    n07_out,
    return_day1,
    run_stage1,
    tail_cells,
    x01_in,
)
from .boundary_d32 import boundary_only_d32, hard_violations_d32
from .boundary_d32 import minimal as minimal_d32
from .harness import run_with_output
from .scenario_defs import SCENARIOS, _rows
from .ward import seed_ward, shift_row, stamp

# ── 엔진 시험 하네스 ───────────────────────────────────────────────────────


def _active(l1: dict, nid: str) -> set[int]:
    blocked = {int(x) for x in ((l1["config_data"].get("blocked_by_nurse_id") or {}).get(nid) or ())}
    return set(range(NDAYS)) - blocked


def _scenario(case: BoundaryCase, extra_shifts=(), mutate=None):
    ward = build_ward(case)
    ward.shifts = list(ward.shifts) + list(extra_shifts)
    if mutate is not None:
        mutate(ward)
    base = SCENARIOS["S6"]
    return dataclasses.replace(base, name=f"W11-{case.name}", seed=partial(seed_ward, ward),
                               patches=base.patches + tuple(case.patches))


def _l1_fix(l1: dict | None) -> dict | None:
    """JSON 캡처가 튜플을 리스트로 바꾼 칸을 되돌린다 — 엔진이 set() 으로 읽는다(확정 O 칸이 있으면 TypeError)."""
    if l1:
        cfg = l1["config_data"]
        for key in ("off_exception_cells", "off_exception_vacation_cells"):
            if isinstance(cfg.get(key), list):
                cfg[key] = [tuple(x) for x in cfg[key]]
    return l1


def _run(engine, case, extra_shifts=(), mutate=None):
    rec = run_with_output(engine, _scenario(case, extra_shifts, mutate)).result
    l0 = rec.layers["L0"][0] if rec.layers.get("L0") else None
    l1 = rec.layers["L1"][0] if rec.layers.get("L1") else None
    return l0, _l1_fix(l1), rec


def _fixed_days(case) -> dict[int, str]:
    return {day - 1: main_code(code) for nid, day, code in case.fixed_wanted if nid == case.focus}


def _check_d32(case, engine, monkeypatch, extra_shifts=(), *, must_feasible=True, extra_probes=(), need_probes=True):
    """D32 — 확정 칸 자체만 면제. stage1 배정 위반 0(D32) · 최소 위반 이어짐(D32) 전부 INFEASIBLE · (기본) 해가 있다."""
    _l0, l1, rec = _run(engine, case, extra_shifts)
    assert l1, (rec.outcome, rec.error)
    cfg = l1["config_data"]
    fixed = _fixed_days(case)
    probes = minimal_d32(boundary_only_d32(case.outside(), case.boundaries, cfg, fixed,
                                           active=_active(l1, case.focus)))
    assert probes or not need_probes, "표본이 헛돈다(D32)"
    forced = [{case.focus: p} for p in probes] + [{case.focus: p} for p, _want in extra_probes]
    st = run_stage1(monkeypatch, l1, probes=forced)
    row = st.rows.get(case.focus, {})
    bad = hard_violations_d32(case.outside(), row, cfg, frozenset(fixed)) if st.status != "INFEASIBLE" else []
    n = len(probes)
    allowed = [f"{''.join(p.values())}@{min(p)}={s}" for p, s in zip(probes, st.probes[:n], strict=True) if s != "INFEASIBLE"]
    extra_bad = [f"{p}={s}(기대 {w})" for (p, w), s in zip(extra_probes, st.probes[n:], strict=True)
                 if (s == "INFEASIBLE") != (w == "INFEASIBLE")]
    infeasible_bad = must_feasible and st.status == "INFEASIBLE"
    if bad or allowed or extra_bad or infeasible_bad:
        raise BoundaryGap(f"{case.name}: stage1({st.status}) D32 위반 {bad} · 허용된 위반 이어짐 {allowed} · 탐침 {extra_bad} · "
                          f"배정 {''.join(row.get(i, '-') for i in range(NDAYS))}")
    return st


# ── 엔진 사례 ──────────────────────────────────────────────────────────────

ED_SHIFT = shift_row("ED", 9, "교육", None, "근무", None, 9, time(9, 0), time(18, 0), True)


def _prev(nid: str, tail: str) -> dict:
    return {**{k: v for k, v in PREV_OCT.items() if k != nid}, nid: tail_cells(tail, OCT_DAYS)}


def _month(name, tail, cfg, fixed, *, extra=()):
    case = BoundaryCase(name=name, focus="N01", boundaries=(("after", 0),), g1_oct=_prev("N01", tail), config=CFG[cfg],
                        fixed_wanted=fixed, g2_nurses=())
    return case, tuple(extra)


CFG["K4"] = {**CFG["Y"], "max_conseq_work": 4}   # 연속근무 상한 4(시험 전용 설정)

G2 = [
    # 월경계 — 전월 꼬리 × 이 달 확정 칸
    _month("g2-month-NN-D@1", "DEOONN", "Y", [("N01", 1, "D")]),                       # 1일 면제 · 2·3일 OFF
    _month("g2-month-NN-D@2", "DEOONN", "Y", [("N01", 2, "D")]),                       # 1일 O · 2일 면제 · 3일 OFF(옮김)
    _month("g2-month-DDDDD-D@1", "ODDDDD", "X", [("N01", 1, "D")]),                    # 2일 OFF(옮김)
    _month("g2-month-NNN-N@1", "OOONNN", "X", [("N01", 1, "N")]),                      # 예전 INFEASIBLE → 생성
    _month("g2-month-NN-ED@1", "DEOONN", "Y", [("N01", 1, "ED")], extra=(ED_SHIFT,)),  # 예전 INFEASIBLE → 생성(특수 근무 W)
    _month("g2-month-N-D@3", "OOOOON", "Y", [("N01", 3, "D")]),                        # 1일 N 금지(이어 붙이면 회복 칸이 확정 D)
    _month("g2-month-NN-O@1-D@2", "DEOONN", "Y", [("N01", 1, "O"), ("N01", 2, "D")]),  # 3일 OFF(하나만 옮김)
    _month("g2-month-N-N@1", "OOOOON", "Y", [("N01", 1, "N")]),                        # 2·3일 OFF
    _month("g2-month-ODDDDD-ED@1-W", "ODDDDD", "X", [("N01", 1, "ED")], extra=(ED_SHIFT,)),
    # 월중 경계 — 합류·복귀
    (BoundaryCase(name="g2-in-NN-D@join", focus="X01", boundaries=(("after", 19),), assignments=[x01_in(20, 25)],
                  g2_nov={"X01": tail_cells("OOOONN", 19)}, config=CFG["Y"], fixed_wanted=[("X01", 20, "D")]), ()),
    (BoundaryCase(name="g2-in-DDDDD-D@join", focus="X01", boundaries=(("after", 19),), assignments=[x01_in(20, 25)],
                  g2_nov={"X01": tail_cells("ODDDDD", 19)}, config=CFG["X"], fixed_wanted=[("X01", 20, "D")]), ()),
    (BoundaryCase(name="g2-early-in3-NN-D@3", focus="X01", boundaries=(("after", 2),), assignments=[x01_in(3, 25)],
                  g2_oct={"X01": tail_cells("OOOOON", OCT_DAYS)}, g2_nov={"X01": tail_cells("NN", 2)},
                  config=CFG["Y"], fixed_wanted=[("X01", 3, "D")]), ()),
    (BoundaryCase(name="g2-ret-NN-D@ret", focus="N07", boundaries=(("after", 14),), assignments=[n07_out(d(11, 10), d(11, 14))],
                  g2_nov={"N07": tail_cells("OOONN", 14)}, config=CFG["Y"], fixed_wanted=[("N07", 15, "D")],
                  g2_nurses=()), ()),
    # 출발 경계 앞 확정 칸
    (BoundaryCase(name="g2-depart-N@last", focus="N07", boundaries=(("before", 9),), assignments=[n07_out(d(11, 10), d(11, 14))],
                  g2_nov={"N07": {10: "D", 11: "D", 12: "O", 13: "D", 14: "D"}}, config=CFG["Y"],
                  fixed_wanted=[("N07", 9, "N")], g2_nurses=()), ()),
    (BoundaryCase(name="g2-depart-N@last-headNN-X", focus="N07", boundaries=(("before", 9),),
                  assignments=[n07_out(d(11, 10), d(11, 14))],
                  g2_nov={"N07": {10: "N", 11: "N", 12: "O", 13: "O", 14: "D"}}, config=CFG["X"],
                  fixed_wanted=[("N07", 9, "N")], g2_nurses=()), ()),
    # 11w-r(리뷰) — 이 달 확정 칸만으로 된 단독 N 에 바깥 꼬리용 상한을 걸면 1N 금지(W)에서 해가 있는 달이 해 없음이 된다
    _month("g2-month-W-OOODNN-N@3", "OOODNN", "W", [("N01", 3, "N")]),
    _month("g2-month-W-DNNDEO-O@2-N@3", "DNNDEO", "W", [("N01", 2, "O"), ("N01", 3, "N")]),
    # 11w-r(리뷰) — 앞 N 블록의 못 받은 회복 OFF 는 뒤따르는 확정 N 이 지우지 못한다(닫힌 모든 블록의 빚)
    _month("g2-month-X-OOENNN-D@1-O@2-N@3", "OOENNN", "X", [("N01", 1, "D"), ("N01", 2, "O"), ("N01", 3, "N")]),
    _month("g2-month-Z-DEODNN-D@1-N@2", "DEODNN", "Z", [("N01", 1, "D"), ("N01", 2, "N")]),
    # 11w-r(리뷰) — 꼬리가 NN 뒤 O 로 끝나(연속근무 0 · 회복 빚 1) 2일에 확정 D·E 가 있는 달: 빚 하나만 있어도 조정이 발동한다
    _month("g2-month-W-OONNNO-D@1-ED@2", "OONNNO", "W", [("N01", 1, "D"), ("N01", 2, "ED")], extra=(ED_SHIFT,)),
    _month("g2-month-K4-EENNNO-D@1", "EENNNO", "K4", [("N01", 1, "D")]),
]


@pytest.mark.parametrize("case,extra", G2, ids=[c.name for c, _e in G2])
def test_g2_fixed_cell_exempt_engine_cells_keep_hard_locks(case, extra, engine, engine_input_tables, monkeypatch):
    """확정 칸 자체만 면제 — 엔진 칸은 하드락을 지키고(위반 이어짐 강제 시 INFEASIBLE) 해가 있다."""
    _check_d32(case, engine, monkeypatch, extra)


def test_g2_moved_off_opens_after(engine, engine_input_tables, monkeypatch):
    """옮긴 OFF 는 딱 그만큼 — 전월 NN · 1일 확정 D(Y) → 2·3일 OFF 뒤 4일은 다시 연다(과잉 금지 헛돎 방지)."""
    case, extra = G2[0]
    _check_d32(case, engine, monkeypatch, extra,
               extra_probes=[({1: "D"}, "INFEASIBLE"), ({2: "D"}, "INFEASIBLE"), ({1: "O", 2: "O", 3: "D"}, "FEASIBLE")])


def test_g2_moved_off_kept_under_personal_limit(engine, engine_input_tables, monkeypatch):
    """개인 월 D 정확 횟수 22(월 OFF 8 과 함께 근무일을 꽉 채움) — 옛 코드는 전월 NN · 1일 확정 D 뒤 2·3일을 D 로 채워 #5 를
    어겼다(설계 실험). 새 코드는 2·3일 OFF(옮긴 OFF)를 지키고도 해가 있다(완화 없이)."""
    prev = {**PREV_OCT, "N01": tail_cells("OOOONN", OCT_DAYS)}
    case = BoundaryCase(name="g2-dexact22", focus="N01", boundaries=(("after", 0),), g1_oct=prev, config=CFG["Y"],
                        fixed_wanted=[("N01", 1, "D")], g2_nurses=())
    limit = {"nurse_id": "N01", "group_id": "G1", "year": 2026, "month": 11, "d_exact": 22, **stamp()}

    def add_limit(ward):
        ward.extras.append(_rows("NurseMonthlyLimit", [limit]))

    _l0, l1, rec = _run(engine, case, mutate=add_limit)
    assert l1, (rec.outcome, rec.error)
    st = run_stage1(monkeypatch, l1, probes=[{"N01": {1: "D"}}, {"N01": {2: "D"}}])
    row = st.rows.get("N01", {})
    assert st.status in ("OPTIMAL", "FEASIBLE") and (row.get(1), row.get(2)) == ("O", "O"), (st.status, row)
    assert st.probes == ["INFEASIBLE", "INFEASIBLE"], st.probes


def test_g2_month_boundary_offline_builder_matches_production(engine, engine_input_tables):
    """오프라인 합본(builder ⑤ 월경계)이 확정 칸 조정을 생성 경로와 같게 재현한다 — ⑤ 는 확정 칸 맵(`fixed_by_nurse`)을 넘겨야
    같은 값이 나온다. 이중 실행 대조 본 시험(`test_dual_build`)은 골든 시나리오만 돌아 경계 영향 구간의 확정 칸이 없다."""
    from .test_dual_build import _run_dual_build

    case, _extra = G2[0]
    res = _run_dual_build(engine, _scenario(case))
    l1 = res.captured_run.result.layers["L1"][0]["config_data"]
    forced = (l1.get("initial_constraints") or {}).get("forced_off", {}).get("N01")
    assert forced == [1, 2], f"표본이 헛돈다 — 월경계 조정(2·3일 강제 OFF)이 L1 에 없다: {forced}"
    assert (res.mismatches, res.load_report_errors) == ([], {}), (res.mismatches, res.load_report_errors)


# ── 순수 함수 단위 ─────────────────────────────────────────────────────────

R = BoundaryRules(K=5, L=3, two2=True, two3=True, ban_nd=True, ban_ed=True, ban_ne=True)
R_X = BoundaryRules(K=5, L=3, two2=False, two3=True, ban_nd=True, ban_ed=True, ban_ne=True)
ALL = list(range(30))


@pytest.mark.parametrize("seq,threshold,expected", [
    ("NN", 2, 0),         # 열린 블록(뒤에 칸 없음)은 0
    ("NND", 2, 2),
    ("NNDO", 2, 1),
    ("NNOO", 2, 0),
    ("NND-", 2, 0),       # 블록 뒤에 모름
    ("N", 2, 0),          # 블록이 T 보다 짧음
    ("NNND", 3, 2),
    ("NND", 3, 0),        # 길이 2 < T=3
    ("NNND", None, 0),    # 회복 규칙 꺼짐
    ("DD", 2, 0),         # N 없음
    ("NNDNND", 2, 2),     # 두 블록 모두 빚 2 — 최댓값
    ("NNNDNND", 3, 2),    # 앞 블록(길이 3 ≥ T)의 빚을 뒤 블록(길이 2 < T)이 지우지 못한다
    ("NNODNND", 2, 2),    # 뒤 블록 빚 2 · 앞 블록 빚 1 — 최댓값
    ("NNONNOO", 2, 0),    # 두 블록 모두 갚았다
])
def test_owed_offs_table(seq, threshold, expected):
    assert owed_offs(list(seq), threshold) == expected


@pytest.mark.parametrize("seq,outside_len,expected", [
    ("NNDE", 4, 0),       # 블록 바로 뒤 두 칸이 바깥(꼬리)의 비-O — 바깥이 먼저 어겼으니 유령 빚을 만들지 않는다
    ("NNDE", 2, 2),       # 같은 열인데 뒤 두 칸이 확정 칸(바깥 아님) — 빚 2
    ("NNOE", 4, 0),       # 바깥의 비-O 가 하나라도 끼면 그 블록은 0
    ("NNOO", 4, 0),
    ("NNOD", 3, 1),       # 바깥은 O 까지 · D 는 확정 칸 — 빚 1
])
def test_owed_offs_outside_violation_makes_no_phantom_debt(seq, outside_len, expected):
    assert owed_offs(list(seq), 2, outside_len) == expected


def test_trail_state_matches_calc_tail_metrics():
    """`trail_state` 는 `_calc_tail_metrics`(월경계 지표)와 같은 값 — 길이 ≤ 6 · {D,E,N,O,-} 전수 대조(W 는 이 모듈만 근무로 센다)."""
    from services.roster_create_service import _calc_tail_metrics

    for n in range(1, 7):
        for seq in itertools.product("DENO-", repeat=n):
            w, c, o, last = trail_state(seq)
            m = _calc_tail_metrics(list(seq))
            got = (w, c, o, last)
            want = (m["consecutive_work_tail"], m["consecutive_night_tail"], m["consecutive_off_tail"], m["last_day_shift"])
            assert got == want, (seq, got, want)


@pytest.mark.parametrize("tail,b,fixed,rules,forced,forbidden,windows", [
    # 전월 NN · 1일 확정 D(Y) → 1일 면제 · 2·3일(색인 1·2) 강제 OFF
    ("DEOONN", 0, {0: "D"}, R, {1, 2}, {}, []),
    # 같은 꼬리 · 2일 확정 D → 1일(색인 0)은 엔진 칸: 회복 OFF 를 먼저 받고(강제 OFF) 전이·N 이어 붙이기 금지, 3일 OFF(옮김)
    ("DEOONN", 0, {1: "D"}, R, {0, 2}, {0: {"N", "D", "E"}}, []),
    # ODDDDD(K=5) · 1일 확정 D → 연속 6 은 확정 칸까지 면제 · 2일 강제 OFF(옮김)
    ("ODDDDD", 0, {0: "D"}, R_X, {1}, {}, []),
    # 꼬리 연속근무가 정확히 K(5) — 엔진 칸 1일이 곧 연속 6 이라 강제 OFF(확정 칸은 영향 구간 안 4일)
    ("ODDDDD", 0, {3: "D"}, R_X, {0}, {}, []),
    # 전월 NNN · 1일 확정 N(블록 4일 — 확정 칸까지 면제) → 2일 N·전이 금지 · 2·3일 강제 OFF
    ("OOONNN", 0, {0: "N"}, R_X, {1, 2}, {1: {"N", "D", "E"}}, []),
    # OOOOON · 3일 확정 D(Y) → 1일 N 금지(이어 붙이면 회복 칸이 확정 D) · 전이 금지 · 연속근무 창
    ("OOOOON", 0, {2: "D"}, R, set(), {0: {"N", "D", "E"}}, [[0, 4]]),
    # 확정 O 는 OFF 로 친다: 전월 NN · 1일 확정 O · 2일 확정 D → 3일만 강제 OFF
    ("DEOONN", 0, {0: "O", 1: "D"}, R, {2}, {}, []),
])
def test_reconcile_forward_table(tail, b, fixed, rules, forced, forbidden, windows):
    got = reconcile_forward(list(tail), b, fixed, 30, ALL, rules)
    assert got is not None
    assert (got.forced, got.forbidden, got.windows) == (forced, forbidden, windows)


@pytest.mark.parametrize("tail,b,fixed,active", [
    ("OOOOOO", 0, {0: "D"}, ALL),            # 꼬리에 의무 없음
    ("DDDDNN", 0, {}, ALL),                  # 확정 칸 없음
    ("DDDDNN", 0, {10: "D"}, ALL),           # 확정 칸이 영향 구간 [0, 5] 밖
    ("DDDDNN", 0, {0: "O", 1: "O"}, ALL),    # 확정 칸이 전부 O
    ("DDDDNN", 0, {0: "D"}, []),             # 활동일 없음 → 건 칸 없음(빈 칸 묶음)
])
def test_reconcile_forward_none_or_empty(tail, b, fixed, active):
    got = reconcile_forward(list(tail), b, fixed, 30, active, R)
    assert got is None or (not got.forced and not got.forbidden and not got.windows)


def test_reconcile_forward_stops_at_end_of_active_days():
    """활동일이 끝나면(출발·차단) 멈춘다 — 그 뒤 의무는 이 병동이 못 건다."""
    got = reconcile_forward(list("DEOONN"), 0, {0: "D"}, 30, [0, 1], R)
    assert got is not None and got.forced == {1}


def test_reconcile_backward_prepends_fixed_cells_to_departure():
    """출발 직전 확정 N + 상대 첫 칸 N → 확정 칸 앞날(색인 8) N 금지 · 확정 칸이 없으면 None."""
    got = reconcile_backward(["N", "N", "O", "O", "D"], 10, {9: "N"}, ALL, R)
    assert got is not None and got.forbidden == {8: {"N"}}
    assert reconcile_backward(["N", "N"], 10, {5: "N"}, ALL, R) is None


def test_night_extension_limit_moved_to_boundary_fixed():
    from services.roster_create_service import _night_extension_limit
    assert _night_extension_limit is night_extension_limit
    assert night_extension_limit(1, 3, True, True) == 0 and night_extension_limit(1, 3, False, False) == 2


def _nurses(*ids):
    return [SimpleNamespace(nurse_id=i, sequence=n, experience=0) for n, i in enumerate(ids)]


def test_fixed_main_by_nurse_normalizes_codes():
    """D·N·OFF·주·휴가 type·근무 type 특수 → W · 모르는 코드 → W · 색인 → 간호사 · 칸 없는 간호사 키 없음."""
    from services.roster_create_service import _fixed_main_by_nurse
    cells = [
        {"nurse_index": 0, "day_index": 0, "shift": "D", "shift_type": "근무"},
        {"nurse_index": 0, "day_index": 1, "shift": "N", "shift_type": "근무"},
        {"nurse_index": 0, "day_index": 2, "shift": "OFF", "shift_type": None},
        {"nurse_index": 0, "day_index": 3, "shift": "주", "shift_type": None},
        {"nurse_index": 0, "day_index": 4, "shift": "AL", "shift_type": "휴가"},
        {"nurse_index": 1, "day_index": 0, "shift": "ED", "shift_type": "근무"},
        {"nurse_index": 1, "day_index": 1, "shift": "ZZ", "shift_type": "근무"},
    ]
    defs = [{"shift_id": "ED", "shift_gb": None, "default_shift": None, "type": "근무"},
            {"shift_id": "VA", "shift_gb": None, "default_shift": None, "type": "휴가"}]
    manage = [{"main_code": "D", "codes": ["D"]}, {"main_code": "N", "codes": ["N"]}]
    got = _fixed_main_by_nurse(cells, _nurses("A", "B"), manage, defs)
    assert got == {"A": {0: "D", 1: "N", 2: "O", 3: "O", 4: "O"}, "B": {0: "W", 1: "W"}}
    assert _fixed_main_by_nurse(None, _nurses("A"), manage, defs) == {}


# ── D32 판정기 손 계산 표 ──────────────────────────────────────────────────

CFG_Y = {**CFG["Y"]}


@pytest.mark.parametrize("outside,mine,given,expected_empty", [
    ({-2: "N", -1: "N"}, {0: "D"}, {0}, True),                       # 확정 칸 자체는 면제(N→D·#5)
    ({-2: "N", -1: "N"}, {0: "D"}, set(), False),                    # 같은 칸이 엔진 칸이면 위반
    ({-2: "N", -1: "N"}, {0: "D", 1: "D"}, {0}, False),              # 옮긴 OFF — 엔진 칸 1일이 D
    ({-2: "N", -1: "N"}, {0: "D", 1: "O", 2: "O", 3: "D"}, {0}, True),   # 옮긴 OFF 이행
    ({-2: "N", -1: "N"}, {0: "O", 1: "D"}, {1}, True),               # 엔진 O 뒤 확정 D — 확정 칸은 면제
])
def test_hard_violations_d32_table(outside, mine, given, expected_empty):
    got = hard_violations_d32(outside, mine, CFG_Y, frozenset(given))
    assert (got == []) is expected_empty, got


# ── 11w-r(리뷰) 보강 ──────────────────────────────────────────────────────


def test_reconcile_forward_in_month_fixed_only_n_block_gets_no_static_limit():
    """이 달 확정 N 만으로 된 열린 블록에는 바깥 꼬리용 N 상한·이어 붙이기 금지를 걸지 않는다(엔진이 이 달 블록을 스스로 센다) —
    1N 금지(W)에서 해가 있는 달이 해 없음이 되는 회귀. 꼬리가 N 에 닿아 있으면(바깥 N 블록) 예전대로 건다."""
    rules = BoundaryRules(K=5, L=2, two2=True, two3=True, ban_nd=True, ban_ed=True, ban_ne=True)
    # 꼬리 DDDOOD(바깥 N 없음) · 2일 확정 N → 3일은 금지칸 없음(전이 금지 D 만) · 강제 OFF 없음
    rc = reconcile_forward(list("DDDOOD"), 0, {1: "N"}, 30, ALL, rules)
    assert rc is not None and 2 not in rc.forced and "N" not in rc.forbidden.get(2, set()), rc
    # 꼬리 …NN 에 닿은 확정 N(블록이 꼬리와 이어짐) → 상한 3N 을 넘으니 다음 날 N 금지
    rc = reconcile_forward(list("OOONN"), 0, {0: "N"}, 30, ALL, dataclasses.replace(rules, L=3))
    assert rc is not None and "N" in rc.forbidden.get(1, set()), rc


def test_g4_day1_return_k6_tail_window_covers_all_six_days(engine, engine_input_tables, monkeypatch):
    """1일 복귀 · 상대 병동 전월 꼬리 DDDDDD · 연속근무 상한 6(K6) — 꼬리 창이 하루라도 짧으면 1일 근무를 허용한다(리뷰 시험 빈틈)."""
    from .test_boundary_hard_lock_engine import _check

    base = return_day1("DDDDDD", "X")
    case = dataclasses.replace(base, name="ret1-DDDDDD-K6", config={**CFG["X"], "max_conseq_work": 6})
    _check(case, engine, monkeypatch)


def test_g4_day1_return_other_ward_only_shift_code_builds_n_tail(engine, engine_input_tables):
    """1일 복귀 · 상대 병동 전월 꼬리가 그 병동에만 있는 세부 근무코드(NX = 나이트)일 때 N 꼬리로 읽는다 — 상대 병동 근무코드 보강을
    빼면 W 로 읽혀 N 이월 값이 사라진다(리뷰 시험 빈틈)."""
    nx = shift_row("NX", 90, "나이트2", None, "근무", "N", 90, time(21, 0), time(7, 0), True, group_id="G2")
    base = return_day1("OOOODD", "Y")
    case = dataclasses.replace(base, name="ret1-otherward-code-NX",
                               g2_oct={"N07": {26: "O", 27: "O", 28: "O", 29: "O", 30: "NX", 31: "NX"}})
    _l0, l1, rec = _run(engine, case, (nx,))
    assert l1, (rec.outcome, rec.error)
    cfg = l1["config_data"]
    assert (cfg.get("_return_source_map") or {}).get("N07", [None])[0] == "G2", cfg.get("_return_source_map")
    assert (cfg.get("prev_month_last_main") or {}).get("N07") == "N", cfg.get("prev_month_last_main")
    assert (cfg.get("prev_month_n_tail") or {}).get("N07", 0) == 2, cfg.get("prev_month_n_tail")


def test_g2_join_fixed_single_n_with_one_night_ban_stays_feasible(engine, engine_input_tables, monkeypatch):
    """월중 합류 · 합류일 확정 O · 다음 날 단독 확정 N · 1N 금지(W) — 해가 있는 달이 해 없음이 되지 않는다(11w-r · 리뷰 HIGH).
    이 달 확정 칸만으로 된 블록이라 위반 이어짐 탐침이 없다(need_probes=False) — FEASIBLE 만 본다."""
    case = BoundaryCase(name="g2-in-W-DDDOOD-O@join-N@+1", focus="X01", boundaries=(("after", 19),), assignments=[x01_in(20, 25)],
                        g2_nov={"X01": tail_cells("DDDOOD", 19)}, config=CFG["W"],
                        fixed_wanted=[("X01", 20, "O"), ("X01", 21, "N")])
    _check_d32(case, engine, monkeypatch, need_probes=False)


def test_g2_month_fixed_o_then_single_n_with_one_night_ban_stays_feasible(engine, engine_input_tables, monkeypatch):
    """월경계 · 1일 확정 O · 2일 단독 확정 N · 1N 금지(W) · 꼬리 DDDOOD — 해가 있다(11w-r · 리뷰 HIGH). 위반 이어짐 탐침이 없다."""
    case, extra = _month("g2-month-W-DDDOOD-O@1-N@2", "DDDOOD", "W", [("N01", 1, "O"), ("N01", 2, "N")])
    _check_d32(case, engine, monkeypatch, extra, need_probes=False)


LEAVE = shift_row("YN", 11, "연차", None, "휴가", "O", 11, None, None, True)


def test_mid_month_fixed_leave_code_counts_as_off_like_month_boundary(engine, engine_input_tables):
    """월중 합류 경로도 default_shift 가 O 인 휴가류 확정 원티드를 OFF 로 센다 — 특수 근무 맵만 쓰면 근무(W)로 잘못 세어 이미 갚은
    회복 OFF 를 22·23일에 또 강제한다(11w-r · 리뷰 LOW). 월경계 경로와 같은 근무코드 정의(그룹 shifts 전체)를 쓴다."""
    case = BoundaryCase(name="leave-mid", focus="X01", boundaries=(("after", 19),), assignments=[x01_in(20, 28)],
                        g2_nov={"X01": tail_cells("OOOONN", 19)}, config=CFG["Y"],
                        fixed_wanted=[("X01", 20, "YN"), ("X01", 21, "YN"), ("X01", 22, "D")])
    _l0, l1, rec = _run(engine, case, (LEAVE,))
    assert l1, (rec.outcome, rec.error)
    forced = l1_cells(l1, "X01").forced
    assert 22 not in forced and 23 not in forced, f"휴가를 근무로 세어 빚이 안 갚인 것으로 계산했다: {sorted(forced)}"
