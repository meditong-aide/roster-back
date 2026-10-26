"""EI-10e 결과 테스트 — precheck 가 주휴를 엔진이 만든 맵(`weekly_off_map`)으로 세어 판정이 바뀌는 사례.

배경(결정 D8 · 설계서 §2.6): `runtime_bridge._weekly_off_count` 는 `nurses.weekly_off_weekday` 를 문자열·리스트로만
읽었다. 생성 경로의 요일은 ORM 정수(0=월~6=일)라 늘 0일을 셌고(결함 ③), 설정 activate · 간호사 enabled · 변동 주기
(달마다 요일이 굴러간다) · 주말휴무자의 일요일 강제는 아예 몰랐다. 엔진은 이것들을
`roster_create_service._compute_weekly_off_day_indices_for_month` 가 해석해 주휴 고정 O 칸으로 만든다. 같은 맵이
`config_dict["weekly_off_map"]`({nurse_id: [0-based 일자]}, 활동 구간·파견·프리셉티로 거른 뒤)으로 precheck 앞에 실려
있으므로, 10e 는 요일을 다시 해석하지 않고 그 맵의 날 수를 `personal_off_adjustment`(하드 용량)에 더한다. 조회 0.

각 테스트의 '엔진 판정'은 주휴 O 칸이 하드 고정이라는 의미로 따진 값이다. ✗ = 10e 전 코드에서 빨강(결함 재현),
(가드) = 전/후 모두 초록이지만 변이·퇴행을 잡는 불변 확인이다. 시나리오 달은 2026-11(30일, 1일이 일요일 —
일요일 인덱스 0·7·14·21·28, 월요일 1·8·15·22·29, 수요일 3·10·17·24, 토·일 합 9일).
"""

from __future__ import annotations

import pytest
from services.ontology_pool import build_pool_snapshot_from_runtime
from services.precheck import run_runtime_precheck
from services.precheck.payload import has_blocking_issues
from services.precheck.runtime_bridge import build_precheck_input
from services.precheck.team_grade_precheck import _working_capacity

CAP = "CAPACITY_TOTAL_SHORTAGE"
SUNDAYS = [0, 7, 14, 21, 28]
MONDAYS = [1, 8, 15, 22, 29]
WEDNESDAYS = [3, 10, 17, 24]
WEEKEND_DAYS = 9


def _cfg(**extra):
    """엔진 기본에 가까운 설정 — 연속근무 상한은 운영에 많은 6."""
    base = {
        "daily_shift_requirements": {"D": 1, "E": 1, "N": 1}, "max_conseq_work": 6,
        "max_nig_per_month": 15, "banned_day_after_eve": True, "three_seq_nig": True,
        "two_offs_after_three_nig": True, "two_offs_after_two_nig": False, "not_one_night": False,
        "use_mid": False, "off_days": 8, "shift_priority": 0.7,
        # EI-14r3 — 이 파일은 연속근무·주휴·퇴사 용량을 따로 잰다. OFF 하드 하한(min_off — off_first=False 병동)은 별개 제약이라 끈다
        # (그 하한은 test_precheck_min_off_floor.py · test_precheck_vs_engine_stage1.py::test_s7e* 가 본다).
        "off_first": True,
    }
    base.update(extra)
    return base


def _nurses(n=3, **extra):
    return [{"nurse_id": f"n{i}", "grade": 1, "allowed_shifts": [], "sequence": i, **extra} for i in range(n)]


def _build(nurses, cfg, cells=None):
    return build_precheck_input(
        nurses_dict=nurses, config_dict=cfg, grade_config=None, fixed_cells=cells, year=2026, month=11)


def _personal_off(nurses, cfg):
    return [n.personal_off_adjustment for n in _build(nurses, cfg).nurses]


def _run(nurses, cfg):
    result = run_runtime_precheck(
        nurses_dict=nurses, config_dict=cfg, grade_config=None, fixed_cells=None,
        year=2026, month=11, stop_on_config_error=False)
    return result, {issue["reason_code"] for issue in result["issues"]}


def _capacity_cfg(**extra):
    """6명 용량 시나리오 — 하루 필요 5명(첫 6일은 6명)이라 월 필요 156 = 6명 × 상한 26(30일 · 연속근무 6)."""
    by_day = [{"D": 2, "E": 2, "N": 2} if day < 6 else {"D": 2, "E": 2, "N": 1} for day in range(30)]
    return _cfg(daily_shift_requirements={"D": 2, "E": 2, "N": 1}, daily_shift_requirements_by_day=by_day, **extra)


