"""EI-10f 결과 테스트 — precheck 가 간호사의 근무 가능 구간을 엔진과 같게 읽어 판정이 바뀌는 사례.

배경(결정 D8 · 설계서 §2.6): `runtime_bridge.build_precheck_input` 은 퇴사일(`resignation_date`) 당일을 근무일로 셌다
(`_to_day_index` — 퇴사일의 일자 인덱스를 `leave_day` 로). 엔진은 퇴사일 그날부터 소속이 아니라서 마지막 근무일을
`resignation_date − 1` 로 본다(`cp_sat_basic.py:2585-2611` · `fallback_lex.py:716-734` · `roster_create_service.
_active_range_in_month` · `cp_sat.feasibility_alerts._active_range`). 그 달과 겹치지 않는 입퇴사(지난 달 퇴사 · 다음 달
입사)도 엔진은 근무 구간이 없는 것으로 보는데, precheck 는 '다른 달이면 None → 월 전체'로 읽었다. 10f 는 규칙을 다시
적지 않고 엔진 쪽 같은 함수를 그대로 불러 같은 구간을 얻는다. 입력 날짜는 이미 메모리에 있다(조회 0).

각 테스트의 '엔진 판정'은 마지막 근무일이 퇴사일 전날이라는 의미로 따진 값이다. ✗ = 10f 전 코드에서 빨강(결함 재현),
(가드) = 전/후 모두 초록이지만 변이·퇴행을 잡는 불변 확인이다. 시나리오 달은 2026-11(30일). 구간은 0-based 양끝 포함이고
근무일이 없으면 엔진처럼 (1, 0) 이다(`cp_sat_basic.py:2597` `join=1 · leave=0`).
"""

from __future__ import annotations

from datetime import date, datetime

import pytest
from services.ontology_pool import build_pool_snapshot_from_runtime
from services.precheck import run_runtime_precheck
from services.precheck.payload import has_blocking_issues
from services.precheck.runtime_bridge import build_precheck_input
from services.precheck.team_grade_precheck import _working_capacity, run_precheck

CAP = "CAPACITY_TOTAL_SHORTAGE"
DAYCAP = "GLOBAL_DAY_CAPACITY_SHORTAGE"
SEQ = "PER_NURSE_SEQUENCE_INFEASIBLE"
EMPTY = (1, 0)  # 근무일이 없는 구간 — 엔진 표기와 같다


def _cfg(**extra):
    """엔진 기본에 가까운 설정 — 연속근무 상한은 운영에 많은 6."""
    base = {
        "daily_shift_requirements": {"D": 1, "E": 1, "N": 1}, "max_conseq_work": 6,
        "max_nig_per_month": 15, "ban_shift_transitions": True, "allow_three_consecutive_nights": True,
        "two_offs_after_three_nig": True, "two_offs_after_two_nig": False, "ban_single_night": False,
        "use_mid": False, "off_days": 8, "shift_priority": 0.7,
        # EI-14r3 — 이 파일은 연속근무·주휴·퇴사 용량을 따로 잰다. OFF 하드 하한(min_off — staffing_first=False 병동)은 별개 제약이라 끈다
        # (그 하한은 test_precheck_min_off_floor.py · test_precheck_vs_engine_stage1.py::test_s7e* 가 본다).
        "staffing_first": True,
    }
    base.update(extra)
    return base


def _nurses(n=3, **extra):
    return [{"nurse_id": f"n{i}", "grade": 1, "allowed_shifts": [], "sequence": i, **extra} for i in range(n)]


def _build(nurses, cfg, cells=None):
    return build_precheck_input(
        nurses_dict=nurses, config_dict=cfg, grade_config=None, fixed_cells=cells, year=2026, month=11)


def _span(joining=None, resignation=None):
    """간호사 한 명의 precheck 활동 구간 `(join_day, leave_day)`."""
    nurse = {"nurse_id": "n0", "grade": 1, "allowed_shifts": [], "joining_date": joining, "resignation_date": resignation}
    got = _build([nurse], _cfg()).nurses[0]
    return got.join_day, got.leave_day


def _run(nurses, cfg, cells=None):
    result = run_runtime_precheck(
        nurses_dict=nurses, config_dict=cfg, grade_config=None, fixed_cells=cells,
        year=2026, month=11, stop_on_config_error=False)
    return result, {issue["reason_code"] for issue in result["issues"]}


