"""
Wanted(근무 희망 요청) 관련 서비스 로직 모듈
- DB 쿼리, 데이터 가공 등 라우터에서 분리
- 모든 함수는 한글 docstring, 한글 print/logging, PEP8 스타일 적용
"""
import json
import traceback
from collections import defaultdict
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional, Set, Tuple

from dateutil.relativedelta import relativedelta
from sqlalchemy import func, or_, and_
from sqlalchemy.orm import Session

from db.models import (
    FixedWantedEntry,
    Group,
    Nurse,
    NursePairRequest,
    NurseShiftRequest,
    Shift,
    ShiftPreference,
    Wanted,
    WantedRequest,
    WeeklyOffSetting,
    WantedConfig,
)
from schemas.auth_schema import User as UserSchema
from schemas.roster_schema import (
    WantedDeadlineRequest,
    WantedInvokeRequest,
    FixedWantedCreate,
    FixedWantedEntryCreate,
    AdjustmentNurse,
    AdjustmentResponse,
    FixedWantedEntryResponse,
)
from services.graph_service import graph_service
from services.weekly_off_service import (
    calc_weekly_off_weekday_by_month,
    calc_weekly_off_weekday_by_week,
)
from utils.utils import send_wanted_request_push


def _yyyymm(year: int, month: int) -> str:
    """연/월을 'YYYY-MM' 문자열로 변환합니다.

    인자:
        year: 연도
        month: 월(1~12)

    반환:
        'YYYY-MM' 형식의 문자열. 예: 2025, 9 → '2025-09'
    """
    return f"{year:04d}-{month:02d}"


def _ymd(year: int, month: int, day: int) -> date:
    """연/월/일을 date 객체로 변환합니다.

    인자:
        year: 연도
        month: 월(1~12)
        day: 일(1~31)

    반환:
        date 객체. 예: 2025, 9, 26 → date(2025, 9, 26)
    """
    return date(year, month, day)


def _next_request_id(db: Session, nurse_id: str, month_str: str) -> int:
    """해당 간호사/월 기준 다음 request_id 를 생성합니다.

    인자:
        db: DB 세션
        nurse_id: 간호사 ID
        month_str: 'YYYY-MM'

    반환:
        다음 request_id (최초면 1)
    """
    row = (
        db.query(WantedRequest.request_id)
        .filter(WantedRequest.nurse_id == nurse_id, WantedRequest.month == month_str)
        .order_by(WantedRequest.request_id.desc())
        .with_for_update()
        .first()
    )
    return (row[0] + 1) if row else 1


def _persist_wanted_request(db: Session, nurse_id: str, month_str: str, request: str | List[str]) -> int:
    """wanted_requests 레코드를 저장하고 request_id 를 반환합니다."""
    request_id = _next_request_id(db, nurse_id, month_str)
    
    request_text = ''.join(request) if isinstance(request, list) else request
    request_text = request_text.strip()
    
    wr = WantedRequest(
        nurse_id=nurse_id,
        request_id=request_id,
        request=request_text,
        month=month_str,
        is_submitted=0,
        created_at=datetime.now(),
        submitted_at=None,
    )
    db.add(wr)
    try:
        db.commit()
    except Exception as e:
        print(f"wanted_requests 저장 오류: {e}")
        db.rollback()
        raise e
    print(f"wanted_requests 저장 완료: nurse_id={nurse_id}, month={month_str}, request_id={request_id}")
    return request_id


def _next_detailed_request_id(db: Session, nurse_id: str, request_id: int, *, table: str) -> int:
    """해당 (nurse_id, request_id) 기준 다음 detailed_request_id 반환"""
    if table == "shift":
        max_id = db.query(func.max(NurseShiftRequest.detailed_request_id)).filter(
            NurseShiftRequest.nurse_id == nurse_id,
            NurseShiftRequest.request_id == request_id
        ).scalar() or 0
    elif table == "pair":
        max_id = db.query(func.max(NursePairRequest.detailed_request_id)).filter(
            NursePairRequest.nurse_id == nurse_id,
            NursePairRequest.request_id == request_id
        ).scalar() or 0
    else:
        raise ValueError("table은 'shift' 또는 'pair' 이어야 합니다.")

    next_id = max_id + 1
    print(f"[{table}] 다음 detailed_request_id 계산: request_id={request_id} → {next_id}")
    return next_id


def _compute_weekly_off_days(
    db: Session,
    nurse_id: str,
    group_id: str | None,
    year: int,
    month: int,
) -> Set[int]:
    """주휴 사용 간호사의 주휴 일자(day set)를 계산합니다.

    인자:
        db: DB 세션
        nurse_id: 간호사 ID
        group_id: 그룹 ID (주휴 설정 조회용)
        year: 대상 연도
        month: 대상 월

    반환:
        주휴 요일에 해당하는 day 집합(1~31). 예: {3, 10, 17, 24}
    """
    nurse_row = db.query(Nurse).filter(Nurse.nurse_id == nurse_id).first()
    if not nurse_row or not getattr(nurse_row, "weekly_off_enabled", False) or nurse_row.weekly_off_weekday is None:
        return set()

    setting = None
    if nurse_row.is_weekend_off:
        preview_weekday = 6
    else:
        if group_id:
            setting = db.query(WeeklyOffSetting).filter(WeeklyOffSetting.group_id == group_id).first()

        preview_weekday = nurse_row.weekly_off_weekday
        if setting and setting.use_variable_cycle:
            if setting.cycle_type == "month" and setting.base_year and setting.base_month:
                preview_weekday = calc_weekly_off_weekday_by_month(
                    base_weekday=nurse_row.weekly_off_weekday,
                    shift_variation=setting.shift_variation,
                    base_year=setting.base_year,
                    base_month=setting.base_month,
                    target_year=year,
                    target_month=month,
                )
            elif setting.cycle_type == "week" and setting.cycle_start_date:
                target_date = date(year, month, 1)
                preview_weekday = calc_weekly_off_weekday_by_week(
                    base_weekday=nurse_row.weekly_off_weekday,
                    shift_variation=setting.shift_variation,
                    cycle_start_date=setting.cycle_start_date,
                    target_date=target_date,
                    cycle_interval_weeks=setting.cycle_interval,
                )

    weekly_off_days: Set[int] = set()
    current = date(year, month, 1)
    while current.month == month:
        if current.weekday() == preview_weekday:
            weekly_off_days.add(current.day)
        current += timedelta(days=1)

    return weekly_off_days


def _drop_weekly_off_from_shift_map(
    shift_map: Dict[str, Dict[int, Dict[str, Any]]],
    weekly_off_days: Set[int],
) -> Dict[str, Dict[int, Dict[str, Any]]]:
    """주휴일과 겹치는 shift_map 엔트리를 제거합니다."""
    if not weekly_off_days or not shift_map:
        return shift_map

    for shift_code, days_map in list(shift_map.items()):
        for day in list(days_map.keys()):
            if day in weekly_off_days:
                print(f"[weekly_off 필터] {day}일 {shift_code} → 주휴로 제거")
                del days_map[day]
        if not days_map:
            del shift_map[shift_code]

    return shift_map


def _parse_case_date(
    date_value: Any,
    req_year: int,
    req_month: int,
    override_year: int | None = None,
    override_month: int | None = None,
) -> date | None:
    """case 항목의 날짜를 현재 요청 연/월 기준으로 파싱합니다.

    인자:
        date_value: 원본 날짜 값(YYYY-MM-DD/일자/숫자 등)
        req_year: 요청 연도
        req_month: 요청 월
        override_year: case가 제공하는 연도(없으면 요청 연도 사용)
        override_month: case가 제공하는 월(없으면 요청 월 사용)

    반환:
        요청 연/월과 일치하는 date 객체 또는 None.
        예: req_year=2025, req_month=9, date_value='2025-09-26' → date(2025, 9, 26)
    """
    target_year = override_year or req_year
    target_month = override_month or req_month

    parsed: date | None = None

    if isinstance(date_value, date):
        parsed = date_value
    elif isinstance(date_value, str):
        try:
            parsed = datetime.strptime(date_value, "%Y-%m-%d").date()
        except Exception:
            if date_value.isdigit():
                try:
                    parsed = date(req_year, req_month, int(date_value))
                except Exception:
                    parsed = None
    elif isinstance(date_value, int):
        try:
            parsed = date(req_year, req_month, int(date_value))
        except Exception:
            parsed = None

    if not parsed or parsed.year != target_year or parsed.month != target_month:
        return None

    return parsed


