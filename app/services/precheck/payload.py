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
}


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
    """Precheck blocking 케이스의 응답 페이로드(HTTP 500 detail로 사용)."""
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
    return {
        "infeasibility": {
            "severity": "blocking",
            "summary_message_ko": summary,
            "preflight_issues": issues,
            "applied_relaxations": [],
            "fix_suggestions_ko": fix_suggestions,
            "violation_summary": {},
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

    return {
        "infeasibility": {
            "severity": severity,
            "summary_message_ko": summary_msg,
            "preflight_issues": issues,  # warning 레벨로만 전달
            "applied_relaxations": relaxations,
            "fix_suggestions_ko": fix_suggestions,
            "violation_summary": violation_summary,
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
            "violated_constraints": list(violated_constraints or []),
            "conflict_cores": list(conflict_cores or []),
            "pool_snapshot": pool_snapshot or {},
            "last_error_reason": last_error_reason,
        }
    }
