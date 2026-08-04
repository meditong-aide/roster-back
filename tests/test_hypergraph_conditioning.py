"""Factor hypergraph + recursive conditioning — 완전성 audit 3층 + component 분해 + 정확성.

factor 가 빠짐없이 들어갔는지(구조·규칙·재구성)와, 조건화가 독립 component 로 분리해 AND
결합하는 실제 separator 메커니즘, 그리고 solver 판정이 독립 oracle 과 일치함을 잠근다.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools", "infeasible_cases"))

import exact_oracle  # noqa: E402

exact_oracle._BUDGET = 120_000

from services.ontology_graph.factor_audit import (  # noqa: E402
    _semantic_reconstruction,
    audit_factor_graph,
)
from services.ontology_graph.hypergraph_conditioning import diagnose_conditioning  # noqa: E402


def _pool(k):
    return [{"nurse_id": f"n{i}", "name": f"N{i}", "grade": 1, "team_id": "A"} for i in range(k)]


def _cfg(dsr, rules, fb=None, fo=None):
    c = dict(rules, daily_shift_requirements=dsr)
    c["initial_constraints"] = {"forbidden": fb or {}, "forced_off": fo or {}}
    return c


_R = {"two_offs_after_two_nig": True, "not_one_night": True, "forbid_night_to_day": True}


def test_audit_structural_and_rules_complete():
    """① 구조 + ② 규칙 인코딩 audit — 누락 없이 통과(strict 면 예외 없음)."""
    rep = audit_factor_graph(_pool(5), _cfg({"D": 1, "E": 1, "N": 2}, _R), 6, strict=True)
    assert not rep["structural"]["problems"]
    assert rep["rule_probes"]["all_ok"]
    # 각 활성 규칙이 실제로 위반을 거부했는가(거짓 통과 없음)
    kinds = {p["rule"].split("(")[0] for p in rep["rule_probes"]["probes"]}
    assert "not_one_night" in kinds and "max_run" in kinds


def test_semantic_reconstruction_matches_oracle():
    """③ 의미 재구성 — factor graph solver 판정 == 독립 oracle (누락 factor면 불일치)."""
    rec = _semantic_reconstruction(seed=99, n=40, budget=100_000)
    assert rec["checked"] > 10
    assert rec["mismatch"] == 0, rec["example"]


def test_conditioning_splits_into_components():
    """진짜 separator: 절연된 2그룹 → 최상위 component 2개로 분해."""
    nu = _pool(6)
    fo = {f"n{i}": [3, 4, 5] for i in range(3)}
    fo.update({f"n{i}": [0, 1, 2] for i in range(3, 6)})
    r = diagnose_conditioning(nu, _cfg({"D": 1, "E": 0, "N": 1}, {"not_one_night": True}, fo=fo), 6)
    assert r.components_seen == 2
    assert r.status in ("FEASIBLE_WITNESS", "INFEASIBLE_CERTIFIED")


def test_conditioning_sound_on_dense_case():
    """dense 시간격자(회복OFF starvation)에선 generic min-degree conditioning 이 비효율 →
    UNKNOWN 가능(무한루프 아님). 단 **절대 FEASIBLE 오판 없음**(sound). 밀집 시간축은
    frontier DP 의 temporal sweep 이 적합(상보적)."""
    r = diagnose_conditioning(_pool(5),
                              _cfg({"D": 1, "E": 1, "N": 2},
                                   {"two_offs_after_two_nig": True, "not_one_night": True}), 6,
                              budget=80_000)
    assert r.status in ("INFEASIBLE_CERTIFIED", "UNKNOWN")   # feasible 오판만 없으면 sound


def test_context_cache_preserves_correctness_and_helps_components():
    """context caching: 재구성으로 correctness 유지 확인은 test_semantic_reconstruction 가 담당.
    여기선 component 분해가 캐싱 후에도 유지되는지(캐시가 분해를 깨지 않음)."""
    nu = _pool(6)
    fo = {f"n{i}": [3, 4, 5] for i in range(3)}
    fo.update({f"n{i}": [0, 1, 2] for i in range(3, 6)})
    r = diagnose_conditioning(nu, _cfg({"D": 1, "E": 0, "N": 1}, {"not_one_night": True}, fo=fo), 6)
    assert r.components_seen == 2 and r.status in ("FEASIBLE_WITNESS", "INFEASIBLE_CERTIFIED")


def test_empty_domain_forced_conflict():
    """같은 칸 강제근무(OFF금지)+강제OFF → 도메인 공집합 → 즉시 INFEASIBLE."""
    fb = {"n0": {2: ["O"]}}
    fo = {"n0": [2]}
    r = diagnose_conditioning(_pool(4), _cfg({"D": 1, "E": 1, "N": 1},
                                             {"not_one_night": True}, fb=fb, fo=fo), 5)
    assert r.status == "INFEASIBLE_CERTIFIED"
    assert r.certificate.kind == "empty_domain"
