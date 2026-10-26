"""상대 병동 근무코드는 그 병동의 정의로 읽는다 — EI-13 12u(Codex 1차 리뷰 P1·P2).

P1 — 같은 근무코드가 병동마다 뜻이 다를 수 있다(여기서 'A' 는 이 병동 데이 · 상대 병동 나이트, 'B' 는 그 반대). 상대 병동 근무표
     칸을 이 병동 맵으로 먼저 읽으면 상대 병동 나이트를 데이로 세어 두 병동 합 월 나이트 상한(#6 · D33)이 안 걸리고, 경계 칸
     (합류·복귀·출발·1일 복귀·1일 합류)도 다른 근무로 걸린다. 그 병동 근무코드 정의(shift_gb·default_shift)가 이 병동 맵보다 먼저다.
P2 — 상대 병동에만 있는 특수 근무코드(shift_gb·default_shift 없음)는 월경계와 같은 type 폴백으로 읽는다 — '근무'(여기서 '교' =
     교육)는 근무라 연속근무를 끊지 않고, '휴가'·'공가'(여기서 '휴')는 OFF 다.
12u-2 — 상대 병동 근무 슬롯(ShiftManage — 1=D·2=E·3=N·5=M)에만 정의된 코드(여기서 'S' = 구분·기본코드 없는 근무 행 · 'T' = 근무코드
     행 없이 슬롯에만)는 그 슬롯 뜻으로 읽는다 — 두 병동 합 월 나이트 상한(#6)과 경계 칸 모두.
12v(D52) — 상대 병동 코드는 그 병동이 자기 근무표를 만들 때와 같은 순서로 읽는다: 슬롯 main_code(`_build_code_to_main_map` — 그 병동
     자신의 함수)가 구분·기본코드보다 먼저다(여기서 'G' = 구분 데이·나이트 슬롯 → N · 'H' = 구분 나이트·데이 슬롯 → D). 하드락이 풀리는
     쪽(근무를 휴무로 · 세부 근무를 WK 로)은 지금 규칙을 유지한다(보수 예외).

판정은 쌍둥이 사례(상대 병동 코드를 손으로 적은 그 병동 뜻 `MEANING` 으로 바꾼 것)로 한다 — 위반 판정기·위반 이어짐 탐침
(`boundary_cases`)은 생성 코드를 쓰지 않는다. 엔진 시험 방법은 `test_boundary_hard_lock_engine.py` 와 같다(L1 → stage1 배정 위반 0
또는 INFEASIBLE · 최소 위반 이어짐 전부 강제 탐침 INFEASIBLE).
"""

from __future__ import annotations

import dataclasses
from datetime import datetime, time
from types import SimpleNamespace

import pytest

from .boundary_cases import (
    CFG,
    NDAYS,
    NIGHT_CAP_G2,
    BoundaryCase,
    BoundaryGap,
    boundary_only,
    d,
    depart_n07,
    early_in,
    hard_violations,
    l1_cells,
    minimal,
    n07_out,
    return_day1,
    run_stage1,
    tail_cells,
    x01_in,
)
from .test_boundary_fixed_d32 import _run
from .test_boundary_hard_lock_engine import _active, _FakeShiftDb
from .ward import OTHER_GROUP_ID, shift_manage_row, shift_row

#: 상대 병동(G2) 근무코드의 뜻 — 손으로 적는다(판정기·쌍둥이 기준). 'W' 는 판정기에서 근무(D·E·N·M 아님)다.
MEANING = {"A": "N", "B": "D", "교": "W", "휴": "O", "S": "N", "T": "N", "G": "N", "H": "D"}

#: 근무코드 정의 — 이 병동 G1: A=데이 · B=나이트 / 상대 병동 G2: A=나이트 · B=데이 · 교=교육(type 근무 · 구분·기본코드 없음) ·
#: 휴=휴가(type 휴가 · 구분·기본코드 없음). 교·휴 는 G2 에만 있다.
SHIFTS = (
    shift_row("A", 81, "A데이", "데이", "근무", "D", 81, time(7, 0), time(15, 0)),
    shift_row("B", 82, "B나이트", "나이트", "근무", "N", 82, time(22, 0), time(7, 0)),
    shift_row("A", 83, "A나이트", "나이트", "근무", "N", 83, time(22, 0), time(7, 0), group_id=OTHER_GROUP_ID),
    shift_row("B", 84, "B데이", "데이", "근무", "D", 84, time(7, 0), time(15, 0), group_id=OTHER_GROUP_ID),
    shift_row("교", 85, "교육", None, "근무", None, 85, time(9, 0), time(18, 0), group_id=OTHER_GROUP_ID),
    shift_row("휴", 86, "휴가", None, "휴가", None, 86, None, None, False, group_id=OTHER_GROUP_ID),
)


