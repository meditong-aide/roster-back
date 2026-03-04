"""
간호사 선호도(Preferences) 관련 서비스 로직 모듈
- DB 쿼리, 데이터 가공 등 라우터에서 분리
- 모든 함수는 한글 docstring, 한글 print/logging, PEP8 스타일 적용
"""

import pprint
from sqlalchemy.orm import Session
from sqlalchemy import String, cast, extract
from db.models import (
    WantedRequest,
    Nurse,
    NurseShiftRequest,
    NursePairRequest,
    ShiftPreference,
    Shift,
    WantedConfig,
    Wanted,
)
from schemas.roster_schema import PreferenceData, PreferenceSubmit
from schemas.auth_schema import User as UserSchema
from datetime import datetime, timezone, timedelta, date


def _carry_forward_pair_data(
    db: Session, nurse_id: str, current_request_id: int, month_str: str
):
    """
    이전 request_id의 pair(선호/비선호) 데이터를 현재 request_id로 복사합니다.
    현재 request_id에 pair 데이터가 이미 있으면 복사하지 않습니다.

    인자:
        db: DB 세션
        nurse_id: 간호사 ID
        current_request_id: 현재(새) request_id
        month_str: 'YYYY-MM'

    반환:
        복사된 pair 건수
    """
    nurse = db.query(Nurse).filter(Nurse.nurse_id == nurse_id).first()
    office_id = nurse.office_id if nurse else None
    group_id = nurse.group_id if nurse else None

    # 현재 request_id에 이미 pair 데이터가 있으면 복사 불필요
    existing_count = (
        db.query(NursePairRequest)
        .filter(
            NursePairRequest.office_id == office_id,
            NursePairRequest.group_id == group_id,
            NursePairRequest.nurse_id == nurse_id,
            NursePairRequest.request_id == current_request_id,
            NursePairRequest.month == month_str,
        )
        .count()
    )
    if existing_count > 0:
        print(
            f"[pair carry-forward] 현재 request_id={current_request_id}에 이미 {existing_count}건 존재 → 스킵"
        )
        return 0

    # 같은 nurse_id/month에서 현재보다 작은 request_id 중 pair 데이터가 있는 가장 큰 것을 찾기
    prev_request_ids = (
        db.query(WantedRequest.request_id)
        .filter(
            WantedRequest.office_id == office_id,
            WantedRequest.group_id == group_id,
            WantedRequest.nurse_id == nurse_id,
            WantedRequest.month == month_str,
            WantedRequest.request_id < current_request_id,
        )
        .order_by(WantedRequest.request_id.desc())
        .all()
    )

    for (prev_rid,) in prev_request_ids:
        pair_rows = (
            db.query(NursePairRequest)
            .filter(
                NursePairRequest.office_id == office_id,
                NursePairRequest.group_id == group_id,
                NursePairRequest.nurse_id == nurse_id,
                NursePairRequest.request_id == prev_rid,
                NursePairRequest.month == month_str,
            )
            .all()
        )

        if pair_rows:
            detailed_id = 1
            for row in pair_rows:
                db.add(
                    NursePairRequest(
                        office_id=office_id,
                        group_id=group_id,
                        nurse_id=nurse_id,
                        request_id=current_request_id,
                        month=month_str,
                        detailed_request_id=detailed_id,
                        target_id=row.target_id,
                        score=row.score,
                        partial_request=row.partial_request,
                    )
                )
                detailed_id += 1
            print(
                f"[pair carry-forward] request_id {prev_rid} → {current_request_id}: {detailed_id - 1}건 복사 완료"
            )
            return detailed_id - 1

    print(
        f"[pair carry-forward] 복사할 이전 pair 데이터 없음 (nurse_id={nurse_id}, month={month_str})"
    )
    return 0


