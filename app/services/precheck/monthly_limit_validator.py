"""nurse_monthly_limit 저장 시점 산술 모순 검증.

정책:
- vacation은 OFF로 카운트하지 않는다 (active_days만 줄이고 OFF 통계엔 미포함).
- 강제 OFF = is_weekend_off의 주말 + weekly_off_weekday 일자 (vacation 제외).
- fixed_wanted는 절대 진리이므로 limit 저장 시점에서 별도 cross check 안 함.
- 검증 통과 못한 경우 HTTP 500 + structured infeasibility payload로 응답.
"""

from __future__ import annotations

import calendar
from datetime import date
from typing import Any, Dict, List, Mapping, Optional, Set

from db.models import Nurse


_SHIFT_LABEL = {"d": "주간(D)", "e": "오후(E)", "n": "야간(N)", "o": "휴무(O)"}
_SHIFT_PREFIXES = ("d", "e", "n", "o")
_WORK_PREFIXES = ("d", "e", "n")  # OFF 제외


# ---------------------------------------------------------------------------
# 산술 헬퍼
# ---------------------------------------------------------------------------

_KOREAN_WEEKDAY_MAP = {
    "월": 0, "화": 1, "수": 2, "목": 3, "금": 4, "토": 5, "일": 6,
    "MON": 0, "TUE": 1, "WED": 2, "THU": 3, "FRI": 4, "SAT": 5, "SUN": 6,
    "0": 0, "1": 1, "2": 2, "3": 3, "4": 4, "5": 5, "6": 6,
}


def _weekend_count_in_month(year: int, month: int) -> int:
    days_in_month = calendar.monthrange(year, month)[1]
    return sum(
        1 for d in range(1, days_in_month + 1)
        if date(year, month, d).weekday() >= 5
    )


def _weekly_off_weekdays(value: Any) -> Set[int]:
    """nurse.weekly_off_weekday가 어떤 형식이든 weekday set(0=월~6=일)으로 정규화."""
    if value is None:
        return set()
    if isinstance(value, int):
        return {value % 7}
    items: List[Any]
    if isinstance(value, str):
        items = [t.strip() for t in value.split(",") if t.strip()]
    elif isinstance(value, (list, tuple, set)):
        items = list(value)
    else:
        return set()
    out: Set[int] = set()
    for it in items:
        if isinstance(it, int):
            out.add(it % 7)
            continue
        key = str(it).strip().upper()
        if key in _KOREAN_WEEKDAY_MAP:
            out.add(_KOREAN_WEEKDAY_MAP[key])
    return out


def _weekly_off_count_in_month(weekday_set: Set[int], year: int, month: int) -> int:
    if not weekday_set:
        return 0
    days_in_month = calendar.monthrange(year, month)[1]
    return sum(
        1 for d in range(1, days_in_month + 1)
        if date(year, month, d).weekday() in weekday_set
    )


def _normalize_shift_list(raw: Any) -> Set[str]:
    """JSON list / 콤마 문자열 / int → 시프트 코드 집합."""
    if raw is None:
        return set()
    if isinstance(raw, str):
        return {t.strip().upper() for t in raw.split(",") if t.strip()}
    if isinstance(raw, (list, tuple, set)):
        return {str(t).strip().upper() for t in raw if t}
    return set()


def _allowed_work_shifts(nurse: Nurse) -> Optional[Set[str]]:
    """nurse가 가능한 D/E/N 시프트 집합. None이면 모든 시프트 가능."""
    universe = {"D", "E", "N"}
    nn_set = _normalize_shift_list(getattr(nurse, "is_night_nurse", None)) & universe
    ws_set = _normalize_shift_list(getattr(nurse, "work_shifts", None)) & universe
    if nn_set and ws_set:
        return nn_set & ws_set or None
    if nn_set:
        return nn_set
    if ws_set:
        return ws_set
    return None  # 제한 없음


def _is_night_dedicated(nurse: Nurse) -> bool:
    allowed = _allowed_work_shifts(nurse)
    return allowed == {"N"}


def _row_value(row: Mapping[str, Any], key: str) -> Optional[int]:
    v = row.get(key)
    if v is None:
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Issue builder
# ---------------------------------------------------------------------------

