"""EI-10d 결과 테스트 — precheck 가 엔진 형식 고정 칸(nurse_index·day_index·원코드)을 읽게 되어 판정이 바뀌는 사례.

배경(결정 D8 · 설계서 §2.6): 생성 서비스의 `combined_fixed_cells` 원소는 엔진 형식
`{nurse_index, day_index, shift(원코드), shift_type, fixed_source}` 인데 `runtime_bridge.
_collect_fixed_assignments` 는 `nurse_id` 와 `date`/`day` 만 읽어 **칸을 전부 버렸다**(결함 ②).
그래서 precheck 는 주휴·휴가·확정 원티드 고정 칸을 한 번도 못 봤다. 10d 는 칸을 엔진 색인·코드 규칙으로
`{nurse_id, day_index, shift}` 로 번역해(`fixed_cell_codes`) 넘기고, 칸이 닿으면서 엔진보다 엄한 검사 2종
(FIXED_ASSIGN_EXCEEDS_NEED · FIXED_OFF_EXCEEDS_SPAN)은 경고로 내린다.

각 테스트의 '엔진 판정'은 fallback_lex 의 하드 제약 의미로 따진 값이다. ✗ = 10d 전 코드에서 빨강(결함 재현),
(가드) = 전/후 모두 초록이지만 변이·퇴행을 잡는 불변 확인이다.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from services.precheck import run_runtime_precheck
from services.precheck.payload import has_blocking_issues
from services.precheck.runtime_bridge import build_precheck_input
from services.precheck.team_grade_precheck import PrecheckInput, PrecheckNurse, run_precheck

SEQ = "PER_NURSE_SEQUENCE_INFEASIBLE"
ALLOWED = "FIXED_ASSIGN_VIOLATES_ALLOWED"
OVER_NEED = "FIXED_ASSIGN_EXCEEDS_NEED"
OFF_SPAN = "FIXED_OFF_EXCEEDS_SPAN"
DAYCAP = "GLOBAL_DAY_CAPACITY_SHORTAGE"


def _cfg(**extra):
    """엔진 기본에 가까운 설정(3연속 N 허용 · 3N→2OFF 켬 · 2N→2OFF 끔 · 1N 허용)."""
    base = {
        "daily_shift_requirements": {"D": 1, "E": 1, "N": 1}, "max_conseq_work": 5,
        "max_nig_per_month": 15, "banned_day_after_eve": True, "three_seq_nig": True,
        "two_offs_after_three_nig": True, "two_offs_after_two_nig": False, "not_one_night": False,
        "use_mid": False, "off_days": 8, "shift_priority": 0.7,
    }
    base.update(extra)
    return base


def _nurses(n=10, **extra):
    return [{"nurse_id": f"n{i}", "grade": 1, "allowed_shifts": [], "sequence": i, **extra} for i in range(n)]


def _cell(nurse_id, day_index, shift):
    """번역을 거친 고정 칸 한 개 — 생성 경로가 bridge 에 넘기는 모양(`day_index` 는 0-based)."""
    return {"nurse_id": nurse_id, "day_index": day_index, "shift": shift}


def _cells(nurse_id, by_day):
    return [_cell(nurse_id, day, shift) for day, shift in by_day.items()]


def _run(nurses, cfg, cells=None, *, stop_on_config_error=False):
    result = run_runtime_precheck(
        nurses_dict=nurses, config_dict=cfg, grade_config=None, fixed_cells=cells,
        year=2026, month=11, stop_on_config_error=stop_on_config_error)
    return result, {issue["reason_code"] for issue in result["issues"]}


def _severities(result, code):
    return {issue["severity"] for issue in result["issues"] if issue["reason_code"] == code}


def _translate(cells, nurses, shift_manage=None, *, use_mid=False, blocked=None):
    """엔진 형식 칸 → precheck 칸. 간호사는 엔진 정렬(sequence·-경력·nurse_id)에 필요한 칸만 가진다.

    `blocked` 는 `config_dict["blocked_by_nurse_id"]`({nurse_id: 0-based 차단일}) — 배정(파견·휴직 등) 기간이다.
    """
    from services.precheck.fixed_cell_codes import precheck_fixed_cells

    live = [SimpleNamespace(nurse_id=n["nurse_id"], sequence=n.get("sequence", 0), experience=n.get("experience", 0))
            for n in nurses]
    manage = [{"main_code": "D", "codes": ["D", "D1"]}, {"main_code": "N", "codes": ["N"]}] \
        if shift_manage is None else shift_manage
    extra = {} if blocked is None else {"blocked_by_nurse_id": blocked}
    return precheck_fixed_cells(live, cells, manage, use_mid=use_mid, **extra)


def _engine_cell(nurse_index, day_index, shift, source="fixed_wanted", shift_type="근무"):
    return {"nurse_index": nurse_index, "day_index": day_index, "shift": shift,
            "fixed_source": source, "shift_type": shift_type}


def _direct_input(*, n=10, num_days=30, fixed_shift=None, fixed_off=None, joins=None, **cfg):
    """bridge 를 거치지 않는 직접 입력 — 라우터 `POST /groups/{id}/roster/precheck` 와 같은 모양."""
    nurses = [
        PrecheckNurse(
            nurse_id=f"n{i}", grade=1, join_day=(joins or {}).get(f"n{i}", 0), leave_day=num_days - 1,
            fixed_shift_assignments=dict((fixed_shift or {}).get(f"n{i}", {})),
            fixed_off_days=set((fixed_off or {}).get(f"n{i}", ())))
        for i in range(n)
    ]
    roster = {"daily_shift_requirements": {"D": 1, "E": 1, "N": 1}, **cfg}
    return PrecheckInput(num_days=num_days, nurses=nurses, teams=[], roster_config=roster,
                         team_coverage={}, grade_constraints={})


# ── 번역(엔진 형식 칸 → precheck 칸) ───────────────────────────────────────


def test_r10d1_translation_engine_order_codes_and_skips():
    """✗ 번역은 엔진 색인 순서로 간호사를 찾고, 엔진 정규화(OFF·주→O, 휴가·공가 특수코드→O)를 따른다.

    표 없이는 엔진 값을 알 수 없는 칸(원티드 하위코드 `D1`, 근무형 특수코드 → W, MID 꺼진 병동의 M,
    프리셉티 모델이 안 쓰는 W)과 색인 밖 칸은 **건너뛴다**(엔진보다 느슨할 뿐 오차단은 없다).
    """
    nurses = [{"nurse_id": "b", "sequence": 2}, {"nurse_id": "a", "sequence": 1}]  # 엔진 색인: a=0, b=1
    cells = [
        _engine_cell(0, 3, "OFF"),
        _engine_cell(1, 4, "연", "special_fixed", "휴가"),
        _engine_cell(1, 5, "D1"),
        _engine_cell(1, 6, "DD", "special_fixed", "근무"),
        _engine_cell(0, 7, "M"),
        _engine_cell(0, 8, "주", "weekly_off", "주휴"),
        _engine_cell(1, 9, "D"),
        _engine_cell(1, 10, "공", "special_fixed", "공가"),
        _engine_cell(0, 11, "W"),
        _engine_cell(9, 12, "N"),
    ]
    assert _translate(cells, nurses) == [
        _cell("a", 3, "O"), _cell("b", 4, "O"), _cell("a", 8, "O"), _cell("b", 9, "D"), _cell("b", 10, "O"),
    ]


def test_r10d1b_translation_keeps_mid_only_when_ward_uses_mid():
    """✗ 엔진은 shift_types 밖 코드를 버린다 — M 은 `use_mid` 일 때만 남긴다."""
    nurses = [{"nurse_id": "a", "sequence": 1}]
    cells = [_engine_cell(0, 7, "M")]
    assert _translate(cells, nurses, use_mid=False) == []
    assert _translate(cells, nurses, use_mid=True) == [_cell("a", 7, "M")]


def test_r10d1c_translation_applies_engine_second_stage_code_table():
    """✗ 엔진 2단계 정규화(`code2main.get(code) or code`, cp_sat_basic.py:2647)와 같다.

    근무코드 표(ShiftManage)가 E 를 D 군에 묶은 병동 — 엔진은 E 칸을 D 로 읽는다.
    """
    nurses = [{"nurse_id": "a", "sequence": 1}]
    manage = [{"main_code": "D", "codes": ["D", "E"]}]
    assert _translate([_engine_cell(0, 2, "E")], nurses, manage) == [_cell("a", 2, "D")]


def test_r10d1d_translation_does_not_mutate_engine_cells():
    """(가드) 번역은 원본 칸(엔진이 그대로 쓰는 `combined_fixed_cells`)을 건드리지 않는다."""
    nurses = [{"nurse_id": "a", "sequence": 1}]
    cells = [_engine_cell(0, 3, "OFF"), _engine_cell(0, 4, "연", "special_fixed", "휴가")]
    before = [dict(cell) for cell in cells]
    _translate(cells, nurses)
    assert cells == before


def test_r10d1e_translation_of_no_cells_is_empty():
    """(가드) 칸이 없으면 번역도 비어 있다(근무코드 표가 없어도)."""
    assert _translate([], [{"nurse_id": "a", "sequence": 1}], []) == []
    assert _translate(None, [{"nurse_id": "a", "sequence": 1}], None) == []


# ── bridge 가 칸을 읽는 방식 ───────────────────────────────────────────────


def _bridge(cells, nurses=None, **cfg):
    return build_precheck_input(nurses_dict=nurses or _nurses(3), config_dict=_cfg(**cfg),
                                grade_config=None, fixed_cells=cells, year=2026, month=11)


def test_r10d2_bridge_reads_engine_day_index():
    """✗ 엔진 형식 `day_index`(0-based)를 읽는다 — 전에는 `date`/`day` 만 읽어 칸을 버렸다."""
    inp = _bridge([_cell("n1", 4, "N"), _cell("n1", 9, "O")])
    assert inp.nurses[1].fixed_shift_assignments == {4: "N"}
    assert inp.nurses[1].fixed_off_days == {9}


def test_r10d2b_bridge_still_reads_legacy_day_formats_and_ignores_bad_days():
    """(가드) 기존 형식(`day` 1-based · `date`)은 그대로 읽고, 달 밖·깨진 일자는 버린다."""
    cells = [
        {"nurse_id": "n0", "day": 5, "shift": "N"},
        {"nurse_id": "n0", "date": "2026-11-06", "shift": "D"},
        {"nurse_id": "n0", "day_index": 30, "shift": "E"},
        {"nurse_id": "n0", "day_index": -1, "shift": "E"},
        {"nurse_id": "n0", "day_index": "x", "shift": "E"},
        {"nurse_id": "n0", "shift": "E"},
    ]
    assert _bridge(cells).nurses[0].fixed_shift_assignments == {4: "N", 5: "D"}


def test_r10d2d_bridge_day_name_precedence_is_date_then_day_index_then_day():
    """(가드) 한 칸에 일자 칸 이름이 여럿이면 `date`/`work_date` → `day_index`(엔진, 0-based) → `day`(1-based) 순이다."""
    cells = [
        {"nurse_id": "n0", "date": "2026-11-06", "day_index": 0, "day": 1, "shift": "D"},
        {"nurse_id": "n1", "day_index": 2, "day": 9, "shift": "E"},
        {"nurse_id": "n2", "work_date": "2026-11-12", "day": 1, "shift": "N"},
    ]
    inp = _bridge(cells)
    assert [n.fixed_shift_assignments for n in inp.nurses] == [{5: "D"}, {2: "E"}, {11: "N"}]


def test_r10d2c_engine_cells_flow_through_translation_into_bridge_inputs():
    """✗ 번역 칸이 bridge 를 지나 간호사별 고정 OFF·고정 근무가 된다(주휴 O · 고정 D/N)."""
    nurses = _nurses(2)
    cells = [_engine_cell(0, 5, "O", "weekly_off", "주휴"), _engine_cell(0, 12, "O", "weekly_off", "주휴"),
             _engine_cell(1, 3, "D"), _engine_cell(1, 4, "N")]
    inp = _bridge(_translate(cells, nurses), nurses)
    assert inp.nurses[0].fixed_off_days == {5, 12}
    assert inp.nurses[1].fixed_shift_assignments == {3: "D", 4: "N"}


# ── 판정이 바뀌는 사례 ────────────────────────────────────────────────────


def test_r10d3_fixed_day_for_night_only_nurse_blocks():
    """✗ N 전담 간호사의 확정 원티드 D — 허용 마스크(D 금지) × 고정 D 는 엔진 불가능(fallback_lex.py:2566-2666)."""
    nurses = _nurses()
    nurses[0]["allowed_shifts"] = ["N"]
    result, codes = _run(nurses, _cfg(), [_cell("n0", 4, "D")])
    assert ALLOWED in codes and has_blocking_issues(result)
    assert [i["evidence"]["nurse_id"] for i in result["issues"] if i["reason_code"] == ALLOWED] == ["n0"]


def test_r10d3b_fixed_night_and_fixed_off_for_night_only_nurse_pass():
    """(가드) N 전담에게 고정 N·고정 OFF 는 허용 마스크와 충돌하지 않는다."""
    nurses = _nurses()
    nurses[0]["allowed_shifts"] = ["N"]
    result, codes = _run(nurses, _cfg(), [_cell("n0", 4, "N"), _cell("n0", 9, "O")])
    assert ALLOWED not in codes and not has_blocking_issues(result)


def test_r10d4_four_fixed_nights_blocks():
    """✗ 고정 NNNN — 연속 N 상한 3(three_seq_nig)을 넘는다: 엔진 불가능(전에는 칸을 못 봐 통과했다)."""
    result, codes = _run(_nurses(), _cfg(), _cells("n0", {4: "N", 5: "N", 6: "N", 7: "N"}))
    assert SEQ in codes and has_blocking_issues(result)


def test_r10d4b_three_fixed_nights_pass():
    """(가드) 고정 NNN(상한 3) 은 엔진이 NNN 뒤 OO 로 푼다 — 막지 않는다(10c 와 짝)."""
    result, codes = _run(_nurses(), _cfg(), _cells("n0", {4: "N", 5: "N", 6: "N"}))
    assert SEQ not in codes and not has_blocking_issues(result)


def test_r10d6_both_fixed_n_to_d_is_exempt_like_engine():
    """(가드) 두 칸 다 고정이면 N→D 금지를 엔진이 면제한다(fallback_lex.py:2000) — 막지 않는다."""
    result, codes = _run(_nurses(), _cfg(), _cells("n0", {4: "N", 5: "D"}))
    assert SEQ not in codes and not has_blocking_issues(result)


def test_r10d5_fixed_overcoverage_is_warning_not_block():
    """✗ 수요 D 1명인 날 고정 D 3명 — 엔진은 고정 칸을 커버리지에서 빼고 초과분을 그대로 둔다
    (`need = 요구 − 고정`, fallback_lex.py:1732·1738). 불가능이 아니라 경고여야 한다."""
    cells = [_cell(f"n{i}", 4, "D") for i in range(3)]
    result, codes = _run(_nurses(), _cfg(), cells)
    assert OVER_NEED in codes and _severities(result, OVER_NEED) == {"warning"}
    assert not has_blocking_issues(result)


def test_r10d5b_direct_input_overcoverage_is_warning():
    """✗ 라우터 직접 입력(bridge 를 안 거침)도 같은 경고다 — 검사 자체가 하드였던 결함 재현."""
    inp = _direct_input(fixed_shift={f"n{i}": {4: "D"} for i in range(3)})
    result = run_precheck(inp)
    assert _severities(result, OVER_NEED) == {"warning"}
    assert not has_blocking_issues(result)


def test_r10d5c_fixed_off_over_whole_active_span_is_warning():
    """✗ 중도 입사자가 활동 구간 전부 고정 OFF — 엔진은 강제 OFF 가 상한을 넘으면 상한을 그 수로 올린다
    (`per_nurse_off_cap_override`, fallback_lex.py:1278-1280). 불가능이 아니라 경고여야 한다."""
    nurses = _nurses() + [{"nurse_id": "p0", "grade": 1, "allowed_shifts": [], "joining_date": "2026-11-21"}]
    cells = [_cell("p0", day, "O") for day in range(20, 30)]
    result, codes = _run(nurses, _cfg(), cells)
    assert OFF_SPAN in codes and _severities(result, OFF_SPAN) == {"warning"}
    assert not has_blocking_issues(result)


def test_r10d5d_direct_input_fixed_off_span_is_warning():
    """✗ 라우터 직접 입력의 FIXED_OFF_EXCEEDS_SPAN 도 경고다."""
    inp = _direct_input(joins={"n0": 20}, fixed_off={"n0": range(20, 30)})
    result = run_precheck(inp)
    assert _severities(result, OFF_SPAN) == {"warning"}
    assert not has_blocking_issues(result)


def test_r10d5e_downgraded_checks_no_longer_stop_the_config_phase():
    """✗ 경고로 내린 두 검사는 설정성 오류가 아니다 — `stop_on_config_error=True` 여도 일자별 검사가 돈다.

    수요 12 > 인원 10 이라 일자별 부족(GLOBAL_DAY_CAPACITY_SHORTAGE)은 늘 있다. 전에는 초과 고정(설정성 오류)이
    조기 중단시켜 이 이슈가 사라졌다.
    """
    cfg = {"daily_shift_requirements": {"D": 4, "E": 4, "N": 4}}
    inp = _direct_input(fixed_shift={f"n{i}": {4: "D"} for i in range(5)}, **cfg)
    codes = {issue["reason_code"] for issue in run_precheck(inp, stop_on_config_error=True)["issues"]}
    assert OVER_NEED in codes and DAYCAP in codes


def test_r10d5f_violates_allowed_still_stops_the_config_phase():
    """(가드) 허용 근무 위반은 여전히 설정성 오류다(엔진 불가능 — 조기 중단 대상)."""
    inp = _direct_input(fixed_shift={"n0": {4: "D"}}, daily_shift_requirements={"D": 4, "E": 4, "N": 4})
    inp.nurses[0].allowed_shifts = ["N"]
    codes = {issue["reason_code"] for issue in run_precheck(inp, stop_on_config_error=True)["issues"]}
    assert ALLOWED in codes and DAYCAP not in codes


def test_r10d7_preceptee_cells_are_dropped():
    """✗ 프리셉티 고정 칸은 뺀다 — 엔진은 팔로우 날 프리셉티 고정 칸을 걸지 않는다(fallback_lex.py:1315).

    날짜별 팔로우는 precheck 가 몰라 그 간호사 칸 전부를 뺀다(엔진보다 느슨한 쪽).
    """
    nurses = _nurses()
    nurses[5]["preceptor_id"] = "n0"
    cells = _cells("n5", {4: "N", 5: "N", 6: "N", 7: "N", 9: "O"}) + _cells("n0", {4: "D"})
    inp = _bridge(cells, nurses)
    assert inp.nurses[5].fixed_shift_assignments == {} and inp.nurses[5].fixed_off_days == set()
    assert inp.nurses[0].fixed_shift_assignments == {4: "D"}


def test_r10d7b_preceptee_by_period_map_is_decided_like_preceptor_id():
    """✗ 프리셉티 판정은 `preceptor_id` 해석(period 권위 우선)과 같은 규칙 한 벌이다.

    권위 모드: 그 달 period 맵에 있으면 프리셉티(캐시가 비어도), 없으면 캐시가 있어도 프리셉티가 아니다(종료월).
    """
    nurses = _nurses()
    nurses[5]["preceptor_id"] = "n0"                        # 캐시는 프리셉티지만
    nurses[6]["preceptor_id"] = None                        # 캐시에는 없지만
    cfg = {"preceptee_period_authoritative": True, "preceptee_period_by_nurse_id": {"n6": {"preceptor_id": "n1"}}}
    cells = _cells("n5", {4: "D"}) + _cells("n6", {4: "D"})
    inp = _bridge(cells, nurses, **cfg)
    assert inp.nurses[5].fixed_shift_assignments == {4: "D"}      # 종료월 — 칸 유지
    assert inp.nurses[6].fixed_shift_assignments == {}            # period 가 프리셉티로 정함 — 칸 제외


def test_r10d8_fixed_off_reduces_daily_availability_like_engine():
    """✗ 고정 OFF 칸은 그 날 D/E/N 을 못 맡게 하드로 묶는다(엔진 고정 칸). 기존 정책인 일자별 가용 인원 검사가
    이제 이 칸을 받는다 — 전에는 칸이 버려져 같은 입력이 통과했다.

    4명·하루 3명 필요: 10일에 2명이 고정 OFF 면 가용 2 < 필요 3.
    """
    cells = [_cell("n0", 10, "O"), _cell("n1", 10, "O")]
    result, codes = _run(_nurses(4), _cfg(), cells)
    days = sorted(i["evidence"]["day"] for i in result["issues"] if i["reason_code"] == DAYCAP)
    assert days == [10] and has_blocking_issues(result)


def test_r10d9_run_does_not_mutate_input_cells():
    """(가드) precheck 를 돌려도 입력 칸 목록·칸은 그대로다."""
    cells = _cells("n0", {4: "N", 5: "D"})
    before = [dict(cell) for cell in cells]
    _run(_nurses(), _cfg(), cells)
    assert cells == before


@pytest.mark.parametrize("fixed_off", [{4, 9}, set()])
def test_r10d9b_weekend_off_nurse_still_gets_weekends_marked_off(fixed_off):
    """주말휴무자의 주말 고정 OFF 표시(기존 동작)는 칸이 들어와도 합쳐질 뿐 사라지지 않는다.

    ✗ 칸이 있으면 주말 표시와 합쳐진다(전에는 칸이 버려졌다) · (가드) 칸이 없으면 주말 표시만이다.
    """
    nurses = _nurses(3)
    nurses[0]["is_weekend_off"] = True
    inp = _bridge([_cell("n0", d, "O") for d in fixed_off], nurses, weekend_off_only_enable=True)
    weekend = {d for d in range(30) if d % 7 in (0, 6)}   # 2026-11-01 은 일요일
    assert inp.nurses[0].fixed_off_days == weekend | fixed_off


# ── 리뷰 지적(10d HIGH) — 배정 차단일 위의 고정 칸은 엔진이 건너뛴다 ────────────────────────────────────────
#
# 파견·휴직·퇴사·병동이동 배정 기간(`config_dict["blocked_by_nurse_id"]`)은 엔진의 활동일에서 빠진다. lex 는 활동일 밖 고정 칸을
# 건너뛰는데(fallback_lex.py:1311) 10d 번역은 그 칸을 precheck 에 그대로 넘겨 FIXED_ASSIGN_VIOLATES_ALLOWED·PER_NURSE_SEQUENCE_
# INFEASIBLE 로 막았다 — 10d 전에는 통과하던 생성을 막는 새 오차단이다. 엔진 판정은 독립 리뷰가 실제 엔진 stage1 으로 쟀다
# (E1a·E1b·E1c 모두 엔진 OPTIMAL · 대조군[차단일 없음]은 엔진·precheck 모두 불가능).


def _blocked_run(nurses, cfg, engine_cells, blocked):
    cells = _translate(engine_cells, nurses, blocked=blocked)
    return _run(nurses, cfg, cells), cells


def test_r10d10_cell_on_a_blocked_day_is_dropped_so_night_only_nurse_is_not_blocked():
    """✗ E1a — N 전담, 차단일 {1,2,3}, 2일 확정 원티드 D: 엔진은 그 칸을 건너뛰어 가능. 칸이 남으면 허용 마스크 위반으로 막힌다."""
    nurses = _nurses(10)
    nurses[0]["allowed_shifts"] = ["N"]
    engine_cells = [_engine_cell(0, 2, "D")]
    ((result, codes), cells) = _blocked_run(nurses, _cfg(), engine_cells, {"n0": {1, 2, 3}})
    assert cells == [] and ALLOWED not in codes and not has_blocking_issues(result)


def test_r10d10b_control_without_blocked_days_still_blocks_night_only_nurse():
    """(가드) 대조군 — 차단일이 없으면 같은 칸은 엔진도 불가능이라 precheck 도 막는다(r10d3 과 같다)."""
    nurses = _nurses(10)
    nurses[0]["allowed_shifts"] = ["N"]
    ((result, codes), cells) = _blocked_run(nurses, _cfg(), [_engine_cell(0, 2, "D")], None)
    assert cells == [_cell("n0", 2, "D")] and ALLOWED in codes and has_blocking_issues(result)


def test_r10d10c_single_night_on_blocked_days_is_not_a_sequence_conflict():
    """✗ E1b — 1N 금지, 차단일 {9,10,11} 위 O·N·O 고정: 엔진은 그 칸들을 건너뛴다. 남기면 단독 N 이라 PER_NURSE 로 막는다."""
    engine_cells = [_engine_cell(0, 9, "OFF"), _engine_cell(0, 10, "N"), _engine_cell(0, 11, "OFF")]
    ((result, codes), cells) = _blocked_run(_nurses(10), _cfg(not_one_night=True), engine_cells, {"n0": {9, 10, 11}})
    assert cells == [] and SEQ not in codes and not has_blocking_issues(result)
    ((_r, control), _c) = _blocked_run(_nurses(10), _cfg(not_one_night=True), engine_cells, None)
    assert SEQ in control  # 대조군: 차단일이 없으면 엔진도 불가능


def test_r10d10d_one_blocked_day_inside_a_night_run_breaks_the_run():
    """✗ E1c — 연속 N 상한 3 에서 NNNN(10~13일) 중 12일이 차단일이면 엔진은 그 칸만 건너뛰어 N N _ N 이라 가능하다."""
    engine_cells = [_engine_cell(0, day, "N") for day in (10, 11, 12, 13)]
    ((result, codes), cells) = _blocked_run(_nurses(10), _cfg(), engine_cells, {"n0": {12}})
    assert [c["day_index"] for c in cells] == [10, 11, 13] and SEQ not in codes
    ((_r, control), _c) = _blocked_run(_nurses(10), _cfg(), engine_cells, None)
    assert SEQ in control  # 대조군: 차단일이 없으면 NNNN 은 엔진도 불가능


def test_r10d10e_only_the_blocked_nurse_and_the_blocked_days_lose_cells():
    """(가드) 차단일은 그 간호사의 그 날 칸만 버린다 — 같은 날 다른 간호사 칸·같은 간호사의 다른 날 칸은 그대로다.

    차단일 값이 비정상(None·정수·문자열)이면 무시하고 칸을 그대로 둔다(엔진 값이 아니므로 판정을 가리지 않는다).
    """
    nurses = _nurses(3)
    engine_cells = [_engine_cell(0, 5, "D"), _engine_cell(1, 5, "D"), _engine_cell(0, 6, "N")]
    assert _translate(engine_cells, nurses, blocked={"n0": {5}}) == [_cell("n1", 5, "D"), _cell("n0", 6, "N")]
    full = [_cell("n0", 5, "D"), _cell("n1", 5, "D"), _cell("n0", 6, "N")]
    for malformed in (None, {}, {"n0": None}, {"n0": 5}, {"n0": "5"}, {"other": {5}}):
        assert _translate(engine_cells, nurses, blocked=malformed) == full


def test_r10d10f_blocked_day_list_and_tuple_forms_are_accepted():
    """(가드) 차단일은 set(생성 경로) · list · tuple 어느 모양이든 같게 읽는다(스냅샷 복원·직접 호출 호환)."""
    nurses = _nurses(2)
    for days in ({5}, [5], (5,), frozenset({5})):
        assert _translate([_engine_cell(0, 5, "D")], nurses, blocked={"n0": days}) == []


def test_r10d11_fixed_cell_outside_the_active_span_is_not_an_allowed_violation():
    """✗ 11/25 퇴사자(구간 0~23)의 11/27(26일) 고정 N — 엔진은 활동일 밖 칸을 건너뛰어 가능하다. 허용 위반 검사도 구간 안 칸만 본다.

    개인 DP 는 이미 구간 안 칸만 봤다. 이 검사만 구간 밖 칸을 봐서 직접 호출에서 새 오차단이 났다(생성 경로는 rcs 가 구간으로
    칸을 미리 거른다). 구간 안 같은 칸은 여전히 위반이다.
    """
    def issues(day):
        inp = _direct_input(n=2, num_days=30, fixed_shift={"n0": {day: "N"}}, joins={})
        inp.nurses[0].allowed_shifts = ["D", "E"]
        inp.nurses[0].leave_day = 23
        return {i["reason_code"] for i in run_precheck(inp)["issues"]}

    assert ALLOWED not in issues(26)
    assert ALLOWED in issues(19)


def test_r10d12_a_later_cell_that_cannot_be_translated_erases_the_earlier_one():
    """✗ 같은 (간호사, 날)에 칸이 겹치면 엔진은 마지막 칸이 이긴다 — 뒤 칸이 표 없는 비표준 코드(D1)라 번역하지 못하면 앞 칸(주휴 O)을
    남기지 않는다('모름' = 느슨한 쪽). 남기면 precheck 만 그 날을 OFF 로 본다."""
    nurses = _nurses(2)
    weekly_off = _engine_cell(0, 3, "주", "weekly_off", "주휴")
    assert _translate([weekly_off, _engine_cell(0, 3, "D1")], nurses) == []
    assert _translate([weekly_off], nurses) == [_cell("n0", 3, "O")]  # 대조군: 뒤 칸이 없으면 주휴 O 가 남는다


def test_r10d12b_a_later_translatable_cell_wins_and_order_is_first_occurrence():
    """(가드) 뒤 칸이 번역되면 그 칸이 이긴다(엔진: 덮어쓰기). 출력 순서는 각 (간호사, 날)이 처음 나온 순서다."""
    nurses = _nurses(2)
    cells = [_engine_cell(0, 3, "OFF"), _engine_cell(1, 4, "D"), _engine_cell(0, 3, "N")]
    assert _translate(cells, nurses) == [_cell("n0", 3, "N"), _cell("n1", 4, "D")]
    assert _translate([_engine_cell(0, 3, "N"), _engine_cell(0, 3, "OFF")], nurses) == [_cell("n0", 3, "O")]


def test_r10d12c_unmodeled_later_cell_does_not_erase_a_different_day():
    """(가드) 뒤 칸을 번역하지 못해도 다른 날·다른 간호사의 칸은 그대로다."""
    nurses = _nurses(2)
    cells = [_engine_cell(0, 3, "N"), _engine_cell(0, 4, "D1"), _engine_cell(1, 3, "D")]
    assert _translate(cells, nurses) == [_cell("n0", 3, "N"), _cell("n1", 3, "D")]


# ── 리뷰 지적(LOW) — 경고로 내린 두 코드를 생성 차단 집합에서 뺀다(동작 불변) ───────────────────────────────


def test_r10d13_lowered_codes_are_out_of_the_blocking_set_and_never_block():
    """FIXED_OFF_EXCEEDS_SPAN·FIXED_ASSIGN_EXCEEDS_NEED 는 10d 부터 경고라 `payload._BLOCKING_CODES` 에서 뺐다 — 차단 여부는 전과 같다.

    `has_blocking_issues` 는 severity(hard/blocking)도 함께 보므로 경고인 두 코드는 집합에 있어도 막지 못했다. 집합에서 빼도
    실제 결과(r10d5·r10d5c 와 같은 입력)는 여전히 차단하지 않고, 하드 코드는 그대로 차단한다.
    """
    from services.precheck.payload import _BLOCKING_CODES

    assert not {OVER_NEED, OFF_SPAN} & _BLOCKING_CODES
    assert {ALLOWED, SEQ, DAYCAP, "CAPACITY_TOTAL_SHORTAGE"} <= _BLOCKING_CODES
    over = run_precheck(_direct_input(fixed_shift={f"n{i}": {4: "D"} for i in range(3)}))
    span = run_precheck(_direct_input(joins={"n0": 20}, fixed_off={"n0": range(20, 30)}))
    assert _severities(over, OVER_NEED) == {"warning"} and not has_blocking_issues(over)
    assert _severities(span, OFF_SPAN) == {"warning"} and not has_blocking_issues(span)
    assert has_blocking_issues({"issues": [{"reason_code": ALLOWED, "severity": "hard"}]})
