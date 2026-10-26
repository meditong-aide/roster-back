"""EI-9-0 단위 테스트 — 고정근무 차감의 계산·읽기·래퍼(설계서 EI9_SPEC.md §4.4·§4.5).

U-1~U-15 는 전환 **뒤** 코드(`services.engine_input.fixed_cut`)에서 돈다 — 전환 전 코드엔
이 모듈이 없어 돌 수 없다. 옛 동작과의 대조는 U-9·U-14 안의 오라클 사본(`_oracle_*`, 3ede458
원문)이 맡는다. + S-1~S-4(전환 뒤 전용 — `rcs.read_fixed_cut_inputs` 등을 스파이해 호출 수·
예외 경로·입력 일치를 본다). 오라클은 고치지 말 것(9-0 불변의 기준). app 모듈은 가드 규약대로
함수 **안에서** 지연 import 한다(collection 시점 import 는 패키지 가드 밖이므로 — conftest.py
머릿말 참조).
"""
from __future__ import annotations

import ast
import calendar
import contextlib
import copy
import inspect
import io
import random
from collections import namedtuple
from datetime import date

import pytest

from .harness import run_with_output
from .scenario_defs import SCENARIOS
from .scenario_ladder import LADDER_SCENARIOS
from .test_fixed_cut_characterization import (
    _s5_allfixed,
    _s5_badcode,
    _s5_fw,
    _s5_max,
    _s5_raise,
    _s5_sp,
)
from .test_mssql_shim import _add_config_rows, shim_db  # noqa: F401 — 픽스처 재사용
from .ward import GROUP_ID, OFFICE_ID, nurse_row, stamp

_FakeEntry = namedtuple("_FakeEntry", ["nurse_id", "shift_date", "shift_id"])
_YEAR, _MONTH, _DAYS = 2026, 2, 28  # 2026-02 — 28일(평년)


def _fixed_cut():
    """`services.engine_input.fixed_cut` 모듈(지연 import — 가드 규약)."""
    from services.engine_input import fixed_cut

    return fixed_cut


def _rcs():
    """`services.roster_create_service` 모듈(지연 import — 가드 규약)."""
    from services import roster_create_service

    return roster_create_service


def _by_day_list(day0: dict | None = None, *, days: int = _DAYS) -> list[dict]:
    """하루만 값이 있고 나머지는 빈 dict 인 by_day 리스트."""
    rows = [{} for _ in range(days)]
    if day0 is not None:
        rows[0] = dict(day0)
    return rows


def _roster(code0: str, *, days: int = _DAYS, nurse_id: str = "F01") -> dict[str, list[str]]:
    row = ["-"] * days
    row[0] = code0
    return {nurse_id: row}


def _inputs(roster: dict[str, list[str]], wanted_rows: tuple = ()):
    fc = _fixed_cut()
    return fc.FixedCutInputs(holiday_off=False, base_roster=roster, wanted_rows=wanted_rows)


def _compute(roster, demand_config, special=(), *, wanted_rows: tuple = ()):
    fc = _fixed_cut()
    return fc.compute_fixed_cut_demand(
        _inputs(roster, wanted_rows), special, demand_config, year=_YEAR, month=_MONTH)


# ── U-1 차감 없음 ─────────────────────────────────────────────────────────


def test_u1_no_cut_when_code_absent_or_zero():
    fc = _fixed_cut()
    demand_config = {"daily_shift_requirements_by_day": _by_day_list({"D": 2})}
    demand = _compute(_roster("주"), demand_config)  # '주' 는 수요에 없는 코드
    assert demand.by_day is None and demand.max_by_day is None
    assert (demand.cut_min, demand.cut_max) == (0, 0)

    demand_config2 = {"daily_shift_requirements_by_day": _by_day_list({"O": 0, "D": 2})}
    demand2 = _compute(_roster("O"), demand_config2)  # 'O' 는 있지만 수요 0
    assert demand2.cut_min == 0

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        fc.apply_fixed_cut_demand(demand_config, demand, n_fixed=1)
    assert buf.getvalue() == ""


# ── U-2 min 만 ────────────────────────────────────────────────────────────


def test_u2_min_only_when_max_all_zero():
    demand_config = {
        "daily_shift_requirements_by_day": _by_day_list({"D": 2}),
        "daily_shift_requirements_max_by_day": _by_day_list({"D": 0}),
    }
    demand = _compute(_roster("D"), demand_config)
    assert demand.by_day is not None and demand.cut_min == 1
    assert demand.max_by_day is None and demand.cut_max == 0


# ── U-3 max 0 되올림 ──────────────────────────────────────────────────────


