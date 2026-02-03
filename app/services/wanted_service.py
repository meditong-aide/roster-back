"""
Wanted(근무 희망 요청) 관련 서비스 로직 모듈
- DB 쿼리, 데이터 가공 등 라우터에서 분리
- 모든 함수는 한글 docstring, 한글 print/logging, PEP8 스타일 적용
"""
import json
import traceback
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Set, Tuple

from dateutil.relativedelta import relativedelta
from sqlalchemy import func
from sqlalchemy.orm import Session

from db.models import (
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
from schemas.roster_schema import WantedDeadlineRequest, WantedInvokeRequest
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

                shift = str(record.get("shift", "")).strip().upper()
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
            print(f"그래프 호출 실패: {e}")
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
                    "comment": info.get("comment", ""),  # AIDE에서 파싱한 사유 사용
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

    # if db.query(Wanted).filter(
    #     Wanted.group_id == target_group_id,
    #     Wanted.year == req.year,
    #     Wanted.month == req.month
    # ).first():
    #     raise Exception("이미 해당 월의 요청이 존재합니다.")
    existing = db.query(Wanted).filter(
        Wanted.group_id == target_group_id,
        Wanted.year == req.year,
        Wanted.month == req.month
    ).first()
    if existing:
        existing.exp_date = req.exp_date
        db.commit()
        db.refresh(existing)
        close_expired_wanted(db)
        db.refresh(existing)
        # 마감 연장 푸시 알림
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
        display_exp_date = "마감일 없음" if existing.exp_date is None else existing.exp_date.strftime("%Y-%m-%d")
        return {
            "message": "마감일이 성공적으로 변경되었습니다.",
            "current_exp_date": existing.exp_date.isoformat() if existing.exp_date else None,
            "display_exp_date": display_exp_date
        }

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
    추가 사항:
    Wanted 테이블의 status 값을 exp_date와 현재 시간 비교해서 유기적으로 처리
    1. exp_date > now 인 경우 status = 'requested' 처리
    2. exp_date <= now 인 경우 status = 'closed' 처리
    3. exp_date is Null 인 경우 status = 'requested' 처리 (무기한)
    반환:
        상태가 변경된 Wanted 레코드 수
    """
    # now = datetime.now()
    utc_now = datetime.now(timezone.utc).replace(tzinfo=None)
    now = utc_now + timedelta(hours=9)
    updated_count = 0
    
    # query = db.query(Wanted).filter(
    #     Wanted.status == 'requested',
    #     Wanted.exp_date.isnot(None),
    #     Wanted.exp_date < now,
    # )

    # updated_count = 0
    # for wanted in query:
    #     wanted.status = 'closed'
    #     updated_count += 1

    # if updated_count > 0:
    #     db.commit()
    #     print(f"Wanted 자동 마감 완료: {updated_count}건")
    # return updated_count
    
    wanted_list = db.query(Wanted).all()
    
    for wanted in wanted_list:
        old_status = wanted.status
        new_status = None
        
        if wanted.exp_date is None:
            new_status = 'requested'
        elif wanted.exp_date > now:
            new_status = 'requested'
        else:
            new_status = 'closed'
        
        if new_status != old_status:
            wanted.status = new_status
            updated_count += 1
            print(f"[STATUS UPDATE] {wanted.year}-{wanted.month} | {wanted.group_id} : {old_status} > {new_status} | (exp_date={wanted.exp_date.isoformat() if wanted.exp_date else 'None'})")
    
    if updated_count > 0:
        db.commit()
        print(f"[STATUS UPDATE] Wanted 테이블 status 업데이트 완료")
    else:
        print(f"[STATUS UPDATE] Wanted 테이블 status 변경 내역 없음")
    return updated_count
