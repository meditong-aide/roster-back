"""금지근무(banned-OFF) × 강제OFF 개인모순 진단 — 증명된 산술(솔버 불필요)."""

from __future__ import annotations

from services.ontology_graph.lagrangian import (
    detect_banned_off_conflict,
    explain_infeasibility_from_config,
)


class _Nu:
    def __init__(self, nurse_id, name, allowed=None):
        self.nurse_id = nurse_id
        self.name = name
        self.allowed_shifts = allowed


def _cfg(forbidden, forced_off):
    return {
        "daily_shift_requirements": {"D": 2, "E": 1, "N": 1},
        "off_days": 9, "max_nig_per_month": 15,
        "initial_constraints": {"forbidden": forbidden, "forced_off": forced_off},
    }


def test_detects_banned_off_vs_forced_off_clash():
    """N전담 장세현: day0·1 OFF 금지(강제근무) ∧ 야간회복 강제OFF day0·1 → 모순."""
    nurses = [_Nu("N1", "장세현", allowed=["N"]), _Nu("N2", "표유진")]
    cfg = _cfg(forbidden={"N1": {0: ["O"], 1: ["O"]}}, forced_off={"N1": [0, 1]})
    hits = detect_banned_off_conflict(nurses, cfg)
    assert len(hits) == 1
    h = hits[0]
    assert h["nurse_id"] == "N1" and h["name"] == "장세현"
    assert h["clash_days"] == [0, 1]
    assert h["is_night_only"] is True
    assert h["allowed_shifts"] == ["N"]


def test_no_clash_when_banned_off_not_on_forced_day():
    """OFF 금지가 강제OFF 날과 안 겹치면 증명된 모순 아님 → 안 잡음."""
    nurses = [_Nu("N1", "장세현", allowed=["N"])]
    cfg = _cfg(forbidden={"N1": {5: ["O"]}}, forced_off={"N1": [0, 1]})
    assert detect_banned_off_conflict(nurses, cfg) == []


def test_non_off_ban_ignored():
    """D/E 금지(OFF 아님)는 강제근무가 아니므로 이 검사 대상 아님."""
    nurses = [_Nu("N1", "장세현", allowed=["N"])]
    cfg = _cfg(forbidden={"N1": {0: ["D", "E"]}}, forced_off={"N1": [0]})
    assert detect_banned_off_conflict(nurses, cfg) == []


def test_explain_classifies_personal_infeasible_banned_wanted():
    """explain 이 최우선으로 banned_wanted personal_infeasible 로 분류 + 이름·안내."""
    nurses = [_Nu("N1", "장세현", allowed=["N"])] + [_Nu(f"n{i}", f"n{i}") for i in range(5)]
    cfg = _cfg(forbidden={"N1": {0: ["O"], 1: ["O"]}}, forced_off={"N1": [0, 1]})
    e = explain_infeasibility_from_config(nurses, cfg, 31, year=2026, month=8)
    assert e.classification == "personal_infeasible"
    assert e.top_family == "banned_wanted"
    assert "장세현" in e.certificate and "금지근무" in e.certificate
    assert e.targets and e.targets[0]["nurse_id"] == "N1"


def test_resolution_offers_two_alternatives():
    """banned_wanted 원인 → 대안 2카드: (A) 금지근무 해제, (B) 근무유형 D/E 추가."""
    from services.ontology_graph.mcs_trace import cause_to_resolution_options
    targets = [{"nurse_id": "N1", "name": "장세현", "family": "banned_wanted",
                "clash_days": [0, 1], "banned_off_days": [0, 1],
                "allowed_shifts": ["N"], "is_night_only": True}]
    cards = cause_to_resolution_options("personal_infeasible", "banned_wanted", targets)
    assert len(cards) == 2
    by_id = {c["option_id"]: c for c in cards}
    # (A) 금지근무 해제 — 겹치는 날 지정
    rel = by_id["cause:banned_release"]
    assert rel["banned_wanted_release"] == [{"nurse_id": "N1", "days": [0, 1]}]
    assert rel["fix"]["mode"] == "auto_apply"
    # (B) 근무유형 D/E 추가
    add = by_id["cause:allowed_add"]
    assert add["allowed_shift_add"] == [{"nurse_id": "N1", "add": ["D", "E"]}]