def submit_preferences_service(
    req: PreferenceData, current_user: UserSchema, db: Session, is_draft: bool = False
):
    """
    희망근무 저장/제출 통합 서비스
    """
    month_str = f"{req.year}-{req.month:02d}"

    # data가 None이거나 없으면 빈 딕셔너리로 안전하게 처리
    preference_data = req.data if req.data is not None else {}

    # draft 찾기/생성
    draft = (
        db.query(WantedRequest)
        .filter(
            WantedRequest.office_id == current_user.office_id,
            WantedRequest.group_id == current_user.group_id,
            WantedRequest.nurse_id == current_user.nurse_id,
            WantedRequest.month == month_str,
            WantedRequest.is_submitted == False,
        )
        .order_by(WantedRequest.created_at.desc())
        .first()
    )

    if draft:
        request_id = draft.request_id
    else:
        draft = WantedRequest(
            office_id=current_user.office_id,
            group_id=current_user.group_id,
            nurse_id=current_user.nurse_id,
            month=month_str,
            request="",
            is_submitted=False,
            created_at=datetime.now(),
        )
        db.add(draft)
        db.flush()
        request_id = draft.request_id

    # ==================== 핵심: 항상 data_to_save 초기화 (에러 방지) ====================
    data_to_save = {}
    comment_by_date = {}

    if isinstance(preference_data, dict):
        # AIDE 스타일 중첩 구조인지 확인
        shift_data = preference_data.get("shift", {})
        if isinstance(shift_data, dict):
            for shift_id, days in shift_data.items():
                if isinstance(days, dict):
                    for day_str, day_value in days.items():
                        try:
                            day_num = int(day_str)
                            full_date = f"{req.year}-{req.month:02d}-{day_num:02d}"
                            data_to_save[full_date] = shift_id
                            if isinstance(day_value, dict):
                                comment_by_date[full_date] = (
                                    day_value.get("comment", "") or ""
                                )
                        except (ValueError, TypeError):
                            continue
        else:
            # 평평한 {날짜: shift} 형식
            data_to_save = preference_data

    # ==================== WantedConfig 검증 (최종 제출 시에만) ====================
    if not is_draft and data_to_save:
        group_id = current_user.group_id
        nurse_id = current_user.nurse_id

        # # 1. GLOBAL 설정 확인 - 더 이상 사용하지 않음 (nurses 테이블로 이동됨)
        # global_config = db.query(WantedConfig).filter(
        #     WantedConfig.group_id == group_id,
        #     WantedConfig.config_type == 'GLOBAL'
        # ).first()

        # 2. Wanted 테이블 상태 확인 (해당 월의 원티드 요청이 생성되었는지, 마감되었는지)
        wanted = (
            db.query(Wanted)
            .filter(
                Wanted.group_id == group_id,
                Wanted.year == req.year,
                Wanted.month == req.month,
            )
            .first()
        )

        # 2-1. Wanted가 존재하지 않으면 (수간호사가 아직 원티드 요청을 생성하지 않음)
        if not wanted:
            print(
                f"[검증 실패] Wanted 요청이 생성되지 않음: group_id={group_id}, {req.year}-{req.month:02d}"
            )
            raise Exception(
                f"{req.year}년 {req.month}월 원티드 요청이 아직 생성되지 않았습니다."
            )

        # 2-2. status가 'closed'인 경우 (이미 마감됨)
        if wanted.status == "closed":
            print(
                f"[검증 실패] Wanted가 이미 마감됨: group_id={group_id}, {req.year}-{req.month:02d}"
            )
            raise Exception(f"{req.year}년 {req.month}월 원티드가 이미 마감되었습니다.")

        # 2-3. exp_date가 지난 경우 (마감일 경과)
        if wanted.exp_date and wanted.exp_date < datetime.now():
            print(
                f"[검증 실패] Wanted 마감일 경과: group_id={group_id}, {req.year}-{req.month:02d}, "
                f"마감일={wanted.exp_date.strftime('%Y-%m-%d %H:%M')}"
            )
            raise Exception(
                f"{req.year}년 {req.month}월 원티드 마감일({wanted.exp_date.strftime('%Y-%m-%d %H:%M')})이 지났습니다."
            )

        print(
            f"[검증 통과] Wanted 상태 확인: status={wanted.status}, "
            f"exp_date={wanted.exp_date.strftime('%Y-%m-%d %H:%M') if wanted.exp_date else 'None'}"
        )

        # 3. 재제출 여부 확인
        existing_submitted = (
            db.query(WantedRequest)
            .filter(
                WantedRequest.office_id == current_user.office_id,
                WantedRequest.group_id == current_user.group_id,
                WantedRequest.nurse_id == nurse_id,
                WantedRequest.month == month_str,
                WantedRequest.is_submitted == True,
            )
            .first()
        )

        is_resubmit = existing_submitted is not None

        print(f"[검증 통과] GLOBAL 설정 확인 완료: is_resubmit={is_resubmit}")

        # # 4. NURSE_LIMIT 검증 - 프론트에서 검증, nurses 테이블로 이동됨
        # nurse = db.query(Nurse).filter(Nurse.nurse_id == nurse_id).first()
        # if nurse and nurse.wanted_max_requests is not None:
        #     start_date = date(req.year, req.month, 1)
        #     if req.month == 12:
        #         end_date = date(req.year + 1, 1, 1)
        #     else:
        #         end_date = date(req.year, req.month + 1, 1)
        #
        #     query = db.query(NurseShiftRequest).filter(
        #         NurseShiftRequest.nurse_id == nurse_id,
        #         NurseShiftRequest.shift_date >= start_date,
        #         NurseShiftRequest.shift_date < end_date
        #     )
        #     if is_resubmit and existing_submitted:
        #         query = query.filter(NurseShiftRequest.request_id != existing_submitted.request_id)
        #
        #     nurse_current_count = query.count()
        #     additional_count = len(data_to_save)
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

        # # 5. DAILY_LIMIT 검증 - 프론트에서 검증
        # case_dates = set()
        # for date_str in data_to_save.keys():
        #     try:
        #         parsed_date = datetime.strptime(date_str, '%Y-%m-%d').date()
        #         case_dates.add(parsed_date)
        #     except ValueError:
        #         continue
        #
        # if case_dates:
        #     daily_limit_configs = db.query(WantedConfig).filter(
        #         WantedConfig.group_id == group_id,
        #         WantedConfig.target_date.in_(case_dates),
        #         WantedConfig.shift_type.is_(None)
        #     ).all()
        #
        #     daily_limit_map = {config.target_date: config.max_requests for config in daily_limit_configs}
        #
        #     if daily_limit_map:
        #         group_nurse_ids = [n[0] for n in db.query(Nurse.nurse_id).filter(Nurse.group_id == group_id).all()]
        #
        #         exceeded_dates = []
        #         for check_date in case_dates:
        #             if check_date in daily_limit_map:
        #                 daily_limit = daily_limit_map[check_date]
        #
        #                 query = db.query(NurseShiftRequest).filter(
        #                     NurseShiftRequest.nurse_id.in_(group_nurse_ids),
        #                     NurseShiftRequest.shift_date == check_date
        #                 )
        #
        #                 if is_resubmit and existing_submitted:
        #                     query = query.filter(
        #                         ~((NurseShiftRequest.nurse_id == nurse_id) &
        #                           (NurseShiftRequest.request_id == existing_submitted.request_id))
        #                     )
        #
        #                 daily_current_count = query.count()
        #
        #                 if daily_current_count + 1 > daily_limit:
        #                     exceeded_dates.append({
        #                         "date": check_date.strftime('%Y-%m-%d'),
        #                         "current": daily_current_count,
        #                         "limit": daily_limit
        #                     })
        #
        #         if exceeded_dates:
        #             exceeded_info = ", ".join([
        #                 f"{d['date']}({d['current']}/{d['limit']})"
        #                 for d in exceeded_dates
        #             ])
        #             print(f"[검증 실패] DAILY_LIMIT 초과: {exceeded_info}")
        #             raise Exception(
        #                 f"다음 날짜의 일자별 최대 요청 개수를 초과했습니다: {exceeded_info}"
        #             )
        #
        #         print(f"[검증 통과] DAILY_LIMIT: case 날짜 {len(case_dates)}개 확인 완료")

    # 최종 제출 시에만 이전 데이터 삭제 (data_to_save가 있을 때만)
    if not is_draft and data_to_save:  # ← 이제 안전하게 참조 가능
        db.query(NurseShiftRequest).filter(
            NurseShiftRequest.office_id == current_user.office_id,
            NurseShiftRequest.group_id == current_user.group_id,
            NurseShiftRequest.nurse_id == current_user.nurse_id,
            NurseShiftRequest.request_id == request_id,
        ).delete()
        db.query(NursePairRequest).filter(
            NursePairRequest.office_id == current_user.office_id,
            NursePairRequest.group_id == current_user.group_id,
            NursePairRequest.nurse_id == current_user.nurse_id,
            NursePairRequest.request_id == request_id,
        ).delete()

    # ==================== 상세 데이터 저장 ====================
    detailed_id = 1
    for date_str, shift_id in data_to_save.items():
        if shift_id:  # 빈 값은 저장하지 않음
            # 사유작성
            comment = comment_by_date.get(date_str, "")
            item = (
                preference_data.get(date_str)
                if isinstance(preference_data, dict)
                else None
            )
            if isinstance(item, dict):
                comment = item.get("comment", "")
            elif isinstance(item, str):
                pass
            db.add(
                NurseShiftRequest(
                    office_id=current_user.office_id,
                    group_id=current_user.group_id,
                    nurse_id=current_user.nurse_id,
                    request_id=request_id,
                    detailed_request_id=detailed_id,
                    shift_date=date_str,
                    shift=shift_id,
                    score=1.0,
                    partial_request="",
                    comment=comment,  # 사유작성
                )
            )
            detailed_id += 1

    # ==================== pair(선호/비선호) 데이터 처리 ====================
    # data 내부 또는 top-level 어디에든 preference가 있으면 인식
    preference_list = (
        preference_data.get("preference", None)
        if isinstance(preference_data, dict)
        else None
    )
    if preference_list is None and req.preference is not None:
        preference_list = req.preference

    if preference_list is not None and isinstance(preference_list, list):
        # 프론트에서 preference 배열을 명시적으로 보낸 경우 → 해당 항목만 저장
        # (제거된 항목은 배열에 미포함되어 자연스럽게 새 request_id에 저장되지 않음)
        db.query(NursePairRequest).filter(
            NursePairRequest.office_id == current_user.office_id,
            NursePairRequest.group_id == current_user.group_id,
            NursePairRequest.nurse_id == current_user.nurse_id,
            NursePairRequest.request_id == request_id,
            NursePairRequest.month == month_str,
        ).delete()

        pair_detailed_id = 1
        for pair in preference_list:
            target_id = pair.get("id")
            weight = pair.get("weight")
            if target_id is None or weight is None:
                continue
            db.add(
                NursePairRequest(
                    office_id=current_user.office_id,
                    group_id=current_user.group_id,
                    nurse_id=current_user.nurse_id,
                    request_id=request_id,
                    month=month_str,
                    detailed_request_id=pair_detailed_id,
                    target_id=str(target_id),
                    score=float(weight),
                    partial_request=pair.get("request", ""),
                )
            )
            pair_detailed_id += 1
        print(
            f"[pair 저장] preference 배열 기반 {pair_detailed_id - 1}건 저장 (request_id={request_id})"
        )
    else:
        # preference 배열이 없는 경우 → 이전 request_id에서 pair 데이터 carry-forward
        # 초기화 시 carry-forward 스킵
        if (
            data_to_save or preference_list
        ):  # shift나 preference가 있으면 carry-forward 진행
            _carry_forward_pair_data(db, current_user.nurse_id, request_id, month_str)
        else:
            print(
                f"[pair 초기화] 빈 데이터로 인해 carry-forward 스킵 (request_id={request_id})"
            )

    # 제출 처리
    if not is_draft:
        draft.is_submitted = True
        draft.submitted_at = datetime.now()

    db.commit()

    return {"message": "임시 저장되었습니다." if is_draft else "제출이 완료되었습니다."}


