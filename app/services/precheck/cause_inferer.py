"""MUS conflict_core / member name → cause_id 자동 추론.

목표 (사용자 핵심 요구 "온톨로지 전부 완성"):
  ontology v4 catalog 의 28 cause 중 산술 detector 가 직접 만들지 않는
  솔버 MUS 영역 (carryover/transition/recovery/preceptee/consecutive 등) 도
  runtime 에서 cause_id 로 자동 분류되도록.

흐름:
  conflict_cores (HardAssumptionRegistry.extract_conflict_cores 출력)
    → core.pattern (예: "cpsat_mus:transition_ban") 매칭
    → 매칭 실패 시 core.members[0] 의 name prefix 매칭
    → cause_id 결정 (ontology lookup) → cause dict {reason_code, node_id, details, ...}

규칙:
  - 같은 cause_id 가 여러 core 에서 나와도 dedup
  - 매칭 실패한 core 는 silent skip (caller 가 raw cores 별도 처리)
  - reason_code 는 cause 의 첫 번째 alias (legacy 호환). alias 없으면 cause_id 그대로.
"""

from __future__ import annotations

from typing import Any, Optional

from services.semantics.ontology import (
    ConstraintOntology,
    OntologyCause,
    get_default_ontology,
)


# MUS pattern 값 (hard_assumption.py 의 cpsat_mus:<x>) → cause_id
# 값은 cp_sat_basic.py + constraints/*.py 의 add_hard meta 에서 추출.
_PATTERN_TO_CAUSE: dict[str, str] = {
    "carryover_boundary":      "cause:carryover:prev_month_n_tail_blocks_start",
    "not_one_night":           "cause:carryover:fixed_n_isolated_by_off_neighbors",
    "transition_ban":          "cause:transition:nod_pattern_forces_infeasibility",
    "recovery_2n2off":         "cause:recovery:two_n_two_off_blocks_demand",
    "recovery_3n2off":         "cause:recovery:three_n_two_off_blocks_demand",
    "max_consecutive_work":    "cause:consecutive:work_limit_blocks_coverage",
    "coverage_min":            "cause:capacity:daily_total_shortage",
    "allowed_shift_mask":      "cause:eligibility:nurse_isolated",
    "forbidden_shift":         "cause:eligibility:nurse_isolated",
    "grade_max":               "cause:grade:max_sum_below_need",
    "grade_min":               "cause:grade:min_sum_over_need",
    "handoff_adj":             "cause:grade:max_sum_below_need",
    "handoff_same":            "cause:grade:max_sum_below_need",
    "initial_forbidden":       "cause:fixed:violates_eligibility",
    "consecutive_night_cap":   "cause:capacity:monthly_night_shortage",
    "max_night":               "cause:capacity:monthly_night_shortage",
    "off_cap":                 "cause:fixed:off_exceeds_span",
    "off_window_requirement":  "cause:fixed:off_exceeds_span",
    "team_min":                "cause:team:min_over_need",
    "grade_minmax_conflict":   "cause:config:grade_min_gt_max",
}


# member name prefix → cause_id (pattern 누락 시 fallback)
_MEMBER_PREFIX_TO_CAUSE: dict[str, str] = {
    "FixedCell":                 "cause:fixed:over_demand",
    "FixedCellBan":              "cause:fixed:violates_eligibility",
    "SpecialShiftBanW":          "cause:eligibility:nurse_isolated",
    "InitialForbidden":          "cause:fixed:violates_eligibility",
    "TransitionBanN2D":          "cause:transition:nod_pattern_forces_infeasibility",
    "TransitionBanE2D":          "cause:transition:nod_pattern_forces_infeasibility",
    "TransitionBanN2E":          "cause:transition:nod_pattern_forces_infeasibility",
    "AllowedShiftMaskBan":       "cause:eligibility:nurse_isolated",
    "CarryoverRecovery":         "cause:carryover:prev_month_n_tail_blocks_start",
    "NotOneNight":               "cause:carryover:fixed_n_isolated_by_off_neighbors",
    "MaxConsecutiveWorkWindow":  "cause:consecutive:work_limit_blocks_coverage",
    "ConsecutiveNightCap":       "cause:capacity:monthly_night_shortage",
    "MaxNight":                  "cause:capacity:monthly_night_shortage",
    "OffCap":                    "cause:fixed:off_exceeds_span",
    "OffWindowRequirement":      "cause:fixed:off_exceeds_span",
    "CoverageMin":               "cause:capacity:daily_total_shortage",
    "GradeMin":                  "cause:grade:min_sum_over_need",
    "GradeMax":                  "cause:grade:max_sum_below_need",
    "TeamMin":                   "cause:team:min_over_need",
    "HandoffSame":               "cause:grade:max_sum_below_need",
    "HandoffAdj":                "cause:grade:max_sum_below_need",
    "Recovery2N2OFF":            "cause:recovery:two_n_two_off_blocks_demand",
    "Recovery3N2OFF":            "cause:recovery:three_n_two_off_blocks_demand",
    "MonthlyLimit":              "cause:config:monthly_limit_contradiction",
}


