"""EI-9-0 특성화 — 고정근무 차감 블록의 경로별 쿼리·순서·대입·예외를 전환 **전** 코드에서 고정한다.

9-0a(설계서 EI9_SPEC.md §4.3). 시나리오는 `scenario_defs` 재료로 만들지만 `SCENARIOS` 레지스트리
에는 넣지 않는다(PATH_CHECKS 전수 전제를 건드리지 않기 위해). 기대값은 `ei9/scratch/exp_tree`
(base_before 사본)에서 지금 코드로 다시 잰 값(`exp_results.json`·`exp_allfixed.json`)을 그대로
상수로 옮겼다 — 재생성하지 않는다. 전환 뒤에도 이 테스트가 그대로 통과해야 한다(9-0b 전환 뒤
전용 테스트는 `test_fixed_cut.py` 의 S-1~S-4).
"""
from __future__ import annotations

from dataclasses import replace
from datetime import date

from .harness import CapturedRun, run_with_output
from .scenario_defs import SCENARIOS, _rows, _scenario, s5_ward
from .ward import GROUP_ID, OFFICE_ID, SEED_TS, stamp

_CUT_PREFIXES = ("[FixedShift", "[Holiday]", "[FixedShiftCoverage]")


def _lines(run: CapturedRun) -> list[str]:
    """차감 관련 로그(분리 줄 제외하지 않음 — 분리 줄도 §3.2 표에 포함)."""
    return [ln for ln in run.stdout.splitlines() if ln.startswith(_CUT_PREFIXES)]


def _segment(run: CapturedRun) -> list[str] | None:
    """L0..L0′ 쿼리 순서열(두 표지가 없으면 None)."""
    q = run.result.queries
    lo, hi = q["marks"].get("L0"), q["marks"].get("L0p")
    if lo is None or hi is None:
        return None
    return q["tables"][lo:hi]


_CUT4 = ["select:information_schema.columns", "select:roster_config", "select:shifts", "select:fixed_wanted_entries"]
_CUT3 = ["select:information_schema.columns", "select:roster_config", "select:shifts"]   # 모르는 고정 코드 — 확정 원티드 조회 전에 멈춘다
_PRECHECK_OWN = ["select:schedules"]   # L0~L0′ 사이 — 탐지기 몫의 조회(차감은 precheck 앞, 단계 B 빌더도 그 앞이다 — EI-10g2·g3 · EI-13c-3 병합 근무코드 재조회 없음)


def _cut_segment(run: CapturedRun, expected: list[str]) -> list[str] | None:
    """고정근무 차감이 읽는 쿼리 순서열 — EI-10g2 부터 차감은 precheck 앞이다: L0 표지(`run_runtime_precheck` 진입)보다 **앞**,
    단계 B 빌더·precheck 입력을 짓는 조회(등급·주말휴무·팀)보다도 앞이다. L0 표지 앞쪽에서 `expected` 와 같은 연속 구간을 찾아 돌려준다
    (없으면 표지 앞 같은 길이 꼬리 — 불일치 보고용)."""
    q = run.result.queries
    lo = q["marks"].get("L0")
    if lo is None:
        return None
    head = q["tables"][:lo]
    size = len(expected)
    for i in range(len(head) - size, -1, -1):
        if head[i:i + size] == expected:
            assert i + size < lo, "차감 쿼리가 precheck(L0 표지)보다 앞이어야 한다"
            return head[i:i + size]
    return head[-size:]


def _by_day(cfg: dict, code: str) -> list:
    return [d.get(code) for d in (cfg.get("daily_shift_requirements_by_day") or [])]


def _by_day_max(cfg: dict, code: str) -> list:
    return [d.get(code) for d in (cfg.get("daily_shift_requirements_max_by_day") or [])]


def _assert_by_day_unchanged(run: CapturedRun) -> None:
    """precheck(L0)·탐지기(L0′) 의 by_day 가 D·E·N 전부 같다(차감이 아무것도 못 바꿨다)."""
    l0, l0p = _l0_config(run), _l0p_config(run)
    for code in ("D", "E", "N"):
        assert _by_day(l0, code) == _by_day(l0p, code), code


def _l0_config(run: CapturedRun) -> dict:
    return run.result.layers["L0"][0]["config_dict"]


def _l0p_config(run: CapturedRun) -> dict:
    return run.result.layers["L0p"][0]["config"]


# ── 변형 ────────────────────────────────────────────────────────────────


def _s5_badcode():
    """S5-Z — F01 고정코드를 shifts 에 없는 'Z' 로(400 경로, P-4 의 유일한 증거)."""
    ward = s5_ward()
    for row in ward.nurses:
        if row["nurse_id"] == "F01":
            row["fixed_shift"] = "Z"
    return _scenario("S5-Z", ward, "S5 + 모르는 고정코드(400)")


