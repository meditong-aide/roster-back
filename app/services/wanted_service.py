"""
Wanted(근무 희망 요청) 관련 서비스 로직 모듈
- DB 쿼리, 데이터 가공 등 라우터에서 분리
- 모든 함수는 한글 docstring, 한글 print/logging, PEP8 스타일 적용
"""
from sqlalchemy.orm import Session
from sqlalchemy import func
from db.models import Wanted, Nurse, ShiftPreference, Shift, Group
from schemas.roster_schema import WantedInvokeRequest, WantedDeadlineRequest
from schemas.auth_schema import User as UserSchema
from datetime import datetime, date, timedelta
from typing import Dict, Any, List, Tuple
from db.models import WantedRequest, NurseShiftRequest, NursePairRequest
from services.graph_service import graph_service
from dateutil.relativedelta import relativedelta
from utils.utils import send_wanted_request_push
import traceback
from collections import defaultdict

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
    request_text = ''.join(request)
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
                print(f"잘못된 day 값 무시됨: {day}")
                continue

            score = float(info.get("score", 1.0))
            score = max(0.0, min(5.0, score))  # 범위 제한 재확인

            request_val = info.get("request", original_request or "AIDE 추천")
            partial_request = normalize_request_text(request_val)

            target_date = _ymd(year, month, day)

            # 이미 존재하는 레코드 → UPDATE
            existing = db.query(NurseShiftRequest).filter(
                NurseShiftRequest.nurse_id == nurse_id,
                NurseShiftRequest.request_id == request_id,
                NurseShiftRequest.shift_date == target_date
            ).first()

            if existing:
                print(f"기존 레코드 업데이트: {shift_code} {day}일")
                existing.shift = shift_code
                existing.score = score
                existing.partial_request = partial_request
            else:
                db.add(NurseShiftRequest(
                    nurse_id=nurse_id,
                    request_id=request_id,
                    detailed_request_id=detailed_id,
                    shift_date=target_date,
                    shift=shift_code,
                    score=score,
                    partial_request=partial_request,
                ))
                detailed_id += 1

            rows += 1

    try:
        db.flush()
        print(f"[FLUSH] shift {rows}건 flush 완료")
    except Exception as e:
        print(f"[ERROR] shift 저장 중 flush 실패: {str(e)}")
        raise


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
    deleted = db.query(NursePairRequest).filter(
        NursePairRequest.nurse_id == nurse_id,
        NursePairRequest.request_id == request_id,
    ).delete()
    if deleted:
        print(f"[pair] 기존 레코드 {deleted}건 삭제 후 재삽입")

    detailed_id = 1
    rows = 0
    for item in pairs or []:
        try:
            target_id = item.get("id") if item.get("id") is not None else None
            weight = float(item.get("weight")) if item.get("weight") is not None else None
            request = item.get("request")
        except Exception as e:
            print(f'pair 데이터 파싱 오류: {e}')
            continue
        if target_id is None or weight is None:
            continue
        row = NursePairRequest(
            nurse_id=nurse_id,
            request_id=request_id,
            detailed_request_id=detailed_id,
            target_id=target_id,
            score=weight,
            partial_request=request,
        )
        db.add(row)
        rows += 1
        detailed_id += 1
    
    db.commit()
    print(f"nurse_pair_requests 저장 완료: 시작 detailed_request_id={1 if rows else 0}, 종료={detailed_id - 1 if rows else 0}, 저장 rows={rows}")


