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

    현재 schema: ``allowed_shifts`` 는 허용 시프트 코드 list (예: ``["N"]`` = N전담,
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
    raw_nights = nurse.get("allowed_shifts")
    if isinstance(raw_nights, (list, tuple, set)):
        # 빈 list 는 "제한 없음" — None 으로 둬 universe 전체 허용
        return _normalize(raw_nights)

    # work_shifts 가 채워진 케이스
    raw_shifts = nurse.get("work_shifts")
    if raw_shifts is not None:
        return _normalize(raw_shifts)

    # 레거시: allowed_shifts 가 int/bool 인 경우
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


def _weekend_day_indices(year: int, month: int) -> Set[int]:
    """주말(토/일)의 0-based day index 집합."""
    dim = calendar.monthrange(year, month)[1]
    return {d - 1 for d in range(1, dim + 1) if date(year, month, d).weekday() >= 5}


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
    # weekend_off_only_enable(엔진 기본 True) 일 때만 주말 강제휴무를 per-day 로 반영.
    _weekend_off_enabled = bool(config_dict.get("weekend_off_only_enable", True))
    _weekend_idx = _weekend_day_indices(year, month) if _weekend_off_enabled else set()

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

    # period SSOT preceptee → preceptor_id overlay.
    # 엔진(cp_sat_basic)은 preceptee_period_by_nurse_id(월 authoritative)를 우선 SSOT 로 쓰지만,
    # precheck 는 그동안 nurse.preceptor_id(캐시) 만 읽어 period 경로의 preceptee 관계에 눈이 멀어
    # 있었다 → 데이터-리딩성 모순(예: 8월에 period 로 preceptee 가 되살아나며 상호배제 공존)을
    # 못 봤다. 여기서 엔진과 동일 규칙으로 매핑을 주입한다(authoritative 우선, 아니면 캐시 폴백).
    _pte_period = config_dict.get("preceptee_period_by_nurse_id") or {}
    _pte_authoritative = bool(config_dict.get("preceptee_period_authoritative"))

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
        # weekend-off 간호사는 주말에만 휴무 강제(weekend_off_only_enable) → 주말 = per-day 미가용.
        # 주말 날짜를 fixed_off 로 표시해 per-day 커버리지 체크가 주말 공급부족을 잡게 한다.
        if _weekend_idx and bool(nd.get("is_weekend_off", False)):
            fixed_off_days = fixed_off_days | _weekend_idx
        fixed_shift_assignments: Dict[int, str] = {
            d: code for d, code in fixed_map.items() if code not in {"O", "OFF"}
        }

        # preceptor 페어링 — preceptor_id 가 있으면 본인이 preceptee
        raw_ptor = nd.get("preceptor_id")
        _pinfo = _pte_period.get(nid) or _pte_period.get(str(nid))
        if isinstance(_pinfo, dict) and _pinfo.get("preceptor_id") is not None:
            # authoritative 이면 period 우선, 아니면 캐시 preceptor_id 없을 때만 폴백 (엔진 규칙과 동일)
            if _pte_authoritative or raw_ptor in (None, "", 0):
                raw_ptor = _pinfo.get("preceptor_id")
        preceptor_id = str(raw_ptor).strip() if raw_ptor not in (None, "", 0) else None
        # 동기 window 는 nurse data 에 명시되지 않으면 None → check 함수가 join/leave 로 채움
        ws_start = nd.get("sync_window_start")
        ws_end = nd.get("sync_window_end")
        sync_start = _to_day_index(ws_start, year, month, days_in_month) if ws_start else None
        sync_end = _to_day_index(ws_end, year, month, days_in_month) if ws_end else None

        # per-nurse 야간 상한: n_exact(정확값) 우선, 없으면 n_max. 엔진과 동일하게 야간 공급을
        # per-nurse 로 제약해 check_monthly_night_capacity 가 과대계산하지 않게 한다.
        _nx, _nm = nd.get("n_exact"), nd.get("n_max")
        night_cap = None
        for _v in (_nx, _nm):
            if _v is not None:
                _iv = _safe_int(_v, -1)
                if _iv >= 0:
                    night_cap = _iv
                    break

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
                preceptor_id=preceptor_id,
                sync_window_start=sync_start,
                sync_window_end=sync_end,
                night_cap=night_cap,
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
        # off 예산: DB/엔진 키는 off_days. (구 dataclass 키 standard_personal_off_days 는 fallback)
        "off_days": _safe_int(
            config_dict.get("off_days", config_dict.get("standard_personal_off_days")), 0),
    }
    # 키 정규화(2026-07-21): precheck config dict 키를 **DB/엔진 canonical(max_nig_per_month /
    # max_conseq_work / off_days)** 로 통일한다. 과거 precheck 는 max_night_shifts_per_month /
    # max_consecutive_work 를 dict 키로 읽어 실 config(DB 키)와 불일치 → 상한 로직이 조용히
    # 죽던 버그가 있었다. 이제 DB 키를 우선, 구 키는 fallback 으로만 읽고 DB 키로 output.
    #
    # 야간 한도는 엔진(create_config_from_db)과 동일 semantics 로 정규화:
    #   None / <=0  → 15 (엔진이 0·None garbage 를 15로 floor). precheck 가 엔진보다
    #   더 낮은 cap 을 쓰면 false positive 가 나므로, 반드시 동일 floor 를 적용한다.
    _raw_max_nig = config_dict.get("max_nig_per_month",
                                   config_dict.get("max_night_shifts_per_month"))
    _mn = _safe_int(_raw_max_nig, 0) if _raw_max_nig is not None else None
    if _mn is not None:
        roster_config["max_nig_per_month"] = _mn if _mn > 0 else 15

    _raw_mcw = config_dict.get("max_conseq_work",
                               config_dict.get("max_consecutive_work"))
    if _raw_mcw is not None:
        _mcw = _safe_int(_raw_mcw, 0)
        if _mcw > 0:
            roster_config["max_conseq_work"] = _mcw

    # 상호배제(배반) 맵 forward — check_preceptee_sync_mismatch 가 preceptor-preceptee 페어가
    # 동시에 상호배제로 걸린 모순(함께근무 + 배반)을 탐지하는 데 쓴다.
    _mx = config_dict.get("mutual_exclusion_by_nurse_id")
    if _mx:
        roster_config["mutual_exclusion_by_nurse_id"] = _mx

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
