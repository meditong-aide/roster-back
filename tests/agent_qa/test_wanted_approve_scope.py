"""집합값 Tier1 (C) — '제출된 원티드 전원 승인'이 주휴/HN추가분 과승인 안 하는지.

wanted_adjustment approve(is_applied=1) 를 nurse 필터 없이 호출하면 승인 대상이
간호사 제출분(original/modified)으로 한정돼야 한다(weekly_off/added 제외).
"""
import pytest

from agents_v2.skills import bulk_mutation as bm
from agents_v2.tools import wanted_tools

_ROWS = [
    {"entry_id": "e1", "nurse_id": "n1", "source_type": "original"},
    {"entry_id": "e2", "nurse_id": "n2", "source_type": "modified"},
    {"entry_id": "e3", "nurse_id": "n3", "source_type": "weekly_off"},
    {"entry_id": "e4", "nurse_id": "n4", "source_type": "added"},
]


@pytest.fixture
def _capture(monkeypatch):
    monkeypatch.setattr(wanted_tools, "get_wanted_adjustments", lambda *a, **k: list(_ROWS))
    cap = {}

    def fake_bulk(db, entry_ids, field, value, *, group_id, preview_only=False):
        cap["ids"] = list(entry_ids); cap["field"] = field; cap["value"] = value
        return {"affected_count": len(entry_ids), "entry_ids": list(entry_ids)}

    monkeypatch.setattr(wanted_tools, "bulk_update_wanted_adjustments", fake_bulk)
    return cap


def _approve(nurse_ids=None, source_types=None):
    p = {"group_id": "G", "year": 2026, "month": 8, "scope": "wanted_adjustment",
         "mutation": {"target_field": "is_applied", "target_value": 1}}
    if nurse_ids:
        p["nurse_ids"] = nurse_ids
    if source_types:
        p["source_types"] = source_types
    return p


def test_approve_all_excludes_weekly_off_and_added(_capture):
    bm.bulk_mutation(None, _approve())
    assert set(_capture["ids"]) == {"e1", "e2"}, "주휴/HN추가분까지 승인됨(과승인)"


def test_approve_specific_nurse_not_source_scoped(_capture):
    # 개별 대상 지정 시엔 source_type 자동한정 안 함(그 간호사 행 그대로)
    bm.bulk_mutation(None, _approve(nurse_ids=["n3"]))
    assert set(_capture["ids"]) == {"e3"}


def test_explicit_source_types_respected(_capture):
    bm.bulk_mutation(None, _approve(source_types=["added"]))
    assert set(_capture["ids"]) == {"e4"}