# ── 활동 구간: 퇴사일 −1 ─────────────────────────────────────────────────────


def test_r10f1_resignation_day_is_not_a_work_day():
    """✗ 11/20 퇴사 — 마지막 근무일은 11/19(인덱스 18)다. 전에는 퇴사일 당일(인덱스 19)까지 근무로 셌다."""
    assert _span(resignation=datetime(2026, 11, 20)) == (0, 18)


@pytest.mark.parametrize("resignation", [
    datetime(2026, 11, 20), date(2026, 11, 20), "2026-11-20", "2026-11-20T09:30:00", datetime(2026, 11, 20, 23, 59),
])
def test_r10f1b_every_date_form_reads_the_same_last_work_day(resignation):
    """✗ 생성 경로는 DB datetime, 직접 호출·스냅샷은 date·ISO 문자열이다 — 어느 모양이든 같은 마지막 근무일이다."""
    assert _span(resignation=resignation) == (0, 18)


def test_r10f1c_resignation_on_the_last_day_of_the_month():
    """✗ 11/30 퇴사 — 마지막 근무일은 11/29(인덱스 28)다."""
    assert _span(resignation=datetime(2026, 11, 30)) == (0, 28)


def test_r10f1d_resignation_on_the_second_day_leaves_one_work_day():
    """✗ 11/2 퇴사 — 11/1 하루만 근무한다(전에는 11/2 까지 이틀)."""
    assert _span(resignation=datetime(2026, 11, 2)) == (0, 0)


def test_r10f1e_resignation_on_the_first_day_means_no_work_day():
    """✗ 11/1 퇴사 — 마지막 근무일이 10/31 이라 이 달에는 근무가 없다(엔진 `join=1 · leave=0`). 전에는 1일을 근무로 셌다.

    생성 경로는 이런 간호사를 엔진 명단에서 미리 뺀다(`_active_range_in_month` 가 None) — 직접 호출·풀 그래프용 값이다.
    """
    assert _span(resignation=datetime(2026, 11, 1)) == EMPTY


@pytest.mark.parametrize("resignation", [datetime(2026, 12, 1), datetime(2026, 12, 15), date(2027, 3, 1)])
def test_r10f1f_resignation_from_next_month_keeps_the_full_month(resignation):
    """(가드) 12/1 퇴사면 마지막 근무일이 11/30 이라 이 달은 전부 근무한다 — 다음 달 이후 퇴사도 월 전체."""
    assert _span(resignation=resignation) == (0, 29)


@pytest.mark.parametrize("resignation", [datetime(2026, 10, 15), "2026-10-31", date(2025, 11, 20)])
def test_r10f1g_resigned_before_the_month_has_no_work_day(resignation):
    """✗ 지난 달(지난해 같은 달 포함) 퇴사 — 이 달에 근무가 없다. 전에는 '다른 달이면 월 전체'로 읽었다."""
    assert _span(resignation=resignation) == EMPTY


@pytest.mark.parametrize("joining", [datetime(2026, 12, 1), datetime(2026, 12, 5), "2026-12-05", date(2027, 1, 3)])
def test_r10f1h_joining_after_the_month_has_no_work_day(joining):
    """✗ 다음 달 이후 입사 — 이 달에 근무가 없다(엔진은 `join > leave`). 전에는 월 전체를 근무로 읽었다."""
    assert _span(joining=joining) == EMPTY


def test_r10f1i_joining_and_resigning_in_the_same_month():
    """✗ 11/5 입사 · 11/20 퇴사 — 11/5(인덱스 4) ~ 11/19(인덱스 18)."""
    assert _span(joining=datetime(2026, 11, 5), resignation=datetime(2026, 11, 20)) == (4, 18)


@pytest.mark.parametrize("joining, expected", [
    (datetime(2026, 11, 10), (9, 29)), ("2026-11-10", (9, 29)), (datetime(2026, 11, 1), (0, 29)),
    (datetime(2024, 3, 1), (0, 29)), (date(2026, 10, 31), (0, 29)), (None, (0, 29)),
])
def test_r10f1j_joining_inside_or_before_the_month_is_unchanged(joining, expected):
    """(가드) 입사일이 이 달 안이면 그날부터, 이전이거나 없으면 월초부터 — 전/후 같다."""
    assert _span(joining=joining) == expected


