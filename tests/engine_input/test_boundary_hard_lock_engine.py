"""실제 엔진 경계 시험 — 경계 전후 하드락이 엔진 모델에 닿는가(EI-11 11q · 점검 `ei11/q11/AUDIT.md` ③ · 리뷰 반영).

방법
  - 프로덕션 경로를 L2 직전까지 돌려 L1 을 받고(`boundary_cases.run_to_l1`), L1 을 실제 엔진(`CPSATBasicEngine.generate_roster`)에
    넣어 lex stage1 첫 풀이에서 멈춘다(`boundary_cases.run_stage1` — 엔진 7파일 무수정, 감싸기만). stage1 모델에도 하드락 8종이 다
    걸린다(커버리지만 최소화 대상) — 그래서 stage1 해를 보면 모델이 하드락을 지키는지 알 수 있다.
  - ① stage1 배정(첫 해): 초점 간호사의 '바깥 근무 + 이 병동 배정'에 하드락 #1~#7 위반이 없거나 풀이가 INFEASIBLE(해 없음 보고)이다.
    첫 해도 모델의 하드 제약을 모두 지키므로, 모델이 옳으면 해 탐색 운과 무관하게 통과한다.
  - ② 강제 탐침: '경계만의 위반 이어짐'의 최소형 **전부**(`boundary_cases.minimal(boundary_only(...))`)를 하나씩 그 모델 사본에
    `x == 1` 로 넣으면 INFEASIBLE 이어야 한다 — 운에 기대지 않는 결정적 판정이다(점검 1·2차 실행에서 같은 입력의 배정이 달랐다).
    규칙당 하나만 넣던 예전 판은 엔진이 L1 칸의 일부(예: E 금지만)를 버리면 첫 해가 우연히 그 칸을 밟을 때만 잡았다(리뷰 MEDIUM).
  - G5(경계 빌더 예외 삼킴)는 고장 주입으로 '생성이 멈추는가(fail-closed)'를 본다.
  - G2(확정 칸 우선)는 EI-11 11w-3(D32)이 닫았다 — 확정 원티드·특수 근무 칸만 면제하고 엔진이 고르는 칸은 하드락을 지킨다.
    D32 판정기(`boundary_d32`)와 사례는 `test_boundary_fixed_d32.py`.
  - G4(1일 복귀 — 전월 말일에 끝난 파견)는 EI-11 11w-1(D35)이 닫았다: 월경계가 그 간호사의 꼬리를 상대 병동 전월 근무표로 보충한다.
  - G3(출발 경계)는 EI-11 11w-2(D34)가 닫았다: 상대 병동 같은 달 근무표(마감본 우선, 없으면 작성 중)가 있으면 그 첫 칸들에 맞춰 이
    병동 마지막 날들에 전이 금지칸·연속근무 OFF 창·N 이어 붙이기 금지를 건다. 이 병동 끝 N 블록의 회복은 엔진이 막는다(출발 뒤 날은
    차단일이라 엔진 회복 식에서 'OFF 아님') — 출발 사례의 탐침이 그것도 본다.
"""

from __future__ import annotations

import dataclasses
from datetime import datetime
from types import SimpleNamespace

import pytest

from .boundary_cases import (
    CFG,
    DEPARTURES,
    EARLY,
    MONTH_START_IN,
    NDAYS,
    OCT_DAYS,
    PREV_OCT,
    PREV_TAILS,
    RETURN_DAY1,
    BoundaryCase,
    BoundaryGap,
    boundary_only,
    d,
    depart_n07,
    hard_violations,
    l1_cells,
    minimal,
    n07_out,
    night_cap_case,
    return_day1,
    run_stage1,
    run_to_l1,
    scenario_for,
    tail_cells,
    transfer_out,
    transfer_out_n08,
    x01_in,
)


def _active(l1: dict, nid: str) -> set[int]:
    blocked = {int(x) for x in ((l1["config_data"].get("blocked_by_nurse_id") or {}).get(nid) or ())}
    return set(range(NDAYS)) - blocked