def _local_slots(ward) -> None:
    """이 병동 근무 슬롯에 A(데이 슬롯)·B(나이트 슬롯)를 더한다 — 이 병동 맵(ShiftManage)이 A=D · B=N 으로 읽는다."""
    for row in ward.shift_manage:
        row["codes"] = list(row["codes"]) + {"D": ["A"], "N": ["B"]}.get(row["main_code"], [])


#: 12u-2 — 상대 병동 G2 의 S(구분·기본코드 없는 근무 행 — type 폴백만으로는 WK). T 는 G2 근무코드 행이 없다(슬롯에만).
SLOT_SHIFTS = (*SHIFTS, shift_row("S", 87, "S근무", None, "근무", None, 87, time(22, 0), time(7, 0), group_id=OTHER_GROUP_ID))


def _g2_slots(ward) -> None:
    """이 병동 슬롯(`_local_slots`) + 상대 병동 G2 근무 슬롯(RN 행 — AN 병동도 'RN' 관례): 1=D · 2=E · 3=N(N·S·T)."""
    _local_slots(ward)
    ward.shift_manage += [{**shift_manage_row(slot, main, codes, 2), "group_id": OTHER_GROUP_ID}
                          for slot, main, codes in ((1, "D", ["D"]), (2, "E", ["E"]), (3, "N", ["N", "S", "T"]))]


def _twin(case: BoundaryCase) -> BoundaryCase:
    """상대 병동 코드를 그 병동 뜻(`MEANING`)으로 바꾼 쌍둥이 — 판정기 기준이자 '같은 경계 칸' 대조 기준."""
    def tr(cells):
        return {nid: {day: MEANING.get(code, code) for day, code in days.items()} for nid, days in (cells or {}).items()}
    return dataclasses.replace(case, name=f"{case.name}-twin", g2_nov=tr(case.g2_nov), g2_oct=tr(case.g2_oct),
                               g2_nov_issued=tr(case.g2_nov_issued))


def _check_coded(case: BoundaryCase, engine, monkeypatch, shifts=SHIFTS, mutate=_local_slots) -> None:
    """상대 병동 코드 사례를 생성 경로로 돌린 L1 을 쌍둥이(그 병동 뜻) 기준으로 판정한다 — stage1 배정 위반 0(또는 INFEASIBLE) ·
    최소 위반 이어짐 전부의 강제 탐침 INFEASIBLE(`test_boundary_hard_lock_engine._check` 와 같은 판정)."""
    twin = _twin(case)
    _l0, l1, rec = _run(engine, case, shifts, mutate)
    assert l1, (rec.outcome, rec.error)
    cfg = l1["config_data"]
    probes = minimal(boundary_only(twin, cfg, active=_active(l1, case.focus)))
    assert probes, "표본이 헛돈다 — 경계만의 위반 이어짐이 없다"
    st = run_stage1(monkeypatch, l1, probes=[{case.focus: p} for p in probes])
    assert st.status in ("OPTIMAL", "FEASIBLE", "INFEASIBLE"), f"stage1 이 판정을 못 냈다({st.status})"
    row = st.rows.get(case.focus, {})
    bad = hard_violations(twin.outside(), row, cfg) if st.status != "INFEASIBLE" else []
    allowed = [f"{''.join(p.values())}@{min(p)}={s}" for p, s in zip(probes, st.probes, strict=True) if s != "INFEASIBLE"]
    if bad or allowed:
        raise BoundaryGap(f"{case.name}: stage1({st.status}) 배정 위반 {bad} · 모델이 막지 않은 위반 이어짐 {allowed} · "
                          f"배정 {''.join(row.get(i, '-') for i in range(NDAYS))}")


def _join(tail: str, cfg: str) -> BoundaryCase:
    """X01(G2 소속)이 11/20~25 G1 로 파견 온다 — G2 11월 꼬리 `tail`(19일에 끝남) 뒤 합류."""
    return BoundaryCase(name=f"cx-in-{tail}-{cfg}", focus="X01", boundaries=(("after", 19),), assignments=[x01_in(20, 25)],
                        g2_nov={"X01": tail_cells(tail, 19)}, config=CFG[cfg])


def _ret(tail: str, cfg: str) -> BoundaryCase:
    """N07 이 11/10~14 G2 파견 뒤 15일 복귀 — G2 11월 꼬리 `tail`(14일에 끝남)."""
    return BoundaryCase(name=f"cx-ret-{tail}-{cfg}", focus="N07", boundaries=(("after", 14),),
                        assignments=[n07_out(d(11, 10), d(11, 14))], g2_nov={"N07": tail_cells(tail, 14)}, config=CFG[cfg],
                        g2_nurses=())