@pytest.mark.parametrize("unreadable", ["", "abc", "2026-13-45", 20261120, 3.5, [], {}])
def test_r10f1k_unreadable_dates_mean_no_date(unreadable):
    """(가드) 읽을 수 없는 값은 날짜가 없는 것으로 본다(입사일도 퇴사일도) — 월 전체 근무, precheck 는 죽지 않는다."""
    assert _span(joining=unreadable, resignation=unreadable) == (0, 29)


def test_r10f1l_every_nurse_gets_their_own_span():
    """✗ 간호사마다 자기 날짜로 구간을 짓는다 — 한 명의 입퇴사가 다른 사람의 구간을 건드리지 않는다."""
    nurses = _nurses(3)
    nurses[0]["resignation_date"] = datetime(2026, 11, 20)
    nurses[1]["joining_date"] = datetime(2026, 11, 10)
    spans = [(nurse.join_day, nurse.leave_day) for nurse in _build(nurses, _cfg()).nurses]
    assert spans == [(0, 18), (9, 29), (0, 29)]


# ── 판정 변화(일자별 · 하드 용량) ────────────────────────────────────────────


def _day_shortage_days(result):
    return sorted(issue["evidence"]["day"] for issue in result["issues"] if issue["reason_code"] == DAYCAP)


def test_r10f2_day_capacity_runs_short_from_the_engine_last_work_day():
    """✗ 4명 · 하루 4명 필요 · 한 명이 11/20 퇴사 — 11/20(인덱스 19)부터 가용 3명이라 그날부터 부족하다.

    엔진 판정: 마지막 근무일이 11/19 라 11/20 부터 한 명이 없다. 전에는 퇴사일 당일을 근무로 세어 11/21(인덱스 20)부터
    부족하다고 봤다 — 퇴사일 하루 공급을 과대로 본 것.
    """
    nurses = _nurses(4)
    nurses[3]["resignation_date"] = datetime(2026, 11, 20)
    result, _ = _run(nurses, _cfg(daily_shift_requirements={"D": 2, "E": 1, "N": 1}))
    shortage_days = _day_shortage_days(result)
    assert shortage_days and shortage_days[0] == 19 and shortage_days == list(range(19, 30))


def test_r10f2c_month_end_resignation_runs_the_last_day_short():
    """✗ 4명 · 하루 4명 필요 · 한 명이 11/30(말일) 퇴사 — 마지막 날(인덱스 29) 하루만 가용이 3명이라 그날만 부족하다.

    엔진 판정: 마지막 근무일이 11/29 라 11/30 에는 그 간호사가 없다. 월말 퇴사는 흔한 경우인데, 전에는 퇴사일을 근무로 세어
    말일까지 4명이 있다고 보고 이 부족을 못 봤다(퇴사일 하루만 걸리는 경우라 뒤 날짜가 없어 다른 날로 잡히지도 않았다).
    """
    nurses = _nurses(4)
    nurses[3]["resignation_date"] = datetime(2026, 11, 30)
    result, _ = _run(nurses, _cfg(daily_shift_requirements={"D": 2, "E": 1, "N": 1}))
    assert _day_shortage_days(result) == [29]


def test_r10f2b_no_resigner_means_no_day_shortage_at_all():
    """(가드) 같은 입력에서 퇴사자가 없으면 일자별 부족이 없다."""
    result, _ = _run(_nurses(4), _cfg(daily_shift_requirements={"D": 2, "E": 1, "N": 1}))
    assert _day_shortage_days(result) == []


def _capacity_cfg(**extra):
    """6명 용량 시나리오 — 하루 필요 5명인데 첫 두 날만 4명이라 월 필요 148 (30일 · 연속근무 6)."""
    by_day = [{"D": 2, "E": 1, "N": 1} if day < 2 else {"D": 2, "E": 2, "N": 1} for day in range(30)]
    return _cfg(daily_shift_requirements={"D": 2, "E": 2, "N": 1}, daily_shift_requirements_by_day=by_day, **extra)


