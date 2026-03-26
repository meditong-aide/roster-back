from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime
import logging
from math import exp
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy.orm import Session

from db.models import (
    Group,
    Nurse,
    NursePairRequest,
    NurseShiftRequest,
    RosterConfig,
    Schedule,
    ScheduleEntry,
    Shift,
    WantedRequest,
)
from schemas.replacement_schema import (
    CandidateRecommendation,
    CandidateScoreBreakdown,
    ReplacementRecommendRequest,
    ReplacementRecommendResponse,
    ReplacementSlot,
    SlotRecommendation,
)


OFF_SHIFT_IDS = {"O", "OFF", "-", "주"}
VACATION_SHIFT_TYPE_TOKENS = {"휴가", "vacation", "annual_leave", "annual leave", "연차", "연가"}
WORK_SHIFT_GB_TO_MAIN = {
    "데이": "D",
    "이브닝": "E",
    "미드": "M",
    "나이트": "N",
}

logger = logging.getLogger(__name__)


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


def _resolve_target_group(
    db: Session,
    current_user,
    requested_group_id: Optional[str],
) -> Tuple[str, str]:
    is_head = bool(getattr(current_user, "is_head_nurse", False))
    is_admin = bool(getattr(current_user, "is_master_admin", False))
    if is_head and getattr(current_user, "group_id", None):
        target_group_id = str(current_user.group_id)
    elif is_admin:
        if not requested_group_id:
            raise ValueError("group_id is required for admin")
        grp = db.query(Group).filter(Group.group_id == requested_group_id).first()
        if not grp:
            raise ValueError("group not found")
        if getattr(current_user, "office_id", None) and current_user.office_id != grp.office_id:
            raise PermissionError("group does not belong to your office")
        target_group_id = str(grp.group_id)
    else:
        raise PermissionError("permission denied")

    grp = db.query(Group).filter(Group.group_id == target_group_id).first()
    if not grp:
        raise ValueError("group not found")
    return target_group_id, str(grp.office_id)


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


def _nurse_is_night_capable(nurse: Any) -> bool:
    raw = getattr(nurse, "is_night_nurse", None)
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


def recommend_replacement_candidates(
    req: ReplacementRecommendRequest,
    current_user,
    db: Session,
    requested_group_id: Optional[str] = None,
) -> ReplacementRecommendResponse:
    target_group_id, _target_office_id = _resolve_target_group(db, current_user, requested_group_id)

    schedule = (
        db.query(Schedule)
        .filter(
            Schedule.schedule_id == req.schedule_id,
            Schedule.group_id == target_group_id,
            Schedule.dropped == False,
        )
        .first()
    )
    if not schedule:
        raise ValueError("schedule not found")

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

    nurses = db.query(Nurse).filter(Nurse.group_id == target_group_id).all()
    nurse_by_id = {str(n.nurse_id): n for n in nurses}

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
    if ranking_scope not in {"ALL", "OFF_ONLY", "ON_DUTY_ONLY", "VACATION_ONLY"}:
        ranking_scope = "ALL"

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

            if not _nurse_can_work_shift(nurse, target_shift_main):
                excluded["shift_not_allowed"] += 1
                continue

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

            if ranking_scope == "OFF_ONLY" and not assigned_is_off_rest:
                excluded["ranking_scope_filtered"] += 1
                continue
            if ranking_scope == "ON_DUTY_ONLY" and assigned_is_off:
                excluded["ranking_scope_filtered"] += 1
                continue
            if ranking_scope == "VACATION_ONLY" and not assigned_is_vacation:
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

            candidate_grade_value = None
            raw_candidate_grade = getattr(nurse, "grade", None)
            if raw_candidate_grade is not None:
                try:
                    candidate_grade_value = int(raw_candidate_grade)
                except Exception:
                    candidate_grade_value = None

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
                        vacation_penalty=vacation_penalty,
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
        for candidate_id, ctx in candidate_pool:
            ctx.fairness = fairness_map.get(candidate_id, 0.5)

        scored = sorted(candidate_pool, key=lambda item: _final_score(item[1]), reverse=True)
        if ranking_scope == "ALL":
            non_vacation = [item for item in scored if not item[1].assigned_is_vacation]
            vacation = [item for item in scored if item[1].assigned_is_vacation]
            scored = non_vacation + vacation
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
