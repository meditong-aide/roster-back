"""US-004 — action 추천 + 최소변경 랭킹 검증."""

from services.ontology_graph.builder import UnifiedGraphInput, build_unified_graph
from services.ontology_graph.recommender import recommend_actions


def _coverage_infeasible():
    # day0 N 3요구, 가용 2 → coverage 부족 (CoverageMin 이 수요 정의자)
    return build_unified_graph(UnifiedGraphInput(
        nurses=[{"nurse_id": "a", "grade": 1, "team_id": "T1"},
                {"nurse_id": "b", "grade": 1, "team_id": "T1"}],
        num_days=1, work_shifts=["D", "E", "N"],
        requirements_by_day={0: {"N": 3}},
    ))


def test_actions_emitted_for_infeasible():
    actions = recommend_actions(_coverage_infeasible())
    assert actions, "infeasible 인데 추천 액션 없음"


def test_top_action_targets_injected_family():
    """coverage 부족 → 1순위가 CoverageMin(수요 정의자) 타겟."""
    actions = recommend_actions(_coverage_infeasible())
    assert actions[0].target_family == "CoverageMin"
    assert actions[0].is_primary


def test_top_action_is_minimal_change_not_disable():
    """1순위는 전면 disable 이 아니라 미세 조정(set_threshold)이어야 한다."""
    actions = recommend_actions(_coverage_infeasible())
    assert actions[0].action_type != "disable_module"
    assert actions[0].action_type == "set_threshold"


def test_delta_amount_equals_shortage():
    """최소변경: delta.amount 가 부족량(1)과 일치 — '부족한 만큼만'."""
    actions = recommend_actions(_coverage_infeasible())
    top = actions[0]
    assert top.delta.get("amount") == 1
    assert top.delta.get("config_key")


def test_ranking_minimal_before_invasive():
    """같은 후보군에서 set_threshold 가 disable 보다 앞선다."""
    actions = recommend_actions(_coverage_infeasible())
    types_in_order = [a.action_type for a in actions]
    if "set_threshold" in types_in_order and "disable_module" in types_in_order:
        assert types_in_order.index("set_threshold") < types_in_order.index("disable_module")


def test_no_actions_when_feasible():
    g = build_unified_graph(UnifiedGraphInput(
        nurses=[{"nurse_id": "a"}, {"nurse_id": "b"}, {"nurse_id": "c"}],
        num_days=1, work_shifts=["D", "E", "N"],
        requirements_by_day={0: {"N": 1}},
    ))
    assert recommend_actions(g) == []


def test_team_min_injection_surfaces_team_action():
    """team_min 과밀도 같은 셀 부족을 통해 TeamMin 완화 액션이 후보에 포함."""
    g = build_unified_graph(UnifiedGraphInput(
        nurses=[{"nurse_id": "a", "team_id": "T1"}, {"nurse_id": "b", "team_id": "T1"}],
        num_days=1, work_shifts=["D", "E", "N"],
        requirements_by_day={0: {"N": 3}},
        team_min={"T1": {"N": 2}},
    ))
    actions = recommend_actions(g)
    families = {a.target_family for a in actions}
    assert "TeamMin" in families
