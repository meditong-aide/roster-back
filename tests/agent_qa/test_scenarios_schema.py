"""E2E 시나리오 코퍼스 schema 및 커버리지 검증."""

from __future__ import annotations

import pytest

from tests.agent_qa.scenarios.e2e_scenarios import (
    SCENARIOS,
    CATEGORIES,
    TOTAL,
    COMPOUND_COUNT,
    get_by_category,
    get_compound_scenarios,
    get_simple_scenarios,
)


REQUIRED_FIELDS = {"id", "category", "complexity", "query", "expected_action", "turns", "mode", "permission"}


def test_total_size_at_least_50():
    assert TOTAL >= 50, f"scenario count={TOTAL} < 50 (확장된 코퍼스)"


def test_compound_count_at_least_7():
    assert COMPOUND_COUNT >= 7, f"compound scenarios={COMPOUND_COUNT} < 7"


def test_required_categories_present():
    required = {
        "settings_read", "settings_update", "wanted_config",
        "wanted_adjustment", "wanted_read", "team", "grade",
        "monthly_limit", "compound",
        "shift_manage", "permission_denial", "edge_case", "analyze",
    }
    missing = required - set(CATEGORIES)
    assert not missing, f"missing categories: {missing}"


def test_expected_action_present_and_substantive():
    """모든 시나리오에 expected_action 한국어 설명 (≥20자) 이 있어야 검수 가능."""
    for s in SCENARIOS:
        assert s.get("expected_action"), f"scenario {s['id']} missing expected_action"
        assert len(s["expected_action"]) >= 20, (
            f"scenario {s['id']} expected_action 너무 짧음 ({len(s['expected_action'])} chars)"
        )


@pytest.mark.parametrize("s", SCENARIOS, ids=lambda s: s["id"])
def test_scenario_schema(s):
    missing = REQUIRED_FIELDS - set(s.keys())
    assert not missing, f"scenario {s['id']} missing fields: {missing}"
    assert s["complexity"] in {"simple", "compound"}
    assert s["mode"] in {"read_only", "preview", "mutation"}
    assert s["permission"] in {"any", "hn_only"}
    assert isinstance(s["turns"], list) and len(s["turns"]) >= 1
    for i, t in enumerate(s["turns"]):
        assert "user" in t, f"scenario {s['id']} turn[{i}] missing 'user'"
        assert "expected_skill_calls" in t, (
            f"scenario {s['id']} turn[{i}] missing 'expected_skill_calls'"
        )


def test_compound_scenarios_have_multiple_turns_or_skills():
    """복합 시나리오는 (a) 여러 turn 이거나 (b) 한 turn 에 여러 skill 호출."""
    for s in get_compound_scenarios():
        total_calls = sum(len(t["expected_skill_calls"]) for t in s["turns"])
        assert total_calls >= 2 or len(s["turns"]) >= 2, (
            f"compound scenario {s['id']} 가 단일 turn 단일 skill — 복합성 부족"
        )


def test_mutation_scenarios_use_preview_only_true_first():
    """mode='mutation'/'preview' 시나리오의 첫 mutation 호출은 preview_only=True 이어야."""
    for s in SCENARIOS:
        if s["mode"] in ("read_only",):
            continue
        first_turn = s["turns"][0]
        for call in first_turn["expected_skill_calls"]:
            args = call["args_subset"]
            # query_schedule / analyze_report / validate_schedule 등 read skill 은 예외
            if call["name"] in {"query_schedule", "analyze_report", "validate_schedule",
                                "recommend_candidates", "repair_schedule"}:
                continue
            # mutation skill 의 args 에 preview_only 가 있으면 반드시 True
            if "preview_only" in args:
                assert args["preview_only"] is True, (
                    f"scenario {s['id']} turn[0] {call['name']} 의 첫 호출이 "
                    f"preview_only=False — 민감 처리 위반"
                )


def test_hn_only_scenarios_all_mutate_or_preview():
    """permission=hn_only 는 mode=mutation/preview 여야 한다."""
    for s in SCENARIOS:
        if s["permission"] == "hn_only":
            assert s["mode"] in ("mutation", "preview"), (
                f"scenario {s['id']} permission=hn_only 인데 mode={s['mode']} — 비정합"
            )


def test_category_coverage_distribution():
    """카테고리별 최소 2 개 (compound 는 5)."""
    for cat in CATEGORIES:
        entries = get_by_category(cat)
        minimum = 5 if cat == "compound" else 2
        assert len(entries) >= minimum, (
            f"category '{cat}' entries={len(entries)} < {minimum}"
        )
