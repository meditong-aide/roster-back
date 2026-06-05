"""친화 라벨링 검증 — raw config_key / direction 이 사용자 한국어 라벨로 변환.

요구 (2026-05-17):
  - dashboard 의 해결책 column 에서 `two_offs_after_two_nig → disable` 같은
    raw setting 키 노출 X
  - `config_key_label_ko` / `direction_label_ko` 가 모든 treatment 직렬화에 포함
"""

from __future__ import annotations

from services.cause_treatment_hitter import propose_bundles
from services.precheck.payload import build_unrecoverable_payload
from services.semantics.ontology import (
    CONFIG_KEY_LABELS_KO,
    DIRECTION_LABELS_KO,
    friendly_config_key_label,
    friendly_direction_label,
)


def test_friendly_config_key_known() -> None:
    assert friendly_config_key_label("two_offs_after_two_nig") == "2N 후 2일 OFF 규칙"
    assert friendly_config_key_label("max_night_shifts_per_month") == "월 야간 한도"
    assert friendly_config_key_label("_force_grade_max_soft_fallback") == "등급 상한 soft 전환"
    assert friendly_config_key_label("team_min_by_team") == "팀 최소 인원"


def test_friendly_config_key_unknown_passthrough() -> None:
    """미정의 키는 raw 그대로 반환 (시스템이 깨지지 않게)."""
    assert friendly_config_key_label("totally_unknown_key") == "totally_unknown_key"
    assert friendly_config_key_label(None) is None
    assert friendly_config_key_label("") is None


def test_friendly_direction_known() -> None:
    assert friendly_direction_label("disable") == "비활성화"
    assert friendly_direction_label("increase") == "상향"
    assert friendly_direction_label("decrease") == "하향"
    assert friendly_direction_label("manual") == "수동 점검"


def test_propose_bundles_treatments_include_labels() -> None:
    bundles = propose_bundles(
        active_causes=["cause:capacity:monthly_night_shortage"],
        max_alternatives=1,
    )
    assert bundles, "no bundle proposed"
    for t in bundles[0].treatments:
        if t.config_key:
            assert t.config_key_label_ko, (
                f"treatment {t.treatment_id} config_key={t.config_key} 의 친화 라벨 누락"
            )
        if t.direction:
            assert t.direction_label_ko, (
                f"treatment {t.treatment_id} direction={t.direction} 의 친화 라벨 누락"
            )


def test_payload_treatments_include_friendly_labels() -> None:
    violated = [
        {"reason_code": "MONTHLY_NIGHT_CAPACITY_SHORTAGE",
         "node_id": "cause:capacity:monthly_night_shortage",
         "details": {"n_required": 84, "n_capacity": 60}},
        {"reason_code": "TEAM_MIN_EXCEEDS_GLOBAL_NEED",
         "node_id": "cause:team:min_over_need",
         "details": {"day": 5, "shift": "D", "min_sum": 6, "required": 4}},
    ]
    payload = build_unrecoverable_payload(
        precheck_result={"issues": []},
        applied_relaxations=[],
        last_error_reason="friendly label test",
        violated_constraints=violated,
        conflict_cores=[],
        pool_snapshot={},
    )
    trs = payload["infeasibility"]["treatment_recommendations"]
    assert len(trs) >= 1
    for bundle in trs:
        for t in bundle["treatments"]:
            assert "config_key_label_ko" in t
            assert "direction_label_ko" in t
            # raw config_key 가 있는 treatment 는 라벨도 있어야 함
            if t.get("config_key"):
                assert t["config_key_label_ko"], (
                    f"treatment {t['treatment_id']} 의 config_key={t['config_key']} 친화 라벨 누락"
                )


def test_narrative_action_levers_include_friendly_labels() -> None:
    violated = [
        {"reason_code": "MONTHLY_NIGHT_CAPACITY_SHORTAGE",
         "node_id": "cause:capacity:monthly_night_shortage",
         "details": {"n_required": 84, "n_capacity": 60}},
    ]
    payload = build_unrecoverable_payload(
        precheck_result={"issues": []},
        applied_relaxations=[],
        last_error_reason="narrative friendly label test",
        violated_constraints=violated,
        conflict_cores=[],
        pool_snapshot={},
    )
    narr = payload["infeasibility"]["resolution_narrative"]
    assert narr is not None
    for a in narr["action_levers"]:
        assert "config_key_label_ko" in a
        assert "direction_label_ko" in a


def test_all_yaml_config_keys_have_label() -> None:
    """ontology.yaml 의 treatments 에 정의된 모든 config_key 가 라벨 사전에 등재.

    이 테스트가 깨지면 새 treatment 추가 시 라벨 매핑도 함께 추가하도록 가이드.
    """
    from services.semantics.ontology import get_default_ontology
    onto = get_default_ontology()
    missing = []
    for tid, t in onto.treatments.items():
        if t.config_key and t.config_key not in CONFIG_KEY_LABELS_KO:
            missing.append(f"{tid} → {t.config_key}")
    assert not missing, f"라벨 사전 누락: {missing}"


def test_all_directions_have_label() -> None:
    """ontology.yaml 의 treatments 에 정의된 모든 direction 이 라벨 사전에 등재."""
    from services.semantics.ontology import get_default_ontology
    onto = get_default_ontology()
    missing = set()
    for t in onto.treatments.values():
        if t.direction and t.direction not in DIRECTION_LABELS_KO:
            missing.add(t.direction)
    assert not missing, f"direction 라벨 사전 누락: {missing}"
