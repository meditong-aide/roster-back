"""Wave-1: manage_wanted_deadline skill 검증.

흐름:
  - read: 현재 마감일/상태 조회
  - set_deadline: 마감일 변경 (preview → apply)
  - close: 즉시 마감 (preview → apply)
  - 'closed' 된 원티드는 마감일 변경 불가
  - 잘못된 날짜 형식 → clarification
"""

from __future__ import annotations

from datetime import datetime

import pytest

from agents_v2.skills.registry import SKILL_REGISTRY, _ensure_loaded, run_skill

_ensure_loaded()


@pytest.fixture
def _wanted(db, seed_data):
    """seed wanted 1건 — 7월 / 마감일 없음 / requested."""
    from db.models import Wanted
    w = Wanted(
        group_id=seed_data["group_id"],
        year=2026,
        month=7,
        exp_date=None,
        status="requested",
    )
    db.add(w)
    db.flush()
    return w


def test_skill_registered():
    assert (
        "manage-wanted-deadline" in SKILL_REGISTRY
        or "manage_wanted_deadline" in SKILL_REGISTRY
    )


# ── basic gates ─────────────────────────────────────────


def test_missing_group_id_returns_error(db):
    res = run_skill(db, "manage-wanted-deadline", {"operation": "read", "year": 2026, "month": 7})
    assert "error" in res


def test_missing_year_month_returns_error(db, seed_data):
    res = run_skill(db, "manage-wanted-deadline", {"operation": "read", "group_id": seed_data["group_id"]})
    assert "error" in res


def test_unknown_operation_returns_clarification(db, seed_data, _wanted):
    res = run_skill(db, "manage-wanted-deadline", {
        "operation": "foo", "group_id": seed_data["group_id"], "year": 2026, "month": 7,
    })
    assert res.get("needs_clarification") is True


def test_wanted_not_found_returns_error(db, seed_data):
    res = run_skill(db, "manage-wanted-deadline", {
        "operation": "read", "group_id": seed_data["group_id"], "year": 2099, "month": 12,
    })
    assert res.get("error") == "not_found"


# ── read ─────────────────────────────────────────────────


def test_read_returns_current_state(db, seed_data, _wanted):
    res = run_skill(db, "manage-wanted-deadline", {
        "operation": "read", "group_id": seed_data["group_id"], "year": 2026, "month": 7,
    })
    assert res["status"] == "requested"
    assert res["exp_date"] is None
    assert res["display_exp_date"] == "마감일 없음"


# ── set_deadline ────────────────────────────────────────


def test_set_deadline_preview_does_not_persist(db, seed_data, _wanted):
    res = run_skill(db, "manage-wanted-deadline", {
        "operation": "set_deadline",
        "group_id": seed_data["group_id"], "year": 2026, "month": 7,
        "exp_date": "2026-07-10",
        "preview_only": True,
    })
    assert res["preview"] is True
    assert res["changes"][0]["after"] == "2026-07-10"
    # DB 확인 — 미적용
    from db.models import Wanted
    w = db.query(Wanted).filter(
        Wanted.group_id == seed_data["group_id"],
        Wanted.year == 2026, Wanted.month == 7,
    ).first()
    assert w.exp_date is None


def test_set_deadline_apply_persists(db, seed_data, _wanted):
    res = run_skill(db, "manage-wanted-deadline", {
        "operation": "set_deadline",
        "group_id": seed_data["group_id"], "year": 2026, "month": 7,
        "exp_date": "2026-07-10",
        "preview_only": False,
    })
    assert res["applied"] is True
    assert res["current_exp_date"] == "2026-07-10"
    from db.models import Wanted
    w = db.query(Wanted).filter(
        Wanted.group_id == seed_data["group_id"],
        Wanted.year == 2026, Wanted.month == 7,
    ).first()
    assert w.exp_date == datetime(2026, 7, 10)


def test_set_deadline_null_means_no_deadline(db, seed_data, _wanted):
    # 먼저 마감일 설정
    _wanted.exp_date = datetime(2026, 7, 10)
    db.flush()
    res = run_skill(db, "manage-wanted-deadline", {
        "operation": "set_deadline",
        "group_id": seed_data["group_id"], "year": 2026, "month": 7,
        "exp_date": None,
        "preview_only": False,
    })
    assert res["applied"] is True
    assert res["current_exp_date"] is None