def _check(case: BoundaryCase, engine, monkeypatch) -> None:
    """① stage1 배정 위반 0(또는 INFEASIBLE) · ② 최소 위반 이어짐 전부의 강제 탐침이 INFEASIBLE."""
    _l0, l1, rec = run_to_l1(engine, case)
    assert l1, (rec.outcome, rec.error)
    cfg = l1["config_data"]
    probes = minimal(boundary_only(case, cfg, active=_active(l1, case.focus)))
    assert probes, "표본이 헛돈다 — 경계만의 위반 이어짐이 없다"
    st = run_stage1(monkeypatch, l1, probes=[{case.focus: p} for p in probes])
    assert st.status in ("OPTIMAL", "FEASIBLE", "INFEASIBLE"), f"stage1 이 판정을 못 냈다({st.status})"
    row = st.rows.get(case.focus, {})
    bad = hard_violations(case.outside(), row, cfg) if st.status != "INFEASIBLE" else []
    allowed = [f"{''.join(p.values())}@{min(p)}={s}" for p, s in zip(probes, st.probes, strict=True) if s != "INFEASIBLE"]
    if bad or allowed:
        raise BoundaryGap(f"{case.name}: stage1({st.status}) 배정 위반 {bad} · 모델이 막지 않은 위반 이어짐 {allowed} · "
                          f"배정 {''.join(row.get(i, '-') for i in range(NDAYS))}")


def _in(tail: str, cfg: str, *, prev: bool = False, reason: str = "파견") -> BoundaryCase:
    end = 25 if reason == "파견" else 0
    return BoundaryCase(name=f"{'in' if reason == '파견' else 'tin'}-{tail}-{cfg}{'-prev' if prev else ''}", focus="X01",
                        boundaries=(("after", 19),), assignments=[x01_in(20, end, reason=reason)],
                        g2_nov={"X01": tail_cells(tail, 19)}, g1_oct=PREV_OCT if prev else None, config=CFG[cfg])


def _ret(tail: str, cfg: str, *, prev: bool = False) -> BoundaryCase:
    return BoundaryCase(name=f"ret-{tail}-{cfg}{'-prev' if prev else ''}", focus="N07", boundaries=(("after", 14),),
                        assignments=[n07_out(d(11, 10), d(11, 14))], g2_nov={"N07": tail_cells(tail, 14)},
                        g1_oct=PREV_OCT if prev else None, config=CFG[cfg], g2_nurses=())


def _month(nid: str, cfg: str) -> BoundaryCase:
    return BoundaryCase(name=f"month-{nid}-{PREV_TAILS[nid]}-{cfg}", focus=nid,
                        boundaries=(("after", 0),), g1_oct=PREV_OCT, config=CFG[cfg], g2_nurses=())


def _ret1_own_away() -> BoundaryCase:
    """1일 복귀 · 상대 병동 전월 근무표 없음 · 이 병동 전월 근무표에 파견 기간 칸(…NN)이 있는 달 — 원 병동이 파견 기간 근무를 만들어
    넘기던 옛 근무표. 그 칸을 꼬리로 쓴다(11q 까지 걸리던 월경계 칸을 지우지 않는다 — 완화 0 · EI-11 11w-1)."""
    base = return_day1("OOOONN", "Y", g2_prev=False)
    return dataclasses.replace(base, name="ret1-ownAway-OOOONN-Y-noG2prev",
                               g1_oct={**base.g1_oct, "N07": {19: "D", **tail_cells("OOOONN", OCT_DAYS)}})


