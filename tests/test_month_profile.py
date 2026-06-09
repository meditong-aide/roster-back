"""월 프로필 시점 해석 (Phase 1: team).

nurse_month_profile 가 진실, nurses 는 현재값 캐시. 그 달 행이 있으면 그 값,
없으면 nurses(현재값) 폴백. upsert 의 if_absent_only(freeze 멱등)·부분갱신 검증.
참조: app/services/month_profile.py, docs/TEMPORAL_NURSE_MODEL_DESIGN.md.
"""

from __future__ import annotations

import pytest

from db.models import Group, Nurse, Office
from services.month_profile import resolve_team_as_of, upsert_month_profile, get_month_profile


@pytest.fixture
def seeded(db):
    db.add(Office(office_id="o1", office_name="병원"))
    db.add(Group(group_id="A", group_name="A병동", office_id="o1"))
    db.add(Group(group_id="B", group_name="B병동", office_id="o1"))
    # nurses 현재값(캐시): team_id=1
    db.add(Nurse(nurse_id="n1", account_id="acc_n1", group_id="A", office_id="o1",
                 name="간호1", active=1, team_id=1, is_night_nurse=[]))
    db.flush()
    return db


def test_no_profile_falls_back_to_nurses_current(seeded):
    """프로필 행 없으면 nurses(현재값=1) 으로 폴백."""
    db = seeded
    assert resolve_team_as_of(db, "n1", 2026, 7) == 1


def test_profile_overrides_nurses(seeded):
    """그 달 프로필이 있으면 nurses 가 아니라 프로필 값(2)."""
    db = seeded
    upsert_month_profile(db, nurse_id="n1", year=2026, month=7, group_id="B",
                         team_id=2, source="redistribute")
    assert resolve_team_as_of(db, "n1", 2026, 7) == 2
    # 다른 달(8월)은 행이 없으니 여전히 현재값 폴백
    assert resolve_team_as_of(db, "n1", 2026, 8) == 1


def test_profile_is_per_month(seeded):
    """6월=1(폴백), 7월=2(override) — 같은 간호사도 달마다 다름."""
    db = seeded
    upsert_month_profile(db, nurse_id="n1", year=2026, month=7, group_id="B", team_id=2)
    assert resolve_team_as_of(db, "n1", 2026, 6) == 1
    assert resolve_team_as_of(db, "n1", 2026, 7) == 2


def test_freeze_is_idempotent(seeded):
    """if_absent_only=True 면 이미 동결된 행을 덮어쓰지 않는다(freeze 멱등)."""
    db = seeded
    upsert_month_profile(db, nurse_id="n1", year=2026, month=7, group_id="A",
                         team_id=1, source="frozen")
    # 이후 현재값이 바뀌어도 frozen 행 보존
    upsert_month_profile(db, nurse_id="n1", year=2026, month=7, group_id="A",
                         team_id=9, source="frozen", if_absent_only=True)
    prof = get_month_profile(db, "n1", 2026, 7)
    assert prof.team_id == 1  # 덮어써지지 않음
    assert resolve_team_as_of(db, "n1", 2026, 7) == 1


def test_upsert_partial_update_keeps_other_columns(seeded):
    """값 인자가 None 이면 해당 컬럼 미변경(부분 갱신)."""
    db = seeded
    upsert_month_profile(db, nurse_id="n1", year=2026, month=7, group_id="B",
                         team_id=2, grade=3)
    # grade 는 안 주고 team_id 만 갱신
    upsert_month_profile(db, nurse_id="n1", year=2026, month=7, group_id="B",
                         team_id=5)
    prof = get_month_profile(db, "n1", 2026, 7)
    assert prof.team_id == 5
    assert prof.grade == 3  # 유지


def test_fallback_team_id_shortcut(seeded):
    """fallback_team_id 주면 nurses 재조회 없이 그 값 폴백(프로필 없을 때)."""
    db = seeded
    assert resolve_team_as_of(db, "n1", 2026, 7, fallback_team_id=7) == 7
