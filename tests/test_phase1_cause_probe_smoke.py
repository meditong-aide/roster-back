"""Phase 1 smoke integration tests — DAY_ZERO_COVERAGE 라벨 제거 + UNDIAGNOSED sentinel 도입의 회귀 방어.

검증 흐름:
  generate → (infeasible) → _validate_generated_roster → cause probe (capacity / grade / undiagnosed) → reason_code.

Phase 1 변경 회귀 방어 포인트:
  1. DAY_ZERO_COVERAGE 라벨이 어떤 경로에서도 노출되지 않는다.
  2. day-zero 발견 시 cause probe 4축을 강제 실행한다 (capacity → grade → undiagnosed).
  3. UNDIAGNOSED sentinel 은 모든 probe 가 침묵하는 경우에만 노출되며 evidence dump 를 포함한다.
  4. _extract_unrecoverable_violated_constraints 의 NO_ASSIGNMENT 4축 분해가 DAY_ZERO_COVERAGE 키워드 없이도 동작.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from services.roster_create_service import (
    _extract_unrecoverable_violated_constraints,
    _validate_generated_roster,
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
    _grade_hard_probe_msg: Any = None
    _validator_evidence: dict = field(default_factory=dict)
    _ontology_last_reason: Any = None
    shift_id_to_main: Any = None
    _constraint_pool_snapshot: dict = field(default_factory=lambda: {"shortages": []})


def _empty_generated(n_nurses: int, n_days: int) -> dict[str, list[str]]:
    """work_cells == 0 인 합성 generated (모든 셀이 OFF)."""
    return {f"N{i}": ["-"] * n_days for i in range(n_nurses)}


def _zero_day_generated(n_nurses: int, n_days: int, zero_day_idx: int) -> dict[str, list[str]]:
    """zero_day_idx 만 모든 nurse 가 OFF, 그 외 day 는 적당히 채워 work_cells > 0."""
    gen: dict[str, list[str]] = {}
    for i in range(n_nurses):
        # 단순 round-robin D/E/N
        cycle = ["D", "E", "N"]
        schedule = [cycle[(i + d) % 3] for d in range(n_days)]
        schedule[zero_day_idx] = "-"
        gen[f"N{i}"] = schedule
    return gen


# ─────────────────────────────────────────────────────────────────────────
# Case 1: work_cells==0 경로 — 월 총 수요 > 공급 → CAPACITY_TOTAL_SHORTAGE
# ─────────────────────────────────────────────────────────────────────────
def test_capacity_total_shortage_emitted_on_work_cells_zero():
    cfg = _MockConfig(
        daily_shift_requirements={"D": 5, "E": 5, "N": 5, "O": 0},
        global_monthly_off_days=8,
    )
    nurses = [_MockNurse(db_id=i, allowed_shifts=["D", "E", "N", "O"]) for i in range(5)]
    rs = _MockRosterSystem(
        config=cfg, nurses=nurses, num_days=30,
        join=[0] * 5, leave=[29] * 5,
    )
    # 5 nurses * (30-8) = 110 capacity; 15*30 = 450 required → CAPACITY_TOTAL_SHORTAGE
    generated = _empty_generated(5, 30)
    err = _validate_generated_roster(generated, rs)
    assert err is not None
    assert "CAPACITY_TOTAL_SHORTAGE" in err, f"기대: CAPACITY_TOTAL_SHORTAGE 포함 / 실제: {err}"
    # Phase 1 회귀: 옛 라벨은 절대 노출 X
    assert "DAY_ZERO_COVERAGE" not in err


# ─────────────────────────────────────────────────────────────────────────
# Case 2: work_cells==0 경로 — N 수요 > N capacity → N_CAPACITY_SHORTAGE
# ─────────────────────────────────────────────────────────────────────────
def test_n_capacity_shortage_emitted_on_work_cells_zero():
    cfg = _MockConfig(
        daily_shift_requirements={"D": 1, "E": 1, "N": 5, "O": 0},
        max_night_shifts_per_month=8,
        global_monthly_off_days=8,
    )
    nurses = [_MockNurse(db_id=i, allowed_shifts=["D", "E", "N", "O"]) for i in range(10)]
    rs = _MockRosterSystem(
        config=cfg, nurses=nurses, num_days=30,
        join=[0] * 10, leave=[29] * 10,
    )
    # total: 7*30=210, capacity 10*22=220 → 통과
    # n: 5*30=150 > 10*8=80 → N_CAPACITY_SHORTAGE
    generated = _empty_generated(10, 30)
    err = _validate_generated_roster(generated, rs)
    assert err is not None
    assert "N_CAPACITY_SHORTAGE" in err, f"기대: N_CAPACITY_SHORTAGE 포함 / 실제: {err}"
    assert "DAY_ZERO_COVERAGE" not in err


# ─────────────────────────────────────────────────────────────────────────
# Case 3: day-zero 경로 — capacity 부족 시 day-zero 발견 후 cause probe 가 CAPACITY 잡음
# ─────────────────────────────────────────────────────────────────────────
def test_day_zero_path_falls_through_to_capacity_diag():
    cfg = _MockConfig(
        daily_shift_requirements={"D": 5, "E": 5, "N": 5, "O": 0},
        global_monthly_off_days=8,
    )
    nurses = [_MockNurse(db_id=i, allowed_shifts=["D", "E", "N", "O"]) for i in range(5)]
    rs = _MockRosterSystem(
        config=cfg, nurses=nurses, num_days=30,
        join=[0] * 5, leave=[29] * 5,
    )
    # work_cells > 0 (zero_day=10 만 비움) 이지만, 10 일 day-zero 가 트리거
    # capacity 부족이라 _build_infeasible_diagnosis 가 CAPACITY_TOTAL_SHORTAGE 반환
    generated = _zero_day_generated(5, 30, zero_day_idx=10)
    err = _validate_generated_roster(generated, rs)
    assert err is not None
    assert "CAPACITY_TOTAL_SHORTAGE" in err
    assert "DAY_ZERO_COVERAGE" not in err
    assert "UNDIAGNOSED" not in err  # capacity 가 잡혔으니 UNDIAGNOSED 로 가지 않음


# ─────────────────────────────────────────────────────────────────────────
# Case 4: day-zero 경로 — 모든 cause probe 침묵 → UNDIAGNOSED sentinel + evidence dump
# ─────────────────────────────────────────────────────────────────────────
def test_day_zero_path_emits_undiagnosed_when_all_probes_silent():
    cfg = _MockConfig(
        daily_shift_requirements={"D": 2, "E": 2, "N": 1, "O": 0},
        global_monthly_off_days=8,
    )
    # 10 nurses, capacity 넉넉, grade_config 없음 → 모든 probe 침묵
    nurses = [_MockNurse(db_id=i, allowed_shifts=["D", "E", "N", "O"]) for i in range(10)]
    rs = _MockRosterSystem(
        config=cfg, nurses=nurses, num_days=30,
        join=[0] * 10, leave=[29] * 10,
    )
    generated = _zero_day_generated(10, 30, zero_day_idx=5)
    err = _validate_generated_roster(generated, rs)
    assert err is not None
    assert "UNDIAGNOSED" in err, f"기대: UNDIAGNOSED sentinel / 실제: {err}"
    assert "DAY_ZERO_COVERAGE" not in err
    # evidence dump 포함 검증
    assert "day=6" in err  # 1-indexed (zero_day_idx=5 → day 6)
    assert "evidence=" in err


# ─────────────────────────────────────────────────────────────────────────
# Case 5 (US-10): NO_ASSIGNMENT* 결과 라벨은 cause-bucket 진입 금지.
# 미배정 cell 자체는 결과(symptom) 이며 cause 가 아니다. 진짜 cause 는
# team_grade_precheck 산술 detector + cause_inferer MUS 추론이 발급한다.
# ─────────────────────────────────────────────────────────────────────────
def test_no_assignment_4axis_not_emitted_anymore():
    """NO_ASSIGNMENT 분해 (4 축) 가 더 이상 reason_code 로 emit 되지 않아야 한다."""
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
    # 단 raw NO_ASSIGNMENT 자체는 솔버 raw output 으로 들어올 수 있고,
    # 그 경우는 classifier 가 symptom-bucket 으로 분리 처리한다.


def test_no_assignment_raw_is_symptom_not_cause():
    """raw NO_ASSIGNMENT 가 들어와도 cause-bucket 에는 들어가지 않는다."""
    from services.precheck.cause_symptom_classifier import classify, split_violations
    # 1) classifier 단독 검증
    assert classify("NO_ASSIGNMENT") == "symptom"
    # 2) split_violations 분리 검증
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


# ─────────────────────────────────────────────────────────────────────────
# Case 6: day-zero 경로 — grade_max 빡빡 → GRADE_HARD_PROBE 노출
# ─────────────────────────────────────────────────────────────────────────
def test_grade_hard_probe_emitted_on_grade_max_block():
    """work_cells==0 경로에서 _probe_first_grade_hard_blocker 가 grade_max 빡빡함을 잡아야."""
    cfg = _MockConfig(
        daily_shift_requirements={"D": 0, "E": 0, "N": 2, "O": 0},
        global_monthly_off_days=8,
    )
    # 5 nurses 모두 grade=1. grade_max[N][1]=0 → 야간 grade=1 0명 허용 → req=2 불가
    nurses = [_MockNurse(db_id=i, grade=1, allowed_shifts=["D", "E", "N", "O"]) for i in range(5)]
    rs = _MockRosterSystem(
        config=cfg, nurses=nurses, num_days=30,
        join=[0] * 5, leave=[29] * 5,
        grade_config={"constraints_max_json": {"N": {1: 0}}},
    )
    # capacity: 5*22=110, 2*30=60 required → capacity OK
    # max_night 미설정 → N_CAPACITY check skip
    # grade probe 가 잡아야: cap=0 < req=2
    generated = _empty_generated(5, 30)
    err = _validate_generated_roster(generated, rs)
    assert err is not None
    # work_cells==0 경로에선 _probe_first_grade_hard_blocker 결과가 메시지에 포함됨
    # (정확한 키워드는 GRADE_HARD_MAX_CAP_SHORTAGE / GRADE_HARD_ACTIVE_SHORTAGE 등 probe 결과)
    assert "GRADE_HARD" in err or "MAX_CAP_SHORTAGE" in err, f"기대: grade probe 결과 / 실제: {err}"
    assert "DAY_ZERO_COVERAGE" not in err