#: 반드시 통과 — 파견 합류(월중) · 파견 복귀(월중) · 병동이동 합류(월중) × 꼬리 × 설정 × 전월 근무표 유무 + 월경계 대조
#: + 월초 근처 월중 경계(G7) · 월초(1일) 합류의 월경계 보충 · #6 상대 병동 나이트(리뷰 반영) · 1일 복귀(G4 · 11w-1)
#: + 출발 경계(G3 · 11w-2 — 11q 의 strict xfail 2건을 재료 그대로 옮겼다 + 상대 첫 칸·설정·짧은 파견·병동이동·원 병동 복귀).
CASES = [
    *[_in(t, c) for t, c in (("OOOONN", "X"), ("OOOONN", "Y"), ("OOOONN", "Z"), ("OOOOON", "X"), ("OOOOON", "Y"),
                             ("OOOOON", "Z"), ("OOONNN", "X"), ("ODDDDD", "Y"), ("OOOODE", "X"), ("OOODDN", "X"),
                             ("DDDDNN", "Y"))],
    *[_in(t, c, prev=True) for t, c in (("OOOONN", "X"), ("OOOOON", "Z"), ("ODDDDD", "X"), ("OOOODE", "Y"))],
    *[_ret(t, c) for t, c in (("OOONN", "X"), ("OOONN", "Y"), ("OOOON", "X"), ("OOOON", "Z"), ("ONNNO", "Y"),
                              ("ODDDD", "Y"), ("OOODE", "Y"))],
    *[_ret(t, c, prev=True) for t, c in (("OOONN", "X"), ("OOOON", "Y"), ("ODDDD", "X"))],
    *[_in(t, c, reason="병동이동") for t, c in (("OOOONN", "X"), ("OOOONN", "Y"), ("OOOOON", "Z"))],
    _in("OOOONN", "Y", prev=True, reason="병동이동"),
    *[_month(n, c) for n, c in (("N01", "X"), ("N01", "Y"), ("N02", "X"), ("N03", "Z"), ("N05", "Y"), ("N08", "Y"),
                                ("N10", "X"))],
    # 1N 금지(#7) 켬 — 경계일 단독 N(이 병동 배정)을 엔진이 막는가 · 경계 N 꼬리와 함께
    *[_in(t, "W") for t in ("OOOOOO", "OOOOON", "OOOONN")],
    *[_ret(t, "W") for t in ("OOOOO", "OOOON")],
    _in("OOOOOO", "W", reason="병동이동"),
    *[_month(n, "W") for n in ("N03", "N10")],
    *EARLY,
    *MONTH_START_IN,
    night_cap_case(),
    # 1일 복귀(G4 · EI-11 11w-1 · D35) — 11q 의 strict xfail 을 재료 그대로 옮겼다 + 꼬리·설정·근무표 유무 · 옛 근무표 칸
    BoundaryCase(name="G4-return-monthstart", focus="N07", boundaries=(("after", 0),),
                 assignments=[n07_out(d(10, 20), d(10, 31), status="completed")],
                 g2_oct={"N07": tail_cells("OOOONN", 31)},
                 g1_oct={**{k: v for k, v in PREV_OCT.items() if k != "N07"}, "N07": {19: "D"}},
                 config=CFG["Y"], g2_nurses=()),
    *RETURN_DAY1,
    _ret1_own_away(),
    BoundaryCase(name="G3-depart-N07", focus="N07", boundaries=(("before", 9),),
                 assignments=[n07_out(d(11, 10), d(11, 14))],
                 g2_nov={"N07": {10: "D", 11: "D", 12: "O", 13: "D", 14: "D"}}, config=CFG["Y"], g2_nurses=()),
    BoundaryCase(name="G3-transfer-out-N08", focus="N08", boundaries=(("before", 15),),
                 assignments=[transfer_out("N08", d(11, 16))], g2_nov={"N08": {16: "D", 17: "D", 18: "E"}},
                 config=CFG["Y"], g2_nurses=()),
    *DEPARTURES,
]


@pytest.mark.parametrize("case", CASES, ids=[c.name for c in CASES])
def test_boundary_hard_locks_reach_engine_model(case, engine, engine_input_tables, monkeypatch):
    _check(case, engine, monkeypatch)


# ── 1일 복귀(G4 · 11w-1) — 월경계 이월 값은 이은 꼬리에서 · 상대 병동 전월 근무표가 없으면 꼬리 없음(D35) ─────────────


def _composed_tail(case: BoundaryCase) -> str:
    """기대 꼬리 — 바깥 근무(파견 시작 앞은 이 병동 칸 · 그 뒤는 상대 병동 칸)의 전월 마지막 6일(모르는 날 '-'). 생성 코드를 안 쓴다."""
    outside = case.outside()
    return "".join(outside.get(day, "-") for day in range(-6, 0))


def _n_before_offs(tail: str) -> int:
    """꼬리 끝 OFF 앞 N 연속 수(월경계 `prev_month_n_tail` 의 뜻)."""
    body = tail.rstrip("O")
    return len(body) - len(body.rstrip("N"))


RETURN_CASES = [c for c in CASES if c.name.startswith(("G4-", "ret1-"))]