# ── P1 — 그 병동 정의가 이 병동 맵보다 먼저(같은 코드 · 다른 뜻) ─────────────────────────────────


P1_CASES = [
    _join("OOOOAA", "Y"),                    # 합류 — 상대 꼬리 NN(이 병동 맵으로는 DD)
    _ret("OOOAA", "Y"),                      # 월중 복귀 — 상대 꼬리 NN
    depart_n07("BBOOD", "Y"),                # 출발 — 상대 첫 칸 DD(이 병동 맵으로는 NN · E→D 금지가 빠진다)
    return_day1("OOOOAA", "Y"),              # 1일 복귀 — 상대 병동 전월 꼬리 NN(`_prev_month_cells_of`)
    early_in(1, "OOOOAA", "", "Y"),          # 1일 합류 — 상대 병동 전월 꼬리 NN(월경계 보충 `_inbound_source_map`)
]


@pytest.mark.parametrize("case", P1_CASES, ids=[c.name for c in P1_CASES])
def test_p1_boundary_reads_other_ward_meaning_engine(case, engine, engine_input_tables, monkeypatch):
    """상대 병동 칸을 그 병동 뜻으로 읽어 경계 하드락(#1·#2·#3·#5)이 엔진 모델에 닿는다."""
    _check_coded(case, engine, monkeypatch)


TWIN_CASES = [*P1_CASES, depart_n07("AAOOD", "Y")]   # 마지막 — 출발 · 상대 첫 칸 NN(이 병동 맵으로는 DD)


def _assert_cells_equal_twin(case: BoundaryCase, engine, shifts=SHIFTS, mutate=_local_slots) -> None:
    """상대 병동 코드 칸이 그 병동 뜻으로 적은 쌍둥이와 같은 경계 칸을 낸다 — 강제 OFF·금지칸·OFF 창·개인 월 N 상한 · 월경계 이월 값."""
    _l0, l1, rec = _run(engine, case, shifts, mutate)
    _t0, t1, trec = _run(engine, _twin(case), shifts, mutate)
    assert l1 and t1, (rec.outcome, rec.error, trec.outcome, trec.error)
    assert l1_cells(l1, case.focus) == l1_cells(t1, case.focus)
    for key in ("prev_month_last_main", "prev_month_n_tail", "prev_month_n_offs_after"):
        got, want = (l1["config_data"].get(key) or {}).get(case.focus), (t1["config_data"].get(key) or {}).get(case.focus)
        assert got == want, (key, got, want)


@pytest.mark.parametrize("case", TWIN_CASES, ids=[c.name for c in TWIN_CASES])
def test_p1_boundary_cells_equal_twin(case, engine, engine_input_tables):
    """상대 병동 코드 칸이 쌍둥이와 같은 경계 칸(출발 'AAOOD' 는 이 병동 맵으로 읽어도 하드락이 더 빡빡해질 뿐이라 엔진 판정으로는
    안 잡히고 칸 대조로만 잡힌다)."""
    _assert_cells_equal_twin(case, engine)


def _check_night_cap(case: BoundaryCase, engine, monkeypatch, shifts=SHIFTS, mutate=_local_slots) -> None:
    """X01 의 상대 병동 나이트 12 → 이 병동 몫 월 N 상한 15 − 12 = 3(L0·L1 같은 값) · 남은 몫(NNN)은 열고 4개는 막는다."""
    l0, l1, rec = _run(engine, case, shifts, mutate)
    assert l0 and l1, (rec.outcome, rec.error)
    room = 15 - NIGHT_CAP_G2.count("N")
    n0 = {str(n["nurse_id"]): n for n in l0["nurses_dict"]}
    n1 = {str(n["nurse_id"]): n for n in l1["nurses_data"]}
    assert (n0["X01"].get("n_max"), n1["X01"].get("n_max")) == (room, room)
    st = run_stage1(monkeypatch, l1, probes=[{"X01": {20: "N", 21: "N", 22: "N"}},
                                             {"X01": {20: "N", 21: "N", 24: "N", 25: "N"}}])
    assert st.probes[0] in ("OPTIMAL", "FEASIBLE") and st.probes[1] == "INFEASIBLE", st.probes


def _night_cap_coded(name: str, pattern: str) -> BoundaryCase:
    """X01 이 상대 병동에서 11/1~19 `pattern`(나이트 12 · 3N 뒤 2OFF 지킴)을 서고 20~30일 G1 로 온다(#6 재료)."""
    return BoundaryCase(name=name, focus="X01", boundaries=(("after", 19),), assignments=[x01_in(20, 30)],
                        g2_nov={"X01": tail_cells(pattern, 19)}, config=CFG["X"])


