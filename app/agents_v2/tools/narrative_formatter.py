"""narrative_formatter — build_unrecoverable_payload 결과를 agent-friendly dict 로 평탄화."""

from __future__ import annotations

from typing import Any

# raw infeasibility cause 코드 → 한국어 자연어 표현
_CAUSE_CODE_KO: dict[str, str] = {
    "NO_ASSIGNMENT_CAPACITY": "필요 인원 부족",
    "NO_ASSIGNMENT_ELIGIBILITY": "자격 요건 부족",
    "NO_ASSIGNMENT_FIXED": "확정 원티드 충돌",
    "CAPACITY_TOTAL_SHORTAGE": "필요 인원 부족",
    "GLOBAL_DAY_CAPACITY_SHORTAGE": "필요 인원 부족",
    "GLOBAL_SHIFT_ALLOWED_SHORTAGE": "허용 시프트 인원 부족",
    "TEAM_SIZE_INSUFFICIENT": "팀 인원 부족",
    "GRADE_MIN_SUM_EXCEEDS_NEED": "직급 최소 인원 초과 요구",
    "GRADE_ANTIPAIR_FORCES_SHORTAGE": "직급 충돌로 인한 인원 부족",
    "TEAM_ACTIVE_MEMBERS_INSUFFICIENT": "팀 활성 인원 부족",
    "TEAM_SHIFT_ALLOWED_SHORTAGE": "팀 시프트 허용 인원 부족",
    "TEAM_MIN_EXCEEDS_GLOBAL_NEED": "팀 최소 인원이 전체 필요 인원 초과",
    "TEAM_GRADE_INTERSECT_SHORTAGE": "팀·직급 교집합 인원 부족",
    "GRADE_MAX_SUM_BELOW_NEED": "직급 최대 인원이 필요 인원 미달",
    "GRADE_MIN_AVAILABLE_SHORTAGE": "직급 가용 인원 부족",
    "MONTHLY_NIGHT_CAPACITY": "월간 야간 가능 인원 부족",
    "MONTHLY_NIGHT_CAPACITY_SHORTAGE": "월간 야간 가능 인원 부족",
    "N_CAPACITY_SHORTAGE": "야간 가능 인원 부족",
    "FIXED_ASSIGN_EXCEEDS_NEED": "확정 원티드 배정이 필요 인원 초과",
    "FIXED_ASSIGN_VIOLATES_ALLOWED": "확정 원티드가 허용 시프트 위반",
    "FIXED_ASSIGN_BREAKS_TEAM_MIN": "확정 원티드 충돌",
    "FIXED_OFF_EXCEEDS_SPAN": "확정 원티드 충돌",
    "ALLOWED_SHIFTS_ISOLATES_NURSE": "자격 요건 부족",
    "MID_REQUIRED_MISSING": "자격 요건 부족",
    "MID_DISABLED_BUT_USED": "자격 요건 부족",
    "PRECEPTEE_SYNC_MISMATCH": "자격 요건 부족",
}


def _causes_to_ko(causes: list) -> list[str]:
    """cause 목록 → raw 코드 없는 한국어 문자열 목록."""
    result = []
    seen: set[str] = set()
    for c in causes or []:
        if not isinstance(c, dict):
            continue
        rc = (c.get("reason_code") or c.get("node_id") or "").upper()
        human = c.get("human_message_ko") or ""
        # raw 코드가 포함된 human_message_ko 는 매핑으로 교체
        if rc in _CAUSE_CODE_KO:
            text = _CAUSE_CODE_KO[rc]
        elif human and not any(k in human for k in _CAUSE_CODE_KO):
            text = human
        else:
            text = _CAUSE_CODE_KO.get(rc, "")
        if text and text not in seen:
            seen.add(text)
            result.append(text)
    return result


