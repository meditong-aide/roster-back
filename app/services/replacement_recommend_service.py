from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta
import logging
from math import exp
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy.orm import Session

from services.group_access import assert_caller_can_access_group

from db.models import (
    FixedWantedEntry,
    Nurse,
    NurseAssignment,
    NursePairRequest,
    NurseShiftRequest,
    RosterConfig,
    Schedule,
    ScheduleEntry,
    Shift,
    WantedRequest,
)
from schemas.replacement_schema import (
    BulkPathRecommendation,
    BulkPathStep,
    CandidateRecommendation,
    CandidateScoreBreakdown,
    ConstraintFactors,
    ExcludedCandidate,
    PathViolationDetail,
    PathViolationSummary,
    ReplacementRecommendRequest,
    ReplacementRecommendResponse,
    ReplacementSlot,
    SlotRecommendation,
)


OFF_SHIFT_IDS = {"O", "OFF", "-", "주"}

# Violation type severity weights — higher = more impactful violation.
# Used in BULK path scoring to penalize severe violations more heavily.
VIOLATION_TYPE_WEIGHT: Dict[str, float] = {
    "night_15n_hard_limit": 5.0,  # 월 15N 절대상한 초과 — 최고 심각도
    "off_day_shortage": 3.5,      # 월간 OFF 일수 기준 미달
    "rec_3n2o": 3.0,              # 3연속 나이트 후 OFF 부족
    "consecutive_work": 2.5,      # 최대 연속 근무 초과
    "night_month_limit": 2.0,     # 월간 나이트 한도 초과
    "rec_2n2o": 1.8,              # 2연속 나이트 후 OFF 부족
    "night_nd": 1.5,              # N→D 전환 위반
    "night_ne": 1.5,              # N→E 전환 위반
    "daily_coverage_shortage": 2.0,  # 일자별 최소 인원(D/E/N) 악화
    "eve_ed": 1.0,                # E→D 전환 위반
}
VACATION_SHIFT_TYPE_TOKENS = {"휴가", "vacation", "annual_leave", "annual leave", "연차", "연가"}
WORK_SHIFT_GB_TO_MAIN = {
    "데이": "D",
    "이브닝": "E",
    "미드": "M",
    "나이트": "N",
    "D": "D",
    "E": "E",
    "M": "M",
    "N": "N",
}

logger = logging.getLogger(__name__)


def _is_candidate_allowed_by_scope(
    scope: str,
    is_off_rest: bool,
    is_vacation: bool,
) -> bool:
    """ranking_scope 필터 — SINGLE/BULK 공통.

    OFF_ONLY 는 순수 휴무(rest)만 허용하며 휴가는 제외.
    VACATION_ONLY 는 휴가만 허용. ON_DUTY_ONLY 는 비-휴무·비-휴가만.
    조합 모드(ON_DUTY_OR_*, OFF_OR_VACATION) 는 union.
    """
    is_on_duty = not (is_off_rest or is_vacation)
    if scope == "ALL":
        return True
    if scope == "ON_DUTY_ONLY":
        return is_on_duty
    if scope == "OFF_ONLY":
        return is_off_rest
    if scope == "VACATION_ONLY":
        return is_vacation
    if scope == "ON_DUTY_OR_OFF":
        return is_on_duty or is_off_rest
    if scope == "ON_DUTY_OR_VACATION":
        return is_on_duty or is_vacation
    if scope == "OFF_OR_VACATION":
        return is_off_rest or is_vacation
    return True


@dataclass
class CandidateContext:
    nurse_id: str
    nurse_name: str
    nurse_grade: Optional[int]
    assigned_shift: str
    assigned_shift_pk_id: Optional[str]
    assigned_is_off: bool
    assigned_is_vacation: bool
    rule_safety: float
    estimated_violation_delta: float
    off_priority: float
    grade_fit: float
    preference: float
    pair: float
    fairness: float
    change_cost: float
    vacation_penalty: float
    tags: List[str]


def _is_vacation_shift(shift_id: str, shift_meta: Dict[str, Dict[str, Any]]) -> bool:
    _main, _is_off_rest, is_vacation, _is_off_any, _reason = _resolve_shift_semantic_with_reason(
        shift_id,
        shift_meta,
    )
    return is_vacation


def _build_assignment_blocked_dates(
    db: Session,
    group_id: str,
    year: int,
    month: int,
    days_in_month: int,
) -> Dict[str, set]:
    """해당 월의 간호사별 blocked date 집합을 구성한다.

    파견/병동이동/휴직: source_group_id == group_id인 아웃바운드 기간(NurseAssignment)
    프리셉티: nurse_preceptee_period(SSOT) 의 그 달 겹치는 구간(assignment 안 봄 — 도려내기)
    """
    month_start = date(year, month, 1)
    month_end = date(year, month, days_in_month)

    assignments = (
        db.query(NurseAssignment)
        .filter(
            NurseAssignment.status != "cancelled",
            NurseAssignment.start_date <= month_end,
        )
        .all()
    )

    blocked: Dict[str, set] = {}
    for a in assignments:
        a_start = a.start_date
        a_end = a.end_date or a.expected_end_date or month_end

        if a_end < month_start or a_start > month_end:
            continue

        nid = str(a.nurse_id)
        overlap_start = max(a_start, month_start)
        overlap_end = min(a_end, month_end)

        # 아웃바운드 (파견/병동이동/휴직) — source 병동 기준
        if a.reason in ("파견", "병동이동", "휴직") and str(a.source_group_id) == group_id:
            if nid not in blocked:
                blocked[nid] = set()
            d = overlap_start
            while d <= overlap_end:
                blocked[nid].add(d)
                d += timedelta(days=1)

    # 프리셉티 — nurse_preceptee_period(SSOT) 의 그 달 겹치는 날짜를 blocked.
    #   resolver 는 {nid: {"days": set(0-based day index)}} 반환 → date 로 환산.
    from services.preceptee_period import resolve_preceptee_days_for_month
    _grp_nurse_ids = [
        str(nid) for (nid,) in db.query(Nurse.nurse_id)
        .filter(Nurse.group_id == group_id).all()
    ]
    _pte_days = resolve_preceptee_days_for_month(db, _grp_nurse_ids, year, month)
    for nid, info in _pte_days.items():
        s = blocked.setdefault(str(nid), set())
        for d0 in info.get("days", set()):
            s.add(month_start + timedelta(days=int(d0)))

    # fixed_wanted_entries — 고정 셀이 있는 날짜는 추천 제외
    fixed_entries = (
        db.query(FixedWantedEntry.nurse_id, FixedWantedEntry.shift_date)
        .filter(
            FixedWantedEntry.group_id == group_id,
            FixedWantedEntry.year == year,
            FixedWantedEntry.month == month,
            FixedWantedEntry.is_applied == True,
        )
        .all()
    )
    for nid_raw, shift_date in fixed_entries:
        nid = str(nid_raw)
        if nid not in blocked:
            blocked[nid] = set()
        blocked[nid].add(shift_date)

    return blocked


def _to_date(v: Any) -> Optional[date]:
    if v is None:
        return None
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    if isinstance(v, str):
        try:
            return datetime.fromisoformat(v).date()
        except Exception:
            return None
    return None


def _normalize_shift_code(raw_shift: str) -> str:
    return str(raw_shift or "").strip().upper()


def _is_off_shift(shift_id: str, shift_meta: Dict[str, Dict[str, Any]]) -> bool:
    _main, _is_off_rest, _is_vacation, is_off_any, _reason = _resolve_shift_semantic_with_reason(
        shift_id,
        shift_meta,
    )
    return is_off_any


def _main_shift_code(shift_id: str, shift_meta: Dict[str, Dict[str, Any]]) -> str:
    code, _reason = _main_shift_code_with_reason(shift_id, shift_meta)
    return code


def _main_shift_code_with_reason(
    shift_id: str,
    shift_meta: Dict[str, Dict[str, Any]],
    shift_pk: Optional[str] = None,
    shift_meta_by_pk: Optional[Dict[str, Dict[str, Any]]] = None,
) -> Tuple[str, Optional[str]]:
    main_code, _is_off_rest, _is_vacation, _is_off_any, reason = _resolve_shift_semantic_with_reason(
        shift_id,
        shift_meta,
        shift_pk=shift_pk,
        shift_meta_by_pk=shift_meta_by_pk,
    )
    return main_code, reason


def _resolve_shift_semantic_with_reason(
    shift_id: str,
    shift_meta: Dict[str, Dict[str, Any]],
    shift_pk: Optional[str] = None,
    shift_meta_by_pk: Optional[Dict[str, Dict[str, Any]]] = None,
) -> Tuple[str, bool, bool, bool, Optional[str]]:
    meta: Optional[Dict[str, Any]] = None
    if shift_pk is not None and shift_meta_by_pk is not None:
        meta = shift_meta_by_pk.get(str(shift_pk))
    if meta is None:
        meta = shift_meta.get(str(shift_id))

    if meta is None:
        fallback_code = _normalize_shift_code(shift_id)
        if fallback_code in OFF_SHIFT_IDS:
            return "O", True, False, True, "missing_shift_meta"
        if fallback_code in {"D", "E", "M", "N"}:
            return fallback_code, False, False, False, "missing_shift_meta"
        return "W", False, False, False, "missing_shift_meta"

    shift_type_raw = str(meta.get("type") or "").strip()
    shift_type_norm = shift_type_raw.lower()
    shift_gb = str(meta.get("shift_gb") or "").strip()

    if shift_type_raw == "휴무":
        return "O", True, False, True, None

    is_vacation = bool(
        shift_type_norm
        and (shift_type_norm in VACATION_SHIFT_TYPE_TOKENS or "휴가" in shift_type_norm or "vacation" in shift_type_norm)
    )
    if is_vacation:
        return "O", False, True, True, None

    if shift_type_raw == "근무":
        return WORK_SHIFT_GB_TO_MAIN.get(shift_gb, "W"), False, False, False, None

    return "W", False, False, False, "unresolved_shift_type"


def _build_schedule_map(entries: List[Any], days_in_month: int) -> Dict[str, List[str]]:
    data: Dict[str, List[str]] = {}
    for e in entries:
        nurse_id = str(e.nurse_id)
        if nurse_id not in data:
            data[nurse_id] = ["-"] * days_in_month
        work_date = getattr(e, "work_date", None)
        if work_date is None:
            continue
        day_idx = work_date.day - 1
        if 0 <= day_idx < days_in_month:
            data[nurse_id][day_idx] = str(e.shift_id)
    return data


def _build_schedule_shift_pk_map(entries: List[Any], days_in_month: int) -> Dict[str, List[Optional[str]]]:
    data: Dict[str, List[Optional[str]]] = {}
    for e in entries:
        nurse_id = str(e.nurse_id)
        if nurse_id not in data:
            data[nurse_id] = [None] * days_in_month
        work_date = getattr(e, "work_date", None)
        if work_date is None:
            continue
        day_idx = work_date.day - 1
        if 0 <= day_idx < days_in_month:
            shift_pk = getattr(e, "id", None)
            data[nurse_id][day_idx] = None if shift_pk is None else str(shift_pk)
    return data


def _load_latest_request_ids(db: Session, nurse_ids: List[str], month_key: str) -> Dict[str, int]:
    request_ids: Dict[str, int] = {}
    for nurse_id in nurse_ids:
        wr = (
            db.query(WantedRequest)
            .filter(WantedRequest.nurse_id == nurse_id, WantedRequest.month == month_key)
            .order_by(WantedRequest.request_id.desc())
            .first()
        )
        if wr is not None:
            req_id = getattr(wr, "request_id", None)
            if req_id is None:
                continue
            request_ids[nurse_id] = int(req_id)
    return request_ids


def _load_preference_score_map(
    db: Session,
    request_ids: Dict[str, int],
    year: int,
    month: int,
) -> Dict[Tuple[str, int, str], float]:
    score_map: Dict[Tuple[str, int, str], float] = {}
    for nurse_id, req_id in request_ids.items():
        rows = (
            db.query(NurseShiftRequest)
            .filter(
                NurseShiftRequest.nurse_id == nurse_id,
                NurseShiftRequest.request_id == req_id,
            )
            .all()
        )
        for r in rows:
            dt = _to_date(r.shift_date)
            if dt is None or dt.year != year or dt.month != month:
                continue
            shift_code = _normalize_shift_code(str(getattr(r, "shift", "") or ""))
            score_raw = getattr(r, "score", 0.0)
            try:
                score = float(score_raw)
            except Exception:
                score = 0.0
            score_map[(nurse_id, dt.day, shift_code)] = score
    return score_map


def _load_pair_score_map(db: Session, nurse_ids: List[str], month_key: str) -> Dict[Tuple[str, str], float]:
    rows = (
        db.query(NursePairRequest)
        .filter(NursePairRequest.month == month_key, NursePairRequest.nurse_id.in_(nurse_ids))
        .all()
    )
    pair_score: Dict[Tuple[str, str], List[float]] = defaultdict(list)
    for r in rows:
        raw = getattr(r, "score", 0.0)
        try:
            val = float(raw)
        except Exception:
            val = 0.0
        pair_score[(str(r.nurse_id), str(r.target_id))].append(val)
    return {k: sum(v) / len(v) for k, v in pair_score.items() if v}


def _overlay_candidate_capability_asof(db: Any, nurses: List[Any], as_of: date) -> None:
    """대체 후보 nurse 들의 allowed_shifts/fixed_shift 를 as_of period 값으로 in-place 오버레이.

    구간 있으면 __dict__ 주입, gap 이면 캐시 유지(무회귀). 추천 흐름은 읽기전용이라
    세션 객체 in-place 주입이 안전(생성기 _overlay_home_profile_asof 와 동일 방식).
    """
    try:
        from services.nurse_period_resolver import fetch_periods, resolve_asof
        from db.models import NurseAllowedShiftPeriod
    except Exception:
        return
    ids = [str(n.nurse_id) for n in nurses]
    if not ids:
        return
    ap = fetch_periods(db, NurseAllowedShiftPeriod, ids, as_of, as_of + timedelta(days=1))
    _SENT = object()
    for n in nurses:
        rows = ap.get(str(n.nurse_id))
        if not rows:
            continue
        a = resolve_asof(rows, as_of, "allowed_shifts", _SENT)
        if a is not _SENT:
            n.__dict__["allowed_shifts"] = a
        f = resolve_asof(rows, as_of, "fixed_shift", _SENT)
        if f is not _SENT:
            n.__dict__["fixed_shift"] = f


def _nurse_is_night_capable(nurse: Any) -> bool:
    raw = getattr(nurse, "allowed_shifts", None)
    if isinstance(raw, list):
        return len(raw) > 0
    if raw is None:
        return False
    return bool(raw)