@pytest.mark.parametrize("case", RETURN_CASES, ids=[c.name for c in RETURN_CASES])
def test_day1_return_carries_composed_prev_tail(case, engine, engine_input_tables):
    """1일 복귀 간호사의 월경계 이월 값(마지막 근무 · 끝 N 연속)이 이은 꼬리에서 나온다 — 보충을 버리거나 이 병동 칸을 날짜로 못
    맞추면 엔진의 월경계 N 상한·회복·전이가 그 간호사에게 틀리게 걸린다."""
    _l0, l1, rec = run_to_l1(engine, case)
    assert l1, (rec.outcome, rec.error)
    cfg = l1["config_data"]
    tail = _composed_tail(case)
    assert tail.strip("-") and (cfg.get("_return_source_map") or {}).get("N07", [None])[0] == "G2", (tail, cfg.get("_return_source_map"))
    assert (cfg.get("prev_month_last_main") or {}).get("N07") == tail[-1], tail
    assert (cfg.get("prev_month_n_tail") or {}).get("N07", 0) == _n_before_offs(tail), tail


def test_day1_return_without_other_ward_prev_has_no_tail(engine, engine_input_tables):
    """D35 — 상대 병동 전월 근무표가 없으면 1일 복귀 간호사의 꼬리는 없다(이 병동 전월 근무표가 없을 때의 월경계 규칙과 같다 · 보수
    칸을 새로 만들지 않는다). 그 간호사에게 월경계 칸(강제 OFF · 1일 금지칸 · OFF 창)과 N 이월 값이 없다."""
    _l0, l1, rec = run_to_l1(engine, return_day1("OOOONN", "Y", g2_prev=False))
    assert l1, (rec.outcome, rec.error)
    cfg = l1["config_data"]
    assert (cfg.get("_return_source_map") or {}).get("N07") == ["G2", "2026-10-20"], "표본이 헛돈다 — 1일 복귀로 안 잡혔다"
    cells = l1_cells(l1, "N07")
    assert not cells.forced and not cells.forbidden.get(0) and not cells.windows, cells
    assert (cfg.get("prev_month_n_tail") or {}).get("N07", 0) == 0


def test_day1_return_offline_builder_matches_production(engine, engine_input_tables):
    """오프라인 합본(builder ⑤ 월경계)이 1일 복귀 보충을 생성 경로와 같게 재현한다 — ⑤ 는 precheck 앞에서 잡은 config(맵이 들어
    있다)로 같은 함수를 부른다. 이중 실행 대조 본 시험(`test_dual_build`)은 골든 시나리오만 돌아 1일 복귀가 없다."""
    from .test_dual_build import _run_dual_build

    res = _run_dual_build(engine, scenario_for(return_day1("OOOONN", "Y")))
    l1 = res.captured_run.result.layers["L1"][0]["config_data"]
    assert (l1.get("_return_source_map") or {}).get("N07") and (l1.get("prev_month_n_tail") or {}).get("N07") == 2, \
        "표본이 헛돈다 — 1일 복귀 꼬리가 L1 에 없다"
    assert (res.mismatches, res.load_report_errors) == ([], {}), (res.mismatches, res.load_report_errors)


#: G1 — 상대 병동 N 꼬리(c 개)에 이어 붙여도 되는 N 수(손으로 구한 값). L=3/2 · 2N→2OFF · 3N→2OFF.
#:   c+j ≤ L 이고, 합친 블록이 회복에 걸리면(c+j ≥ T) 이 병동 몫 j 도 T 이상이어야 엔진이 회복을 본다 — 1..j 가 다 되는 최대 j.
NIGHT_LIMIT = [
    (1, 3, False, True, 1), (2, 3, False, True, 0),       # X: 3N 허용 · 3N 뒤 2OFF
    (1, 3, True, True, 0), (2, 3, True, True, 0),         # Y: + 2N 뒤 2OFF
    (1, 2, True, True, 0),                                # Z: 나이트 2연속 상한
    (1, 3, False, False, 2), (2, 3, False, False, 1),     # 회복 규칙 없음 — 연속 상한만
    (1, 2, False, False, 1), (1, 2, False, True, 1),
]


@pytest.mark.parametrize("cons_n,cap,two2,two3,expected", NIGHT_LIMIT)
def test_night_extension_limit_table(cons_n, cap, two2, two3, expected):
    from services.roster_create_service import _night_extension_limit

    assert _night_extension_limit(cons_n, cap, two2, two3) == expected


# ── #6 — 상대 병동 나이트를 뺀 남은 몫까지는 연다(과잉 수정 헛돎 방지 · 리뷰 MEDIUM) ─────────────────


