"""Surplus-Certified Branch-and-Infer (N축) — typed certificate + proof-tree 병합 + 검증 복구."""

from __future__ import annotations

from services.ontology_graph.branch_infer import diagnose_night_axis, verified_repairs
from services.ontology_graph.certificate import FEASIBLE, INFEASIBLE, UNKNOWN, render_explanation

_RULES = {"two_offs_after_three_nig": True, "not_one_night": True,
          "daily_shift_requirements": {"D": 2, "E": 1, "N": 1}}


def _cfg(forbidden=None, forced_off=None, night=1):
    c = dict(_RULES)
    c["daily_shift_requirements"] = {"D": 2, "E": 1, "N": night}
    c["initial_constraints"] = {"forbidden": forbidden or {}, "forced_off": forced_off or {}}
    return c


def _n_only(k):
    return [{"nurse_id": f"n{i}", "name": f"N{i}", "allowed_shifts": ["N"]} for i in range(k)]


def test_dichotomy_proof_tree_merges_two_branches():
    """각자는 되는데 같이는 X: A 강제N(3·4·6) + B 6일 야간불가 → 어느 배정이든 실패."""
    nurses = [{"nurse_id": "A", "name": "에이", "allowed_shifts": ["N"]},
              {"nurse_id": "B", "name": "비이", "allowed_shifts": ["N"]}]
    cfg = _cfg(forbidden={"A": {3: ["O"], 4: ["O"], 6: ["O"]}, "B": {5: ["N"]}})
    node = diagnose_night_axis(nurses, cfg, 10)
    assert node.status == INFEASIBLE
    assert node.depth() == 2 and node.size() == 3          # 분기 1 + leaf 2
    # 양쪽 leaf 사유가 서로 다름(커버리지 vs 시퀀스)
    kinds = {ch.certificate.kind for ch in node.children}
    assert kinds == {"night_coverage_deficit", "sequence_path_empty"}
    exp = render_explanation(node)
    assert "안 하면" in exp and "반대로" in exp and "어느 선택도 불가능" in exp


def test_single_nurse_sequence_certificate():
    """N전담 4연속 O금지 → 개인 automaton 경로 소멸 certificate(분기 전 종료)."""
    nurses = _n_only(1) + [{"nurse_id": "g", "name": "g"}]
    cfg = _cfg(forbidden={"n0": {10: ["O"], 11: ["O"], 12: ["O"], 13: ["O"]}})
    node = diagnose_night_axis(nurses, cfg, 31)
    assert node.status == INFEASIBLE and node.certificate.kind == "sequence_path_empty"
    assert node.certificate.deficit == 1


def test_aggregate_supply_certificate_has_numbers():
    """2 N전담·수요2 → 월 최대공급 < 총수요, 수치 certificate."""
    node = diagnose_night_axis(_n_only(2), _cfg(night=2), 31)
    assert node.status == INFEASIBLE and node.certificate.kind == "night_supply_deficit"
    c = node.certificate
    assert c.demand == 62 and c.capacity < c.demand and c.deficit == c.demand - c.capacity


def test_feasible_axis_returns_feasible():
    """3 N전담·수요1 → N축 충족 가능 → 원인 아님."""
    assert diagnose_night_axis(_n_only(3), _cfg(night=1), 31).status == FEASIBLE


def test_large_pool_unknown_defers():
    """N-pool 과대 → 판정 유보(UNKNOWN), FEASIBLE 로 취급 금지."""
    assert diagnose_night_axis(_n_only(9), _cfg(night=1), 31).status == UNKNOWN


def test_verified_repairs_filters_by_reevaluation():
    """복구 후보를 N축 재판정으로 검증 — 실제로 푸는 것만 verified=True."""
    nurses = [{"nurse_id": "A", "name": "에이", "allowed_shifts": ["N"]},
              {"nurse_id": "B", "name": "비이", "allowed_shifts": ["N"]}]
    cfg = _cfg(forbidden={"A": {3: ["O"], 4: ["O"], 6: ["O"]}, "B": {5: ["N"]}})
    node = diagnose_night_axis(nurses, cfg, 10)
    reps = verified_repairs(node, nurses, cfg, 10)
    assert any(r["verified"] for r in reps)                # 검증된 복구 최소 1개
    # 야간 수요 -1 은 반드시 커버리지를 풀어줌
    assert any(r["action"] == "reduce_night_demand" and r["verified"] for r in reps)
