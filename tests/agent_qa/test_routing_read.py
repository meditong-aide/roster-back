"""QA-4 (read): 라우팅 정합성 — 조회 코퍼스 전체 dispatch 검증.

Two tiers:
  A. dispatch_via_scripted — agent 가 LLM 의 tool_call 신호를 받았을 때 올바르게
     실행하는지 (agent dispatch 계약). expected_skill 이 등록된 entry 는 PASS 기대.
  B. deterministic_baseline — 현재 DeterministicClient 의 키워드 라우팅이 expected_skill
     에 도달하는 비율 (informational baseline, threshold 없음).
"""

from __future__ import annotations

import pytest

from agents_v2.skills.registry import SKILL_REGISTRY, _ensure_loaded

from tests.agent_qa.corpus.queries_read import READ_QUERIES, get_known_failures
from tests.agent_qa.harness import (
    AgentTestSession,
    ScriptedClient,
    ScriptedResponse,
)


_ensure_loaded()
REGISTERED = set(SKILL_REGISTRY.keys())


def _is_routable(entry: dict) -> bool:
    """expected_skill 이 현재 registered 인 entry — Tier A 의 후보."""
    name = entry["expected_skill"].replace("-", "_")
    registered_norm = {s.replace("-", "_") for s in REGISTERED}
    return name in registered_norm


# ── Tier A: scripted dispatch (agent dispatch 계약 검증) ─────


@pytest.mark.parametrize(
    "entry",
    [e for e in READ_QUERIES if _is_routable(e)],
    ids=lambda e: e["query"][:30],
)
def test_dispatch_via_scripted_skill(db, entry):
    """LLM 이 expected_skill 을 호출하기로 결정했다고 가정 → agent 가 정확히 그 skill 을 dispatch 하는지.

    이는 routing 의 옳고 그름이 아니라 agent 의 dispatch 계약을 검증한다.
    실제 routing 정확도는 Tier B (deterministic baseline) 또는 real-LLM 평가 영역.
    """
    skill = entry["expected_skill"].replace("-", "_")
    # expected_params_subset 을 그대로 사용 (subset 이지만 dispatch 단계는 그대로 전달)
    scripted_args = dict(entry["expected_params_subset"])
    # group_id 등 필수 파라미터는 agent 가 ctx 에서 채워주므로 명시 안 해도 됨
    cli = ScriptedClient([
        ScriptedResponse(tool_calls=[{"name": skill, "args": scripted_args}])
    ])
    sess = AgentTestSession(db, client=cli)
    sess.send(entry["query"])
    sess.assert_tool_called(skill)


# ── Tier B: deterministic baseline (informational pass rate) ──


def _run_deterministic(db, entry: dict) -> tuple[bool, str]:
    """deterministic client 로 entry 실행 → (pass, actual_skill) 반환."""
    sess = AgentTestSession(db)
    sess.send(entry["query"])
    calls = sess.get_tool_calls()
    if not calls:
        return False, "<no_tool_call>"
    actual = calls[0][0].replace("-", "_")
    expected = entry["expected_skill"].replace("-", "_")
    return actual == expected, actual


def test_deterministic_baseline_pass_rate(db):
    """Informational: 현재 DeterministicClient 의 keyword 라우팅 정확도 측정.

    threshold 없음 — 단순히 baseline pass rate 를 기록. 향후 client 개선
    또는 real-LLM 평가의 비교 기준.
    """
    results = []
    for entry in READ_QUERIES:
        passed, actual = _run_deterministic(db, entry)
        results.append({
            "query": entry["query"],
            "expected": entry["expected_skill"],
            "actual": actual,
            "passed": passed,
            "is_known_failure": bool(entry.get("note")),
        })

    total = len(results)
    passed = sum(1 for r in results if r["passed"])
    known = sum(1 for r in results if r["is_known_failure"])
    fail_unexpected = [r for r in results if not r["passed"] and not r["is_known_failure"]]

    # 진단 로그 (pytest -v 시 보임)
    print(f"\n[DeterministicBaseline] total={total} pass={passed} known_failure={known}")
    print(f"  pass_rate (incl. known)= {passed + known}/{total} = {(passed + known) / total:.0%}")
    if fail_unexpected:
        print("  unexpected failures:")
        for r in fail_unexpected[:10]:
            print(f"    - {r['query'][:50]} → expected={r['expected']} actual={r['actual']}")

    # Baseline test: passed + known_failure 가 전체의 ≥50% (의도된 known 포함)
    # — Tier A 가 dispatch 계약을 100% 검증하므로 baseline 은 informational.
    acknowledged = passed + known
    assert acknowledged / total >= 0.5, (
        f"deterministic baseline pass+known={acknowledged}/{total} < 50% — "
        f"client routing 심각하게 후퇴. unexpected failures: "
        f"{[r['query'] for r in fail_unexpected[:5]]}"
    )


def test_known_failures_are_documented(db):
    """모든 known_failure (note 있는) entry 는 미구현 도메인을 명시해야."""
    known = get_known_failures()
    assert len(known) >= 5, (
        f"known_failures count={len(known)} < 5 — QA-1 acceptance criterion violated"
    )
    for entry in known:
        assert "미구현" in entry["note"] or "QA-8" in entry["note"], (
            f"note 가 미구현 또는 QA-8 명시 누락: {entry['note']}"
        )
