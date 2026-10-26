"""EI-10d·10e·10f 결과 테스트(전체 생성 경로) — 고정 칸(10d)·주휴 맵(10e)·입퇴사 구간(10f)이 실제 생성 서비스의 precheck 까지 닿는다.

골든 하네스(`harness.run_with_output`)로 합성 병동을 돌린다. 골든에는 넣지 않는다(`scenario_defs.
SCENARIOS` 에 없는 변형) — 이 파일은 시나리오를 새로 짓는다. precheck 가 막은 뒤의 원인 브리지
(`rcs._build_precheck_cause_bridge_finding`)는 CP-SAT 를 불러 하네스의 solve 0 규칙(`SolveBlocked`)에
걸리므로 경로 단언 대신 **precheck 결과**를 스파이로 붙잡아 단언한다.
"""

from __future__ import annotations

import dataclasses
from datetime import datetime
from functools import partial

from services.precheck.payload import has_blocking_issues

from .harness import Scenario, run_with_output
from .scenario_defs import BASE_DDL, S8_REQUEST, SCENARIOS, _assignment, _d, _request, _rows, s5_ward, s8_ward
from .scenario_defs import _scenario as _plain_scenario
from .scenarios import make_head_nurse
from .ward import GROUP_ID, OFFICE_ID, OTHER_GROUP_ID, base_ward, seed_ward, stamp

OVER_NEED = "FIXED_ASSIGN_EXCEEDS_NEED"
VIOLATES_ALLOWED = "FIXED_ASSIGN_VIOLATES_ALLOWED"


def _scenario(name, ward, patches=()):
    return Scenario(name=name, seed=partial(seed_ward, ward), make_request=_request(2026, 11, **S8_REQUEST),
                    make_user=make_head_nurse, ddl=tuple(BASE_DDL), description=name, patches=tuple(patches))


def _keep_precheck_result(box: dict):
    """`run_runtime_precheck` 의 인자·결과를 `box` 에 남기는 패치(원본은 그대로 부른다)."""

    def patch(mp):
        import services.precheck as precheck

        original = precheck.run_runtime_precheck

        def spy(**kwargs):
            box["fixed_cells"] = kwargs.get("fixed_cells")
            box["result"] = original(**kwargs)
            return box["result"]

        mp.setattr(precheck, "run_runtime_precheck", spy)

    return patch


def _extra_confirmed_day_shifts(nurse_ids, day):
    """확정 원티드 D 를 `day` 일에 더 얹는 시드(S8 의 확정 원티드 행과 같은 모양)."""
    base = {"group_id": GROUP_ID, "year": 2026, "month": 11, **stamp()}
    rows = [{**base, "nurse_id": nid, "shift_date": _d(11, day), "shift_id": "D", "shifts_table_id": None,
             "is_applied": True, "source_type": "original", "original_shift_id": "D", "created_by": "HN01"}
            for nid in nurse_ids]
    return _rows("FixedWantedEntry", rows)


def test_e2e_10d_fixed_day_for_night_only_nurse_blocks_before_solver(engine, engine_input_tables):
    """✗ S8 + N01 을 N 전담으로 — 확정 원티드 3일 D 는 엔진에서 불가능(허용 마스크 × 고정).

    전에는 고정 칸이 precheck 에 닿지 않아 첫 시도(엔진)까지 갔다. 이제 precheck 가 막는다.
    """
    ward = s8_ward()
    ward.nurses[0]["allowed_shifts"] = ["N"]
    box: dict = {}
    run_with_output(engine, _scenario("E2E-10d", ward, (_keep_precheck_result(box),)))
    codes = {issue["reason_code"] for issue in box["result"]["issues"]}
    assert VIOLATES_ALLOWED in codes and has_blocking_issues(box["result"])
    assert {"nurse_id": "N01", "day_index": 2, "shift": "D"} in box["fixed_cells"]


def test_e2e_10d_fixed_overcoverage_warns_and_generation_proceeds(engine, engine_input_tables):
    """✗ S8 + 3일에 확정 원티드 D 4명(수요 2) — 엔진은 초과 고정을 그대로 두므로 경고만 남고 첫 시도까지 간다.

    전에는 칸이 precheck 에 닿지 않아 이슈 자체가 없었다(경고를 하드로 되돌리면 이 테스트가 막는다).
    """
    ward = s8_ward()
    ward.extras.append(_extra_confirmed_day_shifts(["N03", "N05", "N06"], 3))
    box: dict = {}
    run = run_with_output(engine, _scenario("E2E-10d-over", ward, (_keep_precheck_result(box),)))
    over = [issue for issue in box["result"]["issues"] if issue["reason_code"] == OVER_NEED]
    assert [issue["severity"] for issue in over] == ["warning"]
    assert not has_blocking_issues(box["result"])
    assert run.result.outcome == "golden_stop" and run.result.path == "first_attempt"