def _make_issue(
    code: str,
    *,
    nurse_id: str,
    nurse_name: Optional[str],
    evidence: Dict[str, Any],
    human_message_ko: str,
    fix_suggestions_ko: List[str],
    severity: str = "blocking",
) -> Dict[str, Any]:
    ev = {"nurse_id": nurse_id}
    if nurse_name:
        ev["nurse_name"] = nurse_name
    ev.update(evidence)
    return {
        "reason_code": code,
        "severity": severity,
        "evidence": ev,
        "human_message_ko": human_message_ko,
        "fix_suggestions_ko": fix_suggestions_ko,
    }


# ---------------------------------------------------------------------------
# 개별 검사
# ---------------------------------------------------------------------------

def _check_single_bounds(
    row: Mapping[str, Any],
    *,
    nurse_id: str,
    nurse_name: Optional[str],
    cap_days: int,
) -> List[Dict[str, Any]]:
    issues: List[Dict[str, Any]] = []
    for p in _SHIFT_PREFIXES:
        mn = _row_value(row, f"{p}_min")
        mx = _row_value(row, f"{p}_max")
        ex = _row_value(row, f"{p}_exact")

        # 정책: exact가 입력되면 _normalize_row가 min=max=exact로 자동 동기화한다.
        # 프론트는 exact만 보내고 min/max는 null로 보내는 경우가 정상이므로 별도 충돌 검사 안 함.
        # B1: 단일 시프트 min(또는 exact)이 가용일 초과 — exact가 있으면 exact 한 번만 검사
        if ex is not None:
            if ex > cap_days:
                issues.append(_make_issue(
                    "MONTHLY_LIMIT_VALUE_EXCEEDS_ACTIVE_DAYS",
                    nurse_id=nurse_id, nurse_name=nurse_name,
                    evidence={"shift": p.upper(), "field": f"{p}_exact", "value": ex, "active_days": cap_days},
                    human_message_ko=(
                        f"{_SHIFT_LABEL[p]} exact={ex}가 가용일 {cap_days}일을 초과합니다."
                    ),
                    fix_suggestions_ko=[
                        f"{_SHIFT_LABEL[p]} exact를 {cap_days} 이하로 설정하세요.",
                    ],
                ))
        elif mn is not None and mn > cap_days:
            issues.append(_make_issue(
                "MONTHLY_LIMIT_VALUE_EXCEEDS_ACTIVE_DAYS",
                nurse_id=nurse_id, nurse_name=nurse_name,
                evidence={"shift": p.upper(), "field": f"{p}_min", "value": mn, "active_days": cap_days},
                human_message_ko=(
                    f"{_SHIFT_LABEL[p]} min={mn}이 가용일 {cap_days}일을 초과합니다."
                ),
                fix_suggestions_ko=[
                    f"{_SHIFT_LABEL[p]} min을 {cap_days} 이하로 설정하세요.",
                ],
            ))
    return issues


def _check_sum_coverage(
    row: Mapping[str, Any],
    *,
    nurse_id: str,
    nurse_name: Optional[str],
    active_days_for_limits: int,
) -> List[Dict[str, Any]]:
    """B3: sum(max) ≥ active_days_for_limits, B4: sum(max) ≥ sum(min)."""
    issues: List[Dict[str, Any]] = []
    sum_min = 0
    any_max_specified = False
    sum_max = 0
    for p in _SHIFT_PREFIXES:
        ex = _row_value(row, f"{p}_exact")
        mn = _row_value(row, f"{p}_min")
        mx = _row_value(row, f"{p}_max")
        eff_min = ex if ex is not None else (mn or 0)
        eff_max = ex if ex is not None else mx
        sum_min += int(eff_min)
        if eff_max is not None:
            any_max_specified = True
            sum_max += int(eff_max)

    # B4: sum(max) < sum(min) — 정의역 빈집합 (max 명시 시만 의미)
    if any_max_specified and sum_max < sum_min:
        issues.append(_make_issue(
            "MONTHLY_LIMIT_SUM_MAX_BELOW_SUM_MIN",
            nurse_id=nurse_id, nurse_name=nurse_name,
            evidence={"sum_min": sum_min, "sum_max": sum_max},
            human_message_ko=(
                f"시프트별 max 합({sum_max})이 min 합({sum_min})보다 작아 정의역이 비어있습니다."
            ),
            fix_suggestions_ko=[
                "min 합을 줄이거나 max를 늘려주세요.",
            ],
        ))

    # B3: 모든 시프트(D/E/N/O)에 max가 명시되어 있고 합이 active_days_for_limits 미만
    all_max_specified = all(
        _row_value(row, f"{p}_exact") is not None or _row_value(row, f"{p}_max") is not None
        for p in _SHIFT_PREFIXES
    )
    if all_max_specified and sum_max < active_days_for_limits:
        issues.append(_make_issue(
            "MONTHLY_LIMIT_SUM_MAX_BELOW_ACTIVE_DAYS",
            nurse_id=nurse_id, nurse_name=nurse_name,
            evidence={"sum_max": sum_max, "active_days": active_days_for_limits},
            human_message_ko=(
                f"시프트별 max 합({sum_max})이 가용일 {active_days_for_limits}일을 채우지 못합니다. "
                "모든 일자에 어떤 시프트로든 배정되어야 하는데 max 총합이 부족합니다."
            ),
            fix_suggestions_ko=[
                "어떤 시프트의 max를 늘리거나 max 입력을 일부 제거하세요.",
            ],
        ))
    return issues


