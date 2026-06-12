"""US-005 — 하네스 injector 오프라인 검증 (프로덕션 무변경 보장).

라이브 MSSQL baseline 구동은 harness.py __main__ 데모로 실증됨
(9B 2026-06: captured_ok=True, commit_blocked=2, feasible=True work_cells=281).
여기서는 DB 없이 가능한 부분 — 주입이 원본 captured 를 불변으로 두는지 — 만 검증.
"""

import copy

from tools.ontology_eval import harness


def _base_captured():
    return {
        "ward": "TEST", "year": 2026, "month": 6,
        "nurses_data": [{"nurse_id": "n1", "grade": 1, "team_id": "1"}],   # 공유 read-only
        "prefs_data": [],
        "shift_manage_data": [],
        "config_data": {
            "daily_shift_requirements": {"D": 3, "E": 3, "N": 2, "O": 0},
            "daily_shift_requirements_by_day": None,
            "team_min_by_team": {"1": {"D": 1, "N": 1}, "2": {"D": 1}},
            "max_night_shifts_per_month": None,
        },
        "grade_config": {"constraints_max_json": {}, "constraints_json": {}},
    }


def _assert_original_unchanged(fn, **kw):
    cap = _base_captured()
    snap_cfg = copy.deepcopy(cap["config_data"])
    snap_gc = copy.deepcopy(cap["grade_config"])
    out = fn(cap, **kw)
    assert cap["config_data"] == snap_cfg, "injector 가 원본 config_data 변경(deepcopy 위반)"
    assert cap["grade_config"] == snap_gc, "injector 가 원본 grade_config 변경"
    assert out is not cap
    assert "_injected" in out
    return out


def test_c1_coverage_overload():
    out = _assert_original_unchanged(harness.inject_c1_coverage_overload, shift="N", amount=99)
    assert out["_injected"]["case"] == "C1"
    assert out["_injected"]["family"] == "CoverageMin"
    assert out["config_data"]["daily_shift_requirements"]["N"] == 99


def test_c2_team_min_oversubscription():
    out = _assert_original_unchanged(harness.inject_c2_team_min_overload)
    assert out["_injected"]["case"] == "C2"
    assert out["_injected"]["family"] == "TeamMin"
    team = out["_injected"]["team"]
    size = out["_injected"]["team_size"]
    # D/E/N 각각 팀원 수 → 합 3×size > size (교차시프트 과구독, 각 min 은 skip 안 됨)
    tm = out["config_data"]["team_min_by_team"][team]
    assert tm["D"] == size and tm["E"] == size and tm["N"] == size
    assert "baseline" in out["_injected"]


def test_c3_grade_max_too_low_targets_grade_config():
    out = _assert_original_unchanged(harness.inject_c3_grade_max_too_low, shift="N")
    assert out["_injected"]["case"] == "C3"
    assert out["_injected"]["family"] == "GradeMax"
    # grade_config(별도 param)에 주입되어야 함
    assert out["grade_config"]["constraints_max_json"]["N"]["1"] == 0


def test_c4_night_cap_too_low():
    out = _assert_original_unchanged(harness.inject_c4_night_cap_too_low, cap=1)
    assert out["_injected"]["case"] == "C4"
    assert out["_injected"]["family"] == "MonthlyNightCap"
    assert out["_injected"]["knob"] == "max_nig_per_month"   # 엔진 하드 키
    assert out["config_data"]["max_nig_per_month"] == 1


def test_clone_shares_nurses_but_copies_config():
    cap = _base_captured()
    c = harness.clone(cap)
    assert c["nurses_data"] is cap["nurses_data"]        # read-only 공유
    assert c["config_data"] is not cap["config_data"]    # deepcopy
    assert c["grade_config"] is not cap["grade_config"]


def test_all_injectors_registered():
    assert set(harness.INJECTORS.keys()) == {"C1", "C2", "C3", "C4"}


def test_ward_registry():
    assert harness.WARD_GROUP_ID["9B"] == "10135890c287"
    assert harness.WARD_GROUP_ID["ICU"] == "10135857f9f9"