def submit_empty_preferences_service(req: PreferenceSubmit, current_user, db: Session):
    """
    빈 선호도 최종 제출 서비스 함수
    """
    KST = timezone(timedelta(hours=9))
    if not current_user:
        raise Exception("Not authenticated")
    empty_data = {"shift": {}, "preference": []}
    preference = ShiftPreference(
        office_id=current_user.office_id,
        group_id=current_user.group_id,
        nurse_id=current_user.nurse_id,
        year=req.year,
        month=req.month,
        data=empty_data,
        is_submitted=True,
        submitted_at=datetime.now(KST).replace(tzinfo=None),
    )
    db.add(preference)
    db.commit()
    return {"message": "Empty preferences submitted successfully"}


def retract_submission_service(req: PreferenceSubmit, current_user, db: Session):
    """
    선호도 제출 철회 서비스 함수
    """
    if not current_user:
        raise Exception("Not authenticated")
    print("current_user", current_user.__dict__)
    print("req", req.__dict__)
    month_str = f"{req.year}-{req.month:02d}"
    preference = (
        db.query(WantedRequest)
        .filter(
            WantedRequest.office_id == current_user.office_id,
            WantedRequest.group_id == current_user.group_id,
            WantedRequest.nurse_id == current_user.nurse_id,
            WantedRequest.month == month_str,
            WantedRequest.is_submitted == True,
        )
        .order_by(WantedRequest.submitted_at.desc())
        .first()
    )
    if not preference:
        raise Exception("No submitted preference found to retract")
    preference.is_submitted = False
    preference.submitted_at = None
    db.commit()
    return {"message": "Submission retracted successfully"}


