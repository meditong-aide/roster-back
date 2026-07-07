"""부분 재생성 커버리지 미달 경고(§6.1 '조용한 미달 가시화') 단위 테스트.

퇴사로 빈자리를 못 채워 cutoff 이후 D/E/N 커버리지가 config 요구치보다 낮으면
summary.warnings 로 노출돼야 한다.
"""
from __future__ import annotations

from db.models import RosterConfig, Schedule
from services.resignation_partial_resolve_service import (
    _coverage_shortfall_warnings,
    _main_code,
)


def _mk_schedule(db, *, config_id: int, schedule_id: str):
    db.add(RosterConfig(config_id=config_id, day_req=2, eve_req=2, nig_req=1, use_mid=False))
    sched = Schedule(
        schedule_id=schedule_id, year=2026, month=3, version=1, config_id=config_id,
    )
    db.add(sched)
    db.flush()
    return sched


def test_main_code_normalizes_shifts():
    meta = {"D1": {"shift_gb": "데이", "type": "근무"}, "AL": {"type": "휴가"}}
    assert _main_code("D1", meta) == "D"
    assert _main_code("AL", meta) == "O"   # 휴가 → 비커버리지
    assert _main_code("N", {}) == "N"
    assert _main_code("O", {}) == "O"
    assert _main_code("-", {}) == "O"


def test_coverage_shortfall_flags_understaffed_day(db):
    sched = _mk_schedule(db, config_id=7777, schedule_id="draft0000001")
    days = 31
    grid = {
        "a": ["D"] * days,
        "b": ["D"] * days,
        "c": ["E"] * days,
        "d": ["E"] * days,
        "e": ["N"] * days,
    }
    # day idx 15(=3/16)에 b를 O로 → D 커버리지 2→1 (요구=2 미달)
    grid["b"][15] = "O"

    warns = _coverage_shortfall_warnings(
        db=db, draft_schedule=sched, draft_grid=grid,
        cutoff_idx=15, days_in_month=days, shift_meta={},
    )
    assert any("2026-03-16" in w and "데이 커버리지 미달 (1/2)" in w for w in warns), warns
    # cutoff 이전(3/1~3/15)은 스캔하지 않는다
    assert all("2026-03-01" not in w for w in warns)


def test_no_warning_when_coverage_met(db):
    sched = _mk_schedule(db, config_id=7778, schedule_id="draft0000002")
    days = 31
    grid = {
        "a": ["D"] * days, "b": ["D"] * days,
        "c": ["E"] * days, "d": ["E"] * days,
        "e": ["N"] * days,
    }
    warns = _coverage_shortfall_warnings(
        db=db, draft_schedule=sched, draft_grid=grid,
        cutoff_idx=15, days_in_month=days, shift_meta={},
    )
    assert warns == []
