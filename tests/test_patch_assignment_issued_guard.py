"""화면 경로(PATCH /nurses/{id} · 구 배정 API · 일괄 수정)의 병동이동 × 발행된 달 가드 — dev-7.

PATCH·일괄 수정은 op 마다 따로 커밋한다. 발행 가드는 **첫 op 커밋 전에** 한꺼번에 봐서 '일부만 저장된 채
409' 를 막는다. 사용자는 실제 로그인 사용자(UserSchema)로 넣는다.
"""
from __future__ import annotations

from datetime import date

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from db.client2 import get_db
from db.models import Group, IssuedRosterSnapshot, Nurse, NurseAssignment, Office, Schedule
from routers.auth import get_current_user_from_cookie
from routers.nurses import router as nurses_router
from tests.agent_qa.harness import make_auth_user


@pytest.fixture
def client(db):
    db.add(Office(office_id="o1", office_name="병원"))
    db.add(Group(group_id="A", group_name="A병동", office_id="o1"))
    db.add(Group(group_id="B", group_name="B병동", office_id="o1"))
    for nid in ("n1", "n2"):
        db.add(Nurse(nurse_id=nid, account_id=f"acc_{nid}", group_id="A", office_id="o1", name=nid,
                     active=1, allowed_shifts=[], grade=2))
    db.flush()
    app = FastAPI()
    app.include_router(nurses_router)
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_current_user_from_cookie] = lambda: make_auth_user(
        "ADM1", "관리자", role="ADM", office_id="o1", group_id="A")
    return TestClient(app), db


def _issue(db, gid, year, month, office="o1", meta="auto"):
    sid = f"ISS{gid}{year % 100:02d}{month:02d}"
    db.add(Schedule(schedule_id=sid, office_id=office, group_id=gid, year=year, month=month,
                    version=1, status="issued", dropped=False))
    db.add(IssuedRosterSnapshot(office_id=office, group_id=gid, schedule_id=sid, version=1,
                                is_active_issued=True,
                                meta_json={"year": year, "month": month} if meta == "auto" else meta))
    db.flush()


def _transfer_op(start):
    return {"operation": "create", "reason": "병동이동", "target_group_id": "B", "start_date": start}


def _detail(r):
    return r.json()["detail"]


def test_patch_create_transfer_on_issued_is_409(client):
    c, db = client
    _issue(db, "B", 2026, 9)   # 받는 병동(Q48)
    r = c.patch("/nurses/n1?group_id=A", json={"assignment": _transfer_op("2026-09-01")})
    assert r.status_code == 409, r.text
    assert _detail(r)["code"] == "ISSUED_ROSTER_BLOCKS_TRANSFER"
    assert "발행된 근무표" in _detail(r)["message"] and "B병동 2026년 9월" in _detail(r)["message"]
    assert db.query(NurseAssignment).count() == 0


def test_patch_no_partial_commit_when_later_op_blocked(client):
    """[파견 취소, 발행 달 병동이동 생성] — 선행 판정으로 취소도 저장되지 않는다."""
    c, db = client
    _issue(db, "A", 2026, 9)
    d = NurseAssignment(nurse_id="n1", source_group_id="A", target_group_id="B", office_id="o1",
                        start_date=date(2026, 8, 1), expected_end_date=date(2026, 8, 31),
                        reason="파견", kind="dispatch", status="active")
    db.add(d)
    db.flush()
    r = c.patch("/nurses/n1?group_id=A", json={"assignments": [
        {"operation": "cancel", "assignment_id": d.id}, _transfer_op("2026-09-01")]})
    assert r.status_code == 409, r.text
    assert db.get(NurseAssignment, d.id).status == "active"


def test_put_update_end_into_issued_is_409(client):
    c, db = client
    _issue(db, "A", 2026, 12)
    row = NurseAssignment(nurse_id="n1", source_group_id="A", target_group_id="B", office_id="o1",
                          start_date=date(2026, 9, 1), expected_end_date=date(2026, 10, 31),
                          reason="병동이동", kind="transfer", status="active")
    db.add(row)
    db.flush()
    r = c.put(f"/nurses/assignments/{row.id}", json={"expected_end_date": "2026-12-31"})
    assert r.status_code == 409, r.text
    assert db.get(NurseAssignment, row.id).expected_end_date == date(2026, 10, 31)


def test_bulk_update_second_blocked_first_not_applied(client):
    """둘째 간호사만 막혀도 첫째가 먼저 커밋되지 않는다(루프 전 선행 판정)."""
    c, db = client
    _issue(db, "B", 2026, 9)
    body = [
        {"office_id": "o1", "nurse_id": "n1", "group_id": "A", "account_id": "acc_n1", "name": "n1",
         "assignment": _transfer_op("2026-11-01")},   # 11월부터 — 9월 발행본 무관
        {"office_id": "o1", "nurse_id": "n2", "group_id": "A", "account_id": "acc_n2", "name": "n2",
         "assignment": _transfer_op("2026-09-01")},   # 9월부터 — 막힘
    ]
    r = c.post("/nurses/bulk-update?group_id=A", json=body)
    assert r.status_code == 409, r.text
    assert db.query(NurseAssignment).count() == 0