def _parse_shift_results(
    response: List[List[Dict[str, Any]]]
) -> Dict[str, Dict[int, Dict[str, Any]]]:
    """
    그래프 결과에서 shift_result를 {shift: {day: {score, request}}} 형태로 정리
    - 중복 체크를 day 단위로 변경 (같은 날짜에 다른 shift가 나오면 로그만 남기고 둘 다 유지 가능)
    - 같은 날짜 + 같은 shift만 중복으로 간주하고 하나만 남김
    """
    parsed: Dict[str, Dict[int, Dict[str, Any]]] = {}
    seen_per_day: Dict[int, Set[str]] = defaultdict(set)  # day → 이미 본 shift 집합

    for sub in response:
        if not isinstance(sub, list):
            continue

        for entry in sub:
            shift_results = entry.get("shift_result", [])
            if not isinstance(shift_results, list):
                continue

            for sr in shift_results:
                # result 키가 있으면 그 안으로, 없으면 그대로 사용
                record = sr.get("result", sr) if isinstance(sr, dict) else sr
                if not isinstance(record, dict) or "shift" not in record:
                    continue

                shift = str(record.get("shift", "")).strip().upper()
                if not shift:
                    continue

                dates = record.get("date", [])
                scores = record.get("score", [])
                requests = record.get("request", [""]) * len(dates)

                for i in range(min(len(dates), len(scores))):
                    try:
                        day = int(dates[i])
                        if not 1 <= day <= 31:
                            continue
                    except (ValueError, TypeError):
                        continue

                    score = float(scores[i]) if i < len(scores) else 1.0
                    req = str(requests[i] if i < len(requests) else "").strip()

                    # 중복 체크: 같은 날짜에 같은 shift가 이미 있으면 skip
                    if day in seen_per_day and shift in seen_per_day[day]:
                        print(f"[중복 제거] {day}일 {shift} 이미 처리됨 → skip")
                        continue

                    # 기록
                    seen_per_day[day].add(shift)

                    bucket = parsed.setdefault(shift, {})
                    # 같은 날짜에 이미 다른 shift가 있으면 경고 로그만 남김 (덮어쓰지 않음)
                    if day in bucket:
                        existing_shift = bucket[day].get("shift", "알수없음")
                        print(f"[경고] {day}일에 이미 {existing_shift} 존재 → 새 shift {shift} 추가됨")

                    bucket[day] = {
                        "score": score,
                        "request": normalize_request_text(req),
                        # shift 정보도 명시적으로 저장 (디버깅/나중 사용 편의)
                        "shift": shift
                    }
                    print("[DEBUG] raw shift_result:", response)

    # 디버깅용: 최종 파싱 결과 로그
    if parsed:
        print("[parse_shift_results 완료] 파싱된 shift 데이터:")
        for shift, days in parsed.items():
            print(f"  {shift}: {list(days.keys())}일")
    else:
        print("[parse_shift_results] 파싱 결과가 비어있음")

    return parsed



def _parse_preferences(response: List[List[Dict[str, Any]]], schema=None) -> List[Dict[str, Any]]:
    """preference_result를 [{'id': str, 'weight': float, 'request': str}] 형태로 변환"""
    parsed = []
    seen = set()
    valid_ids = {str(n['nurse_id']) for n in (schema or []) if 'nurse_id' in n} if schema else None

    for sub in response:
        if not isinstance(sub, list):
            continue
        for entry in sub:
            pref_list = entry.get("preference_result", [])
            for pr in pref_list if isinstance(pref_list, list) else []:
                _id = pr.get("id")
                weight = pr.get("weight")
                req = pr.get("request")
                if _id is None or weight is None:
                    continue
                _id_str = str(_id)
                if valid_ids and _id_str not in valid_ids:
                    continue
                try:
                    w = float(weight)
                    key = (_id_str, w, req)
                    if key in seen:
                        continue
                    seen.add(key)
                    parsed.append({"id": _id_str, "weight": w, "request": normalize_request_text(req)})
                except Exception as e:
                    print(f"preference 파싱 오류: {e}")
    return parsed


def _copy_existing_requests_to_new(
    db: Session,
    nurse_id: str,
    old_request_id: int,
    new_request_id: int,
    year: int,
    month: int,
    month_str: str,
    case_filter: set = None,
) -> Tuple[int, int]:
    """기존 데이터를 새 request_id로 복사 (필요 시 case_filter 적용)"""
    start = datetime.strptime(month_str, "%Y-%m")
    end = start + relativedelta(months=1)

    shift_count = 0
    detailed_id = 1

    old_shift_rows = db.query(NurseShiftRequest).filter(
        NurseShiftRequest.nurse_id == nurse_id,
        NurseShiftRequest.request_id == old_request_id,
        NurseShiftRequest.shift_date >= start,
        NurseShiftRequest.shift_date < end,
    ).all()

    for old_row in old_shift_rows:
        day = old_row.shift_date.day
        shift = old_row.shift
        if case_filter is not None and (day, shift) not in case_filter:
            continue

        new_row = NurseShiftRequest(
            nurse_id=nurse_id,
            request_id=new_request_id,
            detailed_request_id=detailed_id,
            shift_date=old_row.shift_date,
            shift=shift,
            score=old_row.score,
            partial_request=old_row.partial_request,
        )
        db.merge(new_row)
        shift_count += 1
        detailed_id += 1

    if shift_count > 0:
        db.commit()
        print(f"기존 shift 복사 완료: {shift_count}건")

    # pair 복사
    pair_count = 0
    detailed_id = 1
    old_pair_rows = db.query(NursePairRequest).filter(
        NursePairRequest.nurse_id == nurse_id,
        NursePairRequest.request_id == old_request_id,
    ).all()

    for old_row in old_pair_rows:
        new_row = NursePairRequest(
            nurse_id=nurse_id,
            request_id=new_request_id,
            detailed_request_id=detailed_id,
            target_id=old_row.target_id,
            score=old_row.score,
            partial_request=old_row.partial_request or '기존 데이터에서 로드됨',
        )
        db.merge(new_row)
        pair_count += 1
        detailed_id += 1

    if pair_count > 0:
        db.commit()
        print(f"기존 pair 복사 완료: {pair_count}건")

    return shift_count, pair_count