def _strip_pattern_prefix(pattern: Optional[str]) -> str:
    """`cpsat_mus:transition_ban` → `transition_ban`."""
    if not pattern:
        return ""
    p = str(pattern).strip()
    if p.startswith("cpsat_mus:"):
        return p[len("cpsat_mus:"):]
    return p


def _cause_to_reason_dict(
    cause: OntologyCause,
    *,
    source: str,
    pattern: str = "",
    member_sample: Optional[list[Any]] = None,
) -> dict[str, Any]:
    """OntologyCause → cause-dict (split_violations 가 cause-bucket 에 넣을 형태)."""
    reason_code = cause.aliases[0] if cause.aliases else cause.cause_id
    return {
        "reason_code": reason_code,
        "node_id": cause.cause_id,
        "details": {
            "source": source,
            "pattern": pattern,
            "member_sample": list(member_sample or [])[:3],
        },
        "human_message_ko": (cause.problem_template_ko or "")[:200] or None,
    }


def infer_cause_from_conflict_core(
    core: dict[str, Any],
    ontology: Optional[ConstraintOntology] = None,
) -> Optional[dict[str, Any]]:
    """단일 conflict_core → cause dict (없으면 None).

    1. core["pattern"] (예: "cpsat_mus:transition_ban") → _PATTERN_TO_CAUSE lookup
    2. fallback: core["members"][0] 의 prefix → _MEMBER_PREFIX_TO_CAUSE lookup
    """
    if not isinstance(core, dict):
        return None
    onto = ontology or get_default_ontology()

    pattern_stripped = _strip_pattern_prefix(core.get("pattern"))
    members = core.get("members") or []

    cause_id: Optional[str] = _PATTERN_TO_CAUSE.get(pattern_stripped)

    if cause_id is None and members:
        first = str(members[0])
        # 가장 긴 prefix match (e.g. ConsecutiveNightCapEdge 가 ConsecutiveNightCap 보다 먼저)
        for prefix in sorted(_MEMBER_PREFIX_TO_CAUSE, key=len, reverse=True):
            if first.startswith(prefix):
                cause_id = _MEMBER_PREFIX_TO_CAUSE[prefix]
                break

    if cause_id is None:
        return None

    cause = onto.get_cause(cause_id)
    if cause is None:
        return None

    return _cause_to_reason_dict(
        cause,
        source="conflict_core_inference",
        pattern=pattern_stripped or "",
        member_sample=members,
    )


def infer_causes_from_cores(
    conflict_cores: list[dict[str, Any]] | None,
    ontology: Optional[ConstraintOntology] = None,
) -> list[dict[str, Any]]:
    """conflict_cores list → cause dict list. 같은 cause_id 중복 dedup."""
    onto = ontology or get_default_ontology()
    seen_node_ids: set[str] = set()
    out: list[dict[str, Any]] = []
    for core in conflict_cores or []:
        cause = infer_cause_from_conflict_core(core, ontology=onto)
        if cause is None:
            continue
        node_id = cause.get("node_id")
        if not node_id or node_id in seen_node_ids:
            continue
        seen_node_ids.add(node_id)
        out.append(cause)
    return out
