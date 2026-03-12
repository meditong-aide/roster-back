"""CP-SAT 솔버와 RL 환경을 연결하는 브리지.

Synthetic NurseScenario를 CP-SAT 솔버가 이해하는 형식으로 변환하여
실제 최적화를 실행하고 결과 품질 지표를 반환한다.

이 모듈은 기존 cp_sat_basic.py / fallback_lex.py와의 인터페이스를 정의한다.
"""
from __future__ import annotations

import time
from datetime import date
from typing import TYPE_CHECKING

import numpy as np

if TYPE_CHECKING:
    from services.rl.dataset import NurseScenario
    from services.rl.env import SolverStageController


def build_nurse_data(scenario: "NurseScenario") -> list[dict]:
    """NurseScenario → CP-SAT 솔버 입력 형식 변환."""
    nurses = []
    for i, nu in enumerate(scenario.nurses):
        nurses.append({
            "nurse_id": nu.get("nurse_id", f"nurse_{i}"),
            "name": nu.get("name", f"간호사{i}"),
            "experience": nu.get("experience", 3),
            "is_night_nurse": nu.get("is_night_nurse"),
            "joining_date": nu.get("joining_date", date(scenario.year, scenario.month, 1)),
            "resignation_date": nu.get("resignation_date"),
            "db_id": nu.get("db_id", str(i)),
            "active": 1,
            "grade": None,
            "preceptor_id": None,
            "is_weekend_off": False,
        })
    return nurses


