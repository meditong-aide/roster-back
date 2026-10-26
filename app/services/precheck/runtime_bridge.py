"""솔버 런타임 데이터를 PrecheckInput으로 변환하는 어댑터.

`_run_cp_sat_basic` 직전 시점의 nurses_dict / config_dict / grade_config /
fixed_cells / assignments 등을 모아 `team_grade_precheck.run_precheck` 입력 형태로
바꿔준다. 솔버 호출 전에 산술적으로 명백한 infeasibility를 차단하기 위함.
"""

from __future__ import annotations

import calendar
from collections.abc import Collection
from datetime import date, datetime
from types import SimpleNamespace
from typing import Any, Dict, Iterable, List, Optional, Set

from services.cp_sat.allowed_shift_types import is_n_only_profile
from services.cp_sat.feasibility_alerts import _active_range as _engine_active_range
from services.cp_sat.off_policy import compute_off_bounds
from services.precheck.hard_lock_view import attach_engine_hard_lock
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


def _as_date(value: Any) -> date | None:
    """datetime·date·ISO 문자열 → date(그 밖이거나 읽을 수 없으면 None)."""
    if isinstance(value, datetime):  # 방어용 — 엔진 `_active_range` 가 datetime 을 date 로 바꾸므로 이 분기가 없어도 값은 같다(등가 변이)
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value[:10])
        except ValueError:
            return None
    return None


def _engine_active_span(nd: dict, year: int, month: int, days_in_month: int) -> tuple[int, int]:
    """엔진과 같은 근무 가능 구간 `(join_day, leave_day)` — 0-based, 양끝 포함(EI-10f).

    규칙은 새로 적지 않고 엔진 쪽 `feasibility_alerts._active_range` 를 그대로 부른다 — 퇴사일 그날부터 소속이 아니라
    마지막 근무일은 `resignation_date − 1` 이고, 그 달과 겹치지 않으면(지난 달 퇴사 · 다음 달 입사) 근무가 없다
    (cp_sat_basic.py:2585-2611 · fallback_lex.py:716-734 · rcs `_active_range_in_month` 와 같은 규약).
    근무일이 없으면 엔진 표기와 같은 `(1, 0)`(빈 구간)이다. 날짜는 이미 메모리에 있다(조회 0).
    """
    nurse = SimpleNamespace(
        joining_date=_as_date(nd.get("joining_date")), resignation_date=_as_date(nd.get("resignation_date")))
    span = _engine_active_range(nurse, date(year, month, 1), date(year, month, days_in_month))
    return span if span is not None else (1, 0)


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


#: 고정 칸 코드 중 OFF 로 읽는 것(이 코드가 아니면 그 날은 근무 칸이다).
_OFF_CODES = frozenset({"O", "OFF"})


def _engine_min_off_required(
    nd: dict, config_dict: dict, join_day: int, leave_day: int, days_in_month: int, *,
    preceptor_id: str | None, is_weekend_off: bool, use_mid: bool, n_blocked: int = 0,
) -> int:
    """엔진 OFF 하드 하한(min_off)의 **하한값**(EI-14r3) — `off_first=False` 이고 N 전담·주말휴무·프리셉티가 아닌 간호사에게만.

    엔진(fallback_lex.py:3253-3345)은 `compute_off_bounds` 의 `min_off_required`(휴가 제외·부분월 비례)를 slack 0 하드로 건다.
    여기서는 같은 순수 함수를 휴가 0·근무 구간 일수로 부른다 — 휴가가 있으면 엔진 값보다 크지 않게 `max(global + p, min_off)` 로만
    쓰므로(`_hard_off_floor`) 오차단이 없다. 보건휴가 가산(`extra_min_off`)은 넣지 않는다(느슨한 쪽).
    배정 차단일 수 `n_blocked` 는 엔진처럼 구간 일수에서 뺀다(`avail_days = T1 - T0 + 1 - len(차단일)` — fallback_lex.py:3263 · EI-10g1).
    제외(0): `off_first` 참 · 주말휴무자 · N 전담 · 프리셉티(`preceptor_id`) · 구간 일수 ≤ 0.
    """
    span = leave_day - join_day + 1 - max(0, int(n_blocked))
    if span <= 0 or bool(config_dict.get("off_first", False)) or is_weekend_off or preceptor_id:
        return 0
    if is_n_only_profile(nd.get("allowed_shifts"), use_mid=use_mid):
        return 0
    bounds = compute_off_bounds(
        source=config_dict, avail_days=span, vacation_cnt=0, reference_days=days_in_month, weekend_only=False)
    return max(0, int(bounds["min_off_required"]))


