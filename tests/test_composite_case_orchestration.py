"""U-2 — 복합 케이스 (multi-cause) e2e 오케스트레이션 검증.

요구 (사용자 ralph): "case를 복합적으로도 처리할 수 있어야 해."

검증:
  - ≥3 distinct cause id 동시 자극 시 causes[] 에 모두 노출.
  - treatment_recommendations primary bundle 의 cover 비율 ≥80% 또는 hard_case 표시.
  - resolution_narrative 의 problem_list 가 동일 cause 수와 일치 + 각 problem 에 mapped_treatments.
  - payload.graph 에 cause/treatment/bundle 모두 등장, dangling 0.
  - hard_case=true 시 manual_investigation treatment append.

각 시나리오는 build_unrecoverable_payload 를 fake violated_constraints 로 호출 (HTTP/solver 불요).
"""

from __future__ import annotations

import pytest

from services.precheck.payload import build_unrecoverable_payload


def _vc(cause_id: str, alias: str | None = None, **ev) -> dict:
    return {
        "reason_code": alias or cause_id,
        "node_id": cause_id,
        "details": dict(ev),
        "human_message_ko": f"synthetic {cause_id}",
    }


def _payload_for(violated: list[dict]) -> dict:
    return build_unrecoverable_payload(
        precheck_result={"issues": []},
        applied_relaxations=[],
        last_error_reason="composite test",
        violated_constraints=violated,
        conflict_cores=[],
        pool_snapshot={},
    )


@pytest.mark.parametrize(
    "scenario_name,violated_factory,min_cause_count,expected_categories",
    [
        (
            "capacity+grade+team",
            lambda: [
                _vc("cause:capacity:monthly_total_shortage", "CAPACITY_TOTAL_SHORTAGE",
                    required=100, capacity=80, shortage=20),
                _vc("cause:grade:max_sum_below_need", "GRADE_MAX_SUM_BELOW_NEED",
                    day=5, shift="D", cap=4, required=6),
                _vc("cause:team:min_over_need", "TEAM_MIN_EXCEEDS_GLOBAL_NEED",
                    day=5, shift="D", min_sum=6, required=4),
            ],
            3,
            {"capacity", "grade", "team"},
        ),
        (
            "capacity+team",
            lambda: [
                _vc("cause:capacity:daily_night_shortage", "N_CAPACITY_SHORTAGE",
                    day=5, n_required=6),
                _vc("cause:team:min_over_need", "TEAM_MIN_EXCEEDS_GLOBAL_NEED",
                    day=5, shift="N", min_sum=4, required=2),
            ],
            2,
            {"capacity", "team"},
        ),
        (
            "fixed+eligibility",
            lambda: [
                _vc("cause:fixed:over_demand", "FIXED_ASSIGN_EXCEEDS_NEED",
                    day=5, shift="D", fixed_count=12, required=8),
                _vc("cause:eligibility:nurse_isolated", "ALLOWED_SHIFTS_ISOLATES_NURSE",
                    nurse_id="N001"),
            ],
            2,
            {"fixed", "eligibility"},
        ),
        (
            "grade+team+fixed",
            lambda: [
                _vc("cause:grade:max_sum_below_need", "GRADE_MAX_SUM_BELOW_NEED",
                    day=5, shift="D", cap=4, required=6),
                _vc("cause:team:min_over_need", "TEAM_MIN_EXCEEDS_GLOBAL_NEED",
                    day=5, shift="D", min_sum=6, required=4),
                _vc("cause:fixed:over_demand", "FIXED_ASSIGN_EXCEEDS_NEED",
                    day=5, shift="D", fixed_count=10, required=8),
            ],
            3,
            {"grade", "team", "fixed"},
        ),
    ],
)
def test_composite_cases_expose_all_causes(scenario_name, violated_factory, min_cause_count, expected_categories) -> None:
    payload = _payload_for(violated_factory())
    inf = payload["infeasibility"]
    causes = inf["causes"]
    cause_codes = {c.get("reason_code") for c in causes}
    cause_node_ids = {c.get("node_id") for c in causes}
    # 모든 입력 cause 가 cause-bucket 에 노출
    assert len(causes) >= min_cause_count, f"[{scenario_name}] only {len(causes)} causes (expected ≥{min_cause_count})"
    # graph 카테고리 검증
    graph = inf["graph"]
    cause_node_cats = {n["category"] for n in graph["nodes"] if n["kind"] == "cause"}
    assert expected_categories.issubset(cause_node_cats), (
        f"[{scenario_name}] cats={cause_node_cats} missing {expected_categories - cause_node_cats}"
    )
    # NO_ASSIGNMENT 차단 (U-1 invariant)
    assert all(not (c or "").startswith("NO_ASSIGNMENT") for c in cause_codes), (
        f"[{scenario_name}] NO_ASSIGNMENT 라벨 cause 누출"
    )
    assert "DAY_ZERO_COVERAGE" not in cause_codes


