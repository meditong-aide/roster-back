"""U-5 — payload_graph 빌더 단위 검증.

invariant:
  - dangling_edges == 0
  - cause node 의 category 는 5 종(capacity/eligibility/fixed/team/grade/...) 라벨링
  - bundle/treatment 연결 정합 (bundle → treatment member edge, cause → treatment cover edge)
  - hard_case_meta 노드는 is_hard=true 시 등장 + 모든 cause 에서 aggregation edge
"""

from __future__ import annotations

from services.payload_graph import build_payload_graph


def _c(cid: str, alias: str | None = None, msg: str = "") -> dict:
    return {
        "reason_code": alias or cid,
        "node_id": cid,
        "details": {},
        "human_message_ko": msg,
    }


def _s(code: str, msg: str = "") -> dict:
    return {"reason_code": code, "human_message_ko": msg}


def _bundle(bid: str, treatments: list[dict], covered: list[str], uncovered: list[str] | None = None) -> dict:
    return {
        "bundle_id": bid,
        "total_cost": sum(t.get("cost", 0) for t in treatments),
        "overhead": 0,
        "covered_causes": covered,
        "uncovered_causes": uncovered or [],
        "treatments": treatments,
    }


def _t(tid: str, family: str = "", covers: list[str] | None = None, action_type: str = "force_soft_mode") -> dict:
    return {
        "treatment_id": tid,
        "target_family": family,
        "action_type": action_type,
        "config_key": "k",
        "direction": "enable",
        "rationale_ko": "r",
        "trade_off_ko": "t",
        "cost": 1,
        "covers": covers or [],
    }


def test_empty_inputs_produce_empty_graph() -> None:
    g = build_payload_graph(causes=[], symptoms=[], treatment_bundles=[], evidence=None, hard_case=None)
    assert g["nodes"] == []
    assert g["edges"] == []
    assert g["stats"]["dangling_edges"] == 0


def test_single_cause_single_symptom_emits_causal_edge() -> None:
    causes = [_c("cause:capacity:daily_total_shortage", "GLOBAL_DAY_CAPACITY_SHORTAGE")]
    symptoms = [_s("NO_ASSIGNMENT", "실배정 0")]
    g = build_payload_graph(causes=causes, symptoms=symptoms)
    kinds = [n["kind"] for n in g["nodes"]]
    assert "cause" in kinds and "symptom" in kinds
    assert any(e["kind"] == "causal" for e in g["edges"])
    assert g["stats"]["dangling_edges"] == 0


def test_cause_category_is_resolved_via_ontology() -> None:
    causes = [_c("cause:capacity:daily_total_shortage", "GLOBAL_DAY_CAPACITY_SHORTAGE")]
    g = build_payload_graph(causes=causes)
    cause_node = [n for n in g["nodes"] if n["kind"] == "cause"][0]
    assert cause_node["category"] == "capacity"


def test_bundle_treatment_member_and_cover_edges() -> None:
    causes = [
        _c("cause:capacity:daily_total_shortage", "GLOBAL_DAY_CAPACITY_SHORTAGE"),
        _c("cause:grade:max_sum_below_need", "GRADE_MAX_SUM_BELOW_NEED"),
    ]
    bundles = [
        _bundle(
            "bundle:test:cover",
            treatments=[
                _t("treatment:soft:grade_max", family="GradeMax",
                   covers=["cause:grade:max_sum_below_need"]),
                _t("treatment:threshold:coverage_min", family="CoverageMin",
                   covers=["cause:capacity:daily_total_shortage"]),
            ],
            covered=["cause:grade:max_sum_below_need", "cause:capacity:daily_total_shortage"],
        ),
    ]
    g = build_payload_graph(causes=causes, treatment_bundles=bundles)
    edge_kinds = {e["kind"] for e in g["edges"]}
    assert "treatment" in edge_kinds   # cause → treatment cover edge
    assert "member" in edge_kinds       # bundle → treatment member edge
    assert g["stats"]["dangling_edges"] == 0


def test_evidence_edges_from_bundles() -> None:
    causes = [_c("cause:capacity:monthly_total_shortage", "CAPACITY_TOTAL_SHORTAGE")]
    bundles = [_bundle("bundle:b1", treatments=[_t("treatment:threshold:coverage_min")], covered=[])]
    evidence = {"proof_type": "re_solve", "status": "INFEASIBLE", "verified": False}
    g = build_payload_graph(causes=causes, treatment_bundles=bundles, evidence=evidence)
    assert any(e["kind"] == "evidence" for e in g["edges"])
    assert g["stats"]["dangling_edges"] == 0


def test_hard_case_meta_node_and_aggregation_edges() -> None:
    causes = [
        _c("cause:capacity:monthly_total_shortage", "CAPACITY_TOTAL_SHORTAGE"),
        _c("cause:grade:max_sum_below_need", "GRADE_MAX_SUM_BELOW_NEED"),
        _c("cause:team:min_over_need", "TEAM_MIN_EXCEEDS_GLOBAL_NEED"),
    ]
    hc = {"is_hard": True, "criteria_matched": ["C-MULTI"], "cause_count": 3, "category_count": 3}
    g = build_payload_graph(causes=causes, hard_case=hc)
    hc_node = [n for n in g["nodes"] if n["kind"] == "hard_case_meta"]
    assert len(hc_node) == 1
    agg_edges = [e for e in g["edges"] if e["kind"] == "aggregation"]
    assert len(agg_edges) == 3   # 각 cause → hard_case_meta
    assert g["stats"]["dangling_edges"] == 0


def test_no_hard_case_meta_when_not_hard() -> None:
    causes = [_c("cause:capacity:monthly_total_shortage", "CAPACITY_TOTAL_SHORTAGE")]
    hc = {"is_hard": False, "criteria_matched": []}
    g = build_payload_graph(causes=causes, hard_case=hc)
    assert all(n["kind"] != "hard_case_meta" for n in g["nodes"])
    assert all(e["kind"] != "aggregation" for e in g["edges"])


def test_no_dangling_edges_even_with_unknown_cover() -> None:
    """treatment.covers 가 ontology 에 없는 raw 코드라도 dangling 0."""
    causes = [_c("cause:capacity:monthly_total_shortage", "CAPACITY_TOTAL_SHORTAGE")]
    bundles = [
        _bundle("bundle:unknown_cover", treatments=[
            _t("treatment:threshold:monthly_night_cap", covers=["UNKNOWN_CAUSE_ID"])
        ], covered=["UNKNOWN_CAUSE_ID"]),
    ]
    g = build_payload_graph(causes=causes, treatment_bundles=bundles)
    assert g["stats"]["dangling_edges"] == 0   # 빌더가 미정의 endpoint 를 skip
