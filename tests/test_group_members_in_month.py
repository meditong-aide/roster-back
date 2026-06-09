"""근무자관리 월 셀렉터 — 소속 기준 명단 + 상태 플래그 (Phase 4 P4-1).

group_members_in_month: 그 달 그 병동 '소속'을 근무일 수가 아니라 소속으로 판단.
- 영구 전출(병동이동, start<=월초) → 제외
- 전출 transition(병동이동, 월중 start) → outbound '←'
- 파견 나감 → dispatch_out '파견 중'
- 휴직 → leave '휴직'
- 인바운드(target==group) → inbound '→'
참조: docs/TEMPORAL_NURSE_MODEL_DESIGN.md §3, app/services/assignment_service.py
"""

from __future__ import annotations

from datetime import date

import pytest

from db.models import Group, Nurse, Office, NurseAssignment
from services.assignment_service import group_members_in_month


def _nurse(db, nid, gid, team=1, grade=1):
    db.add(Nurse(nurse_id=nid, account_id=f"acc_{nid}", group_id=gid, office_id="o1",
                 name=nid, active=1, team_id=team, grade=grade, is_night_nurse=[]))


def _asg(db, nid, reason, src, tgt, start):
    db.add(NurseAssignment(nurse_id=nid, source_group_id=src, target_group_id=tgt,
                           office_id="o1", start_date=start, reason=reason, status="active"))


@pytest.fixture
def seeded(db):
    db.add(Office(office_id="o1", office_name="병원"))
    db.add(Group(group_id="A", group_name="A병동", office_id="o1"))
    db.add(Group(group_id="B", group_name="B병동", office_id="o1"))
    # 홈 A
    _nurse(db, "active1", "A")
    _nurse(db, "moved_full", "A")   # 병동이동 7/1 → 제외
    _nurse(db, "moved_mid", "A")    # 병동이동 7/15 → outbound
    _nurse(db, "dispatch1", "A")    # 파견 나감 → dispatch_out
    _nurse(db, "leave1", "A")       # 휴직
    # 홈 B (인바운드 후보)
    _nurse(db, "inbound1", "B", team=2, grade=0)
    # assignments (2026-07)
    _asg(db, "moved_full", "병동이동", "A", "B", date(2026, 7, 1))
    _asg(db, "moved_mid", "병동이동", "A", "B", date(2026, 7, 15))
    _asg(db, "dispatch1", "파견", "A", "B", date(2026, 7, 1))
    _asg(db, "leave1", "휴직", "A", "A", date(2026, 7, 1))
    _asg(db, "inbound1", "병동이동", "B", "A", date(2026, 7, 1))
    db.flush()
    return db


def test_membership_status_and_exclusion(seeded):
    db = seeded
    out = group_members_in_month(db, "A", 2026, 7)
    by_id = {m["nurse_id"]: m for m in out["members"]}

    # 완전 전출은 그 달 미표시
    assert "moved_full" not in by_id
    # 상태/마커
    assert by_id["active1"]["membership_status"] == "active"
    assert by_id["moved_mid"]["membership_status"] == "outbound" and by_id["moved_mid"]["marker"] == "←"
    assert by_id["dispatch1"]["membership_status"] == "dispatch_out" and by_id["dispatch1"]["badge"] == "파견 중"
    assert by_id["leave1"]["membership_status"] == "leave" and by_id["leave1"]["badge"] == "휴직"
    # 인바운드(홈 B → A)
    assert by_id["inbound1"]["membership_status"] == "inbound" and by_id["inbound1"]["marker"] == "→"


def test_headcount_split(seeded):
    db = seeded
    hc = group_members_in_month(db, "A", 2026, 7)["headcount"]
    assert hc == {"regular": 1, "moving": 3, "leave": 1}  # active1 / (moved_mid+dispatch1+inbound1) / leave1


def test_no_assignments_all_active(seeded):
    """assignment 발효 전 달(6월: start 7월 > 6월말)은 홈 전원 active(이동/휴직 0)."""
    db = seeded
    out = group_members_in_month(db, "A", 2026, 6)  # 모든 assignment start=7월 → 6월엔 비활성
    statuses = {m["nurse_id"]: m["membership_status"] for m in out["members"]}
    # 홈 A 5명 전원 active, 인바운드 없음
    assert statuses == {"active1": "active", "moved_full": "active", "moved_mid": "active",
                        "dispatch1": "active", "leave1": "active"}
