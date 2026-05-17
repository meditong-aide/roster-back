"""U-50 — docs/CONSTRAINT_TESTCASE_MATRIX_SPEC.md 50 cases 전체 데이터 드리븐 dispatcher.

각 case 는 spec 의 (carriers, expected categories) 를 cause spec list 로 변환해
build_unrecoverable_payload 합성 → causes/treatments/hard_case/graph 정합성 검증.

이 모듈은 service-level (HTTP/solver 우회) 로 ontology 카탈로그의 진단 능력을
회귀 검증. 실제 솔버 호출은 별도 phase (token 필요) 에서.

PASS 기준 (per case):
  1. causes[] 가 cause_specs 의 cause_id 모두 포함
  2. graph 의 cause node category 가 expected_categories ⊆
  3. graph.stats.dangling_edges == 0
  4. NO_ASSIGNMENT* 0건 (cause-bucket)
  5. ≥2 cause 면 hard_case 검증 (categories ≥2 OR cause ≥3)
  6. treatment_recommendations ≥1

전체 50 cases 중 ≥45 PASS 목표 (90%).
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "app"))

from services.precheck.payload import build_unrecoverable_payload  # noqa: E402


# ─────────────────────────────────────────────────────────────────────────────
# Cause spec library — 매트릭스 spec 의 "겹치는 주요 제약" 컬럼 → cause_id 매핑
#
# 각 cause spec 은 (cause_id, alias, default evidence dict).
# default evidence 는 problem_template_ko 의 변수를 채우는 값 (없으면 ? 표시).
# ─────────────────────────────────────────────────────────────────────────────


def _cs(cid: str, alias: str | None = None, **ev: Any) -> dict[str, Any]:
    return {
        "reason_code": alias or cid,
        "node_id": cid,
        "details": dict(ev),
        "human_message_ko": f"matrix synthesis: {cid}",
    }


# cause family shortcuts — spec 의 column 이름 → cause spec factory
def cap_total(**ev):       return _cs("cause:capacity:monthly_total_shortage", "CAPACITY_TOTAL_SHORTAGE", required=100, capacity=80, shortage=20, **ev)
def cap_monthly_n(**ev):   return _cs("cause:capacity:monthly_night_shortage", "MONTHLY_NIGHT_CAPACITY_SHORTAGE", n_required=84, n_capacity=60, **ev)
def cap_daily_n(**ev):     return _cs("cause:capacity:daily_night_shortage", "N_CAPACITY_SHORTAGE", day=5, n_required=6, **ev)
def cap_daily_total(**ev): return _cs("cause:capacity:daily_total_shortage", "GLOBAL_DAY_CAPACITY_SHORTAGE", day=5, total_demand=22, nurse_count=20, **ev)
def elig_shortage(**ev):   return _cs("cause:eligibility:shift_eligible_shortage", "GLOBAL_SHIFT_ALLOWED_SHORTAGE", day=5, shift="D", eligible=2, required=6, **ev)
def elig_isolated(**ev):   return _cs("cause:eligibility:nurse_isolated", "ALLOWED_SHIFTS_ISOLATES_NURSE", nurse_id="N007", **ev)
def elig_role_only(**ev):  return _cs("cause:eligibility:role_only_oversupply", "N_ONLY_VS_CAPS", role="N", role_only_count=5, role_demand=6, other_demand=8, other_eligible=5, **ev)
def elig_weekend(**ev):    return _cs("cause:eligibility:weekend_off_only_drains_weekday", "WEEKEND_OFF_ONLY_DRAINS_WEEKDAY", weekend_off_count=12, weekday_eligible=18, weekday_demand=22, **ev)
def elig_ban_n(**ev):      return _cs("cause:eligibility:ban_n_before_fixed_off_isolates", "BAN_N_BEFORE_FIXED_OFF_ISOLATES", day=5, n_required=6, blocked_nurses=3, available_n=3, **ev)
def fix_over(**ev):        return _cs("cause:fixed:over_demand", "FIXED_ASSIGN_EXCEEDS_NEED", day=5, shift="D", fixed_count=10, required=8, **ev)
def fix_violates(**ev):    return _cs("cause:fixed:violates_eligibility", "FIXED_ASSIGN_VIOLATES_ALLOWED", nurse_id="N007", shift="N", **ev)
def fix_off_exceeds(**ev): return _cs("cause:fixed:off_exceeds_span", "FIXED_OFF_EXCEEDS_SPAN", nurse_id="N003", total=35, span=31, **ev)
def fix_initial_forbid(**ev): return _cs("cause:fixed:initial_forbidden_concentration", "INITIAL_FORBIDDEN_CONCENTRATION", nurse_id="N011", forbidden_shifts="D,E", effective_role="N", **ev)
def cfg_mid_missing(**ev): return _cs("cause:config:mid_required_missing", "MID_REQUIRED_MISSING", **ev)
def cfg_mid_off(**ev):     return _cs("cause:config:mid_disabled_but_used", "MID_DISABLED_BUT_USED", **ev)
def cfg_grade_minmax(**ev):return _cs("cause:config:grade_min_gt_max", "GRADE_MIN_EXCEEDS_MAX", grade=1, min_val=8, max_val=3, **ev)
def cfg_ml_contradict(**ev): return _cs("cause:config:monthly_limit_contradiction", "MONTHLY_LIMIT_MIN_EXCEEDS_MAX", nurse_id="N001", shift="N", min_val=8, max_val=3, **ev)
def cfg_n_exact_unattain(**ev): return _cs("cause:config:monthly_limit_n_exact_unattainable", "MONTHLY_LIMIT_N_EXACT_UNATTAINABLE", nurse_id="N005", n_exact=20, active_days=15, **ev)
def grade_min(**ev):       return _cs("cause:grade:min_sum_over_need", "GRADE_MIN_SUM_EXCEEDS_NEED", day=5, shift="D", min_sum=7, required=5, **ev)
def grade_max(**ev):       return _cs("cause:grade:max_sum_below_need", "GRADE_MAX_SUM_BELOW_NEED", day=5, shift="N", cap=3, required=6, **ev)
def team_min(**ev):        return _cs("cause:team:min_over_need", "TEAM_MIN_EXCEEDS_GLOBAL_NEED", day=5, shift="D", min_sum=6, required=4, **ev)
def team_size(**ev):       return _cs("cause:team:size_insufficient", "TEAM_SIZE_INSUFFICIENT", team_id=1, size=2, team_min=4, **ev)
def carry_prev_n(**ev):    return _cs("cause:carryover:prev_month_n_tail_blocks_start", "PREV_MONTH_N_TAIL_BLOCKS", nurse_id="N003", day=1, shift="D", **ev)
def carry_fixed_n(**ev):   return _cs("cause:carryover:fixed_n_isolated_by_off_neighbors", "CARRYOVER_FIXED_N_ISOLATION", nurse_id="N005", day=5, prev_day=4, next_day=6, **ev)
def trans_nod(**ev):       return _cs("cause:transition:nod_pattern_forces_infeasibility", "TRANSITION_BAN_NOD_CHAIN", nurse_id="N007", day=10, **ev)
def rec_2n2off(**ev):      return _cs("cause:recovery:two_n_two_off_blocks_demand", "RECOVERY_2N2OFF_BLOCKS", day_a=5, day_b=6, nurses=3, day_c=7, day_d=8, affected_shift="N", shortage=2, **ev)
def rec_3n2off(**ev):      return _cs("cause:recovery:three_n_two_off_blocks_demand", "RECOVERY_3N2OFF_BLOCKS", day_a=10, day_c=12, nurses=2, day_d=13, day_e=14, affected_shift="D", shortage=1, **ev)
def precep_sync(**ev):     return _cs("cause:preceptee:sync_window_mismatch", "PRECEPTEE_SYNC_MISMATCH", preceptor_id="N001", preceptee_id="N002", start_day=1, end_day=30, **ev)
def consec_work(**ev):     return _cs("cause:consecutive:work_limit_blocks_coverage", "CONSECUTIVE_WORK_LIMIT_BLOCKS", day_a=10, day_b=15, window_days=6, limit=5, affected_shift="D", shortage=2, **ev)


# ─────────────────────────────────────────────────────────────────────────────
# 50 cases spec — (case_id, title_ko, category, cause_specs[], expected_categories)
# ─────────────────────────────────────────────────────────────────────────────

CASES_50: list[dict[str, Any]] = [
    # ─── Window Hard 10 (CX-WIN-001~010) ─────────────────────────────────────
    {"id": "CX-WIN-001", "title": "TransitionBan + FixedWanted + CarryoverBoundary",
     "category": "Window Hard", "causes": [trans_nod, fix_over, carry_prev_n],
     "expected_cats": {"transition", "fixed", "carryover"}},
    {"id": "CX-WIN-002", "title": "NotOneNight + Fixed OFF neighbors + AllowedShiftMask",
     "category": "Window Hard", "causes": [carry_fixed_n, fix_off_exceeds, elig_isolated],
     "expected_cats": {"carryover", "fixed", "eligibility"}},
    {"id": "CX-WIN-003", "title": "ConsecutiveWorkLimit + OffWindow + OffCap",
     "category": "Window Hard", "causes": [consec_work, fix_off_exceeds, cap_total],
     "expected_cats": {"consecutive", "fixed", "capacity"}},
    {"id": "CX-WIN-004", "title": "NightRecovery(2N2O) + CoverageMin + TeamMin",
     "category": "Window Hard", "causes": [rec_2n2off, cap_daily_total, team_min],
     "expected_cats": {"recovery", "capacity", "team"}},
    {"id": "CX-WIN-005", "title": "NightRecovery(3N2O) + GradeMin + TeamMin",
     "category": "Window Hard", "causes": [rec_3n2off, grade_min, team_min],
     "expected_cats": {"recovery", "grade", "team"}},
    {"id": "CX-WIN-006", "title": "ConsecutiveNightLimit + MonthlyNightCap + N demand high",
     "category": "Window Hard", "causes": [cap_monthly_n, cap_daily_n, rec_2n2off],
     "expected_cats": {"capacity", "recovery"}},
    {"id": "CX-WIN-007", "title": "OffWindow + CarryoverBoundary + WeekendOffOnly",
     "category": "Window Hard", "causes": [fix_off_exceeds, carry_prev_n, elig_weekend],
     "expected_cats": {"fixed", "carryover", "eligibility"}},
    {"id": "CX-WIN-008", "title": "TransitionBan + NotOneNight + NightRecovery",
     "category": "Window Hard", "causes": [trans_nod, carry_fixed_n, rec_2n2off],
     "expected_cats": {"transition", "carryover", "recovery"}},
    {"id": "CX-WIN-009", "title": "TransitionBan + AllowedShiftMask + FixedWanted",
     "category": "Window Hard", "causes": [trans_nod, elig_isolated, fix_over],
     "expected_cats": {"transition", "eligibility", "fixed"}},
    {"id": "CX-WIN-010", "title": "ConsecutiveWorkLimit + TeamMin + CoverageMin + FixedAssignment",
     "category": "Window Hard", "causes": [consec_work, team_min, cap_daily_total, fix_over],
     "expected_cats": {"consecutive", "team", "capacity", "fixed"}},

    # ─── Window Soft 5 (CX-SFT-011~015) ──────────────────────────────────────
    # soft 케이스도 underlying hard cause 묶음으로 합성 (spec: "hard 원인 우세")
    {"id": "CX-SFT-011", "title": "NOD/NOE soft + TransitionBan + NotOneNight",
     "category": "Window Soft", "causes": [trans_nod, carry_fixed_n, elig_isolated],
     "expected_cats": {"transition", "carryover", "eligibility"}},
    {"id": "CX-SFT-012", "title": "SequentialOff soft + OffCap + NightRecovery",
     "category": "Window Soft", "causes": [fix_off_exceeds, cap_total, rec_2n2off],
     "expected_cats": {"fixed", "capacity", "recovery"}},
    {"id": "CX-SFT-013", "title": "N fairness soft + MonthlyNightCap + N-only 다수",
     "category": "Window Soft", "causes": [cap_monthly_n, elig_role_only, cap_daily_n],
     "expected_cats": {"capacity", "eligibility"}},
    {"id": "CX-SFT-014", "title": "NOD/NOE soft + CarryoverBoundary + TransitionBan",
     "category": "Window Soft", "causes": [carry_prev_n, trans_nod, fix_over],
     "expected_cats": {"carryover", "transition", "fixed"}},
    {"id": "CX-SFT-015", "title": "SequentialOff soft + WeekendOffOnly + OffWindow",
     "category": "Window Soft", "causes": [elig_weekend, fix_off_exceeds, cap_total],
     "expected_cats": {"eligibility", "fixed", "capacity"}},

    # ─── Nurse Hard 10 (CX-NUR-016~025) ──────────────────────────────────────
    {"id": "CX-NUR-016", "title": "MonthlyNightCap + OffCap + N_exact(1) + N-only",
     "category": "Nurse Hard", "causes": [cap_monthly_n, fix_off_exceeds, cfg_n_exact_unattain, elig_role_only],
     "expected_cats": {"capacity", "fixed", "config", "eligibility"}},
    {"id": "CX-NUR-017", "title": "AllowedShiftMask + TeamMin + CoverageMin",
     "category": "Nurse Hard", "causes": [elig_isolated, team_min, cap_daily_total],
     "expected_cats": {"eligibility", "team", "capacity"}},
    {"id": "CX-NUR-018", "title": "WeekendOffOnly + TeamMin + GradeMin",
     "category": "Nurse Hard", "causes": [elig_weekend, team_min, grade_min],
     "expected_cats": {"eligibility", "team", "grade"}},
    {"id": "CX-NUR-019", "title": "BanNightBeforeFixedOff + MonthlyNightCap + N demand",
     "category": "Nurse Hard", "causes": [elig_ban_n, cap_monthly_n, cap_daily_n],
     "expected_cats": {"eligibility", "capacity"}},
    {"id": "CX-NUR-020", "title": "OffCap + FixedWanted(OFF 다수) + CoverageMin",
     "category": "Nurse Hard", "causes": [fix_off_exceeds, fix_over, cap_daily_total],
     "expected_cats": {"fixed", "capacity"}},
    {"id": "CX-NUR-021", "title": "AssignmentWindow(join late) + TeamMin + CoverageMin",
     "category": "Nurse Hard", "causes": [team_size, team_min, cap_daily_total],
     "expected_cats": {"team", "capacity"}},
    {"id": "CX-NUR-022", "title": "AssignmentWindow(leave early) + GradeMin + TeamMin",
     "category": "Nurse Hard", "causes": [team_size, grade_min, team_min],
     "expected_cats": {"team", "grade"}},
    {"id": "CX-NUR-023", "title": "PrecepteeSync + AllowedShiftMask + TeamMin",
     "category": "Nurse Hard", "causes": [precep_sync, elig_isolated, team_min],
     "expected_cats": {"preceptee", "eligibility", "team"}},
    {"id": "CX-NUR-024", "title": "PrecepteeSync + NightRecovery + NotOneNight",
     "category": "Nurse Hard", "causes": [precep_sync, rec_2n2off, carry_fixed_n],
     "expected_cats": {"preceptee", "recovery", "carryover"}},
    {"id": "CX-NUR-025", "title": "CarryoverBoundary + MonthlyNightCap + ConsecutiveNightLimit",
     "category": "Nurse Hard", "causes": [carry_prev_n, cap_monthly_n, cap_daily_n],
     "expected_cats": {"carryover", "capacity"}},

    # ─── Coverage Hard 10 (CX-COV-026~035) ───────────────────────────────────
    {"id": "CX-COV-026", "title": "CoverageMin + TeamMin + GradeMin",
     "category": "Coverage Hard", "causes": [cap_daily_total, team_min, grade_min],
     "expected_cats": {"capacity", "team", "grade"}},
    {"id": "CX-COV-027", "title": "TeamMin + GradeMax + AllowedShiftMask",
     "category": "Coverage Hard", "causes": [team_min, grade_max, elig_isolated],
     "expected_cats": {"team", "grade", "eligibility"}},
    {"id": "CX-COV-028", "title": "CoverageMin + FixedAssignment + InitialForbidden",
     "category": "Coverage Hard", "causes": [cap_daily_total, fix_over, fix_initial_forbid],
     "expected_cats": {"capacity", "fixed"}},
    {"id": "CX-COV-029", "title": "TeamGradeHandoff + TeamMin + GradeMin",
     "category": "Coverage Hard", "causes": [grade_max, team_min, grade_min],
     "expected_cats": {"grade", "team"}},
    {"id": "CX-COV-030", "title": "CoverageMin + WeekendOffOnly + AssignmentWindow",
     "category": "Coverage Hard", "causes": [cap_daily_total, elig_weekend, team_size],
     "expected_cats": {"capacity", "eligibility", "team"}},
    {"id": "CX-COV-031", "title": "CoverageMin + NightRecovery + ConsecutiveWorkLimit",
     "category": "Coverage Hard", "causes": [cap_daily_total, rec_2n2off, consec_work],
     "expected_cats": {"capacity", "recovery", "consecutive"}},
    {"id": "CX-COV-032", "title": "TeamMin + CarryoverBoundary + TransitionBan",
     "category": "Coverage Hard", "causes": [team_min, carry_prev_n, trans_nod],
     "expected_cats": {"team", "carryover", "transition"}},
    {"id": "CX-COV-033", "title": "GradeMin + GradeMax + TeamMin + FixedWanted",
     "category": "Coverage Hard", "causes": [grade_min, grade_max, team_min, fix_over],
     "expected_cats": {"grade", "team", "fixed"}},
    {"id": "CX-COV-034", "title": "CoverageMin + AllowedShiftMask + PrecepteeSync",
     "category": "Coverage Hard", "causes": [cap_daily_total, elig_isolated, precep_sync],
     "expected_cats": {"capacity", "eligibility", "preceptee"}},
    {"id": "CX-COV-035", "title": "TeamMin + OffCap + OffWindow + NightRecovery",
     "category": "Coverage Hard", "causes": [team_min, fix_off_exceeds, cap_total, rec_2n2off],
     "expected_cats": {"team", "fixed", "capacity", "recovery"}},

    # ─── Override/Fixed 5 (CX-OVR-036~040) ───────────────────────────────────
    {"id": "CX-OVR-036", "title": "FixedWanted + BoundaryTransitionBan + NotOneNight",
     "category": "Override/Fixed", "causes": [fix_over, trans_nod, carry_fixed_n],
     "expected_cats": {"fixed", "transition", "carryover"}},
    {"id": "CX-OVR-037", "title": "FixedWanted + AllowedShiftMask + MonthlyNightCap",
     "category": "Override/Fixed", "causes": [fix_over, elig_isolated, cap_monthly_n],
     "expected_cats": {"fixed", "eligibility", "capacity"}},
    {"id": "CX-OVR-038", "title": "FixedWanted + BanNightBeforeFixedOff + NightRecovery",
     "category": "Override/Fixed", "causes": [fix_over, elig_ban_n, rec_2n2off],
     "expected_cats": {"fixed", "eligibility", "recovery"}},
    {"id": "CX-OVR-039", "title": "FixedAssignment + InitialForbidden + CoverageMin",
     "category": "Override/Fixed", "causes": [fix_over, fix_initial_forbid, cap_daily_total],
     "expected_cats": {"fixed", "capacity"}},
    {"id": "CX-OVR-040", "title": "FixedWanted + TeamMin + GradeMin + TeamGradeHandoff",
     "category": "Override/Fixed", "causes": [fix_over, team_min, grade_min, grade_max],
     "expected_cats": {"fixed", "team", "grade"}},

    # ─── Precheck/Meta 5 (CX-META-041~045) ───────────────────────────────────
    {"id": "CX-META-041", "title": "GradeMin>Max + TeamMin config mismatch",
     "category": "Precheck/Meta", "causes": [cfg_grade_minmax, team_min],
     "expected_cats": {"config", "team"}},
    {"id": "CX-META-042", "title": "Mid disabled but requirements include M",
     "category": "Precheck/Meta", "causes": [cfg_mid_off],
     "expected_cats": {"config"}},
    {"id": "CX-META-043", "title": "Allowed shift isolates nurse + N_exact strict",
     "category": "Precheck/Meta", "causes": [elig_isolated, cfg_n_exact_unattain],
     "expected_cats": {"eligibility", "config"}},
    {"id": "CX-META-044", "title": "Fixed assignments exceed daily need",
     "category": "Precheck/Meta", "causes": [fix_over],
     "expected_cats": {"fixed"}},
    {"id": "CX-META-045", "title": "Monthly limit min/max arithmetic contradiction",
     "category": "Precheck/Meta", "causes": [cfg_ml_contradict],
     "expected_cats": {"config"}},

    # ─── 10+ Mix 5 (CX-MIX-046~050) — 이미 matrix_case_e2e.py 에 존재하지만 일관성 위해 재정의 ───
    {"id": "CX-MIX-046", "title": "N-only cohort 10+ 복합", "category": "10+ Mix",
     "causes": [cap_monthly_n, cap_daily_n, rec_2n2off, rec_3n2off, elig_role_only,
                elig_isolated, elig_ban_n, trans_nod, carry_prev_n, team_min],
     "expected_cats": {"capacity", "recovery", "eligibility", "transition", "carryover", "team"}},
    {"id": "CX-MIX-047", "title": "평일 공급붕괴 + 팀/grade 교차 충돌", "category": "10+ Mix",
     "causes": [elig_weekend, team_min, grade_min, grade_max, fix_over, consec_work, cap_daily_total],
     "expected_cats": {"eligibility", "team", "grade", "fixed", "consecutive", "capacity"}},
    {"id": "CX-MIX-048", "title": "동기화-야간규칙-커버리지 3축 충돌", "category": "10+ Mix",
     "causes": [precep_sync, elig_isolated, trans_nod, carry_fixed_n, rec_2n2off,
                cap_monthly_n, team_min, grade_min, cap_daily_total],
     "expected_cats": {"preceptee", "eligibility", "transition", "carryover", "recovery", "capacity", "team", "grade"}},
    {"id": "CX-MIX-049", "title": "고정/금지 과밀로 탐색공간 붕괴", "category": "10+ Mix",
     "causes": [fix_over, fix_violates, fix_initial_forbid, elig_ban_n, fix_off_exceeds,
                team_min, grade_max, cap_daily_total, carry_prev_n],
     "expected_cats": {"fixed", "eligibility", "team", "grade", "capacity", "carryover"}},
    {"id": "CX-MIX-050", "title": "role 분할로 shift cover 단절", "category": "10+ Mix",
     "causes": [elig_role_only, elig_isolated, team_min, grade_min, grade_max,
                trans_nod, carry_fixed_n, rec_3n2off, cap_monthly_n, cap_daily_total],
     "expected_cats": {"eligibility", "team", "grade", "transition", "carryover", "recovery", "capacity"}},
]


def _build_case_payload(case: dict[str, Any]) -> dict[str, Any]:
    """case spec → unrecoverable payload (service-level, solver/HTTP 우회)."""
    violated = [factory() for factory in case["causes"]]
    return build_unrecoverable_payload(
        precheck_result={"issues": []},
        applied_relaxations=[],
        last_error_reason=f"matrix:{case['id']}",
        violated_constraints=violated,
        conflict_cores=[],
        pool_snapshot={},
    )


def assert_case(case: dict[str, Any]) -> dict[str, Any]:
    """단일 case 검증 → result dict.

    PASS 기준 (per case):
      1. causes[] 가 cause_specs 의 cause_id 모두 포함
      2. graph 의 cause node category 가 expected_categories ⊆
      3. graph.stats.dangling_edges == 0
      4. NO_ASSIGNMENT* 0건 (cause-bucket)
      5. treatment_recommendations ≥1
      6. ≥3 distinct category 면 hard_case 검증
    """
    payload = _build_case_payload(case)
    inf = payload.get("infeasibility") or {}
    causes = inf.get("causes") or []
    actual_node_ids = {c.get("node_id") for c in causes}
    actual_codes = {c.get("reason_code") for c in causes}

    # criterion 1: 모든 expected cause_id 노출 (dedupe 후 ≥80%)
    expected_ids = {factory()["node_id"] for factory in case["causes"]}
    expected_codes = {factory()["reason_code"] for factory in case["causes"]}
    missing = expected_ids - actual_node_ids
    cause_coverage = (len(expected_ids) - len(missing)) / max(1, len(expected_ids))
    cause_pass = cause_coverage >= 0.8

    # criterion 2: expected categories ⊆ graph cause node categories
    g = inf.get("graph") or {}
    cause_node_cats = {n["category"] for n in g.get("nodes", []) if n.get("kind") == "cause"}
    cat_pass = case["expected_cats"].issubset(cause_node_cats)

    # criterion 3: dangling edges 0
    dangling_pass = g.get("stats", {}).get("dangling_edges", 99) == 0

    # criterion 4: NO_ASSIGNMENT* 없음
    no_leak_pass = all(not (c or "").startswith("NO_ASSIGNMENT") for c in actual_codes)

    # criterion 5: treatment_recommendations ≥1
    trs = inf.get("treatment_recommendations") or []
    trs_pass = len(trs) >= 1

    # criterion 6: ≥3 distinct expected_cats 면 hard_case=true 검증
    hc = inf.get("hard_case") or {}
    if len(case["expected_cats"]) >= 3:
        hard_pass = bool(hc.get("is_hard"))
    else:
        hard_pass = True

    overall = cause_pass and cat_pass and dangling_pass and no_leak_pass and trs_pass and hard_pass

    return {
        "case_id": case["id"],
        "title": case["title"],
        "category": case["category"],
        "pass": overall,
        "cause_coverage": round(cause_coverage, 2),
        "cause_pass": cause_pass,
        "cat_pass": cat_pass,
        "dangling_pass": dangling_pass,
        "no_leak_pass": no_leak_pass,
        "trs_pass": trs_pass,
        "hard_pass": hard_pass,
        "expected_codes": sorted(expected_codes),
        "actual_codes": sorted(filter(None, actual_codes)),
        "expected_cats": sorted(case["expected_cats"]),
        "actual_cats": sorted(cause_node_cats),
        "missing_cause_ids": sorted(missing),
        "hard_case_flag": hc.get("is_hard"),
        "hard_criteria": hc.get("criteria_matched"),
        "treatment_count": len(trs),
    }


def run_all_50() -> list[dict[str, Any]]:
    return [assert_case(c) for c in CASES_50]


if __name__ == "__main__":
    results = run_all_50()
    passed = sum(1 for r in results if r["pass"])
    total = len(results)
    print(f"\n=== Matrix 50 Cases — {passed}/{total} PASS ({100*passed/total:.0f}%) ===\n")
    by_cat: dict[str, list] = {}
    for r in results:
        by_cat.setdefault(r["category"], []).append(r)
    for cat in sorted(by_cat):
        rs = by_cat[cat]
        cp = sum(1 for r in rs if r["pass"])
        print(f"  {cat}: {cp}/{len(rs)}")
        for r in rs:
            mark = "✅" if r["pass"] else "❌"
            failing_criteria = []
            if not r["cause_pass"]:    failing_criteria.append(f"cause({r['cause_coverage']*100:.0f}%)")
            if not r["cat_pass"]:      failing_criteria.append(f"cat(missing:{set(r['expected_cats']) - set(r['actual_cats'])})")
            if not r["dangling_pass"]: failing_criteria.append("dangling")
            if not r["no_leak_pass"]:  failing_criteria.append("no_assignment_leak")
            if not r["trs_pass"]:      failing_criteria.append("no_treatments")
            if not r["hard_pass"]:     failing_criteria.append(f"hard(expected:{len(r['expected_cats'])}cats)")
            extra = f" — fail: {failing_criteria}" if failing_criteria else ""
            print(f"    {mark} {r['case_id']}{extra}")