def _holiday_raises(mp) -> None:
    """S5-RAISE 패치 — `_holiday_off_enabled` 를 RuntimeError 로 바꾼다."""
    import services.roster_create_service as rcs

    def boom(*_a, **_k):
        raise RuntimeError("fx-boom")

    mp.setattr(rcs, "_holiday_off_enabled", boom)


def _s5_raise():
    base = SCENARIOS["S5"]
    return replace(base, name="S5-RAISE", patches=base.patches + (_holiday_raises,))


def _daily_max_rows(db) -> None:
    """S5-MAX 시드 — 2일만 D max=1(< min 2), 나머지는 2 — 되올림 경로."""
    from db.models import DailyShift

    for day in range(1, 32):
        dmax = 1 if day == 2 else 2
        db.add(DailyShift(office_id=OFFICE_ID, group_id=GROUP_ID, year=2026, month=12, day=day,
                          d_count=2, e_count=2, n_count=2, m_count=0, d_count_max=dmax,
                          e_count_max=2, n_count_max=2, m_count_max=0, max_enabled=True, **stamp()))


def _s5_max():
    ward = s5_ward()
    ward.extras.append(_daily_max_rows)
    return _scenario("S5-MAX", ward, "S5 + 일자별 max(2일 D max1<min2) — 되올림")


def _s5_fw():
    """S5-FW — F01 확정 원티드: 3일 O(적용)·4일 E(적용)·7일 N(미적용)."""
    ward = s5_ward()
    rows = [("F01", 3, "O", True), ("F01", 4, "E", True), ("F01", 7, "N", False)]
    base = {"group_id": GROUP_ID, "year": 2026, "month": 12, **stamp()}
    ward.extras.append(_rows("FixedWantedEntry", [
        {**base, "nurse_id": nid, "shift_date": date(2026, 12, d), "shift_id": c,
         "shifts_table_id": None, "is_applied": a, "source_type": "original",
         "original_shift_id": c, "created_by": "HN01"}
        for nid, d, c, a in rows]))
    return _scenario("S5-FW", ward, "S5 + F01 확정 원티드(적용 O·E, 미적용 N)")


def _s5_allfixed():
    """S5-ALLFIXED — F01 만(전원 고정근무, nurses_for_engine=0 조건 증거)."""
    ward = s5_ward()
    ward.nurses = [r for r in ward.nurses if r["nurse_id"] == "F01"]
    return _scenario("S5-ALLFIXED", ward, "전원 고정근무 — 엔진 대상 0명")


def _s5_sp_wanted(db) -> None:
    """F01 제출 원티드(확정 원티드 없음) — 12/9(평일) 특수요청 '연'(휴가) 1건."""
    from db.models import NurseShiftRequest, WantedRequest

    db.add(WantedRequest(nurse_id="F01", request_id=1, month="2026-12", group_id=GROUP_ID,
                         request="연차 희망", is_submitted=1, created_at=SEED_TS, submitted_at=SEED_TS))
    db.add(NurseShiftRequest(nurse_id="F01", request_id=1, detailed_request_id=9,
                             shift_date=date(2026, 12, 9), group_id=GROUP_ID, shift="연", score=5))


def _s5_sp():
    """S5-SP — 확정 원티드(FixedWantedEntry) 없음 + F01 제출 원티드(WantedRequest/NurseShiftRequest)
    경로로만 들어오는 특수요청. 고정근무 차감 호출 자리에 넘어가는 `special_requests` 가
    확정 원티드 오버레이가 없는 달에서도 채워지는지의 유일한 증거(호출 자리 인자 — 리뷰 HIGH)."""
    from .ward import shift_row

    ward = s5_ward()
    ward.shifts.append(shift_row("연", 8, "연차", None, "휴가", None, 8))
    ward.extras.append(_s5_sp_wanted)
    return _scenario("S5-SP", ward, "확정 원티드 없음 + F01 제출 원티드 특수요청(연, 12/9 평일)")


# ── P-1 S5 ─────────────────────────────────────────────────────────────


def test_p1_s5(engine, engine_input_tables):
    run = run_with_output(engine, SCENARIOS["S5"])
    r = run.result
    assert (r.outcome, r.path) == ("golden_stop", "first_attempt")
    assert r.queries["count"] == 76  # EI-13a-2: 첫 시도 재조회 4건 제거 · EI-13c-2: 팀 N+1 일괄 −20 · 13c-3 병합 재조회 −1
    assert _cut_segment(run, _CUT4) == _CUT4  # EI-10g2 — 차감이 precheck 앞이라 L0 표지 앞에 온다(옛: L0~L0′ 사이 6건)
    assert _segment(run) == _PRECHECK_OWN
    assert _lines(run) == [
        "[FixedShift] 고정 근무 간호사 1명 분리됨 → ['F01(F01): fixed_shift=D']",
        "[FixedShift] shift_lookup 로드: 4건",
        "[Holiday] 공휴일 OFF 적용 — days=[25]",
        "[FixedShift] 적용: F01(F01) 코드=D, 범위=1~31일",
        "[FixedShiftCoverage] 고정근무자 1명 · 요구 차감 min 22셀 / max 0셀 — 솔버는 남은 인원만 채운다",
    ]
    l0p = _l0p_config(run)
    assert _by_day(l0p, "D")[23] == 1  # 24일(평일) — 차감 뒤 1
    assert _by_day(l0p, "D")[24] == 2  # 25일(공휴일) — 차감 없음