def get_latest_preference_service(year: int, month: int, current_user, db: Session):
    """
    최신 선호도 데이터 조회 서비스 함수 (WantedRequest 기반)
    Front 기대 출력 형태에 맞게 중첩 구조로 변환하여 반환
    """
    if not current_user:
        raise Exception("Not authenticated")
    nurse_id = current_user.nurse_id
    month_str = f"{year}-{month:02d}"
    # 1️⃣ 제출된 요청 중 최신 데이터
    submitted_wr = (
        db.query(WantedRequest)
        .filter(
            WantedRequest.office_id == current_user.office_id,
            WantedRequest.group_id == current_user.group_id,
            WantedRequest.nurse_id == nurse_id,
            WantedRequest.month == month_str,
            WantedRequest.is_submitted == True,
        )
        .order_by(WantedRequest.submitted_at.desc())
        .first()
    )
    # 2️⃣ 없으면 가장 오래된(created_at.asc) 임시 요청 선택
    target_wr = submitted_wr or (
        db.query(WantedRequest)
        .filter(
            WantedRequest.office_id == current_user.office_id,
            WantedRequest.group_id == current_user.group_id,
            WantedRequest.nurse_id == nurse_id,
            WantedRequest.month == month_str,
        )
        .order_by(WantedRequest.created_at.desc())
        .first()
    )
    if not target_wr:
        # 아무 데이터도 없을 때
        return {
            "preference_data": None,
            "is_submitted": False,
            "created_at": None,
            "submitted_at": None,
        }
    # 3️⃣ 해당 request_id로 shift / pair 데이터 가져오기
    shift_rows = (
        db.query(NurseShiftRequest)
        .filter(
            NurseShiftRequest.office_id == current_user.office_id,
            NurseShiftRequest.group_id == current_user.group_id,
            NurseShiftRequest.nurse_id == nurse_id,
            NurseShiftRequest.request_id == target_wr.request_id,
            cast(NurseShiftRequest.shift_date, String).like(f"{month_str}-%"),
        )
        .all()
    )

    pair_rows = (
        db.query(NursePairRequest)
        .filter(
            NursePairRequest.office_id == current_user.office_id,
            NursePairRequest.group_id == current_user.group_id,
            NursePairRequest.nurse_id == nurse_id,
            NursePairRequest.request_id == target_wr.request_id,
            NursePairRequest.month == month_str,
        )
        .all()
    )
    # 4️⃣ shift 데이터 구조화 -> 여기만 나중에 바꿀것
    # 예: {'N': {'1': {'request': '주말은 N로 줘', 'score': 1.7}}, 'O': {...}}
    shift_data = {}
    for s in shift_rows:
        shift_code = s.shift.upper()
        day = str(int(s.shift_date.day))
        if shift_code not in shift_data:
            shift_data[shift_code] = {}
        shift_data[shift_code][day] = {
            "request": s.partial_request,
            "score": float(s.score) if s.score is not None else None,
            "comment": s.comment,  # 사유작성
        }

    # 5️⃣ pair 데이터 구조화 -> 여기만 나중에 바꿀것
    # 예: [{'id': '간호사ID', 'request': '같이해주세요', 'weight': -1.5}]
    pair_data = []
    for p in pair_rows:
        pair_data.append(
            {
                "id": p.target_id,
                "request": p.partial_request,
                "weight": float(p.score) if p.score is not None else 0.0,
            }
        )
    # 6️⃣ 최종 JSON 구성 (Front 기대 형식)
    preference_data = {
        "request": target_wr.request,  # 상위 텍스트 그대로
        "shift": shift_data,
        "preference": pair_data,
    }
    return {
        "preference_data": preference_data,
        "is_submitted": bool(target_wr.is_submitted),
        "created_at": target_wr.created_at,
        "submitted_at": target_wr.submitted_at,
    }