# ── 정수 요일 · 엔진 맵 ──────────────────────────────────────────────────────


def test_r10e1_int_weekday_counts_engine_map_days():
    """✗ 정수 요일(ORM 값)인 간호사의 주휴 일수는 엔진 맵의 날 수다 — 전에는 정수 요일을 0일로 셌다."""
    nurses = _nurses()
    nurses[0]["weekly_off_weekday"] = 2
    assert _personal_off(nurses, _cfg(weekly_off_map={"n0": WEDNESDAYS})) == [4, 0, 0]


def test_r10e1b_weekend_off_nurse_counts_sunday_overlap_once():
    """(가드) 주말휴무자는 주말 전체를 이미 세고, 엔진이 일요일을 주휴로 강제한 날은 겹치므로 한 번만 센다."""
    nurses = _nurses(2)
    nurses[0]["is_weekend_off"] = True
    nurses[0]["weekly_off_weekday"] = 6
    assert _personal_off(nurses, _cfg(weekly_off_map={"n0": SUNDAYS})) == [WEEKEND_DAYS, 0]


def test_r10e1c_weekend_off_nurse_counts_only_non_weekend_extra_days():
    """✗ 겹치는 날만 뺀다 — 주말 밖 주휴가 맵에 있으면 그 날은 더한다(집합 차). 운영 맵은 일요일뿐이다."""
    nurses = _nurses(2)
    nurses[0]["is_weekend_off"] = True
    assert _personal_off(nurses, _cfg(weekly_off_map={"n0": [0, 3]})) == [WEEKEND_DAYS + 1, 0]


def test_r10e1d_personal_adjustment_is_not_added_to_engine_hard_off_days():
    """✗ 엔진이 안 쓰는 이월 보정 p(`personal_off_adjustment`)는 엔진이 하드 O 로 박는 주휴 일수와 더하지 않는다 — max.

    p=2, 주휴 4일이면 하드 바닥은 6(합산)이 아니라 4 다. 합산하면 같은 OFF 를 두 번 빼서 엔진이 풀 수 있는 달을 막는다
    (r10e9). p 를 하드 바닥으로 보는 것 자체는 10e 이전 그대로다(p 만 있으면 p — 레지스트리 PERSONAL-OFF-ENGINE-IGNORES).
    (옛 단언은 합 6 을 정답으로 고정했었다 — 리뷰 지적 10e HIGH.) p 를 하드 바닥에서 빼기로 정하면(EI-16 큐) 이 단언은 그대로
    `[4, 0]` 이고, p 가 더 큰 쪽을 고정한 r10e1e[p=7]·r10e9c 가 함께 바뀐다.
    """
    nurses = _nurses(2)
    nurses[0]["personal_off_adjustment"] = 2
    assert _personal_off(nurses, _cfg(weekly_off_map={"n0": WEDNESDAYS})) == [4, 0]


@pytest.mark.parametrize(("p_adj", "expected"), [(2, 4), (4, 4), (7, 7), (0, 4), (-3, 4)])
def test_r10e1e_floor_is_the_larger_of_personal_adjustment_and_hard_off_days(p_adj, expected):
    """✗ 하드 바닥 = max(p, 하드 O 날 수) — p 가 더 크면 p(10e 이전 의미 보존), 작거나 음수면 날 수."""
    nurses = _nurses(2)
    nurses[0]["personal_off_adjustment"] = p_adj
    assert _personal_off(nurses, _cfg(weekly_off_map={"n0": WEDNESDAYS})) == [expected, 0]


@pytest.mark.parametrize("p_adj", [3, 0, -3])
def test_r10e1f_without_hard_off_days_personal_adjustment_is_kept_as_is(p_adj):
    """(가드) 하드 O 날이 없으면 p 를 그대로 둔다(음수 포함) — 바닥을 0 으로 깎지도, 올리지도 않는다(10e 이전과 같다)."""
    nurses = _nurses(2)
    nurses[0]["personal_off_adjustment"] = p_adj
    assert _personal_off(nurses, _cfg(weekly_off_map={})) == [p_adj, 0]