def _normalize_case_items(
    case_raw: Any,
    req_year: int,
    req_month: int,
    allowed_shift_map: Dict[str, str],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """case 목록을 현재 요청 연/월에 맞춰 정규화합니다.

    인자:
        case_raw: 원본 case 목록
        req_year: 요청 연도
        req_month: 요청 월
        allowed_shift_map: 허용되는 근무 코드 맵

    반환:
        (normalized, ignored) 튜플.
        normalized: {'date': date, 'shift': str} 리스트
        ignored: 무시된 항목 로그 리스트
    """
    normalized: list[dict[str, Any]] = []
    ignored: list[dict[str, Any]] = []
    # 대소문자 무시 매칭을 위한 lookup: upper → 원본 shift_id
    allowed_upper_to_original: dict[str, str] = {}
    if allowed_shift_map:
        for sid in allowed_shift_map.keys():
            allowed_upper_to_original[sid.upper()] = sid

    for item in case_raw or []:
        try:
            payload = item.dict() if hasattr(item, "dict") else dict(item)
        except Exception:
            ignored.append({"reason": "형식 오류", "item": str(item)})
            continue

        shift_raw = payload.get("shift")
        date_raw = payload.get("date")
        item_year = payload.get("year")
        item_month = payload.get("month")
        comment_raw = payload.get("comment") # 사유작성

        if not shift_raw:
            ignored.append({"reason": "shift 누락", "item": payload})
            continue

        shift_input = str(shift_raw).strip()
        if not shift_input:
            ignored.append({"reason": "허용되지 않는 shift", "item": payload})
            continue
        # 대소문자 무시로 allowed_shifts에서 원본 shift_id 매칭
        if allowed_upper_to_original:
            matched = allowed_upper_to_original.get(shift_input.upper())
            if not matched:
                ignored.append({"reason": "허용되지 않는 shift", "item": payload})
                continue
            shift = matched
        else:
            shift = shift_input

        parsed_date = _parse_case_date(
            date_value=date_raw,
            req_year=req_year,
            req_month=req_month,
            override_year=item_year,
            override_month=item_month,
        )

        if not parsed_date:
            ignored.append({"reason": "날짜 불일치/파싱 실패", "item": payload})
            continue

        normalized.append({
            "date": parsed_date, 
            "shift": shift,
            "comment": comment_raw # 사유작성
        })

    return normalized, ignored


def _persist_shift_results(
    db: Session,
    nurse_id: str,
    request_id: int,
    year: int,
    month: int,
    month_str: str,
    shift_map: Dict[str, Dict[int, Dict[str, Any]]],
    original_request: str = ''
) -> None:
    """shift_map을 nurse_shift_requests에 저장 (UPSERT 방식)"""
    detailed_id = _next_detailed_request_id(db, nurse_id, request_id, table="shift")
    rows = 0

    # shifts.id 매핑
    _nurse_row = db.query(Nurse).filter(Nurse.nurse_id == nurse_id).first()
    _grp_id = _nurse_row.group_id if _nurse_row else None
    _shift_id_to_table_id: Dict[str, int] = {}
    if _grp_id:
        _shift_q = db.query(Shift.shift_id, Shift.id).filter(Shift.group_id == _grp_id)
        _shift_id_to_table_id = {sid: tid for sid, tid in _shift_q.all()}

    print(f"shift_map 저장 시작 (request_id={request_id}): {shift_map}")

    for shift_code, by_day in (shift_map or {}).items():
        for day, info in (by_day or {}).items():
            if not isinstance(day, int) or not 1 <= day <= 31:
                continue
            
            print(f"[DEBUG-4] 저장 시도: {year}-{month:02d}-{day:02d} {shift_code} | "
                  f"request={info.get('request')!r}, comment={info.get('comment')!r}")

            score = float(info.get("score", 1.0))
            score = max(0.0, min(10.0, score))  # case 우선순위 반영 위해 범위 확대

            request_val = info.get("request", original_request or "AIDE 추천")
            partial_request = normalize_request_text(request_val)
            
            comment = info.get("comment", "") # 사유작성

            target_date = _ymd(year, month, day)

            existing = db.query(NurseShiftRequest).filter(
                NurseShiftRequest.nurse_id == nurse_id,
                NurseShiftRequest.request_id == request_id,
                NurseShiftRequest.shift_date == target_date
            ).first()

            if existing:
                existing.shift = shift_code
                existing.score = score
                existing.partial_request = partial_request
                existing.comment = comment # 사유작성
                existing.shifts_table_id = _shift_id_to_table_id.get(shift_code)
            else:
                db.add(NurseShiftRequest(
                    nurse_id=nurse_id,
                    request_id=request_id,
                    detailed_request_id=detailed_id,
                    shift_date=target_date,
                    shift=shift_code,
                    score=score,
                    partial_request=partial_request,
                    comment=comment, # 사유작성
                    shifts_table_id=_shift_id_to_table_id.get(shift_code),
                ))
                detailed_id += 1
            rows += 1

    db.flush()
    print(f"[shift 저장] {rows}건 flush 완료")


def _persist_pair_results(
    db: Session,
    nurse_id: str,
    request_id: int,
    month_str: str,
    pairs: List[Dict[str, float]],
) -> None:
    """pair 결과를 nurse_pair_requests 테이블에 저장합니다 (병합 방식).

    기존 pair 데이터를 유지하면서, 새로운 pair를 추가하거나 동일 target_id가
    이미 존재하면 업데이트합니다.

    인자:
        db: DB 세션
        nurse_id: 간호사 ID
        request_id: 상위 wanted_requests.request_id
        pairs: [{"id": "12", "weight": -1.5, "request": "..."}, ...]
    """
    # 기존 pair 데이터 조회
    existing_rows = db.query(NursePairRequest).filter(
        NursePairRequest.nurse_id == nurse_id,
        NursePairRequest.request_id == request_id,
        NursePairRequest.month == month_str,
    ).all()
    existing_map = {row.target_id: row for row in existing_rows}

    updated = 0
    added = 0
    next_detailed_id = max((row.detailed_request_id for row in existing_rows), default=0) + 1

    for item in pairs or []:
        target_id = item.get("id")
        weight = item.get("weight")
        request_text = item.get("request")
        if target_id is None or weight is None:
            continue

        target_id_str = str(target_id)

        if target_id_str in existing_map:
            # 동일 target_id가 이미 존재 → 업데이트
            existing_row = existing_map[target_id_str]
            existing_row.score = float(weight)
            existing_row.partial_request = normalize_request_text(request_text)
            updated += 1
        else:
            # 새로운 target_id → 추가
            db.add(NursePairRequest(
                nurse_id=nurse_id,
                request_id=request_id,
                month=month_str,
                detailed_request_id=next_detailed_id,
                target_id=target_id_str,
                score=float(weight),
                partial_request=normalize_request_text(request_text),
            ))
            next_detailed_id += 1
            added += 1

    db.commit()
    print(f"[pair 저장] 기존 유지={len(existing_map) - updated}건, 업데이트={updated}건, 신규={added}건 완료")


def _parse_shift_results(
    response: List[List[Dict[str, Any]]]
) -> Dict[str, Dict[int, Dict[str, Any]]]:
    """
    그래프 결과에서 shift_result를 {shift: {day: {score, request}}} 형태로 정리
    - 중복 체크를 day 단위로 변경 (같은 날짜에 다른 shift가 나오면 로그만 남기고 둘 다 유지 가능)
    - 같은 날짜 + 같은 shift만 중복으로 간주하고 하나만 남김
    """
    parsed: Dict[str, Dict[int, Dict[str, Any]]] = {}
    seen_per_day: Dict[int, Set[str]] = defaultdict(set)

    for sub in response:
        for entry in sub:
            shift_results = entry.get("shift_result", [])
            for sr in shift_results:
                record = sr.get("result", sr) if isinstance(sr, dict) else sr
                if not isinstance(record, dict) or "shift" not in record:
                    continue

                shift = str(record.get("shift", "")).strip()
                if not shift:
                    continue

                dates = record.get("date", [])
                scores = record.get("score", [])
                requests = record.get("request", [""]) * len(dates)
                comments = record.get("comment", [None] * len(dates))

                for i, day_str in enumerate(dates):
                    try:
                        day = int(day_str)
                        if not 1 <= day <= 31:
                            continue
                    except:
                        continue

                    if shift in seen_per_day.get(day, set()):
                        continue
                    seen_per_day[day].add(shift)

                    score = float(scores[i]) if i < len(scores) else 1.0
                    req = str(requests[i] if i < len(requests) else "").strip()
                    comment = comments[i] if i < len(comments) else None

                    parsed.setdefault(shift, {})[day] = {
                        "score": score,
                        "request": normalize_request_text(req),
                        "shift": shift,
                        "comment": comment or ""  # 사유 추가 (None이면 빈 문자열)
                    }

    return parsed


def _parse_preferences(response: List[List[Dict[str, Any]]], schema=None) -> List[Dict[str, Any]]:
    """preference_result를 [{'id': str, 'weight': float, 'request': str}] 형태로 변환"""
    parsed = []
    seen = set()
    valid_ids = {str(n['nurse_id']) for n in (schema or []) if 'nurse_id' in n} if schema else None

    for sub in response:
        for entry in sub:
            pref_list = entry.get("preference_result", [])
            for pr in pref_list if isinstance(pref_list, list) else []:
                _id = str(pr.get("id"))
                weight = pr.get("weight")
                req = pr.get("request")
                if _id == "None" or weight is None:
                    continue
                if valid_ids and _id not in valid_ids:
                    continue
                try:
                    w = float(weight)
                    key = (_id, w, req)
                    if key in seen:
                        continue
                    seen.add(key)
                    parsed.append({"id": _id, "weight": w, "request": normalize_request_text(req)})
                except:
                    pass
    return parsed


def _copy_existing_requests_to_new(
    db: Session,
    nurse_id: str,
    old_request_id: int,
    new_request_id: int,
    year: int,
    month: int,
    month_str: str,
    skip_shift_copy: bool = False,
) -> Tuple[int, int]:
    """기존 데이터를 새 request_id로 복사

    Args:
        db: DB 세션
        nurse_id: 간호사 ID
        old_request_id: 복사할 원본 request_id
        new_request_id: 복사 대상 request_id
        year, month, month_str: 대상 연월
        skip_shift_copy: True이면 shift 데이터 복사 스킵 (case가 전체 상태를 나타낼 때)

    Returns:
        (복사된 shift 건수, 복사된 pair 건수)
    """
    # 이번 달 시작일과 다음 달 1일 계산 (12월 넘김 처리 포함)
    start = date(year, month, 1)
    if month == 12:
        end = date(year + 1, 1, 1)
    else:
        end = date(year, month + 1, 1)

    shift_count = 0

    if not skip_shift_copy:
        detailed_id_shift = 1
        # shift 데이터 복사
        old_shift_rows = db.query(NurseShiftRequest).filter(
            NurseShiftRequest.nurse_id == nurse_id,
            NurseShiftRequest.request_id == old_request_id,
            NurseShiftRequest.shift_date >= start,
            NurseShiftRequest.shift_date < end,
        ).all()

        for old_row in old_shift_rows:
            db.merge(NurseShiftRequest(
                nurse_id=nurse_id,
                request_id=new_request_id,
                detailed_request_id=detailed_id_shift,
                shift_date=old_row.shift_date,
                shift=old_row.shift,
                score=old_row.score,
                partial_request=old_row.partial_request,
                comment=old_row.comment,
                shifts_table_id=old_row.shifts_table_id,
            ))
            shift_count += 1
            detailed_id_shift += 1
    else:
        print("[복사] shift 복사 스킵 (case가 전체 캘린더 상태)")

    # pair 데이터 복사 (항상)
    pair_count = 0
    detailed_id_pair = 1

    old_pair_rows = db.query(NursePairRequest).filter(
        NursePairRequest.nurse_id == nurse_id,
        NursePairRequest.request_id == old_request_id,
        NursePairRequest.month == month_str,
    ).all()

    for old_row in old_pair_rows:
        db.merge(NursePairRequest(
            nurse_id=nurse_id,
            request_id=new_request_id,
            month=month_str,
            detailed_request_id=detailed_id_pair,
            target_id=old_row.target_id,
            score=old_row.score,
            partial_request=old_row.partial_request or '기존 데이터에서 로드됨',
        ))
        pair_count += 1
        detailed_id_pair += 1

    # 실제 변경이 있었다면 commit
    if shift_count > 0 or pair_count > 0:
        db.commit()
        print(f"[복사 완료] shift: {shift_count}건, pair: {pair_count}건 "
              f"(old_request_id={old_request_id} → new_request_id={new_request_id})")
    else:
        print("[복사 스킵] 복사할 데이터 없음")

    return shift_count, pair_count


def _get_off_shift_ids(db: Session, group_id: str) -> list[str]:
    """
    근무 타입이 아닌 모든 shift_id 목록 반환 (휴무, 휴가 등 제한 대상)
    """
    return [
        row[0] for row in db.query(Shift.shift_id).filter(
            Shift.group_id == group_id,
            Shift.type != '근무'
        ).all()
    ]


def _count_existing_off_requests(
    db: Session,
    nurse_id: str,
    year: int,
    month: int,
    group_id: str,
) -> int:
    """
    해당 간호사의 해당 월에 **이미 저장된** 휴무/휴가 요청 개수 (draft + submitted 모두 포함)
    """
    
    month_str = f"{year}-{month:02d}"
    start_date = date(year, month, 1)
    end_date = date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)
    
    latest_request = (
        db.query(WantedRequest).filter(
            WantedRequest.nurse_id == nurse_id,
            WantedRequest.month == month_str,
        ).order_by(WantedRequest.request_id.desc()).first()
    )
    
    if not latest_request:
        return 0
    
    latest_request_id = latest_request.request_id

    off_shift_ids = _get_off_shift_ids(db, group_id)
    if not off_shift_ids:
        return 0

    count = db.query(NurseShiftRequest).filter(
        NurseShiftRequest.nurse_id == nurse_id,
        NurseShiftRequest.request_id == latest_request_id,
        NurseShiftRequest.shift_date >= start_date,
        NurseShiftRequest.shift_date < end_date,
        NurseShiftRequest.shift.in_(off_shift_ids),
    ).count()

    return count


