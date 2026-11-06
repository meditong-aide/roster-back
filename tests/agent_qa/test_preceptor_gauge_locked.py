"""프리셉터 게이지 — 에이전트가 못 건드리게 차단.

dev 머지 후: preceptor_gauge 는 컬럼 DROP + config 화이트리스트(_AGENT_SETTABLE_FIELDS) 밖 →
update_roster_config 가 '설정할 수 없는 필드' 로 거절(과거의 명시 policy_lock 대체).
"""
from agents_v2.skills.update_constraint import update_constraint


def _params(preview):
    return {"group_id": "GRP001", "field": "preceptor_gauge", "value": 5, "preview_only": preview}


def test_preceptor_gauge_blocked_apply(db, seed_data):
    res = update_constraint(db, _params(preview=False))
    assert res.get("error") and res.get("preview") is not True, res


def test_preceptor_gauge_blocked_preview(db, seed_data):
    # 미리보기조차 안 만들어짐(비허용 필드)
    res = update_constraint(db, _params(preview=True))
    assert res.get("error") and res.get("preview") is not True, res


def test_settable_field_still_works(db, seed_data):
    # 대조군: 화이트리스트 필드(연속근무)는 여전히 통과(과잉차단 아님)
    res = update_constraint(db, {"group_id": "GRP001", "field": "max_conseq_work",
                                 "value": 5, "preview_only": True})
    assert not res.get("error") and res.get("preview") is True, res
