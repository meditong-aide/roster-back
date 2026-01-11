"""
Wanted(근무 희망 요청) 관련 서비스 로직 모듈
- DB 쿼리, 데이터 가공 등 라우터에서 분리
- 모든 함수는 한글 docstring, 한글 print/logging, PEP8 스타일 적용
"""
from sqlalchemy.orm import Session
from sqlalchemy import func
from db.models import Wanted, Nurse, ShiftPreference, Shift
from schemas.roster_schema import WantedInvokeRequest, WantedDeadlineRequest
from schemas.auth_schema import User as UserSchema
from datetime import datetime, date, timedelta
from typing import Dict, Any, List, Tuple
from db.models import WantedRequest, NurseShiftRequest, NursePairRequest
from services.graph_service import graph_service
from dateutil.relativedelta import relativedelta
import traceback

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
    """wanted_requests 레코드를 저장하고 request_id 를 반환합니다.

    인자:
        db: DB 세션
        nurse_id: 간호사 ID
        month_str: 'YYYY-MM'
        request: 요청 텍스트 (문자열 또는 리스트)

    반환:
        새로 생성된 request_id
    
    Notes:
        request가 리스트인 경우 '\n'로 join하여 문자열로 변환
    """
    request_id = _next_request_id(db, nurse_id, month_str)
    
    # request가 리스트면 문자열로 변환
    if isinstance(request, list):
        # '기존 데이터에서 로드됨' 제외하고 join
        filtered_requests = [r for r in request if r != '기존 데이터에서 로드됨']
        request_text = '\n'.join(filtered_requests) if filtered_requests else ''
    else:
        request_text = (request or '').strip()
    print(f'request_text, {request_text}')
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
    """
    해당 (nurse_id, request_id) 기준으로 다음 detailed_request_id를 반환합니다.
    → request_id가 바뀔 때마다 무조건 1부터 시작하도록 설계
    """
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
    detailed_id = _next_detailed_request_id(db, nurse_id, request_id, table="shift")
    rows = 0
    
    print(f"shift_map 저장 시작 (request_id={request_id}, detailed_id={detailed_id}): {shift_map}")
    
    try:
        for shift_code, by_day in (shift_map or {}).items():
            for day, info in (by_day or {}).items():
                score = info.get("score", 1.0)
                request_val = info.get("request", "")
                
                partial_request = normalize_request_text(request_val or original_request)

                row = NurseShiftRequest(
                    nurse_id=nurse_id,
                    request_id=request_id,
                    detailed_request_id=detailed_id,
                    shift_date=_ymd(year, month, int(day)),
                    shift=shift_code,
                    score=float(score),
                    partial_request=partial_request,
                )
                
                db.add(row)
                rows += 1
                detailed_id += 1

        # ★★★ 첫 번째 request_id에서도 확실히 반영되도록 flush 강제
        db.flush()
        print(f"[FLUSH] shift {rows}건 flush 완료 (commit 전 확인용)")

        # flush 후 바로 조회해서 로그로 확인 (디버깅용 - 나중엔 제거 가능)
        inserted = db.query(NurseShiftRequest).filter(
            NurseShiftRequest.nurse_id == nurse_id,
            NurseShiftRequest.request_id == request_id
        ).all()
        print(f"[FLUSH 후 즉시 확인] request_id={request_id}에 저장된 shift 건수: {len(inserted)}")

    except Exception as e:
        print(f"[ERROR] shift 저장 중 오류: {str(e)}")
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
    detailed_id = _next_detailed_request_id(db, nurse_id, request_id, table="pair", month_str=month_str)
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
        db.merge(row)
        rows += 1
        detailed_id += 1
    
    db.commit()
    print(f"nurse_pair_requests 저장 완료: 시작 detailed_request_id={detailed_id - rows}, 종료={detailed_id - 1}, 저장 rows={rows}")


