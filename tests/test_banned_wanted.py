"""banned_wanted (금지 원티드) — fixed_wanted 의 배반.

검증 대상:
- 저장 검증: 개수(max2), 근무만(O 불가), 중복, 허용집합 합집합 가드(P3)
- None=미변경, []=전체 해제, 스냅샷 replace
- fixed 충돌 셀 → inert 경고(저장은 진행, fixed 우선)
- 컨버터: banned → initial_constraints forbidden 맵(P1 경로)
"""
from __future__ import annotations

from datetime import date
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from db.models import (
    Office, Group, Nurse, RosterConfig, BannedWantedEntry, FixedWantedEntry, Shift,
)
from schemas.roster_schema import FixedWantedCreate
from services.wanted_service import (
    save_banned_wanted_service, get_wanted_adjustment_service,
)
from services.roster_create_service import _build_banned_wanted_constraints


@pytest.fixture
def seeded(db):
    # 서비스의 db.commit() 이 conftest 단일 트랜잭션(롤백 격리)을 깨지 않도록 flush 로 치환.
    db.commit = db.flush  # type: ignore[assignment]
    db.add(Office(office_id="o1", office_name="병원"))
    db.add(Group(group_id="A", group_name="A병동", office_id="o1"))
    # n1: 제한 없음. n2: N 만 허용(allowed_shifts=["N"] → D,E 이미 금지)
    db.add(Nurse(nurse_id="n1", account_id="acc_n1", group_id="A", office_id="o1",
                 name="김간호", active=1, allowed_shifts=[]))
    db.add(Nurse(nurse_id="n2", account_id="acc_n2", group_id="A", office_id="o1",
                 name="이간호", active=1, allowed_shifts=["N"]))
    db.flush()
    return db


def _req(banned):
    return FixedWantedCreate(year=2026, month=8, entries=[], banned_entries=banned)


def _cell(nid, day, codes):
    return {"nurse_id": nid, "shift_date": f"2026-08-{day:02d}", "banned_shift_ids": codes}


def test_max2_rejects_three_work_shifts(seeded):
    db = seeded
    with pytest.raises(HTTPException) as ei:
        save_banned_wanted_service(db, "A", "n1", _req([_cell("n1", 14, ["D", "E", "N"])]))
    assert ei.value.status_code == 422
    assert ei.value.detail["errors"][0]["type"] == "max_2_bans"


def test_off_ban_not_allowed(seeded):
    db = seeded
    with pytest.raises(HTTPException) as ei:
        save_banned_wanted_service(db, "A", "n1", _req([_cell("n1", 14, ["O"])]))
    assert ei.value.detail["errors"][0]["type"] == "off_ban_not_allowed"


def test_duplicate_shift_rejected(seeded):
    db = seeded
    with pytest.raises(HTTPException) as ei:
        save_banned_wanted_service(db, "A", "n1", _req([_cell("n1", 14, ["D", "D"])]))
    assert ei.value.detail["errors"][0]["type"] == "duplicate_shift"


def test_p3_allowed_union_guard(seeded):
    """n2 는 N 만 허용(D,E 금지) + banned=[N] → 근무 여지 0 → 422."""
    db = seeded
    with pytest.raises(HTTPException) as ei:
        save_banned_wanted_service(db, "A", "n1", _req([_cell("n2", 14, ["N"])]))
    assert ei.value.detail["errors"][0]["type"] == "no_workable_shift_left"


def test_save_and_snapshot_replace(seeded):
    db = seeded
    rows, warns = save_banned_wanted_service(db, "A", "n1", _req([
        _cell("n1", 14, ["D", "E"]), _cell("n1", 20, ["N"]),
    ]))
    assert len(rows) == 2 and warns == []
    stored = {(r.shift_date.day, tuple(r.banned_shift_ids)) for r in
              db.query(BannedWantedEntry).filter_by(group_id="A").all()}
    assert stored == {(14, ("D", "E")), (20, ("N",))}

    # 스냅샷 replace: 14일만 다시 저장 → 20일 사라짐
    save_banned_wanted_service(db, "A", "n1", _req([_cell("n1", 14, ["E"])]))
    remaining = db.query(BannedWantedEntry).filter_by(group_id="A").all()
    assert len(remaining) == 1 and remaining[0].shift_date.day == 14
    assert remaining[0].banned_shift_ids == ["E"]


