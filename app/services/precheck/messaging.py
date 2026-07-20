"""Precheck reason_code → 사용자 친화 메시지 + fix suggestion 매핑.

`team_grade_precheck.py`가 반환하는 issue {reason_code, severity, evidence}에
한국어 human_message_ko와 fix_suggestions_ko를 부착한다.
"""

from __future__ import annotations

from typing import Any, Dict, List


def _shift_label(shift: Any) -> str:
    code = str(shift or "").upper()
    return {"D": "주간(D)", "E": "오후(E)", "N": "야간(N)", "M": "미드(M)"}.get(code, code or "?")


def _ev(issue: Dict[str, Any], key: str, default: Any = None) -> Any:
    return (issue.get("evidence") or {}).get(key, default)


# reason_code → (human_message_template, fix_suggestions)
# 메시지에는 evidence 필드를 {}-format으로 박는다.
_MESSAGES: Dict[str, Dict[str, Any]] = {
    "MID_REQUIRED_MISSING": {
        "msg": "MID 사용 옵션이 켜져 있지만 일별 요구치에 M 시프트가 0으로 설정되어 있습니다.",
        "fix": ["MID 미사용이면 use_mid를 끄세요.", "M 시프트 일별 요구치를 설정하세요."],
    },
    "MID_DISABLED_BUT_USED": {
        "msg": "MID 사용 옵션이 꺼져 있지만 M 시프트 요구치/제약이 설정되어 있습니다.",
        "fix": ["use_mid를 켜거나 M 관련 설정을 제거하세요."],
    },
    "ALLOWED_SHIFTS_ISOLATES_NURSE": {
        "msg": "특정 간호사의 가능한 시프트가 공집합이라 어떤 근무도 배정할 수 없습니다.",
        "fix": ["해당 간호사의 work_shifts(가능 시프트) 설정을 확인하세요."],
    },
    "FIXED_OFF_EXCEEDS_SPAN": {
        "msg": "고정 OFF 일수가 활성 기간을 초과합니다.",
        "fix": ["고정 OFF 또는 휴가 일수를 줄이세요.", "join/leave 날짜를 확인하세요."],
    },
    "TEAM_SIZE_INSUFFICIENT": {
        "msg": "팀 멤버 수가 팀 최소 인원 요구보다 적어 매일 충족이 불가능합니다.",
        "fix": ["해당 팀에 간호사를 추가하세요.", "팀 최소 인원을 줄이세요."],
    },
    "GRADE_MIN_SUM_EXCEEDS_NEED": {
        "msg": "Grade별 최소 인원 합계가 해당 시프트의 일별 요구 인원보다 큽니다 (수학적으로 불가능).",
        "fix": [
            "Grade 최소 인원 합을 일별 요구 인원 이하로 낮추세요.",
            "일별 요구 인원을 늘리세요.",
        ],
    },
    "FIXED_ASSIGN_EXCEEDS_NEED": {
        "msg": "고정 배정만으로 이미 일별 시프트 요구 인원을 초과합니다.",
        "fix": ["해당 일/시프트의 고정 배정을 줄이세요.", "일별 요구 인원을 늘리세요."],
    },
    "FIXED_ASSIGN_VIOLATES_ALLOWED": {
        "msg": "어느 간호사의 고정 배정이 본인의 가능 시프트(work_shifts) 밖에 있습니다.",
        "fix": ["고정 배정을 가능 시프트로 변경하거나 work_shifts를 확장하세요."],
    },
    "GLOBAL_DAY_CAPACITY_SHORTAGE": {
        "msg": "어느 일자에 활성 가능한 간호사 수가 일별 총 요구 인원보다 적습니다.",
        "fix": [
            "휴가/주말휴무가 겹치지 않게 조정하세요.",
            "해당 일의 요구 인원을 줄이거나 join/leave 분포를 분산시키세요.",
        ],
    },
    "GLOBAL_SHIFT_ALLOWED_SHORTAGE": {
        "msg": "어느 일자/시프트에 해당 시프트 가능 간호사 풀 자체가 요구 인원보다 적습니다.",
        "fix": [
            "해당 시프트 가능한 간호사를 추가하거나 work_shifts를 확장하세요.",
            "해당 시프트 요구 인원을 줄이세요.",
        ],
    },
    "CAPACITY_TOTAL_SHORTAGE": {
        "msg": "월간 가용 작업일 합계가 월간 총 요구 인원보다 적어 근무표 자체가 만들어질 수 없습니다.",
        "fix": [
            "간호사를 추가 배치하거나 휴가/휴무 일수를 줄이세요.",
            "월간 일별 요구 인원을 낮추세요.",
        ],
    },
    "TEAM_MIN_EXCEEDS_GLOBAL_NEED": {
        "msg": "한 팀의 최소 인원이 해당 시프트의 일별 요구 인원보다 커서 그 팀만으로는 다 채울 수 없습니다.",
        "fix": [
            "해당 팀의 최소 인원을 낮추세요.",
            "일별 요구 인원을 늘리세요.",
        ],
    },
    "TEAM_ACTIVE_MEMBERS_INSUFFICIENT": {
        "msg": "특정 일자에 활성 가능한 팀 멤버 수가 해당 팀 시프트 최소 인원보다 적습니다.",
        "fix": [
            "해당 팀의 휴무/휴가 분포를 분산시키세요.",
            "해당 팀에 인원을 추가하세요.",
        ],
    },
    "TEAM_SHIFT_ALLOWED_SHORTAGE": {
        "msg": "특정 일자에 해당 팀 내에서 그 시프트가 가능한 멤버 수가 부족합니다.",
        "fix": [
            "해당 팀에서 그 시프트가 가능한 간호사를 추가하세요.",
            "팀 시프트 최소 인원을 낮추세요.",
        ],
    },
    "GRADE_MAX_SUM_BELOW_NEED": {
        "msg": "Grade별 최대 인원(상한) 합계가 해당 시프트의 요구 인원보다 작아 채울 수 없습니다.",
        "fix": [
            "Grade 최대 인원(상한)을 늘리세요.",
            "일별 요구 인원을 줄이세요.",
        ],
    },
    "GRADE_MIN_AVAILABLE_SHORTAGE": {
        "msg": "특정 일/시프트에 해당 Grade가 가능한 간호사 풀이 grade 최소 요구보다 적습니다.",
        "fix": [
            "해당 Grade 간호사를 추가 배치하세요.",
            "해당 Grade의 시프트 최소 인원을 낮추세요.",
        ],
    },
    "GRADE_ANTIPAIR_FORCES_SHORTAGE": {
        "msg": "Grade anti-pair(상한) 제약 + min 제약이 같은 일/시프트에서 서로 충돌해 충족이 불가능합니다.",
        "fix": [
            "Grade max 또는 min 중 하나를 완화하세요.",
        ],
    },
    "FIXED_ASSIGN_BREAKS_TEAM_MIN": {
        "msg": "고정 배정이 팀 최소 인원 충족을 막아 infeasible을 유발합니다.",
        "fix": [
            "충돌하는 고정 배정을 조정하거나 팀 최소 인원을 낮추세요.",
        ],
    },
    "MONTHLY_NIGHT_CAPACITY": {
        "msg": "야간(N) 시프트의 월간 총 요구가 야간 가능 간호사들의 월간 N 가용 횟수 합계를 초과합니다.",
        "fix": [
            "야간 가능 간호사를 추가 배치하세요.",
            "월간 N 일별 요구를 낮추세요.",
            "야간 전담 간호사의 월간 N 상한을 늘리세요.",
        ],
    },
}