def _parse_shift_results(
    response: List[List[Dict[str, Any]]]
) -> Dict[str, Dict[int, Dict[str, Any]]]:
    """
    그래프 결과에서 shift_result를 모아
    {'E': {4: {'score': 1.9, 'request': '4일은 E로 주세요'}}, ...}
    형태로 변환합니다.
    """
    parsed: Dict[str, Dict[int, Dict[str, Any]]] = {}

    if not isinstance(response, list):
        return parsed

    for sub in response:
        if not isinstance(sub, list):
            continue

        for entry in sub:
            shift_results = entry.get("shift_result")
            if not isinstance(shift_results, list):
                continue

            for sr in shift_results:
                # nested 구조 평탄화
                record = (
                    sr["result"]
                    if isinstance(sr, dict) and "result" in sr and isinstance(sr["result"], dict)
                    else sr
                )

                if not isinstance(record, dict) or "shift" not in record:
                    continue

                shift = record.get("shift")
                dates = record.get("date") or []
                scores = record.get("score") or []
                requests = record.get("request") or []

                if not isinstance(dates, list) or not isinstance(scores, list):
                    continue
                if not isinstance(requests, list):
                    # 단일 문자열로 들어오면 리스트로 감싸기
                    requests = [requests]

                n = min(len(dates), len(scores), len(requests) if requests else len(dates))

                bucket = parsed.setdefault(str(shift), {})
                for i in range(n):
                    try:
                        d = int(dates[i])
                        s = float(scores[i])
                        req = requests[i] if i < len(requests) else None
                    except (TypeError, ValueError):
                        continue

                    bucket[d] = {"score": s, "request": req}

    return parsed



def _parse_preferences(response: List[List[Dict[str, Any]]], schema: List[Dict[str, Any]] | None = None) -> List[Dict[str, float]]:
    """그래프 결과에서 preference_result를 [{'id': '12', 'weight': -1.5}, ...]로 변환합니다.

    인자:
        response: 그래프 전체 응답
        schema: 유효한 간호사 ID 필터링용 스키마

    반환:
        선호 리스트 (중복 제거됨)
    """
    parsed: List[Dict[str, float]] = []
    seen = set()  # (id, weight, request) 튜플로 중복 체크
    valid_nurse_ids = set()
    if schema:
        for nurse in schema:
            if isinstance(nurse, dict) and 'nurse_id' in nurse:
                valid_nurse_ids.add(str(nurse['nurse_id']))
    for sub in response:
        if not isinstance(sub, list):
            continue
        for entry in sub:
            pref_list = entry.get("preference_result", [])
            if not isinstance(pref_list, list):
                continue
            for pr in pref_list:
                _id = pr.get("id")
                weight = pr.get("weight")
                request = pr.get("request")
                if _id is None or weight is None:
                    continue
                _id_str = str(_id)
                if valid_nurse_ids and _id_str not in valid_nurse_ids:
                    print(f"Parse Preferences: 무효한 간호사 ID '{_id_str}' 필터링됨")
                    continue
                try:
                    weight_float = float(weight)
                    # 중복 체크: (target_id, score, request) 조합
                    key = (_id_str, weight_float, request)
                    if key in seen:
                        print(f"Parse Preferences: 중복 제거 - target_id={_id_str}, weight={weight_float}")
                        continue
                    seen.add(key)
                    parsed.append({"id": _id_str, "weight": weight_float, "request": request})
                except Exception as e:
                    print('여기 오류 ㅠ')
                    print(f'e', e)
                    continue
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
    """기존 request_id의 데이터를 새 request_id로 복사합니다.

    인자:
        db: DB 세션
        nurse_id: 간호사 ID
        old_request_id: 기존 request_id
        new_request_id: 새 request_id
        year: 연도
        month: 월
        case_filter: 복사할 (day, shift) 튜플 set (None이면 전체 복사)

    반환:
        (복사된 shift 수, 복사된 pair 수) 튜플
        
    Notes:
        case_filter가 있으면 해당 (day, shift)만 복사 (캘린더에서 지운 항목 제외)
    """
    # 1. 기존 shift 데이터 복사
    start = datetime.strptime(month_str, "%Y-%m")
    end = start + relativedelta(months=1)
    old_shift_rows = (
        db.query(NurseShiftRequest)
        .filter(
            NurseShiftRequest.nurse_id == nurse_id,
            NurseShiftRequest.request_id == old_request_id,
            NurseShiftRequest.shift_date >= start,
            NurseShiftRequest.shift_date < end,
        )
        .all()
    )
    
    shift_count = 0
    detailed_id = 1
    for old_row in old_shift_rows:
        # case_filter가 있으면 필터링
        if case_filter is not None:
            day = int(str(old_row.shift_date).split('-')[2])
            shift = old_row.shift
            if (day, shift) not in case_filter:
                print(f"필터링됨: {day}일 {shift} (case에 없음)")
                continue
        
        new_row = NurseShiftRequest(
            nurse_id=nurse_id,
            request_id=new_request_id,
            detailed_request_id=detailed_id,
            shift_date=old_row.shift_date,
            shift=old_row.shift,
            score=old_row.score,
            partial_request=old_row.partial_request,
        )
        print(f'new_row', new_row.__dict__)
        db.merge(new_row)
        shift_count += 1
        detailed_id += 1
    
    if shift_count > 0:
        db.commit()
        print(f"기존 shift 데이터 복사 완료: {shift_count}건")
    
    # 2. 기존 pair 데이터 복사
    old_pair_rows = (
        db.query(NursePairRequest)
        .filter(
            NursePairRequest.nurse_id == nurse_id,
            NursePairRequest.request_id == old_request_id,
        )
        .all()
    )
    print(f'old_pair_rows',old_pair_rows)
    pair_count = 0
    detailed_id = 1
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
    print(f'pair_count',pair_count)
    if pair_count > 0:
        db.commit()
        print(f"기존 pair 데이터 복사 완료: {pair_count}건")
    return shift_count, pair_count