def format_infeasibility(payload: dict | None) -> dict | None:
    """payload (build_unrecoverable_payload 결과) → LLM 이 읽기 쉬운 narrative dict.

    None / 빈 dict 입력은 None 반환. resolution_narrative 가 없으면 raw payload
    의 핵심 필드만 추려서 반환.
    """
    if not payload or not isinstance(payload, dict):
        return None

    infeas = payload.get("infeasibility") or {}
    # resolution_narrative 는 infeasibility 안에 중첩되어 있음
    narrative = infeas.get("resolution_narrative") or payload.get("resolution_narrative") or {}
    hard_case = infeas.get("hard_case") or payload.get("hard_case") or {}
    apply_hint = infeas.get("apply_hint") or payload.get("apply_hint") or {}

    problems = [
        p.get("rendered_ko")
        for p in (narrative.get("problem_list") or [])
        if isinstance(p, dict) and p.get("rendered_ko")
    ]
    actions = [
        a.get("rationale_ko")
        for a in (narrative.get("action_levers") or [])
        if isinstance(a, dict) and a.get("rationale_ko")
    ]
    trade_offs = [
        t.get("trade_off_ko")
        for t in (narrative.get("trade_offs") or [])
        if isinstance(t, dict) and t.get("trade_off_ko")
    ]

    return {
        "infeasible": True,
        "severity": infeas.get("severity"),
        "summary_ko": narrative.get("summary_ko") or infeas.get("summary_message_ko") or "",
        "problems": problems,
        "actions": actions,
        "trade_offs": trade_offs,
        "hard_case": _coerce_hard_case(hard_case),
        "apply_hint": apply_hint or None,
    }


def format_infeasibility_response(payload: dict) -> str:
    """infeasibility payload → 사용자에게 노출되는 한국어 자연어 응답 문자열.

    narrative.problem_list / action_levers / hard_case 를 기반으로 합성.
    raw enum 문자열(NO_ASSIGNMENT_* 등)은 한국어로 변환하여 미노출.
    hard_case=true 시 끝에 '운영자 검토를 권장합니다.' 자동 첨부.
    """
    if not payload or not isinstance(payload, dict):
        return "근무표를 만들 수 없었습니다."

    infeas = payload.get("infeasibility") or {}
    narrative = infeas.get("resolution_narrative") or payload.get("resolution_narrative") or {}
    hard_case_raw = infeas.get("hard_case") or payload.get("hard_case") or {}
    causes = infeas.get("causes") or payload.get("causes") or []

    # 원인 목록 수집: narrative.problem_list 우선, fallback → causes
    problem_texts: list[str] = []
    for p in (narrative.get("problem_list") or []):
        if not isinstance(p, dict):
            continue
        rendered = p.get("rendered_ko") or ""
        # raw 코드가 섞여 있지 않은 경우에만 사용
        if rendered and not any(k in rendered for k in _CAUSE_CODE_KO):
            problem_texts.append(rendered)

    if not problem_texts:
        problem_texts = _causes_to_ko(causes)

    # 권장 조치 목록
    action_texts: list[str] = []
    for a in (narrative.get("action_levers") or []):
        if not isinstance(a, dict):
            continue
        rationale = a.get("rationale_ko") or ""
        if rationale and not any(k in rationale for k in _CAUSE_CODE_KO):
            action_texts.append(rationale)

    parts: list[str] = ["근무표를 만들 수 없었습니다."]

    if problem_texts:
        problem_str = ", ".join(problem_texts)
        parts.append(f"원인: {problem_str}.")

    if action_texts:
        action_str = " / ".join(action_texts)
        parts.append(f"권장 조치: {action_str}.")

    is_hard = _is_hard_case(hard_case_raw)
    if is_hard:
        parts.append("운영자 검토를 권장합니다.")

    return " ".join(parts)


def _is_hard_case(hc: Any) -> bool:
    """hard_case 객체에서 is_hard 여부 bool 반환."""
    if not isinstance(hc, dict):
        return False
    return bool(hc.get("is_hard_case") or hc.get("hard_case") or hc.get("is_hard"))


def _coerce_hard_case(hc: Any) -> dict | None:
    """hard_case 객체에서 LLM 이 알아야 할 정보만 추출."""
    if not isinstance(hc, dict):
        return None
    if not _is_hard_case(hc):
        return None
    return {
        "is_hard_case": True,
        "reason_ko": hc.get("reason_ko") or hc.get("reason") or "",
    }