def test_p1_monthly_night_cap_reads_other_ward_meaning(engine, engine_input_tables, monkeypatch):
    """X01 이 상대 병동에서 'A'(그 병동 나이트 · 이 병동 데이)로 나이트 12 를 섰다 — 이 병동 맵으로 먼저 읽으면 데이로 세어 두 병동 합
    상한(#6 · D33)이 안 걸린다."""
    _check_night_cap(_night_cap_coded("cx-n6-in-G2A12-X", NIGHT_CAP_G2.replace("N", "A")), engine, monkeypatch)


# ── P2 — 상대 병동에만 있는 특수 근무코드는 월경계와 같은 type 폴백(근무 → 근무 · 휴가·공가 → OFF) ─────────────


P2_CASES = [
    return_day1("O교교교교교", "X"),          # 1일 복귀 — 상대 병동 전월 꼬리 교육 5일(Codex 예) → 1일 OFF
    _join("O교교교교교", "X"),                # 합류 — 교육 5일 뒤 합류일 OFF
    depart_n07("교교교교교", "X", end_day=15),  # 출발 — 상대 첫 칸 교육 5일 → 이 병동 마지막 날 근무면 6연속
    _ret("OONN휴", "Y"),                     # 월중 복귀 — NN 뒤 휴가(OFF 1) → 복귀일 OFF(2N 뒤 2OFF)
    early_in(1, "O교교교교교", "", "X"),       # 1일 합류 — 상대 병동 전월 꼬리 교육 5일 → 1일 OFF
]


@pytest.mark.parametrize("case", P2_CASES, ids=[c.name for c in P2_CASES])
def test_p2_other_ward_special_codes_use_type_fallback_engine(case, engine, engine_input_tables, monkeypatch):
    """상대 병동 특수 근무코드가 근무(교육)면 연속근무로 이어지고 휴가면 OFF 로 세어져 경계 OFF 의무가 엔진 모델에 닿는다."""
    _check_coded(case, engine, monkeypatch)


# ── 12u-2 — 상대 병동 근무 슬롯(ShiftManage)에만 정의된 코드는 그 슬롯 뜻(1=D·2=E·3=N·5=M) ─────────────────


def _g2_slots_with_an(ward) -> None:
    """`_g2_slots` + 상대 병동 AN 행(5=미드 슬롯에 S·T) — 생성이 읽는 'RN' 행만 읽어야 S·T 가 나이트로 남는다."""
    _g2_slots(ward)
    ward.shift_manage.append({**shift_manage_row(5, "M", ["S", "T"], 1), "group_id": OTHER_GROUP_ID, "nurse_class": "AN"})


SLOT_CASES = [
    _join("OOOOSS", "Y"),                    # 합류 — 상대 꼬리 NN(S = 구분·기본코드 없는 근무 행 → 나이트 슬롯)
    _ret("OOOTT", "Y"),                      # 월중 복귀 — 상대 꼬리 NN(T = 근무코드 행 없이 나이트 슬롯에만)
    depart_n07("SSOOD", "X"),                # 출발 — 상대 첫 칸 NN(X — Y 는 출발 앞 NN 을 엔진이 스스로 막아 판정이 안 가른다)
    return_day1("OOOOTT", "Y"),              # 1일 복귀 — 상대 병동 전월 꼬리 NN(`_prev_month_cells_of`)
    early_in(1, "OOOOSS", "", "Y"),          # 1일 합류 — 상대 병동 전월 꼬리 NN(월경계 보충)
]


@pytest.mark.parametrize("case", SLOT_CASES, ids=[c.name for c in SLOT_CASES])
def test_slot_only_codes_reach_engine(case, engine, engine_input_tables, monkeypatch):
    """상대 병동 슬롯에만 나이트로 정의된 코드를 N 으로 읽어 경계 하드락(#2·#3·#5)이 엔진 모델에 닿는다."""
    _check_coded(case, engine, monkeypatch, SLOT_SHIFTS, _g2_slots_with_an)


@pytest.mark.parametrize("case", SLOT_CASES, ids=[c.name for c in SLOT_CASES])
def test_slot_only_codes_cells_equal_twin(case, engine, engine_input_tables):
    """상대 병동 슬롯에만 정의된 코드 칸이 N 으로 적은 쌍둥이와 같은 경계 칸을 낸다."""
    _assert_cells_equal_twin(case, engine, SLOT_SHIFTS, _g2_slots_with_an)


