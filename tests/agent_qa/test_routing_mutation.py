"""QA-4 (mutation): 라우팅 정합성 — mutation 코퍼스 전체 dispatch 검증."""

from __future__ import annotations

import pytest

from agents_v2.skills.registry import SKILL_REGISTRY, _ensure_loaded

from tests.agent_qa.corpus.queries_mutation import (
    MUTATION_QUERIES,
    get_known_failures,
)
from tests.agent_qa.harness import (
    AgentTestSession,
    ScriptedClient,
    ScriptedResponse,
)


_ensure_loaded()
REGISTERED = {s.replace("-", "_") for s in SKILL_REGISTRY.keys()}


def _is_routable(entry: dict) -> bool:
    name = entry["expected_skill"].replace("-", "_")
    return name in REGISTERED


# ── Tier A: scripted dispatch ─────────────────────────────


@pytest.mark.parametrize(
    "entry",
    [e for e in MUTATION_QUERIES if _is_routable(e)],
    ids=lambda e: e["query"][:30],
)
def test_dispatch_via_scripted_mutation(db, entry):
    """expected_skill 이 등록된 mutation entry 는 agent 가 dispatch 가능해야 한다."""
    skill = entry["expected_skill"].replace("-", "_")
    args = dict(entry["expected_params_subset"])
    # mutation 은 sensitive → preview_only=True 자동 부여 (QA-7 의 단언 대상)
    if entry["sensitivity"] == "high" and "preview_only" not in args:
        args["preview_only"] = True
    cli = ScriptedClient([
        ScriptedResponse(tool_calls=[{"name": skill, "args": args}])
    ])
    sess = AgentTestSession(db, client=cli)
    sess.send(entry["query"])
    sess.assert_tool_called(skill)


# ── Tier B: unrouted entries (QA-8 에서 구현 예정) ──


def test_known_failures_now_routable_after_qa8():
    """QA-8 이후 known_failures (모두 monthly_limit) 가 등록된 skill 로 라우팅 가능해야 함.

    이전엔 unregistered 였지만 QA-8 (update_monthly_limit skill bridge) 이후 등록 완료.
    """
    known = get_known_failures()
    assert known, "monthly_limit 관련 known_failure entry 가 최소 1개 있어야 함"
    for entry in known:
        skill = entry["expected_skill"].replace("-", "_")
        assert skill in REGISTERED, (
            f"known_failure entry '{entry['query']}' 의 expected_skill '{skill}' 가 "
            f"여전히 미등록 — QA-8 적용 후엔 등록되어야 함."
        )


def test_routable_corpus_size_at_least_25():
    """현재 등록된 skill 로 라우팅 가능한 entry 가 ≥25 (mutation 30+ 중 75%)."""
    routable = [e for e in MUTATION_QUERIES if _is_routable(e)]
    assert len(routable) >= 25, (
        f"routable mutation entries={len(routable)} < 25 — skill 커버리지 부족"
    )
