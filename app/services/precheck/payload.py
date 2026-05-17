"""infeasibility 응답 페이로드 빌더.

precheck 결과 + (선택) 솔버 후 violated_constraints를 결합해 응답 최상위
`infeasibility` 객체를 만든다. HTTP 200/500 양쪽에서 같은 schema로 사용.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from services.precheck.messaging import (
    build_summary_message,
    humanize_all,
    summarize_violation_family,
)
from services.precheck.fix_plan import build_fix_plan
from services.precheck.structural_diagnosis import build_structural_diagnosis
from services.precheck.cause_symptom_classifier import split_violations
from services.precheck.cause_inferer import infer_causes_from_cores
from services.precheck.evidence_builder import build_evidence_node
from services.precheck.hard_case_classifier import (
    classify_hard_case,
    manual_investigation_treatment_dict,
)
from services.cause_treatment_hitter import propose_bundles
from services.resolution_narrative import build_narrative, narrative_to_dict
from services.payload_graph import build_payload_graph


# 설정성 오류로 분류되는 reason_code (즉시 차단 권장)
_BLOCKING_CODES = {
    "MID_REQUIRED_MISSING",
    "MID_DISABLED_BUT_USED",
    "ALLOWED_SHIFTS_ISOLATES_NURSE",
    "FIXED_OFF_EXCEEDS_SPAN",
    "TEAM_SIZE_INSUFFICIENT",
    "GRADE_MIN_SUM_EXCEEDS_NEED",
    "FIXED_ASSIGN_EXCEEDS_NEED",
    "FIXED_ASSIGN_VIOLATES_ALLOWED",
    "GLOBAL_DAY_CAPACITY_SHORTAGE",
    "GLOBAL_SHIFT_ALLOWED_SHORTAGE",
    "CAPACITY_TOTAL_SHORTAGE",
    "TEAM_MIN_EXCEEDS_GLOBAL_NEED",
    "TEAM_ACTIVE_MEMBERS_INSUFFICIENT",
    "TEAM_SHIFT_ALLOWED_SHORTAGE",
    "GRADE_MAX_SUM_BELOW_NEED",
    "GRADE_MIN_AVAILABLE_SHORTAGE",
    "GRADE_ANTIPAIR_FORCES_SHORTAGE",
    "TEAM_GRADE_INTERSECT_SHORTAGE",
    "FIXED_ASSIGN_BREAKS_TEAM_MIN",
    "MONTHLY_NIGHT_CAPACITY",
    "MONTHLY_NIGHT_CAPACITY_SHORTAGE",
    "N_CAPACITY_SHORTAGE",
    "PRECEPTEE_SYNC_MISMATCH",
}


def _dedup_causes_by_reason(causes: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """같은 reason_code 의 cause 들을 1건으로 묶는다.

    cause-bucket 의 의미 단위는 reason_code (cause_id) — day 마다 중복 emit 되면
    narrative 가 noisy 해진다. 첫 번째 cause 를 keep 하고 디테일은 occurrences /
    affected_days / affected_shifts 로 aggregate.
    """
    out: List[Dict[str, Any]] = []
    bucket: Dict[str, Dict[str, Any]] = {}
    for c in causes:
        rc = (c.get("reason_code") or c.get("node_id") or "?")
        raw_det = c.get("details") if isinstance(c.get("details"), dict) else (
            c.get("evidence") if isinstance(c.get("evidence"), dict) else {}
        )
        if rc not in bucket:
            head = dict(c)
            head_details = dict(raw_det)
            head_details["occurrence_count"] = 1
            head_details["affected_days"] = []
            head_details["affected_shifts"] = []
            head["details"] = head_details
            bucket[rc] = head
            out.append(head)
        else:
            head = bucket[rc]
            hd = head["details"]
            hd["occurrence_count"] = int(hd.get("occurrence_count") or 1) + 1
            d = raw_det.get("day")
            s = raw_det.get("shift")
            if d is not None and d not in hd["affected_days"]:
                hd["affected_days"].append(d)
            if s is not None and s not in hd["affected_shifts"]:
                hd["affected_shifts"].append(s)
    return out


def _extract_validator_evidence_summary(violated_constraints: List[Dict[str, Any]]) -> Dict[str, Any]:
    for v in violated_constraints or []:
        if not isinstance(v, dict):
            continue
        details = v.get("details") or {}
        if not isinstance(details, dict):
            continue
        ev = details.get("validator_evidence")
        if isinstance(ev, dict) and ev:
            return {
                "total_failed_cells": int(ev.get("total_failed_cells") or 0),
                "eligible_zero_cells": int(ev.get("eligible_zero_cells") or 0),
                "required_minus_assigned_total": int(ev.get("required_minus_assigned_total") or 0),
                "fixed_forbidden_count": int(ev.get("fixed_forbidden_count") or 0),
                "carryover_artifact_count": int(ev.get("carryover_artifact_count") or 0),
                "top_failed_cells": list(ev.get("top_failed_cells") or [])[:20],
            }
    return {}


def _build_treatments_narrative_and_hard_case(
    causes: List[Dict[str, Any]],
    observed_symptoms: List[Dict[str, Any]],
    evidence: Dict[str, Any],
) -> tuple[List[Dict[str, Any]], Optional[Dict[str, Any]], Dict[str, Any], Dict[str, Any]]:
    """cause/evidence 로 treatment_recommendations + narrative + hard_case + graph 생성.

    blocking / unrecoverable payload 양쪽에서 동일하게 호출 (DRY).
    treatment build 실패 시 graceful degradation — payload 구조 보존.
    """
    treatment_recommendations: List[Dict[str, Any]] = []
    resolution_narrative: Optional[Dict[str, Any]] = None
    bundles_objs: List[Any] = []
    try:
        cause_ids = [c.get("reason_code") for c in causes if c.get("reason_code")]
        if cause_ids:
            bundles_objs = propose_bundles(active_causes=cause_ids, max_alternatives=3)
            for b in bundles_objs:
                treatment_recommendations.append({
                    "bundle_id": b.bundle_id,
                    "total_cost": b.total_cost,
                    "overhead": b.overhead,
                    "covered_causes": b.covered_causes,
                    "uncovered_causes": b.uncovered_causes,
                    "treatments": [
                        {
                            "treatment_id": t.treatment_id,
                            "target_family": t.target_family,
                            "action_type": t.action_type,
                            "config_key": t.config_key,
                            "config_key_label_ko": t.config_key_label_ko,
                            "direction": t.direction,
                            "direction_label_ko": t.direction_label_ko,
                            "rationale_ko": t.rationale_ko,
                            "trade_off_ko": t.trade_off_ko,
                            "cost": t.cost,
                            "covers": t.covers,
                        }
                        for t in b.treatments
                    ],
                })
            primary_bundle = bundles_objs[0] if bundles_objs else None
            narr = build_narrative(
                cause_payloads=causes,
                bundle=primary_bundle,
                evidence=evidence,
            )
            resolution_narrative = narrative_to_dict(narr)
    except Exception:
        treatment_recommendations = []
        resolution_narrative = None
        bundles_objs = []

    hard_case_verdict = classify_hard_case(causes, bundles=bundles_objs)
    if hard_case_verdict.is_hard:
        manual_t = manual_investigation_treatment_dict(hard_case_verdict)
        if manual_t is not None:
            treatment_recommendations.append(manual_t)

    hard_case_dict = hard_case_verdict.to_dict()
    graph = build_payload_graph(
        causes=causes,
        symptoms=observed_symptoms,
        treatment_bundles=treatment_recommendations,
        evidence=evidence,
        hard_case=hard_case_dict,
    )
    return treatment_recommendations, resolution_narrative, hard_case_dict, graph


def has_blocking_issues(precheck_result: Dict[str, Any]) -> bool:
    """precheck 결과에 blocking severity 이슈가 하나라도 있는지."""
    if not precheck_result:
        return False
    for issue in precheck_result.get("issues", []) or []:
        if str(issue.get("severity", "")).lower() in {"hard", "blocking"} \
                and str(issue.get("reason_code", "")).upper() in _BLOCKING_CODES:
            return True
    return False


def build_blocking_payload(precheck_result: Dict[str, Any]) -> Dict[str, Any]:
    """Precheck blocking 케이스의 응답 페이로드(HTTP 500 detail로 사용).

    US-1/US-9 새 필드 (causes / observed_symptoms / evidence /
    treatment_recommendations / resolution_narrative) 도 채워준다.
    precheck issues 의 reason_code 는 곧 cause 의 alias 이므로 split_violations
    가 자동으로 cause-bucket 으로 분리한다.
    """
    issues = humanize_all(precheck_result.get("issues", []) or [])
    # 첫 번째 이슈를 대표 메시지로
    summary = (
        issues[0].get("human_message_ko")
        if issues
        else "사용자 입력만으로 산술적으로 근무표를 만들 수 없습니다."
    )
    fix_suggestions: List[str] = []
    seen = set()
    for it in issues:
        for s in it.get("fix_suggestions_ko", []) or []:
            if s not in seen:
                fix_suggestions.append(s)
                seen.add(s)
    structural = build_structural_diagnosis(
        preflight_issues=issues,
        violated_constraints=[],
        conflict_cores=[],
        pool_snapshot={},
        applied_relaxations=[],
    )

    # US-1: precheck issues 를 cause/symptom 으로 분리
    # issues 안 evidence dict 를 details 키로 normalize 해서 split_violations 가 처리하게.
    normalized = []
    for it in issues:
        item = dict(it)
        if "details" not in item and isinstance(item.get("evidence"), dict):
            item["details"] = item["evidence"]
        normalized.append(item)
    causes, observed_symptoms, _undiag = split_violations(normalized)
    causes = _dedup_causes_by_reason(causes)
    observed_symptoms = _dedup_causes_by_reason(observed_symptoms)
    evidence = build_evidence_node(
        applied_relaxations=[],
        conflict_cores=[],
        status="INFEASIBLE",
        proof_type="precheck_arithmetic_blocking",
        witness_schedule_id=None,
    )

    treatment_recommendations, resolution_narrative, hard_case_dict, graph = (
        _build_treatments_narrative_and_hard_case(causes, observed_symptoms, evidence)
    )

    return {
        "infeasibility": {
            "severity": "blocking",
            "summary_message_ko": summary,
            "preflight_issues": issues,
            "applied_relaxations": [],
            "fix_suggestions_ko": fix_suggestions,
            "violation_summary": {},
            "causes": causes,
            "observed_symptoms": observed_symptoms,
            "evidence": evidence,
            "treatment_recommendations": treatment_recommendations,
            "resolution_narrative": resolution_narrative,
            "hard_case": hard_case_dict,
            "graph": graph,
            "structural_diagnosis": structural,
            "fix_plan": build_fix_plan(
                structural_diagnosis=structural,
                preflight_issues=issues,
                violated_constraints=[],
                conflict_cores=[],
                pool_snapshot={},
            ),
            "last_error_reason": None,
        }
    }


def build_success_payload(
    *,
    precheck_result: Optional[Dict[str, Any]] = None,
    applied_relaxations: Optional[List[str]] = None,
    violated_constraints: Optional[List[Dict[str, Any]]] = None,
    hard_violation_count: int = 0,
) -> Dict[str, Any]:
    """솔버 성공(또는 soft retry 성공) 케이스의 infeasibility 페이로드.

    - applied_relaxations 비어있고 위반도 없으면 severity="ok"
    - 위반 또는 relaxation 있으면 severity="warning"
    """
    relaxations = list(applied_relaxations or [])
    violation_summary = summarize_violation_family(violated_constraints or [])
    has_violations = bool(violation_summary) or hard_violation_count > 0

    if not relaxations and not has_violations:
        severity = "ok"
        summary_msg = ""
    else:
        severity = "warning"
        summary_msg = build_summary_message(
            applied_relaxations=relaxations,
            violation_summary=violation_summary,
        )

    issues = humanize_all((precheck_result or {}).get("issues", []) or [])

    fix_suggestions: List[str] = []
    seen = set()
    for it in issues:
        for s in it.get("fix_suggestions_ko", []) or []:
            if s not in seen:
                fix_suggestions.append(s)
                seen.add(s)

    structural = {
        "mode": "relaxation_candidate",
        "primary_causes": [],
        "signals": {
            "reason_codes": [],
            "shortage_count": 0,
            "conflict_pattern_count": 0,
            "applied_relaxation_count": len(relaxations),
        },
    }

    return {
        "infeasibility": {
            "severity": severity,
            "summary_message_ko": summary_msg,
            "preflight_issues": issues,  # warning 레벨로만 전달
            "applied_relaxations": relaxations,
            "fix_suggestions_ko": fix_suggestions,
            "violation_summary": violation_summary,
            "structural_diagnosis": structural,
            "fix_plan": build_fix_plan(
                structural_diagnosis=structural,
                preflight_issues=issues,
                violated_constraints=list(violated_constraints or []),
                conflict_cores=[],
                pool_snapshot={},
            ),
            "last_error_reason": None,
        }
    }


def build_unrecoverable_payload(
    *,
    precheck_result: Optional[Dict[str, Any]] = None,
    applied_relaxations: Optional[List[str]] = None,
    last_error_reason: Optional[str] = None,
    violated_constraints: Optional[List[Dict[str, Any]]] = None,
    conflict_cores: Optional[List[Dict[str, Any]]] = None,
    pool_snapshot: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """자연 soft까지 시도했음에도 근무표 생성 실패한 케이스(HTTP 500 detail).

    `violated_constraints`: solver/validator가 식별한 인과 제약 리스트
        [{"node_id", "slack", "details", "reason_code", "human_message_ko"}].
        ontology dashboard가 ConstraintNode + CAUSES_VIOLATION 엣지로 표면화한다.

    `pool_snapshot`: :mod:`services.ontology_pool` 에서 산출된 pool 그래프 스냅샷.
        nodes/edges/shortages 가 들어있다. dashboard 가 TeamPool / GradePool /
        CommonPool 풀 상태를 시각화할 때 사용한다.
    """
    issues = humanize_all((precheck_result or {}).get("issues", []) or [])
    fix_suggestions: List[str] = [
        "Grade/팀 최소 인원 요구를 낮춰보세요.",
        "고정 셀(원티드/휴가) 분포가 특정 시프트에 몰려있는지 확인하세요.",
        "월간 N 시프트 상한 또는 야간 가능 인원을 점검하세요.",
    ]
    structural = build_structural_diagnosis(
        preflight_issues=issues,
        violated_constraints=list(violated_constraints or []),
        conflict_cores=list(conflict_cores or []),
        pool_snapshot=pool_snapshot or {},
        applied_relaxations=list(applied_relaxations or []),
    )

    # US-D: conflict_cores 의 MUS pattern → cause_id 자동 추론, violated 와 합침
    core_inferred_causes = infer_causes_from_cores(list(conflict_cores or []))
    # precheck issues 도 cause/symptom 분류 대상에 포함 — payload 가 unrecoverable
    # 경로로 빠져도 precheck 가 잡은 산술 cause 가 표면화되도록.
    precheck_issues_normalized = []
    for it in issues:
        item = dict(it)
        if "details" not in item and isinstance(item.get("evidence"), dict):
            item["details"] = item["evidence"]
        precheck_issues_normalized.append(item)
    combined_violations = (
        list(violated_constraints or [])
        + core_inferred_causes
        + precheck_issues_normalized
    )

    # US-1: cause-bucket / symptom-bucket / evidence 분리 노출 (cause 와 symptom 절대 교차 없음)
    causes, observed_symptoms, _undiag_present = split_violations(combined_violations)
    causes = _dedup_causes_by_reason(causes)
    observed_symptoms = _dedup_causes_by_reason(observed_symptoms)
    evidence = build_evidence_node(
        applied_relaxations=list(applied_relaxations or []),
        conflict_cores=list(conflict_cores or []),
        status="INFEASIBLE",
        proof_type="cp_sat_unsat_core_heuristic",
        witness_schedule_id=None,
    )

    treatment_recommendations, resolution_narrative, hard_case_dict, graph = (
        _build_treatments_narrative_and_hard_case(causes, observed_symptoms, evidence)
    )

    return {
        "infeasibility": {
            "severity": "blocking",
            "summary_message_ko": (
                "근무표 자동 완화(soft fallback)까지 시도했지만 해를 찾지 못했습니다. "
                "제약 설정을 점검해주세요."
            ),
            "preflight_issues": issues,
            "applied_relaxations": list(applied_relaxations or []),
            "fix_suggestions_ko": fix_suggestions,
            "violation_summary": {},
            # US-1 신규 3 필드 (cause/symptom/evidence 분리)
            "causes": causes,
            "observed_symptoms": observed_symptoms,
            "evidence": evidence,
            # US-9 신규 2 필드 (treatment 추천 + 자연어 narrative)
            "treatment_recommendations": treatment_recommendations,
            "resolution_narrative": resolution_narrative,
            # U-3 / U-5 신규 2 필드 (hard_case 판정 + 명확한 그래프)
            "hard_case": hard_case_dict,
            "graph": graph,
            # legacy — 호환 위해 1 릴리즈 유지 (deprecated)
            "violated_constraints": list(violated_constraints or []),
            "conflict_cores": list(conflict_cores or []),
            "pool_snapshot": pool_snapshot or {},
            "validator_evidence_summary": _extract_validator_evidence_summary(list(violated_constraints or [])),
            "structural_diagnosis": structural,
            "fix_plan": build_fix_plan(
                structural_diagnosis=structural,
                preflight_issues=issues,
                violated_constraints=list(violated_constraints or []),
                conflict_cores=list(conflict_cores or []),
                pool_snapshot=pool_snapshot or {},
            ),
            "last_error_reason": last_error_reason,
        }
    }
