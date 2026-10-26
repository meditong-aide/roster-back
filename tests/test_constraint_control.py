"""Unit tests for constraint_impact.control adjustment apply layer.

Focus: apply_adjustments_to_config 가 ontology 매핑표대로 config_dict 를
정확히 변형하고, 잘못된 family/action 입력 시 ValueError 를 일으키는지.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "app"
for p in (str(ROOT), str(APP)):
    if p not in sys.path:
        sys.path.insert(0, p)

from services.constraint_impact.control import (  # noqa: E402
    ConstraintAdjustment,
    apply_adjustments_to_config,
    supported_actions_for,
)


def _baseline_config() -> dict:
    return {
        "team_min_by_team": {"T1": {"D": 2, "N": 1}, "T2": {"D": 1}},
        "team_min_soft_fallback": False,
        "team_handoff_soft_fallback": False,
        "grade_config": {
            "constraints": {"D": {"1": 2}, "N": {"1": 1}},
            "constraints_json": {"D": {"1": 2}, "N": {"1": 1}},
            "allow_soft_fallback": False,
        },
        "ban_n_to_d": True,
        "ban_e_to_d": True,
        "ban_n_to_e": True,
        "max_conseq_work": 5,
        "three_seq_nig": True,
        "two_offs_after_two_nig": True,
        "two_offs_after_three_nig": True,
        "daily_shift_requirements": {"D": 5, "E": 3, "N": 2},
        "daily_shift_requirements_by_day": [
            {"D": 5, "E": 3, "N": 2},
            {"D": 5, "E": 3, "N": 2},
            {"D": 5, "E": 3, "N": 2},
        ],
    }


def test_team_min_disable_module_clears_team_min():
    cfg = _baseline_config()
    out = apply_adjustments_to_config(
        cfg,
        [ConstraintAdjustment(family="TeamMin", action="disable_module")],
    )
    assert out["team_min_by_team"] == {}
    assert cfg["team_min_by_team"]  # original untouched (deepcopy)
    assert out["_applied_constraint_adjustments"][0]["family"] == "TeamMin"
    assert out["_applied_constraint_adjustments"][0]["action"] == "disable_module"


def test_team_min_force_soft_mode_sets_flag():
    out = apply_adjustments_to_config(
        _baseline_config(),
        [ConstraintAdjustment(family="TeamMin", action="force_soft_mode")],
    )
    assert out["team_min_soft_fallback"] is True


def test_team_min_set_threshold_overrides_value():
    out = apply_adjustments_to_config(
        _baseline_config(),
        [
            ConstraintAdjustment(
                family="TeamMin",
                action="set_threshold",
                scope_filter={"team": "T1", "shift": "D"},
                target_value=1,
            )
        ],
    )
    assert out["team_min_by_team"]["T1"]["D"] == 1
    assert out["team_min_by_team"]["T1"]["N"] == 1
    assert out["team_min_by_team"]["T2"]["D"] == 1


def test_team_min_narrow_scope_drops_team():
    out = apply_adjustments_to_config(
        _baseline_config(),
        [
            ConstraintAdjustment(
                family="TeamMin", action="narrow_scope", scope_filter={"team": "T1"}
            )
        ],
    )
    assert "T1" not in out["team_min_by_team"]
    assert "T2" in out["team_min_by_team"]


def test_grade_max_force_soft_sets_force_flag():
    out = apply_adjustments_to_config(
        _baseline_config(),
        [ConstraintAdjustment(family="GradeMax", action="force_soft_mode")],
    )
    assert out["_force_grade_max_soft_fallback"] is True


def test_grade_min_force_soft_is_unsupported_and_changes_nothing():
    """EI-11 11f — GradeMin force_soft 는 매트릭스에서 빠졌다(엔진·생성이 읽지 않는 죽은 쓰기였다). 요청하면
    'unsupported adjustment' 이고 설정은 그대로다. 핸들러도 GradeMin force_soft 를 받지 않는다(쓰기 0)."""
    from services.constraint_impact.control import _apply_grade

    cfg = _baseline_config()
    with pytest.raises(ValueError, match="unsupported adjustment"):
        apply_adjustments_to_config(cfg, [ConstraintAdjustment(family="GradeMin", action="force_soft_mode")])
    assert cfg == _baseline_config()
    assert supported_actions_for("GradeMin") == ["disable_module"]
    assert supported_actions_for("GradeMax") == ["disable_module", "force_soft_mode"]
    direct = _baseline_config()
    with pytest.raises(ValueError, match="unsupported GradeMin action: force_soft_mode"):
        _apply_grade("GradeMin", direct, ConstraintAdjustment(family="GradeMin", action="force_soft_mode"))
    assert direct == _baseline_config()


def test_grade_disable_module_clears_constraints():
    out = apply_adjustments_to_config(
        _baseline_config(),
        [ConstraintAdjustment(family="GradeMin", action="disable_module")],
    )
    assert out["grade_config"]["constraints"] == {}
    assert out["grade_config"]["constraints_json"] == {}


def test_boundary_transition_disable_module_clears_bans():
    out = apply_adjustments_to_config(
        _baseline_config(),
        [ConstraintAdjustment(family="BoundaryTransitionBan", action="disable_module")],
    )
    assert out["ban_n_to_d"] is False
    assert out["ban_e_to_d"] is False
    assert out["ban_n_to_e"] is False


def test_consecutive_work_set_threshold():
    out = apply_adjustments_to_config(
        _baseline_config(),
        [
            ConstraintAdjustment(
                family="ConsecutiveWorkLimit", action="set_threshold", target_value=7
            )
        ],
    )
    assert out["max_conseq_work"] == 7


def test_night_recovery_disable_module():
    out = apply_adjustments_to_config(
        _baseline_config(),
        [ConstraintAdjustment(family="NightRecovery", action="disable_module")],
    )
    assert out["two_offs_after_two_nig"] is False
    assert out["two_offs_after_three_nig"] is False


def test_coverage_min_set_threshold_per_day():
    out = apply_adjustments_to_config(
        _baseline_config(),
        [
            ConstraintAdjustment(
                family="CoverageMin",
                action="set_threshold",
                scope_filter={"day": 2, "shift": "N"},
                target_value=1,
            )
        ],
    )
    assert out["daily_shift_requirements_by_day"][1]["N"] == 1


def test_unknown_family_raises():
    with pytest.raises(ValueError, match="unknown ontology family"):
        apply_adjustments_to_config(
            _baseline_config(),
            [ConstraintAdjustment(family="ImaginaryFamily", action="disable_module")],
        )


def test_unsupported_action_raises():
    with pytest.raises(ValueError, match="unsupported action"):
        apply_adjustments_to_config(
            _baseline_config(),
            [ConstraintAdjustment(family="TeamMin", action="rocket_science")],
        )


def test_unsupported_family_action_pair_raises():
    with pytest.raises(ValueError, match="unsupported adjustment"):
        apply_adjustments_to_config(
            _baseline_config(),
            [ConstraintAdjustment(family="GradeMin", action="set_threshold", target_value=1)],
        )


def test_dict_input_normalizes():
    out = apply_adjustments_to_config(
        _baseline_config(),
        [{"family": "TeamMin", "action": "disable_module", "reason": "test"}],
    )
    assert out["team_min_by_team"] == {}
    assert out["_applied_constraint_adjustments"][0]["reason"] == "test"


def test_alias_resolution_works():
    # team_min ontology aliases include TEAM_MIN_EXCEEDS_GLOBAL_NEED
    out = apply_adjustments_to_config(
        _baseline_config(),
        [
            ConstraintAdjustment(
                family="TEAM_MIN_EXCEEDS_GLOBAL_NEED", action="disable_module"
            )
        ],
    )
    assert out["team_min_by_team"] == {}


def test_supported_actions_for_team_min_returns_all_four():
    actions = supported_actions_for("TeamMin")
    assert set(actions) == {"disable_module", "force_soft_mode", "set_threshold", "narrow_scope"}


def test_ban_night_before_fixed_off_disable_module_sets_flag():
    out = apply_adjustments_to_config(
        _baseline_config(),
        [ConstraintAdjustment(family="BanNightBeforeFixedOff", action="disable_module")],
    )
    assert out["ban_night_before_fixed_off"] is False


def test_weekend_off_only_disable_module_sets_flag():
    out = apply_adjustments_to_config(
        _baseline_config(),
        [ConstraintAdjustment(family="WeekendOffOnly", action="disable_module")],
    )
    assert out["weekend_off_only_enable"] is False


def test_monthly_night_cap_set_threshold_writes_max_night():
    out = apply_adjustments_to_config(
        _baseline_config(),
        [
            ConstraintAdjustment(
                family="MonthlyNightCap", action="set_threshold", target_value=8
            )
        ],
    )
    assert out["max_nig_per_month"] == 8


def test_monthly_night_cap_alias_resolves_to_family():
    out = apply_adjustments_to_config(
        _baseline_config(),
        [
            ConstraintAdjustment(
                family="MONTHLY_NIGHT_CAPACITY_SHORTAGE",
                action="set_threshold",
                target_value=10,
            )
        ],
    )
    assert out["max_nig_per_month"] == 10
    assert out["_applied_constraint_adjustments"][0]["family"] == "MonthlyNightCap"


def test_monthly_night_cap_rejects_negative_threshold():
    import pytest as _pytest
    with _pytest.raises(ValueError, match="non-negative int"):
        apply_adjustments_to_config(
            _baseline_config(),
            [
                ConstraintAdjustment(
                    family="MonthlyNightCap", action="set_threshold", target_value=-1
                )
            ],
        )


def test_multiple_adjustments_apply_in_order():
    out = apply_adjustments_to_config(
        _baseline_config(),
        [
            ConstraintAdjustment(
                family="TeamMin",
                action="set_threshold",
                scope_filter={"team": "T1", "shift": "D"},
                target_value=1,
            ),
            ConstraintAdjustment(family="TeamMin", action="disable_module"),
        ],
    )
    # Last write wins; disable_module clears entire map
    assert out["team_min_by_team"] == {}
    assert len(out["_applied_constraint_adjustments"]) == 2


# ── EI-11 11f — GradeMin force_soft 죽은 쓰기·`treatment:soft:grade_min` 제거 ─────────────────────────

_DEAD_GRADE_MIN_TOKENS = ("_force_grade_min_soft_fallback", "treatment:soft:grade_min")
#: 남겨 두는 참조 2곳(지시서 §2.7 — 값이 안 생겨 무해): 라벨 사전(`semantics/ontology.py:114` — 에이전트 브랜치 파일)과
#: 미리보기 watched(`routers/constraint_impact.py:243` — 라우터 0줄). 줄 번호 대신 줄 내용으로 고정한다.
_DEAD_GRADE_MIN_ALLOWED = {
    "app/routers/constraint_impact.py": ['"_force_grade_max_soft_fallback", "_force_grade_min_soft_fallback",'],
    "app/services/semantics/ontology.py": ['"_force_grade_min_soft_fallback": "등급 최저 soft 전환",'],
}


def test_grade_min_soft_dead_write_has_no_references_left_in_app():
    """app 전체(코드·yaml·문서·정적 파일)에 GradeMin soft 키·처방 id 참조가 남겨 두는 2곳 말고는 없다."""
    found: dict[str, list[str]] = {}
    for path in sorted(APP.rglob("*")):
        if not path.is_file() or "__pycache__" in path.parts:
            continue
        data = path.read_bytes()
        if not any(tok.encode() in data for tok in _DEAD_GRADE_MIN_TOKENS):
            continue
        found[path.relative_to(ROOT).as_posix()] = [
            line.strip() for line in data.decode("utf-8", errors="replace").splitlines()
            if any(tok in line for tok in _DEAD_GRADE_MIN_TOKENS)]
    assert found == _DEAD_GRADE_MIN_ALLOWED


def test_grade_min_mode_estimate_reads_only_grade_config_soft_flag():
    """모듈 목록의 GradeMin 모드 추정은 `grade_config.allow_soft_fallback` 만 본다 — 지운 키가 config 에 남아 있어도
    soft 로 보지 않는다. GradeMax 는 지금처럼 자기 키(`_force_grade_max_soft_fallback`)도 본다."""
    from types import SimpleNamespace

    from services.constraint_impact.control import _module_mode_estimate

    gc = {"constraints": {"D": {"1": 1}}, "allow_soft_fallback": False}
    stale = SimpleNamespace(config_payload={"grade_config": gc, "_force_grade_min_soft_fallback": True})
    assert _module_mode_estimate("GradeMin", stale) == "enforced"
    soft = SimpleNamespace(config_payload={"grade_config": {**gc, "allow_soft_fallback": True}})
    assert _module_mode_estimate("GradeMin", soft) == "soft_fallback"
    forced_max = SimpleNamespace(config_payload={"grade_config": gc, "_force_grade_max_soft_fallback": True})
    assert _module_mode_estimate("GradeMax", forced_max) == "soft_fallback"


def test_consecutive_limit_mode_estimates_read_canonical_keys():
    """12r — 모듈 목록의 연속근무·연속나이트 모드 추정은 정본 키만 읽는다(별칭만 있으면 inactive, 연속나이트는 늘 enforced)."""
    from types import SimpleNamespace

    from services.constraint_impact.control import _module_mode_estimate

    on = SimpleNamespace(config_payload={"max_conseq_work": 5})
    assert _module_mode_estimate("ConsecutiveWorkLimit", on) == "enforced"
    for payload in ({"max_conseq_work": 0}, {"max_conseq_work": None}, {"max_consecutive_work_days": 5}):
        assert _module_mode_estimate("ConsecutiveWorkLimit", SimpleNamespace(config_payload=payload)) == "inactive"
    assert _module_mode_estimate("ConsecutiveNightLimit", SimpleNamespace(config_payload={})) == "enforced"


@pytest.mark.parametrize("issue", [
    {"reason_code": "GRADE_MIN_SUM_EXCEEDS_NEED", "severity": "hard",
     "evidence": {"shift": "D", "day": 3, "min_sum": 4, "need": 3}},
    {"reason_code": "GRADE_MIN_AVAILABLE_SHORTAGE", "severity": "hard",
     "evidence": {"shift": "N", "day": 5, "grade": 1, "required": 2, "available": 1}},
], ids=["min_sum_exceeds_need", "min_available_shortage"])
def test_grade_min_blocking_payload_has_no_soft_card_only_manual_fix_config(issue):
    """등급 최소 원인(`cause:grade:min_sum_over_need` — 별칭 2개) 차단 payload 에 GradeMin soft 카드가 없고, 그 원인의
    처방은 수동 `treatment:data:fix_config` 하나다(Q11 — fix_config 연결). 카드가 0장이라 모달은 '조정 제안'
    (`fix_suggestions_ko` — 설정 > 직급별 최소 인원 안내)을 띄운다."""
    import json

    from services.precheck.payload import build_blocking_payload
    from services.semantics.ontology import get_default_ontology

    onto = get_default_ontology()
    assert onto.get_treatment("treatment:soft:grade_min") is None
    assert [t.treatment_id for t in onto.treatments_for_cause("cause:grade:min_sum_over_need")] == [
        "treatment:data:fix_config"]
    inf = build_blocking_payload({"issues": [dict(issue)]})["infeasibility"]
    assert inf["resolution_options"] == []
    treatments = [t for b in inf["treatment_recommendations"] for t in b["treatments"]]
    assert [(t["treatment_id"], t["action_type"]) for t in treatments] == [
        ("treatment:data:fix_config", "data_correction_required")]
    assert [c["reason_code"] for c in inf["causes"]] == [issue["reason_code"]]
    assert any("설정 > 직급별 최소 인원" in s for s in inf["fix_suggestions_ko"])
    text = json.dumps(inf, ensure_ascii=False, default=str)
    assert not any(s in text for s in ("soft:grade_min", "_force_grade_min_soft_fallback", "등급 최저 soft 전환"))


# ── EI-12 12s 결정 A — 미리보기 diff 가 정본 키를 잡는다 ──────────────────────────────────────────

def _preview_diff(monkeypatch, adjustments: list[dict], payload: dict) -> dict:
    from types import SimpleNamespace

    from routers import constraint_impact as router

    monkeypatch.setattr(router, "_build_listing_snapshot", lambda *a, **k: SimpleNamespace(config_payload=dict(payload)))
    monkeypatch.setattr(router, "resolve_home_group_id", lambda *a, **k: 1)
    body = router._AdjustmentPreviewBody(year=2026, month=11, constraint_adjustments=adjustments)
    return router.preview_adjustments(body, current_user=SimpleNamespace(office_id=1), db=None)


_PREVIEW_PAYLOAD = {"max_conseq_work": 4, "max_nig_per_month": 8, "three_seq_nig": True}


def test_preview_diff_catches_canonical_consecutive_work_key(monkeypatch):
    out = _preview_diff(monkeypatch, [{"family": "ConsecutiveWorkLimit", "action": "set_threshold", "target_value": 5}], _PREVIEW_PAYLOAD)
    assert out["diff"] == {"max_conseq_work": {"before": 4, "after": 5}} and out["diff_count"] == 1


def test_preview_diff_catches_canonical_monthly_night_key(monkeypatch):
    out = _preview_diff(monkeypatch, [{"family": "MonthlyNightCap", "action": "set_threshold", "target_value": 10}], _PREVIEW_PAYLOAD)
    assert out["diff"] == {"max_nig_per_month": {"before": 8, "after": 10}}


def test_preview_diff_is_empty_for_consecutive_night_adjustment(monkeypatch):
    """D13 — 연속 나이트 조정은 '적용 불가'라 설정이 안 바뀐다: diff 없음, applied 에는 기록이 남는다."""
    out = _preview_diff(monkeypatch, [{"family": "ConsecutiveNightLimit", "action": "set_threshold", "target_value": 2}], _PREVIEW_PAYLOAD)
    assert out["diff"] == {} and out["diff_count"] == 0
    assert out["applied"] and out["applied"][-1]["applicable"] is False


def test_preview_diff_catches_three_seq_nig_when_the_forbidden_mark_is_absent(monkeypatch):
    """12t — 레지스트리의 런타임 금지 표지(D13)가 없으면 연속 나이트 조정이 정본 bool `three_seq_nig` 로 접혀 쓰인다(2 → False).
    그때 미리보기 diff 가 그 키를 잡아야 한다 — `watched` 의 `three_seq_nig` 를 고정한다(지금은 표지가 있어 늘 '적용 불가'라 닿지 않는 칸)."""
    from services.policy import hard_lock

    monkeypatch.setattr(hard_lock, "runtime_forbidden_violation", lambda key: None)
    out = _preview_diff(monkeypatch, [{"family": "ConsecutiveNightLimit", "action": "set_threshold", "target_value": 2}], _PREVIEW_PAYLOAD)
    assert out["diff"] == {"three_seq_nig": {"before": True, "after": False}} and out["diff_count"] == 1