# ── P-2 S9-F ───────────────────────────────────────────────────────────


def test_p2_s9f(engine, engine_input_tables):
    run = run_with_output(engine, SCENARIOS["S9-F"])
    r = run.result
    assert (r.outcome, r.path) == ("http_exception", "night_block")
    assert r.queries["count"] == 85  # EI-13c-2: 팀 N+1 일괄 −20 · 13c-3 병합 재조회 −1 · EI-10g3: 나이트 차단 경로도 단계 B 빌더 조회 +6
    assert _cut_segment(run, _CUT4) == _CUT4  # EI-10g2 — 차감이 precheck 앞이라 L0 표지 앞에 온다(옛: L0~L0′ 사이 6건)
    assert _segment(run) == _PRECHECK_OWN
    assert _lines(run) == [
        "[FixedShift] 고정 근무 간호사 1명 분리됨 → ['F01(F01): fixed_shift=N']",
        "[FixedShift] shift_lookup 로드: 5건",
        "[FixedShift] 적용: F01(F01) 코드=N, 범위=1~30일",
        "[FixedShiftCoverage] 고정근무자 1명 · 요구 차감 min 21셀 / max 0셀 — 솔버는 남은 인원만 채운다",
    ]
    assert sum(1 for v in _by_day(_l0p_config(run), "N") if v == 0) == 21  # 평일 21일 0 으로 차감


# ── P-3 S1(고정근무자 없음) ───────────────────────────────────────────


def test_p3_s1(engine, engine_input_tables):
    run = run_with_output(engine, SCENARIOS["S1"])
    assert _segment(run) == _PRECHECK_OWN
    assert [ln for ln in _lines(run) if ln.startswith("[FixedShiftCoverage]")] == []


# ── P-4 S5-Z(모르는 고정 코드 — 400) ──────────────────────────────────


def test_p4_s5_z(engine, engine_input_tables):
    run = run_with_output(engine, _s5_badcode())
    r = run.result
    assert (r.outcome, r.path) == ("golden_stop", "first_attempt")
    assert r.queries["count"] == 75  # EI-13a-2: 첫 시도 재조회 4건 제거 · EI-13c-2: 팀 N+1 일괄 −20 · 13c-3 병합 재조회 −1
    seg = _cut_segment(run, _CUT3)
    assert seg == _CUT3
    assert "select:fixed_wanted_entries" not in seg  # 빌드 위치 불변의 유일한 증거(확정 원티드 조회 전에 멈춘다)
    assert _segment(run) == _PRECHECK_OWN
    assert _lines(run) == [
        "[FixedShift] 고정 근무 간호사 1명 분리됨 → ['F01(F01): fixed_shift=Z']",
        "[FixedShift] shift_lookup 로드: 4건",
        "[Holiday] 공휴일 OFF 적용 — days=[25]",
        "[FixedShiftCoverage] 차감 실패(무시): HTTPException: 400: "
        "fixed_shift가 shifts에 없습니다: nurse=F01(F01), code=Z",
    ]
    _assert_by_day_unchanged(run)


# ── P-5 S5-RAISE(공휴일 조회가 RuntimeError) ──────────────────────────


def test_p5_s5_raise(engine, engine_input_tables):
    run = run_with_output(engine, _s5_raise())
    r = run.result
    assert (r.outcome, r.path) == ("golden_stop", "first_attempt")
    assert r.queries["count"] == 73  # EI-13a-2: 첫 시도 재조회 4건 제거 · EI-13c-2: 팀 N+1 일괄 −20
    assert _segment(run) == _PRECHECK_OWN
    assert _lines(run) == [
        "[FixedShift] 고정 근무 간호사 1명 분리됨 → ['F01(F01): fixed_shift=D']",
        "[FixedShiftCoverage] 차감 실패(무시): RuntimeError: fx-boom",
        "[FixedShift] shift_lookup 로드: 4건",
    ]
    _assert_by_day_unchanged(run)


# ── P-6 S5-MAX(되올림) ─────────────────────────────────────────────────


