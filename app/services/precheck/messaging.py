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
# 문구 원칙(2026-07-22): '실패/불가능/에러/위반' 같은 단어를 쓰지 않는다.
#   대신 "설정이 서로 맞지 않습니다 / 채울 수 없습니다 / 조정이 필요합니다" 로 정돈된 톤.
#   각 msg 는 왜 그런지 직관적으로 설명하고, fix 는 '어디서 무엇을' 을 명확히(화면 위치 포함).
#   위치 표기는 fix_location.py 의 브레드크럼과 정합.
_MESSAGES: Dict[str, Dict[str, Any]] = {
    "MID_REQUIRED_MISSING": {
        "msg": "'중간 근무(M)' 사용은 켜져 있는데, 날짜별 필요 인원에는 중간 근무가 0명으로 되어 있어 두 설정이 서로 맞지 않습니다.",
        "fix": [
            "중간 근무를 쓰지 않으신다면 설정 > 중간 근무 사용 여부를 꺼 주세요.",
            "중간 근무를 쓰신다면 설정 > 날짜별 필요 인원에서 중간 근무 인원을 지정해 주세요.",
        ],
    },
    "MID_DISABLED_BUT_USED": {
        "msg": "'중간 근무(M)' 사용은 꺼져 있는데, 중간 근무 관련 인원·규칙이 남아 있어 두 설정이 서로 맞지 않습니다.",
        "fix": ["설정 > 중간 근무 사용 여부를 켜거나, 중간 근무 관련 설정을 지워 주세요."],
    },
    "ALLOWED_SHIFTS_ISOLATES_NURSE": {
        "msg": "어떤 간호사에게 설 수 있는 근무가 하나도 열려 있지 않아, 그 간호사에게는 어떤 근무도 넣을 수 없습니다.",
        "fix": ["간호사 관리 > 해당 간호사 > 설 수 있는 근무 종류에서 근무를 한 개 이상 열어 주세요."],
    },
    "FIXED_OFF_EXCEEDS_SPAN": {
        "msg": "미리 정해 둔 휴무 일수가 그 간호사의 근무 가능 기간보다 많습니다.",
        "fix": [
            "고정 근무/휴무 편집 > 해당 간호사에서 휴무 일수를 줄여 주세요.",
            "입사/퇴사 등 활동 기간이 맞게 설정됐는지 확인해 주세요.",
        ],
    },
    "TEAM_SIZE_INSUFFICIENT": {
        "msg": "팀 인원이 그 팀에 정한 최소 인원보다 적어, 매일 최소 인원을 채우기 어렵습니다.",
        "fix": ["해당 팀에 간호사를 보강해 주세요.", "설정 > 팀 최소 인원을 낮춰 주세요."],
    },
    "GRADE_MIN_SUM_EXCEEDS_NEED": {
        "msg": "직급별로 정한 최소 인원을 모두 더하면 그 근무에 필요한 총 인원보다 많아집니다. 지금 조합으로는 수를 맞출 수 없어요.",
        "fix": [
            "설정 > 직급별 최소 인원의 합을 날짜별 필요 인원 이하로 낮춰 주세요.",
            "설정 > 날짜별 필요 인원을 늘려 주세요.",
        ],
    },
    "FIXED_ASSIGN_EXCEEDS_NEED": {
        "msg": "미리 확정한 근무(원티드)만으로 이미 그 날 필요한 인원을 넘어섭니다.",
        "fix": [
            "고정 근무 편집 > 해당 날짜에서 확정 근무를 줄여 주세요.",
            "설정 > 날짜별 필요 인원을 늘려 주세요.",
        ],
    },
    "FIXED_ASSIGN_VIOLATES_ALLOWED": {
        "msg": "어떤 간호사의 확정 근무(원티드)가 그 간호사가 설 수 있는 근무 밖에 있습니다.",
        "fix": [
            "고정 근무 편집 > 해당 간호사에서 확정 근무를 바꾸거나, 간호사 관리에서 설 수 있는 근무를 넓혀 주세요.",
        ],
    },
    "GLOBAL_DAY_CAPACITY_SHORTAGE": {
        "msg": "어떤 날에 근무 가능한 간호사 수가 그 날 필요한 총 인원보다 적습니다.",
        "fix": [
            "휴가·주말 휴무가 한 날에 몰리지 않도록 분산해 주세요.",
            "설정 > 날짜별 필요 인원에서 그 날 인원을 줄여 주세요.",
        ],
    },
    "GLOBAL_SHIFT_ALLOWED_SHORTAGE": {
        "msg": "어떤 날의 특정 근무를 설 수 있는 간호사 수 자체가 필요 인원보다 적어, 그 근무를 채울 수 없습니다.",
        "fix": [
            "간호사 관리 > 설 수 있는 근무 종류에서 그 근무 가능 인원을 늘려 주세요.",
            "설정 > 날짜별 필요 인원에서 그 근무 인원을 줄여 주세요.",
        ],
    },
    "CAPACITY_TOTAL_SHORTAGE": {
        "msg": "한 달 동안 간호사들이 일할 수 있는 총 근무일이, 채워야 할 총 근무 수보다 적습니다. 지금 인원·필요 인원 설정으로는 모든 자리를 채울 수 없어요.",
        "fix": [
            "간호사 관리에서 인원을 늘리거나 휴가·휴무 일수를 줄여 주세요.",
            "설정 > 날짜별 필요 인원을 낮춰 주세요.",
        ],
    },
    "TEAM_MIN_EXCEEDS_GLOBAL_NEED": {
        "msg": "한 팀에 정한 최소 인원이 그 근무에 필요한 전체 인원보다 많아, 그 팀만으로는 다 채울 수 없습니다.",
        "fix": [
            "설정 > 팀 최소 인원을 낮춰 주세요.",
            "설정 > 날짜별 필요 인원을 늘려 주세요.",
        ],
    },
    "TEAM_ACTIVE_MEMBERS_INSUFFICIENT": {
        "msg": "어떤 날에 근무 가능한 팀 멤버 수가 그 팀의 근무 최소 인원보다 적습니다.",
        "fix": [
            "그 팀의 휴무·휴가가 한 날에 몰리지 않도록 분산해 주세요.",
            "해당 팀에 인원을 보강하거나 설정 > 팀 최소 인원을 낮춰 주세요.",
        ],
    },
    "TEAM_SHIFT_ALLOWED_SHORTAGE": {
        "msg": "어떤 날, 그 팀 안에서 특정 근무를 설 수 있는 멤버 수가 부족합니다.",
        "fix": [
            "그 팀에서 그 근무를 설 수 있는 간호사를 늘려 주세요.",
            "설정 > 팀 최소 인원(해당 근무)을 낮춰 주세요.",
        ],
    },
    "GRADE_MAX_SUM_BELOW_NEED": {
        "msg": "직급별 최대 인원(상한)을 모두 더해도 그 근무에 필요한 인원에 못 미쳐, 자리를 다 채울 수 없습니다.",
        "fix": [
            "설정 > 직급별 최대 인원(상한)을 늘려 주세요.",
            "설정 > 날짜별 필요 인원을 줄여 주세요.",
        ],
    },
    "GRADE_MIN_AVAILABLE_SHORTAGE": {
        "msg": "어떤 날·근무에 그 직급을 설 수 있는 간호사 수가 직급 최소 요구보다 적습니다.",
        "fix": [
            "해당 직급 간호사를 보강해 주세요.",
            "설정 > 직급별 최소 인원을 낮춰 주세요.",
        ],
    },
    "GRADE_ANTIPAIR_FORCES_SHORTAGE": {
        "msg": "같은 근무에서 직급 상한 규칙과 최소 인원 규칙이 서로 부딪혀, 두 조건을 동시에 맞출 수 없습니다.",
        "fix": [
            "설정 > 직급별 인원에서 최대(상한) 또는 최소 중 하나를 완화해 주세요.",
        ],
    },
    "FIXED_ASSIGN_BREAKS_TEAM_MIN": {
        "msg": "미리 확정한 근무(원티드)가 팀 최소 인원 채우기를 가로막고 있습니다.",
        "fix": [
            "부딪히는 확정 근무를 조정하거나, 설정 > 팀 최소 인원을 낮춰 주세요.",
        ],
    },
    # 주의: 실제 reason_code 는 MONTHLY_NIGHT_CAPACITY_SHORTAGE 다. 예전엔 키가
    # MONTHLY_NIGHT_CAPACITY 뿐이라 매칭이 안 돼 raw 코드로 떨어졌다(필드명 불일치). 둘 다 등록.
    # 개인 속성(월 N 상한) 변경 제안은 제외 — 개인 속성 불가침 원칙.
    "MONTHLY_NIGHT_CAPACITY": {
        "msg": "한 달 야간(N) 근무 총 요구가, 야간을 설 수 있는 간호사들의 한 달 야간 가능 횟수 합보다 많습니다.",
        "fix": [
            "야간을 설 수 있는 간호사를 늘려 주세요.",
            "설정 > 날짜별 필요 인원에서 야간 인원을 낮춰 주세요.",
        ],
    },
    "MONTHLY_NIGHT_CAPACITY_SHORTAGE": {
        "msg": "한 달 야간(N) 근무 총 요구가, 야간을 설 수 있는 간호사들의 한 달 야간 가능 횟수 합보다 많습니다.",
        "fix": [
            "야간을 설 수 있는 간호사를 늘려 주세요.",
            "설정 > 날짜별 필요 인원에서 야간 인원을 낮춰 주세요.",
        ],
    },
    "N_CAPACITY_SHORTAGE": {
        "msg": "어떤 날 야간(N) 요구 인원이 그 날 야간을 설 수 있는 간호사 수보다 많습니다.",
        "fix": [
            "야간을 설 수 있는 간호사를 늘려 주세요.",
            "설정 > 날짜별 필요 인원에서 그 날 야간 인원을 낮춰 주세요.",
        ],
    },
    "PRECEPTEE_SYNC_MISMATCH": {
        "msg": "교육 짝(프리셉티)과 선생님(프리셉터)이 함께 근무할 수 없는 상태입니다. 설 수 있는 근무·팀·교육 기간이 서로 맞지 않거나, 두 사람이 다른 근무를 서도록 지정되어 있어요.",
        "fix": [
            "두 사람의 설 수 있는 근무·팀·교육 기간을 맞춰 주세요.",
            "해당 짝에 배반근무(상호배제)가 걸려 있으면 설정 > 교육(프리셉티) 함께 근무에서 풀어 주세요.",
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
    elif "n_required" in ev and "n_capacity" in ev:
        _p = f"월요구={ev['n_required']} / 월가능={ev['n_capacity']}"
        if ev.get("shortage") is not None:
            _p += f" / 부족={ev['shortage']}"
        suffix_parts.append(_p)

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
        "요구 조건과 제약이 다소 빡빡해, 일부 조건을 완화하여 가능한 범위에서 가장 나은 근무표를 만들었습니다."
    )