def test_preview_endpoint_reports_issued_guard(client):
    c, db = client
    _issue(db, "A", 2026, 10)
    r = c.post("/nurses/assignments/preview", json={
        "nurse_id": "n1", "reason": "병동이동", "start_date": "2026-09-01", "target_group_id": "B"})
    assert r.status_code == 200, r.text
    guard = r.json()["issued_guard"]
    assert guard["blocked"] is True and guard["months"][0]["month"] == 10


# ── 다른 병원 병동은 선행 판정·미리보기에서 읽지 않는다(dev-7 리뷰) ─────────────
# 선행 판정이 권한·병원 경계 검사보다 먼저 돌면, 원래 403/400 이어야 할 요청이 409 로 바뀌고
# 그 병원 병동 이름·발행 월(판정 불가면 발행본 번호까지)이 응답에 실렸다.

_FOREIGN_NAME = "남의병원중환자실"


@pytest.fixture
def other_office(client):
    """o2 병원 — 병동 X·Z, X→Z 병동이동 1건, Z 에 9월 발행본(연월 판정 불가 발행본은 X 에)."""
    c, db = client
    db.add(Office(office_id="o2", office_name="남의병원"))
    db.add(Group(group_id="X", group_name="남의병원병동", office_id="o2"))
    db.add(Group(group_id="Z", group_name=_FOREIGN_NAME, office_id="o2"))
    db.add(Nurse(nurse_id="z1", account_id="acc_z1", group_id="X", office_id="o2", name="z1",
                 active=1, allowed_shifts=[], grade=2))
    row = NurseAssignment(nurse_id="z1", source_group_id="X", target_group_id="Z", office_id="o2",
                          start_date=date(2026, 8, 1), expected_end_date=date(2026, 10, 31),
                          reason="병동이동", kind="transfer", status="active")
    db.add(row)
    db.flush()
    _issue(db, "Z", 2026, 9, office="o2")
    _issue(db, "X", 2026, 1, office="o2", meta=None)
    return c, db, row


def _no_foreign_leak(r):
    assert _FOREIGN_NAME not in r.text and "남의병원병동" not in r.text and "ISSUED_ROSTER" not in r.text


@pytest.mark.parametrize("target", ["Z", "X"])
def test_patch_create_foreign_target_keeps_office_error(other_office, target):
    """다른 병원 병동으로 이동 생성 — 발행본이 있어도 409 가 아니라 원래의 병원 경계 400."""
    c, db, _ = other_office
    op = {"operation": "create", "reason": "병동이동", "target_group_id": target, "start_date": "2026-09-01"}
    r = c.patch("/nurses/n1?group_id=A", json={"assignment": op})
    assert r.status_code == 400 and "office 경계 위반" in r.text, r.text
    _no_foreign_leak(r)
    assert db.query(NurseAssignment).filter(NurseAssignment.nurse_id == "n1").count() == 0


def test_patch_update_foreign_assignment_keeps_403(other_office):
    """다른 병원 배정 id 로 수정 — 원래의 403(다른 병원 배정), 그 병동 이름·발행 월이 실리지 않는다."""
    c, db, row = other_office
    op = {"operation": "update", "assignment_id": row.id, "start_date": "2026-09-15"}
    r = c.patch("/nurses/n1?group_id=A", json={"assignment": op})
    assert r.status_code == 403, r.text
    _no_foreign_leak(r)
    assert db.get(NurseAssignment, row.id).start_date == date(2026, 8, 1)


def test_bulk_update_foreign_assignment_keeps_403(other_office):
    c, db, row = other_office
    body = [{"office_id": "o1", "nurse_id": "n1", "group_id": "A", "account_id": "acc_n1", "name": "n1",
             "assignment": {"operation": "update", "assignment_id": row.id, "start_date": "2026-09-15"}}]
    r = c.post("/nurses/bulk-update?group_id=A", json=body)
    assert r.status_code == 403, r.text
    _no_foreign_leak(r)


def test_preview_foreign_target_not_judged(other_office):
    """미리보기는 다른 병원 도착 병동을 판정하지 않는다(적용은 병원 경계 400 — 409 가 날 일이 없다)."""
    c, db, _ = other_office
    r = c.post("/nurses/assignments/preview", json={
        "nurse_id": "n1", "reason": "병동이동", "start_date": "2026-09-01", "target_group_id": "Z"})
    assert r.status_code == 200, r.text
    assert r.json()["issued_guard"] == {"blocked": False, "code": None, "message": None, "months": []}
    assert _FOREIGN_NAME not in r.text