@pytest.mark.parametrize("weekday", [2, "SUN", "일", [6], "6"])
def test_r10e2_activate_off_ignores_stored_weekday(weekday):
    """✗ 주휴 설정이 꺼져 있으면(엔진 맵 `{}`) 간호사에 저장된 요일이 어떤 모양이든 주휴는 0일이다.

    전에는 문자열·리스트 요일을 맵과 상관없이 세어 꺼진 병동에도 5일(11월 일요일 수)을 하드 감산했다.
    """
    nurses = _nurses(2)
    nurses[0]["weekly_off_weekday"] = weekday
    assert _personal_off(nurses, _cfg(weekly_off_map={})) == [0, 0]


def test_r10e2b_engine_map_is_the_truth_not_the_settings_flag():
    """✗ 맵이 비어 있지 않으면 엔진은 그 날들을 고정 O 로 박는다 — `weekly_off_settings_activate` 는 따로 읽지 않는다.

    그 플래그는 엔진의 주휴 부가 구조(`weekly_off_by_idx`)만 켜고 끈다. 하드 O 칸의 원천은 맵이다.
    """
    nurses = _nurses(2)
    nurses[0]["weekly_off_weekday"] = 2
    cfg = _cfg(weekly_off_map={"n0": WEDNESDAYS}, weekly_off_settings_activate=False)
    assert _personal_off(nurses, cfg) == [4, 0]


def test_r10e4_variable_cycle_map_wins_over_stored_weekday():
    """✗ 변동 주기로 달마다 요일이 굴러가면 저장된 요일(토=5)이 아니라 엔진이 만든 날(월요일 5일)이 맞다.

    저장 요일을 해석했다면 11월 토요일 4일로 세어 하드 용량을 1일 느슨하게 본다.
    """
    nurses = _nurses(2)
    nurses[0]["weekly_off_weekday"] = 5
    assert _personal_off(nurses, _cfg(weekly_off_map={"n0": MONDAYS})) == [5, 0]


def test_r10e5_nurse_outside_map_gets_no_weekly_off():
    """✗ 엔진 맵에 없는 간호사는 저장 요일이 있어도 주휴가 없고, 맵에 있는 간호사만 센다 — 간호사별 enabled 꺼짐 ·
    활동 구간 밖 · 파견 나간 날 · 프리셉티는 엔진이 맵에서 이미 뺐다. 요일을 되살려 세면 n0 이 3일로 잡혀 빨개진다."""
    nurses = _nurses(3)
    nurses[0]["weekly_off_weekday"] = 3
    nurses[0]["weekly_off_enabled"] = 0
    nurses[1]["weekly_off_weekday"] = 2
    assert _personal_off(nurses, _cfg(weekly_off_map={"n1": WEDNESDAYS})) == [0, 4, 0]


@pytest.mark.parametrize("weekly_off_map", [None, [], "x", 3, {"n0": 3}, {"n0": "abc"}, {"n0": [None, "x", 2.5]}])
def test_r10e6_missing_or_malformed_map_counts_zero_without_error(weekly_off_map):
    """(가드) 맵이 없거나 모양이 틀려도 precheck 는 죽지 않고 0일로 본다(직접 호출·옛 설정 호환)."""
    nurses = _nurses(2)
    nurses[0]["weekly_off_weekday"] = 2
    assert _personal_off(nurses, _cfg(weekly_off_map=weekly_off_map)) == [0, 0]


def test_r10e6b_map_without_key_counts_zero():
    """(가드) `weekly_off_map` 키 자체가 없는 설정(직접 호출 · 옛 스냅샷)도 0일이다."""
    nurses = _nurses(2)
    nurses[0]["weekly_off_weekday"] = 2
    assert _personal_off(nurses, _cfg()) == [0, 0]


def test_r10e6c_duplicate_and_non_int_days_count_once_each():
    """✗ 같은 날이 겹쳐 있거나 정수가 아닌 값이 섞여 있어도 일자는 한 번씩만 센다."""
    nurses = _nurses(2)
    assert _personal_off(nurses, _cfg(weekly_off_map={"n0": [3, 3, "x", 10, None]})) == [2, 0]


