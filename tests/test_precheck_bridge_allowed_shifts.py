"""Regression test for runtime_bridge._allowed_shifts_for.

이전 코드는 `int(nurse.get("allowed_shifts", 0) or 0) == 3` 패턴이라
현재 schema (`allowed_shifts: list`) 에서 TypeError 로 죽고, 호출 측의
bare except 가 silent swallow 해 precheck 가 통째로 무력화됐다.

여기서는 list / legacy int / work_shifts / None 케이스 모두에 대해
정상 동작하는지 확인한다.
"""

from __future__ import annotations

from services.precheck.runtime_bridge import _allowed_shifts_for


def test_list_n_only_returns_n():
    assert _allowed_shifts_for({"allowed_shifts": ["N"]}, False) == ["N"]


def test_empty_list_means_no_restriction():
    # 빈 list 는 "제한 없음" → None (= universe 전체 허용)
    assert _allowed_shifts_for({"allowed_shifts": []}, False) is None


def test_list_full_dem_returns_all():
    assert sorted(_allowed_shifts_for({"allowed_shifts": ["D", "E", "N"]}, False)) == ["D", "E", "N"]


def test_legacy_int_3_returns_n():
    assert _allowed_shifts_for({"allowed_shifts": 3}, False) == ["N"]


def test_legacy_int_0_returns_none():
    assert _allowed_shifts_for({"allowed_shifts": 0}, False) is None


def test_legacy_bool_true_returns_n():
    assert _allowed_shifts_for({"allowed_shifts": True}, False) == ["N"]


def test_work_shifts_csv_falls_through():
    assert sorted(_allowed_shifts_for({"work_shifts": "D,E,N"}, False)) == ["D", "E", "N"]


def test_work_shifts_list_falls_through():
    assert sorted(_allowed_shifts_for({"work_shifts": ["D", "E"]}, False)) == ["D", "E"]


def test_use_mid_universe_includes_m():
    assert sorted(_allowed_shifts_for({"allowed_shifts": ["D", "M"]}, True)) == ["D", "M"]


def test_use_mid_off_drops_m():
    # use_mid=False 일 때 M 코드는 무시되어야 함
    assert _allowed_shifts_for({"allowed_shifts": ["M"]}, False) is None


def test_empty_dict_returns_none():
    assert _allowed_shifts_for({}, False) is None


def test_none_value_returns_none():
    assert _allowed_shifts_for({"allowed_shifts": None}, False) is None


def test_invalid_code_filtered():
    # 알 수 없는 코드는 drop, 유효한 것만 남음
    assert _allowed_shifts_for({"allowed_shifts": ["N", "XYZ"]}, False) == ["N"]
