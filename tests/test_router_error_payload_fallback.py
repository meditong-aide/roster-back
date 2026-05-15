from routers.roster_create import _fallback_unrecoverable_from_exception


def test_router_fallback_emits_internal_error_when_no_signals():
    payload = _fallback_unrecoverable_from_exception("근무표 생성 실패: unknown")
    inf = payload["infeasibility"]
    codes = [v.get("reason_code") for v in (inf.get("violated_constraints") or [])]
    assert "INTERNAL_GENERATION_ERROR" in codes


def test_router_fallback_emits_no_assignment_fixed_and_capacity_signals():
    payload = _fallback_unrecoverable_from_exception(
        "근무표 생성 실패: [reason_code=NO_ASSIGNMENT] FIXED conflict and CAPACITY shortage"
    )
    inf = payload["infeasibility"]
    codes = {v.get("reason_code") for v in (inf.get("violated_constraints") or [])}
    assert "NO_ASSIGNMENT" in codes
    assert "NO_ASSIGNMENT_FIXED" in codes
    assert "NO_ASSIGNMENT_CAPACITY" in codes
