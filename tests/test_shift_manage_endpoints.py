"""shift-manage / shifts 엔드포인트 핸들러 통합 테스트(라이브 경로).

HTTP/TestClient 대신 async 라우터 핸들러를 직접 호출해 실제 경로
(핸들러 → 서비스 → group_access 해석 → ShiftManage)를 SQLite 위에서 검증한다.
권한은 is_master_admin 스텁으로 통과(엔드포인트가 HN OR master 허용).

검증 포커스(이번 변경):
- GET 자동생성: 유효 클래스만 생성(junk 'save' 차단), 재호출 시 중복 없음
- save: 비파괴 upsert, 슬롯당 1행, 재저장 멱등
- add → shift_manage codes 동기화, remove → orphan code cascade 정리
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from db.models import Office, Group, Shift, ShiftManage
from routers.shifts import get_shift_manage, save_shift_manage, remove_shift
from schemas.roster_schema import ShiftManageSaveRequest, RemoveShiftRequest


def _stub():
    return SimpleNamespace(
        nurse_id="hn1", account_id="hn1", office_id="o1", group_id="g1",
        is_master_admin=True, is_head_nurse=True, original_group_id=None,
    )


def _seed(db):
    db.add(Office(office_id="o1", office_name="H"))
    db.add(Group(group_id="g1", group_name="g1", office_id="o1"))
    db.flush()


def _run(coro):
    return asyncio.run(coro)


def _slots(db, nurse_class="RN"):
    return (
        db.query(ShiftManage)
        .filter(ShiftManage.office_id == "o1", ShiftManage.group_id == "g1",
                ShiftManage.nurse_class == nurse_class)
        .all()
    )


def test_get_autocreate_valid_class(db):
    """GET RN(유효 클래스) → 기본 슬롯 1/2/3/5 생성 + 응답에 합성 slot4 포함, 중복 없음."""
    _seed(db)
    resp = _run(get_shift_manage(class_name="RN", group_id="g1", current_user=_stub(), db=db))
    stored = _slots(db)
    assert sorted(s.shift_slot for s in stored) == [1, 2, 3, 5]
    assert any(r["shift_slot"] == 4 and r["main_code"] == "O" for r in resp)  # 합성 slot4


def test_get_junk_class_blocked(db):
    """GET save(junk 클래스) → 자동생성 안 함(화이트리스트). 저장 행 0."""
    _seed(db)
    _run(get_shift_manage(class_name="save", group_id="g1", current_user=_stub(), db=db))
    assert _slots(db, "save") == []


def test_get_idempotent_no_duplicate(db):
    """GET RN 2회 → 슬롯당 1행 유지(중복 생성 없음)."""
    _seed(db)
    _run(get_shift_manage(class_name="RN", group_id="g1", current_user=_stub(), db=db))
    _run(get_shift_manage(class_name="RN", group_id="g1", current_user=_stub(), db=db))
    stored = _slots(db)
    assert sorted(s.shift_slot for s in stored) == [1, 2, 3, 5]  # 8개 아님


def test_save_upsert_no_duplicate_and_idempotent(db):
    """save 2회 → 슬롯당 1행, manpower 반영."""
    _seed(db)
    req = ShiftManageSaveRequest(class_name="RN", slots=[
        {"shift_slot": 1, "main_code": "D", "codes": ["D"], "manpower": 5},
        {"shift_slot": 3, "main_code": "N", "codes": ["N"], "manpower": 2},
    ])
    _run(save_shift_manage(req=req, group_id="g1", current_user=_stub(), db=db))
    _run(save_shift_manage(req=req, group_id="g1", current_user=_stub(), db=db))
    rows = {s.shift_slot: s for s in _slots(db)}
    assert set(rows) == {1, 3}
    assert rows[1].manpower == 5 and rows[3].manpower == 2
    assert db.query(ShiftManage).filter(ShiftManage.shift_slot == 1).count() == 1


def test_remove_shift_cascades_codes(db):
    """remove(나이트 N1) 핸들러 → slot3 codes 에서 'N1' 제거(orphan 방지) + Shift 실삭제.

    (add 핸들러는 Shift 복합PK(shift_id,id) 때문에 SQLite 에서 id autoincrement 불가 →
     실 MSSQL 전용. 여기선 Shift 를 id 명시로 사전 시드한 뒤 remove cascade 만 검증.
     add→codes 동기화는 test_shift_manage_dedup_cascade 의 _append 유닛테스트가 커버.)
    """
    _seed(db)
    db.add(Shift(id=1, shift_id="N1", office_id="o1", group_id="g1", name="N1",
                 color="#111111", type="근무", shift_gb="나이트", sequence=1))
    db.add(ShiftManage(office_id="o1", group_id="g1", nurse_class="RN", shift_slot=3,
                       main_code="N", codes=["N1"], manpower=2))
    db.flush()

    _run(remove_shift(req=RemoveShiftRequest(shift_id="N1"), group_id="g1",
                      current_user=_stub(), db=db))

    slot3 = db.query(ShiftManage).filter(
        ShiftManage.group_id == "g1", ShiftManage.shift_slot == 3).one()
    assert "N1" not in (slot3.codes or [])
    assert db.query(Shift).filter(Shift.shift_id == "N1").count() == 0  # 실제 삭제
