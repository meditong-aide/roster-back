"""EI-13c-3 시험 — 공휴일 OFF·근무코드를 한 번만 읽고, 고정근무 병합은 재사용한다(A2 SPEC §3.3).

  1. `read_fixed_cut_inputs(shift_lookup_getter=…)` — getter 1회·`_load_shift_lookup` 0회·`inputs.shift_lookup` 노출.
  2. 생성 경로 — 고정근무자 없는 병동은 병합 때 두 값을 읽지 않고, 있는 병동은 차감 때 읽은 값을 병합이 다시 읽지 않는다.
"""
from __future__ import annotations

import pytest

from .harness import run_with_output
from .scenario_defs import SCENARIOS
from .scenario_ladder import LADDER_SCENARIOS
from .test_fixed_cut import _fixed_cut, _rcs, _seed_shift_and_nurse
from .test_mssql_shim import _add_config_rows, shim_db  # noqa: F401 — 픽스처 재사용
from .ward import GROUP_ID, OFFICE_ID

_YEAR, _MONTH = 2026, 2


def test_read_fixed_cut_inputs_uses_getter_once(shim_db, engine):  # noqa: F811
    from sqlalchemy import text

    from .capture import QueryLog

    nurse = _seed_shift_and_nurse(shim_db)
    shim_db.execute(text("ALTER TABLE roster_config ADD COLUMN fixed_holiday_off_yn INTEGER"))
    _add_config_rows(shim_db, [{"config_id": 1, "group_id": GROUP_ID}])
    shim_db.execute(text("UPDATE roster_config SET fixed_holiday_off_yn = 1 WHERE config_id = 1"))
    shim_db.flush()
    fc, rcs = _fixed_cut(), _rcs()
    from db.models import Shift

    lookup = {s.shift_id: s for s in shim_db.query(Shift).all()}
    calls: list[int] = []

    def getter():
        calls.append(1)
        return lookup

    log = QueryLog()
    log.attach(engine)
    log.armed = True
    try:
        with pytest.MonkeyPatch.context() as mp:
            def _no_load(*_a, **_k):
                raise AssertionError("getter 가 있으면 _load_shift_lookup 을 부르지 않는다")

            mp.setattr(rcs, "_load_shift_lookup", _no_load)
            inputs = fc.read_fixed_cut_inputs(shim_db, [nurse], office_id=OFFICE_ID, group_id=GROUP_ID,
                                              year=_YEAR, month=_MONTH, weekly_off_active=False,
                                              shift_lookup_getter=getter)
    finally:
        log.armed = False
        log.detach()
    assert calls == [1]
    assert inputs.shift_lookup is lookup
    assert "select:shifts" not in log.statements  # 근무코드 조회는 getter 몫 — 여기서는 0
    assert inputs.holiday_off is True


def test_read_fixed_cut_inputs_without_getter_unchanged(shim_db, engine):  # noqa: F811
    """getter 가 없으면 지금처럼 `_load_shift_lookup` 으로 읽고 그 맵을 `inputs.shift_lookup` 에 싣는다."""
    from sqlalchemy import text

    nurse = _seed_shift_and_nurse(shim_db)
    shim_db.execute(text("ALTER TABLE roster_config ADD COLUMN fixed_holiday_off_yn INTEGER"))
    _add_config_rows(shim_db, [{"config_id": 1, "group_id": GROUP_ID}])
    shim_db.flush()
    inputs = _fixed_cut().read_fixed_cut_inputs(shim_db, [nurse], office_id=OFFICE_ID, group_id=GROUP_ID,
                                                year=_YEAR, month=_MONTH, weekly_off_active=False)
    assert set(inputs.shift_lookup) == {"D"}


def _spy_counts(engine, scenario) -> dict[str, int]:
    rcs = _rcs()
    counts = {"holiday": 0, "shift_lookup": 0}
    real_holiday, real_lookup = rcs._holiday_off_enabled, rcs._load_shift_lookup

    def holiday(*a, **k):
        counts["holiday"] += 1
        return real_holiday(*a, **k)

    def lookup(*a, **k):
        counts["shift_lookup"] += 1
        return real_lookup(*a, **k)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(rcs, "_holiday_off_enabled", holiday)
        mp.setattr(rcs, "_load_shift_lookup", lookup)
        run_with_output(engine, scenario)
    return counts


def test_merge_reads_nothing_without_fixed_nurses(engine, engine_input_tables):
    """고정근무자 없는 병동(S1-L3 — 사다리 끝까지 간다): 병합이 공휴일 OFF·근무코드를 따로 읽지 않는다 → 공휴일 0 · 근무코드 1(단계 A 몫)."""
    assert _spy_counts(engine, LADDER_SCENARIOS["S1-L3"]) == {"holiday": 0, "shift_lookup": 1}


def test_cut_shares_stage_a_shift_lookup(engine, engine_input_tables):
    """S5(golden_stop — 병합 전에 멈춘다): 차감 블록이 단계 A 의 근무코드 getter 를 공유한다 — 공휴일 OFF 1·근무코드 1."""
    assert _spy_counts(engine, SCENARIOS["S5"]) == {"holiday": 1, "shift_lookup": 1}


def test_merge_reuses_cut_reads_when_merge_is_reached(engine, engine_input_tables):
    """S1-LT(고정근무자 F01 + 병합 도달): 병합이 차감이 읽은 값을 재사용한다 — 병합 도달 ≥1 · 공휴일 OFF 1 · 근무코드 1."""
    rcs = _rcs()
    merge = {"n": 0}
    real_merge = rcs._overlay_fixed_roster_with_fixed_wanted

    def spy(*a, **k):
        merge["n"] += 1
        return real_merge(*a, **k)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(rcs, "_overlay_fixed_roster_with_fixed_wanted", spy)
        counts = _spy_counts(engine, LADDER_SCENARIOS["S1-LT"])
    assert merge["n"] >= 1
    assert counts == {"holiday": 1, "shift_lookup": 1}