def test_slot_only_nights_count_toward_two_ward_cap(engine, engine_input_tables, monkeypatch):
    """X01 이 상대 병동에서 슬롯에만 나이트로 정의된 S·T 로 나이트 12 를 섰다 → 두 병동 합 월 나이트 상한(#6)에 들어간다."""
    _check_night_cap(_night_cap_coded("cx-n6-in-G2ST12-X", "SSSOOTTTOOSSSOOTTTO"), engine, monkeypatch,
                     SLOT_SHIFTS, _g2_slots_with_an)


# ── 12v(D52) — 그 병동이 자기 근무표를 만들 때와 같은 순서: 슬롯 main_code → 구분·기본코드 ─────────────────────────


#: 상대 병동 G2 의 G(구분 데이·기본코드 D 인데 G2 나이트 슬롯) · H(구분 나이트·기본코드 N 인데 G2 데이 슬롯) — G2 자신은 슬롯으로
#: 읽는다(`_build_code_to_main_map` — G=N · H=D). G·H 는 이 병동 G1 에 없다(이 병동 맵과 무관하게 구분 대 슬롯만 가른다).
V_SHIFTS = (
    *SHIFTS,
    shift_row("G", 88, "G데이", "데이", "근무", "D", 88, time(7, 0), time(15, 0), group_id=OTHER_GROUP_ID),
    shift_row("H", 89, "H나이트", "나이트", "근무", "N", 89, time(22, 0), time(7, 0), group_id=OTHER_GROUP_ID),
)


def _g2_slots_v(ward) -> None:
    """이 병동 슬롯(`_local_slots`) + 상대 병동 G2 RN 슬롯 1=D(D·H) · 2=E(E) · 3=N(N·G) — 구분·기본코드와 슬롯이 엇갈린다."""
    _local_slots(ward)
    ward.shift_manage += [{**shift_manage_row(slot, main, codes, 2), "group_id": OTHER_GROUP_ID}
                          for slot, main, codes in ((1, "D", ["D", "H"]), (2, "E", ["E"]), (3, "N", ["N", "G"]))]


#: G(→N) 엔진 판정 — 출발은 빠진다(구분대로 DD 로 읽어도 이 병동 마지막 날 N 을 막아 엔진 판정이 안 가른다 · 칸 대조가 잡는다).
V_NIGHT_CASES = [
    _join("OOOOGG", "Y"),                    # 합류 — 상대 꼬리 NN(구분으로 읽으면 DD)
    _ret("OOOGG", "Y"),                      # 월중 복귀
    return_day1("OOOOGG", "Y"),              # 1일 복귀(`_prev_month_cells_of`)
    early_in(1, "OOOOGG", "", "Y"),          # 1일 합류(월경계 보충)
]


@pytest.mark.parametrize("case", V_NIGHT_CASES, ids=[c.name for c in V_NIGHT_CASES])
def test_slot_beats_def_night_reaches_engine(case, engine, engine_input_tables, monkeypatch):
    """구분 데이·슬롯 나이트인 상대 병동 코드(G)를 그 병동처럼 N 으로 읽어 경계 하드락(#2·#3·#5)이 엔진 모델에 닿는다."""
    _check_coded(case, engine, monkeypatch, V_SHIFTS, _g2_slots_v)


V_TWIN_CASES = [
    *V_NIGHT_CASES, depart_n07("GGOOD", "X"),
    _join("OOOOHH", "Y"), depart_n07("HHOOD", "X"), return_day1("OOOOHH", "Y"), early_in(1, "OOOOHH", "", "Y"),
    _night_cap_coded("cx-n6-in-G2H12-X", NIGHT_CAP_G2.replace("N", "H")),   # H 12 개 — 나이트가 아니라 #6 에 안 든다
]


@pytest.mark.parametrize("case", V_TWIN_CASES, ids=[c.name for c in V_TWIN_CASES])
def test_slot_beats_def_cells_equal_twin(case, engine, engine_input_tables):
    """G(→N)·H(→D) 칸이 그 병동 뜻으로 적은 쌍둥이와 같은 경계 칸·월경계 이월 값·개인 월 N 상한을 낸다 — H 를 구분대로 N 으로 읽으면
    더 빡빡해질 뿐이라 엔진 판정으로는 안 갈리고 칸 대조로만 잡힌다."""
    _assert_cells_equal_twin(case, engine, V_SHIFTS, _g2_slots_v)


def test_slot_beats_def_night_counts_toward_two_ward_cap(engine, engine_input_tables, monkeypatch):
    """X01 이 상대 병동에서 G(구분 데이·슬롯 나이트)로 나이트 12 를 섰다 → 두 병동 합 월 나이트 상한(#6)에 들어간다."""
    _check_night_cap(_night_cap_coded("cx-n6-in-G2G12-X", NIGHT_CAP_G2.replace("N", "G")), engine, monkeypatch,
                     V_SHIFTS, _g2_slots_v)


