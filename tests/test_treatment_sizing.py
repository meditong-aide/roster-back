"""Treatment magnitude sizing — precheck 산술 숫자로 실효 목표값 계산.

precheck-block(단일축 산술) 케이스는 ontology treatment 가 방향("상향")만 알아
"+1" 고정 제안을 냈다. 이 모듈은 cause.details(=issue evidence)의 정확한 숫자
(요구/가용인원별 capacity)로 최소 실효값을 계산해 resolution_option 에
suggested_value + apply(직접적용 델타)로 승격하는지 검증한다.

핵심: treatment.covers 는 ontology 정식 cause_id, precheck cause 는 reason_code
alias(node_id=None) — enricher 가 resolve_cause_alias 로 둘을 매칭해야 한다.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))

from services.precheck.treatment_enricher import (  # noqa: E402
    _needed_max_nig,
    _size_from_cause_details,
    enrich_treatment_recommendations,
)
from services.cp_sat.undiagnosed_probe import (  # noqa: E402
    treatments_to_resolution_options,
)


def test_needed_max_nig_exact():
    # 23명 각 working_cap 21, 월 요구 180 → 최소 m: 23*m >= 180 → m=8 (184>=180)
    caps = [21] * 23
    assert _needed_max_nig(180, caps) == 8
    assert sum(min(c, 8) for c in caps) >= 180
    assert sum(min(c, 7) for c in caps) < 180


def test_needed_max_nig_insufficient():
    # 야간 가능 5명만 → 상한 최대(21)로도 105 < 180 → None(증원 필요)
    assert _needed_max_nig(180, [21] * 5) is None


def test_size_from_cause_details_night():
    details = {
        "n_required": 180,
        "night_capable_nurses": [
            {"nurse_id": str(i), "capacity_days": 21} for i in range(23)
        ],
    }
    sized = _size_from_cause_details("max_nig_per_month", details)
    assert sized["target_value"] == 8
    assert not sized["insufficient"]


def test_size_unsupported_key_returns_none():
    assert _size_from_cause_details("off_days", {"n_required": 180}) is None


def test_alias_matching_and_apply_promotion():
    # cause 는 reason_code alias(node_id=None), treatment.covers 는 정식 cause_id.
    # enricher 가 resolve_cause_alias 로 매칭해야 suggested_value 가 붙는다.
    details = {
        "n_required": 180,
        "night_capable_nurses": [
            {"nurse_id": str(i), "capacity_days": 21} for i in range(23)
        ],
    }
    causes = [{
        "node_id": None,
        "reason_code": "MONTHLY_NIGHT_CAPACITY_SHORTAGE",
        "details": details,
    }]
    trec = [{
        "bundle_id": "bundle:treatment:threshold:monthly_night_cap",
        "treatments": [{
            "treatment_id": "treatment:threshold:monthly_night_cap",
            "config_key": "max_nig_per_month",
            "config_key_label_ko": "월 야간 한도(개인)",
            "direction": "상향", "direction_label_ko": "상향",
            "action_type": "set_threshold",
            "covers": ["cause:capacity:monthly_night_shortage"],
            "rationale_ko": "개인당 월 최대 야간 +1",
            "trade_off_ko": "야간 부담 증가",
        }],
    }]
    enrich_treatment_recommendations(trec, causes)
    assert trec[0]["treatments"][0]["suggested_value"] == 8

    opts = treatments_to_resolution_options(trec)
    assert len(opts) == 1
    ch = opts[0]["changes"][0]
    assert ch["suggested_value"] == 8
    assert opts[0]["apply"] == {"max_nig_per_month": 8}


def test_daily_shift_requirements_message_only():
    # 일자 총수요 > 가용 → 수요는 nested/DailyShift 출처라 자동 apply 없이 숫자만 안내.
    details = {"day": 8, "required_total": 30, "available_nurses": 28}
    sized = _size_from_cause_details("daily_shift_requirements", details)
    assert sized["target_value"] is None
    assert "2명 감축" in sized["reason_ko"]  # 30-28=2

    causes = [{"node_id": None, "reason_code": "GLOBAL_DAY_CAPACITY_SHORTAGE",
               "details": details}]
    trec = [{
        "bundle_id": "bundle:treatment:threshold:daily_shift",
        "treatments": [{
            "treatment_id": "treatment:threshold:daily_shift",
            "config_key": "daily_shift_requirements", "config_key_label_ko": "일별 시프트 요구",
            "direction": "감소", "direction_label_ko": "감소",
            "action_type": "set_threshold",
            "covers": ["cause:capacity:daily_total_shortage"],
            "rationale_ko": "일별 시프트 요구 감소",
        }],
    }]
    enrich_treatment_recommendations(trec, causes)
    opts = treatments_to_resolution_options(trec)
    ch = opts[0]["changes"][0]
    assert "sizing_ko" in ch and "감축" in ch["sizing_ko"]
    assert opts[0]["apply"] == {}          # nested → 자동 apply 없음
    assert "suggested_value" not in ch     # 스칼라 값 미제시


def test_boolean_treatment_no_sizing():
    # disable 형(boolean) treatment 는 사이징 대상 아님 — apply 비고, 방향만.
    causes = [{"node_id": None, "reason_code": "MONTHLY_NIGHT_CAPACITY_SHORTAGE",
               "details": {"n_required": 180}}]
    trec = [{
        "bundle_id": "bundle:treatment:disable:night_recovery",
        "treatments": [{
            "treatment_id": "treatment:disable:night_recovery",
            "config_key": "two_offs_after_two_nig", "config_key_label_ko": "2N 후 2일 OFF",
            "direction": "비활성화", "direction_label_ko": "비활성화",
            "action_type": "disable_module",
            "covers": ["cause:capacity:monthly_night_shortage"],
            "rationale_ko": "회복 규칙 비활성",
        }],
    }]
    enrich_treatment_recommendations(trec, causes)
    opts = treatments_to_resolution_options(trec)
    assert opts[0]["apply"] == {}
    assert "suggested_value" not in opts[0]["changes"][0]
