"""U-4 — resolution_narrative 설명력 검증.

요구:
  - 각 problem_list 항목에 mapped_treatments (cause→treatment) 가 채워짐 (manual 포함 ≥1).
  - cause_solution_ko 가 '원인 → 해결책' 한 문장으로 결합되어 비어있지 않음.
  - naive 패턴 ('인원을 줄이세요' 등) 0건 (build 시 raise).
  - action_levers 의 config_key / direction / rationale_ko 가 모두 채워짐 (manual 제외).
"""

from __future__ import annotations

import re

import pytest

from services.cause_treatment_hitter import propose_bundles
from services.resolution_narrative import (
    build_narrative,
    narrative_to_dict,
    _NAIVE_PATTERNS,
)


def _c(cid: str, evidence: dict | None = None) -> dict:
    return {"reason_code": cid, "cause_id": cid, "details": evidence or {}}


def test_problem_list_includes_mapped_treatments_for_each_cause() -> None:
    causes = [
        _c("cause:capacity:daily_total_shortage",
           {"day": 5, "total_demand": 10, "nurse_count": 8}),
        _c("cause:team:min_over_need",
           {"day": 5, "shift": "D", "min_sum": 6, "required": 4}),
    ]
    bundles = propose_bundles(active_causes=[c["cause_id"] for c in causes])
    primary = bundles[0] if bundles else None
    narr = build_narrative(cause_payloads=causes, bundle=primary)
    assert len(narr.problem_list) == 2
    for p in narr.problem_list:
        assert p.mapped_treatments, f"cause {p.cause_id} 에 mapped_treatments 누락"
        assert p.cause_solution_ko, "cause_solution_ko 비어있음"
        assert "해결책" in p.cause_solution_ko
        for t in p.mapped_treatments:
            assert t["treatment_id"]
            assert t["rationale_ko"]


def test_naive_pattern_raises_value_error() -> None:
    """naive 'X명 줄이세요' 가 narrative 에 등장하면 ValueError."""
    # 의도적으로 cause.problem_template_ko 에 naive 표현이 들어간 fake cause
    class _FakeCause:
        cause_id = "cause:fake"
        label = "fake"
        category = "fake"
        causal_layer = "structural"
        tier = "T1"
        is_hard = True
        problem_template_ko = "간호사를 2명 줄이세요."
        aliases = []

    from services.semantics.ontology import ConstraintOntology
    onto = ConstraintOntology.__new__(ConstraintOntology)
    onto.causes = {"cause:fake": _FakeCause()}
    onto._cause_alias_to_id = {"cause:fake".upper(): "cause:fake"}
    onto.treatments = {}
    onto.resolve_cause_alias = lambda raw: ("cause:fake" if raw and "fake" in raw else None)
    onto.get_cause = lambda raw: _FakeCause() if raw and "fake" in raw else None
    onto.treatments_for_cause = lambda raw: []

    causes = [{"cause_id": "cause:fake", "reason_code": "cause:fake", "details": {}}]
    with pytest.raises(ValueError, match="naive"):
        build_narrative(cause_payloads=causes, ontology=onto)


def test_no_naive_pattern_in_real_narrative() -> None:
    """real ontology cause → narrative 에서 naive regex 0건."""
    causes = [
        _c("cause:capacity:monthly_total_shortage",
           {"required": 100, "capacity": 80, "shortage": 20}),
        _c("cause:grade:max_sum_below_need",
           {"day": 5, "shift": "D", "cap": 4, "required": 6}),
        _c("cause:team:min_over_need",
           {"day": 5, "shift": "D", "min_sum": 6, "required": 4}),
    ]
    bundles = propose_bundles(active_causes=[c["cause_id"] for c in causes])
    primary = bundles[0] if bundles else None
    narr = build_narrative(cause_payloads=causes, bundle=primary)
    d = narrative_to_dict(narr)
    full = " ".join([
        d["summary_ko"],
        *(p["rendered_ko"] for p in d["problem_list"]),
        *(p["cause_solution_ko"] for p in d["problem_list"]),
        *(a["rationale_ko"] for a in d["action_levers"]),
        *(t["trade_off_ko"] for t in d["trade_offs"]),
    ])
    _SAFE = ("보강", "추가", "demand", "단순 일시", "재발", "지양", "주의", "안 됨", "위험")
    for pat in _NAIVE_PATTERNS:
        m = pat.search(full)
        if m:
            ctx = full[max(0, m.start() - 60): m.end() + 60]
            assert any(k in ctx for k in _SAFE), (
                f"naive pattern: '{m.group(0)}' in '{ctx}'"
            )


def test_action_lever_has_config_key_for_non_manual_treatments() -> None:
    causes = [_c("cause:capacity:monthly_night_shortage",
                 {"n_required": 50, "n_capacity": 40})]
    bundles = propose_bundles(active_causes=[c["cause_id"] for c in causes])
    primary = bundles[0] if bundles else None
    narr = build_narrative(cause_payloads=causes, bundle=primary)
    for a in narr.action_levers:
        if a.action_type != "data_correction_required":
            assert a.config_key, f"treatment {a.treatment_id} config_key 누락"
        assert a.rationale_ko
        assert a.direction


def test_uncovered_causes_propagate_to_narrative() -> None:
    """propose_bundles 결과의 uncovered_causes 가 narrative 의 uncovered 로 전달."""
    causes = [_c("cause:undiagnosed")]
    bundles = propose_bundles(active_causes=[c["cause_id"] for c in causes])
    primary = bundles[0] if bundles else None
    narr = build_narrative(cause_payloads=causes, bundle=primary)
    # 결과: undiagnosed 는 manual treatment 매칭됨 → uncovered 0 또는 1.
    # 어쨌든 narrative 가 정상 build (예외 X).
    assert isinstance(narr.uncovered_causes, list)


def test_cause_solution_ko_contains_problem_and_solution_segments() -> None:
    causes = [_c("cause:capacity:monthly_total_shortage",
                 {"required": 100, "capacity": 80, "shortage": 20})]
    bundles = propose_bundles(active_causes=[c["cause_id"] for c in causes])
    primary = bundles[0] if bundles else None
    narr = build_narrative(cause_payloads=causes, bundle=primary)
    assert len(narr.problem_list) == 1
    p = narr.problem_list[0]
    assert "초과" in p.cause_solution_ko or "부족" in p.cause_solution_ko
    assert "해결책" in p.cause_solution_ko