# ── 순수 함수 단위(기대값은 손으로 적었다) ─────────────────────────────────────────────


def _row(sid: str, gb=None, default=None, kind="근무") -> SimpleNamespace:
    return SimpleNamespace(shift_id=sid, shift_gb=gb, default_shift=default, type=kind)


def _slot(slot: int, main: str, codes, rid: int) -> SimpleNamespace:
    return SimpleNamespace(shift_slot=slot, main_code=main, codes=list(codes), id=rid)


def test_other_ward_slot_precedence():
    """상대 병동 근무 슬롯은 그 병동이 자기 근무표를 만들 때와 같은 함수(`_build_code_to_main_map` — main_code)로 읽고 그 병동
    구분·기본코드·이 병동 맵·type 폴백보다 먼저다(12v · D52). 근무가 아닌 슬롯(휴무 슬롯 4 · main_code O)은 안 읽고, 한 코드가 여러
    슬롯에 있으면 그 병동처럼 슬롯·id 순 마지막 행(행 순서와 무관). 12v 가 바꾼 기대값: G(구분 데이·나이트 슬롯) D → N — 12u-2 는
    구분·기본코드가 슬롯보다 먼저였다."""
    from services.roster_create_service import _group_shift_code2main

    base = {"K": "D", "Y": "N", "O": "O", "OFF": "O", "주": "O"}
    rows = [_row("S"), _row("G", gb="데이"), _row("V", kind="휴가"), _row("Y")]
    slots = [_slot(5, "M", ["M"], 5), _slot(4, "O", ["U"], 4), _slot(3, "N", ["N", "S", "T", "G", "K", "C"], 3),
             _slot(2, "E", ["E", "W2"], 2), _slot(1, "D", ["D", "V", "C"], 1)]
    got = _group_shift_code2main(_FakeShiftDb(rows, slots), OTHER_GROUP_ID, base)
    # S 슬롯 > type · T 슬롯만 · G 슬롯 > 구분 · K 슬롯 > 이 병동 맵 · V 슬롯 > type · Y 이 병동 맵 > type · C 슬롯 1·3 → 3(N) ·
    # U 휴무 슬롯 안 읽음
    assert got == {**base, "D": "D", "N": "N", "M": "M", "S": "N", "T": "N", "G": "N", "K": "N", "V": "D", "C": "N",
                   "E": "E", "W2": "E"}


def test_other_ward_code_map_precedence():
    """그 병동 정의(구분·기본코드)가 이 병동 맵을 이긴다 · type 만으로 정한 뜻은 이 병동 맵이 못 읽는 코드에만(근무는 이 병동 OFF 도
    덮는다) · 그 병동이 정하지 못한 코드와 그 병동에 없는 코드는 이 병동 맵 그대로."""
    from services.roster_create_service import _group_shift_code2main

    base = {"A": "D", "N": "N", "X": "O", "Y": "D", "Z": "D", "K": "E", "O": "O", "OFF": "O", "주": "O"}
    rows = [
        _row("A", gb="나이트"),                   # ① 구분 — 이 병동 D 를 이긴다
        _row("B", default="D"),                   # ② 기본코드
        _row("교"), _row("휴", kind="휴가"), _row("공", kind="공가"),   # ③ type — 이 병동 맵에 없는 코드
        _row("N"),                                # type 만(구분·기본코드 없음) — 이 병동 N 유지(WK 로 내리지 않는다)
        _row("X"),                                # type 근무 — 이 병동 O 를 덮는다(근무를 놓치지 않는다)
        _row("Y", kind="휴가"),                   # type 휴가 — 이 병동 근무 D 유지(보수)
        _row("Z", gb="고정", kind="휴무"),         # 못 정함 — 이 병동 D 유지
    ]
    got = _group_shift_code2main(_FakeShiftDb(rows), OTHER_GROUP_ID, base)
    assert got == {**base, "A": "N", "B": "D", "교": "WK", "휴": "O", "공": "O", "X": "WK"}