def normalize_request_text(value: Any) -> str:
    """입력값을 정리해서 반환"""
    if not value:
        return '기존 데이터 업데이트'

    def clean(text: Any) -> str:
        if not text:
            return ''
        s = str(text).strip()
        lines = [line.strip() for line in s.splitlines() if line.strip()]
        return '\n'.join(lines)

    if isinstance(value, list):
        cleaned = [clean(v) for v in value if clean(v) and clean(v) != '기존 데이터에서 로드됨']
        return '\n'.join(cleaned) or '기존 데이터 업데이트'
    return clean(value) or '기존 데이터 업데이트'


def cleanup_previous_requests(db: Session, nurse_id: str, month_str: str, current_request_id: int):
    """이전 request_id의 shift/pair 데이터 삭제 (필요 시 사용)"""
    print(f"이전 요청 정리 시작: nurse_id={nurse_id}, month={month_str}, current={current_request_id}")
    
    deleted_shift = db.query(NurseShiftRequest).filter(
        NurseShiftRequest.nurse_id == nurse_id,
        NurseShiftRequest.request_id < current_request_id,
    ).delete()
    
    deleted_pair = db.query(NursePairRequest).filter(
        NursePairRequest.nurse_id == nurse_id,
        NursePairRequest.request_id < current_request_id,
        NursePairRequest.month == month_str,
    ).delete()
    
    if deleted_shift or deleted_pair:
        db.commit()
    print(f"정리 완료: shift {deleted_shift}건, pair {deleted_pair}건 삭제")


