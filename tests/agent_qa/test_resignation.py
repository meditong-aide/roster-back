"""퇴사 처리(resignation_date) + 명단삭제 navigate 안내 검증.

- update_person_attr 로 resignation_date 설정(preview→apply, 컬럼 반영)
- 퇴사일 없으면 clarify (반드시 날짜)
- 날짜 파싱(_coerce_date)
- descriptions: 퇴사→resignation_date, 명단삭제→nurse_management 안내 인코딩
"""

from __future__ import annotations

from datetime import datetime

from agents_v2.skills.update_person_attr import update_person_attr
from agents_v2.tools.nurse_tools import _coerce_date, UPDATABLE_FIELDS
from db.models import Nurse


def test_resignation_date_in_updatable_fields():
    assert "resignation_date" in UPDATABLE_FIELDS


def test_coerce_date_formats():
    assert _coerce_date("2026-08-31")[0] == datetime(2026, 8, 31)
    assert _coerce_date("2026.08.31")[0] == datetime(2026, 8, 31)
    assert _coerce_date("2026/08/31")[0] == datetime(2026, 8, 31)
    assert _coerce_date("해제") == (None, None)   # 퇴사 취소
    assert _coerce_date("")[0] is None
    assert _coerce_date("아무거나")[1] is not None  # 파싱 실패 → 에러메시지


def test_resignation_requires_date(db, seed_data):
    # 퇴사인데 날짜 없음 → clarify (반드시 날짜)
    res = update_person_attr(db, {
        "nurse_ids": ["N001"], "group_id": "GRP001",
        "field": "resignation_date", "value": None,
    })
    assert res.get("needs_clarification") is True
    assert "퇴사일" in res.get("question", "")


def test_resignation_preview_and_apply(db, seed_data):
    # preview
    prev = update_person_attr(db, {
        "nurse_ids": ["N001"], "group_id": "GRP001",
        "field": "resignation_date", "value": "2026-08-31", "preview_only": True,
    })
    assert prev.get("preview") is True
    assert any(m["field"] == "resignation_date" for m in prev["applied_mutations"])

    # apply → 컬럼 반영
    update_person_attr(db, {
        "nurse_ids": ["N001"], "group_id": "GRP001",
        "field": "resignation_date", "value": "2026-08-31",
    })
    n = db.query(Nurse).filter(Nurse.nurse_id == "N001").first()
    assert n.resignation_date == datetime(2026, 8, 31)


def test_descriptions_encode_resignation_and_delete():
    from agents_v2.skills.descriptions import SKILL_TOOLS
    upa = next(t for t in SKILL_TOOLS if t["name"] == "update_person_attr")["description"]
    nav = next(t for t in SKILL_TOOLS if t["name"] == "navigate")["description"]
    assert "resignation_date" in upa and "퇴사일이 필요" in upa
    assert "명단에서 삭제" in nav and "수정" in nav
