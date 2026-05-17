"""U-50 — docs/CONSTRAINT_TESTCASE_MATRIX_SPEC.md 50 cases 전체 회귀.

Acceptance: ≥45/50 (90%) PASS — ontology 카탈로그 정합성 / cause-bucket /
treatment / graph dangling=0 / NO_ASSIGNMENT cause-leak 차단 / hard_case 자동
분류.

각 case 의 PASS 정의는 matrix_50_cases.assert_case 참조.
"""

from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "tools" / "harness"))
sys.path.insert(0, str(_ROOT / "app"))

import matrix_50_cases as m50  # noqa: E402


def test_50_cases_at_least_45_pass() -> None:
    """매트릭스 acceptance: ≥45/50 (90%) PASS."""
    results = m50.run_all_50()
    passed = sum(1 for r in results if r["pass"])
    total = len(results)
    failing = [(r["case_id"], r["category"]) for r in results if not r["pass"]]
    assert passed >= 45, (
        f"matrix 50 cases acceptance violation: {passed}/{total} PASS "
        f"(target ≥45/50, 90%). Failing: {failing[:10]}"
    )


def test_50_cases_no_no_assignment_leak_anywhere() -> None:
    """U-1 invariant: NO_ASSIGNMENT* 절대 cause-bucket 진입 금지 (50 case 전부)."""
    results = m50.run_all_50()
    leakers = [r["case_id"] for r in results if not r["no_leak_pass"]]
    assert not leakers, f"NO_ASSIGNMENT cause-leak in cases: {leakers}"


def test_50_cases_no_dangling_edges() -> None:
    """payload.graph 의 dangling_edges == 0 (50 case 전부)."""
    results = m50.run_all_50()
    danglers = [r["case_id"] for r in results if not r["dangling_pass"]]
    assert not danglers, f"dangling edges in graph for cases: {danglers}"


def test_50_cases_each_has_treatment_recommendation() -> None:
    """매 case 에 treatment_recommendations 최소 1건 발급."""
    results = m50.run_all_50()
    empty = [r["case_id"] for r in results if not r["trs_pass"]]
    assert not empty, f"no treatment_recommendations for cases: {empty}"


def test_50_cases_hard_case_classification_correctness() -> None:
    """≥3 distinct expected category 면 hard_case=true 가 자동 발급."""
    results = m50.run_all_50()
    misclass = [r["case_id"] for r in results if not r["hard_pass"]]
    assert not misclass, f"hard_case misclassified for cases: {misclass}"


def test_per_category_pass_distribution() -> None:
    """카테고리별 100% PASS (개별 카테고리 acceptance: ≥80%)."""
    results = m50.run_all_50()
    by_cat: dict[str, list] = {}
    for r in results:
        by_cat.setdefault(r["category"], []).append(r)
    failing_cats = []
    for cat, rs in by_cat.items():
        cp = sum(1 for r in rs if r["pass"])
        if cp / len(rs) < 0.8:
            failing_cats.append((cat, f"{cp}/{len(rs)}"))
    assert not failing_cats, f"카테고리별 acceptance(≥80%) 미달: {failing_cats}"