def _nurse_can_work_shift(nurse: Any, shift_code: str) -> bool:
    code = _normalize_shift_code(shift_code)
    if code == "N" and not _nurse_is_night_capable(nurse):
        return False
    work_shifts = getattr(nurse, "work_shifts", None)
    if not isinstance(work_shifts, list) or not work_shifts:
        return True
    normalized = {_normalize_shift_code(str(s)) for s in work_shifts}
    if code in normalized:
        return True
    if code == "N":
        return any(s.startswith("N") for s in normalized)
    if code == "E":
        return any(s.startswith("E") for s in normalized)
    if code == "D":
        return any(s.startswith("D") or s == "M" for s in normalized)
    return True


def _compute_streak(
    schedule: List[str],
    day_idx: int,
    shift_meta: Dict[str, Dict[str, Any]],
    schedule_shift_pks: Optional[List[Optional[str]]] = None,
    shift_meta_by_pk: Optional[Dict[str, Dict[str, Any]]] = None,
) -> Tuple[int, int]:
    prev_work = 0
    cur = day_idx - 1
    while cur >= 0:
        shift_pk = schedule_shift_pks[cur] if schedule_shift_pks and cur < len(schedule_shift_pks) else None
        _main, _is_off_rest, _is_vacation, is_off_any, _reason = _resolve_shift_semantic_with_reason(
            schedule[cur],
            shift_meta,
            shift_pk=shift_pk,
            shift_meta_by_pk=shift_meta_by_pk,
        )
        if is_off_any:
            break
        prev_work += 1
        cur -= 1
    next_work = 0
    cur = day_idx + 1
    while cur < len(schedule):
        shift_pk = schedule_shift_pks[cur] if schedule_shift_pks and cur < len(schedule_shift_pks) else None
        _main, _is_off_rest, _is_vacation, is_off_any, _reason = _resolve_shift_semantic_with_reason(
            schedule[cur],
            shift_meta,
            shift_pk=shift_pk,
            shift_meta_by_pk=shift_meta_by_pk,
        )
        if is_off_any:
            break
        next_work += 1
        cur += 1
    return prev_work, next_work


def _prev_tail_schedule_map(
    prev_tail_payload: Optional[Dict[str, Any]],
) -> Dict[str, List[str]]:
    result: Dict[str, List[str]] = {}
    if not prev_tail_payload:
        return result
    data = prev_tail_payload.get("data")
    if not isinstance(data, dict):
        return result
    tail_days = data.get("tail_days")
    nurses = data.get("nurses")
    if not isinstance(tail_days, list) or not isinstance(nurses, list):
        return result
    ordered_days: List[str] = [str(d) for d in tail_days]
    for nurse_row in nurses:
        if not isinstance(nurse_row, dict):
            continue
        nurse_id = str(nurse_row.get("nurse_id") or "")
        if not nurse_id:
            continue
        shifts = nurse_row.get("shifts")
        if not isinstance(shifts, dict):
            continue
        result[nurse_id] = [str(shifts.get(day_key) or "-") for day_key in ordered_days]
    return result


def _recovery_off_risk(
    schedule_ctx: List[str],
    local_day_idx: int,
    shift_meta: Dict[str, Dict[str, Any]],
    config: Optional[Any],
    schedule_ctx_shift_pks: Optional[List[Optional[str]]] = None,
    shift_meta_by_pk: Optional[Dict[str, Dict[str, Any]]] = None,
) -> Tuple[float, List[str]]:
    if local_day_idx < 0 or local_day_idx >= len(schedule_ctx):
        return 0.0, []
    current_shift = schedule_ctx[local_day_idx]
    current_shift_pk = (
        schedule_ctx_shift_pks[local_day_idx]
        if schedule_ctx_shift_pks and local_day_idx < len(schedule_ctx_shift_pks)
        else None
    )
    _main, _is_off_rest, _is_vacation, current_is_off_any, _reason = _resolve_shift_semantic_with_reason(
        current_shift,
        shift_meta,
        shift_pk=current_shift_pk,
        shift_meta_by_pk=shift_meta_by_pk,
    )
    if not current_is_off_any:
        return 0.0, []

    block_start = local_day_idx
    while block_start - 1 >= 0:
        prev_idx = block_start - 1
        prev_shift_pk = (
            schedule_ctx_shift_pks[prev_idx]
            if schedule_ctx_shift_pks and prev_idx < len(schedule_ctx_shift_pks)
            else None
        )
        _m, _r, _v, prev_is_off_any, _reason_prev = _resolve_shift_semantic_with_reason(
            schedule_ctx[prev_idx],
            shift_meta,
            shift_pk=prev_shift_pk,
            shift_meta_by_pk=shift_meta_by_pk,
        )
        if not prev_is_off_any:
            break
        block_start -= 1
    off_day_order = local_day_idx - block_start + 1
    if off_day_order not in {1, 2}:
        return 0.0, []

    night_streak = 0
    cursor = block_start - 1
    while cursor >= 0:
        cursor_shift_pk = (
            schedule_ctx_shift_pks[cursor] if schedule_ctx_shift_pks and cursor < len(schedule_ctx_shift_pks) else None
        )
        if (
            _main_shift_code_with_reason(
                schedule_ctx[cursor],
                shift_meta,
                shift_pk=cursor_shift_pk,
                shift_meta_by_pk=shift_meta_by_pk,
            )[0]
            != "N"
        ):
            break
        night_streak += 1
        cursor -= 1

    two_off_after_two = bool(getattr(config, "two_offs_after_two_nig", False)) if config else False
    two_off_after_three = bool(getattr(config, "two_offs_after_three_nig", False)) if config else False

    risk = 0.0
    tags: List[str] = []
    if two_off_after_two and night_streak >= 2:
        risk += 92.0
        tags.append(f"protected_off_2n2o_day{off_day_order}_risk")
    if two_off_after_three and night_streak >= 3:
        risk += 98.0
        tags.append(f"protected_off_3n2o_day{off_day_order}_risk")
    return risk, tags


def _rule_safety_score(
    nurse: Any,
    schedule: List[str],
    day_idx: int,
    target_shift_code: str,
    shift_meta: Dict[str, Dict[str, Any]],
    config: Optional[Any],
    prev_tail_schedule: Optional[List[str]] = None,
    schedule_shift_pks: Optional[List[Optional[str]]] = None,
    prev_tail_shift_pks: Optional[List[Optional[str]]] = None,
    target_shift_pk: Optional[str] = None,
    shift_meta_by_pk: Optional[Dict[str, Dict[str, Any]]] = None,
) -> Tuple[float, float, List[str]]:
    tags: List[str] = []
    risk = 0.0
    assigned_shift = schedule[day_idx] if day_idx < len(schedule) else "-"
    assigned_shift_pk = schedule_shift_pks[day_idx] if schedule_shift_pks and day_idx < len(schedule_shift_pks) else None
    _assigned_main, _assigned_is_off_rest, _assigned_is_vacation, assigned_is_off, _assigned_reason = (
        _resolve_shift_semantic_with_reason(
            assigned_shift,
            shift_meta,
            shift_pk=assigned_shift_pk,
            shift_meta_by_pk=shift_meta_by_pk,
        )
    )
    if assigned_is_off:
        tags.append("off_today")

    prev_tail_ctx = list(prev_tail_schedule or [])
    prev_tail_len = len(prev_tail_ctx)
    schedule_ctx = prev_tail_ctx + schedule

    if prev_tail_shift_pks is None:
        normalized_prev_tail_shift_pks: List[Optional[str]] = [None] * prev_tail_len
    else:
        normalized_prev_tail_shift_pks = list(prev_tail_shift_pks[:prev_tail_len])
        if len(normalized_prev_tail_shift_pks) < prev_tail_len:
            normalized_prev_tail_shift_pks.extend([None] * (prev_tail_len - len(normalized_prev_tail_shift_pks)))

    schedule_ctx_shift_pks = normalized_prev_tail_shift_pks + list(schedule_shift_pks or [])
    if len(schedule_ctx_shift_pks) < len(schedule_ctx):
        schedule_ctx_shift_pks.extend([None] * (len(schedule_ctx) - len(schedule_ctx_shift_pks)))
    elif len(schedule_ctx_shift_pks) > len(schedule_ctx):
        schedule_ctx_shift_pks = schedule_ctx_shift_pks[: len(schedule_ctx)]

    local_day_idx = day_idx + prev_tail_len
    prev_shift = schedule_ctx[local_day_idx - 1] if local_day_idx - 1 >= 0 else "-"
    prev_shift_pk = (
        schedule_ctx_shift_pks[local_day_idx - 1]
        if local_day_idx - 1 >= 0 and local_day_idx - 1 < len(schedule_ctx_shift_pks)
        else None
    )
    prev_main = _main_shift_code_with_reason(
        prev_shift,
        shift_meta,
        shift_pk=prev_shift_pk,
        shift_meta_by_pk=shift_meta_by_pk,
    )[0]
    target_main = _main_shift_code_with_reason(
        target_shift_code,
        shift_meta,
        shift_pk=target_shift_pk,
        shift_meta_by_pk=shift_meta_by_pk,
    )[0]
    next_main = "-"
    if local_day_idx + 1 < len(schedule_ctx):
        next_shift = schedule_ctx[local_day_idx + 1]
        next_shift_pk = (
            schedule_ctx_shift_pks[local_day_idx + 1]
            if local_day_idx + 1 < len(schedule_ctx_shift_pks)
            else None
        )
        next_main = _main_shift_code_with_reason(
            next_shift,
            shift_meta,
            shift_pk=next_shift_pk,
            shift_meta_by_pk=shift_meta_by_pk,
        )[0]

    max_conseq = int(getattr(config, "max_conseq_work", 6) or 6) if config else 6
    monthly_night_limit = int(getattr(config, "max_nig_per_month", 8) or 8) if config else 8
    banned_day_after_eve = bool(getattr(config, "banned_day_after_eve", False)) if config else False

    prev_work, next_work = _compute_streak(
        schedule_ctx,
        local_day_idx,
        shift_meta,
        schedule_shift_pks=schedule_ctx_shift_pks,
        shift_meta_by_pk=shift_meta_by_pk,
    )
    projected_work = prev_work + 1 + next_work
    if target_main != "O" and projected_work > max_conseq:
        over = projected_work - max_conseq
        risk += 45.0 + 12.0 * over
        tags.append("consecutive_work_risk")

    if prev_main == "N" and target_main in {"D", "E"}:
        risk += 60.0
        tags.append("night_to_day_evening_risk")

    if banned_day_after_eve and prev_main == "E" and target_main == "D":
        risk += 55.0
        tags.append("eve_to_day_risk")

    if banned_day_after_eve and target_main == "E" and next_main == "D":
        risk += 55.0
        tags.append("lookahead_eve_to_day_risk")

    if target_main == "N" and next_main == "D":
        risk += 60.0
        tags.append("lookahead_night_to_day_risk")

    if target_main == "N" and next_main == "E":
        risk += 60.0
        tags.append("lookahead_night_to_evening_risk")

    if target_main == "N":
        month_nights = 0
        for idx, shift_id in enumerate(schedule):
            shift_pk = schedule_shift_pks[idx] if schedule_shift_pks and idx < len(schedule_shift_pks) else None
            code = _main_shift_code_with_reason(
                shift_id,
                shift_meta,
                shift_pk=shift_pk,
                shift_meta_by_pk=shift_meta_by_pk,
            )[0]
            if code == "N" and idx != day_idx:
                month_nights += 1
        projected_nights = month_nights + 1
        if projected_nights > monthly_night_limit:
            over = projected_nights - monthly_night_limit
            risk += 40.0 + 10.0 * over
            tags.append("monthly_night_limit_risk")
        # 15N 절대 상한: 월 15회 초과 시 사실상 배제
        if projected_nights > 15:
            risk += 100.0
            tags.append("night_15n_hard_limit")

    # OFF 일수 보장: 대체 투입 시 해당 간호사의 월간 OFF가 기준 이하로 떨어지는지 체크
    required_off = int(getattr(config, "off_days", 8) or 8) if config else 8
    if assigned_is_off:
        current_off_count = 0
        for idx, shift_id in enumerate(schedule):
            shift_pk = schedule_shift_pks[idx] if schedule_shift_pks and idx < len(schedule_shift_pks) else None
            _, _, _, is_off_any, _ = _resolve_shift_semantic_with_reason(
                shift_id, shift_meta, shift_pk=shift_pk, shift_meta_by_pk=shift_meta_by_pk,
            )
            if is_off_any:
                current_off_count += 1
        projected_off = current_off_count - 1  # OFF 하나를 근무로 전환
        if projected_off < required_off:
            shortfall = required_off - projected_off
            risk += 35.0 + 15.0 * shortfall
            tags.append(f"off_day_shortage_{projected_off}of{required_off}")

    if not assigned_is_off:
        risk += 30.0
        tags.append("requires_reassignment")

    recovery_risk, recovery_tags = _recovery_off_risk(
        schedule_ctx=schedule_ctx,
        local_day_idx=local_day_idx,
        shift_meta=shift_meta,
        config=config,
        schedule_ctx_shift_pks=schedule_ctx_shift_pks,
        shift_meta_by_pk=shift_meta_by_pk,
    )
    if recovery_risk > 0.0:
        risk += recovery_risk
        tags.extend(recovery_tags)

    risk_clamped = min(100.0, max(0.0, risk))
    safety = max(0.0, 1.0 - (risk_clamped / 100.0))
    estimated_delta = round(risk_clamped / 35.0, 3)
    return safety, estimated_delta, tags


def _fairness_scores(
    candidates: List[Tuple[str, CandidateContext]],
    schedules: Dict[str, List[str]],
    schedule_shift_pk_map: Dict[str, List[Optional[str]]],
    shift_code: str,
    shift_meta: Dict[str, Dict[str, Any]],
    shift_meta_by_pk: Optional[Dict[str, Dict[str, Any]]] = None,
) -> Dict[str, float]:
    loads: Dict[str, int] = {}
    target_main = _main_shift_code(shift_code, shift_meta)
    for nurse_id, _ctx in candidates:
        schedule = schedules.get(nurse_id, [])
        shift_pk_row = schedule_shift_pk_map.get(nurse_id, [])
        loads[nurse_id] = sum(
            1
            for idx, s in enumerate(schedule)
            if _main_shift_code_with_reason(
                s,
                shift_meta,
                shift_pk=(shift_pk_row[idx] if idx < len(shift_pk_row) else None),
                shift_meta_by_pk=shift_meta_by_pk,
            )[0]
            == target_main
        )

    if not loads:
        return {}
    lo = min(loads.values())
    hi = max(loads.values())
    if hi == lo:
        return {k: 0.5 for k in loads}
    return {k: 1.0 - ((v - lo) / (hi - lo)) for k, v in loads.items()}