def test_r10f3_resignation_tightens_total_capacity_like_engine(monkeypatch):
    """✗ 한 명이 11/20 퇴사 — 그 간호사의 월 최대 근무가 18 → 17 이라 전체 용량(147)이 수요(148)에 못 미친다.

    엔진 판정: 불가능. 마지막 근무일이 11/19 라 19일 구간에서 연속근무 6 이하면 7일 창이 겹치지 않는 둘에 OFF 가 필요해
    최대 근무는 19−2=17 이고 나머지 5명은 30−4=26 이다(17+5×26=147 < 148). 전에는 퇴사일을 근무로 세어 20일 구간(20−2=18)
    →148 ≥ 148 로 통과했다.
    """
    monkeypatch.delenv("AIDE_MAX_CONSEC_WORK", raising=False)  # 연속근무 상한은 설정 값 6 만 쓴다
    nurses = _nurses(6)
    nurses[5]["resignation_date"] = datetime(2026, 11, 20)
    result, codes = _run(nurses, _capacity_cfg())
    assert CAP in codes and has_blocking_issues(result)


def test_r10f3b_same_input_without_resignation_passes(monkeypatch):
    """(가드) 퇴사자가 없으면 용량 156 ≥ 수요 148 로 통과한다."""
    monkeypatch.delenv("AIDE_MAX_CONSEC_WORK", raising=False)
    result, codes = _run(_nurses(6), _capacity_cfg())
    assert CAP not in codes and not has_blocking_issues(result)


def test_r10f3c_need_equal_to_capacity_passes(monkeypatch):
    """(가드) 수요가 용량과 딱 같으면(147=147) 통과한다 — 퇴사일을 하루라도 근무로 세면 거꾸로 빨개지는 경계 확인."""
    monkeypatch.delenv("AIDE_MAX_CONSEC_WORK", raising=False)
    nurses = _nurses(6)
    nurses[5]["resignation_date"] = datetime(2026, 11, 20)
    cfg = _capacity_cfg()
    cfg["daily_shift_requirements_by_day"][2] = {"D": 2, "E": 1, "N": 1}  # 셋째 날 수요 5 → 4: 합 147
    result, codes = _run(nurses, cfg)
    assert CAP not in codes and not has_blocking_issues(result)


def test_r10f3d_resignation_next_month_does_not_tighten_capacity(monkeypatch):
    """(가드) 12/1 퇴사는 이 달 전체 근무라 용량 156 그대로 — 수요 148 통과(퇴사 날짜의 달을 잘못 읽으면 빨개진다)."""
    monkeypatch.delenv("AIDE_MAX_CONSEC_WORK", raising=False)
    nurses = _nurses(6)
    nurses[5]["resignation_date"] = datetime(2026, 12, 1)
    result, codes = _run(nurses, _capacity_cfg())
    assert CAP not in codes and not has_blocking_issues(result)


def test_r10f4_pool_graph_capacity_follows_the_same_engine_last_work_day():
    """✗ PoolGraph(`build_pool_snapshot_from_runtime`)도 같은 입구(`build_precheck_input`)를 타서 퇴사일 하루가 풀 용량에서 빠진다.

    4명 × 30일 = 120 이던 공통풀 용량이 한 명의 11/20 퇴사로 줄어 3×30+19=109 다(전에는 퇴사일을 세어 110).
    """
    nurses = _nurses(4)

    def _common_pool_capacity(resignation):
        nurses[0]["resignation_date"] = resignation
        snapshot = build_pool_snapshot_from_runtime(
            nurses_dict=nurses, config_dict=_cfg(), grade_config=None, fixed_cells=None, year=2026, month=11)
        pools = {pool["pool_id"]: pool["attrs"]["monthly_capacity"] for pool in snapshot.to_dict()["pools"]}
        return pools["common_pool:D"]

    assert _common_pool_capacity(None) == 120
    assert _common_pool_capacity(datetime(2026, 11, 20)) == 109


def _ward_with_n0(**n0_extra):
    """10명 병동(하루 3명 필요 — 다른 검사가 막지 않는다) · n0 에게만 덮어쓰기."""
    nurses = _nurses(10)
    nurses[0].update(n0_extra)
    return nurses