async def invoke_and_persist_wanted_service(
    req: WantedInvokeRequest,
    current_user: UserSchema,
    db: Session,
) -> Dict[str, Any]:
    """
    Wanted 그래프 실행 후 DB 저장 + 응답 반환
    주요 개선:
    - case 정규화를 함수 초반으로 이동 → NameError 방지
    - case가 없어도 안전하게 graph 호출
    - request가 '기존 데이터' 계열 복사 스킵
    - case가 충분히 많으면 전체 재작성으로 간주
    """
    nurse_id = current_user.nurse_id
    month_str = _yyyymm(req.year, req.month)
    group_id = current_user.group_id
    print(f"invoke_and_persist_wanted_service 시작: nurse={nurse_id}, {month_str}")

    # ========== WantedConfig 검증 (프론트에서 검증, 백엔드는 주석 처리) ==========

    # # 1. GLOBAL 설정 확인 - nurses 테이블로 이동됨
    # global_config = get_wanted_config(db, group_id, 'GLOBAL')

    # # 1-1. enable_aide 확인 (AIDE 기능 활성화 여부) - nurses 테이블에서 확인
    # nurse = db.query(Nurse).filter(Nurse.nurse_id == nurse_id).first()
    # if nurse and nurse.enable_aide == False:
    #     print(f"[검증 실패] AIDE 기능이 비활성화됨: nurse_id={nurse_id}")
    #     raise Exception("AIDE 기능이 현재 비활성화되어 있습니다.")

    # # 2. 재제출 여부 확인
    # existing_request = db.query(WantedRequest).filter(
    #     WantedRequest.nurse_id == nurse_id,
    #     WantedRequest.month == month_str
    # ).first()
    # is_resubmit = existing_request is not None

    # 허용 근무코드 조회 (show_in_preference=True)
    allowed_shifts_query = db.query(Shift).filter(
        Shift.group_id == group_id,
        Shift.show_in_preference == True
    ).all()
    allowed_shift_map = {row.shift_id: row.name for row in allowed_shifts_query}

    # ★★★ 핵심: case 정규화를 함수 초반으로 이동 ★★★
    normalized_case, ignored_case = _normalize_case_items(
        case_raw=req.case,
        req_year=req.year,
        req_month=req.month,
        allowed_shift_map=allowed_shift_map,
    )
    has_case = bool(normalized_case)
    
    print("[DEBUG-1] normalized_case 전체 내용 : ", normalized_case)
    for idx, item in enumerate(normalized_case):
        print(f"[DEBUG-1] case[{idx}]: date={item.get('date')}, shift={item.get('shift')}, "
              f"comment={item.get('comment')!r} (type={type(item.get('comment'))})")

    # # 3. NURSE_LIMIT 검증 (간호사별 월단위 요청 개수 제한) - 프론트에서 검증, nurses 테이블로 이동됨
    # nurse = db.query(Nurse).filter(Nurse.nurse_id == nurse_id).first()
    # if nurse and nurse.wanted_max_requests is not None:
    #     start_date = date(req.year, req.month, 1)
    #     if req.month == 12:
    #         end_date = date(req.year + 1, 1, 1)
    #     else:
    #         end_date = date(req.year, req.month + 1, 1)
    #
    #     nurse_current_count = db.query(NurseShiftRequest).filter(
    #         NurseShiftRequest.nurse_id == nurse_id,
    #         NurseShiftRequest.shift_date >= start_date,
    #         NurseShiftRequest.shift_date < end_date
    #     ).count()
    #
    #     additional_count = len(normalized_case) if has_case else 0
    #     total_count = nurse_current_count + additional_count
    #
    #     if total_count > nurse.wanted_max_requests:
    #         print(f"[검증 실패] NURSE_LIMIT 초과: nurse_id={nurse_id}, "
    #               f"현재={nurse_current_count}, 추가={additional_count}, "
    #               f"제한={nurse.wanted_max_requests}")
    #         raise Exception(
    #             f"간호사별 최대 요청 개수를 초과했습니다. "
    #             f"(현재: {nurse_current_count}개, 추가: {additional_count}개, 제한: {nurse.wanted_max_requests}개)"
    #         )
    #
    #     print(f"[검증 통과] NURSE_LIMIT: 현재={nurse_current_count}, 추가={additional_count}, "
    #           f"제한={nurse.wanted_max_requests}")

    # 4. DAILY_LIMIT 검증 (날짜별 그룹 전체 요청 개수 제한)
    # if has_case:
        # case에 포함된 날짜들 DAILY_LIMIT 확인
    # case_dates = {item["date"] for item in normalized_case}

        # 해당 월의 DAILY_LIMIT 설정 조회 (shift_type=None인 전체 날짜 제한)
    # daily_limit_configs = db.query(WantedConfig).filter(
    # WantedConfig.group_id == group_id,
    # WantedConfig.config_type == 'DAILY_LIMIT',
    # WantedConfig.target_date.in_(case_dates),
    # WantedConfig.shift_type.is_(None)
    # ).all()

    # daily_limit_map = {config.target_date: config.max_requests for config in daily_limit_configs}

    # if daily_limit_map:
            # 그룹 내 모든 간호사 ID 조회
    # group_nurse_ids = [n[0] for n in db.query(Nurse.nurse_id).filter(Nurse.group_id == group_id).all()]

            # 제한 초과 날짜 수집
    # exceeded_dates = []
    # for check_date in case_dates:
    # if check_date in daily_limit_map:
    # daily_limit = daily_limit_map[check_date]

                    # 해당 날짜의 그룹 전체 요청 개수
    # daily_current_count = db.query(NurseShiftRequest).filter(
    # NurseShiftRequest.nurse_id.in_(group_nurse_ids),
    # NurseShiftRequest.shift_date == check_date
    # ).count()

                    # 제한 초과 확인 (현재 요청도 카운트에 포함)
    # if daily_current_count >= daily_limit:
    # exceeded_dates.append({
    # "date": check_date.strftime('%Y-%m-%d'),
    # "current": daily_current_count,
    # "limit": daily_limit
    # })

    # if exceeded_dates:
    # exceeded_info = ", ".join([
    # f"{d['date']}({d['current']}/{d['limit']})"
    # for d in exceeded_dates
    # ])
    # print(f"[검증 실패] DAILY_LIMIT 초과: {exceeded_info}")
    # raise Exception(
    # f"다음 날짜의 일자별 최대 요청 개수를 초과했습니다: {exceeded_info}"
    # )

    # print(f"[검증 통과] DAILY_LIMIT: case 날짜 {len(case_dates)}개 확인 완료")

    # ========== WantedConfig 검증 종료 ==========

    # graph에 전달할 case 포맷 (isoformat 처리)
    graph_case_payload = [
        {"date": item["date"].isoformat(), "shift": item["shift"]}
        for item in normalized_case
    ] if has_case else []

    # request 텍스트 정제
    cleaned_request = normalize_request_text(req.request)
    is_dummy_request = not cleaned_request.strip() or '기존 데이터' in cleaned_request

    # 전체 재작성 판단
    days_in_month = 31  # 대략적
    is_full_reset = (
        has_case
        and len(normalized_case) >= 10
        and not is_dummy_request
    )

    print(f"has_case={has_case}, case 건수={len(normalized_case)}, "
          f"is_dummy_request={is_dummy_request}, is_full_reset={is_full_reset}")

    # 그래프 실행 여부
    response = [[], []]
    shift_parsed = {}
    pref_parsed = []

    if not is_dummy_request:
        try:
            raw_response = await graph_service.invoke(
                request=req.request,
                schema=req.schema,
                case=graph_case_payload,
                year=req.year,
                month=req.month,
                allowed_shifts=", ".join(allowed_shift_map.keys()),
                allowed_shift_map=allowed_shift_map
            )
            if isinstance(raw_response, str):
                raw_response = json.loads(raw_response)
            response = raw_response if isinstance(raw_response, list) and len(raw_response) == 2 else [[], []]

            shift_parsed = _parse_shift_results(response)
            pref_parsed = _parse_preferences(response, req.schema)
        except Exception as e:
            print(f"그래프 호출 실패: {e}")
            traceback.print_exc()

    # 새 request_id 생성
    new_request_id = _persist_wanted_request(db, nurse_id, month_str, req.request)

    # 과거 데이터 복사 여부 결정
    copied_shift, copied_pair = 0, 0
    if not is_full_reset and not is_dummy_request:
        latest_wr = db.query(WantedRequest).filter(
            WantedRequest.nurse_id == nurse_id,
            WantedRequest.month == month_str,
        ).order_by(WantedRequest.created_at.desc()).offset(1).first()

        if latest_wr:
            print(f"과거 데이터 복사 시도: old={latest_wr.request_id} → new={new_request_id}")
            # has_case=True이면 case가 현재 캘린더 전체 상태 → shift 복사 불필요 (pair만 복사)
            # has_case=False이면 AIDE 텍스트만 있는 경우 → 기존 shift 데이터 유지 필요
            copied_shift, copied_pair = _copy_existing_requests_to_new(
                db, nurse_id, latest_wr.request_id, new_request_id,
                req.year, req.month, month_str, skip_shift_copy=has_case
            )
    else:
        print("전체 재작성 또는 더미 request → 과거 데이터 복사 스킵")

    # shift_map 구성
    shift_map: Dict[str, Dict[int, Dict[str, Any]]] = {}
    original_request_text = cleaned_request or "AIDE 추천"

    # 1. case 강제 우선 적용 (score 높게)
    if has_case:
        for item in normalized_case:
            day = item["date"].day
            shift = item["shift"]
            comment = item.get("comment", "") # 사유작성
            
            shift_map.setdefault(shift, {})[day] = {
                "score": 10.0,
                "request": "사용자 직접 입력 (최우선)",
                "comment": comment, # 사유작성
                "shift": shift
            }
    
    print("[DEBUG-2] case 처리 완료 후 shift_map : ", shift_map)
    for shift_code, days in shift_map.items():
        for day, info in days.items():
            print(f"[DEBUG-2]   {shift_code} {day}일 → comment={info.get('comment')!r}")

    # 2. AIDE 결과 병합 (case 없는 날만 반영)
    for shift_id, days_dict in shift_parsed.items():
        for day_str, info in days_dict.items():
            try:
                day_int = int(day_str)
                if not 1 <= day_int <= 31:
                    continue
            except:
                continue

            score = float(info.get("score", 1.0))
            req_text = info.get("request", original_request_text)

            # case로 지정된 조합이면 스킵
            if any(d == day_int and s == shift_id for d, s in
                   ((item["date"].day, item["shift"]) for item in normalized_case)):
                print(f"AIDE 스킵 (case 우선): {shift_id} {day_int}일")
                continue

            current = shift_map.get(shift_id, {}).get(day_int)
            if current is None or score > current["score"]:
                shift_map.setdefault(shift_id, {})[day_int] = {
                    "score": score,
                    "request": req_text,
                    "comment": info.get("comment", ""),  # AIDE에서 파싱한 사유 사용
                    "shift": shift_id
                }
    print("[DEBUG-3] AIDE 병합 완료 후 shift_map : ", shift_map)

    # 주휴 필터링
    weekly_off_days = _compute_weekly_off_days(
        db, nurse_id, current_user.group_id, req.year, req.month
    )
    shift_map = _drop_weekly_off_from_shift_map(shift_map, weekly_off_days)

    # case가 하나라도 있으면 → case 없는 날짜의 기존 shift 기록 삭제
    if has_case:
        case_days = {item["date"].day for item in normalized_case}
        start_date = date(req.year, req.month, 1)
        end_date = date(req.year, req.month + 1, 1) if req.month < 12 else date(req.year + 1, 1, 1)

        deleted = db.query(NurseShiftRequest).filter(
            NurseShiftRequest.nurse_id == nurse_id,
            NurseShiftRequest.request_id == new_request_id,
            NurseShiftRequest.shift_date >= start_date,
            NurseShiftRequest.shift_date < end_date,
            ~NurseShiftRequest.shift_date.in_(
                [date(req.year, req.month, d) for d in case_days]
            )
        ).delete(synchronize_session=False)

        if deleted:
            print(f"[case 제한] case에 없는 날짜의 기존 shift {deleted}건 삭제")

    # 주휴일 DB 레코드 삭제
    if weekly_off_days:
        deleted_weekly = db.query(NurseShiftRequest).filter(
            NurseShiftRequest.nurse_id == nurse_id,
            NurseShiftRequest.request_id == new_request_id,
            NurseShiftRequest.shift_date.in_(
                [date(req.year, req.month, d) for d in weekly_off_days]
            )
        ).delete(synchronize_session=False)
        if deleted_weekly:
            print(f"[weekly_off] DB에서 {deleted_weekly}건 제거")
    
    nurse = db.query(Nurse).filter(Nurse.nurse_id == nurse_id).first()
    max_requests = nurse.wanted_max_requests if nurse else None
    
    excluded_off_dates = []
    off_shift_ids_set = set(_get_off_shift_ids(db, group_id))
    
    if max_requests is not None:
        # 복사된 데이터에서 휴무/휴가 날짜 조회 (new_request_id 기준)
        start_date = date(req.year, req.month, 1)
        end_date = date(req.year + 1, 1, 1) if req.month == 12 else date(req.year, req.month + 1, 1)
        copied_off_rows = db.query(NurseShiftRequest.shift_date).filter(
            NurseShiftRequest.nurse_id == nurse_id,
            NurseShiftRequest.request_id == new_request_id,
            NurseShiftRequest.shift_date >= start_date,
            NurseShiftRequest.shift_date < end_date,
            NurseShiftRequest.shift.in_(list(off_shift_ids_set)),
        ).all()
        copied_off_days = {row[0].day for row in copied_off_rows}

        # shift_map에서 휴무/휴가 날짜
        candidate_off_days = set()
        for shift_id, days_map in shift_map.items():
            if shift_id in off_shift_ids_set:
                candidate_off_days.update(days_map.keys())

        # 합집합으로 실제 총 개수 계산 (이중 카운트 방지)
        all_off_days = copied_off_days | candidate_off_days
        potential_total = len(all_off_days)

        print(f"[AIDE OFF LIMIT] 복사된 휴무/휴가={len(copied_off_days)}개, "
              f"shift_map 휴무/휴가={len(candidate_off_days)}개, "
              f"합집합 총={potential_total}개, 제한={max_requests}")

        if potential_total > max_requests:
            allowable_total = max_requests
            # 복사된 off 날짜는 우선 유지, shift_map 항목 중 초과분 제거
            # 복사 전용 off 날짜 수 (shift_map과 겹치지 않는 것)
            copy_only_off = copied_off_days - candidate_off_days
            remaining_slots = max(0, allowable_total - len(copy_only_off))

            kept_days = set()
            excluded_items = []  # (day, shift_id) 쌍으로 추적

            for shift_id, days_map in list(shift_map.items()):
                if shift_id not in off_shift_ids_set:
                    continue

                for day in sorted(days_map.keys()):
                    if len(kept_days) < remaining_slots:
                        kept_days.add(day)
                    else:
                        excluded_items.append((day, shift_id))
                        del days_map[day]
                if not days_map:
                    del shift_map[shift_id]

            excluded_off_dates = sorted(set(d for d, _ in excluded_items))

            # 이미 copy로 저장된 excluded (날짜, shift) 쌍도 DB에서 삭제
            if excluded_items:
                total_deleted = 0
                for exc_day, exc_shift in excluded_items:
                    exc_date = date(req.year, req.month, exc_day)
                    cnt = db.query(NurseShiftRequest).filter(
                        NurseShiftRequest.nurse_id == nurse_id,
                        NurseShiftRequest.request_id == new_request_id,
                        NurseShiftRequest.shift_date == exc_date,
                        NurseShiftRequest.shift == exc_shift,
                    ).delete(synchronize_session='fetch')
                    total_deleted += cnt
                if total_deleted:
                    print(f"[AIDE OFF LIMIT] copy된 초과 off {total_deleted}건 DB에서 삭제: {excluded_items}")

            # 프론트에 전달할 명확한 메시지
            excluded_detail = ", ".join(
                f"{req.month}/{d}일({s})" for d, s in excluded_items
            )
            warn_msg = (
                f"휴무/휴가 요청 가능 수({max_requests}개) 초과로 "
                f"다음 요청이 제외되었습니다: {excluded_detail}"
            )
            print(f"[AIDE OFF LIMIT PARTIAL] {warn_msg}")

        else:
            print(f"[AIDE OFF LIMIT OK] 복사={len(copied_off_days)}, "
                  f"추가={len(candidate_off_days)}, 합집합={potential_total}, 제한={max_requests}")
        
    # shift 저장
    print(f"[AIDE PERSIST] shift_map 저장 직전: {list(shift_map.keys())}")
    if shift_map:
        _persist_shift_results(
            db, nurse_id, new_request_id, req.year, req.month, month_str,
            shift_map, original_request_text
        )

    # pair 저장
    if pref_parsed:
        # enable_nurse_pair_preference 확인 (시크릿 기능 활성화 여부) - 프론트에서 검증
        # nurse = db.query(Nurse).filter(Nurse.nurse_id == nurse_id).first()
        # if nurse and nurse.enable_nurse_pair_preference == False:
        #     print(f"[경고] 선호 간호사 기능이 비활성화됨: pair 데이터는 저장되지 않습니다. nurse_id={nurse_id}")
        # else:
        _persist_pair_results(db, nurse_id, new_request_id, month_str, pref_parsed)

    # 최종 커밋
    try:
        db.commit()
        print(f"최종 commit 완료 - request_id={new_request_id}")
    except Exception as e:
        db.rollback()
        print(f"최종 commit 실패: {e}")
        traceback.print_exc()
        raise
    
    # 제외된 항목을 shift_parsed에서도 제거 (프론트가 저장된 것처럼 표시하지 않도록)
    if excluded_off_dates:
        for exc_day, exc_shift in excluded_items:
            if exc_shift in shift_parsed and exc_day in shift_parsed[exc_shift]:
                del shift_parsed[exc_shift][exc_day]
                if not shift_parsed[exc_shift]:
                    del shift_parsed[exc_shift]

    result = {
        "shift": shift_parsed,
        "preference": pref_parsed,
        "warning": None
    }

    if excluded_off_dates:
        result["warning"] = {
            "message": warn_msg,
            "excluded_items": [
                {"day": d, "shift": s, "date": f"{req.year}-{req.month:02d}-{d:02d}"}
                for d, s in excluded_items
            ],
            "existing_off_count": len(copied_off_days),
            "excluded_count": len(excluded_items),
            "limit": max_requests
        }

    # return {
    #     "shift": shift_parsed,
    #     "preference": pref_parsed
    # }
    return result


