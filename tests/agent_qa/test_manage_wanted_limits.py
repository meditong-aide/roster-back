"""Wave-2: manage_wanted_limits skill 검증.

복잡한 wanted seed 없이 happy/empty path + clarification 위주.
실제 over-limit 시나리오는 services 단의 wanted_service 테스트가 담당.
"""

from __future__ import annotations

from agents_v2.skills.registry import SKILL_REGISTRY, _ensure_loaded, run_skill

_ensure_loaded()


def _params(seed_data, **kw):
    base = {"group_id": seed_data["group_id"], "year": 2026, "month": 7}
    base.update(kw)
    return base


def test_skill_registered():
    assert (
        "manage-wanted-limits" in SKILL_REGISTRY
        or "manage_wanted_limits" in SKILL_REGISTRY
    )


# ── gates ─────────────────────────────────────────────


def test_missing_rbac_returns_error(db):
    res = run_skill(db, "manage-wanted-limits", {"operation": "list_over_limit", "year": 2026, "month": 7})
    assert "error" in res


def test_missing_year_month_returns_error(db, seed_data):
    res = run_skill(db, "manage-wanted-limits", {
        "operation": "list_over_limit", "group_id": seed_data["group_id"],
    })
    assert "error" in res


def test_unknown_operation_returns_clarification(db, seed_data):
    res = run_skill(db, "manage-wanted-limits", _params(seed_data, operation="foo"))
    assert res.get("needs_clarification") is True


# ── list_over_limit (empty) ───────────────────────────


def test_list_over_limit_empty_returns_friendly_message(db, seed_data):
    res = run_skill(db, "manage-wanted-limits", _params(
        seed_data, operation="list_over_limit",
    ))
    assert res["operation"] == "list_over_limit"
    assert res["count"] == 0
    assert isinstance(res["items"], list)
    assert "없어요" in res["message"]


# ── delete_excess_off ─────────────────────────────────


def test_delete_excess_off_missing_nurse_clarifies(db, seed_data):
    res = run_skill(db, "manage-wanted-limits", _params(
        seed_data, operation="delete_excess_off",
    ))
    assert res.get("needs_clarification") is True


def test_delete_excess_off_preview_for_non_over_limit_says_nothing_to_clean(db, seed_data):
    """seed 에 wanted 가 없으므로 N002 도 over-limit 아님."""
    res = run_skill(db, "manage-wanted-limits", _params(
        seed_data, operation="delete_excess_off",
        nurse_id="N002", nurse_name="박지은", preview_only=True,
    ))
    assert res["preview"] is True
    assert res["applied"] is False
    assert "박지은" in res["message"]
    assert "초과하지 않" in res["message"] or "정리할 것이 없" in res["message"]


def test_delete_excess_off_apply_returns_message(db, seed_data):
    """over-limit 없는 상태에서도 apply 호출은 안전하게 처리 (count=0)."""
    res = run_skill(db, "manage-wanted-limits", _params(
        seed_data, operation="delete_excess_off",
        nurse_id="N002", nurse_name="박지은", preview_only=False,
    ))
    assert res["applied"] is True
    assert "박지은" in res["message"]
    # nurse_id 는 _internal 격리.
    assert "_internal" in res
    assert res["_internal"]["nurse_id"] == "N002"


# ── UX 가드 ────────────────────────────────────────────


def test_list_over_limit_message_contains_year_month(db, seed_data):
    res = run_skill(db, "manage-wanted-limits", _params(
        seed_data, operation="list_over_limit", year=2026, month=8,
    ))
    assert "2026" in res["message"] and "8월" in res["message"]