def build_config_data(scenario: "NurseScenario") -> dict:
    """NurseScenario → RosterConfig dict 형식 변환."""
    cfg = scenario.config_overrides.copy()
    n = scenario.n_nurses
    d = scenario.n_days

    # 기본값 설정
    cfg.setdefault("shift_types", ["D", "E", "N", "O"])
    cfg.setdefault("daily_shift_requirements", scenario.daily_requirements)
    cfg.setdefault("max_night_shifts_per_month", min(10, d // 3))
    cfg.setdefault("max_consecutive_work", 5)
    cfg.setdefault("off_days", max(7, d // 4))
    cfg.setdefault("min_exp_per_shift", 1)
    cfg.setdefault("two_offs_per_week", True)
    cfg.setdefault("three_seq_nig", True)
    cfg.setdefault("banned_day_after_eve", True)
    cfg.setdefault("grade_strategy", "BASE")
    cfg.setdefault("use_mid", False)
    cfg.setdefault("even_nights", True)
    cfg.setdefault("preceptee_on", False)
    cfg.setdefault("team_balance_on", False)
    cfg.setdefault("weekend_off_only_enable", False)
    cfg.setdefault("off_exception_cells", [])
    cfg.setdefault("off_exception_vacation_cells", [])
    cfg.setdefault("shift_definitions", _build_shift_defs(cfg["shift_types"]))

    return cfg


def _build_shift_defs(shift_types: list[str]) -> list[dict]:
    """기본 shift 정의 목록 생성."""
    shift_gb_map = {"D": "D", "E": "E", "N": "N", "O": "O", "M": "M", "W": "W"}
    type_map = {"D": "근무", "E": "근무", "N": "근무", "O": "OFF", "M": "근무", "W": "근무"}
    return [
        {
            "shift_id": code,
            "main_code": code,
            "codes": [code],
            "shift_gb": shift_gb_map.get(code, code),
            "type": type_map.get(code, "근무"),
        }
        for code in shift_types
    ]


def build_prefs_data(scenario: "NurseScenario") -> list[dict]:
    """선호도 행렬 → CP-SAT 입력 형식 변환."""
    prefs = []
    if scenario.preference_matrix is None:
        return prefs

    P = scenario.preference_matrix
    shift_types = scenario.config_overrides.get("shift_types", ["D", "E", "N", "O"])
    N, D, S = P.shape

    for n in range(N):
        nurse_id = scenario.nurses[n]["nurse_id"]
        for d in range(D):
            for s in range(min(S, len(shift_types))):
                val = float(P[n, d, s])
                if abs(val) > 0.01:
                    prefs.append({
                        "nurse_id": nurse_id,
                        "day_index": d,
                        "shift_id": shift_types[s],
                        "score": val,
                    })
    return prefs


def extract_result_metrics(
    roster_system,
    scenario: "NurseScenario",
) -> dict:
    """솔버 결과(RosterSystem)에서 품질 지표 추출."""
    from services.rl.reward import compute_night_fairness_score, compute_preference_satisfaction_score

    n_nurses = len(roster_system.nurses)
    n_days = roster_system.num_days
    shift_types = roster_system.config.shift_types

    try:
        night_idx = shift_types.index("N")
    except ValueError:
        night_idx = 2

    # 야간 근무 횟수 집계
    night_counts = []
    n_only_mask = []
    for n in range(n_nurses):
        count = int(np.sum(roster_system.roster[n, :, night_idx]))
        night_counts.append(count)
        raw = getattr(roster_system.nurses[n], "is_night_nurse", None)
        n_only_mask.append(raw == "N")

    fairness_score = compute_night_fairness_score(night_counts, n_only_mask)

    # 선호 만족도
    pref_score = 0.5
    try:
        P = roster_system.preference_matrix
        R = roster_system.roster
        pref_score = compute_preference_satisfaction_score(P, R)
    except Exception:
        pass

    return {
        "night_counts": night_counts,
        "night_fairness_score": fairness_score,
        "preference_score": pref_score,
    }


def run_synthetic_scenario_fallback_direct(
    scenario: "NurseScenario",
    controller: "SolverStageController",
    time_limit_seconds: int = 30,
) -> dict:
    """Synthetic scenario를 fallback optimizer에 직접 전달.

    RL 콜백이 항상 실행되도록, _optimize_with_enhanced_constraints를 건너뛰고
    optimize_fallback_lex_hard_first를 직접 호출한다.
    이 함수는 RL 학습/평가 전용이다.

    Production에서는 run_synthetic_scenario()를 사용할 것.
    """
    from services.cp_sat_basic import CPSATBasicEngine, generate_roster_cp_sat
    from services.cp_sat.fallback_lex import optimize_fallback_lex_hard_first
    from services.cp_sat.postprocess_off import postprocess_rebalance_off, postprocess_trim_extra_offs
    from services.constraints.grade_constraints import add_grade_constraints
    from services.objectives.team_objective import add_team_balance_objective_terms
    from timer import Timer

    import numpy as np

    nurses_data = build_nurse_data(scenario)
    config_data = build_config_data(scenario)

    engine = CPSATBasicEngine()
    try:
        config = engine.create_config_from_db(config_data)
    except Exception:
        return {"coverage_short": 99, "safety_violation_sum": 99, "solver_failed": True}

    from services.roster_system import RosterSystem
    from datetime import date

    nurses_objs = engine._create_nurses(nurses_data)
    rs = RosterSystem(
        nurses=nurses_objs,
        config=config,
        year=scenario.year,
        month=scenario.month,
        preferences=[],
    )

    grouped = config_data.get("shift_definitions", [])
    shift_id_to_type = {s["shift_id"]: s.get("type", "") for s in grouped}

    t_start = time.time()
    try:
        success = optimize_fallback_lex_hard_first(
            roster_system=rs,
            time_limit_seconds=time_limit_seconds,
            grouped=grouped,
            shift_type_map=shift_id_to_type,
            logger_prefix="[RL-Bridge]",
            timer_cls=Timer,
            add_preceptor_terms_fn=lambda **kw: [],
            add_team_balance_terms_fn=lambda **kw: [],
            add_grade_constraints_fn=lambda **kw: None,
            postprocess_rebalance_off_fn=lambda **kw: None,
            rl_stage_callback=controller,
        )
    except Exception as e:
        return {"coverage_short": 99, "safety_violation_sum": 99, "solver_failed": True, "error": str(e)}

    elapsed = time.time() - t_start

    coverage_short = controller.stage1_result.get("coverage_short", 0)
    safety_sum = controller.stage2_result.get("safety_violation_sum", 0)

    pref_score = 0.5
    fairness_score = 0.5
    try:
        metrics = extract_result_metrics(rs, scenario)
        pref_score = metrics["preference_score"]
        fairness_score = metrics["night_fairness_score"]
    except Exception:
        pass

    return {
        "coverage_short": coverage_short,
        "safety_violation_sum": safety_sum,
        "preference_score": pref_score,
        "night_fairness_score": fairness_score,
        "elapsed": elapsed,
        "feasible": success,
    }


def run_synthetic_scenario(
    scenario: "NurseScenario",
    controller: "SolverStageController",
    time_limit_seconds: int = 30,
) -> dict:
    """Synthetic scenario를 실제 CP-SAT 솔버로 실행.

    Args:
        scenario:              실행할 시나리오
        controller:            RL 콜백 컨트롤러
        time_limit_seconds:    총 시간 제한

    Returns:
        결과 지표 dict:
            coverage_short, safety_violation_sum,
            preference_score, night_fairness_score,
            roster_system (선택)
    """
    from services.cp_sat_basic import generate_roster_cp_sat

    nurses_data = build_nurse_data(scenario)
    config_data = build_config_data(scenario)
    prefs_data = build_prefs_data(scenario)

    # shift_manage_data: grouped 형식 (shift 정의 목록)
    shift_manage_data = config_data.get("shift_definitions", [])

    # RL 콜백 주입
    # generate_roster_cp_sat → CPSATBasicEngine.generate_roster
    # → _optimize_fallback_lex_hard_first → optimize_fallback_lex_hard_first
    # → rl_stage_callback 호출 (수정된 fallback_lex.py)
    t_start = time.time()

    try:
        result = generate_roster_cp_sat(
            nurses_data=nurses_data,
            prefs_data=prefs_data,
            config_data=config_data,
            year=scenario.year,
            month=scenario.month,
            shift_manage_data=shift_manage_data,
            time_limit_seconds=time_limit_seconds,
            randomize=False,
            seed=scenario.seed,
            grade_strategy="BASE",
            grade_config=None,
            rl_stage_callback=controller,
        )
    except TypeError:
        # rl_stage_callback 파라미터가 아직 전달되지 않는 경우 fallback
        result = generate_roster_cp_sat(
            nurses_data=nurses_data,
            prefs_data=prefs_data,
            config_data=config_data,
            year=scenario.year,
            month=scenario.month,
            shift_manage_data=shift_manage_data,
            time_limit_seconds=time_limit_seconds,
            randomize=False,
            seed=scenario.seed,
            grade_strategy="BASE",
            grade_config=None,
        )

    elapsed = time.time() - t_start

    if result is None:
        return {
            "coverage_short": scenario.n_nurses * scenario.n_days,
            "safety_violation_sum": 999,
            "preference_score": 0.0,
            "night_fairness_score": 0.0,
            "solver_failed": True,
            "elapsed": elapsed,
        }

    # 결과 지표 추출
    roster_system = result.get("roster_system") if isinstance(result, dict) else None
    coverage_short = 0
    safety_sum = 0
    pref_score = 0.5
    fairness_score = 0.5

    if roster_system is not None:
        metrics = extract_result_metrics(roster_system, scenario)
        pref_score = metrics["preference_score"]
        fairness_score = metrics["night_fairness_score"]

    # controller에서 stage 결과 수집 (fallback_lex에서 채워진 것)
    coverage_short = controller.stage1_result.get("coverage_short", 0)
    safety_sum = controller.stage2_result.get("safety_violation_sum", 0)

    return {
        "coverage_short": coverage_short,
        "safety_violation_sum": safety_sum,
        "preference_score": pref_score,
        "night_fairness_score": fairness_score,
        "elapsed": elapsed,
        "roster_system": roster_system,
    }