def test_none_is_noop_empty_clears(seeded):
    db = seeded
    save_banned_wanted_service(db, "A", "n1", _req([_cell("n1", 14, ["D"])]))
    # None → 미변경(기존 반환)
    rows, _ = save_banned_wanted_service(db, "A", "n1", _req(None))
    assert len(rows) == 1
    assert db.query(BannedWantedEntry).filter_by(group_id="A").count() == 1
    # [] → 전체 해제
    rows, _ = save_banned_wanted_service(db, "A", "n1", _req([]))
    assert rows == []
    assert db.query(BannedWantedEntry).filter_by(group_id="A").count() == 0


def test_fixed_conflict_dropped_not_stored(seeded):
    """fixed 셀의 banned 는 의미 없음 → 저장하지 않고(정합성) 통지만."""
    db = seeded
    db.add(FixedWantedEntry(group_id="A", year=2026, month=8, nurse_id="n1",
                            shift_date=date(2026, 8, 14), shift_id="D",
                            is_applied=True, source_type="original"))
    db.flush()
    rows, warns = save_banned_wanted_service(db, "A", "n1", _req([_cell("n1", 14, ["E"])]))
    assert len(rows) == 0  # 확정 셀이라 저장 안 함
    assert db.query(BannedWantedEntry).filter_by(group_id="A").count() == 0
    assert len(warns) == 1 and warns[0]["reason"] == "dropped_on_fixed_cell"


def test_toggle_banned_reflects_in_converter(seeded):
    """반려(is_applied=False) 시 생성 컨버터가 제외, 다시 토글하면 되살아난다."""
    db = seeded
    from services.wanted_service import toggle_banned_wanted_entry_service
    rows, _ = save_banned_wanted_service(db, "A", "n1", _req([_cell("n1", 14, ["D", "E"])]))
    eid = rows[0].id
    cur = SimpleNamespace(group_id="A")
    req = SimpleNamespace(year=2026, month=8)
    nurses = [db.query(Nurse).get("n1")]

    # 반려 → is_applied False → 컨버터 제외
    e = toggle_banned_wanted_entry_service(db, eid, caller_group_id="A")
    assert e.is_applied is False
    assert _build_banned_wanted_constraints(db, cur, req, nurses)["forbidden"] == {}

    # 다시 토글 → 적용 → 되살아남
    e = toggle_banned_wanted_entry_service(db, eid, caller_group_id="A")
    assert e.is_applied is True
    assert _build_banned_wanted_constraints(db, cur, req, nurses)["forbidden"] == {"n1": {13: ["D", "E"]}}


def test_toggle_banned_cross_group_blocked(seeded):
    """다른 병동 caller 는 토글 불가(403)."""
    db = seeded
    from services.wanted_service import toggle_banned_wanted_entry_service
    rows, _ = save_banned_wanted_service(db, "A", "n1", _req([_cell("n1", 14, ["D"])]))
    with pytest.raises(HTTPException) as ei:
        toggle_banned_wanted_entry_service(db, rows[0].id, caller_group_id="B")
    assert ei.value.status_code == 403


def test_converter_builds_forbidden_map(seeded):
    """banned → {"forbidden": {nurse_id:{day_idx:[codes]}}} (P1 경로)."""
    db = seeded
    db.add(BannedWantedEntry(group_id="A", year=2026, month=8, nurse_id="n1",
                             shift_date=date(2026, 8, 14), banned_shift_ids=["D", "E"],
                             is_applied=True))
    db.add(BannedWantedEntry(group_id="A", year=2026, month=8, nurse_id="n1",
                             shift_date=date(2026, 8, 20), banned_shift_ids=["N"],
                             is_applied=False))  # 미적용 → 제외
    db.flush()
    cur = SimpleNamespace(group_id="A")
    req = SimpleNamespace(year=2026, month=8)
    nurses = [db.query(Nurse).get("n1")]

    # 별도 플래그 없이 항상 적용 — is_applied 금지만 반영
    out = _build_banned_wanted_constraints(db, cur, req, nurses)
    assert out["forbidden"] == {"n1": {13: ["D", "E"]}}  # day 14 → idx 13, 미적용 제외
