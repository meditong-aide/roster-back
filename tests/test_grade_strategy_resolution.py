from __future__ import annotations

from services.roster_create_service import _select_effective_grade_strategy


def test_select_effective_strategy_uses_resolved_when_request_omitted():
    out = _select_effective_grade_strategy(
        req_strategy="",
        resolved_strategy="COMBINED",
        grade_config={"constraints_json": {"D": {"1": 1}}},
    )
    assert out == "COMBINED"


def test_select_effective_strategy_base_when_explicit_base():
    out = _select_effective_grade_strategy(
        req_strategy="BASE",
        resolved_strategy="COMBINED",
        grade_config={"constraints_json": {"D": {"1": 1}}},
    )
    assert out == "BASE"
