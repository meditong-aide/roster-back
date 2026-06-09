"""전출(병동이동) 완료 간호사의 과거 병동(A) read-only 가시성.

전출이 발효되면 nurses.group_id 가 target(B)으로 바뀌어 A 의 명단 쿼리(group_id==A,
inbound target==A)에 안 걸린다. source==A 인 병동이동 행으로 역으로 잡아 A 명단에
'전출함'으로 노출하고, inbound 블록에 completed 병동이동을 실어 프론트가 렌더하도록 한다.
참조: docs/NURSE_GROUP_CHANGE_MODEL.md (근무자 관리 UX), 기존 inbound 메커니즘 재사용.
"""

from __future__ import annotations

from datetime import date

from db.models import Group, Nurse, NurseAssignment, Office
from schemas.auth_schema import User as UserSchema
from services.nurse_service import get_nurses_in_group_service


def _user(group_id: str, office_id: str) -> UserSchema:
    return UserSchema(
        nurse_id="HN_A", account_id="acc_HN_A", office_id=office_id, group_id=group_id,
        is_head_nurse=True, is_master_admin=False, name="수간호사", EmpSeqNo="",
        EmpAuthGbn="", mb_part="", office_name="테스트병원", mb_part_name="",
        official_title_name=None, is_nurse_registered=True, hn_auth="HN",
        original_group_id=group_id, gw_useYN="Y", qpis_useYN="Y",
    )


def _mk_nurse(db, nurse_id, group_id, office_id, name):
    db.add(Nurse(
        nurse_id=nurse_id, account_id=f"acc_{nurse_id}", group_id=group_id,
        office_id=office_id, name=name, active=1,
    ))


def _seed(db):
    db.add(Office(office_id="o1", office_name="병원"))
    db.add(Group(group_id="A", group_name="A병동", office_id="o1"))
    db.add(Group(group_id="B", group_name="B병동", office_id="o1"))
    _mk_nurse(db, "stay", "A", "o1", "잔류간호")        # A 현직
    _mk_nurse(db, "moved", "B", "o1", "김간호")          # A→B 전출됨 (group_id 이미 B)
    _mk_nurse(db, "bonly", "B", "o1", "비관련")          # B 전용, A와 무관
    # 전출 완료 행: source=A, target=B, completed
    db.add(NurseAssignment(
        nurse_id="moved", source_group_id="A", target_group_id="B", office_id="o1",
        start_date=date(2026, 5, 1), end_date=date(2026, 5, 1),
        reason="병동이동", kind="transfer", status="completed",
    ))
    db.flush()


def test_outbound_transfer_nurse_visible_in_source_list(db):
    _seed(db)
    rows = get_nurses_in_group_service(_user("A", "o1"), db)
    ids = {r["nurse_id"] for r in rows}
    # A 현직 + 전출자 포함, B 전용은 제외
    assert "stay" in ids
    assert "moved" in ids, "전출 완료 간호사가 과거 병동 A 명단에 보여야 함"
    assert "bonly" not in ids, "A와 무관한 B 전용 간호사는 안 보여야 함"


def test_outbound_transfer_inbound_block_carries_transfer(db):
    _seed(db)
    rows = get_nurses_in_group_service(_user("A", "o1"), db)
    moved = next(r for r in rows if r["nurse_id"] == "moved")
    entries = moved.get("inbound") or []
    assert entries, "전출자 inbound 블록에 completed 병동이동이 실려야 함"
    e = entries[0]
    assert e["reason"] == "병동이동"
    assert e["source_group_id"] == "A"      # 출발지가 나(A) → 프론트가 '전출' 방향 판단
    assert e["target_group_id"] == "B"
    assert e["target_group_name"] == "B병동"


def test_source_list_unaffected_for_pure_member(db):
    _seed(db)
    rows = get_nurses_in_group_service(_user("A", "o1"), db)
    stay = next(r for r in rows if r["nurse_id"] == "stay")
    assert (stay.get("inbound") or []) == []  # 현직은 inbound 없음


def test_destination_view_not_polluted_by_completed_transfer(db):
    """전입처(B)는 완료된 병동이동(source=A)으로 화면 오염되지 않아야 함."""
    _seed(db)
    rows = get_nurses_in_group_service(_user("B", "o1"), db)
    moved = next(r for r in rows if r["nurse_id"] == "moved")
    # B 기준 source==B 인 completed 만 포함하므로 source=A 전출 행은 미포함
    assert (moved.get("inbound") or []) == []
