"""manage_team_min skill — 팀 최소 인원 조회/수정 + grounding + preview/confirm + 권한.

seed_data 팀 구성: A팀(team_id=1)=N001,N002,N005 / B팀(team_id=2)=N003,N004,N006 (각 3명).
초기 min_shift 는 둘 다 미설정(None).
"""

from __future__ import annotations

from agents_v2.middleware import _check_permission
from agents_v2.schemas.session_context import SessionContext
from agents_v2.skills.manage_team_min import manage_team_min
from agents_v2.skills.registry import SKILL_REGISTRY, _ensure_loaded
from services.team_service import list_teams_with_members

_ensure_loaded()


def _team_min(db, team_name: str):
    for t in list_teams_with_members(db, "OFF001", "GRP001"):
        if t["team_name"] == team_name:
            return t["min_shift"]
    return None


def _args(op: str, **kw) -> dict:
    return {"operation": op, "group_id": "GRP001", "office_id": "OFF001", **kw}


# ── registry ─────────────────────────────────────────────


def test_manage_team_min_registered():
    assert "manage-team-min" in SKILL_REGISTRY or "manage_team_min" in SKILL_REGISTRY


# ── read ─────────────────────────────────────────────────


def test_read_empty(db, seed_data):
    res = manage_team_min(db, _args("read"))
    teams = {t["team"]: t for t in res["teams"]}
    assert "A팀" in teams and "B팀" in teams
    assert teams["A팀"]["min_requirements"] == []


def test_read_after_set(db, seed_data):
    manage_team_min(db, _args("set_min", team_name="A팀", shift_name="데이", min_count=2, preview_only=False))
    res = manage_team_min(db, _args("read"))
    a = next(t for t in res["teams"] if t["team"] == "A팀")
    assert {"shift": "데이", "min_count": 2} in a["min_requirements"]


# ── set_min: preview / merge / capacity ──────────────────


def test_set_min_preview_does_not_persist(db, seed_data):
    res = manage_team_min(db, _args("set_min", team_name="A팀", shift_name="나이트", min_count=1, preview_only=True))
    assert res["preview"] is True
    assert res["changes"][0]["after"] == 1
    assert _team_min(db, "A팀") in (None, {})  # DB 미반영


def test_set_min_apply_then_merge_preserves(db, seed_data):
    manage_team_min(db, _args("set_min", team_name="A팀", shift_name="데이", min_count=2, preview_only=False))
    manage_team_min(db, _args("set_min", team_name="A팀", shift_name="나이트", min_count=1, preview_only=False))
    ms = _team_min(db, "A팀")
    assert int(ms["D"]) == 2  # 기존 보존
    assert int(ms["N"]) == 1  # 추가


def test_set_min_capacity_exceeded_rejected(db, seed_data):
    # A팀 3명인데 데이 최소 10명 → 불가
    res = manage_team_min(db, _args("set_min", team_name="A팀", shift_name="데이", min_count=10, preview_only=True))
    assert "error" in res
    assert _team_min(db, "A팀") in (None, {})


# ── clear_min ────────────────────────────────────────────


def test_clear_min_one_shift_preserves_others(db, seed_data):
    manage_team_min(db, _args("set_min", team_name="A팀", shift_name="데이", min_count=2, preview_only=False))
    manage_team_min(db, _args("set_min", team_name="A팀", shift_name="나이트", min_count=1, preview_only=False))
    manage_team_min(db, _args("clear_min", team_name="A팀", shift_name="나이트", preview_only=False))
    ms = _team_min(db, "A팀")
    assert int(ms["D"]) == 2
    assert "N" not in ms


def test_clear_min_all(db, seed_data):
    manage_team_min(db, _args("set_min", team_name="A팀", shift_name="데이", min_count=2, preview_only=False))
    manage_team_min(db, _args("clear_min", team_name="A팀", preview_only=False))
    assert _team_min(db, "A팀") in (None, {})


# ── grounding clarifications ─────────────────────────────


def test_team_not_found_clarifies(db, seed_data):
    res = manage_team_min(db, _args("set_min", team_name="Z팀", shift_name="데이", min_count=1, preview_only=True))
    assert res.get("needs_clarification") is True
    assert "A팀" in res["options"] and "B팀" in res["options"]


def test_shift_unresolved_clarifies(db, seed_data):
    # use_mid=False 인데 '미드' → 미해석 → 재질의
    res = manage_team_min(db, _args("set_min", team_name="A팀", shift_name="미드", min_count=1, preview_only=True))
    assert res.get("needs_clarification") is True


# ── payload 최소화 — 변경이 팀 생성/이름변경/멤버이동을 일으키지 않음 ──


def test_apply_does_not_create_or_rename_team(db, seed_data):
    before = list_teams_with_members(db, "OFF001", "GRP001")
    before_names = sorted(t["team_name"] for t in before)
    before_members = {t["team_name"]: sorted(t["team_members"]) for t in before}

    manage_team_min(db, _args("set_min", team_name="A팀", shift_name="데이", min_count=2, preview_only=False))

    after = list_teams_with_members(db, "OFF001", "GRP001")
    assert sorted(t["team_name"] for t in after) == before_names  # 팀 수/이름 불변
    assert {t["team_name"]: sorted(t["team_members"]) for t in after} == before_members  # 멤버 불변


# ── permission ───────────────────────────────────────────


def _ctx(role: str) -> SessionContext:
    return SessionContext(
        office_id="OFF001", group_id="GRP001", year=2026, month=5,
        nurse_id="N001", nurse_name="김민지", user_role=role,
    )


def test_permission_nurse_blocked_for_mutation():
    err = _check_permission("manage_team_min", {"operation": "set_min"}, _ctx("nurse"))
    assert err is not None and ("수간호사" in err or "ADM" in err)


def test_permission_nurse_blocked_for_read():
    # 팀 최소 인원도 HN/ADM 전용 — read 포함 차단.
    err = _check_permission("manage_team_min", {"operation": "read"}, _ctx("nurse"))
    assert err is not None and ("수간호사" in err or "ADM" in err)


def test_permission_hn_allowed_for_mutation():
    err = _check_permission("manage_team_min", {"operation": "clear_min"}, _ctx("HN"))
    assert err is None