def request_wanted_shifts_service(
    req: WantedDeadlineRequest,
    current_user,
    db: Session,
    override_group_id: str | None = None,
):
    """
    Wanted 작성 요청 생성 서비스 함수.

    관리자(ADM)의 경우 `override_group_id`로 대상 그룹을 지정합니다.
    """
    if not current_user or not (getattr(current_user, 'is_head_nurse', False) or getattr(current_user, 'is_master_admin', False)):
        raise Exception("Permission denied")

    target_group_id = override_group_id or current_user.group_id
    if not target_group_id:
        raise Exception("대상 그룹이 없습니다.")

    if db.query(Wanted).filter(
        Wanted.group_id == target_group_id,
        Wanted.year == req.year,
        Wanted.month == req.month
    ).first():
        raise Exception("이미 해당 월의 요청이 존재합니다.")

    # 마감일은 요청에서 전달된 값 사용 (향후 default_deadline_days 자동 계산 기능 추가 예정)
    new_wanted = Wanted(
        group_id=target_group_id,
        year=req.year,
        month=req.month,
        exp_date=req.exp_date,
        status='requested'
    )
    db.add(new_wanted)
    db.commit()
    db.refresh(new_wanted)

    # 푸시 알림
    group_row = db.query(Group).filter(Group.group_id == target_group_id).first()
    office_id = group_row.office_id if group_row and group_row.office_id else current_user.office_id
    nurse_ids = [row.nurse_id for row in db.query(Nurse.nurse_id).filter(Nurse.group_id == target_group_id).all()]

    send_wanted_request_push(
        year=req.year,
        month=req.month,
        recipients=nurse_ids,
        office_code=office_id,
        sender_emp_seq_no=current_user.nurse_id,
        sender_member_id=current_user.account_id,
        deadline=new_wanted.exp_date,
    )
    
    display_exp_date = "마감일 없음" if new_wanted.exp_date is None else new_wanted.exp_date.strftime("%Y-%m-%d")
    
    return {
        "message": "Wanted 작성 요청이 성공적으로 생성되었습니다.",
        "current_exp_date": new_wanted.exp_date.isoformat() if new_wanted.exp_date else None,
        "display_exp_date": display_exp_date
    }


def close_expired_wanted(db: Session) -> int:
    """
    exp_date가 지난 Wanted 요청의 status를 'closed'로 일괄 변경합니다.

    인자:
        db: DB 세션 객체

    반환:
        int: 'requested' 상태에서 'closed'로 변경된 Wanted 건수.
             예를 들어 만료된 건이 3건이면 3을 반환합니다.
    """
    now = datetime.now()
    updated_count = db.query(Wanted).filter(
        Wanted.status == 'requested',
        Wanted.exp_date.isnot(None),
        Wanted.exp_date < now,
    ).update({'status': 'closed'}, synchronize_session=False)

    if updated_count > 0:
        db.commit()
        print(f"Wanted 자동 마감 완료: {updated_count}건")
    else:
        print("Wanted 자동 마감: 만료된 항목 없음")

    return updated_count


# WantedConfig 관련 서비스 함수
def get_wanted_config(db: Session, group_id: str, filters: dict = None):
    """일자별 원티드 제한 설정 조회 (DAILY_LIMIT 전용, 활성 설정만)

    - GLOBAL, NURSE_LIMIT 설정은 nurses 테이블로 이동됨

    인자:
        db: DB 세션
        group_id: 그룹 ID
        filters: 추가 필터 조건
            - year, month: 해당 월의 설정 조회
            - target_date: 특정 일자 조회
            - shift_type: 근무 타입 필터

    반환:
        List[WantedConfig]
    """
    query = db.query(WantedConfig).filter(
        WantedConfig.group_id == group_id,
    )

    # 추가 필터 적용
    if filters:
        if 'year' in filters and 'month' in filters:
            # 해당 월의 범위로 필터링
            year = filters['year']
            month = filters['month']
            start_date = date(year, month, 1)
            if month == 12:
                end_date = date(year + 1, 1, 1)
            else:
                end_date = date(year, month + 1, 1)
            query = query.filter(
                WantedConfig.target_date >= start_date,
                WantedConfig.target_date < end_date
            )
        if 'target_date' in filters:
            target_date = filters['target_date']
            if isinstance(target_date, str):
                target_date = datetime.strptime(target_date, '%Y-%m-%d').date()
            query = query.filter(WantedConfig.target_date == target_date)
        if 'shift_type' in filters:
            query = query.filter(WantedConfig.shift_type == filters['shift_type'])

    return query.all()