def test_u3_raise_max_below_min():
    demand_config = {
        "daily_shift_requirements_by_day": _by_day_list({"D": 2, "E": 0}),
        "daily_shift_requirements_max_by_day": _by_day_list({"D": 1, "E": 0}),
    }
    demand = _compute(_roster("D"), demand_config)
    assert (demand.cut_min, demand.cut_max, demand.raised) == (1, 1, 1)
    assert demand.by_day[0]["D"] == 1
    assert demand.max_by_day[0]["D"] == 1  # 되올림(0 → min 1)
    assert demand.max_by_day[0]["E"] == 0  # min 0 + max 0 은 그대로(되올림 아님)

    # cut_min=0 인데도 cut_max>0·raised>0 인 분기(리뷰 MEDIUM) — 로스터 칸(D)의 min 이 이미
    # 0 이라 min 차감은 안 일어나지만 max 차감은 일어나고, 그 되올림이 **다른 날의 다른
    # 코드(2일 E)** 에서 터진다. `if cut_max and ...` 를 `if cut_min and ...` 로 바꾸는
    # 변이가 이 분기를 안 거치면 걸리지 않는다.
    fc = _fixed_cut()
    days = _DAYS
    by_day2 = [{"D": 0} for _ in range(days)]
    by_day2[1]["E"] = 2
    by_day_max2 = [{"D": 1, "E": 1} for _ in range(days)]
    demand_config2 = {"daily_shift_requirements_by_day": by_day2,
                      "daily_shift_requirements_max_by_day": by_day_max2}
    inputs2 = fc.FixedCutInputs(holiday_off=False, base_roster=_roster("D"), wanted_rows=())
    demand2 = fc.compute_fixed_cut_demand(inputs2, (), demand_config2, year=_YEAR, month=_MONTH)
    assert (demand2.cut_min, demand2.cut_max, demand2.raised) == (0, 1, 1)
    assert demand2.by_day is None  # min 차감이 없었다 — 대입 안 함
    assert demand2.max_by_day[1]["E"] == 2  # 되올림(1 → min 2, 2일)


# ── U-4 확정 원티드 덮기 ──────────────────────────────────────────────────


def test_u4_fixed_wanted_overlay():
    base_roster = {"F01": ["D"] * _DAYS}
    entries = (
        _FakeEntry("F01", date(_YEAR, _MONTH, 1), "O"),     # 1일 → O(평일 D 가 덮임)
        _FakeEntry("F01", date(_YEAR, _MONTH, 2), "E"),      # 2일 → E
        _FakeEntry("F01", date(2026, 1, 30), "O"),           # day=30 > 28일 → 무시(범위 밖)
        _FakeEntry("F01", date(_YEAR, _MONTH, 3), ""),       # 빈 코드 → 무시
        _FakeEntry("F01", None, "O"),                         # shift_date None → 무시
    )
    full_by_day = [{"D": 2, "E": 2} for _ in range(_DAYS)]
    demand_config = {"daily_shift_requirements_by_day": full_by_day}
    demand = _compute(base_roster, demand_config, wanted_rows=entries)
    assert demand.final_roster["F01"][0] == "O"
    assert demand.final_roster["F01"][1] == "E"
    assert demand.final_roster["F01"][2] == "D"  # 빈 코드 무시 → 원본(D) 유지
    assert demand.by_day[0]["D"] == 2  # 1일 O 라서 D 차감 없음
    assert demand.by_day[1]["E"] == 1  # 2일 E 차감


# ── U-5 특수요청 덮기(확정 원티드보다 나중 — 이긴다) ───────────────────────


def test_u5_special_request_overrides_fixed_wanted():
    base_roster = {"F01": ["D"] * _DAYS}
    wanted = (_FakeEntry("F01", date(_YEAR, _MONTH, 1), "E"),)  # 1일 확정원티드 E
    special = [
        {"nurse_id": "F01", "day": 1, "shift_id": "", "shift_type": "휴가"},   # 같은 날 — 휴가가 이김
        {"nurse_id": "F01", "day": 2, "shift_id": "E", "shift_type": "근무"},
    ]
    full_by_day = [{"D": 2, "E": 2} for _ in range(_DAYS)]
    demand_config = {"daily_shift_requirements_by_day": full_by_day}
    demand = _compute(base_roster, demand_config, special, wanted_rows=wanted)
    assert demand.final_roster["F01"][0] == "O"  # 확정원티드(E) 를 특수요청(휴가)이 덮음
    assert demand.final_roster["F01"][1] == "E"
    assert demand.by_day[0]["E"] == 2  # O 라서 E 차감 없음
    assert demand.by_day[1]["E"] == 1  # 2일 E 차감


# ── U-6 by_day 모양 ───────────────────────────────────────────────────────


