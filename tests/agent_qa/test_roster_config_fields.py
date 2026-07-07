"""B: RosterConfig 읽기 완전성 + 신규 문서화 필드 update 검증."""

from __future__ import annotations

from agents_v2.skills.descriptions import SKILL_TOOLS
from agents_v2.skills.update_constraint import update_constraint
from agents_v2.tools import constraint_tools


def _desc(name: str) -> str:
    for t in SKILL_TOOLS:
        if t["name"] == name:
            return t["description"]
    raise AssertionError(f"{name} not in SKILL_TOOLS")


# ── read 완전성 — 이전에 _config_dict 가 누락하던 4 필드 ──


def test_get_roster_config_includes_previously_missing_fields(db, seed_data):
    cfg = constraint_tools.get_roster_config(db, "GRP001")
    for field in ("patient_amount", "ban_night_before_fixed_off", "off_first", "off_swap_enabled"):
        assert field in cfg, f"{field} 가 조회 결과에 없음"
    assert cfg["patient_amount"] == 30  # seed 값


def test_get_roster_config_covers_all_model_columns(db, seed_data):
    """_config_dict 가 RosterConfig 의 의미있는 컬럼을 모두 노출하는지 (메타/관계 제외)."""
    from db.models import RosterConfig

    cfg = constraint_tools.get_roster_config(db, "GRP001")
    # updated_at 은 created_at 과 같은 메타 타임스탬프 → 노출 제외.
    skip = {"created_at", "updated_at", "config", "group", "office"}
    model_cols = {c.name for c in RosterConfig.__table__.columns} - skip
    missing = model_cols - set(cfg.keys())
    assert not missing, f"조회에서 누락된 컬럼: {missing}"


# ── description 보강 — 신규 정책 필드 ──


def test_description_documents_new_policy_fields():
    desc = _desc("update_constraint")
    for field in ("min_exp_per_shift", "req_exp_nurses", "not_one_night",
                  "nod_noe", "ban_night_before_fixed_off", "preceptee_shift_count"):
        assert field in desc, f"{field} 가 update_constraint 설명에 없음"


def test_description_excludes_display_and_opaque_fields():
    """표시/불투명 필드는 의도적으로 미문서화 (에이전트 튜닝 대상 아님)."""
    desc = _desc("update_constraint")
    for field in ("show_level", "show_preceptor", "off_first", "team_balance_mode"):
        assert field not in desc, f"{field} 는 설명에 노출되면 안 됨"


# ── update — 신규 문서화 필드 적용 ──


def test_update_min_exp_per_shift_apply(db, seed_data):
    res = update_constraint(db, {
        "group_id": "GRP001", "field": "min_exp_per_shift", "value": 3,
        "preview_only": False,
    })
    assert res["preview"] is False
    assert res["changes"]["min_exp_per_shift"]["new"] == 3
    assert constraint_tools.get_roster_config(db, "GRP001")["min_exp_per_shift"] == 3


def test_update_not_one_night_preview_then_apply(db, seed_data):
    # preview — DB 미반영
    prev = update_constraint(db, {
        "group_id": "GRP001", "field": "not_one_night", "value": True,
        "preview_only": True,
    })
    assert prev["preview"] is True
    assert constraint_tools.get_roster_config(db, "GRP001")["not_one_night"] is False
    # apply
    update_constraint(db, {
        "group_id": "GRP001", "field": "not_one_night", "value": True,
        "preview_only": False,
    })
    assert constraint_tools.get_roster_config(db, "GRP001")["not_one_night"] is True


def test_update_ban_night_before_fixed_off_apply(db, seed_data):
    update_constraint(db, {
        "group_id": "GRP001", "field": "ban_night_before_fixed_off", "value": False,
        "preview_only": False,
    })
    assert constraint_tools.get_roster_config(db, "GRP001")["ban_night_before_fixed_off"] is False