def _empty_span_nurses():
    """이 달 근무가 없는 간호사 셋 — 11/1 퇴사 · 12/5 입사 · 지난 달 퇴사한 N 전담(팀 있음)."""
    return [
        {"nurse_id": "x1", "grade": 1, "allowed_shifts": [], "sequence": 11, "resignation_date": datetime(2026, 11, 1)},
        {"nurse_id": "x2", "grade": 1, "allowed_shifts": [], "sequence": 12, "joining_date": datetime(2026, 12, 5)},
        {"nurse_id": "x3", "grade": 1, "allowed_shifts": ["N"], "sequence": 13, "team_id": 3,
         "resignation_date": datetime(2026, 10, 1)},
    ]


def test_r10f7_empty_span_nurses_add_nothing_and_break_nothing():
    """✗ 근무 구간이 없는 간호사가 섞여도 모든 검사가 예외 없이 돌고 용량 0 — 있어도 없는 것과 같은 결과다.

    구간이 `(1, 0)` 이면 활동일이 하나도 없다(`team_grade_precheck` 의 범위 검사가 전부 거른다). 전에는 이 셋이 월 전체
    근무자(용량 26)나 하루 근무자(용량 1)로 잡혀 유령 공급이 되었다. 생성 경로는 이런 간호사를 엔진 명단에서 미리 빼므로
    직접 호출·풀 그래프용이다.
    """
    base = _ward_with_n0()
    cells = [{"nurse_id": "x1", "day_index": 0, "shift": "N"}]  # 구간 밖 고정 칸 — 판정에 쓰이지 않는다
    with_empty = base + _empty_span_nurses()
    inp = _build(with_empty, _cfg(ban_single_night=True), cells)
    empty = [nurse for nurse in inp.nurses if nurse.nurse_id.startswith("x")]
    assert [(nurse.join_day, nurse.leave_day) for nurse in empty] == [EMPTY] * 3
    assert [_working_capacity(nurse, inp.roster_config) for nurse in empty] == [0, 0, 0]
    reference = run_precheck(_build(base, _cfg(ban_single_night=True)))  # 예외 없이 끝까지 돈다(run_runtime_precheck 는 삼킨다)
    assert {i["reason_code"] for i in run_precheck(inp)["issues"]} == {i["reason_code"] for i in reference["issues"]}


def test_r10f7b_pool_graph_ignores_empty_span_nurses():
    """✗ PoolGraph 도 구간 없는 간호사를 풀 용량에 넣지 않는다 — 있어도 없는 것과 같은 공통풀 용량이다."""
    base = _ward_with_n0()

    def _common_pool_capacity(nurses):
        snapshot = build_pool_snapshot_from_runtime(
            nurses_dict=nurses, config_dict=_cfg(), grade_config=None, fixed_cells=None, year=2026, month=11)
        pools = {pool["pool_id"]: pool["attrs"]["monthly_capacity"] for pool in snapshot.to_dict()["pools"]}
        return pools["common_pool:D"]

    assert _common_pool_capacity(base + _empty_span_nurses()) == _common_pool_capacity(base) == 300


# ── 10d(고정 칸)와 10f(활동 구간)의 만남 ─────────────────────────────────────


def _single_night_cells(night_day):
    """n0 의 직전 날 고정 OFF · `night_day` 고정 N(0-based) — 앞뒤에 N 이웃이 없으면 1N 이다."""
    return [{"nurse_id": "n0", "day_index": night_day - 1, "shift": "O"},
            {"nurse_id": "n0", "day_index": night_day, "shift": "N"}]


def test_r10f5_fixed_night_on_the_resignation_day_is_outside_the_engine_range():
    """✗ 11/10 퇴사 · 11/10(인덱스 9)에 고정 N · 11/9 고정 OFF · 1N 금지 — 엔진은 활동 구간 밖 고정 칸을 걸지 않는다.

    엔진 판정: 가능. 마지막 근무일이 11/9(인덱스 8)라 11/10 의 고정 N 은 건너뛰고(`fallback_lex.py:1311`) 11/9 는 OFF 다.
    전에는 퇴사일을 구간에 넣어 그 고정 N 이 구간 끝의 단독 N(1N)으로 보여 PER_NURSE_SEQUENCE_INFEASIBLE 로 막았다(오차단).
    """
    nurses = _ward_with_n0(resignation_date=datetime(2026, 11, 10))
    result, codes = _run(nurses, _cfg(ban_single_night=True), _single_night_cells(9))
    assert SEQ not in codes and not has_blocking_issues(result)