def _check_night_dedicated(
    row: Mapping[str, Any],
    *,
    nurse_id: str,
    nurse_name: Optional[str],
    nurse: Nurse,
) -> List[Dict[str, Any]]:
    issues: List[Dict[str, Any]] = []
    if not _is_night_dedicated(nurse):
        return issues
    for p in ("d", "e"):
        mn = _row_value(row, f"{p}_min")
        ex = _row_value(row, f"{p}_exact")
        if (mn is not None and mn > 0) or (ex is not None and ex > 0):
            issues.append(_make_issue(
                "MONTHLY_LIMIT_NIGHT_DEDICATED_HAS_DAY_OR_EVENING",
                nurse_id=nurse_id, nurse_name=nurse_name,
                evidence={"shift": p.upper(), "min": mn, "exact": ex},
                human_message_ko=(
                    f"{nurse_name or nurse_id} 간호사는 N 전담입니다. "
                    f"{_SHIFT_LABEL[p]} min/exact 양수 설정은 불가능합니다."
                ),
                fix_suggestions_ko=[
                    f"{_SHIFT_LABEL[p]} min/exact를 0으로 두거나 N 전담 해제 후 다시 시도하세요.",
                ],
            ))
    return issues


# N전담이 가용일의 절반 이하로 N이 묶이면 사실상 대부분 OFF → 가용성 신호로 본다.
_NIGHT_LOW_N_RATIO = 0.5


def warn_night_dedicated_low_n(
    row: Mapping[str, Any],
    *,
    nurse_id: str,
    nurse_name: Optional[str],
    nurse: Nurse,
    cap_days: int,
) -> List[Dict[str, Any]]:
    """Soft 경고(하드 차단 아님): N전담인데 N 한도(n_exact 우선, 없으면 n_max)가 낮아
    사실상 대부분 OFF가 되는 경우.

    이 모순은 *항상* infeasible은 아니다 — 그 병동 야간을 다른 간호사가 채우면 풀린다
    (커버리지 의존). 따라서 저장 자체는 막지 않고, 설정 시점에 경고만 띄워
    "단기 부재라면 제한가용/휴직으로 모델링" 하도록 유도한다. 최종 하드 게이트는
    생성 precheck(`cause:config:monthly_limit_n_exact_unattainable`)가 담당한다.
    """
    if not _is_night_dedicated(nurse):
        return []
    n_exact = _row_value(row, "n_exact")
    n_max = _row_value(row, "n_max")
    n_cap = n_exact if n_exact is not None else n_max
    if n_cap is None or cap_days <= 0:
        return []
    if n_cap <= int(cap_days * _NIGHT_LOW_N_RATIO):
        implied_off = max(0, cap_days - n_cap)
        label = "정확" if n_exact is not None else "최대"
        return [{
            "code": "MONTHLY_LIMIT_NIGHT_DEDICATED_LOW_N",
            "message": (
                f"{nurse_name or nurse_id} 간호사는 N 전담인데 N {label} 한도가 {n_cap}회로 "
                f"낮아, 가용 {cap_days}일 중 약 {implied_off}일이 강제 OFF가 됩니다. "
                f"야간 커버리지가 부족하면 근무표 생성이 실패할 수 있습니다. "
                f"단기 부재라면 N 한도 대신 제한가용/휴직 구간으로 설정하는 것을 권장합니다."
            ),
        }]
    return []