def test_other_ward_nights_use_that_wards_meaning():
    """이 병동 맵이 알아보는 코드(A=D)도 그 병동 정의(나이트)로 센다 — 이 병동 맵으로 다 풀린다고 그 병동 정의를 건너뛰지 않는다
    (Codex P1). 병동마다 한 번 읽고(근무코드·근무 슬롯 조회 2 · 캐시) · 차단일에 고를 칸이 없으면 읽지 않는다."""
    from services.roster_create_service import _count_other_ward_nights

    entries = [SimpleNamespace(work_date=datetime(2026, 11, day), shift_id=code)
               for day, code in ((1, "A"), (2, "A"), (3, "OFF"), (4, "N"))]
    db = _FakeShiftDb([_row("A", gb="나이트")])
    base = {"A": "D", "D": "D", "E": "E", "N": "N", "O": "O", "OFF": "O", "주": "O"}
    maps: dict = {}
    assert (_count_other_ward_nights(db, entries, set(range(19)), base, OTHER_GROUP_ID, maps), db.calls) == (3, 2)
    assert (_count_other_ward_nights(db, entries, set(range(19)), base, OTHER_GROUP_ID, maps), db.calls) == (3, 2)
    assert (_count_other_ward_nights(db, entries, set(range(10, 19)), base, OTHER_GROUP_ID, {}), db.calls) == (0, 2)


@pytest.mark.parametrize("gb,default,kind,expected", [
    ("나이트", None, "근무", ("N", False)), ("데이", "N", "근무", ("D", False)),     # ① 구분이 기본코드보다 먼저
    ("D", "D", "근무", ("D", False)),                                                # 구분 'D'(표 밖) → 기본코드
    (None, "OFF", "휴무", ("O", False)), (None, "주", "휴무", ("O", False)), ("고정", "M", "근무", ("M", False)),
    (None, None, "근무", ("WK", True)), (None, None, "휴가", ("O", True)), (None, None, "공가", ("O", True)),
    (None, None, "휴무", (None, False)), ("고정", None, "오프", (None, False)),
])
def test_shift_def_main_table(gb, default, kind, expected):
    """월경계·상대 병동이 같이 쓰는 근무코드 판정 한 곳 — ① shift_gb ② default_shift ③ type(근무 WK · 휴가·공가 O)."""
    from services.roster_create_service import _shift_def_main

    assert _shift_def_main(_row("Q", gb=gb, default=default, kind=kind)) == expected


def test_other_ward_def_conflicts_with_slot(capsys):
    """그 병동 구분·기본코드와 슬롯이 다를 때(설정 오류) — 그 병동 자신처럼 슬롯을 따르고 알린다(WARN · 12v · D52). 정의가 휴무(O)여도
    슬롯 근무를 지우지 않는다(Xr 와 같다). 12v 가 바꾼 기대값: T(구분 데이·나이트 슬롯) D → N — Xr 는 근무끼리 다르면 정의를 따랐다."""
    from services.roster_create_service import _group_shift_code2main

    base = {"O": "O", "OFF": "O", "주": "O"}
    rows = [_row("S", default="OFF"), _row("P", default="주"), _row("T", gb="데이"), _row("U", gb="나이트")]
    slots = [_slot(3, "N", ["S", "P", "T", "U"], 3)]
    got = _group_shift_code2main(_FakeShiftDb(rows, slots), OTHER_GROUP_ID, base)
    assert got == {**base, "S": "N", "P": "N", "T": "N", "U": "N"}
    warned = [line.split("code=")[1].split()[0] for line in capsys.readouterr().out.splitlines() if "정의≠슬롯" in line]
    assert warned == ["S", "P", "T"]


def test_other_ward_off_slot_does_not_erase_work():
    """보수 예외(12v) — 그 병동 슬롯이 휴무로 정한 코드(휴무 슬롯 4 · main_code O)는 슬롯으로 읽지 않는다. 그 병동 자신은 휴무로 읽지만
    그 병동 근무코드 정의(U 데이)·이 병동 맵(Q 나이트)이 근무로 읽는 코드를 휴무로 바꾸면 하드락이 풀린다(12u-2 도 슬롯 4 를 안 읽었다).
    다른 근거가 없는 코드(R)는 지금처럼 모르는 코드다."""
    from services.roster_create_service import _group_shift_code2main

    base = {"Q": "N", "OFF": "O"}
    got = _group_shift_code2main(_FakeShiftDb([_row("U", gb="데이")], [_slot(4, "O", ["U", "Q", "R"], 4)]),
                                 OTHER_GROUP_ID, base)
    assert got == {**base, "U": "D"}


