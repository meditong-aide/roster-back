"""사이드프로필(PATCH /nurses/{id} = update_nurse_profile_service) 선택월 발효 + weekend period.

버그(회귀): update_nurse_profile_service 의 저장 분기는 호출자 역할에 따라 3개로 갈린다.
  - admin   : master_admin 이 수정              → nurses.* 직접
  - source  : 수간호사가 본인 병동 간호사 수정     → nurses.* (원장)   ← 제일 흔한 경로
  - target  : 그 간호사가 내 병동으로 파견/병동이동 → assignment.target_*
세 분기 모두 grade/weekend/fixed 를 _persist_profile_period_change(valid_from=선택월) 로 보내야 하는데,
source 분기만 valid_from 을 빼먹어 당월(예: 6월)로 폴백 → 8월 OFF 가 6월 row 를 제자리 갱신,
6/7월 소실. 기존 테스트가 admin 만 써서 이 버그를 못 잡았다(거짓 그린).

→ 이 테스트는 **세 역할 전부 parametrize** 하여 모든 period-쓰기 분기를 커버한다.
참조: app/services/nurse_service.py::update_nurse_profile_service / _persist_profile_period_change
"""
from __future__ import annotations

from datetime import date, timedelta

import pytest

from db.models import (
    Office, Group, Nurse, NurseAssignment,
    NurseWeekendOffPeriod, NurseGradePeriod,
)
from schemas.roster_schema import NurseProfileUpdate
from schemas.auth_schema import User as UserSchema
from services.nurse_service import update_nurse_profile_service
from services.nurse_period_resolver import fetch_periods, resolve_asof


def _user(**over):
    base = dict(
        nurse_id="ADM", account_id="acc_ADM", office_id="o1", group_id="A",
        is_head_nurse=False, is_master_admin=False, name="u", EmpSeqNo="",
        EmpAuthGbn="", mb_part="", office_name="병원", mb_part_name="",
        official_title_name=None, is_nurse_registered=True, hn_auth=None,
        original_group_id="A", gw_useYN="Y", qpis_useYN="Y",
    )
    base.update(over)
    return UserSchema(**base)


@pytest.fixture
def seeded(db):
    db.add(Office(office_id="o1", office_name="병원"))
    db.add(Group(group_id="A", group_name="A병동", office_id="o1"))
    db.add(Group(group_id="B", group_name="B병동", office_id="o1"))
    # 수정 대상: 홈=A
    db.add(Nurse(nurse_id="n1", account_id="acc_n1", group_id="A", office_id="o1", name="n1",
                 active=1, allowed_shifts=[], grade=1))
    # A 병동 수간호사(source 호출자)
    db.add(Nurse(nurse_id="HN_A", account_id="acc_HN_A", group_id="A", office_id="o1",
                 name="수간호사A", active=1, is_head_nurse=1, hn_auth="HN",
                 allowed_shifts=[], grade=1))
    # B 병동 수간호사(target 호출자) + n1 을 B 로 파견
    db.add(Nurse(nurse_id="HN_B", account_id="acc_HN_B", group_id="B", office_id="o1",
                 name="수간호사B", active=1, is_head_nurse=1, hn_auth="HN",
                 allowed_shifts=[], grade=1))
    db.add(NurseAssignment(
        nurse_id="n1", source_group_id="A", target_group_id="B", office_id="o1",
        start_date=date(2026, 1, 1), reason="파견", status="active",
    ))
    db.flush()
    return db


def _wk(db, day):
    rows = fetch_periods(db, NurseWeekendOffPeriod, ["n1"], day, day + timedelta(days=1))
    return resolve_asof(rows.get("n1"), day, "weekend_off")


# (라벨, 호출자) — 세 분기 전부
_ROLES = [
    ("admin", _user(nurse_id="ADM", account_id="acc_ADM", is_master_admin=True)),
    ("source_HN", _user(nurse_id="HN_A", account_id="acc_HN_A", group_id="A",
                        is_head_nurse=True, hn_auth="HN")),
    ("target_HN", _user(nurse_id="HN_B", account_id="acc_HN_B", group_id="B",
                        is_head_nurse=True, hn_auth="HN", original_group_id="B")),
]


@pytest.mark.parametrize("label,caller", _ROLES, ids=[r[0] for r in _ROLES])
def test_weekend_selected_month_close_before_open_all_roles(seeded, label, caller):
    """★회귀: 어느 역할(admin/source/target)로 저장해도 6월 ON → 8월 OFF 시 6/7월 유지."""
    db = seeded
    update_nurse_profile_service("n1", NurseProfileUpdate(is_weekend_off=True), caller, db,
                                 effective_year=2026, effective_month=6)
    assert _wk(db, date(2026, 6, 15)) == 1, f"[{label}] 6월 ON 실패"

    update_nurse_profile_service("n1", NurseProfileUpdate(is_weekend_off=False), caller, db,
                                 effective_year=2026, effective_month=8)
    assert _wk(db, date(2026, 6, 15)) == 1, f"[{label}] 6월 소실(버그 재발)"
    assert _wk(db, date(2026, 7, 15)) == 1, f"[{label}] 7월 소실(버그 재발)"
    assert _wk(db, date(2026, 8, 15)) == 0, f"[{label}] 8월 OFF 미반영"