def _check_work_shifts(
    row: Mapping[str, Any],
    *,
    nurse_id: str,
    nurse_name: Optional[str],
    nurse: Nurse,
) -> List[Dict[str, Any]]:
    """C3: nurse.work_shifts에 없는 시프트에 limit 양수가 들어가면 모순."""
    allowed = _allowed_work_shifts(nurse)
    if allowed is None:
        return []
    issues: List[Dict[str, Any]] = []
    for p in _WORK_PREFIXES:
        if p.upper() in allowed:
            continue
        mn = _row_value(row, f"{p}_min")
        ex = _row_value(row, f"{p}_exact")
        if (mn is not None and mn > 0) or (ex is not None and ex > 0):
            issues.append(_make_issue(
                "MONTHLY_LIMIT_NOT_IN_WORK_SHIFTS",
                nurse_id=nurse_id, nurse_name=nurse_name,
                evidence={"shift": p.upper(), "min": mn, "exact": ex, "allowed": sorted(allowed)},
                human_message_ko=(
                    f"{nurse_name or nurse_id} 간호사의 가능 시프트는 {sorted(allowed)} 입니다. "
                    f"{_SHIFT_LABEL[p]} min/exact 양수 설정은 불가능합니다."
                ),
                fix_suggestions_ko=[
                    f"{_SHIFT_LABEL[p]} min/exact를 0으로 두거나 work_shifts에 {p.upper()}를 추가하세요.",
                ],
            ))
    return issues


def _check_forced_off(
    row: Mapping[str, Any],
    *,
    nurse_id: str,
    nurse_name: Optional[str],
    forced_off_count: int,
    active_days_for_limits: int,
) -> List[Dict[str, Any]]:
    """C5/D1/F1: 강제 OFF(weekend_off + weekly_off, vacation 제외) vs o_*, work_shift 합."""
    issues: List[Dict[str, Any]] = []
    if forced_off_count <= 0:
        return issues

    o_min = _row_value(row, "o_min")
    o_max = _row_value(row, "o_max")
    o_exact = _row_value(row, "o_exact")

    # o_max < forced_off_count (모순)
    eff_o_max = o_exact if o_exact is not None else o_max
    if eff_o_max is not None and eff_o_max < forced_off_count:
        issues.append(_make_issue(
            "MONTHLY_LIMIT_O_MAX_BELOW_FORCED_OFF",
            nurse_id=nurse_id, nurse_name=nurse_name,
            evidence={"o_max": eff_o_max, "forced_off_count": forced_off_count},
            human_message_ko=(
                f"강제 OFF 일수({forced_off_count}일, 주말휴무/주휴 합)가 o_max={eff_o_max}을 초과합니다."
            ),
            fix_suggestions_ko=[
                f"o_max를 {forced_off_count} 이상으로 설정하세요.",
                "주말휴무 또는 weekly_off 설정을 확인하세요.",
            ],
        ))

    # work shift max 합 > 가용일 - 강제 OFF
    work_max_sum = 0
    all_work_max_specified = True
    for p in _WORK_PREFIXES:
        ex = _row_value(row, f"{p}_exact")
        mx = _row_value(row, f"{p}_max")
        eff = ex if ex is not None else mx
        if eff is None:
            all_work_max_specified = False
            break
        work_max_sum += int(eff)
    if all_work_max_specified:
        work_capacity = active_days_for_limits - forced_off_count
        # work_max_sum이 work_capacity를 초과해도 정상 (기능적으로 work_capacity까지만 사용)
        # 하지만 work_min 합이 work_capacity를 초과하면 모순
    work_min_sum = 0
    for p in _WORK_PREFIXES:
        ex = _row_value(row, f"{p}_exact")
        mn = _row_value(row, f"{p}_min")
        eff = ex if ex is not None else (mn or 0)
        work_min_sum += int(eff)
    work_capacity = active_days_for_limits - forced_off_count
    if work_min_sum > work_capacity:
        issues.append(_make_issue(
            "MONTHLY_LIMIT_WORK_MIN_EXCEEDS_AVAILABLE_WEEKDAYS",
            nurse_id=nurse_id, nurse_name=nurse_name,
            evidence={
                "work_min_sum": work_min_sum,
                "active_days": active_days_for_limits,
                "forced_off_count": forced_off_count,
                "work_capacity": work_capacity,
            },
            human_message_ko=(
                f"D/E/N min 합({work_min_sum})이 강제 OFF({forced_off_count}일)를 제외한 "
                f"가능 근무일 {work_capacity}일을 초과합니다."
            ),
            fix_suggestions_ko=[
                "D/E/N min 합을 줄이세요.",
                "주말휴무/weekly_off 설정을 확인하세요.",
            ],
        ))

    return issues