def normalize_request_text(value: Any) -> str:
    if value is None:
        return '기존 데이터 업데이트'
    if isinstance(value, list):
        filtered = [r for r in value if r and r != '기존 데이터에서 로드됨']
        return '\n'.join(filtered) if filtered else '기존 데이터 업데이트'
    return str(value).strip() or '기존 데이터 업데이트'


def cleanup_previous_requests(db: Session, nurse_id: str, month_str: str, current_request_id: int):
    """
    같은 월의 이전 request_id에 대한 shift/pair 데이터를 삭제합니다.
    → 항상 최신 request_id의 데이터만 남기고 싶을 때 사용
    (필요 없을 경우 이 함수 호출 생략 가능)
    """
    print(f"이전 요청 데이터 정리 시작: nurse_id={nurse_id}, month={month_str}, current={current_request_id}")
    
    deleted_shift = db.query(NurseShiftRequest).filter(
        NurseShiftRequest.nurse_id == nurse_id,
        NurseShiftRequest.request_id < current_request_id,
        # 필요하면 month 조건 추가: NurseShiftRequest.shift_date >= ..., < ...
    ).delete()
    
    deleted_pair = db.query(NursePairRequest).filter(
        NursePairRequest.nurse_id == nurse_id,
        NursePairRequest.request_id < current_request_id,
    ).delete()
    
    print(f"정리 완료: shift {deleted_shift}건, pair {deleted_pair}건 삭제")


