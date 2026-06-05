"""QA-2: mutation query 코퍼스 schema + sensitivity 분포 + 도메인 커버리지 검증."""

from __future__ import annotations

import pytest

from tests.agent_qa.corpus.queries_mutation import (
    MUTATION_QUERIES,
    get_high_sensitivity,
    get_low_sensitivity,
    get_queries_by_category,
    get_known_failures,
)


REGISTERED_SKILLS = {
    "query_schedule",
    "bulk_mutation",
    "update_constraint",
    "update_person_attr",
    "generate_schedule",
    "validate_schedule",
    "recommend_candidates",
    "repair_schedule",
    "analyze_report",
    # QA-8 에서 추가될 신규 skill — 코퍼스가 먼저 정의되고 후속에서 구현됨
    "update_monthly_limit",
}

REQUIRED_FIELDS = {
    "query",
    "expected_skill",
    "expected_params_subset",
    "sensitivity",
    "category",
}


def test_corpus_size_at_least_30():
    assert len(MUTATION_QUERIES) >= 30


def test_all_entries_have_required_schema():
    for i, entry in enumerate(MUTATION_QUERIES):
        missing = REQUIRED_FIELDS - set(entry.keys())
        assert not missing, f"entry[{i}] missing: {missing}"
        assert entry["sensitivity"] in {"low", "high"}


def test_expected_skill_is_registered_or_planned():
    for entry in MUTATION_QUERIES:
        assert entry["expected_skill"] in REGISTERED_SKILLS, (
            f"unknown skill '{entry['expected_skill']}' for: {entry['query']}"
        )


def test_sensitivity_distribution_at_least_10_high():
    high = get_high_sensitivity()
    assert len(high) >= 10, (
        f"high sensitivity count={len(high)} < 10 — QA-7 acceptance requires ≥10"
    )


def test_low_sensitivity_at_least_3():
    low = get_low_sensitivity()
    assert len(low) >= 3, (
        f"low sensitivity count={len(low)} < 3 — preview-skip optimization 변별을 위해 필요"
    )


def test_domain_coverage_at_least_5():
    categories = {entry["category"] for entry in MUTATION_QUERIES}
    assert len(categories) >= 5, f"only {len(categories)} categories: {categories}"


def test_expected_domains_present():
    categories = {entry["category"] for entry in MUTATION_QUERIES}
    required_substrings = {
        "roster_config",
        "person_attr",
        "wanted",
        "monthly_limit",
        "schedule_generate",
    }
    for sub in required_substrings:
        assert any(sub in c for c in categories), f"no category contains '{sub}': {categories}"


def test_known_failures_target_monthly_limit():
    failures = get_known_failures()
    assert len(failures) >= 5
    for f in failures:
        assert f["expected_skill"] == "update_monthly_limit", (
            "known_failure 는 모두 update_monthly_limit (QA-8 구현 대상) 여야 함"
        )


@pytest.mark.parametrize("entry", MUTATION_QUERIES, ids=lambda e: e["query"][:30])
def test_each_entry_individually_valid(entry):
    assert entry["query"]
    assert entry["expected_skill"] in REGISTERED_SKILLS
    assert entry["sensitivity"] in {"low", "high"}