def test_u6_by_day_shape_fallback():
    demand_config = {"day_req": 3, "eve_req": 1, "nig_req": 0}  # daily_shift_requirements 없음
    demand = _compute(_roster("D"), demand_config)
    assert demand.cut_min == 1
    assert demand.by_day[0]["D"] == 2  # 기본요구 3 에서 1 차감

    demand_config2 = {
        "daily_shift_requirements": {"D": 4},
        "daily_shift_requirements_by_day": [{"D": 4}],  # 길이 1 ≠ 28(불일치)
    }
    demand2 = _compute(_roster("D"), demand_config2)
    assert demand2.cut_min == 1
    assert len(demand2.by_day) == _DAYS
    assert demand2.by_day[0]["D"] == 3

    demand3 = _compute(_roster("D"), {"day_req": 0, "eve_req": 0, "nig_req": 0})
    assert demand3.by_day is None  # 차감 0 → 대입 안 함(원문과 같은 의미)


# ── U-7 순수성 ────────────────────────────────────────────────────────────


def test_u7_purity_and_signature_has_no_db():
    fc = _fixed_cut()
    base_roster = {"F01": ["D"] * _DAYS}
    base_roster_copy = copy.deepcopy(base_roster)
    demand_config = {"daily_shift_requirements_by_day": [{"D": 2} for _ in range(_DAYS)]}
    demand_config_copy = copy.deepcopy(demand_config)
    # 확정 원티드로 1일을 덮는다 — compute 가 `base_roster` 를 사본 없이 제자리 변경하면
    # 여기서 드러난다(wanted_rows 가 비어 있으면 덮을 셀이 없어 변경 여부를 못 본다).
    wanted = (_FakeEntry("F01", date(_YEAR, _MONTH, 1), "O"),)
    inputs = _inputs(base_roster, wanted_rows=wanted)
    d1 = fc.compute_fixed_cut_demand(inputs, (), demand_config, year=_YEAR, month=_MONTH)
    d2 = fc.compute_fixed_cut_demand(inputs, (), demand_config, year=_YEAR, month=_MONTH)
    assert d1.by_day == d2.by_day and d1.cut_min == d2.cut_min
    assert d1.final_roster["F01"][0] == "O"
    assert base_roster == base_roster_copy  # inputs.base_roster 는 바뀌면 안 된다
    assert demand_config == demand_config_copy
    assert "db" not in inspect.signature(fc.compute_fixed_cut_demand).parameters
    assert "db" not in inspect.signature(fc.apply_fixed_cut_demand).parameters


# ── U-8 계산 예외 ─────────────────────────────────────────────────────────


def test_u8_compute_raises_apply_not_called_config_untouched():
    fc = _fixed_cut()
    full_by_day = [{"D": "x", "E": 2} for _ in range(_DAYS)]
    full_max = [{"D": 1, "E": 1} for _ in range(_DAYS)]
    demand_config = {"daily_shift_requirements_by_day": full_by_day,
                     "daily_shift_requirements_max_by_day": full_max}
    roster = {"F01": ["E"] * _DAYS}  # 'D' 는 로스터에 전혀 없다 — 컷 루프는 안 건드리고 되올림 루프만 본다
    inputs = _inputs(roster)
    with pytest.raises(ValueError):
        fc.compute_fixed_cut_demand(inputs, (), demand_config, year=_YEAR, month=_MONTH)
    assert demand_config["daily_shift_requirements_by_day"] == full_by_day
    assert demand_config["daily_shift_requirements_max_by_day"] == full_max


# ── U-9 차분(오라클 vs 새 코드, 400건) ────────────────────────────────────