def _build_slots_for_bulk(
    req: ReplacementRecommendRequest,
    target_schedule: List[str],
    shift_meta: Dict[str, Dict[str, Any]],
    target_schedule_shift_pks: Optional[List[Optional[str]]] = None,
    shift_meta_by_pk: Optional[Dict[str, Dict[str, Any]]] = None,
) -> List[ReplacementSlot]:
    assert req.absence_window is not None
    slots: List[ReplacementSlot] = []
    for day_idx, shift_id in enumerate(target_schedule):
        dt = date(req.absence_window.start_date.year, req.absence_window.start_date.month, day_idx + 1)
        if dt < req.absence_window.start_date or dt > req.absence_window.end_date:
            continue
        shift_pk = target_schedule_shift_pks[day_idx] if target_schedule_shift_pks and day_idx < len(target_schedule_shift_pks) else None
        _main, _is_off_rest, _is_vacation, is_off_any, _reason = _resolve_shift_semantic_with_reason(
            shift_id,
            shift_meta,
            shift_pk=shift_pk,
            shift_meta_by_pk=shift_meta_by_pk,
        )
        if is_off_any:
            continue
        slots.append(ReplacementSlot(date=dt, shift=str(shift_id)))
    return slots


def _build_coverage_req(config: Any) -> Dict[str, int]:
    """config에서 시프트 타입별 최소 커버리지 요구치를 구성.

    솔버와 동일 로직: daily_shift_requirements JSON 우선, 없으면 구 필드 폴백.
    use_mid=True 시 M 키 포함.
    """
    if not config:
        return {}
    raw = getattr(config, "daily_shift_requirements", None)
    if raw and isinstance(raw, dict):
        req = {}
        for k, v in raw.items():
            key = str(k).strip().upper()
            try:
                req[key] = int(v or 0)
            except Exception:
                req[key] = 0
        return req
    req = {
        "D": int(getattr(config, "day_req", 0) or 0),
        "E": int(getattr(config, "eve_req", 0) or 0),
        "N": int(getattr(config, "nig_req", 0) or 0),
    }
    if bool(getattr(config, "use_mid", False)):
        req["M"] = int(getattr(config, "mid_req", 0) or 0)
    return req


# ── Split Coverage 분할 전략 ──────────────────────────────