def upsert_wanted_config(db: Session, group_id: str, configs_data: list[dict], year: int | None = None, month: int | None = None):
    """일자별 원티드 제한 설정 생성/수정 (DAILY_LIMIT 전용)

    - 프론트에서 보내온 날짜만 유지, 요청에 없는 기존 DB 데이터는 삭제
    - max_requests가 None이면 해당 날짜 row 삭제
    - configs_data가 빈 리스트면 해당 월 설정 전체 삭제

    인자:
        db: DB 세션
        group_id: 그룹 ID
        configs_data: 설정 데이터 리스트
        year: 대상 연도 (configs_data가 빈 리스트일 때 필수)
        month: 대상 월 (configs_data가 빈 리스트일 때 필수)

    반환:
        List[WantedConfig]
    """
    if not configs_data:
        if not year or not month:
            return []
        start_date = date(year, month, 1)
        end_date = date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)
        deleted_count = db.query(WantedConfig).filter(
            WantedConfig.group_id == group_id,
            WantedConfig.target_date >= start_date,
            WantedConfig.target_date < end_date,
        ).delete(synchronize_session=False)
        if deleted_count:
            print(f"[DAILY_LIMIT] 빈 요청으로 기존 설정 {deleted_count}건 전체 삭제")
        db.commit()
        return []

    # year/month 추출 (첫 번째 항목 기준)
    first = configs_data[0]
    first_date_str = first.get('target_date')
    if isinstance(first_date_str, str):
        first_date = datetime.strptime(first_date_str, '%Y-%m-%d').date()
    else:
        first_date = first_date_str
    req_year = first.get('year', first_date.year)
    req_month = first.get('month', first_date.month)

    start_date = date(req_year, req_month, 1)
    if req_month == 12:
        end_date = date(req_year + 1, 1, 1)
    else:
        end_date = date(req_year, req_month + 1, 1)

    # 프론트에서 보내온 target_date 목록 수집
    incoming_dates = set()
    for config_data in configs_data:
        td = config_data.get('target_date')
        if isinstance(td, str):
            incoming_dates.add(datetime.strptime(td, '%Y-%m-%d').date())
        elif td:
            incoming_dates.add(td)

    # 요청에 없는 기존 DB row 삭제
    deleted_count = db.query(WantedConfig).filter(
        WantedConfig.group_id == group_id,
        WantedConfig.target_date >= start_date,
        WantedConfig.target_date < end_date,
        ~WantedConfig.target_date.in_(incoming_dates) if incoming_dates else True
    ).delete(synchronize_session=False)
    if deleted_count:
        print(f"[DAILY_LIMIT] 요청에 없는 기존 설정 {deleted_count}건 삭제")

    # upsert 처리
    results = []
    for config_data in configs_data:
        target_date_str = config_data.get('target_date')
        if not target_date_str:
            raise ValueError("각 설정에 target_date가 필수입니다.")

        if isinstance(target_date_str, str):
            target_date = datetime.strptime(target_date_str, '%Y-%m-%d').date()
        else:
            target_date = target_date_str

        year = config_data.get('year', target_date.year)
        month = config_data.get('month', target_date.month)
        shift_type = config_data.get('shift_type')
        max_requests = config_data.get('max_requests')

        existing = db.query(WantedConfig).filter(
            WantedConfig.group_id == group_id,
            WantedConfig.target_date == target_date,
            WantedConfig.shift_type == shift_type
        ).first()

        # max_requests가 None이면 해당 row 삭제
        if max_requests is None:
            if existing:
                db.delete(existing)
                print(f"[DAILY_LIMIT] 설정 삭제: target_date={target_date}, shift_type={shift_type}")
            continue

        if existing:
            existing.max_requests = max_requests
            existing.year = year
            existing.month = month
            results.append(existing)
        else:
            config = WantedConfig(
                group_id=group_id,
                year=year,
                month=month,
                target_date=target_date,
                shift_type=shift_type,
                max_requests=max_requests,
            )
            db.add(config)
            results.append(config)

    db.commit()
    for r in results:
        db.refresh(r)
    print(f"[DAILY_LIMIT] 설정 저장 완료: group_id={group_id}, count={len(results)}, 삭제={deleted_count}")
    return results



def delete_wanted_config(db: Session, group_id: str, filters: dict = None) -> int:
    """일자별 원티드 제한 설정 삭제 (DAILY_LIMIT 전용)

    - GLOBAL, NURSE_LIMIT 설정은 nurses 테이블로 이동됨

    인자:
        db: DB 세션
        group_id: 그룹 ID
        filters: 삭제 조건
            - target_date: 특정 일자 (YYYY-MM-DD)
            - shift_type: 근무 타입

    반환:
        삭제된 레코드 수
    """
    query = db.query(WantedConfig).filter(
        WantedConfig.group_id == group_id
    )

    # 추가 필터 적용
    if filters:
        if 'target_date' in filters:
            target_date_str = filters['target_date']
            if isinstance(target_date_str, str):
                target_date = datetime.strptime(target_date_str, '%Y-%m-%d').date()
            else:
                target_date = target_date_str
            query = query.filter(WantedConfig.target_date == target_date)
        if 'shift_type' in filters:
            query = query.filter(WantedConfig.shift_type == filters['shift_type'])

    deleted = query.delete()
    db.commit()
    print(f"[DAILY_LIMIT] 설정 삭제: group_id={group_id}, deleted={deleted}")
    return deleted


def delete_wanted_config_by_month(db: Session, group_id: str, year: int, month: int) -> int:
    """해당 그룹/월의 일자별 원티드 제한 설정 전체 삭제

    인자:
        db: DB 세션
        group_id: 그룹 ID
        year: 연도
        month: 월

    반환:
        삭제된 레코드 수
    """
    start_date = date(year, month, 1)
    if month == 12:
        end_date = date(year + 1, 1, 1)
    else:
        end_date = date(year, month + 1, 1)

    deleted = db.query(WantedConfig).filter(
        WantedConfig.group_id == group_id,
        WantedConfig.target_date >= start_date,
        WantedConfig.target_date < end_date
    ).delete(synchronize_session=False)
    db.commit()
    print(f"[DAILY_LIMIT] 일괄 삭제: group_id={group_id}, {year}-{month:02d}, deleted={deleted}")
    return deleted


def validate_wanted_limits(db: Session, nurse_id: str, group_id: str, year: int, month: int, shift_date: date) -> dict:
    """원티드 요청 제한 검증

    - NURSE_LIMIT: nurses 테이블의 wanted_max_requests 컬럼에서 조회
    - DAILY_LIMIT: wanted_config 테이블에서 조회 (config_type 컬럼 제거됨)

    인자:
        db: DB 세션
        nurse_id: 간호사 ID
        group_id: 그룹 ID
        year: 연도
        month: 월
        shift_date: 근무 날짜

    반환:
        {
            "valid": bool,
            "errors": List[str],
            "nurse_limit": int | None,
            "nurse_current": int,
            "daily_limit": int | None,
            "daily_current": int
        }
    """

    errors = []

    # 1. 간호사별 제한 확인 (nurses 테이블에서 조회)
    nurse = db.query(Nurse).filter(Nurse.nurse_id == nurse_id).first()
    nurse_limit = nurse.wanted_max_requests if nurse else None

    # 현재 간호사의 요청 개수
    start_date = date(year, month, 1)
    if month == 12:
        end_date = date(year + 1, 1, 1)
    else:
        end_date = date(year, month + 1, 1)

    nurse_current = db.query(NurseShiftRequest).filter(
        NurseShiftRequest.nurse_id == nurse_id,
        NurseShiftRequest.shift_date >= start_date,
        NurseShiftRequest.shift_date < end_date
    ).count()

    if nurse_limit is not None and nurse_current >= nurse_limit:
        errors.append(f"간호사별 최대 요청 개수({nurse_limit}개)를 초과했습니다.")

    # 2. 일자별 제한 확인 (wanted_config 테이블, DAILY_LIMIT 전용, 활성 설정만)
    daily_config = db.query(WantedConfig).filter(
        WantedConfig.group_id == group_id,
        WantedConfig.target_date == shift_date,
        WantedConfig.shift_type.is_(None),
    ).first()

    daily_limit = daily_config.max_requests if daily_config else None

    # 해당 날짜의 그룹 전체 요청 개수
    nurse_ids = [n[0] for n in db.query(Nurse.nurse_id).filter(Nurse.group_id == group_id).all()]
    daily_current = db.query(NurseShiftRequest).filter(
        NurseShiftRequest.nurse_id.in_(nurse_ids),
        NurseShiftRequest.shift_date == shift_date
    ).count()

    if daily_limit is not None and daily_current >= daily_limit:
        errors.append(f"{shift_date.strftime('%Y-%m-%d')} 일자별 최대 요청 개수({daily_limit}개)를 초과했습니다.")

    return {
        "valid": len(errors) == 0,
        "errors": errors,
        "nurse_limit": nurse_limit,
        "nurse_current": nurse_current,
        "daily_limit": daily_limit,
        "daily_current": daily_current
    }