def _oracle_compute_and_apply(config_dict: dict, fx_roster: dict, n_fixed: int,
                              year: int, month: int) -> list[str]:
    """3ede458 원문 사본 — 고치지 말 것(9-0 불변의 기준). rcs :6704-:6767(차감·대입)."""
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        _days_in_month = calendar.monthrange(year, month)[1]
        _base_req = dict(config_dict.get("daily_shift_requirements") or {}) or {
            "D": int(config_dict.get("day_req") or 0),
            "E": int(config_dict.get("eve_req") or 0),
            "N": int(config_dict.get("nig_req") or 0),
        }
        _by_day = config_dict.get("daily_shift_requirements_by_day")
        if not (isinstance(_by_day, list) and len(_by_day) == _days_in_month):
            _by_day = [dict(_base_req) for _ in range(_days_in_month)]
        else:
            _by_day = [dict(dm or {}) for dm in _by_day]
        _by_day_max = config_dict.get("daily_shift_requirements_max_by_day")
        _has_max = isinstance(_by_day_max, list) and len(_by_day_max) == _days_in_month
        _max_active: list[set] = []
        if _has_max:
            _by_day_max = [dict(dm or {}) for dm in _by_day_max]
            _max_active = [{_k for _k, _v in dm.items() if int(_v or 0) > 0} for dm in _by_day_max]
        _fx_cut = 0
        _fx_cut_max = 0
        for _codes in fx_roster.values():
            for _d, _code in enumerate(_codes or []):
                if _d >= _days_in_month:
                    break
                _c = str(_code or "").strip().upper()
                if _c in _by_day[_d] and int(_by_day[_d][_c] or 0) > 0:
                    _by_day[_d][_c] = int(_by_day[_d][_c]) - 1
                    _fx_cut += 1
                if _has_max and int(_by_day_max[_d].get(_c) or 0) > 0:
                    _by_day_max[_d][_c] = int(_by_day_max[_d][_c]) - 1
                    _fx_cut_max += 1
        if _fx_cut:
            config_dict["daily_shift_requirements_by_day"] = _by_day
        if _fx_cut_max:
            _raised = 0
            for _d in range(_days_in_month):
                for _c in _max_active[_d]:
                    _mn = int(_by_day[_d].get(_c) or 0)
                    if int(_by_day_max[_d].get(_c) or 0) < _mn:
                        _by_day_max[_d][_c] = _mn
                        _raised += 1
            config_dict["daily_shift_requirements_max_by_day"] = _by_day_max
            if _raised:
                print(f"[FixedShiftCoverage] max < min 이 된 {_raised}칸을 min 으로 되올림 "
                      f"(원설정이 max < min 인 칸)")
        if _fx_cut or _fx_cut_max:
            print(f"[FixedShiftCoverage] 고정근무자 {n_fixed}명 · "
                  f"요구 차감 min {_fx_cut}셀 / max {_fx_cut_max}셀 — 솔버는 남은 인원만 채운다")
    return buf.getvalue().splitlines()


_DEMAND_CODES = ("D", "E", "N", "M")
_ROSTER_CODES = ("D", "E", "N", "M", "O", "주", "-", "d", "", None)
_VALUE_POOL = (0, 1, 2, 3, None, "2")
_DAYS_BY_MONTH = {28: (2026, 2), 30: (2026, 4), 31: (2026, 1)}


def _gen_u9_case(rng: random.Random) -> dict:
    """U-9 생성 — 날짜 28/30/31 · 수요 코드 부분집합 · by_day/max 모양 변주 · 로스터 1~3명."""
    days = rng.choice((28, 30, 31))
    year, month = _DAYS_BY_MONTH[days]
    codes = rng.sample(_DEMAND_CODES, k=rng.randint(1, len(_DEMAND_CODES)))
    demand_config: dict = {"day_req": rng.choice(_VALUE_POOL), "eve_req": rng.choice(_VALUE_POOL),
                           "nig_req": rng.choice(_VALUE_POOL)}
    if rng.random() < 0.5:
        demand_config["daily_shift_requirements"] = {c: rng.choice(_VALUE_POOL) for c in codes}
    by_day_mode = rng.choice(("none", "mismatch", "normal"))
    if by_day_mode == "none":
        demand_config["daily_shift_requirements_by_day"] = None
    elif by_day_mode == "mismatch":
        demand_config["daily_shift_requirements_by_day"] = [
            {c: rng.choice(_VALUE_POOL) for c in codes} for _ in range(days - 1)]
    else:
        demand_config["daily_shift_requirements_by_day"] = [
            {c: rng.choice(_VALUE_POOL) for c in codes} for _ in range(days)]
    max_mode = rng.choice(("none", "zero", "normal", "contradictory"))
    if max_mode == "none":
        demand_config["daily_shift_requirements_max_by_day"] = None
    elif max_mode == "zero":
        demand_config["daily_shift_requirements_max_by_day"] = [{c: 0 for c in codes}
                                                                 for _ in range(days)]
    elif max_mode == "normal":
        demand_config["daily_shift_requirements_max_by_day"] = [
            {c: rng.choice((0, 1, 2, 3)) for c in codes} for _ in range(days)]
    else:
        demand_config["daily_shift_requirements_max_by_day"] = [
            {c: rng.choice((0, 1)) for c in codes} for _ in range(days)]
    n_nurses = rng.randint(1, 3)
    roster = {f"N{i:02d}": [rng.choice(_ROSTER_CODES) for _ in range(days)] for i in range(n_nurses)}
    return {"year": year, "month": month, "demand_config": demand_config, "roster": roster,
            "n_fixed": n_nurses}