def test_same_office_still_blocked_with_other_office_present(other_office):
    """경계 검사를 통과한 같은 병원 이동은 그대로 막힌다(거르기가 판정을 끄지 않는다)."""
    c, db, _ = other_office
    _issue(db, "B", 2026, 9)
    r = c.patch("/nurses/n1?group_id=A", json={"assignment": _transfer_op("2026-09-01")})
    assert r.status_code == 409 and _detail(r)["months"][0]["group_name"] == "B병동", r.text


# ── 일괄 수정 잠금 순서: 마감 달 잠금 → 프로필 행 쓰기(dev-7 리뷰) ─────────────


def test_bulk_takes_month_lock_before_prior_nurse_row_write(client, monkeypatch):
    """앞 간호사의 nurses UPDATE 가 뒤 간호사 병동이동의 마감 달 잠금보다 먼저 나가지 않는다.

    마감은 그 달 잠금을 쥔 채 그 병동 간호사를 읽는다(RCSI 꺼짐) — 행을 먼저 잠그고 잠금을 기다리면 교착한다.
    배정 op 를 모든 간호사 먼저 처리해 '잠금 → 행' 순서를 지킨다.
    """
    from sqlalchemy import event
    from services import schedule_versioning

    c, db = client
    db.add(Schedule(schedule_id="SA2611", office_id="o1", group_id="A", year=2026, month=11,
                    version=1, status="draft", dropped=False))
    db.flush()
    events = []
    monkeypatch.setattr(schedule_versioning, "lock_month_publish",
                        lambda _db, g, y, m: events.append(("lock", g, y, m)))

    def _spy(conn, cursor, statement, params, context, executemany):
        if statement.strip().upper().startswith("UPDATE NURSES"):
            events.append(("UPDATE nurses",))

    engine = db.get_bind()
    event.listen(engine, "before_cursor_execute", _spy)
    try:
        r = c.post("/nurses/bulk-update?group_id=A", json=[
            {"office_id": "o1", "nurse_id": "n1", "group_id": "A", "account_id": "acc_n1", "name": "n1-이름변경"},
            {"office_id": "o1", "nurse_id": "n2", "group_id": "A", "account_id": "acc_n2", "name": "n2",
             "assignment": _transfer_op("2026-11-01")},
        ])
    finally:
        event.remove(engine, "before_cursor_execute", _spy)
    assert r.status_code == 200, r.text
    kinds = [e[0] for e in events]
    assert "lock" in kinds and "UPDATE nurses" in kinds, events
    assert kinds.index("lock") < kinds.index("UPDATE nurses"), events
    assert db.get(Nurse, "n1").name == "n1-이름변경"
    assert db.query(NurseAssignment).filter(NurseAssignment.nurse_id == "n2").count() == 1


# ── 취소도 같은 판정(사용자 결정 10-08) ─────────────────────────────────


def _active_transfer(db, start, end=None, reason="병동이동", kind="transfer"):
    row = NurseAssignment(nurse_id="n1", source_group_id="A", target_group_id="B", office_id="o1",
                          start_date=start, expected_end_date=end, reason=reason, kind=kind, status="active")
    db.add(row)
    db.flush()
    return row


def test_patch_cancel_transfer_on_issued_is_409(client):
    """휴지통(PATCH cancel)도 발행된 달에 걸린 병동이동은 막는다 — 취소로 수정 가드를 우회하지 못한다."""
    c, db = client
    _issue(db, "B", 2026, 9)
    row = _active_transfer(db, date(2026, 9, 1))
    r = c.patch("/nurses/n1?group_id=A", json={"assignment": {"operation": "cancel", "assignment_id": row.id}})
    assert r.status_code == 409, r.text
    assert _detail(r)["code"] == "ISSUED_ROSTER_BLOCKS_TRANSFER"
    assert db.get(NurseAssignment, row.id).status == "active"


def test_patch_cancel_transfer_outside_issued_months_passes(client):
    """발행된 달에 걸리지 않으면 취소는 그대로 된다."""
    c, db = client
    _issue(db, "A", 2026, 8)
    row = _active_transfer(db, date(2026, 10, 1))
    r = c.patch("/nurses/n1?group_id=A", json={"assignment": {"operation": "cancel", "assignment_id": row.id}})
    assert r.status_code == 200, r.text
    assert db.get(NurseAssignment, row.id).status == "cancelled"


def test_patch_cancel_dispatch_on_issued_still_passes(client):
    """파견은 가드 대상이 아니다(사용자 결정 10-08) — 발행된 달이어도 취소된다."""
    c, db = client
    _issue(db, "A", 2026, 9)
    row = _active_transfer(db, date(2026, 9, 1), date(2026, 9, 20), reason="파견", kind="dispatch")
    r = c.patch("/nurses/n1?group_id=A", json={"assignment": {"operation": "cancel", "assignment_id": row.id}})
    assert r.status_code == 200, r.text
    assert db.get(NurseAssignment, row.id).status == "cancelled"