def test_set_deadline_invalid_format_clarifies(db, seed_data, _wanted):
    res = run_skill(db, "manage-wanted-deadline", {
        "operation": "set_deadline",
        "group_id": seed_data["group_id"], "year": 2026, "month": 7,
        "exp_date": "다음주",
        "preview_only": True,
    })
    assert res.get("needs_clarification") is True


def test_set_deadline_rejected_when_already_closed(db, seed_data, _wanted):
    _wanted.status = "closed"
    db.flush()
    res = run_skill(db, "manage-wanted-deadline", {
        "operation": "set_deadline",
        "group_id": seed_data["group_id"], "year": 2026, "month": 7,
        "exp_date": "2026-07-10",
    })
    assert res.get("error") == "already_closed"


# ── close ───────────────────────────────────────────────


def test_close_preview_does_not_persist(db, seed_data, _wanted):
    res = run_skill(db, "manage-wanted-deadline", {
        "operation": "close",
        "group_id": seed_data["group_id"], "year": 2026, "month": 7,
        "preview_only": True,
    })
    assert res["preview"] is True
    from db.models import Wanted
    w = db.query(Wanted).filter(
        Wanted.group_id == seed_data["group_id"],
        Wanted.year == 2026, Wanted.month == 7,
    ).first()
    assert w.status == "requested"


def test_close_apply_sets_closed(db, seed_data, _wanted):
    res = run_skill(db, "manage-wanted-deadline", {
        "operation": "close",
        "group_id": seed_data["group_id"], "year": 2026, "month": 7,
        "preview_only": False,
    })
    assert res["applied"] is True
    assert res["status"] == "closed"
    from db.models import Wanted
    w = db.query(Wanted).filter(
        Wanted.group_id == seed_data["group_id"],
        Wanted.year == 2026, Wanted.month == 7,
    ).first()
    assert w.status == "closed"


def test_close_idempotent_when_already_closed(db, seed_data, _wanted):
    _wanted.status = "closed"
    db.flush()
    res = run_skill(db, "manage-wanted-deadline", {
        "operation": "close",
        "group_id": seed_data["group_id"], "year": 2026, "month": 7,
        "preview_only": False,
    })
    assert res["applied"] is False
    assert res["status"] == "closed"


# ── UX message 가드 — apply/preview 후 사용자 응답에 쓸 자연어 보장 ─────


def test_set_deadline_preview_has_message(db, seed_data, _wanted):
    res = run_skill(db, "manage-wanted-deadline", {
        "operation": "set_deadline",
        "group_id": seed_data["group_id"], "year": 2026, "month": 7,
        "exp_date": "2026-07-10", "preview_only": True,
    })
    assert "message" in res
    assert "2026-07-10" in res["message"] and "진행할까요" in res["message"]


def test_set_deadline_apply_has_friendly_message(db, seed_data, _wanted):
    res = run_skill(db, "manage-wanted-deadline", {
        "operation": "set_deadline",
        "group_id": seed_data["group_id"], "year": 2026, "month": 7,
        "exp_date": "2026-07-10", "preview_only": False,
    })
    assert "변경했어요" in res["message"]
    assert "2026-07-10" in res["message"]


def test_set_deadline_null_apply_says_removed(db, seed_data, _wanted):
    res = run_skill(db, "manage-wanted-deadline", {
        "operation": "set_deadline",
        "group_id": seed_data["group_id"], "year": 2026, "month": 7,
        "exp_date": None, "preview_only": False,
    })
    assert "없앴어요" in res["message"]


def test_close_preview_has_message(db, seed_data, _wanted):
    res = run_skill(db, "manage-wanted-deadline", {
        "operation": "close",
        "group_id": seed_data["group_id"], "year": 2026, "month": 7,
        "preview_only": True,
    })
    assert "마감합니다" in res["message"]
    assert "진행할까요" in res["message"]


def test_close_apply_has_friendly_message(db, seed_data, _wanted):
    res = run_skill(db, "manage-wanted-deadline", {
        "operation": "close",
        "group_id": seed_data["group_id"], "year": 2026, "month": 7,
        "preview_only": False,
    })
    assert "마감했어요" in res["message"]