def test_r10f5b_fixed_night_on_the_new_last_work_day_blocks_like_engine():
    """✗ 11/11 퇴사 — 같은 고정 N(11/10, 인덱스 9)이 이제 마지막 근무일이라 구간 끝의 단독 N(1N)이다. 엔진도 불가능.

    전에는 퇴사일(11/11, 인덱스 10)이 구간에 있어 N 을 하루 더 이을 수 있다고 보고 통과시켰다(놓침).
    """
    nurses = _ward_with_n0(resignation_date=datetime(2026, 11, 11))
    result, codes = _run(nurses, _cfg(ban_single_night=True), _single_night_cells(9))
    assert SEQ in codes and has_blocking_issues(result)


def test_r10f5c_same_cells_without_resignation_pass():
    """(가드) 퇴사자가 없으면 11/10 고정 N 뒤에 11/11 N 을 이을 수 있어 가능하다 — 전/후 통과."""
    result, codes = _run(_ward_with_n0(), _cfg(ban_single_night=True), _single_night_cells(9))
    assert SEQ not in codes and not has_blocking_issues(result)


# ── 리뷰 지적(10f HIGH·MEDIUM) — 주말휴무자의 주말 감산은 플래그·근무 구간을 따른다 ────────────────────────────
#
# 엔진은 `weekend_off_only_enable` 이 켜졌을 때 주말휴무자의 **활동 구간 `[입사일, 퇴사일−1]` 안 주말만** OFF 로 강제하고 평일
# OFF 는 막는다(fallback_lex.py:1496-1526). 예전 bridge 는 플래그와 근무 구간을 보지 않고 그 달 주말 전체(11월 9일)를 하드 바닥에
# 넣었다 — 10f 가 퇴사일 하루를 거두며 그 과다 감산이 1일 더 커졌다. 시나리오 달 2026-11: 일요일 0·7·14·21·28, 토요일
# 6·13·20·27(1일이 일요일).

WEEKEND_ALL = [0, 6, 7, 13, 14, 20, 21, 27, 28]


def _five_per_day_cfg(total_need, **extra):
    """하루 5명씩 150 에서 뒤쪽 날부터 줄여 월 수요를 `total_need` 로 맞춘다(일자별 수요는 6명 중 5명 이하라 일자별 검사는 통과)."""
    totals = [5] * 30
    cut = 150 - total_need
    for day in range(29, -1, -1):
        step = min(5, cut)
        totals[day] -= step
        cut -= step
    by_day = [{"D": max(total - 2, 0), "E": min(total, 1), "N": 1 if total >= 2 else 0} for total in totals]
    assert sum(totals) == total_need
    return _cfg(daily_shift_requirements={"D": 3, "E": 1, "N": 1}, daily_shift_requirements_by_day=by_day, **extra)


def test_r10f8_weekend_off_resigner_counts_only_weekends_inside_the_active_span(monkeypatch):
    """✗ 리뷰 재현 — 주말휴무자가 11/20 퇴사(구간 0~18)면 하드 바닥은 구간 안 주말 5일이다(그 달 주말 9일이 아니다).

    엔진: 구간 안 평일 14일은 근무 강제 · 주말 OFF 강제라 정확히 14일 근무. 나머지 5명 26일씩 → 합 144 ≥ 수요 141 이라 풀린다.
    전에는 바닥 9(그 달 주말 전체)로 n0 용량 10, 합 140 < 141 → 엔진이 푸는 달을 CAPACITY_TOTAL 로 막았다.
    """
    monkeypatch.delenv("AIDE_MAX_CONSEC_WORK", raising=False)  # 연속근무 상한은 설정 값 6 만 쓴다
    nurses = _nurses(6)
    nurses[0]["is_weekend_off"] = True
    nurses[0]["resignation_date"] = datetime(2026, 11, 20)
    cfg = _five_per_day_cfg(141)
    inp = _build(nurses, cfg)
    assert _working_capacity(inp.nurses[0], inp.roster_config) == 14  # 구간 19일 − 구간 안 주말 5일
    assert sum(_working_capacity(n, inp.roster_config) for n in inp.nurses) == 144  # 엔진 상한과 같다
    result, codes = _run(nurses, cfg)
    assert CAP not in codes and not has_blocking_issues(result)


