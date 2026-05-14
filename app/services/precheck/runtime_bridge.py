"""솔버 런타임 데이터를 PrecheckInput으로 변환하는 어댑터.

`_run_cp_sat_basic` 직전 시점의 nurses_dict / config_dict / grade_config /
fixed_cells / assignments 등을 모아 `team_grade_precheck.run_precheck` 입력 형태로
바꿔준다. 솔버 호출 전에 산술적으로 명백한 infeasibility를 차단하기 위함.
"""

from __future__ import annotations

import calendar
from datetime import date, datetime
from typing import Any, Dict, Iterable, List, Optional, Set

from services.precheck.team_grade_precheck import (
    PrecheckInput,
    PrecheckNurse,
    run_precheck,
)


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _to_day_index(value: Any, year: int, month: int, days_in_month: int) -> Optional[int]:
    """일자형 입력을 0-based day index로 변환한다(해당 월 외이면 None)."""
    if value is None:
        return None
    if isinstance(value, datetime):
        d = value.date()
    elif isinstance(value, date):
        d = value
    elif isinstance(value, str):
        try:
            d = date.fromisoformat(value[:10])
        except (TypeError, ValueError):
            return None
    else:
        return None
    if d.year != year or d.month != month:
        return None
    return d.day - 1


def _allowed_shifts_for(nurse: dict, use_mid: bool) -> Optional[List[str]]:
    """해당 간호사가 가능한 시프트 코드 집합을 반환한다(공집합/기본=None).

    현재 schema: ``is_night_nurse`` 는 허용 시프트 코드 list (예: ``["N"]`` = N전담,
    ``[]`` = 전부 가능). ``work_shifts`` 는 일부 legacy 데이터에서만 채워진다.
    """
    universe = {"D", "E", "N"}
    if use_mid:
        universe.add("M")

    def _normalize(raw: object) -> Optional[List[str]]:
        if raw is None:
            return None
        if isinstance(raw, str):
            codes = [c.strip().upper() for c in raw.split(",") if c.strip()]
        elif isinstance(raw, (list, tuple, set)):
            codes = [str(c).strip().upper() for c in raw if c is not None and str(c).strip()]
        else:
            return None
        cleaned = [c for c in codes if c in universe]
        return cleaned or None

    # 현재 schema 우선
    raw_nights = nurse.get("is_night_nurse")
    if isinstance(raw_nights, (list, tuple, set)):
        # 빈 list 는 "제한 없음" — None 으로 둬 universe 전체 허용
        return _normalize(raw_nights)

    # work_shifts 가 채워진 케이스
    raw_shifts = nurse.get("work_shifts")
    if raw_shifts is not None:
        return _normalize(raw_shifts)

    # 레거시: is_night_nurse 가 int/bool 인 경우
    if isinstance(raw_nights, bool):
        return ["N"] if raw_nights else None
    if isinstance(raw_nights, (int, float)):
        try:
            return ["N"] if int(raw_nights) == 3 else None
        except (TypeError, ValueError):
            return None
    return None


def _weekly_off_count(weekly_off_weekday: Any, year: int, month: int) -> int:
    """주말 휴무 weekday 패턴이 해당 월에 몇 일 발생하는지 카운트."""
    if not weekly_off_weekday:
        return 0
    if isinstance(weekly_off_weekday, str):
        days = [d.strip() for d in weekly_off_weekday.split(",") if d.strip()]
    elif isinstance(weekly_off_weekday, (list, tuple, set)):
        days = [str(d) for d in weekly_off_weekday if d]
    else:
        return 0
    weekday_map = {
        "MON": 0, "TUE": 1, "WED": 2, "THU": 3, "FRI": 4, "SAT": 5, "SUN": 6,
        "월": 0, "화": 1, "수": 2, "목": 3, "금": 4, "토": 5, "일": 6,
        "0": 0, "1": 1, "2": 2, "3": 3, "4": 4, "5": 5, "6": 6,
    }
    target_weekdays = {weekday_map.get(d.upper()) for d in days}
    target_weekdays.discard(None)
    if not target_weekdays:
        return 0
    days_in_month = calendar.monthrange(year, month)[1]
    cnt = 0
    for day in range(1, days_in_month + 1):
        if date(year, month, day).weekday() in target_weekdays:
            cnt += 1
    return cnt


