"""upsert_period 중간시점 편집 — 분할 시 뒤 변경점 보존(겹침 금지).

버그: 구간 분할 때 새 구간을 valid_to=None(열린 구간)으로 삽입 → 뒤에 이미 있던
변경점(예: 8/1)을 덮어 겹침이 생김. 이후 그 뒤 구간을 다시 편집해도 앞 구간이
계속 이겨서 표시가 안 됨.

시나리오(사용자 보고): 6월 true → 8월 false → 7월 false → 8월 true 했더니
7월 false 가 8월까지 계속 적용되어 8월 true 가 안 보임.
"""
from __future__ import annotations

from datetime import date, timedelta

import pytest

from db.models import Office, Group, Nurse, NurseWeekendOffPeriod
from services.nurse_period_resolver import upsert_period, fetch_periods, resolve_asof


@pytest.fixture
def seeded(db):
    db.add(Office(office_id="o1", office_name="병원"))
    db.add(Group(group_id="A", group_name="A병동", office_id="o1"))
    db.add(Nurse(nurse_id="n1", account_id="acc_n1", group_id="A", office_id="o1", name="n1",
                 active=1, is_weekend_off=False, allowed_shifts=[], grade=1))
    db.flush()
    return db


def _set(db, vf: date, val: int):
    # today 를 과거로 둬서 캐시 투영 분기와 무관하게 period 만 검증
    upsert_period(db, NurseWeekendOffPeriod, "n1", vf, "weekend_off", val,
                  source="test", today=date(2026, 1, 1))
    db.flush()


def _wk(db, day: date):
    rows = fetch_periods(db, NurseWeekendOffPeriod, ["n1"], day, day + timedelta(days=1))
    return resolve_asof(rows.get("n1"), day, "weekend_off")


def _spans(db):
    rows = (db.query(NurseWeekendOffPeriod)
            .filter(NurseWeekendOffPeriod.nurse_id == "n1")
            .order_by(NurseWeekendOffPeriod.valid_from.asc()).all())
    return [(r.valid_from, r.valid_to, r.weekend_off) for r in rows]


def test_midmonth_split_preserves_later_changepoint(seeded):
    """6월T → 8월F → 7월F → 8월T: 각 단계 타임라인이 겹침 없이 정확."""
    db = seeded
    _set(db, date(2026, 6, 1), 1)          # 6월 true
    _set(db, date(2026, 8, 1), 0)          # 8월 false
    assert _spans(db) == [
        (date(2026, 6, 1), date(2026, 8, 1), 1),
        (date(2026, 8, 1), None, 0),
    ]

    _set(db, date(2026, 7, 1), 0)          # 7월 false — [6,8)=1 을 분할
    # ★ 새 [7/1,?) 가 None 이 아니라 8/1 까지여야 8월 변경점 보존
    assert _spans(db) == [
        (date(2026, 6, 1), date(2026, 7, 1), 1),
        (date(2026, 7, 1), date(2026, 8, 1), 0),
        (date(2026, 8, 1), None, 0),
    ]

    _set(db, date(2026, 8, 1), 1)          # 8월 true — 제자리 갱신
    assert _wk(db, date(2026, 6, 15)) == 1
    assert _wk(db, date(2026, 7, 15)) == 0
    assert _wk(db, date(2026, 8, 15)) == 1   # ★ 버그면 7월 false 가 이겨서 0
    # 열린 구간(valid_to=None)은 정확히 1개여야 한다(겹침 금지 불변식)
    assert sum(1 for _, vt, _v in _spans(db) if vt is None) == 1


def test_gap_edit_before_all_clips_to_next(seeded):
    """앞쪽 gap 편집: 5월 값을 넣어도 기존 6월 구간을 삼키지 않는다."""
    db = seeded
    _set(db, date(2026, 6, 1), 1)          # [6/1, null)=1
    _set(db, date(2026, 5, 1), 0)          # gap → [5/1, 6/1)=0 로 잘려야
    assert _spans(db) == [
        (date(2026, 5, 1), date(2026, 6, 1), 0),
        (date(2026, 6, 1), None, 1),
    ]
    assert _wk(db, date(2026, 5, 15)) == 0
    assert _wk(db, date(2026, 6, 15)) == 1