# 간호사 원티드 개수 제한 초과분인 경우에 대한 조회 및 무조건적인 삭제 기능 서비스 함수
def get_over_limit_nurses(
    db: Session,
    year: int,
    month: int,
    group_id: str | None = None
) -> List[dict]:
    """
    wanted_max_requests 보다 많은 휴무/휴가 요청을 사전에 제출한 간호사 목록 반환
    """
    month_str = f"{year}-{month:02d}"
    start_date = date(year, month, 1)
    end_date = date(year + 1, 1, 1) if month == 12 else date(year, month +1, 1)
    
    off_shift_ids = _get_off_shift_ids(db, group_id) if group_id else []
    
    query = (
        db.query(
            Nurse.nurse_id,
            Nurse.name,
            Nurse.wanted_max_requests,
            func.count(NurseShiftRequest.detailed_request_id).label("current_count")
        )
        .join(WantedRequest, WantedRequest.nurse_id == Nurse.nurse_id)
        .join(NurseShiftRequest,
              (NurseShiftRequest.nurse_id == WantedRequest.nurse_id) &
              (NurseShiftRequest.request_id == WantedRequest.request_id))
        .filter(
            WantedRequest.month == month_str,
            WantedRequest.is_submitted == True,
            NurseShiftRequest.shift_date >= start_date,
            NurseShiftRequest.shift_date < end_date,
            NurseShiftRequest.shift.in_(off_shift_ids),
            Nurse.wanted_max_requests.isnot(None)
        )
    )
    
    if group_id:
        query = query.filter(Nurse.group_id == group_id)
        
    results = (
        query.group_by(
            Nurse.nurse_id, Nurse.name, Nurse.wanted_max_requests
        )
        .having(func.count(NurseShiftRequest.detailed_request_id) > Nurse.wanted_max_requests)
        .all()
    )
    
    return [
        {
            "nurse_id": r.nurse_id,
            "name": r.name,
            "wanted_max_requests": r.wanted_max_requests,
            "current_count": r.current_count,
            "excess_count": r.current_count - r.wanted_max_requests,
            "month": month_str
        }
        for r in results
    ]


def delete_excess_off_requests(
    db: Session,
    nurse_id: str,
    year: int,
    month: int,
    force_delete: bool = False
) -> dict:
    """
    해당 간호사의 초과된 휴무/휴가 요청삭제
    - score 기준 낮은 순으로 삭제
    """
    nurse = db.query(Nurse).filter(Nurse.nurse_id == nurse_id).first()
    if not nurse or nurse.wanted_max_requests is None:
        return {"deleted" : 0, "message" : "제한값이 설정되지 않았습니다."}
    
    month_str = f"{year}-{month:02d}"
    off_shift_ids = _get_off_shift_ids(db, nurse.group_id)
    
    latest_request = (
        db.query(WantedRequest).filter(
            WantedRequest.nurse_id == nurse_id,
            WantedRequest.month == month_str,
            WantedRequest.is_submitted == True
        )
        .order_by(WantedRequest.request_id.desc())
        .first()
    )
    
    if not latest_request:
        return {"deleted" : 0, "message" : "제출된 요청이 없습니다."}
    
    current_count = db.query(NurseShiftRequest).filter(
        NurseShiftRequest.nurse_id == nurse_id,
        NurseShiftRequest.request_id == latest_request.request_id,
        NurseShiftRequest.shift.in_(off_shift_ids)
    ).count()
    
    if current_count <= nurse.wanted_max_requests:
        return {"deleted" : 0, "message" : "초과된 요청이 없습니다."}
    
    excess = current_count - nurse.wanted_max_requests
    
    to_delete = (
        db.query(NurseShiftRequest).filter(
            NurseShiftRequest.nurse_id == nurse_id,
            NurseShiftRequest.request_id == latest_request.request_id,
            NurseShiftRequest.shift.in_(off_shift_ids)
        )
        .order_by(NurseShiftRequest.shift_date.desc())
        .limit(excess)
        .all()
    )
    
    deleted_count = 0
    deleted_dates = []
    
    for row in to_delete:
        deleted_dates.append({
            "date": row.shift_date.strftime("%Y-%m-%d"),
            "shift": row.shift,
            "score": float(row.score)
        })
        db.delete(row)
        deleted_count += 1
        
    db.commit()
    
    return {
        "deleted": deleted_count,
        "excess": excess,
        "remaining": nurse.wanted_max_requests,
        "deleted_items": deleted_dates,
        "request_id": latest_request.request_id
    }


# Fixed Wanted (확정 원티드) 서비스
def get_wanted_adjustment_service(
    db: Session,
    group_id: str,
    year: int,
    month: int,
) -> AdjustmentResponse:
    """
    원티드 조정판 데이터 조회 서비스
    - 기존 근무표 만들기와 동일한 구성
    - 각 간호사의 원티드 제출한 코드 내역들의 한달치 총 합 실시간 노출

    인자:
        db: DB 세션
        group_id: 그룹 ID
        year: 연도
        month: 월

    반환:
        AdjustmentResponse: 간호사별 원티드 + 월간 집계 + 확정 원티드 존재 여부
    """
    month_str = _yyyymm(year, month)
    start_date = date(year, month, 1)
    end_date = date(year, month + 1, 1) if month < 12 else date(year + 1, 1, 1)

    # 기존 FixedWantedEntry 조회 (단일 테이블)
    fixed_entries_exist = db.query(FixedWantedEntry).filter(
        FixedWantedEntry.group_id == group_id,
        FixedWantedEntry.year == year,
        FixedWantedEntry.month == month,
    ).first() is not None

    # 그룹 내 간호사 목록
    nurses = db.query(Nurse).filter(
        Nurse.group_id == group_id,
        Nurse.active == 1
    ).order_by(Nurse.sequence).all()

    nurse_data_list: List[AdjustmentNurse] = []

    for nurse in nurses:
        entries: List[FixedWantedEntryResponse] = []
        monthly_summary: Dict[str, int] = defaultdict(int)

        if fixed_entries_exist:
            # FixedWantedEntry에서 조회 (단일 테이블)
            fixed_entries = db.query(FixedWantedEntry).filter(
                FixedWantedEntry.group_id == group_id,
                FixedWantedEntry.year == year,
                FixedWantedEntry.month == month,
                FixedWantedEntry.nurse_id == nurse.nurse_id,
            ).all()

            for fe in fixed_entries:
                entries.append(FixedWantedEntryResponse(
                    id=fe.id,
                    group_id=fe.group_id,
                    year=fe.year,
                    month=fe.month,
                    nurse_id=fe.nurse_id,
                    shift_date=fe.shift_date,
                    shift_id=fe.shift_id,
                    shifts_table_id=fe.shifts_table_id,
                    is_applied=fe.is_applied,
                    source_type=fe.source_type,
                    original_shift_id=fe.original_shift_id,
                    reason=fe.reason,
                    head_nurse_memo=fe.head_nurse_memo,
                    created_by=fe.created_by,
                ))
                if fe.is_applied:
                    monthly_summary[fe.shift_id] += 1
        else:
            # NurseShiftRequest에서 조회 (최종 request_id 기준 제출 여부 확인)
            latest_wr = db.query(WantedRequest).filter(
                WantedRequest.nurse_id == nurse.nurse_id,
                WantedRequest.month == month_str,
            ).order_by(WantedRequest.request_id.desc()).first()

            if latest_wr and latest_wr.is_submitted:
                shift_requests = db.query(NurseShiftRequest).filter(
                    NurseShiftRequest.nurse_id == nurse.nurse_id,
                    NurseShiftRequest.request_id == latest_wr.request_id,
                    NurseShiftRequest.shift_date >= start_date,
                    NurseShiftRequest.shift_date < end_date,
                ).all()

                for idx, sr in enumerate(shift_requests):
                    entries.append(FixedWantedEntryResponse(
                        id=idx,  # 임시 ID (아직 FixedWantedEntry에 저장 전)
                        group_id=group_id,
                        year=year,
                        month=month,
                        nurse_id=sr.nurse_id,
                        shift_date=sr.shift_date,
                        shift_id=sr.shift,
                        shifts_table_id=sr.shifts_table_id,
                        is_applied=True,
                        source_type='original',
                        original_shift_id=None,
                        reason=sr.comment if sr.comment else None,
                        head_nurse_memo=None,
                        created_by=None,
                    ))
                    monthly_summary[sr.shift] += 1

        # 주휴 일자 계산 및 entries에 추가
        weekly_off_enabled = getattr(nurse, "weekly_off_enabled", False) or False
        weekly_off_days: List[int] = []
        if weekly_off_enabled:
            weekly_off_days = sorted(_compute_weekly_off_days(
                db, nurse.nurse_id, group_id, year, month
            ))
            # 주휴 일자를 entries에 추가 (source_type: "weekly_off")
            if weekly_off_days:
                for day in weekly_off_days:
                    weekly_off_date = date(year, month, day)
                    entries.append(FixedWantedEntryResponse(
                        id=-day,  # 음수 ID로 주휴 구분 (실제 DB ID가 아님)
                        group_id=group_id,
                        year=year,
                        month=month,
                        nurse_id=nurse.nurse_id,
                        shift_date=weekly_off_date,
                        shift_id="주",
                        is_applied=True,  # 주휴는 항상 적용됨 (토글 불가)
                        source_type="weekly_off",
                        original_shift_id=None,
                        reason=None,
                        head_nurse_memo=None,
                        created_by=None,
                    ))
                monthly_summary["주"] = len(weekly_off_days)

        nurse_data_list.append(AdjustmentNurse(
            nurse_id=nurse.nurse_id,
            name=nurse.name,
            entries=entries,
            monthly_summary=dict(monthly_summary),
        ))

    return AdjustmentResponse(
        nurses=nurse_data_list,
        has_fixed_wanted=fixed_entries_exist,
    )