# ---------------------------------------------------------------------------
# 진입점
# ---------------------------------------------------------------------------

def validate_monthly_limit_row(
    *,
    row: Mapping[str, Any],
    nurse: Nurse,
    cap_days: int,
    year: int,
    month: int,
) -> List[Dict[str, Any]]:
    """단일 (nurse, group, year, month) row에 대한 산술 모순 검사.

    Args:
        row: limit row dict ({d_min, d_max, d_exact, ...})
        nurse: Nurse ORM 객체 (work_shifts, is_night_nurse, is_weekend_off, weekly_off_weekday 등)
        cap_days: 그룹 활성 가용일 (build_blocked_days 반영, vacation 제외 전)
        year/month: 검증 대상 월

    Returns:
        list of issue dicts. 빈 리스트면 통과.
    """
    nurse_id = str(getattr(nurse, "nurse_id", ""))
    nurse_name = getattr(nurse, "name", None)

    # 강제 OFF 일수 산정 (vacation 제외!)
    weekend_count = 0
    if bool(getattr(nurse, "is_weekend_off", False)):
        weekend_count = _weekend_count_in_month(year, month)

    weekly_off_weekday_set = _weekly_off_weekdays(getattr(nurse, "weekly_off_weekday", None))
    weekly_off_count = _weekly_off_count_in_month(weekly_off_weekday_set, year, month)

    # 중복 방지(같은 일자가 weekend_off와 weekly_off에 모두 해당될 수 있음 — 단순 합)
    # 정확한 union이 필요하다면 일자별 set 계산이지만, 보수적으로 sum 사용.
    # weekend_off=True가 아닌데 weekly_off에 토/일 들어있는 경우가 정상이므로 sum으로 처리.
    forced_off_count = weekend_count + weekly_off_count
    if forced_off_count > cap_days:
        forced_off_count = cap_days

    # active_days_for_limits = cap_days (vacation은 별도 ; 본 검사에선 cap_days 그대로 사용)
    active_days_for_limits = cap_days

    issues: List[Dict[str, Any]] = []
    issues.extend(_check_single_bounds(row, nurse_id=nurse_id, nurse_name=nurse_name, cap_days=cap_days))
    issues.extend(_check_sum_coverage(row, nurse_id=nurse_id, nurse_name=nurse_name, active_days_for_limits=active_days_for_limits))
    issues.extend(_check_night_dedicated(row, nurse_id=nurse_id, nurse_name=nurse_name, nurse=nurse))
    issues.extend(_check_work_shifts(row, nurse_id=nurse_id, nurse_name=nurse_name, nurse=nurse))
    issues.extend(_check_forced_off(
        row,
        nurse_id=nurse_id,
        nurse_name=nurse_name,
        forced_off_count=forced_off_count,
        active_days_for_limits=active_days_for_limits,
    ))
    return issues


