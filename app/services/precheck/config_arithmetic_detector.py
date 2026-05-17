"""Config arithmetic detectors — solver 호출 없이 closed-form 산술 모순 검출.

기존 team_grade_precheck.py 의 산술 검사를 보완하는 detector 들. 모두:
  - 입력 data 만으로 결정적 (deterministic)
  - 어떤 병원/병동/간호사 수에서도 동일하게 작동 (정적 fixture 없음)
  - magic number 없음 — 임계값은 모두 입력 데이터에서 derive

신규 reason_code 들:
  - DAILY_DEMAND_EXCEEDS_NURSE_COUNT — 어떤 day 의 총 shift 요구 > 간호사 수
  - OFF_BUDGET_EXCEEDS_NUM_DAYS     — 간호사별 OFF 예산 > num_days
  - MONTHLY_LIMIT_MIN_EXCEEDS_MAX   — nurse_monthly_limit 의 min > max
  - TEAM_MIN_EXCEEDS_TEAM_SIZE       — 팀 최소 인원 > 팀 크기

기존 reason_code 들과 시맨틱 중복 없음 (각각 다른 차원의 모순).

※ capacity/eligibility/preceptee 영역의 산술 검출은 team_grade_precheck.py
   의 check_capacity_total_shortage / check_global_shift_allowed_shortage /
   check_monthly_night_capacity / check_daily_night_shortage /
   check_preceptee_sync_mismatch 가 담당한다 — PrecheckInput dataclass 기반
   단일 진실 영역.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional


def _resolve_req_by_day(
    *,
    by_day: Optional[List[Dict[str, int]]],
    base: Optional[Dict[str, int]],
    day_idx: int,
) -> Dict[str, int]:
    if isinstance(by_day, list) and day_idx < len(by_day) and isinstance(by_day[day_idx], dict):
        return {str(k).upper(): int(v or 0) for k, v in by_day[day_idx].items()}
    if isinstance(base, dict):
        return {str(k).upper(): int(v or 0) for k, v in base.items()}
    return {}


def detect_daily_demand_exceeds_nurse_count(
    *,
    daily_shift_requirements_by_day: Optional[List[Dict[str, int]]] = None,
    daily_shift_requirements: Optional[Dict[str, int]] = None,
    nurse_count: int,
    num_days: int,
) -> List[Dict[str, Any]]:
    """day 별 total work demand 가 nurse_count 초과 → 모든 간호사가 동시 근무해도 부족.

    `O`/`OFF`/`-` 는 work demand 가 아니므로 합산에서 제외.
    """
    issues: List[Dict[str, Any]] = []
    if nurse_count <= 0 or num_days <= 0:
        return issues

    for d in range(num_days):
        req = _resolve_req_by_day(
            by_day=daily_shift_requirements_by_day,
            base=daily_shift_requirements,
            day_idx=d,
        )
        total = sum(v for k, v in req.items() if k not in ("O", "OFF", "-") and v > 0)
        if total > nurse_count:
            issues.append({
                "reason_code": "DAILY_DEMAND_EXCEEDS_NURSE_COUNT",
                "severity": "hard",
                "details": {
                    "day": d + 1,
                    "total_demand": total,
                    "nurse_count": nurse_count,
                    "shortage": total - nurse_count,
                    "by_shift": {k: v for k, v in req.items() if v > 0},
                },
                "human_message_ko": (
                    f"{d + 1}일 총 근무 요구({total}명)가 가용 간호사 수({nurse_count}명)를 "
                    f"{total - nurse_count}명 초과합니다."
                ),
                "fix_suggestions_ko": [
                    f"daily_shift_requirements 의 {d + 1}일 합계를 {nurse_count} 이하로 조정",
                    "특정 shift 의 일별 요구를 감소",
                    "팀/그룹에 간호사 추가 배치",
                ],
            })
    return issues


def detect_off_budget_exceeds_num_days(
    *,
    nurse_off_budgets: List[Dict[str, Any]],
    num_days: int,
) -> List[Dict[str, Any]]:
    """간호사별 OFF 총량이 num_days 초과 → 근무 0일도 안 됨.

    nurse_off_budgets 형식: [{"nurse_id": ..., "off_days_total": int}, ...]
    """
    issues: List[Dict[str, Any]] = []
    if num_days <= 0:
        return issues
    for nb in nurse_off_budgets or []:
        if not isinstance(nb, dict):
            continue
        try:
            total = int(nb.get("off_days_total") or 0)
        except Exception:
            continue
        if total > num_days:
            issues.append({
                "reason_code": "OFF_BUDGET_EXCEEDS_NUM_DAYS",
                "severity": "hard",
                "details": {
                    "nurse_id": nb.get("nurse_id"),
                    "off_days_total": total,
                    "num_days": num_days,
                    "excess": total - num_days,
                    # ontology cause template aliases
                    "total": total,
                    "span": num_days,
                },
                "human_message_ko": (
                    f"간호사 {nb.get('nurse_id', '?')} 의 OFF 예산({total}일)이 "
                    f"월 일수({num_days}일)를 {total - num_days}일 초과합니다."
                ),
                "fix_suggestions_ko": [
                    f"개인 OFF 조정(personal_off_adjustment) 또는 global_monthly_off_days 합을 {num_days} 이하로 재설정",
                    "휴가 입력 데이터 재확인",
                ],
            })
    return issues


def detect_monthly_limit_min_exceeds_max(
    *,
    monthly_limits: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """nurse_monthly_limit 의 (shift × nurse) min > max — 산술 모순.

    monthly_limits 형식: [{"nurse_id": ..., "shift": "N", "min_val": int, "max_val": int}, ...]
    """
    issues: List[Dict[str, Any]] = []
    for lim in monthly_limits or []:
        if not isinstance(lim, dict):
            continue
        mn = lim.get("min_val")
        mx = lim.get("max_val")
        if mn is None or mx is None:
            continue
        try:
            mn_i, mx_i = int(mn), int(mx)
        except Exception:
            continue
        if mn_i > mx_i:
            issues.append({
                "reason_code": "MONTHLY_LIMIT_MIN_EXCEEDS_MAX",
                "severity": "hard",
                "details": {
                    "nurse_id": lim.get("nurse_id"),
                    "shift": str(lim.get("shift") or "").upper(),
                    "min_val": mn_i,
                    "max_val": mx_i,
                },
                "human_message_ko": (
                    f"간호사 {lim.get('nurse_id', '?')} 의 {lim.get('shift', '?')} 월간 "
                    f"최소({mn_i}) > 최대({mx_i}) — 산술 모순."
                ),
                "fix_suggestions_ko": [
                    "NurseMonthlyLimit 의 min/max 재설정",
                    "shift 입력 데이터 확인",
                ],
            })
    return issues


def detect_team_min_exceeds_team_size(
    *,
    team_min_by_team: Dict[Any, Dict[str, int]],
    team_size: Dict[Any, int],
) -> List[Dict[str, Any]]:
    """팀 최소 인원 > 팀 크기 — 같은 팀에서 동시에 그만큼 출근 불가."""
    issues: List[Dict[str, Any]] = []
    for team_id, by_shift in (team_min_by_team or {}).items():
        try:
            size = int(team_size.get(team_id, 0))
        except Exception:
            continue
        if not isinstance(by_shift, dict):
            continue
        for shift, mn in by_shift.items():
            try:
                mn_i = int(mn or 0)
            except Exception:
                continue
            if mn_i > size:
                issues.append({
                    "reason_code": "TEAM_MIN_EXCEEDS_TEAM_SIZE",
                    "severity": "hard",
                    "details": {
                        "team_id": team_id,
                        "shift": str(shift).upper(),
                        "team_min": mn_i,
                        "team_size": size,
                        # ontology cause template alias
                        "size": size,
                    },
                    "human_message_ko": (
                        f"팀 {team_id} 의 {str(shift).upper()} 시프트 최소 인원({mn_i})이 "
                        f"팀 크기({size})를 초과합니다."
                    ),
                    "fix_suggestions_ko": [
                        f"team_min[{team_id}][{str(shift).upper()}] 를 {size} 이하로 조정",
                        f"팀 {team_id} 에 간호사 추가 배치",
                    ],
                })
    return issues


def run_arithmetic_detectors(input_data: Dict[str, Any]) -> List[Dict[str, Any]]:
    """편의 entrypoint — 모든 detector 일괄 실행 + issue list 반환."""
    issues: List[Dict[str, Any]] = []
    issues.extend(detect_daily_demand_exceeds_nurse_count(
        daily_shift_requirements_by_day=input_data.get("daily_shift_requirements_by_day"),
        daily_shift_requirements=input_data.get("daily_shift_requirements"),
        nurse_count=int(input_data.get("nurse_count") or 0),
        num_days=int(input_data.get("num_days") or 0),
    ))
    issues.extend(detect_off_budget_exceeds_num_days(
        nurse_off_budgets=input_data.get("nurse_off_budgets") or [],
        num_days=int(input_data.get("num_days") or 0),
    ))
    issues.extend(detect_monthly_limit_min_exceeds_max(
        monthly_limits=input_data.get("monthly_limits") or [],
    ))
    issues.extend(detect_team_min_exceeds_team_size(
        team_min_by_team=input_data.get("team_min_by_team") or {},
        team_size=input_data.get("team_size") or {},
    ))
    return issues