def humanize(issue: Dict[str, Any]) -> Dict[str, Any]:
    """단일 issue에 human_message_ko와 fix_suggestions_ko를 추가."""
    code = str(issue.get("reason_code") or "").upper()
    base = _MESSAGES.get(code, {"msg": code, "fix": []})
    msg_template = base["msg"]
    ev = issue.get("evidence") or {}
    # 자주 쓰이는 evidence 정보를 메시지 뒤에 부착
    suffix_parts: List[str] = []
    if "shift" in ev:
        suffix_parts.append(f"시프트={_shift_label(ev['shift'])}")
    if "grade" in ev:
        suffix_parts.append(f"grade={ev['grade']}")
    if "team_id" in ev:
        suffix_parts.append(f"팀={ev['team_id']}")
    if "day" in ev:
        try:
            suffix_parts.append(f"일자={int(ev['day']) + 1}일")
        except (TypeError, ValueError):
            pass
    if "need" in ev and "available" in ev:
        suffix_parts.append(f"필요={ev['need']} / 가능={ev['available']}")
    elif "min_sum" in ev and "need" in ev:
        suffix_parts.append(f"최소합계={ev['min_sum']} / 요구={ev['need']}")
    elif "monthly_demand" in ev and "monthly_capacity" in ev:
        suffix_parts.append(
            f"월요구={ev['monthly_demand']} / 월가능={ev['monthly_capacity']}"
        )

    suffix = f" ({', '.join(suffix_parts)})" if suffix_parts else ""
    out = dict(issue)
    out["human_message_ko"] = msg_template + suffix
    out["fix_suggestions_ko"] = list(base["fix"])
    return out