def check_group_n_pool(
    *,
    group_id: str,
    monthly_n_demand: int,
    forced_n_sum: int,
    free_capacity_sum: int,
    nurses_with_n_forced: int,
    total_n_capable_nurses: int,
    daily_n_max_total: Optional[int],
    days_in_month: int,
) -> List[Dict[str, Any]]:
    """그룹 단위 N 풀 산술 검증.

    Args:
        forced_n_sum: 강제(n_exact) 설정된 nurse들의 합
        free_capacity_sum: 자유 nurse(n_exact 미설정)의 가용 N 상한 합
            (n_max가 있으면 n_max, 없으면 active_days)
        nurses_with_n_forced: n_exact가 설정된 nurse 수
        total_n_capable_nurses: N이 가능한 nurse 총 수
        daily_n_max_total: 월간 daily_max(N) 합. None이면 상한 없음.
        monthly_n_demand: 30일 × 일별 N 요구치 합
    """
    issues: List[Dict[str, Any]] = []

    total_capacity = forced_n_sum + free_capacity_sum
    if monthly_n_demand > 0 and total_capacity < monthly_n_demand:
        issues.append({
            "reason_code": "MONTHLY_LIMIT_GROUP_N_CAPACITY_BELOW_DEMAND",
            "severity": "blocking",
            "evidence": {
                "group_id": group_id,
                "monthly_n_demand": monthly_n_demand,
                "forced_n_sum": forced_n_sum,
                "free_capacity_sum": free_capacity_sum,
                "total_capacity": total_capacity,
            },
            "human_message_ko": (
                f"그룹 N 가용 합({total_capacity})이 월간 N 요구({monthly_n_demand})에 부족합니다. "
                f"(강제 합 {forced_n_sum} + 자유 한도 합 {free_capacity_sum})"
            ),
            "fix_suggestions_ko": [
                "일부 nurse의 n_exact를 늘리세요.",
                "n_exact를 빼고 자유 nurse를 늘리세요.",
                "야간 가능 간호사를 추가 배치하세요.",
            ],
        })

    # 모든 N 가능 nurse가 forced(n_exact)인데 합이 부족 → 자유도 0 + 부족
    if (
        nurses_with_n_forced > 0
        and nurses_with_n_forced == total_n_capable_nurses
        and monthly_n_demand > 0
        and forced_n_sum < monthly_n_demand
    ):
        issues.append({
            "reason_code": "MONTHLY_LIMIT_GROUP_N_FORCED_SUM_BELOW_DEMAND",
            "severity": "blocking",
            "evidence": {
                "group_id": group_id,
                "monthly_n_demand": monthly_n_demand,
                "forced_n_sum": forced_n_sum,
                "nurses_forced": nurses_with_n_forced,
            },
            "human_message_ko": (
                f"모든 N 가능 간호사 {nurses_with_n_forced}명이 n_exact로 고정되었지만 "
                f"합({forced_n_sum})이 월 N 요구({monthly_n_demand})에 부족합니다."
            ),
            "fix_suggestions_ko": [
                "일부 nurse의 n_exact를 빼서 솔버 자율 분배에 맡기세요.",
                "n_exact 값을 더 큰 수로 조정하세요.",
            ],
        })

    # 강제 합이 일별 N max로 분배 불가 (daily_n_max_total = days × max)
    if (
        daily_n_max_total is not None
        and daily_n_max_total > 0
        and forced_n_sum > daily_n_max_total
    ):
        issues.append({
            "reason_code": "MONTHLY_LIMIT_GROUP_N_FORCED_EXCEEDS_DAILY_CAP",
            "severity": "blocking",
            "evidence": {
                "group_id": group_id,
                "forced_n_sum": forced_n_sum,
                "daily_n_max_total": daily_n_max_total,
                "days_in_month": days_in_month,
            },
            "human_message_ko": (
                f"강제 N 합({forced_n_sum})이 월간 일별 N 상한 합({daily_n_max_total})을 초과합니다."
            ),
            "fix_suggestions_ko": [
                "일부 nurse의 n_exact를 줄이세요.",
                "일별 N 상한(daily_shift_requirements_max_by_day)을 늘리세요.",
            ],
        })

    return issues


def build_validation_payload(issues: List[Dict[str, Any]]) -> Dict[str, Any]:
    """validate_monthly_limit_row / check_group_n_pool 결과를 HTTP 422 detail 페이로드로 변환.

    일괄 저장 시 여러 nurse 의 issue 가 함께 올 수 있으므로, human_message_ko 들을
    중복 제거 후 줄바꿈으로 조합해 summary 로 노출한다(단건이면 메시지 그대로).
    """
    msgs: List[str] = []
    seen_msgs: Set[str] = set()
    for it in issues:
        m = it.get("human_message_ko")
        if m and m not in seen_msgs:
            seen_msgs.add(m)
            msgs.append(m)
    if not msgs:
        summary = "월 시프트 한도 입력에 산술적으로 불가능한 항목이 있습니다."
    elif len(msgs) == 1:
        summary = msgs[0]
    else:
        summary = f"{len(msgs)}건의 설정이 불가합니다:\n" + "\n".join(f"- {m}" for m in msgs)
    fix_suggestions: List[str] = []
    seen: Set[str] = set()
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