@pytest.mark.parametrize("days", [(3, 10), {3, 10}, [3, 10]])
def test_r10e6d_list_tuple_and_set_days_are_all_accepted(days):
    """✗ 일자 목록은 list(생성 경로) · tuple · set 어느 모양이든 같은 일수로 센다(스냅샷 복원·직접 호출 호환)."""
    nurses = _nurses(2)
    assert _personal_off(nurses, _cfg(weekly_off_map={"n0": days})) == [2, 0]


# ── 판정 변화(하드 용량) ─────────────────────────────────────────────────────


def test_r10e3_weekly_off_tightens_total_capacity_like_engine(monkeypatch):
    """✗ 월요일 주휴 5일은 엔진에서 하드 O 칸이다 — 한 명의 상한이 26 → 25 가 되어 전체 용량(155)이 수요(156)에 못 미친다.

    엔진 판정: 불가능. 7일 창마다 OFF 1이 필요한데 주휴 O 가 7일 간격이라 그 O 가 곧 필수 OFF 를 겸하므로 n0 의
    최대 근무는 30−5=25 이고 나머지는 26이다(25+5×26=155 < 156). 전에는 주휴를 0일로 세어 156 ≥ 156 으로 통과했다.
    """
    monkeypatch.delenv("AIDE_MAX_CONSEC_WORK", raising=False)  # 연속근무 상한은 설정 값 6 만 쓴다
    nurses = _nurses(6)
    nurses[0]["weekly_off_weekday"] = 0
    result, codes = _run(nurses, _capacity_cfg(weekly_off_map={"n0": MONDAYS}))
    assert CAP in codes and has_blocking_issues(result)


def test_r10e3b_same_input_without_map_entry_passes(monkeypatch):
    """(가드) 같은 입력이라도 엔진 맵에 그 간호사가 없으면(주휴 꺼짐) 156 ≥ 156 으로 통과한다."""
    monkeypatch.delenv("AIDE_MAX_CONSEC_WORK", raising=False)  # 연속근무 상한은 설정 값 6 만 쓴다
    nurses = _nurses(6)
    nurses[0]["weekly_off_weekday"] = 0
    result, codes = _run(nurses, _capacity_cfg(weekly_off_map={}))
    assert CAP not in codes and not has_blocking_issues(result)


def test_r10e3c_need_equal_to_capacity_passes(monkeypatch):
    """(가드) 수요가 용량과 딱 같으면(155=155) 통과한다 — 일수를 하나라도 더 세면 빨개지는 경계 확인."""
    monkeypatch.delenv("AIDE_MAX_CONSEC_WORK", raising=False)  # 연속근무 상한은 설정 값 6 만 쓴다
    nurses = _nurses(6)
    nurses[0]["weekly_off_weekday"] = 0
    cfg = _capacity_cfg(weekly_off_map={"n0": MONDAYS})
    cfg["daily_shift_requirements_by_day"][0] = {"D": 2, "E": 2, "N": 1}  # 첫날 수요 6 → 5: 합 155
    result, codes = _run(nurses, cfg)
    assert CAP not in codes and not has_blocking_issues(result)


# ── 10d(일자별 고정 OFF)와 10e(용량)가 같은 날을 두 번 빼지 않는다 ───────────────


def test_r10e7_weekly_off_cells_and_capacity_agree_without_double_count():
    """✗ 10d 는 주휴 O 칸을 일자별(`fixed_off_days`)로, 10e 는 용량(`personal_off_adjustment`)으로 넣는다.

    용량은 `fixed_off_days` 를 따로 빼지 않으므로(`_working_capacity`) 같은 5일이 한 번만 빠진다 — 이중 계산이면 20.
    """
    nurses = _nurses(2)
    cells = [{"nurse_id": "n0", "day_index": day, "shift": "O"} for day in MONDAYS]
    inp = _build(nurses, _cfg(weekly_off_map={"n0": MONDAYS}), cells)
    n0 = inp.nurses[0]
    assert n0.fixed_off_days == set(MONDAYS) and n0.personal_off_adjustment == 5
    assert _working_capacity(n0, inp.roster_config) == 25


