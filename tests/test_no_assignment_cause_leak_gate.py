"""U-1 — NO_ASSIGNMENT 계열 라벨이 cause-bucket 으로 진입하지 못함을 검증.

규칙 (사용자 ralph 요구 "NO ASSIGNMENT같은게 원인으로 나오지 않게"):
  - NO_ASSIGNMENT, NO_ASSIGNMENT_CAPACITY, NO_ASSIGNMENT_ELIGIBILITY,
    NO_ASSIGNMENT_FIXED, NO_ASSIGNMENT_CARRYOVER, DAY_ZERO_COVERAGE 는
    cause 가 아닌 symptom 신호이므로 split_violations 의 causes[] 에 절대
    포함되지 않아야 한다.
  - violated_constraints (legacy 신호 컨테이너) 에는 남아도 OK.
"""

from __future__ import annotations

import pytest

from services.precheck.cause_symptom_classifier import (
    SYMPTOM_CODES,
    classify,
    split_violations,
)
from services.precheck.payload import (
    build_blocking_payload,
    build_unrecoverable_payload,
)


_LEAK_CODES = [
    "NO_ASSIGNMENT",
    "NO_ASSIGNMENT_CAPACITY",
    "NO_ASSIGNMENT_ELIGIBILITY",
    "NO_ASSIGNMENT_FIXED",
    "NO_ASSIGNMENT_CARRYOVER",
    "DAY_ZERO_COVERAGE",
]


@pytest.mark.parametrize("code", _LEAK_CODES)
def test_classify_returns_symptom_for_leak_codes(code: str) -> None:
    assert classify(code) == "symptom"
    assert code in SYMPTOM_CODES


@pytest.mark.parametrize("code", _LEAK_CODES)
def test_split_violations_routes_leak_codes_to_symptom_bucket(code: str) -> None:
    causes, symptoms, undiag = split_violations([
        {"reason_code": code, "details": {"source": "synthetic"}},
        {"reason_code": "CAPACITY_TOTAL_SHORTAGE", "details": {"source": "real_cause"}},
    ])
    cause_codes = {c.get("reason_code") for c in causes}
    symptom_codes = {s.get("reason_code") for s in symptoms}
    assert code not in cause_codes, f"{code} leaked into causes[]"
    assert code in symptom_codes, f"{code} should be in observed_symptoms[]"
    # 실제 cause 는 cause-bucket 으로 정상 이동
    assert "CAPACITY_TOTAL_SHORTAGE" in cause_codes


def test_build_unrecoverable_payload_does_not_leak_no_assignment_into_causes() -> None:
    violated = [
        {"reason_code": code, "details": {"source": "synthetic"},
         "human_message_ko": f"signal {code}"}
        for code in _LEAK_CODES
    ]
    payload = build_unrecoverable_payload(
        precheck_result={"issues": []},
        applied_relaxations=[],
        last_error_reason="synthetic test",
        violated_constraints=violated,
        conflict_cores=[],
        pool_snapshot={},
    )
    causes = payload["infeasibility"]["causes"] or []
    cause_codes = {c.get("reason_code") for c in causes}
    for code in _LEAK_CODES:
        assert code not in cause_codes, (
            f"{code} leaked into payload.infeasibility.causes — violates policy."
        )
    # 신호는 observed_symptoms 또는 violated_constraints 에 남음
    symptom_codes = {s.get("reason_code") for s in (payload["infeasibility"]["observed_symptoms"] or [])}
    # NO_ASSIGNMENT 같은 코드는 symptom-bucket 에 들어가야 함 (적어도 하나 이상)
    assert any(c in symptom_codes for c in _LEAK_CODES)


def test_build_blocking_payload_does_not_leak_no_assignment_into_causes() -> None:
    """precheck 가 직접 NO_ASSIGNMENT_* 발급하는 시나리오 — 차단되어야 함."""
    precheck_result = {
        "issues": [
            {
                "reason_code": "NO_ASSIGNMENT_FIXED",
                "severity": "blocking",
                "evidence": {"source": "synthetic"},
                "human_message_ko": "no-assignment fixed",
            },
            {
                "reason_code": "CAPACITY_TOTAL_SHORTAGE",
                "severity": "blocking",
                "evidence": {"source": "real_cause"},
                "human_message_ko": "capacity shortage",
            },
        ]
    }
    payload = build_blocking_payload(precheck_result)
    causes = payload["infeasibility"]["causes"] or []
    cause_codes = {c.get("reason_code") for c in causes}
    assert "NO_ASSIGNMENT_FIXED" not in cause_codes
    assert "CAPACITY_TOTAL_SHORTAGE" in cause_codes
