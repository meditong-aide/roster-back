"""assignment-exclusion: grade/allowed/fixed 는 assignment.target_* 가 아니라 period(SSOT).

사용자 시나리오: "병동이동/속성 조정 후 grade 수정은 period 에서" — assignment.target_* 미사용.
- 영구 속성변경(create_permanent_change) → nurse_grade_period / nurse_allowed_shift_period 기록
- 발효(flush_pending_permanent_changes) → period→캐시 투영 (target_* 안 읽음)
- inbound 표시(group_members_in_month) → period as-of (base 캐시/target_* 아님)
참조: app/services/assignment_service.py, app/services/nurse_service.py
"""
from __future__ import annotations

from datetime import date, timedelta

import pytest

from db.models import (
    Office, Group, Nurse, NurseAssignment, NurseGradePeriod, NurseAllowedShiftPeriod,
)
from services.assignment_service import (
    create_permanent_change, flush_pending_permanent_changes, group_members_in_month,
)
from services.nurse_period_resolver import fetch_periods, resolve_asof, upsert_period


def _grade_asof(db, nid, group, day):
    rows = fetch_periods(db, NurseGradePeriod, [nid], day, day + timedelta(days=1), group_id=group)
    return resolve_asof(rows.get(nid), day, "grade", default=None)


def _allowed_asof(db, nid, day):
    rows = fetch_periods(db, NurseAllowedShiftPeriod, [nid], day, day + timedelta(days=1))
    return resolve_asof(rows.get(nid), day, "allowed_shifts", default=None)


@pytest.fixture
def seeded(db):
    db.add(Office(office_id="o1", office_name="병원"))
    db.add(Group(group_id="A", group_name="A병동", office_id="o1"))
    db.add(Nurse(nurse_id="n1", account_id="acc_n1", group_id="A", office_id="o1",
                 name="n1", active=1, grade=1, allowed_shifts=[]))
    db.flush()
    return db


def test_permanent_change_writes_grade_to_period(seeded):
    """속성변경이 grade/allowed 를 period(SSOT)에 기록. (assignment.target_* 가 source 아님)"""
    db = seeded
    start = date(2026, 8, 1)
    create_permanent_change(
        db, nurse_id="n1", group_id="A", office_id="o1", start_date=start,
        new_grade=3, new_shift_types=["N"],
    )
    assert _grade_asof(db, "n1", "A", start) == 3
    assert _allowed_asof(db, "n1", start) == ["N"]
    # 발효 전 구간(7/31)은 미적용 — close-before-open
    assert _grade_asof(db, "n1", "A", date(2026, 7, 31)) is None


def test_flush_projects_period_to_cache(seeded):
    """발효 flush 가 period→nurse 캐시로 투영(target_* 미사용). gap 이면 현재값 유지."""
    db = seeded
    start = date(2026, 1, 1)
    create_permanent_change(
        db, nurse_id="n1", group_id="A", office_id="o1", start_date=start,
        new_grade=2, new_shift_types=["D", "E"],
    )
    n = db.query(Nurse).filter_by(nurse_id="n1").first()
    flush_pending_permanent_changes(db, as_of=date(2026, 2, 1))
    db.refresh(n)
    assert n.grade == 2              # period 값 투영
    assert n.allowed_shifts == ["D", "E"]


def test_inbound_grade_reads_period_over_base(db):
    """병동이동 inbound 표시는 target group period grade — base 캐시/target_* 아님."""
    db.add(Office(office_id="o1", office_name="병원"))
    db.add(Group(group_id="A", group_name="A병동", office_id="o1"))
    db.add(Group(group_id="B", group_name="B병동", office_id="o1"))
    db.add(Nurse(nurse_id="mv", account_id="acc_mv", group_id="B", office_id="o1",
                 name="mover", active=1, grade=1, allowed_shifts=[]))
    db.add(NurseAssignment(nurse_id="mv", source_group_id="B", target_group_id="A",
                           office_id="o1", start_date=date(2026, 7, 1),
                           reason="병동이동", status="active"))
    db.flush()
    # target group A 의 grade period(이동 시 새 등급 3) — base nurses.grade=1 과 다름
    upsert_period(db, NurseGradePeriod, "mv", date(2026, 7, 1), "grade", 3, group_id="A",
                  source="transfer")
    db.flush()
    members = {m["nurse_id"]: m for m in group_members_in_month(db, "A", 2026, 7)["members"]}
    assert members["mv"]["membership_status"] == "inbound"
    assert members["mv"]["as_of_grade"] == 3      # period(A) 값 (base 1 아님)


def test_dispatch_inbound_keeps_target_overlay(db):
    """파견(임시) inbound 은 period 아님 — dispatch target_* overlay 유지."""
    db.add(Office(office_id="o1", office_name="병원"))
    db.add(Group(group_id="A", group_name="A병동", office_id="o1"))
    db.add(Group(group_id="B", group_name="B병동", office_id="o1"))
    db.add(Nurse(nurse_id="dp", account_id="acc_dp", group_id="B", office_id="o1",
                 name="disp", active=1, grade=2, allowed_shifts=[]))
    db.add(NurseAssignment(nurse_id="dp", source_group_id="B", target_group_id="A",
                           office_id="o1", start_date=date(2026, 7, 1),
                           reason="파견", status="active", target_grade=4))
    db.flush()
    members = {m["nurse_id"]: m for m in group_members_in_month(db, "A", 2026, 7)["members"]}
    assert members["dp"]["membership_status"] == "inbound"
    assert members["dp"]["badge"] == "파견"
    assert members["dp"]["as_of_grade"] == 4      # 파견 overlay(target_grade), period 미관여