def test_r10e8_pool_graph_capacity_follows_the_same_engine_map():
    """✗ PoolGraph(`build_pool_snapshot_from_runtime`)도 같은 입구(`build_precheck_input`)를 타서 주휴가 풀 용량에서 빠진다.

    4명 × 30일 = 120 이던 공통풀 용량이 한 명의 수요일 주휴 4일만큼 줄어 116 이다(전에는 정수 요일이라 120).
    """
    nurses = _nurses(4)
    nurses[0]["weekly_off_weekday"] = 2

    def _common_pool_capacity(weekly_off_map):
        snapshot = build_pool_snapshot_from_runtime(
            nurses_dict=nurses, config_dict=_cfg(weekly_off_map=weekly_off_map), grade_config=None,
            fixed_cells=None, year=2026, month=11)
        pools = {pool["pool_id"]: pool["attrs"]["monthly_capacity"] for pool in snapshot.to_dict()["pools"]}
        return pools["common_pool:D"]

    assert _common_pool_capacity({}) == 120
    assert _common_pool_capacity({"n0": WEDNESDAYS}) == 116


# ── 리뷰 지적(10e HIGH) — 엔진이 안 쓰는 이월 보정 p 와 주휴 일수를 더해 엔진이 푸는 달을 막지 않는다 ─────────────


def _run_with_cells(nurses, cfg, cells):
    result = run_runtime_precheck(
        nurses_dict=nurses, config_dict=cfg, grade_config=None, fixed_cells=cells,
        year=2026, month=11, stop_on_config_error=False)
    return result, {issue["reason_code"] for issue in result["issues"]}


def _monday_off_ward_cfg(total_need, **extra):
    """6명 · 연속근무 6 · n0 가 월요일(5일) 주휴인 병동의 월 수요 `total_need` — 월요일은 5명(n0 없음), 그 밖의 날은 5~6명.

    일자별 수요가 그 날 가용 인원(월요일 5 · 그 밖 6)을 넘지 않게 짜서 일자별 검사는 통과하고 월 용량만 갈린다.
    총량만 의미 있다 — 일자별 요구는 D 가 나머지, E·N 은 1명씩.
    """
    totals = [5] * 30
    extra_days = [d for d in range(30) if d not in MONDAYS][: total_need - 150]
    for day in extra_days:
        totals[day] = 6
    by_day = [{"D": total - 2, "E": 1, "N": 1} for total in totals]
    assert sum(totals) == total_need
    return _cfg(daily_shift_requirements={"D": 3, "E": 1, "N": 1}, daily_shift_requirements_by_day=by_day,
                weekly_off_map={"n0": MONDAYS}, **extra)


def _monday_off_cells():
    return [{"nurse_id": "n0", "day_index": day, "shift": "O"} for day in MONDAYS]


def test_r10e9_personal_adjustment_and_weekly_off_are_not_double_counted(monkeypatch):
    """✗ 리뷰 재현 — p=2 + 월요일 주휴 5일 + 연속근무 6 · 월 수요 154: 엔진 상한은 25 + 5×26 = 155 ≥ 154 라 풀린다.

    엔진은 p 를 모델에 안 쓴다(PERSONAL-OFF-ENGINE-IGNORES) — 하드 O 인 주휴 5일이 n0 의 근무일을 30−5=25 로 막을 뿐이다.
    전에는 p(2) 와 주휴(5)를 더한 7 을 하드 바닥으로 빼서 n0 용량이 23, 합 153 < 154 → 엔진이 푸는 달을 CAPACITY_TOTAL 로 막았다.
    """
    monkeypatch.delenv("AIDE_MAX_CONSEC_WORK", raising=False)  # 연속근무 상한은 설정 값 6 만 쓴다
    nurses = _nurses(6)
    nurses[0]["personal_off_adjustment"] = 2
    cfg = _monday_off_ward_cfg(154)
    inp = _build(nurses, cfg, _monday_off_cells())
    assert _working_capacity(inp.nurses[0], inp.roster_config) == 25  # 엔진 최대 근무일(30 − 월요일 5일)
    assert sum(_working_capacity(n, inp.roster_config) for n in inp.nurses) == 155  # 엔진 상한과 같은 용량
    result, codes = _run_with_cells(nurses, cfg, _monday_off_cells())
    assert CAP not in codes and not has_blocking_issues(result)