def test_monthly_night_cap_opens_exactly_the_room(engine, engine_input_tables, monkeypatch):
    """상대 병동 나이트 12 → 이 병동 남은 몫 3. 나이트 3개(경계 다음 날부터 NNN)는 모델이 허용하고 4개는 막는다.
    INFEASIBLE 만 보는 탐침은 상한을 0 으로 막아도 통과하므로 '남은 몫은 연다'를 따로 본다."""
    _l0, l1, rec = run_to_l1(engine, night_cap_case())
    assert l1, (rec.outcome, rec.error)
    st = run_stage1(monkeypatch, l1, probes=[{"X01": {20: "N", 21: "N", 22: "N"}},
                                             {"X01": {20: "N", 21: "N", 24: "N", 25: "N"}}])
    assert st.probes[0] in ("OPTIMAL", "FEASIBLE") and st.probes[1] == "INFEASIBLE", st.probes


@pytest.mark.parametrize("cap,used,active,n_max,exact,one,expected", [
    (15, 12, 11, None, None, False, {"n_max": 3}),
    (15, 12, 3, None, None, False, None),                    # 이 병동에서 남은 몫보다 많이 설 수 없다 — 건드리지 않는다
    (15, 12, 11, 2, None, False, {"n_max": 2}),             # 저장된 상한이 더 낮으면 그대로
    (15, 14, 11, None, None, True, {"n_max": 0}),           # 1N 금지 — 새 상한 1(면제)을 만들지 않는다
    (15, 14, 11, 1, None, True, {"n_max": 1}),              # 이미 n_max 1 로 저장된 면제는 그대로
    (15, 12, 11, None, 3, False, None),                      # 정확 횟수가 남은 몫 안이면 그대로
    (15, 12, 11, None, 5, False, {"n_min": 5, "n_exact": None, "n_max": 3}),   # 넘으면 해 없음(완화하지 않는다)
    (8, 12, 11, None, None, False, {"n_max": 0}),            # 설정 상한이 낮으면 그 값 기준(남은 몫 0)
    (20, 12, 11, None, None, False, {"n_max": 3}),           # 설정이 하드락 15 를 넘어도 15 기준
])
def test_cross_ward_night_cap_rules(cap, used, active, n_max, exact, one, expected):
    from services.roster_create_service import _apply_cross_ward_night_caps

    nurse = SimpleNamespace(nurse_id="X01", n_max=n_max, n_exact=exact, n_min=None)
    before = dict(vars(nurse))
    changed = _apply_cross_ward_night_caps([nurse], {"X01": used}, {"X01": active},
                                           {"max_nig_per_month": cap, "ban_single_night": one})
    if expected is None:
        assert (changed, vars(nurse)) == ({}, before)
    else:
        assert {k: vars(nurse)[k] for k in expected} == expected and changed == {"X01": vars(nurse)["n_max"]}


class _FakeShiftDb:
    """그 병동 근무코드(Shift)·근무 슬롯(ShiftManage — EI-13 12u-2) 조회만 답하는 가짜 세션 — 몇 번 불렸는지 센다."""

    def __init__(self, rows, slots=()):
        self.rows, self.slots, self.calls = rows, slots, 0

    def query(self, model):
        from db.models import ShiftManage

        self.calls += 1
        data = self.slots if model is ShiftManage else self.rows
        return SimpleNamespace(filter=lambda *a, **k: SimpleNamespace(all=lambda: list(data)))


def test_other_ward_nights_count_only_days_away():
    """상대 병동 나이트는 이 병동이 배정하지 않는 날(차단일)만 센다 — 상대 근무표가 파견 전에 지어져 이 병동이 배정할 날까지 칸이
    있어도 두 번 세지 않는다. 근무코드(NT 등)는 그 병동 근무코드 정의·근무 슬롯으로 병동마다 한 번 읽는다(조회 2 — 이 병동 맵으로
    다 풀려도 읽는다 · EI-13 12u·12u-2 · 같은 코드의 다른 뜻은 `test_boundary_other_ward_codes.py`)."""
    from services.roster_create_service import _count_other_ward_nights

    entries = [SimpleNamespace(work_date=datetime(2026, 11, day), shift_id=code)
               for day, code in ((1, "N"), (2, "N"), (3, "OFF"), (20, "N"), (21, "NT"))]
    db = _FakeShiftDb([SimpleNamespace(shift_id="NT", shift_gb="나이트", default_shift=None)])
    base = {"D": "D", "E": "E", "N": "N", "O": "O", "OFF": "O", "주": "O"}
    assert (_count_other_ward_nights(db, entries, set(range(19)), base, "G2", {}), db.calls) == (2, 2)
    maps: dict = {}
    assert (_count_other_ward_nights(db, entries, set(range(30)), base, "G2", maps), db.calls) == (4, 4)
    assert (_count_other_ward_nights(db, entries, set(range(30)), base, "G2", maps), db.calls) == (4, 4)