def _split_slots_even(
    slots: List[ReplacementSlot],
    split_count: Optional[int] = None,
) -> List[Tuple[str, List[ReplacementSlot]]]:
    """균등 분할: N개 구간으로 슬롯을 균등 배분."""
    if not slots:
        return []
    # 미지정 시 구간당 최대 4일 기준 자동 계산
    n = split_count or max(2, -(-len(slots) // 4))
    n = min(n, len(slots))
    base, remainder = divmod(len(slots), n)
    segments: List[Tuple[str, List[ReplacementSlot]]] = []
    offset = 0
    for i in range(n):
        size = base + (1 if i < remainder else 0)
        seg = slots[offset : offset + size]
        label = f"구간 {i + 1} ({seg[0].date}~{seg[-1].date})"
        segments.append((label, seg))
        offset += size
    return segments


def _split_slots_ngram_optimal(
    slots: List[ReplacementSlot],
    max_window: int = 4,
    evaluate_fn=None,
    min_window: int = 1,
) -> List[Tuple[str, List[ReplacementSlot]]]:
    """n-gram 슬라이딩 윈도우 최적 분할 (DP).

    가능한 모든 윈도우 크기(1~max_window) 조합 중
    최소 위반 + 최고 점수인 파티션을 DP로 탐색.
    evaluate_fn: (seg_slots) -> (violations, score) 평가 함수.
    """
    if not slots:
        return []
    n = len(slots)
    if n <= max_window:
        label = f"구간 1 ({slots[0].date}~{slots[-1].date})"
        return [(label, slots)]

    INF_V = 999999
    # dp[i] = (total_violations, total_score, partition_indices)
    dp: List[Tuple[int, float, List[Tuple[int, int]]]] = [
        (INF_V, -1.0, []) for _ in range(n + 1)
    ]
    dp[0] = (0, 0.0, [])

    # 위반 최소화 우선. 분할 비용 없음.
    segment_penalty = 0

    for i in range(1, n + 1):
        _lo = max(1, min_window)
        _hi = min(max_window, i)
        # 전체 슬롯 수가 min_window 미만이면 어쩔 수 없이 1-window 허용
        if i < min_window and n < min_window:
            _lo = 1
        for w in range(_lo, _hi + 1):
            j = i - w
            if dp[j][0] == INF_V and j > 0:
                continue
            seg = slots[j:i]
            if evaluate_fn:
                v_count, score = evaluate_fn(seg)
            else:
                v_count, score = 0, float(w)
            # 첫 세그먼트(j==0)는 패널티 없음, 이후 세그먼트마다 패널티 추가
            split_cost = segment_penalty if j > 0 else 0
            # 1-day 구간은 작은 패널티(soft): 2일 이상 묶음을 선호
            small_seg_penalty = 1 if (w == 1 and min_window >= 2) else 0
            total_v = dp[j][0] + v_count + split_cost + small_seg_penalty
            total_s = dp[j][1] + score
            if total_v < dp[i][0] or (total_v == dp[i][0] and total_s > dp[i][1]):
                dp[i] = (total_v, total_s, dp[j][2] + [(j, i)])

    segments: List[Tuple[str, List[ReplacementSlot]]] = []
    for idx, (start, end) in enumerate(dp[n][2]):
        seg = slots[start:end]
        label = f"구간 {idx + 1} ({seg[0].date}~{seg[-1].date})"
        segments.append((label, seg))
    return segments


def _split_slots_by_shift_type(
    slots: List[ReplacementSlot],
    shift_meta: Dict[str, Dict[str, Any]],
    shift_meta_by_pk: Optional[Dict[str, Dict[str, Any]]] = None,
    target_schedule_shift_pks: Optional[List[Optional[str]]] = None,
    max_segment_days: int = 3,
) -> List[Tuple[str, List[ReplacementSlot]]]:
    """근무 유형 경계 분할: D/E/M vs N 연속 구간별 분리.

    동일 유형 연속이라도 max_segment_days 초과 시 강제 분할하여
    1명이 과도한 연속 근무로 몰리는 것을 방지.
    """
    if not slots:
        return []

    def _classify(slot: ReplacementSlot) -> str:
        pk = None
        if target_schedule_shift_pks:
            idx = slot.date.day - 1
            if idx < len(target_schedule_shift_pks):
                pk = target_schedule_shift_pks[idx]
        main, *_ = _resolve_shift_semantic_with_reason(
            slot.shift, shift_meta, shift_pk=pk, shift_meta_by_pk=shift_meta_by_pk,
        )
        return "N" if main == "N" else "D/E"

    segments: List[Tuple[str, List[ReplacementSlot]]] = []
    current_type = _classify(slots[0])
    current_group: List[ReplacementSlot] = [slots[0]]
    for slot in slots[1:]:
        st = _classify(slot)
        date_gap = (slot.date - current_group[-1].date).days > 1
        over_cap = len(current_group) >= max_segment_days
        if st != current_type or date_gap or over_cap:
            label = f"{current_type} ({current_group[0].date}~{current_group[-1].date})"
            segments.append((label, current_group))
            current_type = st
            current_group = [slot]
        else:
            current_group.append(slot)
    label = f"{current_type} ({current_group[0].date}~{current_group[-1].date})"
    segments.append((label, current_group))
    return segments


def _split_slots_by_weekday_weekend(
    slots: List[ReplacementSlot],
) -> List[Tuple[str, List[ReplacementSlot]]]:
    """주중/주말 분할: 연속 주중(Mon-Fri)/주말(Sat-Sun) 구간별 분리."""
    if not slots:
        return []

    def _is_weekend(d: date) -> bool:
        return d.weekday() >= 5

    segments: List[Tuple[str, List[ReplacementSlot]]] = []
    current_is_wknd = _is_weekend(slots[0].date)
    current_group: List[ReplacementSlot] = [slots[0]]
    for slot in slots[1:]:
        wknd = _is_weekend(slot.date)
        if wknd != current_is_wknd:
            tag = "주말" if current_is_wknd else "주중"
            label = f"{tag} ({current_group[0].date}~{current_group[-1].date})"
            segments.append((label, current_group))
            current_is_wknd = wknd
            current_group = [slot]
        else:
            current_group.append(slot)
    tag = "주말" if current_is_wknd else "주중"
    label = f"{tag} ({current_group[0].date}~{current_group[-1].date})"
    segments.append((label, current_group))
    return segments


def _split_slots(
    slots: List[ReplacementSlot],
    strategy: str,
    shift_meta: Dict[str, Dict[str, Any]],
    shift_meta_by_pk: Optional[Dict[str, Dict[str, Any]]] = None,
    target_schedule_shift_pks: Optional[List[Optional[str]]] = None,
    split_count: Optional[int] = None,
) -> List[Tuple[str, List[ReplacementSlot]]]:
    """전략에 따라 슬롯을 구간별로 분할."""
    if strategy == "EVEN":
        return _split_slots_even(slots, split_count)
    elif strategy == "SHIFT_TYPE":
        return _split_slots_by_shift_type(
            slots, shift_meta, shift_meta_by_pk, target_schedule_shift_pks,
        )
    elif strategy == "WEEKDAY_WEEKEND":
        return _split_slots_by_weekday_weekend(slots)
    return [("전체", slots)]


def _evaluate_segment_single_nurse(
    seg_slots: List[ReplacementSlot],
    req: ReplacementRecommendRequest,
    schedule_map: Dict[str, List[str]],
    schedule_shift_pk_map: Dict[str, List[Optional[str]]],
    nurse_by_id: Dict[str, Any],
    shift_meta: Dict[str, Dict[str, Any]],
    shift_meta_by_pk: Dict[str, Dict[str, Any]],
    config: Any,
    prev_tail_by_nurse: Dict[str, List[str]],
    pref_score_map: Dict[Tuple[str, int, str], float],
    pair_score_map: Dict[Tuple[str, str], float],
    target_grade: Optional[int],
    ranking_scope: str,
    max_scan: int,
    used_nurse_ids: set,
    assignment_blocked: Optional[Dict[str, set]] = None,
    allow_capability_fallback: bool = False,
) -> Tuple[Optional[CandidateRecommendation], float]:
    """구간 전체를 1명이 커버할 최적 간호사를 평가.

    각 후보에 대해 구간 내 모든 슬롯의 점수를 합산하고,
    이전 구간에서 선점된 간호사(used_nurse_ids)는 제외.
    """
    if not seg_slots:
        return None, 0.0

    first_slot = seg_slots[0]
    scored = _evaluate_single_slot(
        slot=first_slot, req=req,
        schedule_map=schedule_map,
        schedule_shift_pk_map=schedule_shift_pk_map,
        nurse_by_id=nurse_by_id, shift_meta=shift_meta,
        shift_meta_by_pk=shift_meta_by_pk, config=config,
        prev_tail_by_nurse=prev_tail_by_nurse,
        pref_score_map=pref_score_map, pair_score_map=pair_score_map,
        target_grade=target_grade, ranking_scope=ranking_scope,
        max_scan=max_scan,
        assignment_blocked=assignment_blocked,
        allow_capability_fallback=allow_capability_fallback,
    )

    best_cid: Optional[str] = None
    best_total = -1.0
    best_violations = 999999
    best_ctx: Optional[CandidateContext] = None
    target_pk_row = schedule_shift_pk_map.get(req.target_nurse_id, [])

    eval_limit = min(max_scan, len(scored))
    _max_conseq = int(getattr(config, "max_conseq_work", 5) or 5) if config else 5
    for cid, ctx, base_score in scored[:eval_limit]:
        if cid == req.target_nurse_id or cid in used_nurse_ids:
            continue
        nurse_sched = schedule_map.get(cid, [])
        nurse_pk_row = schedule_shift_pk_map.get(cid, [])
        total = 0.0
        feasible = True
        _seg_blocked = (assignment_blocked or {}).get(cid, set())

        # 사전 필터: seg_slots 전체 덮었을 때 이 후보의 연속근무가 max_conseq 초과면 컷
        if nurse_sched:
            _seg_days = {s.date.day - 1 for s in seg_slots}
            _sim_row = list(nurse_sched)
            for _d in _seg_days:
                if _d < len(_sim_row):
                    _sim_row[_d] = "X"  # 임시 근무 표식 (비-OFF)
            _max_streak = 0
            _cur = 0
            for _i, _sh in enumerate(_sim_row):
                _pk = nurse_pk_row[_i] if _i < len(nurse_pk_row) else None
                if _sh == "X":
                    _is_off_here = False
                else:
                    _, _, _, _is_off_here, _ = _resolve_shift_semantic_with_reason(
                        _sh, shift_meta, shift_pk=_pk, shift_meta_by_pk=shift_meta_by_pk,
                    )
                if _is_off_here:
                    _max_streak = max(_max_streak, _cur)
                    _cur = 0
                else:
                    _cur += 1
            _max_streak = max(_max_streak, _cur)
            if _max_streak > _max_conseq:
                continue

        for slot in seg_slots:
            day_idx = slot.date.day - 1
            if day_idx >= len(nurse_sched):
                feasible = False
                break
            if slot.date in _seg_blocked:
                feasible = False
                break
            cur_shift = nurse_sched[day_idx]
            cur_pk = nurse_pk_row[day_idx] if day_idx < len(nurse_pk_row) else None
            _main, _is_off_rest, _is_vac, is_off, _reason = _resolve_shift_semantic_with_reason(
                cur_shift, shift_meta, shift_pk=cur_pk, shift_meta_by_pk=shift_meta_by_pk,
            )
            # ranking_scope 기반 필터 (구간 내 모든 날에 대해 일관되게 적용)
            if not _is_candidate_allowed_by_scope(
                ranking_scope, _is_off_rest, _is_vac,
            ):
                feasible = False
                break
            total += base_score * (1.0 if is_off else 0.5)
        if not feasible:
            continue
        # 위반 시뮬레이션: 해당 간호사에 구간 전체 배정 후 위반 수 계산
        trial_candidate = _ctx_to_candidate_model(ctx, rank=1, include_explanations=False)
        trial_steps = [
            BulkPathStep(slot=s, candidate=trial_candidate, transition_score=0.0)
            for s in seg_slots
        ]
        v_summary = _count_path_violations(
            path_steps=trial_steps,
            schedule_map=schedule_map,
            schedule_shift_pk_map=schedule_shift_pk_map,
            nurse_by_id=nurse_by_id,
            shift_meta=shift_meta,
            shift_meta_by_pk=shift_meta_by_pk,
            config=config,
            prev_tail_by_nurse=prev_tail_by_nurse,
            target_nurse_id=req.target_nurse_id,
            include_target_nurse=False,
        )
        v_count = v_summary.total_count if v_summary else 0
        # 커버리지 검증: 후보가 빠지면 원래 시프트 최소인원 위반 여부
        _cov_req = _build_coverage_req(config)
        for slot in seg_slots:
            day_idx = slot.date.day - 1
            if day_idx >= len(nurse_sched):
                continue
            cur_pk = nurse_pk_row[day_idx] if day_idx < len(nurse_pk_row) else None
            _m, _, _, is_off, _ = _resolve_shift_semantic_with_reason(
                nurse_sched[day_idx], shift_meta,
                shift_pk=cur_pk, shift_meta_by_pk=shift_meta_by_pk,
            )
            if is_off or _m not in _cov_req or _cov_req[_m] <= 0:
                continue
            # 해당 일 해당 시프트 타입 인원 수 계산
            day_count = 0
            for other_id, other_sched in schedule_map.items():
                if other_id == req.target_nurse_id:
                    continue
                if day_idx >= len(other_sched):
                    continue
                o_pk_row = schedule_shift_pk_map.get(other_id, [])
                o_pk = o_pk_row[day_idx] if day_idx < len(o_pk_row) else None
                o_main, _, _, o_off, _ = _resolve_shift_semantic_with_reason(
                    other_sched[day_idx], shift_meta,
                    shift_pk=o_pk, shift_meta_by_pk=shift_meta_by_pk,
                )
                if not o_off and o_main == _m:
                    day_count += 1
            # 후보가 빠지면 day_count - 1
            if (day_count - 1) < _cov_req[_m]:
                v_count += 1
        # 최소 위반 우선, 동일 위반 시 점수 우선
        if v_count < best_violations or (v_count == best_violations and total > best_total):
            best_violations = v_count
            best_total = total
            best_cid = cid
            best_ctx = ctx

    if best_cid is None or best_ctx is None:
        return None, 0.0

    candidate = _ctx_to_candidate_model(
        best_ctx, rank=1,
        include_explanations=req.options.include_explanations,
    )
    # 구간 내 각 슬롯의 기존 시프트 정보를 태그에 추가
    nurse_sched = schedule_map.get(best_cid, [])
    nurse_pk_row = schedule_shift_pk_map.get(best_cid, [])
    off_days = 0
    for slot in seg_slots:
        day_idx = slot.date.day - 1
        if day_idx < len(nurse_sched):
            cur_shift = nurse_sched[day_idx]
            cur_pk = nurse_pk_row[day_idx] if day_idx < len(nurse_pk_row) else None
            _m, _r, _v, is_off, _ = _resolve_shift_semantic_with_reason(
                cur_shift, shift_meta, shift_pk=cur_pk, shift_meta_by_pk=shift_meta_by_pk,
            )
            if is_off:
                off_days += 1
    seg_len = len(seg_slots)
    if off_days == seg_len:
        candidate.tags.append(f"all_off_{seg_len}d")
    elif off_days > 0:
        candidate.tags.append(f"off_{off_days}_of_{seg_len}d")
    else:
        candidate.tags.append(f"requires_full_reassignment_{seg_len}d")

    return candidate, best_total


def _evaluate_segment_with_fallback(
    seg_slots: List[ReplacementSlot],
    used_nurse_ids: set,
    eval_kwargs: Dict[str, Any],
) -> Tuple[Optional[CandidateRecommendation], float, int]:
    """구간 후보를 Level 0→1→2로 단계 완화하며 평가.

    Level 0: 정상 평가 (regular pool only).
    Level 1: shift_capability_fallback 풀 병합 (cap 미충족 후보 허용 + 페널티).
    Level 2: used_nurse_ids 격리 해제 (다른 path/segment에서 쓰인 후보 허용 + 페널티).

    하드 제약(연속근무·NDED·1N·max_conseq·assignment_blocked·ranking_scope·휴직 등)은
    어떤 단계에서도 완화되지 않는다.
    """
    cand0, score0 = _evaluate_segment_single_nurse(
        seg_slots=seg_slots, used_nurse_ids=used_nurse_ids,
        allow_capability_fallback=False, **eval_kwargs,
    )
    if cand0 is not None:
        return cand0, score0, 0

    cand1, score1 = _evaluate_segment_single_nurse(
        seg_slots=seg_slots, used_nurse_ids=used_nurse_ids,
        allow_capability_fallback=True, **eval_kwargs,
    )
    if cand1 is not None:
        return cand1, score1, 1

    cand2, score2 = _evaluate_segment_single_nurse(
        seg_slots=seg_slots, used_nurse_ids=set(),
        allow_capability_fallback=True, **eval_kwargs,
    )
    if cand2 is not None:
        if "relaxed_path_isolation" not in cand2.tags:
            cand2.tags.append("relaxed_path_isolation")
        # Level 2 추가 페널티: 정규 점수 대비 큰 폭으로 감쇄 (정렬에서 항상 후순위 보장)
        score2 = score2 - 200.0 * max(1, len(seg_slots))
        return cand2, score2, 2

    return None, 0.0, 0


def _build_split_path(
    segments_raw: List[Tuple[str, List[ReplacementSlot]]],
    path_rank: int,
    scenario_label: str,
    req: ReplacementRecommendRequest,
    schedule_map: Dict[str, List[str]],
    schedule_shift_pk_map: Dict[str, List[Optional[str]]],
    nurse_by_id: Dict[str, Any],
    shift_meta: Dict[str, Dict[str, Any]],
    shift_meta_by_pk: Dict[str, Dict[str, Any]],
    config: Any,
    prev_tail_by_nurse: Dict[str, List[str]],
    pref_score_map: Dict[Tuple[str, int, str], float],
    pair_score_map: Dict[Tuple[str, str], float],
    target_grade: Optional[int],
    ranking_scope: str,
    max_scan: int,
    assignment_blocked: Optional[Dict[str, set]] = None,
) -> Optional[BulkPathRecommendation]:
    """구간 리스트를 받아 구간별 1명 평가 후 BulkPathRecommendation 반환."""
    if not segments_raw:
        return None
    all_steps: List[BulkPathStep] = []
    total_score = 0.0
    used_nurse_ids: set = set()
    path_max_relax_level = 0
    eval_kwargs = dict(
        req=req,
        schedule_map=schedule_map,
        schedule_shift_pk_map=schedule_shift_pk_map,
        nurse_by_id=nurse_by_id,
        shift_meta=shift_meta,
        shift_meta_by_pk=shift_meta_by_pk,
        config=config,
        prev_tail_by_nurse=prev_tail_by_nurse,
        pref_score_map=pref_score_map,
        pair_score_map=pair_score_map,
        target_grade=target_grade,
        ranking_scope=ranking_scope,
        max_scan=max_scan,
        assignment_blocked=assignment_blocked,
    )
    for _seg_label, seg_slots in segments_raw:
        best_candidate, best_score, relax_level = _evaluate_segment_with_fallback(
            seg_slots=seg_slots,
            used_nurse_ids=used_nurse_ids,
            eval_kwargs=eval_kwargs,
        )
        if best_candidate:
            # Level 2(used 해제)에서는 used 집합에 추가하지 않음 — 다음 segment에서도 같은 후보 재사용 가능하도록.
            if relax_level < 2:
                used_nurse_ids.add(best_candidate.nurse_id)
            path_max_relax_level = max(path_max_relax_level, relax_level)
            _cid = best_candidate.nurse_id
            _c_sched = schedule_map.get(_cid, [])
            _c_pk_row = schedule_shift_pk_map.get(_cid, [])
            for s in seg_slots:
                _di = s.date.day - 1
                all_steps.append(BulkPathStep(
                    slot=s,
                    candidate=best_candidate,
                    original_shift_id=_c_sched[_di] if _di < len(_c_sched) else None,
                    original_shift_pk=str(_c_pk_row[_di]) if _di < len(_c_pk_row) and _c_pk_row[_di] is not None else None,
                    transition_score=round(best_score / len(seg_slots), 3),
                ))
            total_score += best_score
        else:
            no_cand = CandidateRecommendation(
                nurse_id="", name="(후보 없음)", final_score=0.0, rank=0,
                tags=["no_candidate_available"],
                breakdown=CandidateScoreBreakdown(
                    rule_safety=0, off_priority=0, grade_fit=0,
                    preference=0, pair=0, fairness=0,
                    change_cost=0, estimated_violation_delta=0,
                ),
                no_candidate=True,
            )
            for s in seg_slots:
                all_steps.append(BulkPathStep(slot=s, candidate=no_cand))

    violations = _count_path_violations(
        path_steps=all_steps,
        schedule_map=schedule_map,
        schedule_shift_pk_map=schedule_shift_pk_map,
        nurse_by_id=nurse_by_id,
        shift_meta=shift_meta,
        shift_meta_by_pk=shift_meta_by_pk,
        config=config,
        prev_tail_by_nurse=prev_tail_by_nurse,
        target_nurse_id=req.target_nurse_id,
        include_target_nurse=False,
    )
    if path_max_relax_level > 0:
        logger.debug(
            "BULK segment path fallback applied: scenario=%s rank=%s max_relax_level=%s",
            scenario_label, path_rank, path_max_relax_level,
        )
    return BulkPathRecommendation(
        path_rank=path_rank,
        scenario_label=scenario_label,
        steps=all_steps,
        total_path_score=round(total_score, 3),
        violations=violations,
    )


def _final_score(ctx: CandidateContext) -> float:
    return round(
        100.0 * ctx.rule_safety
        + 20.0 * ctx.off_priority
        + 15.0 * ctx.grade_fit
        + 10.0 * ctx.preference
        + 8.0 * ctx.pair
        + 7.0 * ctx.fairness
        - 10.0 * ctx.change_cost
        - ctx.vacation_penalty,
        3,
    )


def _load_schedule_for_caller(db: Session, current_user, schedule_id: str) -> Schedule:
    """schedule_id(PK)로 스케줄을 로드하고 호출자의 그룹 접근 권한을 검증한다.

    그룹 스코프의 진실은 '스케줄 행의 group_id'다(schedule_id 가 그룹을 이미 확정).
    호출자-resolve group 을 쿼리 필터로 쓰면 HN 의 비-home 관리병동 스케줄이 home 으로
    해석돼 "not found" 로 숨는다(긴급대체 "스케줄 없음" 버그) → 스코프는 행에서 가져오고
    권한은 assert_caller_can_access_group 으로 별도 검증한다. roster.py 의 동명 SSOT
    헬퍼와 같은 패턴(서비스→라우터 역의존 회피 위해 별도 구현).
    """
    schedule = (
        db.query(Schedule)
        .filter(
            Schedule.schedule_id == schedule_id,
            Schedule.dropped == False,
        )
        .first()
    )
    if not schedule:
        raise ValueError("schedule not found")
    assert_caller_can_access_group(db, current_user, schedule.group_id)
    return schedule


def recommend_replacement_candidates(
    req: ReplacementRecommendRequest,
    current_user,
    db: Session,
    requested_group_id: Optional[str] = None,
) -> ReplacementRecommendResponse:
    # 그룹은 스케줄 행에서, 권한은 별도 검증(비-home 관리병동 스케줄 숨김 방지).
    # requested_group_id 는 더 이상 그룹 스코프 결정에 쓰지 않는다(행의 group_id 가 진실).
    schedule = _load_schedule_for_caller(db, current_user, req.schedule_id)
    target_group_id = str(schedule.group_id)

    config = None
    config_id = getattr(schedule, "config_id", None)
    if config_id is not None:
        config = db.query(RosterConfig).filter(RosterConfig.config_id == config_id).first()

    shifts = db.query(Shift).filter(Shift.group_id == target_group_id).all()
    shift_meta = {
        str(s.shift_id): {
            "type": str(getattr(s, "type", "") or ""),
            "default_shift": str(getattr(s, "default_shift", "") or ""),
            "shift_gb": str(getattr(s, "shift_gb", "") or ""),
        }
        for s in shifts
    }
    shift_meta_by_pk = {
        str(getattr(s, "id")): {
            "type": str(getattr(s, "type", "") or ""),
            "default_shift": str(getattr(s, "default_shift", "") or ""),
            "shift_gb": str(getattr(s, "shift_gb", "") or ""),
        }
        for s in shifts
        if getattr(s, "id", None) is not None
    }

    entries = db.query(ScheduleEntry).filter(ScheduleEntry.schedule_id == schedule.schedule_id).all()
    days_in_month = 31
    year_value = int(getattr(schedule, "year"))
    month_value = int(getattr(schedule, "month"))
    if month_value in {1, 3, 5, 7, 8, 10, 12}:
            days_in_month = 31
    elif month_value in {4, 6, 9, 11}:
        days_in_month = 30
    else:
        leap = year_value % 4 == 0 and (year_value % 100 != 0 or year_value % 400 == 0)
        days_in_month = 29 if leap else 28

    schedule_map = _build_schedule_map(entries, days_in_month)
    schedule_shift_pk_map = _build_schedule_shift_pk_map(entries, days_in_month)
    if req.target_nurse_id not in schedule_map:
        raise ValueError("target_nurse_id not found in schedule entries")

    nurses = db.query(Nurse).filter(Nurse.group_id == target_group_id).order_by(Nurse.nurse_id).all()
    # (P5) 대체 후보 capability(allowed_shifts/fixed_shift)를 대상 스케줄 월 as-of 로 오버레이.
    #   캐시(오늘값) 대신 그 달 period 값으로 야간가능·고정근무를 판정 — 생성기와 동일 SSOT.
    #   구간 없으면 캐시 유지(무회귀). 생성기 _overlay_home_profile_asof 와 동일하게 __dict__ 주입.
    _overlay_candidate_capability_asof(
        db, nurses, date(year_value, month_value, days_in_month)
    )
    nurse_by_id = {str(n.nurse_id): n for n in nurses}

    assignment_blocked = _build_assignment_blocked_dates(
        db, target_group_id, year_value, month_value, days_in_month,
    )

    if req.mode == "SINGLE":
        slots = req.slots or []
    else:
        slots = _build_slots_for_bulk(
            req,
            schedule_map[req.target_nurse_id],
            shift_meta,
            target_schedule_shift_pks=schedule_shift_pk_map.get(req.target_nurse_id),
            shift_meta_by_pk=shift_meta_by_pk,
        )

    month_key = f"{year_value:04d}-{month_value:02d}"
    nurse_ids = list(schedule_map.keys())
    request_ids = _load_latest_request_ids(db, nurse_ids, month_key)
    pref_score_map = _load_preference_score_map(db, request_ids, year_value, month_value)
    pair_score_map = _load_pair_score_map(db, nurse_ids, month_key)

    from services.roster_service import get_prev_month_tail_service

    prev_tail_payload = get_prev_month_tail_service(
        year=year_value,
        month=month_value,
        schedule_id=schedule.schedule_id,
        tail_days=6,
        group_id=target_group_id,
        current_user=current_user,
        db=db,
    )
    prev_tail_by_nurse = _prev_tail_schedule_map(prev_tail_payload)

    target_grade = getattr(nurse_by_id.get(req.target_nurse_id), "grade", None)
    top_k = req.top_k
    max_scan = req.options.max_candidate_scan
    ranking_scope = str(getattr(req.options, "ranking_scope", "ALL") or "ALL").upper()
    violation_delta_lambda = 18.0
    if ranking_scope not in {
        "ALL", "OFF_ONLY", "ON_DUTY_ONLY", "VACATION_ONLY",
        "ON_DUTY_OR_OFF", "ON_DUTY_OR_VACATION", "OFF_OR_VACATION",
    }:
        ranking_scope = "ALL"

    if req.mode == "BULK":
        # Path 1: 일자별 최적 (기존 markov, 최적안만)
        markov_results, markov_paths = _recommend_bulk_markov(
            slots=slots,
            req=req,
            schedule_map=schedule_map,
            schedule_shift_pk_map=schedule_shift_pk_map,
            nurse_by_id=nurse_by_id,
            shift_meta=shift_meta,
            shift_meta_by_pk=shift_meta_by_pk,
            config=config,
            prev_tail_by_nurse=prev_tail_by_nurse,
            pref_score_map=pref_score_map,
            pair_score_map=pair_score_map,
            target_grade=target_grade,
            ranking_scope=ranking_scope,
            max_scan=max_scan,
            assignment_blocked=assignment_blocked,
        )
        path_1 = markov_paths[0] if markov_paths else None
        if path_1:
            path_1.path_rank = 1
            path_1.scenario_label = "일자별 최적"

        # Path 2: EVEN n-gram 최적 분할 (구간별 1명 전담)
        target_pk_row = schedule_shift_pk_map.get(req.target_nurse_id)
        _common_eval_args = dict(
            req=req,
            schedule_map=schedule_map,
            schedule_shift_pk_map=schedule_shift_pk_map,
            nurse_by_id=nurse_by_id,
            shift_meta=shift_meta,
            shift_meta_by_pk=shift_meta_by_pk,
            config=config,
            prev_tail_by_nurse=prev_tail_by_nurse,
            pref_score_map=pref_score_map,
            pair_score_map=pair_score_map,
            target_grade=target_grade,
            ranking_scope=ranking_scope,
            max_scan=max_scan,
            assignment_blocked=assignment_blocked,
        )

        def _ngram_eval(seg_slots: List[ReplacementSlot]) -> Tuple[int, float]:
            """n-gram DP용 평가: (위반수, 점수) 반환."""
            cand, score = _evaluate_segment_single_nurse(
                seg_slots=seg_slots, used_nurse_ids=set(), **_common_eval_args,
            )
            if not cand:
                return 999, 0.0
            trial_steps = [BulkPathStep(slot=s, candidate=cand) for s in seg_slots]
            v = _count_path_violations(
                path_steps=trial_steps, schedule_map=schedule_map,
                schedule_shift_pk_map=schedule_shift_pk_map,
                nurse_by_id=nurse_by_id, shift_meta=shift_meta,
                shift_meta_by_pk=shift_meta_by_pk, config=config,
                prev_tail_by_nurse=prev_tail_by_nurse,
                target_nurse_id=req.target_nurse_id, include_target_nurse=False,
            )
            return (v.total_count if v else 0), score

        even_segments = _split_slots_ngram_optimal(
            slots=slots, max_window=3, min_window=2, evaluate_fn=_ngram_eval,
        )
        path_2 = _build_split_path(
            segments_raw=even_segments,
            path_rank=2, scenario_label="균등 분할",
            **_common_eval_args,
        )

        # Path 3: SHIFT_TYPE 분할 (구간별 1명 전담)
        shift_type_segments = _split_slots_by_shift_type(
            slots=slots, shift_meta=shift_meta,
            shift_meta_by_pk=shift_meta_by_pk,
            target_schedule_shift_pks=target_pk_row,
        )
        path_3 = _build_split_path(
            segments_raw=shift_type_segments,
            path_rank=3, scenario_label="근무유형 분할",
            **_common_eval_args,
        )

        bulk_paths = [p for p in [path_1, path_2, path_3] if p]
        return ReplacementRecommendResponse(
            schedule_id=req.schedule_id,
            mode=req.mode,
            target_nurse_id=req.target_nurse_id,
            results=markov_results,
            bulk_paths=bulk_paths,
            metadata={
                "evaluated_slots": len(slots),
                "candidate_scan_limit": max_scan,
                "allow_non_off_candidates": req.options.allow_non_off_candidates,
                "applied_ranking_scope": ranking_scope,
                "violation_delta_lambda": round(violation_delta_lambda, 3),
                "violation_type_weights": VIOLATION_TYPE_WEIGHT,
                "scoring_policy": "unified_3_strategy",
            },
        )

    shift_fallback_reason_counts: Dict[str, int] = defaultdict(int)
    shift_fallback_event_count = 0

    results: List[SlotRecommendation] = []

    for slot in slots:
        day = slot.date.day
        day_idx = day - 1
        target_shift_raw = slot.shift
        target_shift_main, target_shift_reason = _main_shift_code_with_reason(
            target_shift_raw,
            shift_meta,
            shift_meta_by_pk=shift_meta_by_pk,
        )
        if target_shift_reason:
            shift_fallback_event_count += 1
            shift_fallback_reason_counts[target_shift_reason] += 1
            logger.warning(
                "near-error: replacement semantic mapping fallback reason=%s shift=%s slot_date=%s",
                target_shift_reason,
                target_shift_raw,
                slot.date.isoformat(),
            )

        excluded = defaultdict(int)
        candidate_pool: List[Tuple[str, CandidateContext]] = []
        fallback_pool: List[Tuple[str, CandidateContext]] = []

        for candidate_id, nurse in nurse_by_id.items():
            if candidate_id == req.target_nurse_id:
                excluded["same_as_target"] += 1
                continue
            if candidate_id not in schedule_map:
                excluded["not_in_schedule"] += 1
                continue

            if int(getattr(nurse, "active", 1) or 1) == 0:
                excluded["inactive_nurse"] += 1
                continue

            join_date = _to_date(getattr(nurse, "joining_date", None))
            resign_date = _to_date(getattr(nurse, "resignation_date", None))
            if join_date and slot.date < join_date:
                excluded["before_joining_date"] += 1
                continue
            if resign_date and slot.date > resign_date:
                excluded["after_resignation_date"] += 1
                continue

            if slot.date in assignment_blocked.get(candidate_id, set()):
                excluded["assignment_blocked"] += 1
                continue

            _cap_fallback = not _nurse_can_work_shift(nurse, target_shift_main)
            if _cap_fallback:
                excluded["shift_not_allowed"] += 1

            schedule_row = schedule_map[candidate_id]
            schedule_shift_pk_row = schedule_shift_pk_map.get(candidate_id, [])
            assigned_shift = schedule_row[day_idx] if day_idx < len(schedule_row) else "-"
            assigned_shift_pk = schedule_shift_pk_row[day_idx] if day_idx < len(schedule_shift_pk_row) else None
            assigned_main, assigned_is_off_rest, assigned_is_vacation, assigned_is_off, assigned_reason = (
                _resolve_shift_semantic_with_reason(
                    assigned_shift,
                    shift_meta,
                    shift_pk=assigned_shift_pk,
                    shift_meta_by_pk=shift_meta_by_pk,
                )
            )
            assigned_is_off_rest = assigned_is_off and not assigned_is_vacation

            # 휴가(생, 연 등) 또는 특수 코드 — 건드리면 안 되는 셀
            if assigned_is_vacation:
                excluded["vacation_shift"] += 1
                continue
            if not assigned_is_off and assigned_main not in ("D", "E", "N", "M"):
                excluded["special_shift_code"] += 1
                continue

            if assigned_reason:
                shift_fallback_event_count += 1
                shift_fallback_reason_counts[assigned_reason] += 1
                logger.warning(
                    "near-error: replacement semantic mapping fallback reason=%s shift=%s candidate_id=%s slot_date=%s",
                    assigned_reason,
                    assigned_shift,
                    candidate_id,
                    slot.date.isoformat(),
                )

            if not _is_candidate_allowed_by_scope(
                ranking_scope, assigned_is_off_rest, assigned_is_vacation,
            ):
                excluded["ranking_scope_filtered"] += 1
                continue
            if ranking_scope == "ALL" and not req.options.allow_non_off_candidates and not assigned_is_off:
                excluded["non_off_candidate_disabled"] += 1
                continue

            rule_safety, estimated_delta, rule_tags = _rule_safety_score(
                nurse=nurse,
                schedule=schedule_row,
                day_idx=day_idx,
                target_shift_code=target_shift_raw,
                shift_meta=shift_meta,
                config=config,
                prev_tail_schedule=prev_tail_by_nurse.get(candidate_id),
                schedule_shift_pks=schedule_shift_pk_row,
                target_shift_pk=(
                    schedule_shift_pk_map.get(req.target_nurse_id, [])[day_idx]
                    if day_idx < len(schedule_shift_pk_map.get(req.target_nurse_id, []))
                    else None
                ),
                shift_meta_by_pk=shift_meta_by_pk,
            )

            off_priority = 1.0 if assigned_is_off else 0.35
            if target_grade is None or getattr(nurse, "grade", None) is None:
                grade_fit = 0.5
            else:
                nurse_grade_value = int(getattr(nurse, "grade"))
                target_grade_value = int(target_grade)
                grade_diff = abs(nurse_grade_value - target_grade_value)
                grade_fit = float(exp(-0.7 * grade_diff))

            pref_value = pref_score_map.get((candidate_id, day, target_shift_main), 0.0)
            preference = max(0.0, min(1.0, pref_value / 5.0))

            p1 = pair_score_map.get((req.target_nurse_id, candidate_id), 0.0)
            p2 = pair_score_map.get((candidate_id, req.target_nurse_id), 0.0)
            pair = max(0.0, min(1.0, ((p1 + p2) / 2.0) / 5.0))

            tags = list(rule_tags)
            if assigned_is_vacation:
                tags.append("vacation_candidate")
            if target_shift_reason:
                tags.append("fallback_target_shift_mapping")
            if assigned_reason:
                tags.append("fallback_shift_mapping")
            if assigned_main == "O" and assigned_is_off_rest:
                tags.append("off_rest_candidate")
            if grade_fit >= 0.99:
                tags.append("same_grade")
            elif grade_fit >= 0.45:
                tags.append("near_grade")
            if preference >= 0.7:
                tags.append("high_preference")
            if pair >= 0.7:
                tags.append("high_pair_fit")

            vacation_penalty = 0.0
            if ranking_scope == "ALL" and assigned_is_vacation:
                vacation_penalty = 70.0
                tags.append("vacation_penalized")

            if _cap_fallback:
                tags.append("shift_capability_fallback")

            candidate_grade_value = None
            raw_candidate_grade = getattr(nurse, "grade", None)
            if raw_candidate_grade is not None:
                try:
                    candidate_grade_value = int(raw_candidate_grade)
                except Exception:
                    candidate_grade_value = None

            _ctx = CandidateContext(
                nurse_id=candidate_id,
                nurse_name=str(getattr(nurse, "name", candidate_id)),
                nurse_grade=candidate_grade_value,
                assigned_shift=assigned_shift,
                assigned_shift_pk_id=assigned_shift_pk,
                assigned_is_off=assigned_is_off,
                assigned_is_vacation=assigned_is_vacation,
                rule_safety=rule_safety,
                estimated_violation_delta=estimated_delta,
                off_priority=off_priority,
                grade_fit=grade_fit,
                preference=preference,
                pair=pair,
                fairness=0.5,
                change_cost=0.0 if assigned_is_off else 1.0,
                vacation_penalty=vacation_penalty,
                tags=tags,
            )
            if _cap_fallback:
                fallback_pool.append((candidate_id, _ctx))
            else:
                candidate_pool.append((candidate_id, _ctx))

            if len(candidate_pool) >= max_scan:
                break

        # top_k 미만이면 fallback 풀 전체 병합 (후속 정렬에서 점수 상위만 살아남음)
        _top_k = int(getattr(req, "top_k", 10) or 10)
        if len(candidate_pool) < _top_k and fallback_pool:
            candidate_pool.extend(fallback_pool)

        fairness_map = _fairness_scores(
            candidate_pool,
            schedule_map,
            schedule_shift_pk_map,
            target_shift_raw,
            shift_meta,
            shift_meta_by_pk=shift_meta_by_pk,
        )
        for candidate_id, ctx in candidate_pool:
            ctx.fairness = fairness_map.get(candidate_id, 0.5)

        scored = sorted(candidate_pool, key=lambda item: (_final_score(item[1]), item[1].grade_fit, item[0]), reverse=True)
        if ranking_scope == "ALL":
            non_vacation = [item for item in scored if not item[1].assigned_is_vacation]
            vacation = [item for item in scored if item[1].assigned_is_vacation]
            scored = non_vacation + vacation
        # shift_capability_fallback 후보는 항상 정규 후보 뒤로 배치 (내부 순서는 기존 정렬 유지)
        _regular = [it for it in scored if "shift_capability_fallback" not in it[1].tags]
        _fb = [it for it in scored if "shift_capability_fallback" in it[1].tags]
        scored = _regular + _fb
        top = scored[:top_k]

        candidate_models: List[CandidateRecommendation] = []
        for idx, (_candidate_id, ctx) in enumerate(top, start=1):
            candidate_models.append(
                CandidateRecommendation(
                    nurse_id=ctx.nurse_id,
                    name=ctx.nurse_name,
                    candidate_grade=ctx.nurse_grade,
                    current_assigned_shift_code=ctx.assigned_shift,
                    current_assigned_shift_pk_id=ctx.assigned_shift_pk_id,
                    final_score=_final_score(ctx),
                    rank=idx,
                    tags=ctx.tags if req.options.include_explanations else [],
                    breakdown=CandidateScoreBreakdown(
                        rule_safety=round(ctx.rule_safety, 3),
                        off_priority=round(ctx.off_priority, 3),
                        grade_fit=round(ctx.grade_fit, 3),
                        preference=round(ctx.preference, 3),
                        pair=round(ctx.pair, 3),
                        fairness=round(ctx.fairness, 3),
                        change_cost=round(ctx.change_cost, 3),
                        estimated_violation_delta=round(ctx.estimated_violation_delta, 3),
                    ),
                )
            )

        status = "OK"
        if not candidate_models:
            status = "NONE"
        elif len(candidate_models) < top_k:
            status = "LIMITED"

        results.append(
            SlotRecommendation(
                slot=slot,
                recommendation_status=status,
                candidates=candidate_models,
                excluded_summary=dict(excluded),
            )
        )

    return ReplacementRecommendResponse(
        schedule_id=req.schedule_id,
        mode=req.mode,
        target_nurse_id=req.target_nurse_id,
        results=results,
        metadata={
            "evaluated_slots": len(slots),
            "candidate_scan_limit": max_scan,
            "allow_non_off_candidates": req.options.allow_non_off_candidates,
            "applied_ranking_scope": ranking_scope,
            "shift_semantic_fallback_event_count": shift_fallback_event_count,
            "shift_semantic_fallback_reasons": dict(shift_fallback_reason_counts),
            "scoring_policy": "rule_safety_priority_with_grade_fit",
        },
    )


def _evaluate_single_slot(
    slot: ReplacementSlot,
    req: ReplacementRecommendRequest,
    schedule_map: Dict[str, List[str]],
    schedule_shift_pk_map: Dict[str, List[Optional[str]]],
    nurse_by_id: Dict[str, Any],
    shift_meta: Dict[str, Dict[str, Any]],
    shift_meta_by_pk: Dict[str, Dict[str, Any]],
    config: Any,
    prev_tail_by_nurse: Dict[str, List[str]],
    pref_score_map: Dict[Tuple[str, int, str], float],
    pair_score_map: Dict[Tuple[str, str], float],
    target_grade: Optional[int],
    ranking_scope: str,
    max_scan: int,
    replacement_usage: Optional[Dict[str, int]] = None,
    root_nurse_id: Optional[str] = None,
    prev_nurse_id: Optional[str] = None,
    assignment_blocked: Optional[Dict[str, set]] = None,
    allow_capability_fallback: bool = False,
) -> List[Tuple[str, CandidateContext, float]]:
    day = slot.date.day
    day_idx = day - 1
    target_shift_raw = slot.shift
    target_shift_main = _main_shift_code(target_shift_raw, shift_meta)

    usage = replacement_usage or {}
    _asgn_blocked = assignment_blocked or {}
    candidate_pool: List[Tuple[str, CandidateContext]] = []

    for candidate_id, nurse in nurse_by_id.items():
        if candidate_id == req.target_nurse_id:
            continue
        if candidate_id not in schedule_map:
            continue
        if int(getattr(nurse, "active", 1) or 1) == 0:
            continue

        join_date = _to_date(getattr(nurse, "joining_date", None))
        resign_date = _to_date(getattr(nurse, "resignation_date", None))
        if join_date and slot.date < join_date:
            continue
        if resign_date and slot.date > resign_date:
            continue
        if slot.date in _asgn_blocked.get(candidate_id, set()):
            continue
        _cap_fallback = not _nurse_can_work_shift(nurse, target_shift_main)
        if _cap_fallback and not allow_capability_fallback:
            continue

        schedule_row = schedule_map[candidate_id]
        schedule_shift_pk_row = schedule_shift_pk_map.get(candidate_id, [])
        assigned_shift = schedule_row[day_idx] if day_idx < len(schedule_row) else "-"
        assigned_shift_pk = (
            schedule_shift_pk_row[day_idx]
            if day_idx < len(schedule_shift_pk_row)
            else None
        )
        (
            assigned_main,
            assigned_is_off_rest,
            assigned_is_vacation,
            assigned_is_off,
            _reason,
        ) = _resolve_shift_semantic_with_reason(
            assigned_shift,
            shift_meta,
            shift_pk=assigned_shift_pk,
            shift_meta_by_pk=shift_meta_by_pk,
        )
        assigned_is_off_rest = assigned_is_off and not assigned_is_vacation

        # 휴가(생, 연 등) 또는 특수 코드 — 건드리면 안 되는 셀
        if assigned_is_vacation:
            continue
        if not assigned_is_off and assigned_main not in ("D", "E", "N", "M"):
            continue

        if not _is_candidate_allowed_by_scope(
            ranking_scope, assigned_is_off_rest, assigned_is_vacation,
        ):
            continue
        if (
            ranking_scope == "ALL"
            and not req.options.allow_non_off_candidates
            and not assigned_is_off
        ):
            continue

        rule_safety, estimated_delta, rule_tags = _rule_safety_score(
            nurse=nurse,
            schedule=schedule_row,
            day_idx=day_idx,
            target_shift_code=target_shift_raw,
            shift_meta=shift_meta,
            config=config,
            prev_tail_schedule=prev_tail_by_nurse.get(candidate_id),
            schedule_shift_pks=schedule_shift_pk_row,
            target_shift_pk=(
                schedule_shift_pk_map.get(req.target_nurse_id, [])[day_idx]
                if day_idx < len(schedule_shift_pk_map.get(req.target_nurse_id, []))
                else None
            ),
            shift_meta_by_pk=shift_meta_by_pk,
        )

        prior_usage = usage.get(candidate_id, 0)
        path_penalty = 0.0
        if "consecutive_work_risk" in rule_tags:
            path_penalty += 30.0
            estimated_delta += 0.7
            rule_tags = list(rule_tags) + ["consecutive_work_penalized"]
        if _cap_fallback:
            # Level 1 fallback: shift_capability 미충족 — 큰 패널티로 정규 후보 뒤로 밀어냄
            path_penalty += 100.0
            estimated_delta += 1.0
            rule_tags = list(rule_tags) + ["shift_capability_fallback"]

        if assigned_is_off:
            off_tail = 0
            cursor = day_idx + 1
            while cursor < len(schedule_row):
                cursor_shift_pk = (
                    schedule_shift_pk_row[cursor]
                    if cursor < len(schedule_shift_pk_row)
                    else None
                )
                _m, _r, _v, is_off_any, _reason2 = _resolve_shift_semantic_with_reason(
                    schedule_row[cursor],
                    shift_meta,
                    shift_pk=cursor_shift_pk,
                    shift_meta_by_pk=shift_meta_by_pk,
                )
                if not is_off_any:
                    break
                off_tail += 1
                cursor += 1
            if off_tail > 0:
                path_penalty += min(40.0, 11.0 * off_tail)
                estimated_delta += round(min(1.2, 0.2 * off_tail), 3)
                rule_tags = list(rule_tags) + [
                    f"residual_off_preservation_penalty_x{off_tail}"
                ]

        if prior_usage > 0:
            usage_risk = min(95.0, 25.0 * prior_usage)
            rule_safety = max(0.0, rule_safety - (usage_risk / 100.0))
            estimated_delta += round(usage_risk / 35.0, 3)
            path_penalty += 30.0 * prior_usage
            rule_tags = list(rule_tags) + [f"replacement_reuse_x{prior_usage}"]
        if prev_nurse_id and candidate_id == prev_nurse_id:
            path_penalty += 34.0
            estimated_delta += 0.6
            rule_tags = list(rule_tags) + ["back_to_back_replacement_penalty"]

        off_priority = 1.0 if assigned_is_off else 0.35
        if target_grade is None or getattr(nurse, "grade", None) is None:
            grade_fit = 0.5
        else:
            grade_diff = abs(int(getattr(nurse, "grade")) - int(target_grade))
            grade_fit = float(exp(-0.7 * grade_diff))

        pref_value = pref_score_map.get((candidate_id, day, target_shift_main), 0.0)
        preference = max(0.0, min(1.0, pref_value / 5.0))

        p1 = pair_score_map.get((req.target_nurse_id, candidate_id), 0.0)
        p2 = pair_score_map.get((candidate_id, req.target_nurse_id), 0.0)
        pair = max(0.0, min(1.0, ((p1 + p2) / 2.0) / 5.0))
        prev_pair = 0.0
        if prev_nurse_id and prev_nurse_id != req.target_nurse_id:
            pp1 = pair_score_map.get((prev_nurse_id, candidate_id), 0.0)
            pp2 = pair_score_map.get((candidate_id, prev_nurse_id), 0.0)
            prev_pair = max(0.0, min(1.0, ((pp1 + pp2) / 2.0) / 5.0))
        root_pair = 0.0
        if root_nurse_id and root_nurse_id != req.target_nurse_id:
            rp1 = pair_score_map.get((root_nurse_id, candidate_id), 0.0)
            rp2 = pair_score_map.get((candidate_id, root_nurse_id), 0.0)
            root_pair = max(0.0, min(1.0, ((rp1 + rp2) / 2.0) / 5.0))
        if prev_nurse_id and root_nurse_id:
            pair = max(
                0.0, min(1.0, (pair * 0.35) + (prev_pair * 0.45) + (root_pair * 0.2))
            )
        elif prev_nurse_id:
            pair = max(0.0, min(1.0, (pair * 0.45) + (prev_pair * 0.55)))
        elif root_nurse_id:
            pair = max(0.0, min(1.0, (pair * 0.55) + (root_pair * 0.45)))

        tags = list(rule_tags)
        if assigned_is_vacation:
            tags.append("vacation_candidate")
        if assigned_is_off_rest:
            tags.append("off_rest_candidate")
        if grade_fit >= 0.99:
            tags.append("same_grade")
        elif grade_fit >= 0.45:
            tags.append("near_grade")
        if preference >= 0.7:
            tags.append("high_preference")
        if pair >= 0.7:
            tags.append("high_pair_fit")
        if prev_pair >= 0.7:
            tags.append("high_prev_pair_fit")
        if root_pair >= 0.7:
            tags.append("high_root_pair_fit")

        vacation_penalty = 0.0
        if ranking_scope == "ALL" and assigned_is_vacation:
            vacation_penalty = 70.0
            tags.append("vacation_penalized")

        candidate_grade_value = None
        raw_grade = getattr(nurse, "grade", None)
        if raw_grade is not None:
            try:
                candidate_grade_value = int(raw_grade)
            except Exception:
                pass

        candidate_pool.append(
            (
                candidate_id,
                CandidateContext(
                    nurse_id=candidate_id,
                    nurse_name=str(getattr(nurse, "name", candidate_id)),
                    nurse_grade=candidate_grade_value,
                    assigned_shift=assigned_shift,
                    assigned_shift_pk_id=assigned_shift_pk,
                    assigned_is_off=assigned_is_off,
                    assigned_is_vacation=assigned_is_vacation,
                    rule_safety=rule_safety,
                    estimated_violation_delta=estimated_delta,
                    off_priority=off_priority,
                    grade_fit=grade_fit,
                    preference=preference,
                    pair=pair,
                    fairness=0.5,
                    change_cost=0.0 if assigned_is_off else 1.0,
                    vacation_penalty=vacation_penalty + path_penalty,
                    tags=tags,
                ),
            )
        )

        if len(candidate_pool) >= max_scan:
            break

    fairness_map = _fairness_scores(
        candidate_pool,
        schedule_map,
        schedule_shift_pk_map,
        target_shift_raw,
        shift_meta,
        shift_meta_by_pk=shift_meta_by_pk,
    )
    for cid, ctx in candidate_pool:
        ctx.fairness = fairness_map.get(cid, 0.5)

    scored = sorted(
        candidate_pool, key=lambda item: _final_score(item[1]), reverse=True
    )
    if ranking_scope == "ALL":
        non_vacation = [item for item in scored if not item[1].assigned_is_vacation]
        vacation = [item for item in scored if item[1].assigned_is_vacation]
        scored = non_vacation + vacation

    return [(cid, ctx, _final_score(ctx)) for cid, ctx in scored]


def _apply_virtual_assignment(
    schedule_map: Dict[str, List[str]],
    schedule_shift_pk_map: Dict[str, List[Optional[str]]],
    candidate_id: str,
    day_idx: int,
    new_shift: str,
    new_shift_pk: Optional[str],
) -> Tuple[Dict[str, List[str]], Dict[str, List[Optional[str]]]]:
    new_schedule_map = {k: list(v) for k, v in schedule_map.items()}
    new_pk_map = {k: list(v) for k, v in schedule_shift_pk_map.items()}
    if candidate_id in new_schedule_map and day_idx < len(
        new_schedule_map[candidate_id]
    ):
        new_schedule_map[candidate_id][day_idx] = new_shift
        if candidate_id in new_pk_map and day_idx < len(new_pk_map[candidate_id]):
            new_pk_map[candidate_id][day_idx] = new_shift_pk
    return new_schedule_map, new_pk_map


def _ctx_to_candidate_model(
    ctx: CandidateContext, rank: int, include_explanations: bool
) -> CandidateRecommendation:
    return CandidateRecommendation(
        nurse_id=ctx.nurse_id,
        name=ctx.nurse_name,
        candidate_grade=ctx.nurse_grade,
        current_assigned_shift_code=ctx.assigned_shift,
        current_assigned_shift_pk_id=ctx.assigned_shift_pk_id,
        final_score=_final_score(ctx),
        rank=rank,
        tags=ctx.tags if include_explanations else [],
        breakdown=CandidateScoreBreakdown(
            rule_safety=round(ctx.rule_safety, 3),
            off_priority=round(ctx.off_priority, 3),
            grade_fit=round(ctx.grade_fit, 3),
            preference=round(ctx.preference, 3),
            pair=round(ctx.pair, 3),
            fairness=round(ctx.fairness, 3),
            change_cost=round(ctx.change_cost, 3),
            estimated_violation_delta=round(ctx.estimated_violation_delta, 3),
        ),
    )


def _count_path_violations(
    path_steps: List[BulkPathStep],
    schedule_map: Dict[str, List[str]],
    schedule_shift_pk_map: Dict[str, List[Optional[str]]],
    nurse_by_id: Dict[str, Any],
    shift_meta: Dict[str, Dict[str, Any]],
    shift_meta_by_pk: Dict[str, Dict[str, Any]],
    config: Any,
    prev_tail_by_nurse: Dict[str, List[str]],
    target_nurse_id: str,
    include_target_nurse: bool = True,
) -> PathViolationSummary:
    v_map = {k: list(v) for k, v in schedule_map.items()}
    v_pk_map = {k: list(v) for k, v in schedule_shift_pk_map.items()}
    target_pk_row = schedule_shift_pk_map.get(target_nurse_id, [])
    affected_nurses: Dict[str, set] = {}

    for step in path_steps:
        cid = step.candidate.nurse_id
        if not cid:
            continue
        day_idx = step.slot.date.day - 1
        if cid in v_map and day_idx < len(v_map[cid]):
            v_map[cid][day_idx] = step.slot.shift
        if cid in v_pk_map and day_idx < len(v_pk_map[cid]):
            v_pk_map[cid][day_idx] = (
                target_pk_row[day_idx] if day_idx < len(target_pk_row) else None
            )
        if cid not in affected_nurses:
            affected_nurses[cid] = set()
        affected_nurses[cid].add(day_idx)

    if include_target_nurse:
        for step in path_steps:
            day_idx = step.slot.date.day - 1
            if target_nurse_id not in affected_nurses:
                affected_nurses[target_nurse_id] = set()
            affected_nurses[target_nurse_id].add(day_idx)

    max_conseq = int(getattr(config, "max_conseq_work", 6) or 6) if config else 6
    monthly_night_limit = (
        int(getattr(config, "max_nig_per_month", 8) or 8) if config else 8
    )
    banned_day_after_eve = (
        bool(getattr(config, "banned_day_after_eve", False)) if config else False
    )
    two_off_after_two = (
        bool(getattr(config, "two_offs_after_two_nig", False)) if config else False
    )
    two_off_after_three = (
        bool(getattr(config, "two_offs_after_three_nig", False)) if config else False
    )

    violations: List[PathViolationDetail] = []
    by_type: Dict[str, int] = defaultdict(int)

    def _main_for(nid: str, shift_id: str, day_idx: int) -> str:
        pk_row = v_pk_map.get(nid, [])
        pk = pk_row[day_idx] if day_idx < len(pk_row) else None
        code, _ = _main_shift_code_with_reason(
            shift_id, shift_meta, shift_pk=pk, shift_meta_by_pk=shift_meta_by_pk
        )
        return code

    def _is_off_for(nid: str, shift_id: str, day_idx: int) -> bool:
        pk_row = v_pk_map.get(nid, [])
        pk = pk_row[day_idx] if day_idx < len(pk_row) else None
        _, _, _, is_off_any, _ = _resolve_shift_semantic_with_reason(
            shift_id,
            shift_meta,
            shift_pk=pk,
            shift_meta_by_pk=shift_meta_by_pk,
        )
        return is_off_any

    def _add(vtype: str, nid: str, day_idx: int, desc: str):
        nurse = nurse_by_id.get(nid)
        name = str(getattr(nurse, "name", nid)) if nurse else nid
        violations.append(
            PathViolationDetail(
                type=vtype,
                nurse_id=nid,
                nurse_name=name,
                day=day_idx + 1,
                description=desc,
            )
        )
        by_type[vtype] += 1

    for nid in affected_nurses:
        sched_row = v_map.get(nid)
        if not sched_row:
            continue
        nurse = nurse_by_id.get(nid)
        name = str(getattr(nurse, "name", nid)) if nurse else nid
        num_days = len(sched_row)

        for d in range(num_days):
            code = _main_for(nid, sched_row[d], d)
            if code == "O":
                continue

            streak = 1
            c = d - 1
            while c >= 0 and not _is_off_for(nid, sched_row[c], c):
                streak += 1
                c -= 1
            if c < 0:
                tail = prev_tail_by_nurse.get(nid, [])
                for t_shift in reversed(tail):
                    if _is_off_shift(t_shift, shift_meta):
                        break
                    streak += 1
            c = d + 1
            while c < num_days and not _is_off_for(nid, sched_row[c], c):
                streak += 1
                c += 1
            if streak > max_conseq:
                _add(
                    "consecutive_work",
                    nid,
                    d,
                    f"{name}: 연속 {streak}일 근무 (제한 {max_conseq}일)",
                )

            if d > 0:
                prev_code = _main_for(nid, sched_row[d - 1], d - 1)
                if prev_code == "N" and code == "D":
                    _add("night_nd", nid, d, f"{name}: N→D 위반")
                if prev_code == "N" and code == "E":
                    _add("night_ne", nid, d, f"{name}: N→E 위반")
                if banned_day_after_eve and prev_code == "E" and code == "D":
                    _add("eve_ed", nid, d, f"{name}: E→D 위반")

        night_count = sum(
            1 for d in range(num_days) if _main_for(nid, sched_row[d], d) == "N"
        )
        if night_count > monthly_night_limit:
            _add(
                "night_month_limit",
                nid,
                0,
                f"{name}: 월 N근무 {night_count}회 (제한 {monthly_night_limit}회)",
            )
        if night_count > 15:
            _add(
                "night_15n_hard_limit",
                nid,
                0,
                f"{name}: 월 N근무 {night_count}회 (절대상한 15회 초과)",
            )

        # OFF 일수 보장 위반 체크
        required_off = int(getattr(config, "off_days", 8) or 8) if config else 8
        off_count = sum(
            1 for d in range(num_days) if _is_off_for(nid, sched_row[d], d)
        )
        if off_count < required_off:
            _add(
                "off_day_shortage",
                nid,
                0,
                f"{name}: 월 OFF {off_count}일 (기준 {required_off}일 미달)",
            )

        if two_off_after_two or two_off_after_three:
            for d in range(1, num_days):
                if _main_for(nid, sched_row[d], d) != "N":
                    continue
                n_streak = 1
                c = d - 1
                while c >= 0 and _main_for(nid, sched_row[c], c) == "N":
                    n_streak += 1
                    c -= 1
                if d + 1 < num_days and _main_for(nid, sched_row[d + 1], d + 1) == "N":
                    continue
                off_after = 0
                for o in range(d + 1, min(d + 3, num_days)):
                    if _is_off_for(nid, sched_row[o], o):
                        off_after += 1
                    else:
                        break
                if two_off_after_two and n_streak >= 2 and off_after < 2:
                    _add(
                        "rec_2n2o",
                        nid,
                        d,
                        f"{name}: {n_streak}N 후 OFF {off_after}일 (2일 필요)",
                    )
                if two_off_after_three and n_streak >= 3 and off_after < 2:
                    _add(
                        "rec_3n2o",
                        nid,
                        d,
                        f"{name}: {n_streak}N 후 OFF {off_after}일 (2일 필요)",
                    )

    # 일자별 커버리지 위반 — path가 새로 만들거나 악화시킨 미충족만 카운트
    # real_baseline = target 포함 실제 현재 상태, post = target 제외 + path overwrite 적용
    cov_req = _build_coverage_req(config) if config else {}
    if cov_req:
        def _count_shift_at(src_map, src_pk_map, d: int) -> Dict[str, int]:
            c: Dict[str, int] = defaultdict(int)
            for nid, sched_row in src_map.items():
                if nid == target_nurse_id or d >= len(sched_row):
                    continue
                pk_row = src_pk_map.get(nid, [])
                pk = pk_row[d] if d < len(pk_row) else None
                m_code, _, _, is_off_here, _ = _resolve_shift_semantic_with_reason(
                    sched_row[d], shift_meta, shift_pk=pk, shift_meta_by_pk=shift_meta_by_pk,
                )
                if not is_off_here and m_code in cov_req:
                    c[m_code] += 1
            return c

        target_sched_row = schedule_map.get(target_nurse_id, [])
        target_pk_row_for_cov = schedule_shift_pk_map.get(target_nurse_id, [])

        def _target_shift_on(d: int) -> Optional[str]:
            if d >= len(target_sched_row):
                return None
            pk = target_pk_row_for_cov[d] if d < len(target_pk_row_for_cov) else None
            m_code, _, _, is_off_here, _ = _resolve_shift_semantic_with_reason(
                target_sched_row[d],
                shift_meta,
                shift_pk=pk,
                shift_meta_by_pk=shift_meta_by_pk,
            )
            return None if is_off_here else m_code

        affected_days = sorted({step.slot.date.day - 1 for step in path_steps})
        for d in affected_days:
            base_cnt = _count_shift_at(schedule_map, schedule_shift_pk_map, d)
            post_cnt = _count_shift_at(v_map, v_pk_map, d)
            target_shift = _target_shift_on(d)
            for m_code, req_min in cov_req.items():
                if req_min <= 0:
                    continue
                post = post_cnt.get(m_code, 0)
                base = base_cnt.get(m_code, 0)
                real_baseline = base + (1 if target_shift == m_code else 0)
                if post >= req_min:
                    continue
                # path가 NEW 위반 만든 경우 OR 이미 미충족인데 더 악화시킨 경우만
                if real_baseline < req_min and post >= real_baseline:
                    continue
                delta = " 악화" if post < real_baseline else ""
                violations.append(
                    PathViolationDetail(
                        type="daily_coverage_shortage",
                        nurse_id="",
                        nurse_name="",
                        day=d + 1,
                        description=f"{m_code} {post}/{req_min}{delta} (현재 {real_baseline})",
                    )
                )
                by_type["daily_coverage_shortage"] += 1

    seen = set()
    unique_violations = []
    for v in violations:
        key = (v.type, v.nurse_id, v.day, v.description)
        if key not in seen:
            seen.add(key)
            unique_violations.append(v)

    unique_by_type: Dict[str, int] = defaultdict(int)
    for v in unique_violations:
        unique_by_type[v.type] += 1

    return PathViolationSummary(
        total_count=len(unique_violations),
        by_type=dict(unique_by_type),
        details=unique_violations,
    )


def _weighted_violation_score(summary: PathViolationSummary) -> float:
    """Compute weighted violation penalty from a PathViolationSummary.

    Instead of treating all violations equally, applies VIOLATION_TYPE_WEIGHT
    so severe violations (rec_3n2o, consecutive_work) penalize much more
    than mild ones (eve_ed).
    """
    score = 0.0
    for vtype, count in summary.by_type.items():
        weight = VIOLATION_TYPE_WEIGHT.get(vtype, 1.0)
        score += weight * count
    return score


def _nurse_repeat_violation_penalty(summary: PathViolationSummary) -> float:
    """Extra penalty when the same nurse is violated on multiple days.

    Detects patterns where a single nurse accumulates violations across
    different days in the path — indicates that nurse is being overloaded.
    """
    nurse_days: Dict[str, set] = defaultdict(set)
    for detail in summary.details:
        nurse_days[detail.nurse_id].add(detail.day)

    penalty = 0.0
    for nid, days in nurse_days.items():
        if len(days) > 1:
            # Each additional violation day for the same nurse adds escalating penalty
            penalty += 1.5 * (len(days) - 1)
    return penalty


def _markov_fallback_pick(
    slot: ReplacementSlot,
    path_idx: int,
    used_cids: set,
    **eval_kwargs: Any,
) -> Tuple[Optional[Tuple[str, "CandidateContext", float]], int]:
    """Markov path에서 정규 후보가 없을 때 Level 1 → Level 2 단계 완화 평가.

    Level 1: shift_capability_fallback 풀 병합. used_cids는 그대로 적용.
    Level 2: used_cids 격리도 해제. Level 1 → Level 2 순으로 시도.

    하드 제약(연속근무·NDED·1N·max_conseq·assignment_blocked·ranking_scope·휴직 등)은
    어떤 단계에서도 완화되지 않는다.
    """
    scored_lvl1 = _evaluate_single_slot(
        slot=slot, allow_capability_fallback=True, **eval_kwargs,
    )
    pick_lvl1 = next(
        ((cid, ctx, score) for cid, ctx, score in scored_lvl1 if cid not in used_cids),
        None,
    )
    if pick_lvl1 is not None:
        return pick_lvl1, 1

    pick_lvl2 = next(iter(scored_lvl1), None)
    if pick_lvl2 is not None:
        cid, ctx, score = pick_lvl2
        # Level 2: 점수 추가 페널티로 정렬에서 항상 후순위 보장
        return (cid, ctx, score - 200.0), 2

    return None, 0


def _recommend_bulk_markov(
    slots: List[ReplacementSlot],
    req: ReplacementRecommendRequest,
    schedule_map: Dict[str, List[str]],
    schedule_shift_pk_map: Dict[str, List[Optional[str]]],
    nurse_by_id: Dict[str, Any],
    shift_meta: Dict[str, Dict[str, Any]],
    shift_meta_by_pk: Dict[str, Dict[str, Any]],
    config: Any,
    prev_tail_by_nurse: Dict[str, List[str]],
    pref_score_map: Dict[Tuple[str, int, str], float],
    pair_score_map: Dict[Tuple[str, str], float],
    target_grade: Optional[int],
    ranking_scope: str,
    max_scan: int,
    assignment_blocked: Optional[Dict[str, set]] = None,
) -> Tuple[List[SlotRecommendation], List[BulkPathRecommendation]]:
    """BULK recommendation — 3 scenario paths with exclusive candidate pools.

    Scenario A (최적안): each slot picks the best available candidate.
    Scenario B (차선안): excludes A's pick per slot, picks best from remaining.
    Scenario C (대안):   excludes A+B's picks per slot, picks best from remaining.

    Each path maintains its own virtual schedule, usage tracking, and
    multiplicative constraint factors (violation, reuse, back-to-back,
    nurse repeat, consecutive work).
    """
    if not slots:
        return [], []

    num_paths = 3
    target_pk_row = schedule_shift_pk_map.get(req.target_nurse_id, [])

    # ── Initialize per-path state ──
    paths: List[List[BulkPathStep]] = [[] for _ in range(num_paths)]
    path_scores: List[float] = [0.0] * num_paths
    path_schedule_maps: List[Dict[str, List[str]]] = [
        {k: list(v) for k, v in schedule_map.items()} for _ in range(num_paths)
    ]
    path_shift_pk_maps: List[Dict[str, List[Optional[str]]]] = [
        {k: list(v) for k, v in schedule_shift_pk_map.items()} for _ in range(num_paths)
    ]
    path_usage: List[Dict[str, int]] = [{} for _ in range(num_paths)]
    path_max_relax_level: List[int] = [0] * num_paths

    slot_results: List[SlotRecommendation] = []

    _no_candidate_step = lambda s: BulkPathStep(
        slot=s,
        candidate=CandidateRecommendation(
            nurse_id="", name="(no candidate)", final_score=0.0,
            rank=0, tags=["no_candidate_available"],
            breakdown=CandidateScoreBreakdown(
                rule_safety=0, off_priority=0, grade_fit=0,
                preference=0, pair=0, fairness=0,
                change_cost=0, estimated_violation_delta=0,
            ),
            no_candidate=True,
        ),
        transition_score=0.0,
    )

    # ── Process each slot ──
    for slot_idx, slot in enumerate(slots):
        day_idx = slot.date.day - 1
        target_shift_pk = (
            target_pk_row[day_idx] if day_idx < len(target_pk_row) else None
        )

        scenario_labels = ["A", "B", "C"]
        per_slot_candidates: List[CandidateRecommendation] = []

        # ── Phase 1: Evaluate and rank candidates for each path independently ──
        path_ranked: List[List[Tuple[int, float, str, CandidateContext, float, ConstraintFactors]]] = []

        for path_idx in range(num_paths):
            usage = path_usage[path_idx]
            prev_nurse_id = (
                paths[path_idx][-1].candidate.nurse_id
                if paths[path_idx] else None
            )
            root_nurse_id = (
                paths[path_idx][0].candidate.nurse_id
                if paths[path_idx] else None
            )

            scored = _evaluate_single_slot(
                slot=slot, req=req,
                schedule_map=path_schedule_maps[path_idx],
                schedule_shift_pk_map=path_shift_pk_maps[path_idx],
                nurse_by_id=nurse_by_id, shift_meta=shift_meta,
                shift_meta_by_pk=shift_meta_by_pk, config=config,
                prev_tail_by_nurse=prev_tail_by_nurse,
                pref_score_map=pref_score_map, pair_score_map=pair_score_map,
                target_grade=target_grade, ranking_scope=ranking_scope,
                max_scan=max_scan,
                replacement_usage=usage,
                prev_nurse_id=prev_nurse_id,
                root_nurse_id=root_nurse_id,
                assignment_blocked=assignment_blocked,
            )

            baseline_summary = _count_path_violations(
                path_steps=paths[path_idx],
                schedule_map=schedule_map,
                schedule_shift_pk_map=schedule_shift_pk_map,
                nurse_by_id=nurse_by_id, shift_meta=shift_meta,
                shift_meta_by_pk=shift_meta_by_pk, config=config,
                prev_tail_by_nurse=prev_tail_by_nurse,
                target_nurse_id=req.target_nurse_id,
                include_target_nurse=False,
            )
            baseline_weighted = _weighted_violation_score(baseline_summary)
            baseline_repeat = _nurse_repeat_violation_penalty(baseline_summary)

            candidates: List[Tuple[int, float, str, CandidateContext, float, ConstraintFactors]] = []
            eval_limit = min(8, len(scored))

            for cid, ctx, base_score in scored[:eval_limit]:
                candidate_model = _ctx_to_candidate_model(
                    ctx, rank=1,
                    include_explanations=req.options.include_explanations,
                )
                trial_step = BulkPathStep(
                    slot=slot, candidate=candidate_model,
                    transition_score=round(base_score, 3),
                )

                v_map, v_pk_map = _apply_virtual_assignment(
                    path_schedule_maps[path_idx],
                    path_shift_pk_maps[path_idx],
                    cid, day_idx, slot.shift, target_shift_pk,
                )
                sim_summary = _count_path_violations(
                    path_steps=paths[path_idx] + [trial_step],
                    schedule_map=schedule_map,
                    schedule_shift_pk_map=schedule_shift_pk_map,
                    nurse_by_id=nurse_by_id, shift_meta=shift_meta,
                    shift_meta_by_pk=shift_meta_by_pk, config=config,
                    prev_tail_by_nurse=prev_tail_by_nurse,
                    target_nurse_id=req.target_nurse_id,
                    include_target_nurse=False,
                )
                sim_violation_count = sim_summary.total_count if sim_summary else 0

                # ── Multiplicative constraint factors ──
                sim_weighted = _weighted_violation_score(sim_summary)
                weighted_delta = max(0.0, sim_weighted - baseline_weighted)
                f_violation = max(0.1, 1.0 - 0.12 * weighted_delta)

                prior_reuse = usage.get(cid, 0)
                f_reuse = max(0.15, 0.55 ** prior_reuse) if prior_reuse > 0 else 1.0

                f_back_to_back = 0.5 if (prev_nurse_id and cid == prev_nurse_id) else 1.0

                sim_repeat = _nurse_repeat_violation_penalty(sim_summary)
                repeat_delta = max(0.0, sim_repeat - baseline_repeat)
                f_nurse_repeat = max(0.2, 1.0 - 0.18 * repeat_delta)

                v_sched_row = v_map.get(cid, [])
                f_consecutive = 1.0
                if v_sched_row and day_idx < len(v_sched_row):
                    streak = 1
                    c = day_idx - 1
                    while c >= 0:
                        c_pk = (v_pk_map.get(cid, []) or [])[c] if c < len(v_pk_map.get(cid, []) or []) else None
                        _, _, _, is_off, _ = _resolve_shift_semantic_with_reason(
                            v_sched_row[c], shift_meta,
                            shift_pk=c_pk, shift_meta_by_pk=shift_meta_by_pk,
                        )
                        if is_off:
                            break
                        streak += 1
                        c -= 1
                    c = day_idx + 1
                    while c < len(v_sched_row):
                        c_pk = (v_pk_map.get(cid, []) or [])[c] if c < len(v_pk_map.get(cid, []) or []) else None
                        _, _, _, is_off, _ = _resolve_shift_semantic_with_reason(
                            v_sched_row[c], shift_meta,
                            shift_pk=c_pk, shift_meta_by_pk=shift_meta_by_pk,
                        )
                        if is_off:
                            break
                        streak += 1
                        c += 1
                    max_conseq = int(getattr(config, "max_conseq_work", 6) or 6) if config else 6
                    streak_over = max(0, streak - max_conseq)
                    f_consecutive = max(0.3, 1.0 - 0.1 * streak_over)

                # 커버리지 검증: 후보가 빠지면 원래 시프트 최소인원 위반 여부
                f_coverage = 1.0
                _cov_req_bulk = _build_coverage_req(config)
                cid_sched = path_schedule_maps[path_idx].get(cid, [])
                if cid_sched and day_idx < len(cid_sched):
                    c_pk = (path_shift_pk_maps[path_idx].get(cid, []) or [])[day_idx] if day_idx < len(path_shift_pk_maps[path_idx].get(cid, []) or []) else None
                    c_main, _, _, c_off, _ = _resolve_shift_semantic_with_reason(
                        cid_sched[day_idx], shift_meta,
                        shift_pk=c_pk, shift_meta_by_pk=shift_meta_by_pk,
                    )
                    if not c_off and c_main in _cov_req_bulk and _cov_req_bulk[c_main] > 0:
                        day_shift_count = 0
                        for o_id, o_sched in path_schedule_maps[path_idx].items():
                            if o_id == req.target_nurse_id or day_idx >= len(o_sched):
                                continue
                            o_pk_r = path_shift_pk_maps[path_idx].get(o_id, [])
                            o_pk = o_pk_r[day_idx] if day_idx < len(o_pk_r) else None
                            o_m, _, _, o_off, _ = _resolve_shift_semantic_with_reason(
                                o_sched[day_idx], shift_meta,
                                shift_pk=o_pk, shift_meta_by_pk=shift_meta_by_pk,
                            )
                            if not o_off and o_m == c_main:
                                day_shift_count += 1
                        if (day_shift_count - 1) < _cov_req_bulk[c_main]:
                            f_coverage = 0.1

                constraint_multiplier = (
                    f_violation * f_reuse * f_back_to_back
                    * f_nurse_repeat * f_consecutive * f_coverage
                )
                step_score = base_score * constraint_multiplier

                factors = ConstraintFactors(
                    f_violation=round(f_violation, 4),
                    f_reuse=round(f_reuse, 4),
                    f_back_to_back=round(f_back_to_back, 4),
                    f_nurse_repeat=round(f_nurse_repeat, 4),
                    f_consecutive=round(f_consecutive, 4),
                    f_coverage=round(f_coverage, 4),
                    combined=round(constraint_multiplier, 4),
                )

                candidates.append((sim_violation_count, step_score, cid, ctx, base_score, factors))

            # Sort: violation count asc, then step_score desc
            candidates.sort(key=lambda x: (x[0], -x[1]))
            path_ranked.append(candidates)

        # ── Phase 2: Coordinated top-k assignment across paths ──
        used_cids: set = set()
        # Maps cid -> (name, score, scenario_label) for excluded evidence
        slot_assignments: Dict[str, Tuple[str, float, str]] = {}

        for path_idx in range(num_paths):
            # Build excluded evidence from prior path assignments
            step_excluded_list: List[ExcludedCandidate] = []
            for ex_cid, (ex_name, ex_score, ex_scenario) in slot_assignments.items():
                step_excluded_list.append(ExcludedCandidate(
                    nurse_id=ex_cid,
                    name=ex_name,
                    original_score=round(ex_score, 3),
                    excluded_by_scenario=ex_scenario,
                ))

            assigned = False
            for candidate in path_ranked[path_idx]:
                _viol, pick_score, pick_cid, pick_ctx, pick_base, pick_factors = candidate
                if pick_cid in used_cids:
                    continue

                _orig_sched = path_schedule_maps[path_idx].get(pick_cid, [])
                _orig_pk_row = path_shift_pk_maps[path_idx].get(pick_cid, [])
                step = BulkPathStep(
                    slot=slot,
                    candidate=_ctx_to_candidate_model(
                        pick_ctx, rank=1,
                        include_explanations=req.options.include_explanations,
                    ),
                    original_shift_id=_orig_sched[day_idx] if day_idx < len(_orig_sched) else None,
                    original_shift_pk=str(_orig_pk_row[day_idx]) if day_idx < len(_orig_pk_row) and _orig_pk_row[day_idx] is not None else None,
                    transition_score=round(pick_score, 3),
                    base_score=round(pick_base, 3),
                    constraint_factors=pick_factors,
                    excluded_candidates=step_excluded_list,
                )
                paths[path_idx].append(step)
                path_scores[path_idx] += pick_score

                v_map, v_pk_map = _apply_virtual_assignment(
                    path_schedule_maps[path_idx],
                    path_shift_pk_maps[path_idx],
                    pick_cid, day_idx, slot.shift, target_shift_pk,
                )
                path_schedule_maps[path_idx] = v_map
                path_shift_pk_maps[path_idx] = v_pk_map
                path_usage[path_idx][pick_cid] = path_usage[path_idx].get(pick_cid, 0) + 1

                used_cids.add(pick_cid)
                slot_assignments[pick_cid] = (
                    pick_ctx.nurse_name,
                    pick_base,
                    scenario_labels[path_idx],
                )

                if path_idx == 0:
                    per_slot_candidates.append(step.candidate)

                assigned = True
                break

            if not assigned:
                fb_pick, fb_level = _markov_fallback_pick(
                    slot=slot, path_idx=path_idx, used_cids=used_cids,
                    req=req,
                    schedule_map=path_schedule_maps[path_idx],
                    schedule_shift_pk_map=path_shift_pk_maps[path_idx],
                    nurse_by_id=nurse_by_id, shift_meta=shift_meta,
                    shift_meta_by_pk=shift_meta_by_pk, config=config,
                    prev_tail_by_nurse=prev_tail_by_nurse,
                    pref_score_map=pref_score_map,
                    pair_score_map=pair_score_map,
                    target_grade=target_grade,
                    ranking_scope=ranking_scope, max_scan=max_scan,
                    replacement_usage=path_usage[path_idx],
                    prev_nurse_id=(
                        paths[path_idx][-1].candidate.nurse_id
                        if paths[path_idx] else None
                    ),
                    root_nurse_id=(
                        paths[path_idx][0].candidate.nurse_id
                        if paths[path_idx] else None
                    ),
                    assignment_blocked=assignment_blocked,
                )
                if fb_pick is not None:
                    fb_cid, fb_ctx, fb_score = fb_pick
                    fb_candidate = _ctx_to_candidate_model(
                        fb_ctx, rank=1,
                        include_explanations=req.options.include_explanations,
                    )
                    if fb_level == 2 and "relaxed_path_isolation" not in fb_candidate.tags:
                        fb_candidate.tags.append("relaxed_path_isolation")
                    fb_orig_sched = path_schedule_maps[path_idx].get(fb_cid, [])
                    fb_orig_pk_row = path_shift_pk_maps[path_idx].get(fb_cid, [])
                    logger.debug(
                        "BULK markov fallback applied: path_idx=%s slot=%s level=%s nurse=%s",
                        path_idx, slot.date, fb_level, fb_cid,
                    )
                    fb_step = BulkPathStep(
                        slot=slot,
                        candidate=fb_candidate,
                        original_shift_id=fb_orig_sched[day_idx] if day_idx < len(fb_orig_sched) else None,
                        original_shift_pk=str(fb_orig_pk_row[day_idx]) if day_idx < len(fb_orig_pk_row) and fb_orig_pk_row[day_idx] is not None else None,
                        transition_score=round(fb_score, 3),
                        base_score=round(fb_score, 3),
                        excluded_candidates=step_excluded_list,
                    )
                    paths[path_idx].append(fb_step)
                    path_scores[path_idx] += fb_score
                    v_map, v_pk_map = _apply_virtual_assignment(
                        path_schedule_maps[path_idx],
                        path_shift_pk_maps[path_idx],
                        fb_cid, day_idx, slot.shift, target_shift_pk,
                    )
                    path_schedule_maps[path_idx] = v_map
                    path_shift_pk_maps[path_idx] = v_pk_map
                    path_usage[path_idx][fb_cid] = path_usage[path_idx].get(fb_cid, 0) + 1
                    # Level 1만 used_cids에 등록 (Level 2는 재사용 허용)
                    if fb_level == 1:
                        used_cids.add(fb_cid)
                        slot_assignments[fb_cid] = (
                            fb_ctx.nurse_name, fb_score,
                            scenario_labels[path_idx],
                        )
                    if path_idx == 0:
                        per_slot_candidates.append(fb_candidate)
                    path_max_relax_level[path_idx] = max(
                        path_max_relax_level[path_idx], fb_level,
                    )
                else:
                    paths[path_idx].append(_no_candidate_step(slot))

        status = "OK" if per_slot_candidates else "NONE"
        slot_results.append(SlotRecommendation(
            slot=slot, recommendation_status=status,
            candidates=per_slot_candidates, excluded_summary={},
        ))

    # ── Compute final violations for each completed path ──
    scenario_descriptions = ["최적안", "차선안", "대안"]
    bulk_paths: List[BulkPathRecommendation] = []
    for path_idx in range(num_paths):
        violation_summary = _count_path_violations(
            path_steps=paths[path_idx],
            schedule_map=schedule_map,
            schedule_shift_pk_map=schedule_shift_pk_map,
            nurse_by_id=nurse_by_id,
            shift_meta=shift_meta,
            shift_meta_by_pk=shift_meta_by_pk,
            config=config,
            prev_tail_by_nurse=prev_tail_by_nurse,
            target_nurse_id=req.target_nurse_id,
            include_target_nurse=False,
        )
        if path_max_relax_level[path_idx] > 0:
            logger.debug(
                "BULK markov path fallback applied: path_idx=%s max_relax_level=%s",
                path_idx, path_max_relax_level[path_idx],
            )
        bulk_paths.append(BulkPathRecommendation(
            path_rank=path_idx + 1,
            scenario_label=scenario_descriptions[path_idx] if path_idx < len(scenario_descriptions) else f"시나리오 {path_idx + 1}",
            steps=paths[path_idx],
            total_path_score=round(path_scores[path_idx], 3),
            violations=violation_summary,
        ))

    return slot_results, bulk_paths