def test_r10e9b_demand_above_the_engine_bound_is_still_blocked(monkeypatch):
    """(가드) 같은 병동에서 수요가 엔진 상한(155)을 넘는 156 이면 여전히 CAPACITY_TOTAL — 용량을 느슨하게만 만들지 않았다."""
    monkeypatch.delenv("AIDE_MAX_CONSEC_WORK", raising=False)
    nurses = _nurses(6)
    nurses[0]["personal_off_adjustment"] = 2
    result, codes = _run_with_cells(nurses, _monday_off_ward_cfg(156), _monday_off_cells())
    assert CAP in codes and has_blocking_issues(result)


def test_r10e9c_personal_adjustment_larger_than_hard_off_days_still_binds(monkeypatch):
    """(가드) p 가 하드 O 날 수보다 크면 p 가 바닥이다(10e 이전 의미 보존) — p=7 이면 n0 용량 23, 합 153 < 154.

    엔진은 p 를 안 쓰므로(PERSONAL-OFF-ENGINE-IGNORES) p 를 하드 바닥에서 빼기로 정하면(EI-16 큐) 이 단언과 r10e1e 의 p 우선 행
    (p=7)이 함께 바뀐다 — 그때 이 테스트를 '용량 25' 로 뒤집는다. 지금은 10e 이전 동작을 바꾸지 않는다는 결정(10-19)의 가드다.
    """
    monkeypatch.delenv("AIDE_MAX_CONSEC_WORK", raising=False)
    nurses = _nurses(6)
    nurses[0]["personal_off_adjustment"] = 7
    cfg = _monday_off_ward_cfg(154)
    inp = _build(nurses, cfg, _monday_off_cells())
    assert _working_capacity(inp.nurses[0], inp.roster_config) == 23
    _result, codes = _run_with_cells(nurses, cfg, _monday_off_cells())
    assert CAP in codes


# ── 리뷰 지적(10e LOW) — 같은 날 칸이 겹치면 엔진은 마지막 칸이 이긴다 ──────────────────────────────────────


def test_r10e10_weekly_off_day_overridden_by_later_work_cell_is_not_counted():
    """✗ 주휴 맵의 날에 뒤따르는 고정 근무 칸이 있으면 엔진은 그 날을 근무로 고정한다(`fixed[(n, d)]` 덮어쓰기) — 주휴 바닥에서 뺀다.

    전에는 맵의 날을 그대로 세어 엔진이 O 로 고정한 3일보다 1 많은 4 를 하드 바닥으로 봤다.
    """
    nurses = _nurses(2)
    cells = [{"nurse_id": "n0", "day_index": day, "shift": "O"} for day in WEDNESDAYS]
    cells.append({"nurse_id": "n0", "day_index": 10, "shift": "N"})  # 뒤에 오는 칸이 이긴다
    inp = _build(nurses, _cfg(weekly_off_map={"n0": WEDNESDAYS}), cells)
    assert inp.nurses[0].personal_off_adjustment == 3
    assert inp.nurses[0].fixed_shift_assignments == {10: "N"}


def test_r10e10b_later_off_cell_after_work_cell_keeps_the_day_off():
    """(가드) 반대 순서(근무 칸 뒤에 O)면 O 가 이긴다 — 그 날은 주휴로 센다."""
    nurses = _nurses(2)
    cells = [{"nurse_id": "n0", "day_index": 10, "shift": "N"}]
    cells += [{"nurse_id": "n0", "day_index": day, "shift": "O"} for day in WEDNESDAYS]
    inp = _build(nurses, _cfg(weekly_off_map={"n0": WEDNESDAYS}), cells)
    assert inp.nurses[0].personal_off_adjustment == 4


def test_r10e11_weekly_off_days_outside_the_active_span_are_not_counted():
    """✗ 근무 구간(11/20 퇴사 → 0~18) 밖의 맵 날은 하드 바닥이 아니다 — 구간 밖은 용량(span)에도 안 들어 이중으로 빼게 된다.

    생성 경로의 맵은 엔진 활동 구간으로 이미 잘려 있어 직접 입력·퇴행 방어용이다(전에는 맵의 3일을 다 셌다).
    """
    nurses = _nurses(2)
    nurses[0]["resignation_date"] = "2026-11-20"
    inp = _build(nurses, _cfg(weekly_off_map={"n0": [3, 20, 27]}))
    assert (inp.nurses[0].join_day, inp.nurses[0].leave_day) == (0, 18)
    assert inp.nurses[0].personal_off_adjustment == 1