def _blocked_days_for(
    blocked_map: Any, nid: str, nd: dict, join_day: int, leave_day: int,
) -> tuple[int, frozenset]:
    """(엔진이 구간 일수에서 빼는 차단일 수, 근무 구간 안 차단일 집합) — EI-10g1.

    `config_dict["blocked_by_nurse_id"]`({nurse_id(str): 0-based 일자 집합})는 precheck 앞에서 이미 메모리에 있다(쿼리 0).
    엔진은 구간 밖 차단일도 `len()` 으로 센다(OFF 하한의 `avail_days`) — 수는 전부, 용량에서 빼는 날은 구간 안만 돌려준다.
    """
    if not isinstance(blocked_map, dict) or not blocked_map:
        return 0, frozenset()
    raw = blocked_map.get(nid)
    if raw is None and nd.get("db_id") is not None:
        raw = blocked_map.get(str(nd.get("db_id")))
    if not isinstance(raw, (set, frozenset, list, tuple)):
        return 0, frozenset()
    days = {d for d in raw if isinstance(d, int) and not isinstance(d, bool)}
    return len(days), frozenset(d for d in days if join_day <= d <= leave_day)


def _stage_b_constraints_for(
    initial_constraints: Any, nid: str, nd: dict, span: tuple[int, int],
    skip_days: Collection[int],
) -> tuple[frozenset, dict[int, frozenset]]:
    """(강제 OFF 날, 날 → 금지 코드) — 단계 B 제약 중 이 간호사의 근무 구간 안 · `skip_days`(차단일·고정 칸이 있는 날)가 아닌 것만(EI-10g4).

    엔진은 고정 칸이 있는 날의 금지·강제 OFF 를 무시하고(고정이 이긴다) 활동 구간 밖 날과 프리셉티 날의 금지도 건너뛴다 — 그 날들은 뺀다.
    코드는 대문자 `D/E/N/M/O` 만 쓴다(모르는 코드는 무시 = 선택지가 줄지 않는 느슨한 쪽). 모양이 틀리면 빈 값(fail-open).
    """
    if not isinstance(initial_constraints, dict):
        return frozenset(), {}
    keys = [k for k in (nid, str(nd.get("db_id")) if nd.get("db_id") is not None else None) if k]
    join_day, leave_day = span

    def _lookup(section: Any) -> Any:
        if not isinstance(section, dict):
            return None
        for key in keys:
            if key in section:
                return section[key]
        return None

    forced: set[int] = set()
    raw_forced = _lookup(initial_constraints.get("forced_off"))
    if isinstance(raw_forced, (list, tuple, set, frozenset)):
        for d in raw_forced:
            if isinstance(d, int) and not isinstance(d, bool) and join_day <= d <= leave_day and d not in skip_days:
                forced.add(d)
    forbidden: dict[int, frozenset] = {}
    raw_forbidden = _lookup(initial_constraints.get("forbidden"))
    if isinstance(raw_forbidden, dict):
        for d_raw, codes in raw_forbidden.items():
            try:
                d = int(d_raw)
            except (TypeError, ValueError):
                continue
            if not (join_day <= d <= leave_day) or d in skip_days or not isinstance(codes, (list, tuple, set, frozenset)):
                continue
            known = frozenset(str(c).strip().upper() for c in codes) & {"D", "E", "N", "M", "O"}
            if known:
                forbidden[d] = known
    return frozenset(forced), forbidden


def _engine_hard_off_days(
    weekly_days: Any, weekend_idx: set[int], span: tuple[int, int], fixed_map: dict[int, str],
) -> set[int]:
    """이 간호사에게 엔진이 하드 O 로 박는 날(0-based) — 주휴 맵의 날 ∪ 근무 구간 안 주말(EI-10e·10f).

    - 주휴: `config_dict["weekly_off_map"]`({nurse_id: [0-based 일자]})은 `roster_create_service` 가 설정 activate ·
      간호사 enabled · 변동 주기 · 주말휴무자 일요일 강제 · 정수 요일을 해석하고 활동 구간 · 파견 · 프리셉티로 거른 결과다
      — 요일을 여기서 다시 해석하지 않는다(추가 조회 0). `weekly_days` 는 그 간호사의 값(없거나 모양이 틀리면 빈 집합).
    - 주말: 엔진은 `weekend_off_only_enable` 이 켜졌을 때 주말휴무자의 활동 구간 `[join, leave]` 안 주말만 O 로 강제한다
      (fallback_lex.py:1496-1526). 호출부가 플래그·주말휴무자 여부를 보고 `weekend_idx` 를 넘긴다(아니면 빈 집합).
    같은 날은 한 번만 센다(집합). 같은 날 뒤에 오는 고정 근무 칸이 이기므로(엔진: 고정 칸 덮어쓰기, 주말 OFF 강제는 고정
    근무 우선 — fallback_lex.py:1519) 그 날은 뺀다.
    """
    join_day, leave_day = span
    days = {d for d in weekly_days if isinstance(d, int)} if isinstance(weekly_days, (list, tuple, set)) else set()
    days |= weekend_idx
    return {d for d in days if join_day <= d <= leave_day and fixed_map.get(d, "O") in _OFF_CODES}


