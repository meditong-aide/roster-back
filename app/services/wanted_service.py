"""
Wanted(근무 희망 요청) 관련 서비스 로직 모듈
- DB 쿼리, 데이터 가공 등 라우터에서 분리
- 모든 함수는 한글 docstring, 한글 print/logging, PEP8 스타일 적용
"""
import json
import traceback
from collections import defaultdict
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Set, Tuple

from dateutil.relativedelta import relativedelta
from sqlalchemy import func
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
    """wanted_requests 레코드를 저���하고 request_id 를 반환합니다."""
    request_id = _next_request_id(db, nurse_id, month_str)
    
    # def clean_text(text: Any) -> str:
    #     if not text:
    #         return ''
    #     s = str(text).strip()
    #     lines = [line.strip() for line in s.splitlines() if line.strip()]
    #     return '\n'.join(lines)

    # if isinstance(request, list):
    #     cleaned = [clean_text(item) for item in request if clean_text(item) and clean_text(item) != '기존 데이터에서 로드됨']
    #     request_text = '\n'.join(cleaned)
    # else:
    #     request_text = clean_text(request)

    # print(f"[DEBUG] 저장할 request_text (repr): {repr(request_text)}")
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
        nurse_id: ���호사 ID
        group_id: 그룹 ID (주휴 설정 조회용)
        year: 대상 연도
        month: 대상 월

    반환:
        주휴 요일에 해당����는 day 집합(1~31). 예: {3, 10, 17, 24}
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
    allowed_shifts = set(allowed_shift_map.keys()) if allowed_shift_map else None

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

        shift = str(shift_raw).strip().upper()
        if not shift or (allowed_shifts and shift not in allowed_shifts):
            ignored.append({"reason": "허용되지 않는 shift", "item": payload})
            continue

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
    """pair 결과를 nurse_pair_requests 테이블에 저장합니다.

    인자:
        db: DB 세션
        nurse_id: 간호사 ID
        request_id: 상위 wanted_requests.request_id
        pairs: [{"id": "12", "weight": -1.5, "request": "..."}, ...]
    
    Notes:
        detailed_request_id는 기존 데이터 다음 순번부터 시작
    """
    # 중복 누적 방지: 동일 nurse_id + request_id 레코드를 먼저 삭제
    db.query(NursePairRequest).filter(
        NursePairRequest.nurse_id == nurse_id,
        NursePairRequest.request_id == request_id,
    ).delete()

    detailed_id = 1
    for item in pairs or []:
        target_id = item.get("id")
        weight = item.get("weight")
        request_text = item.get("request")
        if target_id is None or weight is None:
            continue

        db.add(NursePairRequest(
            nurse_id=nurse_id,
            request_id=request_id,
            detailed_request_id=detailed_id,
            target_id=str(target_id),
            score=float(weight),
            partial_request=normalize_request_text(request_text),
        ))
        detailed_id += 1

    db.commit()
    print(f"[pair 저장] {detailed_id-1}건 완료")


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

                shift = str(record.get("shift", "")).strip().upper()
                if not shift:
                    continue

                dates = record.get("date", [])
                scores = record.get("score", [])
                requests = record.get("request", [""]) * len(dates)

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

                    parsed.setdefault(shift, {})[day] = {
                        "score": score,
                        "request": normalize_request_text(req),
                        "shift": shift
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
    case_exclude: set | None = None,
) -> Tuple[int, int]:
    """기존 데이터를 새 request_id로 복사 (새로 입력된 case는 제외)

    Args:
        db: DB 세션
        nurse_id: 간호사 ID
        old_request_id: 복사할 원본 request_id
        new_request_id: 복사 대상 request_id
        year, month, month_str: 대상 연월
        case_exclude: {(day, shift), ...} 형태의 set. 이 조합은 복사에서 제외 (새로 저장할 것이므로)

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
    detailed_id_shift = 1

    # shift 데이터 복사
    old_shift_rows = db.query(NurseShiftRequest).filter(
        NurseShiftRequest.nurse_id == nurse_id,
        NurseShiftRequest.request_id == old_request_id,
        NurseShiftRequest.shift_date >= start,
        NurseShiftRequest.shift_date < end,
    ).all()

    for old_row in old_shift_rows:
        day = old_row.shift_date.day
        shift = old_row.shift

        # case_exclude가 있으면 해당 (day, shift) 조합은 복사에서 제외 (새로 저장할 것이므로)
        if case_exclude is not None and (day, shift) in case_exclude:
            print(f"[복사 제외] {day}일 {shift} → 새로 입력된 case")
            continue

        db.merge(NurseShiftRequest(
            nurse_id=nurse_id,
            request_id=new_request_id,
            detailed_request_id=detailed_id_shift,
            shift_date=old_row.shift_date,
            shift=shift,
            score=old_row.score,
            partial_request=old_row.partial_request,
            comment=old_row.comment # 사유작성
        ))
        shift_count += 1
        detailed_id_shift += 1

    # pair 데이터 복사
    pair_count = 0
    detailed_id_pair = 1

    old_pair_rows = db.query(NursePairRequest).filter(
        NursePairRequest.nurse_id == nurse_id,
        NursePairRequest.request_id == old_request_id,
    ).all()

    for old_row in old_pair_rows:
        db.merge(NursePairRequest(
            nurse_id=nurse_id,
            request_id=new_request_id,
            detailed_request_id=detailed_id_pair,
            target_id=old_row.target_id,
            score=old_row.score,
            partial_request=old_row.partial_request or '기존 데이터에��� 로드됨',
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
    - request가 '기존 데이터' 계열이면 복사 스킵
    - case가 충분히 많으면 전체 재작성으로 간주
    """
    nurse_id = current_user.nurse_id
    month_str = _yyyymm(req.year, req.month)
    print(f"invoke_and_persist_wanted_service 시작: nurse={nurse_id}, {month_str}")

    # 허용 근무코드 조회 (show_in_preference=True)
    allowed_shifts_query = db.query(Shift).filter(
        Shift.group_id == current_user.group_id,
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
                case=graph_case_payload,                    # ← 이제 안전하게 전달
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
            print(f"그래프 호출 ��패: {e}")
            traceback.print_exc()

    # 새 request_id 생성
    new_request_id = _persist_wanted_request(db, nurse_id, month_str, req.request)

    # 과거 데이터 복사 여부 결정
    # has_case=True면 부분 업데이트이므로 기존 데이터 복사 필요
    copied_shift, copied_pair = 0, 0
    if not is_full_reset and (not is_dummy_request or has_case):
        latest_wr = db.query(WantedRequest).filter(
            WantedRequest.nurse_id == nurse_id,
            WantedRequest.month == month_str,
        ).order_by(WantedRequest.created_at.desc()).offset(1).first()

        if latest_wr:
            print(f"과거 데이터 복사 시도: old={latest_wr.request_id} → new={new_request_id}")
            # 새로 입력된 case는 복사에서 제외 (새로 저장할 것이므로)
            case_exclude = {(item["date"].day, item["shift"]) for item in normalized_case} if has_case else None
            copied_shift, copied_pair = _copy_existing_requests_to_new(
                db, nurse_id, latest_wr.request_id, new_request_id,
                req.year, req.month, month_str, case_exclude=case_exclude
            )
    else:
        print("전체 재작성(is_full_reset) → 과거 데이터 복사 스킵")

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
                print(f"AIDE ���킵 (case 우선): {shift_id} {day_int}일")
                continue

            current = shift_map.get(shift_id, {}).get(day_int)
            if current is None or score > current["score"]:
                shift_map.setdefault(shift_id, {})[day_int] = {
                    "score": score,
                    "request": req_text,
                    "comment": req_text, # AIDE 결과는 partial_request와 동일하게
                    "shift": shift_id
                }
    print("[DEBUG-3] AIDE 병합 완료 후 shift_map : ", shift_map)

    # 주휴 필터링
    weekly_off_days = _compute_weekly_off_days(
        db, nurse_id, current_user.group_id, req.year, req.month
    )
    shift_map = _drop_weekly_off_from_shift_map(shift_map, weekly_off_days)

    # 전체 재작성(is_full_reset)일 때만 case 없는 날짜의 기존 shift 기록 삭제
    # 부분 업데이트(is_dummy_request && has_case)일 때는 기존 데이터 유지
    if is_full_reset and has_case:
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
            print(f"[전체 재작성] case에 없는 날짜의 기존 shift {deleted}건 삭제")

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

    # shift 저장
    if shift_map:
        _persist_shift_results(
            db, nurse_id, new_request_id, req.year, req.month, month_str,
            shift_map, original_request_text
        )

    # pair 저장
    if pref_parsed:
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

    return {
        "shift": shift_parsed,
        "preference": pref_parsed
    }


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
        deadline=req.exp_date,
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
    query = db.query(Wanted).filter(
        Wanted.status == 'requested',
        Wanted.exp_date.isnot(None),
        Wanted.exp_date < now,
    )

    updated_count = 0
    for wanted in query:
        wanted.status = 'closed'
        updated_count += 1

    if updated_count > 0:
        db.commit()
        print(f"Wanted 자동 마감 완료: {updated_count}건")
    return updated_count


# ───────────────────────────── Fixed Wanted (확정 원티드) 서비스 ─────────────────────────────

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
                        is_applied=True,
                        source_type='original',
                        original_shift_id=None,
                        reason=sr.comment if sr.comment else None,
                        head_nurse_memo=None,
                        created_by=None,
                    ))
                    monthly_summary[sr.shift] += 1

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

    인자:
        db: DB 세션
        group_id: 그룹 ID
        nurse_id: 생성자 간호사 ID
        req: FixedWantedCreate 요청

    반환:
        List[FixedWantedEntry]: 생성된 엔트리 목록
    """
    month_str = _yyyymm(req.year, req.month)
    start_date = date(req.year, req.month, 1)
    end_date = date(req.year, req.month + 1, 1) if req.month < 12 else date(req.year + 1, 1, 1)

    # ── 원본 NurseShiftRequest 맵 구축: {(nurse_id, shift_date_iso): shift_code} ──
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

    # ── 기존 FixedWantedEntry 삭제 ──
    deleted_count = db.query(FixedWantedEntry).filter(
        FixedWantedEntry.group_id == group_id,
        FixedWantedEntry.year == req.year,
        FixedWantedEntry.month == req.month,
    ).delete()

    if deleted_count > 0:
        print(f"기존 FixedWantedEntry 삭제: {deleted_count}건")

    # ── 새 엔트리 추가 (source_type / original_shift_id 자동 감지) ──
    new_entries: List[FixedWantedEntry] = []
    for entry in req.entries:
        key = (entry.nurse_id, entry.shift_date.isoformat())
        original_shift = original_map.get(key)

        # source_type 자동 감지 (프론트가 명시적으로 보낸 경우 그 값 우선)
        if entry.source_type:
            resolved_source_type = entry.source_type
            resolved_original_shift_id = entry.original_shift_id
        elif original_shift is None:
            # 원본에 없는 날짜/간호사 → 수간호사가 추가
            resolved_source_type = 'added'
            resolved_original_shift_id = None
        elif original_shift != entry.shift_id:
            # 원본과 근무코드가 다름 → 수간호사가 변경
            resolved_source_type = 'modified'
            resolved_original_shift_id = original_shift
        else:
            # 원본과 동일 → 원본 그대로
            resolved_source_type = 'original'
            resolved_original_shift_id = None

        new_entry = FixedWantedEntry(
            group_id=group_id,
            year=req.year,
            month=req.month,
            nurse_id=entry.nurse_id,
            shift_date=entry.shift_date,
            shift_id=entry.shift_id,
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

    # ID 확보를 위해 refresh
    for entry in new_entries:
        db.refresh(entry)

    print(f"FixedWantedEntry 저장 완료: group={group_id}, {req.year}-{req.month}, entries={len(new_entries)}건")

    return new_entries


def toggle_fixed_wanted_entry_service(
    db: Session,
    entry_id: int,
) -> FixedWantedEntry:
    """
    확정 원티드 개별 항목 적용/미적용 토글 서비스

    인자:
        db: DB 세션
        entry_id: FixedWantedEntry ID

    반환:
        FixedWantedEntry: 토글된 엔트리 객체

    예외:
        ValueError: 엔트리를 찾을 수 없는 경우
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

    인자:
        db: DB 세션
        group_id: 그룹 ID
        year: 연도
        month: 월

    반환:
        AdjustmentResponse: 원본 원티드 데이터 기반 조정판 응답 (has_fixed_wanted=False)
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
    - NurseShiftRequest와 호환되는 형식으로 반환

    인자:
        db: DB 세션
        group_id: 그룹 ID
        year: 연도
        month: 월

    반환:
        List[Dict]: 확정된 원티드 목록 (근무표 생성에 사용할 형식)
        [{"nurse_id": str, "shift_date": date, "shift": str, "score": float, "reason": str}, ...]
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
            "score": 10.0,  # 확정 원티드는 최고 우선순위
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

    인자:
        db: DB 세션
        group_id: 그룹 ID
        year: 연도
        month: 월

    반환:
        FixedWantedEntry 목록
    """
    return db.query(FixedWantedEntry).filter(
        FixedWantedEntry.group_id == group_id,
        FixedWantedEntry.year == year,
        FixedWantedEntry.month == month,
    ).all()
