"""금지근무(banned-OFF) 개인모순 진단 — per-nurse 상태DAG 도달성(솔버 불필요).

①연속초과 ②회복막힘 ③고립1N 을 패턴 열거 없이 한 DP로 잡는다.
"""

from __future__ import annotations

from services.ontology_graph.lagrangian import (
    detect_banned_off_conflict,
    explain_infeasibility_from_config,
    per_nurse_night_feasible,
)


class _Nu:
    def __init__(self, nurse_id, name, allowed=None):
        self.nurse_id = nurse_id
        self.name = name
        self.allowed_shifts = allowed


# 3N2OFF(3연속 후 2OFF) + not_one_night(고립1N 금지) 활성 = 실병동 설정.
_RULES = {"two_offs_after_three_nig": True, "not_one_night": True}


def _cfg(forbidden, forced_off, **rules):
    c = {"daily_shift_requirements": {"D": 2, "E": 1, "N": 1},
         "off_days": 9, "max_nig_per_month": 15,
         "initial_constraints": {"forbidden": forbidden, "forced_off": forced_off}}
    c.update(_RULES)
    c.update(rules)
    return c


# ── per_nurse_night_feasible DP 단위 (①②③) ──────────────────────────────────

def test_dp_case1_four_consecutive_banned_infeasible():
    """① NNNN: 4연속 O금지 → 4연속 N > 최대3(3N2OFF) → 배열 없음."""
    assert per_nurse_night_feasible({10, 11, 12, 13}, set(), 31, _RULES) is False


def test_dp_case2_recovery_blocked_infeasible():
    """② NNN(10~12) + 하루 건너 O금지(14): 3N 회복 2OFF(13·14) 중 14가 금지근무 → 막힘."""
    assert per_nurse_night_feasible({10, 11, 12, 14}, set(), 31, _RULES) is False


def test_dp_case3_isolated_single_night_infeasible():
    """③ 고립 1N: 11일 O금지(강제N)인데 10·12 강제OFF → 1N 고립 → not_one_night 위반."""
    assert per_nurse_night_feasible({11}, {10, 12}, 31, _RULES) is False


def test_dp_feasible_when_pattern_ok():
    """정상: 2연속 O금지(N N) 후 여유 → 배열 존재."""
    assert per_nurse_night_feasible({10, 11}, set(), 31, _RULES) is True


# ── detector (N전담만 시퀀스, clash 는 전원) ─────────────────────────────────

def test_detect_case1_night_only():
    nurses = [_Nu("N1", "장세현", allowed=["N"])]
    cfg = _cfg(forbidden={"N1": {10: ["O"], 11: ["O"], 12: ["O"], 13: ["O"]}}, forced_off={})
    hits = detect_banned_off_conflict(nurses, cfg, 31)
    assert len(hits) == 1 and hits[0]["reason"] == "sequence_infeasible"
    assert hits[0]["is_night_only"] is True


def test_detect_clash_even_non_night_only():
    """셀 모순(banned-OFF ∩ forced_off)은 N전담 아니어도 잡음."""
    nurses = [_Nu("N1", "일반", allowed=["D", "E", "N"])]
    cfg = _cfg(forbidden={"N1": {5: ["O"]}}, forced_off={"N1": [5]})
    hits = detect_banned_off_conflict(nurses, cfg, 31)
    assert len(hits) == 1 and hits[0]["reason"] == "clash"


def test_detect_skips_non_night_only_sequence():
    """N전담 아니면 4연속 O금지여도 D/E로 채울 수 있어 시퀀스 모순 아님."""
    nurses = [_Nu("N1", "일반", allowed=["D", "E", "N"])]
    cfg = _cfg(forbidden={"N1": {10: ["O"], 11: ["O"], 12: ["O"], 13: ["O"]}}, forced_off={})
    assert detect_banned_off_conflict(nurses, cfg, 31) == []


def test_explain_classifies_and_offers_two_cards():
    from services.ontology_graph.mcs_trace import cause_to_resolution_options
    nurses = [_Nu("N1", "장세현", allowed=["N"])] + [_Nu(f"n{i}", f"n{i}") for i in range(5)]
    cfg = _cfg(forbidden={"N1": {10: ["O"], 11: ["O"], 12: ["O"], 13: ["O"]}}, forced_off={})
    e = explain_infeasibility_from_config(nurses, cfg, 31, year=2026, month=8)
    assert e.classification == "personal_infeasible" and e.top_family == "banned_wanted"
    assert "장세현" in e.certificate
    cards = cause_to_resolution_options(e.classification, e.top_family, e.targets)
    assert {c["option_id"] for c in cards} == {"cause:banned_release", "cause:allowed_add"}
