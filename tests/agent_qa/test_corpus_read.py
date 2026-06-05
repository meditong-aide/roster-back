"""QA-1: 조회 query 코퍼스 schema + 도메인 커버리지 검증."""

from __future__ import annotations

import pytest

from tests.agent_qa.corpus.queries_read import (
    READ_QUERIES,
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
}

REQUIRED_FIELDS = {"query", "expected_skill", "expected_params_subset", "category"}


def test_corpus_size_at_least_30():
    assert len(READ_QUERIES) >= 30, f"corpus size={len(READ_QUERIES)} < 30 (acceptance criterion)"


def test_all_entries_have_required_schema():
    for i, entry in enumerate(READ_QUERIES):
        missing = REQUIRED_FIELDS - set(entry.keys())
        assert not missing, f"entry[{i}] missing fields: {missing}"
        assert isinstance(entry["query"], str) and entry["query"].strip()
        assert isinstance(entry["expected_skill"], str)
        assert isinstance(entry["expected_params_subset"], dict)
        assert isinstance(entry["category"], str)


def test_expected_skill_is_registered():
    for entry in READ_QUERIES:
        assert entry["expected_skill"] in REGISTERED_SKILLS, (
            f"unknown skill '{entry['expected_skill']}' for query: {entry['query']}"
        )


def test_domain_coverage_at_least_6():
    categories = {entry["category"] for entry in READ_QUERIES}
    assert len(categories) >= 6, f"only {len(categories)} domains, need ≥6: {categories}"


def test_expected_domains_present():
    categories = {entry["category"] for entry in READ_QUERIES}
    required = {
        "roster_config",
        "wanted_config",
        "monthly_limit",
        "schedule",
        "wanted_submission",
        "nurse_info",
    }
    missing = required - categories
    assert not missing, f"missing required categories: {missing}"


def test_each_domain_has_at_least_one_query():
    for entry in READ_QUERIES:
        category = entry["category"]
        results = get_queries_by_category(category)
        assert len(results) >= 1


def test_known_failures_documented():
    failures = get_known_failures()
    assert len(failures) >= 5, (
        "코퍼스에 known_failure (미구현 도메인) 가 ≥5 명시되어야 "
        "QA-8 등 후속 보완 작업의 baseline 이 됨"
    )


@pytest.mark.parametrize("entry", READ_QUERIES, ids=lambda e: e["query"][:30])
def test_each_entry_individually_valid(entry):
    """parametric — 각 entry 단독 schema 검증 (실패 시 정확한 query 식별)."""
    assert entry["query"]
    assert entry["expected_skill"] in REGISTERED_SKILLS
    if entry.get("expected_scope"):
        assert isinstance(entry["expected_scope"], str)