def _weekend_off_count(year: int, month: int) -> int:
    """주말(토/일) 일수."""
    days_in_month = calendar.monthrange(year, month)[1]
    return sum(
        1 for d in range(1, days_in_month + 1) if date(year, month, d).weekday() >= 5
    )


def _collect_fixed_assignments(
    fixed_cells: Optional[Iterable[dict]],
    nurse_id_lookup: Dict[str, str],
    year: int,
    month: int,
    days_in_month: int,
) -> Dict[str, Dict[int, str]]:
    """fixed_cells → {nurse_id: {day_idx: shift_code}} 형태로 정리."""
    out: Dict[str, Dict[int, str]] = {}
    if not fixed_cells:
        return out
    for cell in fixed_cells:
        if not isinstance(cell, dict):
            continue
        nid = str(cell.get("nurse_id") or cell.get("db_id") or "").strip()
        if not nid:
            continue
        # nurse_id가 다른 표기인 경우 lookup 적용
        nid = nurse_id_lookup.get(nid, nid)
        day_idx = _to_day_index(cell.get("date") or cell.get("work_date"), year, month, days_in_month)
        if day_idx is None:
            day_str = cell.get("day")
            if day_str is not None:
                try:
                    day_idx = int(day_str) - 1
                except (TypeError, ValueError):
                    day_idx = None
        if day_idx is None or not (0 <= day_idx < days_in_month):
            continue
        code = str(cell.get("shift") or cell.get("shift_code") or "").strip().upper()
        if not code:
            continue
        out.setdefault(nid, {})[day_idx] = code
    return out


def _grade_constraints_for_precheck(grade_config: Optional[dict]) -> Dict[str, Any]:
    """엔진용 grade_config → precheck용 minimum/max_by_shift 형태."""
    gc = grade_config or {}
    constraints_min = gc.get("constraints") or gc.get("constraints_json") or {}
    constraints_max = gc.get("constraints_max") or gc.get("constraints_max_json") or {}

    def _normalize(m: Any) -> Dict[str, Dict[int, int]]:
        if not isinstance(m, dict):
            return {}
        out: Dict[str, Dict[int, int]] = {}
        for shift, by_grade in m.items():
            if not isinstance(by_grade, dict):
                continue
            shift_code = str(shift).upper()
            inner: Dict[int, int] = {}
            for k, v in by_grade.items():
                try:
                    inner[int(k)] = int(v or 0)
                except (TypeError, ValueError):
                    continue
            if inner:
                out[shift_code] = inner
        return out

    return {
        "minimum_by_shift": _normalize(constraints_min),
        "max_by_shift": _normalize(constraints_max),
    }