def normalize_request_text(value: Any) -> str:
    """입력값을 정리해서 반환"""
    if value is None or value == '':
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
    else:
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
    - case 있으면 복사 스킵 (case에 과거 데이터 포함됨)
    - 새 요청은 무조건 병합
    """
    print("그래프 실행 및 DB 저장 시작", req.__dict__)

    nurse_id = current_user.nurse_id
    print('request:', req.__dict__)
    month_str = _yyyymm(req.year, req.month)

    # 허용 근무코드
    allowed_shifts_query = db.query(Shift.shift_id, Shift.name).filter(
        Shift.group_id == current_user.group_id,
        Shift.show_in_preference == True
    ).all()
    
    allowed_shift_map = {row.shift_id: row.name for row in allowed_shifts_query}
    allowed_shifts_str = ", ".join(allowed_shift_map.keys()) if allowed_shift_map else "없음"
    print(f"허용 근무 코드: {allowed_shifts_str}")

    # # 중복 방지
    # recent_duplicate = db.query(WantedRequest).filter(
    #     WantedRequest.nurse_id == nurse_id,
    #     WantedRequest.month == month_str,
    #     WantedRequest.request == normalize_request_text(req.request),
    #     WantedRequest.created_at > datetime.now() - timedelta(seconds=30)
    # ).order_by(WantedRequest.created_at.desc()).first()
    
    # if recent_duplicate:
    #     print(f"중복 요청 감지 - 기존 request_id={recent_duplicate.request_id}")
    #     return {
    #         "message": "이미 저장 처리 중입니다.",
    #         "request_id": recent_duplicate.request_id,
    #         "shift": {},
    #         "preference": []
    #     }

    # case 확인 및 자동 로드
    has_case = (req.case is not None and len(req.case) > 0) or (''.join(req.request) is "" and ''.join(req.case) is "")
    print(f"has_case: {has_case}")

    # if not has_case:
    #     print("case 없음 → 과거 데이터 자동 로드 시도")
    #     latest_wr = db.query(WantedRequest).filter(
    #         WantedRequest.nurse_id == nurse_id,
    #         WantedRequest.month == month_str,
    #     ).order_by(WantedRequest.created_at.desc()).first()
        
    #     if latest_wr and latest_wr.request != normalize_request_text(req.request):
    #         print(f"과거 데이터 발견 (request_id={latest_wr.request_id}) → case 자동 생성")
    #         shift_rows = db.query(NurseShiftRequest).filter(
    #             NurseShiftRequest.nurse_id == nurse_id,
    #             NurseShiftRequest.request_id == latest_wr.request_id
    #         ).all()
            
    #         req.case = [{"date": str(row.shift_date), "shift": row.shift} for row in shift_rows]
    #         has_case = True
    #         print(f"자동 case 생성: {len(req.case)}건")
    #     else:
    #         print("과거 데이터 없거나 동일 요청 → 새로 생성")

    # 그래프 실행 여부
    print('req.request!!', req.request)
    should_run_graph = True
    if isinstance(req.request, (list, str)):
        cleaned = [str(r).strip() for r in (req.request if isinstance(req.request, list) else [req.request]) if str(r).strip()]
        if not cleaned:
            should_run_graph = False
            print("request 입력 없음 → 그래프 스킵")

    print(f"[DEBUG] should_run_graph: {should_run_graph}")

    response = None
    shift_parsed = {}

    if should_run_graph:
        print("[DEBUG] 그래프 실행 시작")
        try:
            raw_response = await graph_service.invoke(
                request=req.request,
                schema=req.schema,
                case=req.case,
                year=req.year,
                month=req.month,
                allowed_shifts=allowed_shifts_str,
                allowed_shift_map=allowed_shift_map
            )
        except Exception as e:
            print(f"그래프 호출 실패: {e}")
            traceback.print_exc()
            raw_response = None

        print(f"[FORCE] raw_response 타입: {type(raw_response)}")

        if isinstance(raw_response, str):
            try:
                raw_response = json.loads(raw_response)
                print("[FORCE] JSON 복구 성공")
            except:
                raw_response = [[], []]

        response = raw_response if isinstance(raw_response, list) and len(raw_response) == 2 else [[], []]

        try:
            shift_parsed = _parse_shift_results(response)
            print(f"[FORCE] shift_parsed: {shift_parsed}")
        except Exception as e:
            print(f"_parse_shift_results 실패: {e}")
            shift_parsed = {}

    else:
        print("[DEBUG] 그래프 스킵 → 빈 결과")
        response = [[], []]

    # 새 request_id 생성
    new_request_id = _persist_wanted_request(db, nurse_id, month_str, req.request)
    print(f"new_request_id: {new_request_id}")

    # 과거 데이터 복사 → case가 있어도 이전 LLM 결과를 초기값으로 가져옴
    copied_shift, copied_pair = 0, 0
    latest_wr = db.query(WantedRequest).filter(
        WantedRequest.nurse_id == nurse_id,
        WantedRequest.month == month_str,
    ).order_by(WantedRequest.created_at.desc()).offset(1).first()

    # case가 주어졌다면, 사용자가 남긴 (day, shift)만 복사하여 삭제한 항목은 제외
    case_filter: set[tuple[int, str]] | None = None
    if has_case:
        case_filter = set()
        for item in req.case or []:
            try:
                date_str = item.get("date")
                shift_type = item.get("shift")
                if not date_str or not shift_type:
                    continue
                day = int(str(date_str).split("-")[2]) if "-" in str(date_str) else int(date_str)
                if not 1 <= day <= 31:
                    continue
                case_filter.add((day, str(shift_type)))
            except Exception:
                continue

    if latest_wr:
        print(
            f"과거 데이터 복사: old={latest_wr.request_id} → new={new_request_id} "
            f"(case_filter 적용: {bool(case_filter)})"
        )
        copied_shift, copied_pair = _copy_existing_requests_to_new(
            db,
            nurse_id,
            latest_wr.request_id,
            new_request_id,
            req.year,
            req.month,
            month_str,
            case_filter=case_filter,
        )
    else:
        print("최초 요청 → 복사 스킵")

    # shift_map 구성
    shift_map = {}
    original_request_text = normalize_request_text(req.request)

    # 현재 new_request_id에 이미 복사된 레코드(기존 LLM 결과) 수집 → case가 덮어쓰지 않도록 보호
    existing_rows = db.query(NurseShiftRequest).filter(
        NurseShiftRequest.nurse_id == nurse_id,
        NurseShiftRequest.request_id == new_request_id,
    ).all()
    existing_index = {
        (_ymd(row.shift_date.year, row.shift_date.month, row.shift_date.day), row.shift): row
        for row in existing_rows
    }

    # 1. case 병합 (score 1.0 고정 + 사용자 직접 선택 플래그)
    if has_case:
        print("case 병합 시작")
        for item in req.case or []:
            print('item!!', item)
            date_str = item.get('date')
            shift_type = item.get('shift')
            if not date_str or not shift_type:
                continue
            try:
                # 날짜 파싱 (YYYY-MM-DD 또는 단순 일자 모두 허용)
                if '-' in str(date_str):
                    day = int(str(date_str).split('-')[2])
                else:
                    day = int(date_str)
                if not 1 <= day <= 31:
                    continue
            except:
                continue

            target_date = _ymd(req.year, req.month, day)
            # 기존 복사/LLM 데이터가 있으면 덮어쓰지 않고 유지
            if (target_date, shift_type) in existing_index:
                print(f"[case 스킵] 기존 데이터 유지: {shift_type} {day}일 (partial_request 보존)")
                continue

            shift_map.setdefault(shift_type, {})[day] = {
                'score': 1.0,
                'request': '사용자 직접 선택',
                'shift': shift_type  # 명시적으로 저장
            }

    # 2. AIDE 결과 병합 (case 보호 조건 완화)
    if shift_parsed:
        print("AIDE 결과 병합 시작")
        for shift_id, days_dict in shift_parsed.items():
            for day_str, info in days_dict.items():
                try:
                    day_int = int(day_str)
                    if not 1 <= day_int <= 31:
                        continue
                except:
                    continue

                score = float(info.get('score', 1.0))
                score = max(0.0, min(5.0, score))  # 범위 제한

                req_text = info.get('request', original_request_text or 'AIDE 추천')

                current = shift_map.get(shift_id, {}).get(day_int)

                # case 보호 로직 완화
                if current:
                    if current.get('request') == '사용자 직접 선택':
                        # 같은 shift면 보호, 다른 shift면 AIDE 우선 적용 허용
                        current_shift = current.get('shift')
                        if current_shift == shift_id:
                            print(f"case 보호 (같은 shift): {shift_id} {day_int}일 유지")
                            continue
                        else:
                            print(f"[AIDE 우선 적용] {day_int}일 case({current_shift}) → AIDE({shift_id})")
                            # 여기서 계속 진행 → 아래에서 업데이트됨

                # 기존 값 없거나 score가 더 높으면 업데이트
                if current is None or score > current['score']:
                    shift_map.setdefault(shift_id, {})[day_int] = {
                        'score': score,
                        'request': req_text,
                        'shift': shift_id
                    }
                    print(f"업데이트: {shift_id} {day_int}일 score={score}")

    # 최종 shift_map 로그 (디버깅용)
    print(f"최종 shift_map: {shift_map}")
    # 저장
    if shift_map:
        _persist_shift_results(
            db, nurse_id, new_request_id, req.year, req.month, month_str,
            shift_map, original_request_text
        )

    # preference 저장
    try:
        pref_parsed = _parse_preferences(response, req.schema)
        if pref_parsed:
            _persist_pair_results(db, nurse_id, new_request_id, month_str, pref_parsed)
    except Exception as e:
        print(f"preference 처리 오류: {e}")
        traceback.print_exc()

    # 최종 커밋
    try:
        db.commit()
        print(f"최종 commit 완료 - request_id={new_request_id}")
    except Exception as e:
        db.rollback()
        print(f"최종 commit 실패: {e}")
        traceback.print_exc()
        raise

    result = {
        "shift": shift_parsed,
        "preference": pref_parsed or []
    }
    print("그래프 실행 및 DB 저장 완료")
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
    if not current_user:
        raise Exception("Not authenticated")
    if not (getattr(current_user, 'is_head_nurse', False) or getattr(current_user, 'is_master_admin', False)):
        raise Exception("Permission denied")

    target_group_id = override_group_id or current_user.group_id
    if not target_group_id:
        raise Exception("대상 그룹이 없습니다.")

    existing_wanted = db.query(Wanted).filter(
        Wanted.group_id == target_group_id,
        Wanted.year == req.year,
        Wanted.month == req.month
    ).first()
    if existing_wanted:
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

    # 근무 희망 작성 요청 푸시 알림
    group_row = db.query(Group).filter(Group.group_id == target_group_id).first()
    office_id = group_row.office_id if group_row and group_row.office_id else current_user.office_id
    nurse_rows = (
        db.query(Nurse.nurse_id)
        .filter(Nurse.group_id == target_group_id)
        .all()
    )
    recipients = [row.nurse_id for row in nurse_rows]
    send_wanted_request_push(
        year=req.year,
        month=req.month,
        recipients=recipients,
        office_code=office_id,
        sender_emp_seq_no=current_user.nurse_id,
        sender_member_id=current_user.account_id,
        deadline=req.exp_date,
    )
    return {"message": "Wanted 작성 요청이 성공적으로 생성되었습니다."}

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

    # exp_date가 설정되어 있고 현재 시각보다 과거이며,
    # 아직 'requested' 상태인 Wanted만 조회합니다.
    query = (
        db.query(Wanted)
        .filter(
            Wanted.status == 'requested',
            Wanted.exp_date.isnot(None),
            Wanted.exp_date < now,
        )
    )

    updated_count = 0
    try:
        for wanted in query:
            wanted.status = 'closed'
            updated_count += 1

        if updated_count > 0:
            db.commit()
            print(f"Wanted 자동 마감 완료: {updated_count}건 (기준 시각: {now.isoformat()})")
        else:
            # 변경 사항이 없으면 굳이 커밋하지 않아도 되지만,
            # 세션 상태를 명확히 하기 위해 flush만 보장하는 수준으로 둡니다.
            db.flush()
    except Exception as exc:
        db.rollback()
        print(f"Wanted 자동 마감 중 오류 발생: {exc}")
        raise

    return updated_count
