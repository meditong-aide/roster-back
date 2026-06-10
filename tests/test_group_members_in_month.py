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


def test_transition_marker_only_in_move_month(seeded):
    """병동이동 전입(→)은 이동月에만. 다음 달엔 정착(active, 마커 없음) — 명단엔 유지."""
    db = seeded
    jul = {m["nurse_id"]: m for m in group_members_in_month(db, "A", 2026, 7)["members"]}
    aug = {m["nurse_id"]: m for m in group_members_in_month(db, "A", 2026, 8)["members"]}
    # 7월(이동月): 전입 →
    assert jul["inbound1"]["membership_status"] == "inbound"
    assert jul["inbound1"]["marker"] == "→"
    # 8월(정착): active, 마커 없음 — 그래도 명단엔 존재
    assert "inbound1" in aug
    assert aug["inbound1"]["membership_status"] == "active"
    assert aug["inbound1"]["marker"] is None


def test_dispatch_out_badge_persists_across_months(seeded):
    """파견 나감(지속 상태)은 이동月 다음 달에도 '파견 중' 배지 유지(전이 아님)."""
    db = seeded
    for mon in (7, 8):
        by_id = {m["nurse_id"]: m for m in group_members_in_month(db, "A", 2026, mon)["members"]}
        assert by_id["dispatch1"]["membership_status"] == "dispatch_out"
        assert by_id["dispatch1"]["badge"] == "파견 중"


def test_night_dedicated_is_unassigned(db):
    """N전담(is_night_nurse=['N'])은 as_of_team=None + 'N전담' 배지 + is_night_dedicated=True."""
    db.add(Office(office_id="o1", office_name="병원"))
    db.add(Group(group_id="A", group_name="A병동", office_id="o1"))
    db.add(Nurse(nurse_id="reg", account_id="acc_reg", group_id="A", office_id="o1",
                 name="일반", active=1, team_id=1, grade=1, is_night_nurse=[]))
    db.add(Nurse(nurse_id="night", account_id="acc_night", group_id="A", office_id="o1",
                 name="야간", active=1, team_id=2, grade=2, is_night_nurse=["N"]))
    db.flush()
    by_id = {m["nurse_id"]: m for m in group_members_in_month(db, "A", 2026, 7)["members"]}
    assert by_id["night"]["is_night_dedicated"] is True
    assert by_id["night"]["as_of_team"] is None
    assert by_id["night"]["badge"] == "N전담"
    assert by_id["reg"]["is_night_dedicated"] is False
    assert by_id["reg"]["as_of_team"] == 1


def test_night_dedicated_robust_string_form(db):
    """is_night_nurse 가 문자열 '[\"N\"]'(MSSQL JSON-as-string)이어도 N전담 인식."""
    db.add(Office(office_id="o1", office_name="병원"))
    db.add(Group(group_id="A", group_name="A병동", office_id="o1"))
    db.add(Nurse(nurse_id="night2", account_id="acc_n2", group_id="A", office_id="o1",
                 name="야간2", active=1, team_id=1, grade=2, is_night_nurse='["N"]'))
    db.flush()
    by_id = {m["nurse_id"]: m for m in group_members_in_month(db, "A", 2026, 7)["members"]}
    assert by_id["night2"]["is_night_dedicated"] is True
    assert by_id["night2"]["as_of_team"] is None


def test_as_of_team_uses_period_over_cache(db):
    """[Perf 배치 회귀] nurse_team_period 구간이 있으면 as_of_team 은 캐시(nurses.team_id)가
    아니라 구간 팀. 배치 로드(_resolved_team)가 resolve_team(구간 우선·시점)과 동일함을 고정."""
    from services.team_period import set_team_period

    db.add(Office(office_id="o1", office_name="병원"))
    db.add(Group(group_id="A", group_name="A병동", office_id="o1"))
    _nurse(db, "n_period", "A", team=1)   # 캐시 team=1
    _nurse(db, "n_cache", "A", team=3)    # 구간 없음 → 캐시 폴백
    db.flush()
    set_team_period(db, nurse_id="n_period", group_id="A",
                    valid_from=date(2026, 7, 1), team_id=2)
    jul = {m["nurse_id"]: m for m in group_members_in_month(db, "A", 2026, 7)["members"]}
    assert jul["n_period"]["as_of_team"] == 2   # 구간 우선(캐시 1 아님)
    assert jul["n_cache"]["as_of_team"] == 3     # 구간 없음 → 캐시 폴백
    # 7/1 구간은 6월엔 미적용 → 폴백(캐시 1)
    jun = {m["nurse_id"]: m for m in group_members_in_month(db, "A", 2026, 6)["members"]}
    assert jun["n_period"]["as_of_team"] == 1


def test_dispatch_inbound_badge_persists(seeded):
    """파견 인바운드(target측)도 지속 → 다음 달에도 '파견' 배지, 마커 없음."""
    db = seeded
    _nurse(db, "dispatch_in", "B")
    _asg(db, "dispatch_in", "파견", "B", "A", date(2026, 7, 1))
    db.flush()
    for mon in (7, 8):
        by_id = {m["nurse_id"]: m for m in group_members_in_month(db, "A", 2026, mon)["members"]}
        assert by_id["dispatch_in"]["membership_status"] == "inbound"
        assert by_id["dispatch_in"]["badge"] == "파견"
        assert by_id["dispatch_in"]["marker"] is None