# ── 출발 경계(G3 · 11w-2) — 짧은 파견 앞뒤 · 월말 · 상대 근무표 고르기 ─────────────────────────────


def test_short_dispatch_joins_both_sides_for_consecutive_work(engine, engine_input_tables, monkeypatch):
    """2일 파견(11/10~11 · 상대 D,D) 앞뒤의 이 병동 근무가 이어지면 연속근무(#1)를 넘는다 — 한 방향씩만 이어 보는 탐침(`boundary_only`)은
    이 이어짐을 못 만들어 따로 강제한다. 파견 앞 3일 + 뒤 1일 · 앞 2일 + 뒤 2일 · 앞 1일 + 뒤 3일(모두 6연속)은 막고, 사이를 OFF 로
    끊은 배정과 5연속은 연다(과잉 금지 헛돎 방지)."""
    _l0, l1, rec = run_to_l1(engine, depart_n07("DD", "X", end_day=11, after=True))
    assert l1, (rec.outcome, rec.error)
    blocked = [{6: "D", 7: "D", 8: "D", 11: "D"}, {7: "D", 8: "D", 11: "D", 12: "D"}, {8: "D", 11: "D", 12: "D", 13: "D"}]
    opened = [{6: "D", 7: "D", 8: "O", 11: "D", 12: "D"}, {7: "D", 8: "D", 11: "D", 12: "O"}]
    st = run_stage1(monkeypatch, l1, probes=[{"N07": p} for p in blocked + opened])
    assert st.probes[:3] == ["INFEASIBLE"] * 3, st.probes
    assert all(s in ("OPTIMAL", "FEASIBLE") for s in st.probes[3:]), st.probes


def test_departure_near_month_end_opens_room(engine, engine_input_tables, monkeypatch):
    """11/28 병동이동(상대 첫 칸 D,D,D) — 떠나기 전 24~26일 D(상대와 6연속)는 막고, 25·26일 D(5연속)는 연다. 다음 달 칸은 아직 없어
    끊김이라(규약 — G7·D35 와 같다) 월말을 넘는 창은 걸지 않는다(걸면 26일 OFF 를 강제해 5연속까지 막는다)."""
    _l0, l1, rec = run_to_l1(engine, transfer_out_n08(28, "DDD", "X"))
    assert l1, (rec.outcome, rec.error)
    st = run_stage1(monkeypatch, l1, probes=[{"N08": {23: "O", 24: "D", 25: "D", 26: "D"}},
                                             {"N08": {24: "O", 25: "D", 26: "D"}}])
    assert st.probes[0] == "INFEASIBLE" and st.probes[1] in ("OPTIMAL", "FEASIBLE"), st.probes


@pytest.mark.parametrize("which", ["issued", "draft", "none"])
def test_departure_reads_issued_then_draft(which, engine, engine_input_tables):
    """상대 병동 같은 달 근무표 고르기(D34) — 마감본과 작성 중이 같이 있으면 마감본, 작성 중만 있으면 작성 중, 없으면 출발 칸 0.
    두 벌의 첫 칸을 다르게 둬(마감본 D,D,O → 9일 E·N 금지 · 작성 중 N,N,O → 9일 N 금지만) 어느 쪽을 읽었는지 L1 칸으로 본다."""
    draft = depart_n07("NNO", "Y")
    case = {"issued": dataclasses.replace(draft, g2_nov_issued={"N07": {10: "D", 11: "D", 12: "O"}}),
            "draft": draft, "none": dataclasses.replace(draft, g2_nov={})}[which]
    _l0, l1, rec = run_to_l1(engine, case)
    assert l1, (rec.outcome, rec.error)
    cells = l1_cells(l1, "N07")
    want = {"issued": ({"E", "N"}, True), "draft": ({"N"}, True), "none": (set(), False)}[which]
    assert (cells.forbidden.get(8, set()), (5, 8) in cells.windows) == want, cells


# ── G5 — 경계 빌더 실패는 생성 실패로(fail-closed) ─────────────────────────────────