def _weekend_day_indices(year: int, month: int) -> Set[int]:
    """주말(토/일)의 0-based day index 집합."""
    dim = calendar.monthrange(year, month)[1]
    return {d - 1 for d in range(1, dim + 1) if date(year, month, d).weekday() >= 5}


def _cell_day_index(cell: dict, year: int, month: int, days_in_month: int) -> int | None:
    """고정 칸의 0-based 일자 — `date`/`work_date` → `day_index`(엔진 형식, 0-based) → `day`(1-based) 순으로 읽는다.

    범위(달 안인지)는 부르는 쪽이 본다. 읽을 수 없는 값은 건너뛰고 다음 칸 이름을 시도한다.
    """
    day_idx = _to_day_index(cell.get("date") or cell.get("work_date"), year, month, days_in_month)
    if day_idx is not None:
        return day_idx
    for key, offset in (("day_index", 0), ("day", -1)):
        try:
            return int(cell[key]) + offset
        except (KeyError, TypeError, ValueError):
            continue
    return None


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
        day_idx = _cell_day_index(cell, year, month, days_in_month)
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
    # EI-10e: 주휴는 엔진이 만든 맵으로 센다(요일 재해석 없음).
    weekly_off_map = config_dict.get("weekly_off_map")
    # weekend_off_only_enable(엔진 기본 True) 일 때만 주말 강제휴무를 반영한다 — per-day 표시와 개인 OFF 바닥 둘 다.
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
    blocked_map = config_dict.get("blocked_by_nurse_id")

    nurses: List[PrecheckNurse] = []
    for nd in nurses_dict:
        nid = str(nd.get("nurse_id") or nd.get("db_id") or "").strip()
        if not nid:
            continue
        # EI-10f: 근무 가능 구간은 엔진과 같은 규칙으로 — 퇴사일 당일은 근무하지 않고, 그 달과 안 겹치면 근무 없음.
        join_day, leave_day = _engine_active_span(nd, year, month, days_in_month)

        # preceptor 페어링 — preceptor_id 가 있으면 본인이 preceptee.
        # 캐시(nurses.preceptor_id)는 폐지 예정 전환기 투영이라 엔진과 '동일 규칙'으로 해석한다
        # (roster_create_service._pte_authoritative 참조):
        #   authoritative(그룹 백필됨) → period 가 유일 SSOT. 미포함(종료월)=관계 해제 → 캐시 무시.
        #   비authoritative(pre-backfill·period row 전무) → 캐시 폴백(무회귀).
        # 과거엔 캐시를 base 로 깔고 period 를 '있을 때만' 덧칠해, 종료월(빈 맵)에 stale 캐시가
        # 살아남아 precheck 만 엔진(active=0)과 갈리며 PRECEPTEE_SYNC_MISMATCH false-positive 를 냈다.
        raw_ptor = nd.get("preceptor_id")
        _pinfo = _pte_period.get(nid) or _pte_period.get(str(nid))
        _period_ptor = _pinfo.get("preceptor_id") if isinstance(_pinfo, dict) else None
        if _pte_authoritative:
            raw_ptor = _period_ptor  # period 유일 SSOT — 종료월엔 None(해제)
        elif _period_ptor is not None and raw_ptor in (None, "", 0):
            raw_ptor = _period_ptor  # 캐시 비었을 때만 period 폴백
        preceptor_id = str(raw_ptor).strip() if raw_ptor not in (None, "", 0) else None

        # EI-10d: 엔진은 프리셉티의 팔로우 날 고정 칸을 걸지 않는다(fallback_lex.py:1315 `_is_preceptee_at`).
        # 날짜별 팔로우를 precheck 는 몰라 그 간호사 칸을 통째로 뺀다 — 엔진보다 느슨한 쪽(놓칠 수는 있어도 오차단은 없다).
        fixed_map = {} if preceptor_id else fixed_assignments_by_nurse.get(nid, {})
        is_weekend_off = bool(nd.get("is_weekend_off", False))

        # 개인 OFF 하드 바닥(`personal_off_adjustment`): 엔진이 하드 O 로 박는 날(주휴 맵의 날 ∪ 근무 구간 안 주말휴무 주말)을
        # **날짜 집합으로 한 번만** 센다. 엔진이 모델에 안 쓰는 이월 보정 p(레지스트리 PERSONAL-OFF-ENGINE-IGNORES)는 그 집합과
        # 더하지 않고 `max` 로 합친다 — 같은 OFF 를 두 번 빼면 엔진이 푸는 달을 막는다(p 를 하드 바닥으로 보는 것 자체는
        # 10e 이전 그대로: EI-16 큐). 하드 O 날이 없으면 p 그대로.
        personal_off = _safe_int(nd.get("personal_off_adjustment", 0), 0)
        # (EI-10g1) 배정 차단일 — 엔진은 그날 변수가 없다. 구간 안 날은 용량에서 빼고(`blocked_days`), 하드 O 날과 겹치면 한 번만 센다.
        n_blocked_all, blocked_in_span = _blocked_days_for(blocked_map, nid, nd, join_day, leave_day)
        hard_off = _engine_hard_off_days(
            weekly_off_map.get(nid) if isinstance(weekly_off_map, dict) else None,
            _weekend_idx if is_weekend_off else set(), (join_day, leave_day), fixed_map) - blocked_in_span
        if hard_off:
            personal_off = max(personal_off, len(hard_off))

        fixed_off_days: Set[int] = {d for d, code in fixed_map.items() if code in _OFF_CODES}
        # weekend-off 간호사는 주말에만 휴무 강제(weekend_off_only_enable) → 주말 = per-day 미가용.
        # 주말 날짜를 fixed_off 로 표시해 per-day 커버리지 체크가 주말 공급부족을 잡게 한다.
        if _weekend_idx and is_weekend_off:
            fixed_off_days = fixed_off_days | _weekend_idx
        fixed_shift_assignments: Dict[int, str] = {
            d: code for d, code in fixed_map.items() if code not in {"O", "OFF"}
        }

        # 단계 B 제약(EI-10g4) — 프리셉티는 엔진이 날짜별 팔로우를 따라 금지를 건너뛰므로(`_is_preceptee_at`) 통째로 뺀다(느슨한 쪽).
        forced_off_days, forbidden_by_day = ((frozenset(), {}) if preceptor_id else _stage_b_constraints_for(
            config_dict.get("initial_constraints"), nid, nd, (join_day, leave_day),
            blocked_in_span | set(fixed_map)))

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
                min_off_required=_engine_min_off_required(
                    nd, config_dict, join_day, leave_day, days_in_month,
                    preceptor_id=preceptor_id, is_weekend_off=is_weekend_off, use_mid=use_mid, n_blocked=n_blocked_all),
                blocked_days=blocked_in_span,
                forced_off_days=forced_off_days,
                forbidden_by_day=forbidden_by_day,
                fixed_off_days=fixed_off_days,
                fixed_shift_assignments=fixed_shift_assignments,
                preceptor_id=preceptor_id,
                sync_window_start=sync_start,
                sync_window_end=sync_end,
                night_cap=night_cap,
                # 진단 카드가 사번 대신 이름으로 사람을 지목하도록 실어 보낸다.
                name=(str(nd.get("name")).strip() or None) if nd.get("name") else None,
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


def prepare_precheck_input(
    *,
    nurses_dict: list[dict],
    config_dict: dict,
    grade_config: dict | None,
    fixed_cells: Iterable[dict] | None,
    year: int,
    month: int,
) -> PrecheckInput:
    """`build_precheck_input` + 엔진 하드락 실효값(EI-10c — `hard_lock_view`).

    생성 경로(`run_runtime_precheck`)가 쓰는 입구다. PoolGraph 는 `build_precheck_input` 을 직접
    부르므로(하드락 키를 읽지 않는다) 엔진 해석 출력이 거기까지 늘지 않는다. 엔진도 못 읽는
    설정이면 값을 싣지 않고 10c 이전과 같은 입력을 돌려준다.
    """
    inp = build_precheck_input(
        nurses_dict=nurses_dict, config_dict=config_dict, grade_config=grade_config,
        fixed_cells=fixed_cells, year=year, month=month,
    )
    attach_engine_hard_lock(inp.roster_config, config_dict)
    return inp


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
    inp = prepare_precheck_input(
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