def test_u9_differential_400_cases():
    fc = _fixed_cut()
    rng = random.Random(20261015)
    for i in range(400):
        case = _gen_u9_case(rng)
        oracle_cfg = copy.deepcopy(case["demand_config"])
        oracle_lines = _oracle_compute_and_apply(
            oracle_cfg, copy.deepcopy(case["roster"]), case["n_fixed"], case["year"], case["month"])

        new_cfg = copy.deepcopy(case["demand_config"])
        inputs = fc.FixedCutInputs(holiday_off=False, base_roster=copy.deepcopy(case["roster"]),
                                   wanted_rows=())
        demand = fc.compute_fixed_cut_demand(inputs, (), new_cfg, year=case["year"],
                                             month=case["month"])
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            fc.apply_fixed_cut_demand(new_cfg, demand, n_fixed=case["n_fixed"])
        new_lines = buf.getvalue().splitlines()

        assert new_cfg.get("daily_shift_requirements_by_day") == \
            oracle_cfg.get("daily_shift_requirements_by_day"), i
        assert new_cfg.get("daily_shift_requirements_max_by_day") == \
            oracle_cfg.get("daily_shift_requirements_max_by_day"), i
        assert new_lines == oracle_lines, i


# ── U-10 apply ────────────────────────────────────────────────────────────


def test_u10_apply_assigns_by_key_and_preserves_identity(capsys):
    fc = _fixed_cut()
    original_by_day = [{"D": 9}]
    original_max = [{"D": 9}]
    config_dict = {"daily_shift_requirements_by_day": original_by_day,
                   "daily_shift_requirements_max_by_day": original_max}
    demand = fc.FixedCutDemand(by_day=None, max_by_day=None, cut_min=0, cut_max=0, raised=0,
                               final_roster={})
    fc.apply_fixed_cut_demand(config_dict, demand, n_fixed=3)
    assert config_dict["daily_shift_requirements_by_day"] is original_by_day
    assert config_dict["daily_shift_requirements_max_by_day"] is original_max
    assert capsys.readouterr().out == ""

    new_by_day = [{"D": 1}]
    new_max = [{"D": 0}]
    demand2 = fc.FixedCutDemand(by_day=new_by_day, max_by_day=new_max, cut_min=1, cut_max=1,
                                raised=1, final_roster={})
    fc.apply_fixed_cut_demand(config_dict, demand2, n_fixed=2)
    assert config_dict["daily_shift_requirements_by_day"] is new_by_day
    assert config_dict["daily_shift_requirements_max_by_day"] is new_max
    out = capsys.readouterr().out.splitlines()
    assert out == [
        "[FixedShiftCoverage] max < min 이 된 1칸을 min 으로 되올림 (원설정이 max < min 인 칸)",
        "[FixedShiftCoverage] 고정근무자 2명 · 요구 차감 min 1셀 / max 1셀 — 솔버는 남은 인원만 채운다",
    ]


# ── U-11~U-13 읽기(shim_db) ───────────────────────────────────────────────


def _seed_shift_and_nurse(db, *, fixed_shift: str = "D") -> object:
    """U-11~U-14 공용 — Shift 1건(D) + 고정근무자 F01 을 넣고 Nurse 객체를 돌려준다."""
    from datetime import time

    from db.models import Nurse, Shift

    from .ward import shift_row

    db.add(Shift(**shift_row("D", 1, "데이", "D", "근무", "D", 1, time(7, 0), time(15, 0))))
    nurse = Nurse(**nurse_row("F01", 1, fixed_shift=fixed_shift), **stamp())
    db.add(nurse)
    db.flush()
    return nurse


def test_u11_read_order(shim_db, engine):  # noqa: F811 — 픽스처 재사용(이름 일치 필요)
    from sqlalchemy import text

    from .capture import QueryLog

    nurse = _seed_shift_and_nurse(shim_db)
    shim_db.execute(text("ALTER TABLE roster_config ADD COLUMN fixed_holiday_off_yn INTEGER"))
    _add_config_rows(shim_db, [{"config_id": 1, "group_id": GROUP_ID}])
    shim_db.execute(text("UPDATE roster_config SET fixed_holiday_off_yn = 1 WHERE config_id = 1"))

    log = QueryLog()
    log.attach(engine)
    log.armed = True
    try:
        fc = _fixed_cut()
        inputs = fc.read_fixed_cut_inputs(shim_db, [nurse], office_id=OFFICE_ID, group_id=GROUP_ID,
                                          year=_YEAR, month=_MONTH, weekly_off_active=False)
    finally:
        log.armed = False
        log.detach()
    assert log.statements == [
        "select:information_schema.columns", "select:roster_config",
        "select:shifts", "select:fixed_wanted_entries",
    ]
    assert inputs.holiday_off is True
    assert inputs.base_roster["F01"][1] == "D"  # 2026-02-02(월, 평일·비공휴일)
    assert inputs.wanted_rows == ()

    # M17 증거 — weekly_off_active 파라미터가 실제로 쓰여야 한다(무시하고 True 로 고정하면 안 됨).
    # F01 은 weekly_off_enabled=1 이지만 이 호출은 weekly_off_active=False 를 넘긴다 → 일요일(2/1)은
    # 그래도 'O'(weekly_off_active=False 가 AND 로 눌러야 한다) — 'True 고정' 변이면 '주' 가 된다.
    nurse.weekly_off_enabled = 1
    shim_db.flush()
    inputs2 = fc.read_fixed_cut_inputs(shim_db, [nurse], office_id=OFFICE_ID, group_id=GROUP_ID,
                                       year=_YEAR, month=_MONTH, weekly_off_active=False)
    assert inputs2.base_roster["F01"][0] == "O"  # 2026-02-01(일)


