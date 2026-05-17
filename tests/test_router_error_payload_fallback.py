"""Router exception → infeasibility payload 변환 검증.

정책 (U-1, 2026-05-17 ralph): NO_ASSIGNMENT* 4 축 라벨은 cause 가 아닌 symptom 신호다.
  - violated_constraints (legacy 신호 컨테이너) 에는 message 에서 추출한
    원본 reason_code 가 남아도 OK (호환).
  - 그러나 payload.causes[] 에는 절대 NO_ASSIGNMENT* 라벨 진입 금지
    (split_violations 가 symptom-bucket 으로 라우팅).
  - 라우터 fallback 은 더 이상 message 텍스트 분석으로 NO_ASSIGNMENT_FIXED /
    NO_ASSIGNMENT_CAPACITY 같은 합성 라벨을 fabricate 하지 않는다 — 진짜 cause 는
    산술 detector / MUS inferer 가 정확한 cause_id 로 발급한다.
"""

from routers.roster_create import _fallback_unrecoverable_from_exception


def test_router_fallback_emits_internal_error_when_no_signals():
    payload = _fallback_unrecoverable_from_exception("근무표 생성 실패: unknown")
    inf = payload["infeasibility"]
    codes = [v.get("reason_code") for v in (inf.get("violated_constraints") or [])]
    assert "INTERNAL_GENERATION_ERROR" in codes


def test_router_fallback_extracts_explicit_reason_codes_from_message():
    """message 내 [reason_code=X] 패턴은 violated_constraints 에 그대로 추출."""
    payload = _fallback_unrecoverable_from_exception(
        "근무표 생성 실패: [reason_code=NO_ASSIGNMENT] FIXED conflict and CAPACITY shortage"
    )
    inf = payload["infeasibility"]
    vc_codes = {v.get("reason_code") for v in (inf.get("violated_constraints") or [])}
    # 신호 자체는 violated_constraints 에 남음 (호환).
    assert "NO_ASSIGNMENT" in vc_codes
    # 그러나 더 이상 합성 라벨 fabricate 하지 않는다 (텍스트 키워드 추론 X).
    assert "NO_ASSIGNMENT_FIXED" not in vc_codes
    assert "NO_ASSIGNMENT_CAPACITY" not in vc_codes


def test_router_fallback_does_not_leak_no_assignment_into_causes():
    """U-1 invariant: NO_ASSIGNMENT* 절대 causes[] 진입 금지."""
    payload = _fallback_unrecoverable_from_exception(
        "근무표 생성 실패: [reason_code=NO_ASSIGNMENT] something broken"
    )
    inf = payload["infeasibility"]
    cause_codes = {c.get("reason_code") for c in (inf.get("causes") or [])}
    for forbidden in (
        "NO_ASSIGNMENT", "NO_ASSIGNMENT_CAPACITY", "NO_ASSIGNMENT_FIXED",
        "NO_ASSIGNMENT_ELIGIBILITY", "NO_ASSIGNMENT_CARRYOVER", "DAY_ZERO_COVERAGE",
    ):
        assert forbidden not in cause_codes, (
            f"{forbidden} leaked into payload.infeasibility.causes — violates U-1 policy."
        )
