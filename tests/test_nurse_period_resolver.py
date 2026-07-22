# tests/test_nurse_period_resolver.py
"""P1 — 제너릭 시점 리졸버/upsert 단위 테스트.

검증: close-before-open(겹침금지) · gap=default · bulk fetch · 단방향 투영 · ward-aware.
"""
from __future__ import annotations

from datetime import date

import pytest

from db.models import (
    Nurse, NurseAllowedShiftPeriod, NurseWeekendOffPeriod, NurseGradePeriod,
)
from services.nurse_period_resolver import (
    fetch_periods, resolve_asof, upsert_period, open_span_covering,
)

MS, ME = date(2026, 7, 1), date(2026, 8, 1)   # 7월 [month_start, month_end)


# ── resolve_asof: 경계/ gap ────────────────────────────────────────────────
def test_resolve_asof_halfopen_and_gap():
    Row = NurseAllowedShiftPeriod
    rows = [
        Row(nurse_id="n1", valid_from=date(2026, 7, 1), valid_to=date(2026, 7, 22),
            allowed_shifts=["D"]),
        Row(nurse_id="n1", valid_from=date(2026, 7, 22), valid_to=None,
            allowed_shifts=["D", "E"]),
    ]
    assert resolve_asof(rows, date(2026, 7, 21), "allowed_shifts") == ["D"]
    assert resolve_asof(rows, date(2026, 7, 22), "allowed_shifts") == ["D", "E"]  # [from,to) 경계
    assert resolve_asof(rows, date(2026, 7, 31), "allowed_shifts") == ["D", "E"]  # 열린 구간
    # gap: 구간 시작 전
    assert resolve_asof(rows, date(2026, 6, 30), "allowed_shifts", default="GAP") == "GAP"
    assert resolve_asof([], date(2026, 7, 1), "allowed_shifts", default=None) is None


# ── upsert: close-before-open, 겹침 금지 ────────────────────────────────────
def test_upsert_close_before_open(db):
    upsert_period(db, NurseAllowedShiftPeriod, "n1", date(2026, 7, 1),
                  "allowed_shifts", ["D"])
    db.flush()
    # 7/22 부터 D,E 로 변경
    upsert_period(db, NurseAllowedShiftPeriod, "n1", date(2026, 7, 22),
                  "allowed_shifts", ["D", "E"])
    db.flush()

    rows = db.query(NurseAllowedShiftPeriod).filter_by(nurse_id="n1") \
             .order_by(NurseAllowedShiftPeriod.valid_from).all()
    assert len(rows) == 2
    assert rows[0].valid_from == date(2026, 7, 1) and rows[0].valid_to == date(2026, 7, 22)
    assert rows[1].valid_from == date(2026, 7, 22) and rows[1].valid_to is None
    # 겹침 없음: 첫 구간 close == 둘째 구간 open
    assert rows[0].valid_to == rows[1].valid_from
    # as-of 해석 일치
    by = fetch_periods(db, NurseAllowedShiftPeriod, ["n1"], MS, ME)
    assert resolve_asof(by["n1"], date(2026, 7, 10), "allowed_shifts") == ["D"]
    assert resolve_asof(by["n1"], date(2026, 7, 25), "allowed_shifts") == ["D", "E"]


def test_upsert_same_value_noop(db):
    upsert_period(db, NurseAllowedShiftPeriod, "n2", date(2026, 7, 1),
                  "allowed_shifts", ["N"])
    db.flush()
    upsert_period(db, NurseAllowedShiftPeriod, "n2", date(2026, 7, 15),
                  "allowed_shifts", ["N"])      # 동일값
    db.flush()
    rows = db.query(NurseAllowedShiftPeriod).filter_by(nurse_id="n2").all()
    assert len(rows) == 1                        # 새 구간 안 생김


def test_upsert_same_start_inplace(db):
    upsert_period(db, NurseAllowedShiftPeriod, "n3", date(2026, 7, 1),
                  "allowed_shifts", ["D"])
    db.flush()
    upsert_period(db, NurseAllowedShiftPeriod, "n3", date(2026, 7, 1),
                  "allowed_shifts", ["D", "E"])  # 같은 시작일 → 제자리 갱신
    db.flush()
    rows = db.query(NurseAllowedShiftPeriod).filter_by(nurse_id="n3").all()
    assert len(rows) == 1                        # empty span 안 생김
    assert rows[0].allowed_shifts == ["D", "E"]


