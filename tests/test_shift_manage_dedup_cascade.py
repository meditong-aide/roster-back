"""shift_manage 스키마 정합·중복·cascade 회귀 테스트.

검증 대상(이번 변경):
- ShiftManage 모델 PK=id + UNIQUE(office, group, nurse_class, shift_slot)
- remove_shift_service → _remove_shift_manage_code 로 orphan code 정리(cascade 누락 보완)
- routers.shifts._upsert_shift_manage_slots: 슬롯당 1행 upsert + nurse_class 필터
- 로더 견고화: _build_shift_manage_and_requirements(단일행 정확) /
  _build_code_to_main_map(codes 합집합)
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from sqlalchemy.exc import IntegrityError

from db.models import ShiftManage
from services.shift_service import (
    _append_shift_manage_code,
    _remove_shift_manage_code,
)
from routers.shifts import _upsert_shift_manage_slots
from services.roster_create_service import (
    _build_shift_manage_and_requirements,
    _build_code_to_main_map,
)


def _sm(db, office, group, slot, main, codes, mp, nurse_class="RN"):
    row = ShiftManage(
        office_id=office, group_id=group, nurse_class=nurse_class,
        shift_slot=slot, main_code=main, codes=codes, manpower=mp,
    )
    db.add(row)
    db.flush()
    return row


# ───────────────────────── UNIQUE 제약 ─────────────────────────

def test_unique_constraint_blocks_duplicate_slot(db):
    """(office, group, nurse_class, slot) 중복 insert 는 거부된다(중복 재발 방지의 핵심)."""
    _sm(db, "o1", "g1", 1, "D", ["D"], 3)
    db.add(ShiftManage(office_id="o1", group_id="g1", nurse_class="RN",
                       shift_slot=1, main_code="D", codes=[], manpower=9))
    with pytest.raises(IntegrityError):
        db.flush()
    db.rollback()


def test_unique_allows_other_class_and_slot(db):
    """같은 슬롯이라도 다른 nurse_class, 또는 다른 slot 은 허용된다."""
    _sm(db, "o1", "g1", 1, "D", [], 3, nurse_class="RN")
    _sm(db, "o1", "g1", 1, "D", [], 1, nurse_class="AN")  # 다른 class
    _sm(db, "o1", "g1", 2, "E", [], 3, nurse_class="RN")  # 다른 slot
    assert db.query(ShiftManage).count() == 3


# ───────────────────────── cascade (append/remove) ─────────────────────────

def test_append_then_remove_cascade(db):
    """근무코드 추가 → codes 반영, 삭제 cascade → codes 에서 제거(orphan 방지)."""
    _sm(db, "o1", "g1", 3, "N", [], 2)
    _append_shift_manage_code(db, "o1", "g1", "NT", "나이트")
    row = db.query(ShiftManage).filter_by(office_id="o1", group_id="g1", shift_slot=3).one()
    assert "NT" in row.codes

    removed = _remove_shift_manage_code(db, "o1", "g1", "NT", "나이트")
    db.flush()
    assert removed is True
    row = db.query(ShiftManage).filter_by(office_id="o1", group_id="g1", shift_slot=3).one()
    assert "NT" not in row.codes


def test_remove_cascade_unmapped_gb_is_noop(db):
    """매핑되지 않는 shift_gb('고정')·None 은 정리 대상이 아니다(False 반환, 예외 없음)."""
    _sm(db, "o1", "g1", 3, "N", ["NT"], 2)
    assert _remove_shift_manage_code(db, "o1", "g1", "X", "고정") is False
    assert _remove_shift_manage_code(db, "o1", "g1", "X", None) is False


def test_append_english_and_korean_gb_map_same_slot(db):
    """영문 'N' 과 한글 '나이트' 둘 다 slot 3(main_code N) 로 매핑된다."""
    _sm(db, "o1", "g1", 3, "N", [], 2)
    _append_shift_manage_code(db, "o1", "g1", "N1", "N")        # 영문 키
    _append_shift_manage_code(db, "o1", "g1", "N2", "나이트")    # 한글 키
    row = db.query(ShiftManage).filter_by(office_id="o1", group_id="g1", shift_slot=3).one()
    assert "N1" in row.codes and "N2" in row.codes


# ───────────────────────── upsert (저장) ─────────────────────────

def test_upsert_updates_existing_and_filters_class(db):
    """기존 RN 슬롯은 payload 로 갱신, 동일 슬롯의 AN 행은 건드리지 않는다."""
    _sm(db, "o1", "g1", 1, "D", ["D1"], 3, nurse_class="RN")
    _sm(db, "o1", "g1", 1, "D", [], 2, nurse_class="AN")

    _upsert_shift_manage_slots(
        db, "o1", "g1", "RN",
        [{"shift_slot": 1, "main_code": "D", "codes": ["D1", "D2"], "manpower": 5}],
    )
    db.flush()

    rn = db.query(ShiftManage).filter_by(
        office_id="o1", group_id="g1", shift_slot=1, nurse_class="RN").all()
    assert len(rn) == 1
    assert rn[0].codes == ["D1", "D2"]
    assert rn[0].manpower == 5
    an = db.query(ShiftManage).filter_by(
        office_id="o1", group_id="g1", shift_slot=1, nurse_class="AN").one()
    assert an.manpower == 2  # 미변경


def test_upsert_inserts_when_missing(db):
    """없는 슬롯은 새로 insert 된다."""
    _upsert_shift_manage_slots(
        db, "o1", "g1", "RN",
        [{"shift_slot": 2, "main_code": "E", "codes": ["E"], "manpower": 3}],
    )
    db.flush()
    row = db.query(ShiftManage).filter_by(office_id="o1", group_id="g1", shift_slot=2).one()
    assert row.nurse_class == "RN" and row.manpower == 3 and row.codes == ["E"]


# ───────────────────────── 로더 견고화 ─────────────────────────

def test_loader_reads_manpower_per_main(db):
    """_build_shift_manage_and_requirements 가 슬롯별 manpower 를 main_code 키로 정확히 읽는다."""
    _sm(db, "o1", "g1", 1, "D", ["D"], 3)
    _sm(db, "o1", "g1", 2, "E", ["E"], 4)
    _sm(db, "o1", "g1", 3, "N", ["N"], 7)
    cu = SimpleNamespace(office_id="o1", group_id="g1")
    cfg = SimpleNamespace(use_mid=False)
    req = SimpleNamespace(year=2026, month=7)

    data, daily, by_day, max_by_day = _build_shift_manage_and_requirements(db, cu, cfg, req)
    assert daily == {"D": 3, "E": 4, "N": 7}
    assert len(by_day) == 31  # 7월 일수, 일자별 폴백 = base dict


def test_code_to_main_unions_scattered_codes():
    """_build_code_to_main_map 은 같은 슬롯의 흩어진 codes 를 합집합으로 매핑한다(dedup 보존 규칙)."""
    # dedup 전 흩어진 중복행을 흉내낸 입력(순수 함수라 DB 불필요)
    shift_manage_data = [
        {"main_code": "N", "codes": ["N", "N2"]},
        {"main_code": "N", "codes": ["N2", "N3"]},
    ]
    code2main = _build_code_to_main_map(shift_manage_data)
    for c in ("N", "N2", "N3"):
        assert code2main[c] == "N"
    assert code2main["주"] == "O"  # 휴무 정규화