def build_precheck_input(
    *,
    nurses_dict: List[dict],
    config_dict: dict,
    grade_config: Optional[dict],
    fixed_cells: Optional[Iterable[dict]],
    year: int,
    month: int,
) -> PrecheckInput:
    """솔버 런타임 데이터를 PrecheckInput으로 변환한다."""
    days_in_month = calendar.monthrange(year, month)[1]
    use_mid = bool(config_dict.get("use_mid", False))
    weekend_off_days = _weekend_off_count(year, month)

    # nurse_id 표기 alias 매핑 (some 데이터는 db_id를 키로 씀)
    nurse_id_lookup: Dict[str, str] = {}
    for nd in nurses_dict:
        nid = str(nd.get("nurse_id") or nd.get("db_id") or "").strip()
        if nid:
            nurse_id_lookup[nid] = nid
            db_id = str(nd.get("db_id") or "").strip()
            if db_id and db_id != nid:
                nurse_id_lookup[db_id] = nid

    fixed_assignments_by_nurse = _collect_fixed_assignments(
        fixed_cells, nurse_id_lookup, year, month, days_in_month
    )

    nurses: List[PrecheckNurse] = []
    for nd in nurses_dict:
        nid = str(nd.get("nurse_id") or nd.get("db_id") or "").strip()
        if not nid:
            continue
        join_idx = _to_day_index(nd.get("joining_date"), year, month, days_in_month)
        leave_idx = _to_day_index(nd.get("resignation_date"), year, month, days_in_month)
        join_day = max(0, join_idx) if join_idx is not None else 0
        leave_day = leave_idx if leave_idx is not None else (days_in_month - 1)

        # personal off adjustment: weekly_off + is_weekend_off + 사용자 입력 보정
        personal_off = _safe_int(nd.get("personal_off_adjustment", 0), 0)
        if bool(nd.get("is_weekend_off", False)):
            personal_off += weekend_off_days
        weekly_off = _weekly_off_count(nd.get("weekly_off_weekday"), year, month)
        personal_off += weekly_off

        fixed_map = fixed_assignments_by_nurse.get(nid, {})
        fixed_off_days: Set[int] = {d for d, code in fixed_map.items() if code in {"O", "OFF"}}
        fixed_shift_assignments: Dict[int, str] = {
            d: code for d, code in fixed_map.items() if code not in {"O", "OFF"}
        }

        nurses.append(
            PrecheckNurse(
                nurse_id=nid,
                grade=nd.get("grade"),
                team_id=nd.get("team_id"),
                allowed_shifts=_allowed_shifts_for(nd, use_mid),
                join_day=join_day,
                leave_day=leave_day,
                personal_off_adjustment=personal_off,
                fixed_off_days=fixed_off_days,
                fixed_shift_assignments=fixed_shift_assignments,
            )
        )

    # team_min_by_team → team_coverage
    team_coverage: Dict[Any, Dict[str, int]] = {}
    for tid, by_shift in (config_dict.get("team_min_by_team") or {}).items():
        if not isinstance(by_shift, dict):
            continue
        cleaned: Dict[str, int] = {}
        for k, v in by_shift.items():
            code = str(k).upper()
            cnt = _safe_int(v, 0)
            if cnt > 0:
                cleaned[code] = cnt
        if cleaned:
            team_coverage[str(tid)] = cleaned

    teams = list(team_coverage.keys()) if team_coverage else []

    roster_config = {
        "use_mid": use_mid,
        "daily_shift_requirements": dict(config_dict.get("daily_shift_requirements") or {}),
        "daily_shift_requirements_by_day": list(config_dict.get("daily_shift_requirements_by_day") or []),
        "global_monthly_off_days": _safe_int(config_dict.get("global_monthly_off_days"), 0),
        "standard_personal_off_days": _safe_int(config_dict.get("standard_personal_off_days"), 0),
    }

    return PrecheckInput(
        num_days=days_in_month,
        nurses=nurses,
        teams=teams,
        roster_config=roster_config,
        team_coverage=team_coverage,
        grade_constraints=_grade_constraints_for_precheck(grade_config),
    )


def run_runtime_precheck(
    *,
    nurses_dict: List[dict],
    config_dict: dict,
    grade_config: Optional[dict],
    fixed_cells: Optional[Iterable[dict]],
    year: int,
    month: int,
    stop_on_config_error: bool = False,
) -> Dict[str, Any]:
    """런타임 데이터에 대해 precheck을 실행한 결과({status, issues})를 반환."""
    inp = build_precheck_input(
        nurses_dict=nurses_dict,
        config_dict=config_dict,
        grade_config=grade_config,
        fixed_cells=fixed_cells,
        year=year,
        month=month,
    )
    try:
        return run_precheck(inp, stop_on_config_error=stop_on_config_error)
    except Exception as exc:
        # precheck 자체가 실패하면 무시하고 솔버에 위임 (블로킹 회피)
        print(f"[Precheck] runtime precheck 실행 실패(무시): {exc}")
        return {"status": "OK", "issues": []}
