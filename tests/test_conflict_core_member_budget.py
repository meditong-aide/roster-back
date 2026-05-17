from services.cp_sat.hard_assumption import _apply_member_budget


def test_member_budget_no_truncation():
    core = {"members": [{"node_id": f"n{i}"} for i in range(5)]}
    out = _apply_member_budget(core, 10)
    assert out["members_total"] == 5
    assert out["members_collapsed_count"] == 0
    assert out["members_truncated"] is False


def test_member_budget_truncates_and_adds_metadata():
    core = {"members": [{"node_id": f"n{i}"} for i in range(20)], "derivation": []}
    out = _apply_member_budget(core, 8)
    assert len(out["members"]) == 8
    assert out["members_total"] == 20
    assert out["members_visible"] == 8
    assert out["members_collapsed_count"] == 12
    assert out["members_truncated"] is True
    assert len(out["members_overflow_sample"]) <= 5
    assert any(d.get("from") == "member_budget" for d in out["derivation"])