def test_e2e_10d_translated_cells_reach_precheck_in_engine_order(engine, engine_input_tables):
    """✗ S8 의 엔진 형식 칸 4개가 번역되어 precheck 로 간다 — 연차(휴가)·OFF 는 O, D·N 은 그대로(전에는 원형 칸 그대로)."""
    box: dict = {}
    run_with_output(engine, _scenario("E2E-10d-cells", s8_ward(), (_keep_precheck_result(box),)))
    assert box["fixed_cells"] == [
        {"nurse_id": "N04", "day_index": 11, "shift": "O"},
        {"nurse_id": "N01", "day_index": 2, "shift": "D"},
        {"nurse_id": "N01", "day_index": 9, "shift": "O"},
        {"nurse_id": "N02", "day_index": 4, "shift": "N"},
    ]


# ── 10e: 주휴 — 생성 서비스가 만든 `weekly_off_map` 이 precheck 의 하드 용량까지 닿는다 ──────────────────


def _keep_precheck_input(box: dict):
    """`runtime_bridge.run_precheck` 가 받는 `PrecheckInput`(주휴 일수가 들어간 실물)을 `box` 에 남기는 패치."""

    def patch(mp):
        import services.precheck.runtime_bridge as bridge

        original = bridge.run_precheck

        def spy(inp, **kwargs):
            box["inp"] = inp
            return original(inp, **kwargs)

        mp.setattr(bridge, "run_precheck", spy)

    return patch


def _s5_periods_weekly_off_inactive(db) -> None:
    """S5 와 같은 주말휴무 기간표(F01·N01·N02) + 주휴 설정만 비활성(activate=0)."""
    from db.models import NurseWeekendOffPeriod, WeeklyOffSetting

    for nid in ("F01", "N01", "N02"):
        db.add(NurseWeekendOffPeriod(nurse_id=nid, valid_from=_d(1, 1), weekend_off=1, source="edited", **stamp()))
    db.add(WeeklyOffSetting(office_id=OFFICE_ID, group_id=GROUP_ID, activate=0, use_variable_cycle=1,
                            cycle_type="month", base_year=2026, base_month=10, cycle_interval=1,
                            shift_variation=-1, **stamp()))


def _personal_off_by_nurse(box: dict) -> dict[str, int]:
    return {nurse.nurse_id: nurse.personal_off_adjustment for nurse in box["inp"].nurses}


def test_e2e_10e_weekly_off_map_reaches_precheck_capacity(engine, engine_input_tables):
    """✗ S5(변동 주기 주휴 병동)를 실제 생성 경로로 — precheck 가 받은 간호사별 주휴 일수가 엔진 맵 그대로다.

    주말휴무자 N01·N02 는 주말 8일(맵의 일요일은 겹쳐 한 번만), 저장 요일이 토·수(정수)인 N03·N04 는 변동 주기로
    굴러간 목요일 5일 · 월요일 4일이다(전에는 정수 요일이라 0). 주휴 설정이 없는 N05~N10 은 0.
    """
    box: dict = {}
    scenario = dataclasses.replace(SCENARIOS["S5"], name="E2E-10e", patches=(_keep_precheck_input(box),))
    run = run_with_output(engine, scenario)
    got = _personal_off_by_nurse(box)
    assert (got["N01"], got["N02"], got["N03"], got["N04"]) == (8, 8, 5, 4)
    assert all(got[f"N{i:02d}"] == 0 for i in range(5, 11))
    assert run.result.outcome == "golden_stop" and run.result.path == "first_attempt"


def test_e2e_10e_inactive_weekly_off_setting_means_no_weekly_off_capacity(engine, engine_input_tables):
    """(가드) 주휴 설정이 꺼진 병동(activate=0)은 엔진 맵이 `{}` 라 저장 요일(정수)이 있어도 주휴 용량이 0 이다.

    주말휴무자 N01·N02 는 주말 8일 그대로. 생성 경로에서 activate 가 맵을 거쳐 precheck 까지 닿는지 확인한다.
    """
    ward = s5_ward()
    ward.extras = [_s5_periods_weekly_off_inactive]
    box: dict = {}
    scenario = dataclasses.replace(
        SCENARIOS["S5"], name="E2E-10e-off", seed=partial(seed_ward, ward), patches=(_keep_precheck_input(box),))
    run_with_output(engine, scenario)
    got = _personal_off_by_nurse(box)
    assert (got["N01"], got["N02"], got["N03"], got["N04"]) == (8, 8, 0, 0)


# ── 10f: 입퇴사 구간 — 생성 서비스가 엔진 명단을 거른 뒤 precheck 가 받는 활동 구간이 엔진과 같다 ─────────────


def _span_by_nurse(box: dict) -> dict[str, tuple[int, int]]:
    return {nurse.nurse_id: (nurse.join_day, nurse.leave_day) for nurse in box["inp"].nurses}