def get_all_preferences_service(
    year: int,
    month: int,
    current_user,
    db: Session,
    override_group_id: str | None = None,
):
    """
    모든 간호사의 최신 선호도 데이터 조회 서비스 함수 (새 구조 기반)
    - WantedRequest, NurseShiftRequest, NursePairRequest 조합
    - Output은 기존 ShiftPreference.data 구조와 동일하게 유지

    관리자(ADM)는 `override_group_id`로 대상 그룹을 지정할 수 있습니다.
    """
    if not current_user:
        raise Exception("Not authenticated")
    month_str = f"{year}-{month:02d}"

    target_group_id = override_group_id or current_user.group_id
    if not target_group_id:
        raise Exception("대상 그룹이 없습니다.")
    # ✅ 1️⃣ 그룹 내 간호사 목록 가져오기
    nurse_ids = [
        n.nurse_id
        for n in db.query(Nurse.nurse_id)
        .filter(Nurse.group_id == target_group_id)
        .all()
    ]
    # ✅ 2️⃣ 각 간호사별 최신 요청(WantedRequest) 찾기
    wanted_requests = (
        db.query(WantedRequest)
        .filter(
            WantedRequest.office_id == current_user.office_id,
            WantedRequest.group_id == target_group_id,
            WantedRequest.nurse_id.in_(nurse_ids),
            WantedRequest.month == month_str,
            WantedRequest.is_submitted == True,
        )
        .order_by(WantedRequest.nurse_id, WantedRequest.submitted_at.desc())
        .all()
    )
    # ✅ 3️⃣ nurse_id별 최신 submitted request만 남기기
    latest_wr_map = {}
    for wr in wanted_requests:
        if wr.nurse_id not in latest_wr_map:
            latest_wr_map[wr.nurse_id] = wr
    results = []

    # ✅ 4️⃣ 각 nurse_id에 대해 shift/pair 요청 조회 및 JSON 구성
    for nurse_id, wr in latest_wr_map.items():
        # shift 요청들
        shift_rows = (
            db.query(NurseShiftRequest)
            .filter(
                NurseShiftRequest.office_id == current_user.office_id,
                NurseShiftRequest.group_id == target_group_id,
                NurseShiftRequest.nurse_id == nurse_id,
                NurseShiftRequest.request_id == wr.request_id,
                cast(NurseShiftRequest.shift_date, String).like(f"{month_str}-%"),
            )
            .all()
        )

        allowed_shifts = {
            row[0].upper()
            for row in db.query(Shift.shift_id)
            .filter(Shift.group_id == target_group_id, Shift.show_in_preference == True)
            .all()
        }

        shift_data = {}

        for s in shift_rows:
            shift_type = (s.shift or "").upper().strip()
            if not shift_type or shift_type not in allowed_shifts:
                continue

            day = str(s.shift_date.day).lstrip("0")

            if shift_type not in shift_data:
                shift_data[shift_type] = {}

            # shift_data[shift_type][day] = (
            #     int(s.score) if s.score is not None else 1
            # )
            shift_data[shift_type][day] = {
                "score": float(s.score) if s.score is not None else 1.0,
                "comment": s.comment,  # 사유작성
            }

        # pair 요청들
        pair_rows = (
            db.query(NursePairRequest)
            .filter(
                NursePairRequest.office_id == current_user.office_id,
                NursePairRequest.group_id == target_group_id,
                NursePairRequest.nurse_id == nurse_id,
                NursePairRequest.request_id == wr.request_id,
                NursePairRequest.month == month_str,
            )
            .all()
        )
        pair_data = [{"id": p.target_id, "weight": p.score} for p in pair_rows]
        # ✅ data JSON 구성
        data_json = {
            "request": wr.request,
            "shift": {k: v for k, v in shift_data.items() if v},
            "preference": pair_data,
        }
        results.append(
            {
                "nurse_id": nurse_id,
                "year": year,
                "month": month,
                "is_submitted": bool(wr.is_submitted),
                "created_at": wr.created_at,
                "submitted_at": wr.submitted_at,
                "data": data_json,
            }
        )
    return results
