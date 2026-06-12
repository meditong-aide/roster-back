"""Infeasible 검증 경로 회귀 — 정직한 증상/UNDIAGNOSED 계약.

이전 버전은 work_cells==0 시 휴리스틱(_build_infeasible_diagnosis /
_probe_first_grade_hard_blocker)으로 CAPACITY_TOTAL_SHORTAGE · N_CAPACITY_SHORTAGE
· GRADE_HARD_PROBE 같은 '그럴듯한 원인'을 추측해 반환했다. 그 휴리스틱은
실제 solver MUS 가 침묵하면 무고한 제약을 지목하거나 결과론적 '배정 0건'을
원인처럼 내놓는 구조였다 (test_conflict_core_real_solver.py 로 입증).

→ 휴리스틱 전부 제거. 이제 _validate_generated_roster 는:
  · work_cells==0      → [reason_code=NO_ASSIGNMENT] 증상 + evidence (가짜 원인 X)
  · day 하나 coverage 0 → [reason_code=UNDIAGNOSED] + evidence
  둘 다 roster_system._infeasible_empty=True 로 '실제 신호'를 세운다.
실제 cause 는 하류 build_unrecoverable_payload (pool snapshot · conflict_detector
· structural_diagnosis · CP-SAT MUS) 가 결정론적으로 만든다.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from services.roster_create_service import (
    _extract_unrecoverable_violated_constraints,
    _validate_generated_roster,
)

# 휴리스틱이 만들던 가짜 원인 라벨 — 더 이상 _validate_generated_roster 가 emit 하면 안 됨.
_FABRICATED_CAUSE_LABELS = (
    "CAPACITY_TOTAL_SHORTAGE",
    "N_CAPACITY_SHORTAGE",
    "GRADE_MAX_SUM_BELOW_NEED",
    "GRADE_HARD_PROBE",
    "GRADE_HARD_MAX_CAP_SHORTAGE",
    "MAX_CAP_SHORTAGE",
)


@dataclass
class _MockConfig:
    shift_types: list = field(default_factory=lambda: ["D", "E", "N", "O"])
    daily_shift_requirements: dict = field(default_factory=dict)
    daily_shift_requirements_by_day: Any = None
    daily_shift_requirements_max_by_day: Any = None
    max_night_shifts_per_month: int = 0
    off_first: bool = False
    global_monthly_off_days: int = 8
    standard_personal_off_days: int = 0
    shift_definitions: Any = None
    use_mid: bool = False


@dataclass
class _MockNurse:
    db_id: int = 1
    grade: int = 1
    allowed_shifts: list = field(default_factory=list)
    is_night_nurse: int = 0
    personal_off_adjustment: int = 0


@dataclass
class _MockRosterSystem:
    config: Any = None
    nurses: list = field(default_factory=list)
    num_days: int = 30
    join: list = field(default_factory=list)
    leave: list = field(default_factory=list)
    blocked_by_nurse: dict = field(default_factory=dict)
    grade_config: dict = field(default_factory=dict)
    _validator_evidence: dict = field(default_factory=dict)
    _ontology_last_reason: Any = None
    _infeasible_empty: bool = False
    shift_id_to_main: Any = None
    _constraint_pool_snapshot: dict = field(default_factory=lambda: {"shortages": []})


def _empty_generated(n_nurses: int, n_days: int) -> dict[str, list[str]]:
    """work_cells == 0 인 합성 generated (모든 셀이 OFF)."""
    return {f"N{i}": ["-"] * n_days for i in range(n_nurses)}


def _zero_day_generated(n_nurses: int, n_days: int, zero_day_idx: int) -> dict[str, list[str]]:
    """zero_day_idx 만 모든 nurse 가 OFF, 그 외 day 는 채워 work_cells > 0."""
    gen: dict[str, list[str]] = {}
    for i in range(n_nurses):
        cycle = ["D", "E", "N"]
        schedule = [cycle[(i + d) % 3] for d in range(n_days)]
        schedule[zero_day_idx] = "-"
        gen[f"N{i}"] = schedule
    return gen


# ─────────────────────────────────────────────────────────────────────────
# work_cells==0 → 정직한 NO_ASSIGNMENT 증상 + evidence (가짜 원인 추측 금지)
# ─────────────────────────────────────────────────────────────────────────
def test_work_cells_zero_returns_symptom_not_fabricated_cause():
    cfg = _MockConfig(daily_shift_requirements={"D": 5, "E": 5, "N": 5, "O": 0})
    nurses = [_MockNurse(db_id=i, allowed_shifts=["D", "E", "N", "O"]) for i in range(5)]
    rs = _MockRosterSystem(config=cfg, nurses=nurses, num_days=30, join=[0] * 5, leave=[29] * 5)

    err = _validate_generated_roster(_empty_generated(5, 30), rs)

    assert err is not None
    assert "[reason_code=NO_ASSIGNMENT]" in err
    assert "evidence=" in err          # 증상에 evidence 동반
    assert "DAY_ZERO_COVERAGE" not in err
    # 핵심: 휴리스틱이 만들던 가짜 원인 라벨은 절대 emit 하지 않는다.
    for banned in _FABRICATED_CAUSE_LABELS:
        assert banned not in err, f"가짜 원인 라벨 {banned} 이 재등장하면 안 됨 / 실제: {err}"


def test_work_cells_zero_sets_infeasible_empty_signal():
    """soft-fallback 자동재시도가 의존하는 '실제 신호' 플래그가 세워져야 한다."""
    cfg = _MockConfig(daily_shift_requirements={"D": 1, "E": 1, "N": 5, "O": 0})
    nurses = [_MockNurse(db_id=i, allowed_shifts=["D", "E", "N", "O"]) for i in range(10)]
    rs = _MockRosterSystem(config=cfg, nurses=nurses, num_days=30, join=[0] * 10, leave=[29] * 10)

    _validate_generated_roster(_empty_generated(10, 30), rs)

    assert rs._infeasible_empty is True


def test_grade_max_block_no_longer_fabricates_grade_probe():
    """grade_max 가 빡빡해도 _validate 가 GRADE_HARD probe 를 지어내지 않는다.
    (원인 추측은 하류 구조진단 담당). 증상만 정직하게 반환."""
    cfg = _MockConfig(daily_shift_requirements={"D": 0, "E": 0, "N": 2, "O": 0})
    nurses = [_MockNurse(db_id=i, grade=1, allowed_shifts=["D", "E", "N", "O"]) for i in range(5)]
    rs = _MockRosterSystem(
        config=cfg, nurses=nurses, num_days=30, join=[0] * 5, leave=[29] * 5,
        grade_config={"constraints_max_json": {"N": {1: 0}}},
    )

    err = _validate_generated_roster(_empty_generated(5, 30), rs)

    assert err is not None
    assert "[reason_code=NO_ASSIGNMENT]" in err
    for banned in _FABRICATED_CAUSE_LABELS:
        assert banned not in err, f"가짜 원인 라벨 {banned} 재등장 금지 / 실제: {err}"


# ─────────────────────────────────────────────────────────────────────────
# day-zero (특정일 coverage 0) → UNDIAGNOSED sentinel + evidence dump
# ─────────────────────────────────────────────────────────────────────────
def test_day_zero_path_emits_undiagnosed_with_evidence():
    cfg = _MockConfig(daily_shift_requirements={"D": 2, "E": 2, "N": 1, "O": 0})
    nurses = [_MockNurse(db_id=i, allowed_shifts=["D", "E", "N", "O"]) for i in range(10)]
    rs = _MockRosterSystem(config=cfg, nurses=nurses, num_days=30, join=[0] * 10, leave=[29] * 10)

    err = _validate_generated_roster(_zero_day_generated(10, 30, zero_day_idx=5), rs)

    assert err is not None
    assert "UNDIAGNOSED" in err, f"기대: UNDIAGNOSED sentinel / 실제: {err}"
    assert "DAY_ZERO_COVERAGE" not in err
    assert "day=6" in err          # 1-indexed (zero_day_idx=5 → day 6)
    assert "evidence=" in err
    # day-zero 도 가짜 원인을 지어내지 않는다.
    for banned in _FABRICATED_CAUSE_LABELS:
        assert banned not in err, f"가짜 원인 라벨 {banned} 재등장 금지 / 실제: {err}"
    assert rs._infeasible_empty is True


# ─────────────────────────────────────────────────────────────────────────
# 증상/원인 분리 게이트 (보존) — NO_ASSIGNMENT 은 symptom, cause-bucket 진입 금지
# ─────────────────────────────────────────────────────────────────────────
def test_no_assignment_4axis_not_emitted_anymore():
    rs = _MockRosterSystem(
        config=_MockConfig(),
        nurses=[],
        _constraint_pool_snapshot={"shortages": [{"pool_id": "team_pool:1:D", "shortage": 3}]},
    )
    msg = "[reason_code=NO_ASSIGNMENT] | [reason_code=CAPACITY_TOTAL_SHORTAGE]"
    items = _extract_unrecoverable_violated_constraints(rs, generated=None, validation_error=msg)
    codes = {x.get("reason_code") for x in items}
    for banned in (
        "NO_ASSIGNMENT_CAPACITY", "NO_ASSIGNMENT_ELIGIBILITY",
        "NO_ASSIGNMENT_FIXED", "NO_ASSIGNMENT_CARRYOVER",
    ):
        assert banned not in codes, f"4-axis label {banned} should be blocked"


def test_no_assignment_raw_is_symptom_not_cause():
    from services.precheck.cause_symptom_classifier import classify, split_violations
    assert classify("NO_ASSIGNMENT") == "symptom"
    violations = [
        {"reason_code": "NO_ASSIGNMENT", "details": {}},
        {"reason_code": "CAPACITY_TOTAL_SHORTAGE", "details": {}},
    ]
    causes, symptoms, _ = split_violations(violations)
    cause_codes = {c.get("reason_code") for c in causes}
    sym_codes = {s.get("reason_code") for s in symptoms}
    assert "NO_ASSIGNMENT" not in cause_codes
    assert "NO_ASSIGNMENT" in sym_codes
    assert "CAPACITY_TOTAL_SHORTAGE" in cause_codes