def test_u12_read_unknown_code_raises_400_without_wanted_query(shim_db, engine):  # noqa: F811 — 픽스처 재사용(이름 일치 필요)
    from fastapi import HTTPException
    from sqlalchemy import text

    from .capture import QueryLog

    nurse = _seed_shift_and_nurse(shim_db, fixed_shift="Z")  # shifts 에 없는 코드
    shim_db.execute(text("ALTER TABLE roster_config ADD COLUMN fixed_holiday_off_yn INTEGER"))

    log = QueryLog()
    log.attach(engine)
    log.armed = True
    try:
        fc = _fixed_cut()
        with pytest.raises(HTTPException) as exc_info:
            fc.read_fixed_cut_inputs(shim_db, [nurse], office_id=OFFICE_ID, group_id=GROUP_ID,
                                     year=_YEAR, month=_MONTH, weekly_off_active=False)
    finally:
        log.armed = False
        log.detach()
    assert exc_info.value.status_code == 400
    assert "select:fixed_wanted_entries" not in log.statements  # P-4 의 단위판


def test_u13_read_no_fixed_nurses(shim_db, engine):  # noqa: F811 — 픽스처 재사용(이름 일치 필요)
    from sqlalchemy import text

    from .capture import QueryLog

    shim_db.execute(text("ALTER TABLE roster_config ADD COLUMN fixed_holiday_off_yn INTEGER"))

    log = QueryLog()
    log.attach(engine)
    log.armed = True
    try:
        fc = _fixed_cut()
        inputs = fc.read_fixed_cut_inputs(shim_db, [], office_id=OFFICE_ID, group_id=GROUP_ID,
                                          year=_YEAR, month=_MONTH, weekly_off_active=False)
    finally:
        log.armed = False
        log.detach()
    assert inputs.base_roster == {}
    assert inputs.wanted_rows == ()
    assert "select:fixed_wanted_entries" not in log.statements


# ── U-14 래퍼 ─────────────────────────────────────────────────────────────


def test_u14_wrapper_matches_oracle(shim_db, engine):  # noqa: F811 — 픽스처 재사용(이름 일치 필요)
    from .capture import QueryLog

    rcs = _rcs()

    log = QueryLog()
    log.attach(engine)
    log.armed = True
    try:
        empty = rcs._overlay_fixed_roster_with_fixed_wanted(shim_db, {}, GROUP_ID, _YEAR, _MONTH)
    finally:
        log.armed = False
        log.detach()
    assert empty == {} and empty is not None
    assert log.statements == []  # 빈 로스터 → 조회 0(조기 반환)

    _seed_shift_and_nurse(shim_db)
    db_entries = [
        {"group_id": GROUP_ID, "year": _YEAR, "month": _MONTH, "nurse_id": "F01",
         "shift_date": date(_YEAR, _MONTH, 1), "shift_id": "O", "is_applied": True,
         "source_type": "original", "original_shift_id": "O", "created_by": "HN01", **stamp()},
        # 미적용(is_applied=False) 행 — 읽기의 필터가 빠지면(N3) 이 칸도 덮인다.
        {"group_id": GROUP_ID, "year": _YEAR, "month": _MONTH, "nurse_id": "F01",
         "shift_date": date(_YEAR, _MONTH, 2), "shift_id": "E", "is_applied": False,
         "source_type": "original", "original_shift_id": "E", "created_by": "HN01", **stamp()},
    ]
    from db.models import FixedWantedEntry

    for row in db_entries:
        shim_db.add(FixedWantedEntry(**row))
    shim_db.flush()

    roster = {"F01": ["D"] * _DAYS}
    roster_for_oracle = copy.deepcopy(roster)
    log2 = QueryLog()
    log2.attach(engine)
    log2.armed = True
    try:
        result = rcs._overlay_fixed_roster_with_fixed_wanted(shim_db, roster, GROUP_ID, _YEAR, _MONTH)
    finally:
        log2.armed = False
        log2.detach()
    assert log2.statements == ["select:fixed_wanted_entries"]
    assert result["F01"][0] == "O"
    assert result["F01"][1] == "D"  # 미적용 행은 무시 — 원본(D) 유지

    # 오라클 입력은 읽기 함수(`_read_fixed_wanted_rows`)를 다시 불러 짓지 않는다 — 그러면 읽기의
    # is_applied 필터가 빠졌을 때 오라클도 같은 오류를 공유해 차이가 드러나지 않는다(N3, 리뷰
    # MEDIUM). "적용된 행만"을 테스트에서 직접 짓는다 — DB 에 심은 행 중 is_applied=True 뿐.
    applied_entries = [_FakeEntry("F01", date(_YEAR, _MONTH, 1), "O")]
    oracle_result = _oracle_overlay_fixed_wanted(roster_for_oracle, applied_entries, _YEAR, _MONTH)
    assert result == oracle_result