def test_p6_s5_max(engine, engine_input_tables):
    run = run_with_output(engine, _s5_max())
    r = run.result
    assert (r.outcome, r.path) == ("golden_stop", "first_attempt")
    assert r.queries["count"] == 76  # EI-13a-2: 첫 시도 재조회 4건 제거 · EI-13c-2: 팀 N+1 일괄 −20 · 13c-3 병합 재조회 −1
    assert _lines(run) == [
        "[FixedShift] 고정 근무 간호사 1명 분리됨 → ['F01(F01): fixed_shift=D']",
        "[FixedShift] shift_lookup 로드: 4건",
        "[Holiday] 공휴일 OFF 적용 — days=[25]",
        "[FixedShift] 적용: F01(F01) 코드=D, 범위=1~31일",
        "[FixedShiftCoverage] max < min 이 된 1칸을 min 으로 되올림 (원설정이 max < min 인 칸)",
        "[FixedShiftCoverage] 고정근무자 1명 · 요구 차감 min 22셀 / max 22셀 — 솔버는 남은 인원만 채운다",
    ]
    l0p = _l0p_config(run)
    assert _by_day_max(l0p, "D")[1] == 1  # 2일 — 원래 1(max<min)이었다가 되올림으로 1(=min)
    assert _by_day(l0p, "D")[1] == 1


# ── P-7 S5-FW(확정 원티드 덮기) ────────────────────────────────────────


def test_p7_s5_fw(engine, engine_input_tables):
    run = run_with_output(engine, _s5_fw())
    r = run.result
    assert (r.outcome, r.path) == ("golden_stop", "first_attempt")
    assert r.queries["count"] == 77  # EI-13a-2: 첫 시도 재조회 4건 제거 · EI-13c-2: 팀 N+1 일괄 −20 · 13c-3 병합 재조회 −1
    assert _lines(run) == [
        "[FixedShift] 고정 근무 간호사 1명 분리됨 → ['F01(F01): fixed_shift=D']",
        "[FixedShift] shift_lookup 로드: 4건",
        "[Holiday] 공휴일 OFF 적용 — days=[25]",
        "[FixedShift] 적용: F01(F01) 코드=D, 범위=1~31일",
        "[FixedShiftCoverage] 고정근무자 1명 · 요구 차감 min 21셀 / max 0셀 — 솔버는 남은 인원만 채운다",
    ]
    l0p = _l0p_config(run)
    assert _by_day(l0p, "D")[2] == 2  # 3일(O 로 덮임) — 차감 없음
    assert _by_day(l0p, "D")[3] == 2  # 4일(E 로 덮임) — D 차감 없음
    assert _by_day(l0p, "E")[3] == 1  # 4일 — E 차감 있음(F01 이 E 로 덮임)
    assert _by_day(l0p, "D")[6] == 1  # 7일 — 미적용(is_applied=False) N 은 무시, D 그대로 차감
    assert _by_day(l0p, "N")[6] == 2  # 7일 — 미적용 N 이 적용됐다면 줄었을 칸(리뷰 MEDIUM)


# ── P-8 S5-ALLFIXED(전원 고정 — 조건 nurses_for_engine) ──────────────


def test_p8_s5_allfixed(engine, engine_input_tables):
    run = run_with_output(engine, _s5_allfixed())
    r = run.result
    assert r.outcome == "returned"
    assert r.queries["count"] == 76
    lines = _lines(run)
    assert [ln for ln in lines if ln.startswith("[FixedShiftCoverage]")] == []
    assert sum(1 for ln in lines if "shift_lookup 로드" in ln) == 1  # 병합 경로만(차감 블록 건너뜀)


# ── P-9 S5-SP(확정 원티드 없음 + 제출 원티드 특수요청) ────────────────


def test_p9_s5_sp(engine, engine_input_tables):
    run = run_with_output(engine, _s5_sp())
    r = run.result
    assert (r.outcome, r.path) == ("golden_stop", "first_attempt")
    assert _lines(run) == [
        "[FixedShift] 고정 근무 간호사 1명 분리됨 → ['F01(F01): fixed_shift=D']",
        "[FixedShift] shift_lookup 로드: 5건",  # S5 의 4건 + '연'(특수요청 코드)
        "[Holiday] 공휴일 OFF 적용 — days=[25]",
        "[FixedShift] 적용: F01(F01) 코드=D, 범위=1~31일",
        "[FixedShiftCoverage] 고정근무자 1명 · 요구 차감 min 21셀 / max 0셀 — 솔버는 남은 인원만 채운다",
    ]  # min 22(P-1, 특수요청 없음)가 아니라 21 — 9일이 '연'으로 덮여 D 차감에서 빠진다
    l0p = _l0p_config(run)
    assert _by_day(l0p, "D")[8] == 2  # 9일(제출 원티드 특수요청 '연' 로 덮임) — D 차감 없음