#: 12v 동치 표본 — (상대 병동 근무 슬롯 행, 근무코드 행, 이 병동 맵). 이 병동 맵에는 일부러 다른 뜻을 적었다.
V_SAMPLES = {
    "보통 병동": (
        [_slot(1, "D", ["D", "D1"], 1), _slot(2, "E", ["E", "E1"], 2), _slot(3, "N", ["N", "NT"], 3), _slot(5, "M", ["M"], 5)],
        [_row("D", gb="데이"), _row("D1", gb="데이"), _row("E", gb="이브닝"), _row("E1", gb="이브닝"), _row("N", gb="나이트"),
         _row("NT", gb="나이트"), _row("M", gb="미드"), _row("EX", gb="이브닝"), _row("OFF", default="OFF", kind="휴무"),
         _row("교"), _row("휴", kind="휴가")],
        {"D1": "E", "NT": "D", "E1": "N", "EX": "D"}),
    "구분과 슬롯이 엇갈림": (
        [_slot(1, "D", ["D", "H"], 1), _slot(3, "N", ["N", "G"], 3)],
        [_row("G", gb="데이", default="D"), _row("H", gb="나이트", default="N"), _row("N", gb="나이트")],
        {"G": "D", "H": "N"}),
    "한 코드가 두 슬롯·같은 슬롯 중복 행(main_code 가 슬롯 번호와 다름)": (
        [_slot(3, "N", ["C"], 2), _slot(1, "D", ["C", "Q"], 9), _slot(2, "N", ["Q"], 7), _slot(2, "E", ["Q", "R"], 3)],
        [_row("C", gb="데이"), _row("Q", gb="이브닝")],
        {"C": "D", "Q": "E", "R": "D"}),
    "소문자·공백": (
        [_slot(3, " n ", [" nt ", "N"], 3), _slot(1, "d", ["d2"], 1)],
        [_row("NT", default="E")],
        {"NT": "E", "D2": "N"}),
}


def _own_reading(slots, rows) -> dict:
    """그 병동이 자기 근무표를 만들 때의 판정 — 슬롯 행(생성 조회처럼 슬롯·id 순)을 `_build_code_to_main_map` 으로, 슬롯에 없는 코드는
    근무코드 정의(`_shift_def_main` — 월경계 보강과 같다)로."""
    from services.roster_create_service import _build_code_to_main_map, _shift_def_main

    own = _build_code_to_main_map([{"main_code": s.main_code, "codes": s.codes}
                                   for s in sorted(slots, key=lambda s: (s.shift_slot, s.id))])
    for r in rows:
        if r.shift_id not in own and _shift_def_main(r)[0] is not None:
            own[r.shift_id] = _shift_def_main(r)[0]
    return own


@pytest.mark.parametrize("name", list(V_SAMPLES))
def test_other_ward_reads_like_that_ward_itself(name):
    """동치(12v · D52) — 상대 병동 판정이 그 병동 자신의 판정과 같은 뜻을 낸다(근무표 칸을 읽는 `_normalize_to_main` 기준 · 그 병동이 아는
    코드 전부). 이 병동 맵은 그 병동이 아는 코드에서 이기지 못한다. 슬롯 행은 거꾸로 넣는다(정렬은 판정이 한다)."""
    from services.roster_create_service import _group_shift_code2main, _normalize_to_main

    slots, rows, base = V_SAMPLES[name]
    got = _group_shift_code2main(_FakeShiftDb(rows, list(reversed(slots))), OTHER_GROUP_ID, base)
    own = _own_reading(slots, rows)
    assert {c: _normalize_to_main(c, got) for c in own} == {c: _normalize_to_main(c, own) for c in own}


def test_other_ward_slot_lookup_failure_is_raised():
    """상대 병동 근무 슬롯(ShiftManage) 조회 실패를 삼키지 않는다 — 세부 코드를 못 읽으면 하드락 판정을 할 수 없다(막는 쪽)."""
    from db.models import ShiftManage
    from services.roster_create_service import _group_shift_code2main

    class _SlotFails(_FakeShiftDb):
        def query(self, model):
            if model is ShiftManage:
                raise RuntimeError("슬롯 주입")
            return super().query(model)

    with pytest.raises(RuntimeError, match="슬롯 주입"):
        _group_shift_code2main(_SlotFails([_row("A")]), OTHER_GROUP_ID, {"OFF": "O"})
    with pytest.raises(RuntimeError, match="슬롯 주입"):
        _group_shift_code2main(_SlotFails([_row("A")]), OTHER_GROUP_ID, {"OFF": "O"}, {}, {})


def test_group_code_maps_cache_reads_once_per_ward():
    """`maps` 캐시 — 같은 병동은 합류·복귀가 같이 써도 근무코드·슬롯 조회 각 1회(`reads` 없이도)."""
    from services.roster_create_service import _group_shift_code2main

    db = _FakeShiftDb([_row("A", gb="나이트")])
    maps: dict = {}
    first = _group_shift_code2main(db, OTHER_GROUP_ID, {"OFF": "O"}, maps)
    assert _group_shift_code2main(db, OTHER_GROUP_ID, {"OFF": "O"}, maps) is first and db.calls == 2