def _oracle_overlay_fixed_wanted(fixed_roster: dict, entries, year: int, month: int) -> dict:
    """3ede458 원문 사본 — 고치지 말 것. 옛 `_overlay_fixed_roster_with_fixed_wanted` 본문
    (행 목록을 받는 형태, 원문 :1551-:1585)."""
    if not fixed_roster:
        return fixed_roster
    nurse_ids = list(fixed_roster.keys())
    if not nurse_ids:
        return fixed_roster
    days_in_month = calendar.monthrange(year, month)[1]
    for e in entries:
        nurse_id = str(e.nurse_id)
        row = fixed_roster.get(nurse_id)
        if not row or e.shift_date is None:
            continue
        day = e.shift_date.day
        if day <= 0 or day > days_in_month:
            continue
        day_idx = day - 1
        if day_idx >= len(row):
            continue
        code = str(e.shift_id or "").strip()
        if not code:
            continue
        row[day_idx] = code
        fixed_roster[nurse_id] = row
    return fixed_roster


# ── U-15 import 모양 ──────────────────────────────────────────────────────


def test_u15_fixed_cut_has_no_top_level_rcs_import():
    from pathlib import Path

    import services.engine_input as engine_input_pkg
    import services.engine_input.fixed_cut as fixed_cut_module

    tree = ast.parse(Path(fixed_cut_module.__file__).read_text())
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, ast.ImportFrom):
            assert node.module != "services.roster_create_service", ast.dump(node)
        elif isinstance(node, ast.Import):
            assert all("roster_create_service" not in alias.name for alias in node.names)

    init_tree = ast.parse(Path(engine_input_pkg.__file__).read_text())
    init_imports = [n for n in ast.walk(init_tree)
                    if isinstance(n, (ast.Import, ast.ImportFrom))
                    and getattr(n, "module", None) != "__future__"]
    assert init_imports == []


# ── S-1~S-4 전환 뒤 전용 ───────────────────────────────────────────────────


def _spy_calls(mp, module, name: str) -> list:
    """`module.<name>` 을 원본을 그대로 부르는 호출-카운터로 바꾸고 호출 기록 리스트를 돌려준다."""
    original = getattr(module, name)
    calls: list = []

    def spy(*args, **kwargs):
        calls.append((args, kwargs))
        return original(*args, **kwargs)

    mp.setattr(module, name, spy)
    return calls


def test_s1_read_inputs_call_counts(engine, engine_input_tables):
    # S1-LT(사다리)는 `redirect_live_graph` 패치가 모듈 전역 `ladder.LIVE_GRAPH_DIRS` 에
    # 임시 폴더를 쌓는다 — test_ladder.py 의 `drain_live_graph_dirs()` 가 그 사이 누적분을
    # 세므로, 여기서 돌린 뒤 바로 비워야 다른 테스트 파일의 집계를 오염시키지 않는다.
    from .ladder import drain_live_graph_dirs

    rcs = _rcs()
    cases = [
        (SCENARIOS["S5"], 1), (SCENARIOS["S9-F"], 1), (SCENARIOS["S1"], 0),
        (_s5_badcode(), 1), (_s5_raise(), 1), (_s5_max(), 1), (_s5_fw(), 1),
        (_s5_allfixed(), 0), (_s5_sp(), 1), (LADDER_SCENARIOS["S1-LT"], 1),
    ]
    try:
        for scenario, want in cases:
            with pytest.MonkeyPatch.context() as mp:
                calls = _spy_calls(mp, rcs, "read_fixed_cut_inputs")
                run_with_output(engine, scenario)
            assert len(calls) == want, scenario.name
    finally:
        drain_live_graph_dirs()