def test_e2e_10f_resigning_and_joining_nurses_reach_precheck_with_the_engine_span(engine, engine_input_tables):
    """✗ 11/20 퇴사 · 11/10 입사 · 11/1 퇴사를 가진 병동을 실제 생성 경로로 — precheck 가 받은 구간이 엔진 값이다.

    N03(11/20 퇴사)은 마지막 근무일이 11/19 라 `(0, 18)`(전에는 퇴사일까지 `(0, 19)`), N04(11/10 입사)는 `(9, 29)`,
    나머지는 월 전체다. N05(11/1 퇴사)는 마지막 근무일이 10/31 이라 생성 서비스가 엔진 명단에서 이미 빼서 precheck 입력에도
    없다(`rcs._active_range_in_month` 가 None). 구간을 엔진 함수로 읽어도 생성은 첫 시도까지 간다.
    """
    ward = base_ward(10)
    ward.nurses[2]["resignation_date"] = datetime(2026, 11, 20)  # N03
    ward.nurses[3]["joining_date"] = datetime(2026, 11, 10)  # N04
    ward.nurses[4]["resignation_date"] = datetime(2026, 11, 1)  # N05
    box: dict = {}
    scenario = _plain_scenario("E2E-10f", ward, "입퇴사 구간", patches=(_keep_precheck_input(box),))
    run = run_with_output(engine, scenario)
    spans = _span_by_nurse(box)
    assert spans["N03"] == (0, 18) and spans["N04"] == (9, 29) and "N05" not in spans
    assert all(spans[f"N{i:02d}"] == (0, 29) for i in (1, 2, 6, 7, 8, 9, 10))
    assert run.result.outcome == "golden_stop" and run.result.path == "first_attempt"


# ── 10d 리뷰 지적(HIGH) — 배정 차단일(파견) 위의 확정 원티드 칸은 엔진이 건너뛴다 ───────────────────────────────


def _keep_precheck_call(box: dict):
    """`run_runtime_precheck` 의 인자(고정 칸 번역본·차단일)와 결과를 `box` 에 남기는 패치(원본은 그대로 부른다)."""

    def patch(mp):
        import services.precheck as precheck

        original = precheck.run_runtime_precheck

        def spy(**kwargs):
            box["fixed_cells"] = kwargs.get("fixed_cells")
            box["blocked"] = (kwargs.get("config_dict") or {}).get("blocked_by_nurse_id")
            box["result"] = original(**kwargs)
            return box["result"]

        mp.setattr(precheck, "run_runtime_precheck", spy)

    return patch


def test_e2e_10d_confirmed_wanted_on_dispatch_days_is_not_blocked_by_precheck(engine, engine_input_tables):
    """✗ 리뷰 재현 — N01(N 전담)이 11/2~11/4 파견 나가 있고 11/3 에 확정 원티드 D 가 있다.

    엔진 lex 는 활동일(차단일 제외) 밖 고정 칸을 건너뛰어(fallback_lex.py:1311) 가능하다(독립 리뷰가 실제 엔진 stage1 로
    OPTIMAL 을 쟀다). 전에는 10d 번역이 그 칸을 precheck 에 넘겨 FIXED_ASSIGN_VIOLATES_ALLOWED 로 막고 생성이 error 로 갔다
    (10d 전에는 칸을 못 읽어 첫 시도까지 갔다). 이제 차단일 칸은 번역에서 버려 첫 시도까지 간다.
    """
    ward = s8_ward()
    ward.nurses[0]["allowed_shifts"] = ["N"]
    ward.other_groups = [OTHER_GROUP_ID]
    ward.extras.append(_rows("NurseAssignment", [
        _assignment("N01", GROUP_ID, OTHER_GROUP_ID, _d(11, 2), _d(11, 4), "파견", target_grade=1, target_shift_types=["N"])]))
    box: dict = {}
    run = run_with_output(engine, _scenario("E2E-10d-blocked", ward, (_keep_precheck_call(box),)))
    n01_cells = [cell for cell in box["fixed_cells"] if cell["nurse_id"] == "N01"]
    assert {1, 2, 3} <= set(box["blocked"]["N01"])
    assert n01_cells == [{"nurse_id": "N01", "day_index": 9, "shift": "O"}]  # 차단일(2) 칸은 빠지고 10일 OFF 칸만 남는다
    assert VIOLATES_ALLOWED not in {issue["reason_code"] for issue in box["result"]["issues"]}
    assert not has_blocking_issues(box["result"])
    assert run.result.outcome == "golden_stop" and run.result.path == "first_attempt"


def test_e2e_10d_same_cells_without_dispatch_still_block(engine, engine_input_tables):
    """(가드) 같은 병동에서 파견이 없으면(차단일 없음) 같은 확정 원티드 D 가 N 전담에게는 불가능이라 precheck 가 막는다(r10d3 과 같다)."""
    ward = s8_ward()
    ward.nurses[0]["allowed_shifts"] = ["N"]
    box: dict = {}
    run_with_output(engine, _scenario("E2E-10d-unblocked", ward, (_keep_precheck_call(box),)))
    assert box["blocked"] is None
    assert {"nurse_id": "N01", "day_index": 2, "shift": "D"} in box["fixed_cells"]
    assert VIOLATES_ALLOWED in {issue["reason_code"] for issue in box["result"]["issues"]}
    assert has_blocking_issues(box["result"])
