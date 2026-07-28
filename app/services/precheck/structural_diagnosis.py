"""G0 structural-feasibility diagnosis helpers.

레버(완화) 우선순위 이전에, 현재 infeasible이 구조 결핍인지 여부를
간단 규칙 기반으로 판정한다.
"""

from __future__ import annotations

from typing import Any, Dict, List


# NOTE(2026-07-22): 'capacity_structural' 라벨 제거.
#   NO_ASSIGNMENT(=배치 0건)은 infeasible 의 '결과/증상'인데 이게 구조 '원인'으로 둔갑해
#   primary_causes 를 채우고, 그 때문에 진짜 진단(UNDIAGNOSED probe)이 스킵되던 버그의 원흉.
#   genuine capacity 부족은 precheck(CAPACITY_TOTAL_SHORTAGE 등)이 이미 구체 메시지+해결책을
#   주므로 이 뭉뚱그린 라벨은 사용자 가치 0. 그래서 append 와 _STRUCTURAL_CODES 를 삭제한다.

_ROLE_CODES = {
    "ALLOWED_SHIFTS_ISOLATES_NURSE",
}

_HARD_IMPOSSIBLE_CODES = {
    "GRADE_MIN_SUM_EXCEEDS_NEED",
    "TEAM_MIN_EXCEEDS_GLOBAL_NEED",
    "CAPACITY_TOTAL_SHORTAGE",
    "GLOBAL_DAY_CAPACITY_SHORTAGE",
    "GLOBAL_SHIFT_ALLOWED_SHORTAGE",
}

_FIXED_CODES = {
    "FIXED_ASSIGN_EXCEEDS_NEED",
    "FIXED_ASSIGN_VIOLATES_ALLOWED",
    "FIXED_ASSIGN_BREAKS_TEAM_MIN",
}

_CARRYOVER_PREFIX = "PREV_"


def _collect_reason_codes(
    preflight_issues: List[Dict[str, Any]],
    violated_constraints: List[Dict[str, Any]],
) -> set[str]:
    out: set[str] = set()
    for x in preflight_issues or []:
        code = str(x.get("reason_code", "")).upper()
        if code:
            out.add(code)
    for x in violated_constraints or []:
        code = str(x.get("reason_code", "")).upper()
        if code:
            out.add(code)
    return out


def build_structural_diagnosis(
    *,
    preflight_issues: List[Dict[str, Any]] | None = None,
    violated_constraints: List[Dict[str, Any]] | None = None,
    conflict_cores: List[Dict[str, Any]] | None = None,
    pool_snapshot: Dict[str, Any] | None = None,
    applied_relaxations: List[str] | None = None,
) -> Dict[str, Any]:
    """구조 결핍 여부를 판정한 진단 객체를 반환한다.

    low-sample 안전 모드: 가중치 기반 점수 대신 규칙 기반 결정 트리로 판정.
    """
    issues = list(preflight_issues or [])
    violations = list(violated_constraints or [])
    cores = list(conflict_cores or [])
    pools = dict(pool_snapshot or {})
    relaxations = list(applied_relaxations or [])

    codes = _collect_reason_codes(issues, violations)
    shortages = list(pools.get("shortages") or [])
    patterns = {
        str(c.get("pattern", "")).lower()
        for c in cores
        if isinstance(c, dict)
    }

    causes: List[str] = []
    # 'capacity_structural' 은 결과-라벨이라 제거(위 NOTE 참조). 실제 원인 후보만 남긴다.
    if codes & _ROLE_CODES or any("allowed_shift_mask" in p or "n_only_vs_caps" in p for p in patterns):
        causes.append("role_isolation")
    if codes & _FIXED_CODES or any("fixed_assignment" in p or "initial_forbidden" in p for p in patterns):
        causes.append("fixed_lock")
    if any(code.startswith(_CARRYOVER_PREFIX) for code in codes) or any("carryover" in p or "boundary" in p for p in patterns):
        causes.append("carryover_lock")

    decision_trace: List[str] = []

    # RULE 1: 수학적 불가능 코드가 하나라도 있으면 즉시 구조 결핍
    if codes & _HARD_IMPOSSIBLE_CODES:
        mode = "hard_block_structural"
        decision_trace.append(
            "RULE_1_MATCH:HARD_IMPOSSIBLE_CODE:"
            + ",".join(sorted(codes & _HARD_IMPOSSIBLE_CODES))
        )
    # RULE 2: NO_ASSIGNMENT + shortage 동시 → 구조 결핍
    elif "NO_ASSIGNMENT" in codes and shortages:
        mode = "hard_block_structural"
        decision_trace.append("RULE_2_MATCH:NO_ASSIGNMENT_WITH_SHORTAGE")
    # RULE 3: role/fixed/carryover 충돌이 있으면 혼합 완화 필요
    elif any(x in causes for x in ("role_isolation", "fixed_lock", "carryover_lock")):
        mode = "mixed_relaxation_needed"
        decision_trace.append(
            "RULE_3_MATCH:MIXED_CAUSE:" + ",".join(sorted(causes))
        )
    # RULE 4: 기타는 완화 후보
    else:
        mode = "relaxation_candidate"
        decision_trace.append("RULE_4_MATCH:DEFAULT_RELAXATION_CANDIDATE")

    return {
        "mode": mode,
        "primary_causes": causes,
        "signals": {
            "reason_codes": sorted(codes),
            "shortage_count": len(shortages),
            "conflict_pattern_count": len(patterns),
            "applied_relaxation_count": len(relaxations),
        },
        "decision_trace": decision_trace,
    }