def save_fixed_wanted_service(
    db: Session,
    group_id: str,
    nurse_id: str,
    req: FixedWantedCreate,
) -> List[FixedWantedEntry]:
    """
    확정 원티드 저장 서비스 (단일 테이블 구조)
    - 기존 데이터가 있으면 삭제 후 재생성
    - 없으면 새로 생성
    - source_type / original_shift_id를 원본 NurseShiftRequest와 비교하여 자동 감지
    """
    month_str = _yyyymm(req.year, req.month)
    start_date = date(req.year, req.month, 1)
    end_date = date(req.year, req.month + 1, 1) if req.month < 12 else date(req.year + 1, 1, 1)

    # ── 원본 NurseShiftRequest 맵 구축 ──
    nurses_in_group = db.query(Nurse.nurse_id).filter(
        Nurse.group_id == group_id,
        Nurse.active == 1,
    ).all()
    nurse_ids = [n.nurse_id for n in nurses_in_group]

    original_map: Dict[Tuple[str, str], str] = {}
    for nid in nurse_ids:
        latest_wr = db.query(WantedRequest).filter(
            WantedRequest.nurse_id == nid,
            WantedRequest.month == month_str,
        ).order_by(WantedRequest.request_id.desc()).first()

        if latest_wr and latest_wr.is_submitted:
            shift_requests = db.query(NurseShiftRequest).filter(
                NurseShiftRequest.nurse_id == nid,
                NurseShiftRequest.request_id == latest_wr.request_id,
                NurseShiftRequest.shift_date >= start_date,
                NurseShiftRequest.shift_date < end_date,
            ).all()
            for sr in shift_requests:
                original_map[(nid, sr.shift_date.isoformat())] = sr.shift

    print(f"원본 맵 구축 완료: {len(original_map)}건")

    # ── shifts.id 매핑 (shift_id → shifts.id) ──
    group_row = db.query(Group).filter(Group.group_id == group_id).first()
    _office_id = group_row.office_id if group_row else None
    _shift_q = db.query(Shift).filter(Shift.group_id == group_id)
    if _office_id:
        _shift_q = _shift_q.filter(Shift.office_id == _office_id)
    _shift_id_to_table_id = {s.shift_id: s.id for s in _shift_q.all()}

    # ── 기존 FixedWantedEntry 삭제 ──
    deleted_count = db.query(FixedWantedEntry).filter(
        FixedWantedEntry.group_id == group_id,
        FixedWantedEntry.year == req.year,
        FixedWantedEntry.month == req.month,
    ).delete()

    if deleted_count > 0:
        print(f"기존 FixedWantedEntry 삭제: {deleted_count}건")

    # ── 간호사별 주휴 일자 계산 (주휴일은 저장에서 제외) ──
    nurse_weekly_off_map: Dict[str, Set[int]] = {}
    for nid in nurse_ids:
        nurse_row = db.query(Nurse).filter(Nurse.nurse_id == nid).first()
        if nurse_row and getattr(nurse_row, "weekly_off_enabled", False):
            weekly_off_days = _compute_weekly_off_days(db, nid, group_id, req.year, req.month)
            if weekly_off_days:
                nurse_weekly_off_map[nid] = weekly_off_days

    # ── 새 엔트리 추가 ──
    new_entries: List[FixedWantedEntry] = []
    skipped_weekly_off_count = 0
    for entry in req.entries:
        if entry.source_type == "weekly_off":
            skipped_weekly_off_count += 1
            continue

        entry_day = entry.shift_date.day
        nurse_weekly_off_days = nurse_weekly_off_map.get(entry.nurse_id, set())
        if entry_day in nurse_weekly_off_days:
            skipped_weekly_off_count += 1
            continue

        key = (entry.nurse_id, entry.shift_date.isoformat())
        original_shift = original_map.get(key)

        if entry.source_type:
            resolved_source_type = entry.source_type
            resolved_original_shift_id = entry.original_shift_id
        elif original_shift is None:
            resolved_source_type = 'added'
            resolved_original_shift_id = None
        elif original_shift != entry.shift_id:
            resolved_source_type = 'modified'
            resolved_original_shift_id = original_shift
        else:
            resolved_source_type = 'original'
            resolved_original_shift_id = None

        new_entry = FixedWantedEntry(
            group_id=group_id,
            year=req.year,
            month=req.month,
            nurse_id=entry.nurse_id,
            shift_date=entry.shift_date,
            shift_id=entry.shift_id,
            shifts_table_id=_shift_id_to_table_id.get(entry.shift_id),
            is_applied=entry.is_applied,
            source_type=resolved_source_type,
            original_shift_id=resolved_original_shift_id,
            reason=entry.reason,
            head_nurse_memo=entry.head_nurse_memo,
            created_by=nurse_id,
        )
        db.add(new_entry)
        new_entries.append(new_entry)

    db.commit()

    for entry in new_entries:
        db.refresh(entry)

    print(f"FixedWantedEntry 저장 완료: group={group_id}, {req.year}-{req.month}, entries={len(new_entries)}건 (주휴일 제외: {skipped_weekly_off_count}건)")

    return new_entries


def toggle_fixed_wanted_entry_service(
    db: Session,
    entry_id: int,
) -> FixedWantedEntry:
    """
    확정 원티드 개별 항목 적용/미적용 토글 서비스
    """
    entry = db.query(FixedWantedEntry).filter(
        FixedWantedEntry.id == entry_id
    ).first()

    if not entry:
        raise ValueError(f"Entry not found: {entry_id}")

    entry.is_applied = not entry.is_applied
    db.commit()
    db.refresh(entry)

    print(f"FixedWantedEntry 토글: id={entry_id}, is_applied={entry.is_applied}")
    return entry


def reset_fixed_wanted_service(
    db: Session,
    group_id: str,
    year: int,
    month: int,
) -> AdjustmentResponse:
    """
    확정 원티드 재설정 서비스
    - FixedWantedEntry 해당 group/year/month 데이터 전체 삭제
    - 원본 WantedRequest + NurseShiftRequest 기반 데이터를 AdjustmentResponse로 반환
    """
    deleted_count = db.query(FixedWantedEntry).filter(
        FixedWantedEntry.group_id == group_id,
        FixedWantedEntry.year == year,
        FixedWantedEntry.month == month,
    ).delete()
    db.commit()

    print(f"FixedWantedEntry 재설정: group={group_id}, {year}-{month:02d}, 삭제={deleted_count}건")

    return get_wanted_adjustment_service(db, group_id, year, month)


def get_fixed_wanted_for_roster_service(
    db: Session,
    group_id: str,
    year: int,
    month: int,
) -> List[Dict[str, Any]]:
    """
    근무표 생성용 확정 원티드 조회 서비스 (단일 테이블 구조)
    - is_applied=True인 항목만 반환
    """
    entries = db.query(FixedWantedEntry).filter(
        FixedWantedEntry.group_id == group_id,
        FixedWantedEntry.year == year,
        FixedWantedEntry.month == month,
        FixedWantedEntry.is_applied == True,
    ).all()

    result = []
    for entry in entries:
        result.append({
            "nurse_id": entry.nurse_id,
            "shift_date": entry.shift_date,
            "shift": entry.shift_id,
            "shifts_table_id": entry.shifts_table_id,
            "score": 10.0,
            "reason": entry.reason,
        })

    print(f"FixedWantedEntry 조회 (근무표 생성용): group={group_id}, {year}-{month}, entries={len(result)}건")
    return result


def get_fixed_wanted_entries_service(
    db: Session,
    group_id: str,
    year: int,
    month: int,
) -> List[FixedWantedEntry]:
    """
    확정 원티드 엔트리 목록 조회 서비스 (단일 테이블 구조)
    """
    return db.query(FixedWantedEntry).filter(
        FixedWantedEntry.group_id == group_id,
        FixedWantedEntry.year == year,
        FixedWantedEntry.month == month,
    ).all()


def get_shift_requests_service(
    db: Session,
    target_group_id: str,
    year: int,
    month: int,
    shift_type: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """해당 년/월 그룹 내 전체 간호사의 원티드 제출 여부 + shift 내역 반환"""
    nurse_ids = [
        n[0] for n in
        db.query(Nurse.nurse_id).filter(Nurse.group_id == target_group_id).all()
    ]
    if not nurse_ids:
        return []

    month_str = f"{year:04d}-{month:02d}"
    first_day = date(year, month, 1)
    last_day = date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)

    # 제출 현황 일괄 조회 (is_submitted=True인 최신 request_id만)
    submitted_requests = (
        db.query(WantedRequest)
        .filter(
            WantedRequest.nurse_id.in_(nurse_ids),
            WantedRequest.month == month_str,
            WantedRequest.is_submitted == True,
        )
        .all()
    )
    submitted_map = {wr.nurse_id: wr for wr in submitted_requests}

    # 제출된 간호사의 (nurse_id, request_id) 쌍으로 shift 필터
    submitted_pairs = [
        (wr.nurse_id, wr.request_id) for wr in submitted_requests
    ]

    shift_map: Dict[str, list] = {}
    if submitted_pairs:
        shift_query = db.query(NurseShiftRequest).filter(
            NurseShiftRequest.shift_date >= first_day,
            NurseShiftRequest.shift_date < last_day,
            or_(
                *(
                    and_(
                        NurseShiftRequest.nurse_id == nid,
                        NurseShiftRequest.request_id == rid,
                    )
                    for nid, rid in submitted_pairs
                )
            ),
        )
        if shift_type:
            shift_query = shift_query.filter(NurseShiftRequest.shift == shift_type)
        shift_rows = shift_query.all()

        for row in shift_rows:
            shift_map.setdefault(row.nurse_id, []).append({
                "shift_date": row.shift_date.isoformat(),
                "shift": row.shift,
                "shifts_table_id": row.shifts_table_id,
                "score": float(row.score),
                "comment": row.comment,
            })

    results = []
    for nurse_id in nurse_ids:
        wr = submitted_map.get(nurse_id)
        results.append({
            "nurse_id": nurse_id,
            "is_submitted": wr is not None,
            "submitted_at": wr.submitted_at.isoformat() if wr and wr.submitted_at else None,
            "shifts": shift_map.get(nurse_id, []),
        })

    return results
