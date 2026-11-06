"""RosterConfig 읽기 + 정책 필드 update 검증 (dev 머지 후 curated _config_dict 기준).

dev 변경: _config_dict 는 큐레이션된 부분집합(전 컬럼 아님), patient_amount·
ban_night_before_fixed_off 컬럼 DROP, config 쓰기는 _AGENT_SETTABLE_FIELDS 화이트리스트.
"""

from __future__ import annotations

from agents_v2.skills.descriptions import SKILL_TOOLS
from agents_v2.skills.update_constraint import update_constraint
from agents_v2.tools import constraint_tools
from agents_v2.tools.constraint_tools import _AGENT_SETTABLE_FIELDS


def _desc(name: str) -> str:
    for t in SKILL_TOOLS:
        if t["name"] == name:
            return t["description"]
    raise AssertionError(f"{name} not in SKILL_TOOLS")


# ── read — 핵심 정책 필드가 조회에 노출되는지 (curated 부분집합) ──


def test_get_roster_config_exposes_core_policy_fields(db, seed_data):
    cfg = constraint_tools.get_roster_config(db, "GRP001")
    for field in ("day_req", "max_conseq_work", "max_nig_per_month", "not_one_night",
                  "min_exp_per_shift", "off_days"):
        assert field in cfg, f"{field} 가 조회 결과에 없음"


# ── description 보강 — 설정 가능한 정책 필드 문서화 ──


def test_description_documents_settable_policy_fields():
    desc = _desc("update_constraint")
    for field in ("min_exp_per_shift", "req_exp_nurses", "not_one_night",
                  "nod_noe", "preceptee_shift_count"):
        assert field in desc, f"{field} 가 update_constraint 설명에 없음"


def test_description_excludes_dropped_and_display_fields():
    """DROP/비설정 필드는 설명에 노출되면 안 됨(에이전트가 시도→거절되는 오유도 방지)."""
    desc = _desc("update_constraint")
    for field in ("team_balance_enable", "team_balance_gauge", "team_balance_mode",
                  "ban_night_before_fixed_off", "preceptor_gauge"):
        assert field not in desc, f"{field} 는 DROP/비설정 필드 — 설명에 노출 금지"


# ── update — 화이트리스트 필드 적용 ──


def test_update_min_exp_per_shift_apply(db, seed_data):
    res = update_constraint(db, {
        "group_id": "GRP001", "field": "min_exp_per_shift", "value": 3,
        "preview_only": False,
    })
    assert res["preview"] is False
    assert res["changes"]["min_exp_per_shift"]["new"] == 3
    assert constraint_tools.get_roster_config(db, "GRP001")["min_exp_per_shift"] == 3


def test_update_not_one_night_preview_then_apply(db, seed_data):
    prev = update_constraint(db, {
        "group_id": "GRP001", "field": "not_one_night", "value": True,
        "preview_only": True,
    })
    assert prev["preview"] is True
    assert constraint_tools.get_roster_config(db, "GRP001")["not_one_night"] is False
    update_constraint(db, {
        "group_id": "GRP001", "field": "not_one_night", "value": True,
        "preview_only": False,
    })
    assert constraint_tools.get_roster_config(db, "GRP001")["not_one_night"] is True


def test_non_whitelisted_field_rejected(db, seed_data):
    # 화이트리스트 밖 필드(DROP된 것 포함)는 거절 + allowed 목록 반환.
    res = update_constraint(db, {
        "group_id": "GRP001", "field": "ban_night_before_fixed_off", "value": False,
        "preview_only": False,
    })
    assert res.get("error"), res
