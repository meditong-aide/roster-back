# tests/test_allowed_shift_period_wiring.py
"""P3 — allowed_shifts 솔버 주입의 period day-grain 전환.

build_allowed_shift_type_constraints 가 NurseAllowedShiftPeriod(시점)를 일자별로 읽고,
구간 없으면 nurses 캐시로 폴백하는지(무회귀) 검증.
참조: app/services/roster_create_service.py.
"""
from __future__ import annotations

from datetime import date

import pytest

from db.models import Nurse, NurseAllowedShiftPeriod
from services.roster_create_service import build_allowed_shift_type_constraints

Y, M = 2026, 7  # 31일


def _nurse(db, nid, allowed_shifts):
    n = Nurse(nurse_id=nid, account_id=f"acc_{nid}", name=nid, group_id=None,
              allowed_shifts=allowed_shifts, joining_date=None, resignation_date=None,
              active=1)
    db.add(n); db.flush()
    return n


def _call(nurses, db=None):
    return build_allowed_shift_type_constraints(
        nurses_in_group=nurses, year=Y, month=M,
        shift_manage_data=[], fixed_cells=None, use_mid=False, db=db,
    )["forbidden"]


def test_db_none_backward_compat(db):
    # N전담(allowed={N}) → 월 전체 D,E 금지. db 미전달 = 기존 동작.
    n = _nurse(db, "bc1", ["N"])
    forb = _call([n], db=None)
    assert set(forb["bc1"].keys()) == set(range(31))         # 전일
    assert all(v == ["D", "E"] for v in forb["bc1"].values())


def test_period_education_d_to_de(db):
    # 캐시=제한없음([]), period: 7/1~7/22 = ["D"], 7/22~ = ["D","E"]
    n = _nurse(db, "edu", [])
    db.add(NurseAllowedShiftPeriod(nurse_id="edu", valid_from=date(2026, 7, 1),
           valid_to=date(2026, 7, 22), allowed_shifts=["D"]))
    db.add(NurseAllowedShiftPeriod(nurse_id="edu", valid_from=date(2026, 7, 22),
           valid_to=None, allowed_shifts=["D", "E"]))
    db.flush()

    forb = _call([n], db=db)["edu"]
    # day_idx d → 달력 d+1일. 7/21=d20(["D"]→E,N 금지), 7/22=d21(["D","E"]→N 금지)
    assert forb[20] == ["E", "N"]      # 7/21: D만 허용
    assert forb[21] == ["N"]           # 7/22: D,E 허용
    assert forb[0] == ["E", "N"]       # 7/1
    assert forb[30] == ["N"]           # 7/31


def test_gap_falls_back_to_cache(db):
    # period 없음 + db 전달 → 캐시(N전담)로 폴백 (무회귀)
    n = _nurse(db, "gap1", ["N"])
    forb = _call([n], db=db)
    assert all(v == ["D", "E"] for v in forb["gap1"].values())
    assert len(forb["gap1"]) == 31


def test_empty_period_means_no_restriction(db):
    # 캐시=N전담이지만 period=[] (제한없음) → 금지 셀 없음
    n = _nurse(db, "free1", ["N"])
    db.add(NurseAllowedShiftPeriod(nurse_id="free1", valid_from=date(2026, 7, 1),
           valid_to=None, allowed_shifts=[]))
    db.flush()
    forb = _call([n], db=db)
    assert "free1" not in forb          # 제한 없음 → forbidden 미등록