def test_r10f8b_demand_above_the_engine_bound_is_still_blocked(monkeypatch):
    """(가드) 같은 병동에서 수요가 엔진 상한(144)을 넘는 145 이면 여전히 CAPACITY_TOTAL."""
    monkeypatch.delenv("AIDE_MAX_CONSEC_WORK", raising=False)
    nurses = _nurses(6)
    nurses[0]["is_weekend_off"] = True
    nurses[0]["resignation_date"] = datetime(2026, 11, 20)
    result, codes = _run(nurses, _five_per_day_cfg(145))
    assert CAP in codes and has_blocking_issues(result)


def test_r10f8c_weekend_off_joiner_counts_only_weekends_from_the_join_day():
    """✗ 11/15 입사 주말휴무자 — 구간 14~29 안 주말(14·20·21·27·28) 5일이 바닥이다(전에는 그 달 주말 9일)."""
    nurses = _nurses(2)
    nurses[0]["is_weekend_off"] = True
    nurses[0]["joining_date"] = datetime(2026, 11, 15)
    inp = _build(nurses, _cfg())
    assert (inp.nurses[0].join_day, inp.nurses[0].leave_day) == (14, 29)
    assert inp.nurses[0].personal_off_adjustment == 5


def test_r10f8d_weekend_off_full_month_nurse_is_unchanged():
    """(가드) 한 달 내내 근무하는 주말휴무자는 이전과 같이 그 달 주말 9일이다."""
    nurses = _nurses(2)
    nurses[0]["is_weekend_off"] = True
    assert [n.personal_off_adjustment for n in _build(nurses, _cfg()).nurses] == [9, 0]


def test_r10f9_weekend_flag_off_means_no_weekend_floor(monkeypatch):
    """✗ 리뷰 재현 — `weekend_off_only_enable=False` 면 엔진은 주말휴무자의 주말 OFF 를 강제하지 않는다(fallback_lex.py:1496).

    주말휴무자 6명이 전원 26일(30−4) 근무 가능 → 합 156 ≥ 수요 150. 전에는 플래그와 무관하게 주말 9일을 빼 1인 21, 합 126 < 150 으로
    막았다(일자별 표시는 이미 플래그를 따랐다 — 같은 함수 안 두 경로가 서로 달랐다).
    """
    monkeypatch.delenv("AIDE_MAX_CONSEC_WORK", raising=False)
    nurses = _nurses(6, is_weekend_off=True)
    cfg = _five_per_day_cfg(150, weekend_off_only_enable=False)
    inp = _build(nurses, cfg)
    assert [n.personal_off_adjustment for n in inp.nurses] == [0] * 6
    assert sum(_working_capacity(n, inp.roster_config) for n in inp.nurses) == 156
    result, codes = _run(nurses, cfg)
    assert CAP not in codes and not has_blocking_issues(result)


def test_r10f9b_weekend_flag_on_same_nurses_are_capped_by_the_weekend_floor(monkeypatch):
    """(가드) 같은 병동에서 플래그가 켜져 있으면(엔진 기본) 주말 9일이 바닥이라 합 126 < 150 으로 막힌다 — 플래그가 갈림길이다."""
    monkeypatch.delenv("AIDE_MAX_CONSEC_WORK", raising=False)
    nurses = _nurses(6, is_weekend_off=True)
    result, codes = _run(nurses, _five_per_day_cfg(150, weekend_off_only_enable=True))
    assert CAP in codes and has_blocking_issues(result)


def test_r10f9c_weekend_flag_off_counts_a_sunday_weekly_off_once():
    """✗ 플래그가 꺼져 있으면 주말은 안 빼므로 주휴(일요일) 5일만 바닥이다 — 주휴가 주말과 겹친다고 빼지 않는다.

    전에는 '이미 센 날' 집합이 플래그와 무관한 그 달 주말 전체라 일요일을 주말에 이미 센 것으로 보고 주휴를 0으로 셌다.
    """
    nurses = _nurses(2, is_weekend_off=True)
    sundays = [0, 7, 14, 21, 28]
    inp = _build(nurses, _cfg(weekend_off_only_enable=False, weekly_off_map={"n0": sundays}))
    assert [n.personal_off_adjustment for n in inp.nurses] == [5, 0]