def _raise_in(name: str):
    def patch(mp):
        import services.roster_create_service as rcs

        def boom(*_args, **_kwargs):
            raise RuntimeError(f"[11q 주입] {name} 실패")
        mp.setattr(rcs, name, boom)
    return patch


FAULTS = [
    BoundaryCase(name="fault-crossmonth", focus="N01", boundaries=(("after", 0),), g1_oct=PREV_OCT, config=CFG["Y"],
                 g2_nurses=(), patches=(_raise_in("_get_last_days_map"),)),
    # 월중 경계 빌더는 상대 병동 칸을 `_load_other_ward_cells` 한 곳에서 읽는다(EI-11 11w-2 — 예전 `_get_boundary_tail` 은 이 빌더가 안 쓴다)
    BoundaryCase(name="fault-midmonth", focus="X01", boundaries=(("after", 19),), assignments=[x01_in(20, 25)],
                 g2_nov={"X01": tail_cells("OOOONN", 19)}, config=CFG["Y"], patches=(_raise_in("_load_other_ward_cells"),)),
    # #6 — 상대 병동 나이트를 못 세면 상한 없이 가지 않는다(리뷰 반영)
    BoundaryCase(name="fault-other-ward-nights", focus="X01", boundaries=(("after", 19),), assignments=[x01_in(20, 30)],
                 g2_nov={"X01": tail_cells("NNNOONNNOONNNOONNNO", 19)}, config=CFG["X"],
                 patches=(_raise_in("_count_other_ward_nights"),)),
]


@pytest.mark.parametrize("case", FAULTS, ids=[c.name for c in FAULTS])
def test_boundary_builder_failure_stops_generation(case, engine, engine_input_tables):
    """경계 빌더가 실패하면 경계 하드락 없이 엔진에 가지 않는다(예전엔 삼키고 생성 — 점검 G5)."""
    _l0, l1, rec = run_to_l1(engine, case)
    assert (rec.outcome, l1) == ("exception", None), rec.outcome
    assert "[11q 주입]" in (rec.error or "")


class _ShiftQueryFails:
    """`Shift` 조회만 실패하는 가짜 세션 — 나머지 조회는 빈 결과(근무표 1벌만 있는 것처럼)."""

    def __init__(self, shift_model, schedule_model):
        self._shift, self._schedule = shift_model, schedule_model

    def query(self, model):
        if model is self._shift:
            raise RuntimeError("[11q 주입] Shift 조회 실패")
        row = SimpleNamespace(schedule_id="s1") if model is self._schedule else None
        return SimpleNamespace(filter=lambda *a, **k: self._q(row), order_by=lambda *a, **k: self._q(row))

    def _q(self, row):
        return SimpleNamespace(filter=lambda *a, **k: self._q(row), order_by=lambda *a, **k: self._q(row),
                               first=lambda: row, all=lambda: [])


def test_cross_month_code_supplement_failure_is_raised():
    """월경계 빌더의 세부 근무코드 보강(Shift 조회) 실패를 삼키면 세부 코드가 '미인식 = 연속 끊김'으로 읽혀 꼬리 하드락이 빠진다."""
    from db.models import Schedule, Shift
    from services.roster_create_service import build_cross_month_constraints

    req = SimpleNamespace(year=2026, month=11)
    user = SimpleNamespace(group_id="G1", office_id="O1")
    with pytest.raises(RuntimeError, match="11q 주입"):
        build_cross_month_constraints(_ShiftQueryFails(Shift, Schedule), req, user, [], {"allow_three_consecutive_nights": True}, ["N01"])


@pytest.mark.parametrize("direction", ["inbound", "return"])
def test_mid_month_code_supplement_failure_is_raised(direction):
    """월중 경계 빌더의 상대 병동 코드 보강 실패도 같다(삼키지 않는다)."""
    from db.models import Schedule, Shift
    from services.roster_create_service import build_mid_month_boundary_constraints

    a = SimpleNamespace(nurse_id="X01", source_group_id="G2", target_group_id="G2", start_date=d(11, 20),
                        end_date=d(11, 14), expected_end_date=d(11, 14), reason="파견")
    inbound, outbound = ([a], []) if direction == "inbound" else ([], [a])
    with pytest.raises(RuntimeError, match="11q 주입"):
        build_mid_month_boundary_constraints(_ShiftQueryFails(Shift, Schedule), inbound, "G1", 2026, 11,
                                             {"allow_three_consecutive_nights": True}, {}, outbound_assignments=outbound)