def test_s2_compute_exception_leaves_demand_unapplied(engine, engine_input_tables):
    rcs = _rcs()

    def boom(*_a, **_k):
        raise RuntimeError("compute-boom")

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(rcs, "compute_fixed_cut_demand", boom)
        run = run_with_output(engine, SCENARIOS["S5"])
    r = run.result
    assert (r.outcome, r.path) == ("golden_stop", "first_attempt")
    lines = [ln for ln in run.stdout.splitlines()
             if ln.startswith(("[FixedShift", "[Holiday]", "[FixedShiftCoverage]"))]
    # S5 의 정상 7줄 중 4번째(차감 요약)만 오류 줄로 바뀐다 — 앞뒤 shift_lookup·공휴일·적용 로그는 그대로.
    assert lines == [
        "[FixedShift] 고정 근무 간호사 1명 분리됨 → ['F01(F01): fixed_shift=D']",
        "[FixedShift] shift_lookup 로드: 4건",
        "[Holiday] 공휴일 OFF 적용 — days=[25]",
        "[FixedShift] 적용: F01(F01) 코드=D, 범위=1~31일",
        "[FixedShiftCoverage] 차감 실패(무시): RuntimeError: compute-boom",
    ]
    q = r.queries
    lo = q["marks"]["L0"]
    cut = ["select:information_schema.columns", "select:roster_config", "select:shifts", "select:fixed_wanted_entries"]
    head = q["tables"][:lo]
    # 읽기는 이미 돎(compute 가 실패) — S5 와 같은 4건이 precheck(L0 표지) 앞에 있다(EI-10g2: 차감이 precheck 앞으로 옮겨졌다)
    assert any(head[i:i + len(cut)] == cut for i in range(len(head))), head[-12:]


def test_s3_apply_exception_still_golden_stop(engine, engine_input_tables):
    rcs = _rcs()

    def boom(*_a, **_k):
        raise RuntimeError("apply-boom")

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(rcs, "apply_fixed_cut_demand", boom)
        run = run_with_output(engine, SCENARIOS["S5"])
    r = run.result
    assert (r.outcome, r.path) == ("golden_stop", "first_attempt")
    lines = [ln for ln in run.stdout.splitlines()
             if ln.startswith(("[FixedShift", "[Holiday]", "[FixedShiftCoverage]"))]
    assert lines == [
        "[FixedShift] 고정 근무 간호사 1명 분리됨 → ['F01(F01): fixed_shift=D']",
        "[FixedShift] shift_lookup 로드: 4건",
        "[Holiday] 공휴일 OFF 적용 — days=[25]",
        "[FixedShift] 적용: F01(F01) 코드=D, 범위=1~31일",
        "[FixedShiftCoverage] 차감 실패(무시): RuntimeError: apply-boom",
    ]


def test_s4_compute_inputs_match_l0_and_special_requests(engine, engine_input_tables):
    rcs = _rcs()
    captured: dict = {}
    original = rcs.compute_fixed_cut_demand

    def spy(inputs, special_requests, demand_config, **kwargs):
        captured["demand_config"] = dict(demand_config)
        captured["special_requests"] = list(special_requests)
        return original(inputs, special_requests, demand_config, **kwargs)

    original_stage_a = rcs.stage_a_at_precheck

    def stage_a_spy(db, **kwargs):
        captured["pre_cut"] = copy.deepcopy(kwargs["config_dict"])   # 단계 A 입구 = 차감 전(차감은 그 직후다 — EI-10g2)
        return original_stage_a(db, **kwargs)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(rcs, "compute_fixed_cut_demand", spy)
        mp.setattr(rcs, "stage_a_at_precheck", stage_a_spy)
        run = run_with_output(engine, SCENARIOS["S5"])
    r = run.result
    l0_config = r.layers["L0"][0]["config_dict"]
    fc = _fixed_cut()
    for key in fc.FIXED_CUT_DEMAND_KEYS:
        # 차감 입력 = 단계 A 입구(차감 전) 값. L0(precheck 입력)는 이제 차감 뒤라 같지 않다 — 차감이 실제로 수요를 줄였다.
        assert captured["demand_config"].get(key) == captured["pre_cut"].get(key), key
    assert l0_config["daily_shift_requirements_by_day"] != captured["pre_cut"]["daily_shift_requirements_by_day"]
    assert captured["special_requests"] == []  # S5 병동엔 특수요청 없음


def test_s4b_special_requests_reach_compute_at_callsite(engine, engine_input_tables):
    """S5-SP(확정 원티드 없음 · 제출 원티드 특수요청만) — 호출 자리의 `fixed_special_shift_requests`
    인자가 실제로 compute 에 넘어가는지(리뷰 HIGH — 회귀해도 기존 테스트가 하나도 못 잡던 자리)."""
    rcs = _rcs()
    captured: dict = {}
    original = rcs.compute_fixed_cut_demand

    def spy(inputs, special_requests, demand_config, **kwargs):
        captured["special_requests"] = list(special_requests)
        return original(inputs, special_requests, demand_config, **kwargs)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(rcs, "compute_fixed_cut_demand", spy)
        run_with_output(engine, _s5_sp())
    assert captured["special_requests"] == [
        {"nurse_id": "F01", "day": 9, "shift_id": "연", "shift_type": "휴가"},
    ]