def _gr(db, day, group_id):
    rows = fetch_periods(db, NurseGradePeriod, ["n1"], day, day + timedelta(days=1), group_id=group_id)
    return resolve_asof(rows.get("n1"), day, "grade")


def test_sideprofile_grade_selected_month_source(seeded):
    """grade 도 source(수간호사) 경로에서 선택월 발효 — 홈 group(A) period 에 기록."""
    db = seeded
    u = _user(nurse_id="HN_A", account_id="acc_HN_A", group_id="A",
              is_head_nurse=True, hn_auth="HN")
    update_nurse_profile_service("n1", NurseProfileUpdate(grade=2), u, db,
                                 effective_year=2026, effective_month=6)
    update_nurse_profile_service("n1", NurseProfileUpdate(grade=3), u, db,
                                 effective_year=2026, effective_month=8)
    assert _gr(db, date(2026, 7, 15), "A") == 2   # 7월 유지
    assert _gr(db, date(2026, 8, 15), "A") == 3   # 8월부터 3


def _partner(db, nid, day):
    from services.mutual_exclusion_period import resolve_partner_asof
    return resolve_partner_asof(db, [nid], day).get(nid)


@pytest.mark.parametrize("label,caller", _ROLES, ids=[r[0] for r in _ROLES])
def test_mutual_exclusion_set_and_release_all_roles(seeded, label, caller):
    """★회귀: 어느 역할(admin/source/target)로 저장해도 상호배제 설정·해제가 선택월 1일 발효로 반영.

    특히 target(인바운드) 편집에서 해제(null) payload 가 유실되면 안 된다 — 기존엔
    set_mutual_exclusion 이 `applied_source` 게이트에 묶여 target 모드에서 스킵됐다.
    """
    db = seeded
    # 배제 파트너 n2 (홈 A)
    db.add(Nurse(nurse_id="n2", account_id="acc_n2", group_id="A", office_id="o1",
                 name="이영희", active=1, allowed_shifts=[], grade=1))
    db.flush()

    # 6월 1일 발효로 설정
    update_nurse_profile_service("n1", NurseProfileUpdate(exclusion_partner_id="n2"), caller, db,
                                 effective_year=2026, effective_month=6)
    assert _partner(db, "n1", date(2026, 6, 1)) == "n2", f"[{label}] 설정 미반영"
    assert _partner(db, "n2", date(2026, 6, 1)) == "n1", f"[{label}] 양방향 미반영"
    assert _partner(db, "n1", date(2026, 5, 31)) is None, f"[{label}] valid_from 이 선택월 1일 아님"

    # 8월 1일 발효로 해제(null) — target 모드에서도 반드시 실행돼야 함
    update_nurse_profile_service("n1", NurseProfileUpdate(exclusion_partner_id=None), caller, db,
                                 effective_year=2026, effective_month=8)
    assert _partner(db, "n1", date(2026, 8, 1)) is None, f"[{label}] 해제 미반영(target 유실 회귀)"
    assert _partner(db, "n2", date(2026, 8, 1)) is None, f"[{label}] 양방향 해제 미반영"
    assert _partner(db, "n1", date(2026, 7, 15)) == "n2", f"[{label}] 과거 배제 구간 소실"


def test_mutual_exclusion_field_omitted_no_change(seeded):
    """★필드 생략(≠null) PATCH 는 기존 상호배제 관계를 변경하지 않는다."""
    db = seeded
    u = _user(nurse_id="HN_A", account_id="acc_HN_A", group_id="A",
              is_head_nurse=True, hn_auth="HN")
    db.add(Nurse(nurse_id="n2", account_id="acc_n2", group_id="A", office_id="o1",
                 name="이영희", active=1, allowed_shifts=[], grade=1))
    db.flush()
    update_nurse_profile_service("n1", NurseProfileUpdate(exclusion_partner_id="n2"), u, db,
                                 effective_year=2026, effective_month=6)
    # exclusion_partner_id 를 생략한 다른 필드만 수정 → 관계 유지
    update_nurse_profile_service("n1", NurseProfileUpdate(grade=3), u, db,
                                 effective_year=2026, effective_month=6)
    assert _partner(db, "n1", date(2026, 6, 1)) == "n2"  # 생략 → 무변경
