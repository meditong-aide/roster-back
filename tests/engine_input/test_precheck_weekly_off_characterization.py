"""EI-10e 특성화 테스트 — 골든 S5(변동 주기 주휴 병동)의 실제 precheck 입력에서 주휴 일수가 엔진 고정 칸과 같다.

S5(2026-12)의 저장 요일은 N03 토(5) · N04 수(2) 인데, 변동 주기 때문에 엔진이 박은 주휴는 N03 목요일 5일 ·
N04 월요일 4일이다(`weekly_off_map`). 저장 요일을 다시 해석하면 4일 · 5일로 거꾸로 센다. N01·N02 는 주말휴무자라
엔진이 일요일을 주휴로 강제했고(맵), 그 날들은 주말 전체(8일)에 이미 들어 있어 따로 더하지 않는다.

골든 L0 의 `nurses_dict`·`config_dict`·`fixed_cells`(precheck 에 들어가는 값 그대로)로 `build_precheck_input` 을 다시
돌려 '엔진이 박은 주휴 O 칸 수'(S5 의 고정 칸 17개는 전부 주휴 O)와 대조한다. 골든 파일은 읽기만 한다.
"""
from __future__ import annotations

import calendar
from datetime import date

import pytest
from services.precheck import run_runtime_precheck
from services.precheck.payload import has_blocking_issues
from services.precheck.runtime_bridge import build_precheck_input

from .golden import read_golden


@pytest.fixture(scope="module")
def s5_call():
    """골든 S5 의 precheck 호출 한 벌(L0 첫 시도)."""
    record = read_golden("S5")
    assert record is not None, "골든 S5 가 없다"
    return record["layers"]["L0"][0]


def _kwargs(call):
    return {
        "nurses_dict": call["nurses_dict"], "config_dict": call["config_dict"],
        "grade_config": call["grade_config"], "fixed_cells": call["fixed_cells"],
        "year": call["year"], "month": call["month"],
    }


def _weekday_count(year, month, weekday):
    return sum(1 for day in range(1, calendar.monthrange(year, month)[1] + 1)
               if date(year, month, day).weekday() == weekday)


def test_s5_personal_off_counts_the_engine_weekly_off_cells(s5_call):
    """S5 의 간호사별 주휴 일수는 엔진이 박은 주휴 O 칸 수다 — 주말휴무자(N01·N02)는 주말 8일이 전부다."""
    inp = build_precheck_input(**_kwargs(s5_call))
    engine_days: dict[str, set[int]] = {}
    for cell in s5_call["fixed_cells"]:
        assert cell["shift"] == "O"  # S5 의 고정 칸은 전부 주휴 O
        engine_days.setdefault(cell["nurse_id"], set()).add(cell["day_index"])

    got = {nurse.nurse_id: nurse.personal_off_adjustment for nurse in inp.nurses}
    assert got == {"N01": 8, "N02": 8, "N03": 5, "N04": 4,
                   "N05": 0, "N06": 0, "N07": 0, "N08": 0, "N09": 0, "N10": 0}
    # 주휴가 아닌 날을 따로 세는 간호사(N03·N04)는 엔진 칸 수와 정확히 같고, 일자별(fixed_off_days)도 같은 날이다.
    by_id = {nurse.nurse_id: nurse for nurse in inp.nurses}
    for nurse_id in ("N03", "N04"):
        assert got[nurse_id] == len(engine_days[nurse_id])
        assert by_id[nurse_id].fixed_off_days == engine_days[nurse_id]


def test_s5_variable_cycle_would_be_miscounted_by_stored_weekday(s5_call):
    """저장 요일을 다시 해석했다면 N03·N04 가 거꾸로 센다 — 맵을 읽는 이유(변동 주기)를 고정한다."""
    year, month = s5_call["year"], s5_call["month"]
    stored = {nurse["nurse_id"]: nurse["weekly_off_weekday"] for nurse in s5_call["nurses_dict"]}
    assert (stored["N03"], stored["N04"]) == (5, 2)
    assert (_weekday_count(year, month, 5), _weekday_count(year, month, 2)) == (4, 5)  # 저장 요일로 세면 4 · 5
    inp = build_precheck_input(**_kwargs(s5_call))
    got = {nurse.nurse_id: nurse.personal_off_adjustment for nurse in inp.nurses}
    assert (got["N03"], got["N04"]) == (5, 4)  # 엔진 맵으로 세면 5 · 4


def test_s5_precheck_verdict_is_not_blocked_by_weekly_off(s5_call, monkeypatch):
    """골든 S5 는 주휴 용량을 넣어도 차단되지 않는다 — 10e 가 골든 시나리오의 판정을 바꾸지 않는다."""
    monkeypatch.delenv("AIDE_MAX_CONSEC_WORK", raising=False)  # 연속근무 상한은 골든 설정 값만 쓴다
    result = run_runtime_precheck(**_kwargs(s5_call), stop_on_config_error=False)
    assert not has_blocking_issues(result)