async def invoke_and_persist_wanted_service(
    req: WantedInvokeRequest,
    current_user: UserSchema,
    db: Session,
) -> Dict[str, Any]:
    """
    Wanted 그래프 실행 후 결과를 DB에 저장하고 응답을 반환합니다.
    - 사용자 직접 선택(case)은 항상 유지
    - request_id가 바뀔 때마다 새로운 레코드로 누적 저장 (삭제하지 않음)
    - 첫 번째 request_id에서도 확실히 저장되도록 flush 및 로그 강화
    """
    print("그래프 실행 및 DB 저장을 시작합니다.")

    nurse_id = current_user.nurse_id
    print('request:', req.__dict__)
    month_str = _yyyymm(req.year, req.month)

    # 허용 근무코드 조회
    allowed_shifts_query = db.query(Shift.shift_id, Shift.name).filter(
        Shift.group_id == current_user.group_id,
        Shift.show_in_preference == True
    ).all()
    
    allowed_shift_map = {row.shift_id: row.name for row in allowed_shifts_query}
    allowed_shifts_str = ", ".join(allowed_shift_map.keys()) if allowed_shift_map else "없음"
    print(f"허용 근무 코드: {allowed_shifts_str}")

    # 중복 방지 (1분 이내 동일 request 무시)
    recent_duplicate = (
        db.query(WantedRequest).filter(
            WantedRequest.nurse_id == nurse_id,
            WantedRequest.month == month_str,
            WantedRequest.request == normalize_request_text(req.request),
            WantedRequest.created_at > datetime.now() - timedelta(seconds=30)
        )
        .first()
    )
    
    if recent_duplicate:
        print(f"중복 요청 감지 - 기존 request_id={recent_duplicate.request_id}")
        return {
            "message": "이미 저장 처리 중입니다.",
            "request_id": recent_duplicate.request_id,
            "shift": {},
            "preference": []
        }

    # case 여부 확인 및 자동 로드
    has_case = req.case is not None and len(req.case) > 0
    print(f'original has_case: {has_case}')

    if not has_case:
        print("case 없음 → 과거 최신 데이터 자동 로드 시도")
        latest_wr = (
            db.query(WantedRequest)
            .filter(
                WantedRequest.nurse_id == nurse_id,
                WantedRequest.month == month_str,
            )
            .order_by(WantedRequest.created_at.desc())
            .first()
        )
        
        if latest_wr and latest_wr.request != normalize_request_text(req.request):
            print(f"과거 데이터 발견 (request_id={latest_wr.request_id}) → 자동 case 설정")
            shift_rows = db.query(NurseShiftRequest).filter(
                NurseShiftRequest.nurse_id == nurse_id,
                NurseShiftRequest.request_id == latest_wr.request_id
            ).all()
            
            req.case = [
                {"date": str(row.shift_date), "shift": row.shift}
                for row in shift_rows
            ]
            has_case = True
            print(f"자동 case 생성 완료 ({len(req.case)}건)")
        else:
            print("과거 데이터 없거나 동일 요청 → 새로 생성")

    # request가 실질적으로 비었는지 확인 → 그래프 스킵 여부
    should_run_graph = True
    if isinstance(req.request, list):
        cleaned = [str(r).strip() for r in req.request if r and str(r).strip()]
        if not cleaned:
            should_run_graph = False
            print("request 입력 없음 → AIDE 그래프 스킵")
    elif isinstance(req.request, str):
        if not req.request.strip():
            should_run_graph = False
            print("request 빈 문자열 → AIDE 그래프 스킵")

    print("[DEBUG] should_run_graph 값:", should_run_graph)

    response = None  # 반드시 초기화!
    shift_parsed = {}  # 기본값으로 빈 딕셔너리 (500 에러 방지)

    if should_run_graph:
        print("[DEBUG] 그래프 실제 실행 시작!")
        raw_response = None
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
            print(f"그래프 호출 자체 실패: {e}")
            traceback.print_exc()

        # ★★★★★ try 밖에서 무조건 복구 로직 실행 ★★★★★
        import json
        print(f"[FORCE DEBUG] invoke 직후 raw_response 타입: {type(raw_response)}")

        if isinstance(raw_response, str):
            print("[FORCE] 문자열 감지됨 → json.loads 복구 시도")
            try:
                raw_response = json.loads(raw_response)
                print("[FORCE] 복구 성공! 정상 형태로 변환됨")
                if isinstance(raw_response, list) and len(raw_response) == 2:
                    print(f"[FORCE] 복구 후 shift_results 예시: {raw_response[0][0].get('shift_result', [{}])[0] if raw_response[0] else '빈 값'}")
                else:
                    print("[FORCE WARNING] 복구 후 구조 이상 → 빈 리스트로 대체")
                    raw_response = [[], []]
            except json.JSONDecodeError as je:
                print(f"[FORCE ERROR] JSON 파싱 실패: {je}")
                print(f"[FORCE ERROR] 원본 문자열 미리보기 (500자): {str(raw_response)[:500]}...")
                raw_response = [[], []]
            except Exception as e:
                print(f"[FORCE ERROR] 복구 중 기타 에러: {e}")
                raw_response = [[], []]
        else:
            print("[FORCE] 정상 list 타입 반환됨 → 그대로 사용")

        response = raw_response

        # 최종 안전장치
        if not isinstance(response, list) or len(response) != 2:
            print("[FORCE WARNING] response 구조 이상 → 빈 리스트로 강제 초기화")
            response = [[], []]

        # 이제 파싱 시도
        try:
            shift_parsed = _parse_shift_results(response)
            print(f"[FORCE] shift_parsed 최종 결과: {shift_parsed}")
        except Exception as e:
            print(f"_parse_shift_results 실패: {e}")
            traceback.print_exc()
            shift_parsed = {}
    else:
        print("[DEBUG] 그래프 스킵됨 → response를 빈 값으로 설정")
        response = [[], []]
        shift_parsed = {}

    # 새 wanted_request 생성
    new_request_id = _persist_wanted_request(db, nurse_id, month_str, req.request)
    print(f'new_request_id: {new_request_id}')

    # shift_map 구성
    shift_map = {}
    original_request_text = normalize_request_text(req.request)

    # 1. 사용자 직접 선택(case) → 항상 score 1.0으로 최우선
    if has_case:
        print("case 병합 시작")
        for item in req.case:
            date_str = item.get('date', '')
            shift_type = item.get('shift', '')
            if not date_str or not shift_type:
                continue
            day = int(date_str.split('-')[2]) if '-' in date_str else int(date_str)
            
            current = shift_map.get(shift_type, {}).get(day)
            if current is None:
                shift_map.setdefault(shift_type, {})[day] = {
                    'score': 1.0,
                    'request': '사용자 직접 선택'
                }

    # 2. AIDE 추천 병합 → score가 더 높을 때만 덮어쓰기
    if shift_parsed:
        print("AIDE 추천 병합")
        for shift_id, days_dict in shift_parsed.items():
            for day_str, info in days_dict.items():
                day = int(day_str)
                score = info.get('score', 1.0)
                current = shift_map.get(shift_id, {}).get(day)
                if current is None or score > current['score']:
                    shift_map.setdefault(shift_id, {})[day] = {
                        'score': score,
                        'request': original_request_text or 'AIDE 추천'
                    }

    print(f"최종 shift_map: {shift_map}")

    # shift 데이터 저장
    if shift_map:
        _persist_shift_results(
            db=db,
            nurse_id=nurse_id,
            request_id=new_request_id,
            year=req.year,
            month=req.month,
            month_str=month_str,
            shift_map=shift_map,
            original_request=original_request_text
        )

    # preference 처리
    try:
        pref_parsed = _parse_preferences(response or [], req.schema)
        if pref_parsed:
            _persist_pair_results(db, nurse_id, new_request_id, month_str, pref_parsed)
    except Exception as e:
        print(f"preference 처리 오류: {e}")
        traceback.print_exc()

    # 최종 commit (모든 변경사항 한 번에 반영)
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