@pytest.mark.parametrize("scenario_name,violated_factory", [
    ("3-cause", lambda: [
        _vc("cause:capacity:monthly_total_shortage", "CAPACITY_TOTAL_SHORTAGE"),
        _vc("cause:grade:max_sum_below_need", "GRADE_MAX_SUM_BELOW_NEED"),
        _vc("cause:team:min_over_need", "TEAM_MIN_EXCEEDS_GLOBAL_NEED"),
    ]),
])
def test_composite_case_has_treatment_recommendations(scenario_name, violated_factory) -> None:
    payload = _payload_for(violated_factory())
    inf = payload["infeasibility"]
    trs = inf["treatment_recommendations"]
    assert len(trs) >= 1, f"[{scenario_name}] treatment_recommendations 비어있음"
    primary = trs[0]
    covered = len(primary.get("covered_causes", []))
    uncovered = len(primary.get("uncovered_causes", []))
    total = covered + uncovered
    if total > 0:
        ratio = covered / total
        # 80% 이상 cover OR hard_case 로 flagged
        if ratio < 0.8:
            assert inf["hard_case"]["is_hard"], (
                f"primary bundle cover {ratio*100:.1f}% (<80%) — hard_case 표시 필요"
            )


def test_composite_case_narrative_has_mapped_treatments_for_each_problem() -> None:
    violated = [
        _vc("cause:capacity:monthly_total_shortage", "CAPACITY_TOTAL_SHORTAGE",
            required=100, capacity=80, shortage=20),
        _vc("cause:grade:max_sum_below_need", "GRADE_MAX_SUM_BELOW_NEED",
            day=5, shift="D", cap=4, required=6),
        _vc("cause:team:min_over_need", "TEAM_MIN_EXCEEDS_GLOBAL_NEED",
            day=5, shift="D", min_sum=6, required=4),
    ]
    payload = _payload_for(violated)
    inf = payload["infeasibility"]
    narr = inf["resolution_narrative"]
    assert narr is not None
    pl = narr["problem_list"]
    assert len(pl) >= 3
    for p in pl:
        assert p["cause_id"]
        assert p["mapped_treatments"], f"cause {p['cause_id']} mapped_treatments 누락"
        assert p["cause_solution_ko"]
        assert "해결책" in p["cause_solution_ko"]


def test_composite_hard_case_flagged_for_4_category_explosion() -> None:
    violated = [
        _vc("cause:capacity:monthly_total_shortage", "CAPACITY_TOTAL_SHORTAGE"),
        _vc("cause:eligibility:nurse_isolated", "ALLOWED_SHIFTS_ISOLATES_NURSE"),
        _vc("cause:fixed:over_demand", "FIXED_ASSIGN_EXCEEDS_NEED"),
        _vc("cause:team:min_over_need", "TEAM_MIN_EXCEEDS_GLOBAL_NEED"),
        _vc("cause:grade:max_sum_below_need", "GRADE_MAX_SUM_BELOW_NEED"),
    ]
    payload = _payload_for(violated)
    inf = payload["infeasibility"]
    hc = inf["hard_case"]
    assert hc["is_hard"] is True
    assert "C-WIDE" in hc["criteria_matched"] or "C-MULTI" in hc["criteria_matched"]
    # manual_investigation treatment 가 append 되었는지
    trs = inf["treatment_recommendations"]
    assert any(t.get("bundle_id") == "bundle:meta:manual_investigation" for t in trs)


def test_composite_payload_graph_consistency_no_dangling() -> None:
    violated = [
        _vc("cause:capacity:monthly_total_shortage", "CAPACITY_TOTAL_SHORTAGE"),
        _vc("cause:grade:max_sum_below_need", "GRADE_MAX_SUM_BELOW_NEED"),
        _vc("cause:team:min_over_need", "TEAM_MIN_EXCEEDS_GLOBAL_NEED"),
    ]
    payload = _payload_for(violated)
    g = payload["infeasibility"]["graph"]
    assert g["stats"]["dangling_edges"] == 0
    # 노드 종류 모두 존재
    kinds = {n["kind"] for n in g["nodes"]}
    assert "cause" in kinds
    # treatment_recommendations 가 있으면 treatment/bundle 도 등장
    if payload["infeasibility"]["treatment_recommendations"]:
        assert "treatment" in kinds or "bundle" in kinds


def test_single_cause_simple_case_is_not_hard() -> None:
    violated = [
        _vc("cause:capacity:daily_total_shortage", "GLOBAL_DAY_CAPACITY_SHORTAGE",
            day=5, total_demand=10, nurse_count=8),
    ]
    payload = _payload_for(violated)
    inf = payload["infeasibility"]
    assert inf["hard_case"]["is_hard"] is False
    # manual_investigation treatment 안 들어감
    trs = inf["treatment_recommendations"]
    assert not any(t.get("bundle_id") == "bundle:meta:manual_investigation" for t in trs)