# ── fetch_periods: bulk + 월 겹침 필터 ──────────────────────────────────────
def test_fetch_periods_bulk_and_month_filter(db):
    # n1: 7월 구간, n2: 6월에 끝난 구간(7월과 안 겹침), n3: 5월 시작 열린 구간(7월 덮음)
    upsert_period(db, NurseAllowedShiftPeriod, "a1", date(2026, 7, 5), "allowed_shifts", ["D"])
    db.add(NurseAllowedShiftPeriod(nurse_id="a2", valid_from=date(2026, 6, 1),
           valid_to=date(2026, 6, 20), allowed_shifts=["E"]))
    db.add(NurseAllowedShiftPeriod(nurse_id="a3", valid_from=date(2026, 5, 1),
           valid_to=None, allowed_shifts=["N"]))
    db.flush()

    by = fetch_periods(db, NurseAllowedShiftPeriod, ["a1", "a2", "a3"], MS, ME)
    assert "a1" in by                            # 7월 구간 포함
    assert "a2" not in by                         # 6월에 끝남 → 제외
    assert resolve_asof(by["a3"], date(2026, 7, 10), "allowed_shifts") == ["N"]  # 열린구간 덮음


def test_fetch_periods_empty_ids(db):
    assert fetch_periods(db, NurseAllowedShiftPeriod, [], MS, ME) == {}


# ── 단방향 캐시 투영 ────────────────────────────────────────────────────────
def test_projection_past_effective_updates_cache(db):
    nurse = Nurse(nurse_id="p1", account_id="acc_p1", name="투영", group_id=None,
                  )
    db.add(nurse); db.flush()
    # valid_from <= today → 컬럼 투영됨
    upsert_period(db, NurseWeekendOffPeriod, "p1", date(2026, 1, 1),
                  "weekend_off", 1, nurse=nurse, cache_attr="is_weekend_off",
                  today=date(2026, 7, 1))
    assert nurse.is_weekend_off == 1


def test_projection_future_effective_skips_cache(db):
    nurse = Nurse(nurse_id="p2", account_id="acc_p2", name="미래", group_id=None,
                  )
    db.add(nurse); db.flush()
    # valid_from > today → as-of 발효 전엔 미반영(주말휴무 컬럼 언매핑 → period 로 검증)
    upsert_period(db, NurseWeekendOffPeriod, "p2", date(2026, 9, 1),
                  "weekend_off", 1, today=date(2026, 7, 1))
    from services.nurse_period_resolver import is_weekend_off_asof
    assert is_weekend_off_asof(db, "p2", date(2026, 7, 1)) is False   # 발효 전
    assert is_weekend_off_asof(db, "p2", date(2026, 9, 1)) is True    # 발효 후


# ── ward-aware (group_id 있는 grade) ────────────────────────────────────────
def test_ward_aware_grade_filter(db):
    # 같은 간호사가 두 병동에서 다른 grade 구간
    db.add(NurseGradePeriod(nurse_id="g1", group_id="A", valid_from=date(2026, 7, 1),
           valid_to=None, grade=1))
    db.add(NurseGradePeriod(nurse_id="g1", group_id="B", valid_from=date(2026, 7, 1),
           valid_to=None, grade=3))
    db.flush()

    by_a = fetch_periods(db, NurseGradePeriod, ["g1"], MS, ME, group_id="A")
    by_b = fetch_periods(db, NurseGradePeriod, ["g1"], MS, ME, group_id="B")
    assert resolve_asof(by_a["g1"], date(2026, 7, 10), "grade") == 1
    assert resolve_asof(by_b["g1"], date(2026, 7, 10), "grade") == 3


def test_open_span_covering(db):
    upsert_period(db, NurseAllowedShiftPeriod, "o1", date(2026, 7, 1), "allowed_shifts", ["D"])
    db.flush()
    cur = open_span_covering(db, NurseAllowedShiftPeriod, "o1", date(2026, 7, 15))
    assert cur is not None and cur.allowed_shifts == ["D"]
    # 구간 시작 전엔 None
    assert open_span_covering(db, NurseAllowedShiftPeriod, "o1", date(2026, 6, 1)) is None