def humanize_all(issues: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [humanize(i) for i in issues or []]


def summarize_violation_family(violated_constraints: List[Dict[str, Any]]) -> Dict[str, Any]:
    """`constraint_impact.violated_constraints`를 family별로 묶고 상위 샘플 반환."""
    by_family: Dict[str, List[Dict[str, Any]]] = {}
    for v in violated_constraints or []:
        node_id = str(v.get("node_id") or "")
        family = node_id.split(":")[0] or "unknown"
        # coverage:min:... 같은 경우 두 토큰 family로 보존
        if family == "coverage" and ":" in node_id:
            family = "coverage_min" if node_id.startswith("coverage:min") else "coverage"
        by_family.setdefault(family, []).append(v)

    summary: Dict[str, Any] = {}
    for family, items in by_family.items():
        samples = [
            {
                "node_id": it.get("node_id"),
                "details": it.get("details"),
                "slack": it.get("slack"),
            }
            for it in items[:5]
        ]
        summary[family] = {"count": len(items), "samples": samples}
    return summary


def build_summary_message(
    *,
    applied_relaxations: List[str],
    violation_summary: Dict[str, Any],
) -> str:
    """자연 soft 적용 시 사용자에게 보일 한 줄 요약."""
    if not applied_relaxations:
        return ""
    grade_count = (violation_summary.get("grade_min") or {}).get("count", 0)
    team_count = (violation_summary.get("team_min") or {}).get("count", 0)
    coverage_count = (violation_summary.get("coverage_min") or {}).get("count", 0)

    if grade_count > 0 and team_count > 0:
        return (
            "Grade와 TEAM 최소 인원 요구 대비 가용 인원이 제한적이어서, "
            "가능한 범위에서 최적의 근무표를 생성했습니다."
        )
    if grade_count > 0:
        return (
            "입력하신 Grade 최소 인원 요구 대비 가용 인원이 제한적이어서, "
            "가능한 범위에서 최적의 근무표를 생성했습니다."
        )
    if team_count > 0:
        return (
            "입력하신 TEAM별 최소 인원 요구 대비 가용 인원이 제한적이어서, "
            "가능한 범위에서 최적의 근무표를 생성했습니다."
        )
    if coverage_count > 0:
        return (
            "일별 요구 인원을 일부 충족하지 못한 채 가능한 범위에서 최적의 근무표를 생성했습니다."
        )
    return (
        "입력 요구조건 및 제약이 제한적이어서 하여 일부 제약을 완화하고 가능한 범위에서 최적의 근무표를 생성했습니다."
    )
