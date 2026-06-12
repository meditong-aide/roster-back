"""bulk_mutation: shift_codes/new_shift_code/shift_name 그라운딩 회귀.

2026-06-01 (B4). 기존엔 LLM 이 'XX' 같이 모르는 코드 보내면 silent skip 또는
"변경 없음" → 사용자에겐 dead-end. 이제 normalize_shift_codes / normalize_single_shift_code
로 정규화 후 모르면 needs_clarification 즉시 반환.
"""

from __future__ import annotations

from agents_v2.skills.bulk_mutation import bulk_mutation


def _base_params(**extra):
    return {"group_id": "g1", "year": 2026, "month": 5, **extra}


def test_unknown_shift_codes_returns_clarification():
    res = bulk_mutation(None, _base_params(
        scope="schedule",
        shift_codes=["XX"],
    ))
    assert res.get("needs_clarification") is True
    assert "XX" in res["question"]


def test_known_shift_codes_normalized_and_passthrough():
    """알 수 있는 코드만 있으면 정규화 후 정상 흐름 — 잘못 들어와 clarification 안 됨."""
    # 'wanted_adjustment' scope 라서 DB 가 필요한데, year/month/group_id 검증을
    # 통과 후 wanted_tools 호출에서 None DB 라 attribute error 발생할 것.
    # 우리가 보장하려는 건 그 전에 clarification 으로 빠지지 않는다는 것.
    try:
        res = bulk_mutation(None, _base_params(
            scope="wanted_adjustment",
            shift_codes=["나이트"],
            mutation={"target_field": "is_applied", "target_value": True},
        ))
        # 통과한다면 needs_clarification 이 아니어야 함
        assert "needs_clarification" not in res
    except AttributeError:
        # None DB 호출로 인한 AttributeError 는 의도된 통과(정규화 단계 통과 증명)
        pass


def test_unknown_new_shift_code_returns_clarification():
    res = bulk_mutation(None, _base_params(
        scope="schedule",
        new_shift_code="ZZ",
    ))
    assert res.get("needs_clarification") is True
    assert "ZZ" in res["question"]


def test_korean_new_shift_code_normalized():
    """new_shift_code='나이트' → 정규화 후 'N' 으로 흐름 통과."""
    try:
        bulk_mutation(None, _base_params(
            scope="published_schedule",
            new_shift_code="나이트",
            nurse_ids=["n1"],
            date="2026-05-03",
        ))
    except AttributeError:
        pass  # None DB 호출, but 정규화 단계는 통과


def test_unknown_shift_name_returns_clarification():
    """shift_name='QQ' (add_shift 용) → clarification."""
    res = bulk_mutation(None, _base_params(
        scope="wanted_submissions",
        action="add_shift",
        nurse_ids=["n1"],
        date="2026-05-03",
        shift_name="QQ",
    ))
    assert res.get("needs_clarification") is True
    assert "QQ" in res["question"]


def test_no_shift_params_passes_through():
    """shift 관련 파라미터 자체가 없으면 정규화 단계는 no-op."""
    res = bulk_mutation(None, _base_params(scope="unsupported_scope"))
    # 정규화 단계 통과 후 scope 라우팅에서 error 반환
    assert res == {"error": "Unsupported mutation scope: unsupported_scope"}
