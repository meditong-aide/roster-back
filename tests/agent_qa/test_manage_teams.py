"""Wave-2: manage_teams skill 검증.

흐름: read / add / rename / delete (preview → apply 각각).
회귀 가드: internal team_id 노출 금지, 자연어 message 보장.
"""

from __future__ import annotations

import pytest

from agents_v2.skills.registry import SKILL_REGISTRY, _ensure_loaded, run_skill

_ensure_loaded()


def _params(seed_data, **kw):
    base = {
        "group_id": seed_data["group_id"],
        "office_id": seed_data["office_id"],
    }
    base.update(kw)
    return base


def test_skill_registered():
    assert "manage-teams" in SKILL_REGISTRY or "manage_teams" in SKILL_REGISTRY


# ── gates ───────────────────────────────────────────────


def test_missing_rbac_returns_error(db):
    res = run_skill(db, "manage-teams", {"operation": "read"})
    assert "error" in res


def test_unknown_operation_returns_clarification(db, seed_data):
    res = run_skill(db, "manage-teams", _params(seed_data, operation="foo"))
    assert res.get("needs_clarification") is True


# ── read ────────────────────────────────────────────────


def test_read_returns_team_list(db, seed_data):
    res = run_skill(db, "manage-teams", _params(seed_data, operation="read"))
    assert res["operation"] == "read"
    names = [t["team_name"] for t in res["teams"]]
    assert "A팀" in names and "B팀" in names
    # internal team_id 노출 금지.
    for t in res["teams"]:
        assert "team_id" not in t
    assert "message" in res


# ── add ─────────────────────────────────────────────────


def test_add_requires_name(db, seed_data):
    res = run_skill(db, "manage-teams", _params(seed_data, operation="add"))
    assert res.get("needs_clarification") is True


def test_add_duplicate_rejected(db, seed_data):
    res = run_skill(db, "manage-teams", _params(seed_data, operation="add", team_name="A팀"))
    assert res.get("error") == "duplicate_name"


def test_add_preview_does_not_persist(db, seed_data):
    res = run_skill(db, "manage-teams", _params(
        seed_data, operation="add", team_name="신생아실", preview_only=True,
    ))
    assert res["preview"] is True
    assert "신생아실" in res["message"] and "진행할까요" in res["message"]
    from db.models import Team
    found = db.query(Team).filter_by(
        group_id=seed_data["group_id"], team_name="신생아실",
    ).first()
    assert found is None


def test_add_apply_persists(db, seed_data):
    res = run_skill(db, "manage-teams", _params(
        seed_data, operation="add", team_name="신생아실", preview_only=False,
    ))
    assert res["applied"] is True
    assert "추가했어요" in res["message"]
    from db.models import Team
    found = db.query(Team).filter_by(
        group_id=seed_data["group_id"], team_name="신생아실",
    ).first()
    assert found is not None


# ── rename ──────────────────────────────────────────────


def test_rename_unknown_clarifies(db, seed_data):
    res = run_skill(db, "manage-teams", _params(
        seed_data, operation="rename", team_name="존재하지않음", new_name="X",
    ))
    assert res.get("needs_clarification") is True


def test_rename_to_existing_name_rejected(db, seed_data):
    res = run_skill(db, "manage-teams", _params(
        seed_data, operation="rename", team_name="A팀", new_name="B팀",
    ))
    assert res.get("error") == "duplicate_name"


def test_rename_apply_persists(db, seed_data):
    res = run_skill(db, "manage-teams", _params(
        seed_data, operation="rename", team_name="A팀", new_name="응급실",
        preview_only=False,
    ))
    assert res["applied"] is True
    assert "응급실" in res["message"]
    from db.models import Team
    found = db.query(Team).filter_by(
        group_id=seed_data["group_id"], team_name="응급실",
    ).first()
    assert found is not None


def test_rename_preview_does_not_persist(db, seed_data):
    res = run_skill(db, "manage-teams", _params(
        seed_data, operation="rename", team_name="A팀", new_name="응급실",
        preview_only=True,
    ))
    assert res["preview"] is True
    from db.models import Team
    assert db.query(Team).filter_by(
        group_id=seed_data["group_id"], team_name="응급실",
    ).first() is None


# ── delete ──────────────────────────────────────────────


def test_delete_unknown_clarifies(db, seed_data):
    res = run_skill(db, "manage-teams", _params(
        seed_data, operation="delete", team_name="존재하지않음",
    ))
    assert res.get("needs_clarification") is True


def test_delete_preview_shows_member_count_warn(db, seed_data):
    # A팀 에는 N001/N002/N005 (3명) 시드됨.
    res = run_skill(db, "manage-teams", _params(
        seed_data, operation="delete", team_name="A팀", preview_only=True,
    ))
    assert res["preview"] is True
    assert "A팀" in res["message"]
    assert "진행할까요" in res["message"]
