"""프리셉터 게이지 — 에이전트가 못 건드리게 차단됐는지 (policy_locked)."""
from agents_v2.skills.update_constraint import update_constraint


def _params(preview):
    return {"group_id": "GRP001", "field": "preceptor_gauge", "value": 5, "preview_only": preview}


def test_preceptor_gauge_blocked_apply(db, seed_data):
    res = update_constraint(db, _params(preview=False))
    assert res.get("error") == "policy_locked" and res.get("field") == "preceptor_gauge"


def test_preceptor_gauge_blocked_preview(db, seed_data):
    # preview 도 거절(미리보기조차 안 만들어짐)
    res = update_constraint(db, _params(preview=True))
    assert res.get("error") == "policy_locked"


def test_team_balance_gauge_still_works(db, seed_data):
    # 대조군: 정상 게이지(team_balance_gauge)는 여전히 통과(과잉차단 아님)
    res = update_constraint(db, {"group_id": "GRP001", "field": "team_balance_gauge",
                                 "value": 7, "preview_only": True})
    assert res.get("error") != "policy_locked"
