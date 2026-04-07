import datetime
import io
import os
import random
import re
import shutil
import time
import urllib.parse
from pathlib import Path
from typing import List, Literal, Tuple, Dict, Union

import numpy as np
import pandas as pd
import requests
from fastapi import UploadFile, HTTPException
from fastapi.responses import FileResponse
from starlette.status import HTTP_400_BAD_REQUEST, HTTP_404_NOT_FOUND

from datalayer.common import Common
from db.client2 import msdb_manager

async def excel_to_pandas (file : UploadFile) -> pd.DataFrame:
    if file.content_type not in ["application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                                  "application/vnd.ms-excel"]:
        raise HTTPException(status_code=400, detail="Invalid file type. Please upload an Excel file (.xlsx, .xls).")
    try:
        contents = await file.read()
        file_stream = io.BytesIO(contents)

        # Use the correct engine based on the file type
        if file.filename.endswith('.xlsx'):
            df = pd.read_excel(file_stream, engine='openpyxl', skiprows=2)
        else: # Assumes .xls
            df = pd.read_excel(file_stream, engine='xlrd', skiprows=2)
        return df
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"An error occurred while processing the file: {str(e)}")

async def create_upload_file(file : UploadFile, upload_directory : str):
    if not os.path.exists(upload_directory):
        os.makedirs(upload_directory)

    file_name, file_extension = os.path.splitext(file.filename)

    # 중복 파일명 처리 로직
    # 현재 타임스탬프를 'YYYYMMDD_HHMMSS' 형식으로 생성
    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")

    # 새로운 파일명 생성 (원본 파일명 + 타임스탬프)
    unique_filename = f"{file_name}_{timestamp}{file_extension}"

    # 파일 저장 경로 설정
    file_location = os.path.join(upload_directory, unique_filename)

    # shutil.copyfileobj()를 사용하여 파일 객체 복사
    # 비동기적으로 처리하여 I/O 성능 향상
    try:
        with open(file_location, "wb") as buffer:
            shutil.copyfileobj(file.file, buffer)

        return {"filename": file.filename, "info": "The file was saved successfully."}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"An error occurred while processing the file: {str(e)}")


def clean_non_printable_chars(df: pd.DataFrame) -> pd.DataFrame:
    """
    간단하게, 일반적으로 사용되지 않는 ASCII 제어 문자를 모두 제거합니다.
    """
    # 1단계: 제거할 제어 문자 (Null Byte 포함)
    control_chars = r'[\x00-\x08\x0B\x0C\x0E-\x1F\x7F]'
    # 2단계: 제거할 특수 공백 문자 (NBSP, Narrow NBSP 등)
    # \xa0 (U+00A0: Non-breaking space)
    # \u202f (U+202F: Narrow non-breaking space)
    special_spaces = r'[\xa0\u202f]'

    # DataFrame의 모든 문자열(object/string) 컬럼에 적용
    for col in df.select_dtypes(include=['object', 'string']).columns:
        df[col] = df[col].replace({np.nan: ''})
        # 모든 데이터를 문자열로 명시적 변환
        df[col] = df[col].astype(str)
        # 제어 문자 제거 (Null Byte 포함)
        df[col] = df[col].str.replace(control_chars, '', regex=True)
        # 특수 공백 문자를 일반 공백(U+0020)으로 대체
        # Null Byte 외의 숨겨진 유니코드 공백을 처리합니다.
        df[col] = df[col].str.replace(special_spaces, ' ', regex=True)
        # 연속된 공백을 단일 공백으로 축소 (선택 사항이나 권장)
        # NBSP나 제어 문자를 공백으로 대체한 후 여러 공백이 생겼을 때 정리합니다.
        df[col] = df[col].str.replace(r'\s+', ' ', regex=True)
        # 양쪽 공백 제거
        df[col] = df[col].str.strip()
    return df

def set_mlink_push(empseqno: str, officecode: str, str_to_arr: str, contents: str):
    """
    링크 푸시 보내기, str_to_arr는 콤마(,)로 구분하여 여러명에게 보낼 수 있음
    """
    if not empseqno:
        return {"result" : "fail", "message": "empSeqNo가 없습니다."}
    if not officecode:
        return {"result" : "fail", "message": "officeCode가 없습니다."}

    if str_to_arr:
        send_all_yn = 'N'
        clean_arr = str_to_arr.rstrip(',')
        recipient_list: List[str] = [f"'{item.strip()}'" for item in clean_arr.split(',') if item.strip()]
        arr_emp_seq_no = ", ".join(recipient_list)
    else:
        send_all_yn = 'Y'

    # 보내는 사람 체크
    sender_chk = msdb_manager.fetch_all(Common.get_mlink_sender_chk(), params=(officecode, empseqno))
    if not sender_chk:
        return {"result": "fail", "message": "보내는 사람 오류"}

    if send_all_yn == 'Y':
        params = (officecode)
    else:
        params = (arr_emp_seq_no, officecode)

    # 받는 사람 체크
    receiver_chk = msdb_manager.fetch_all(Common.get_mlink_receiver_chk(send_all_yn), params=(empseqno, officecode))
    if sender_chk:
        sender_chk_str = [str(item['memberID']) for item in sender_chk]
        result_string = ",".join(sender_chk_str)

        sURL = "http://link.meditong.com:30500/noticePushUploader/executeNotice"
        html = contents.replace('"', '&quot;') + " [메디통] "
        encoded_html = urllib.parse.quote(html, safe='')
        param1 = f"newsSeq=1&sawonNo={result_string}&msg={encoded_html}"

        try:
            headers = {
                'Content-Type': 'application/x-www-form-urlencoded',
                'priority': 'high',
                'content_available': 'true'
            }

            response = requests.post(sURL, data=param1, headers=headers)
            if response.status_code != 200:
                return {"result": "fail", "message": "통신오류 발생"}
            else:
                if "success" in response.text:
                    return {"result": "succees", "message": "발송완료"}
                    print("ok:발송완료")  # & response.text
                else:
                    return {"result": "succees", "message": response.text}
        except requests.exceptions.RequestException as e:
            # Handle general request errors (e.g., DNS failure, connection refused)
            return {"result": "fail", "message": "통신오류 발생 (Request Exception: {e})"}
    else:
        return {"result": "fail", "message": "받는사람 사람 오류"}


def set_app_push(pushCode: str, pushSubCode: str, officeCode: str,
                 sendEmpSeqNo: str, sendMemberId: str, receiveEmpSeqNo: str,
                 pushMessage: str, orgPushMessage: str, linkUrl: str, linkCode: str):
    """
    앱 푸시 보내기, receiveEmpSeqNo는 콤마(,)로 구분하여 여러명에게 보낼 수 있음
    """
    if not sendEmpSeqNo:
        return {"result" : "fail", "message": "sendEmpSeqNo가 없습니다."}
    if not officeCode:
        return {"result" : "fail", "message": "officeCode가 없습니다."}
    if not sendMemberId:
        return {"result" : "fail", "message": "sendMemberId가 없습니다."}
    if not receiveEmpSeqNo:
        return {"result" : "fail", "message": "receiveEmpSeqNo가 없습니다."}

    # master에 정보 저장
    params = (orgPushMessage, officeCode, sendEmpSeqNo, sendMemberId, pushCode, pushSubCode, linkUrl, linkCode)
    push_master_result = msdb_manager.execute(Common.set_push_master(), params=params)

    if not push_master_result:
        return {"result": "fail", "message": "master 입력 오류"}
    else:
        # 입력된 max idx 값 가져오기
        max_idx = msdb_manager.fetch_one(Common.get_push_max_id())

        receive_empseq_no = receiveEmpSeqNo.split(',')

        push_user_params = [
            (max_idx, officeCode, emp_seq_no)
            for emp_seq_no in receive_empseq_no
        ]

        if push_user_params:
            push_user_result = msdb_manager.bulk_execute(Common.set_push_receiver(), params=push_user_params)
            if not push_user_result:
                return {"result": "fail", "message": "user 입력 오류 (bulk)"}

        user_device_key = msdb_manager.fetch_all(Common.get_user_device_key(receiveEmpSeqNo))

        # 4. 푸시 메시지 벌크 전송
        if user_device_key:
            m_status = 'N'
            push_message_params = [
                (sendEmpSeqNo, officeCode, pushMessage, gcm_id['DeviceKey'], max_idx, pushCode, pushSubCode, m_status)
                for gcm_id in user_device_key
            ]

            # print("aa : ", push_message_params)
            if push_message_params:
                set_push_result = msdb_manager.bulk_execute(Common.set_push_message(), params=push_message_params)
                if not set_push_result:
                    return {"result": "fail", "message": "push 입력 오류 (bulk)"}
                else:
                    return {"result": "success", "message": "push 발송 완료"}

    return {"result": "success", "message": "푸시 발송 대상이 없습니다."}



def send_roster_publish_push(
    year: int,
    month: int,
    recipients: List[str],
    office_code: str,
    sender_emp_seq_no: str,
    sender_member_id: str,
    test_prefix: str = "[Test발송]",
):
    """
    근무표 발행 시 공용 푸시 메시지를 전송합니다.

    - 인자: year(int, 예: 2024), month(int, 예: 5), recipients(List[str]), office_code(str),
      sender_emp_seq_no(str), sender_member_id(str), test_prefix(str)
    - 반환: set_app_push 결과 딕셔너리
    - 예외: 없음
    - 예시: send_roster_publish_push(2024, 5, ["11", "22"], "OFF01", "900", "admin01")
    """
    if not recipients:
        return {"result": "fail", "message": "receiveEmpSeqNo가 없습니다."}

    receive_emp_seq_no = ",".join(map(str, recipients))
    push_code = "P30"
    push_sub_code = "S01"
    push_message = (
        f"{year}년 {month}월 근무표 마감"
    )
    org_push_message = f"{year}년 {month}월 근무표 마감"

    return set_app_push(
        pushCode=push_code,
        pushSubCode=push_sub_code,
        officeCode=office_code,
        sendEmpSeqNo=sender_emp_seq_no,
        sendMemberId=sender_member_id,
        receiveEmpSeqNo=receive_emp_seq_no,
        pushMessage=push_message,
        orgPushMessage=org_push_message,
        linkUrl="",
        linkCode=f"ROSTER:{year}:{month:02d}",
    )


def send_wanted_request_push(
    year: int,
    month: int,
    recipients: List[str],
    office_code: str,
    sender_emp_seq_no: str,
    sender_member_id: str,
    deadline: datetime.datetime | None = None,
    test_prefix: str = "[Test발송]",
):
    """
    근무 희망(원티드) 작성 요청 발생 시 공용 푸시 메시지를 전송합니다.

    - 인자: year(int, 예: 2024), month(int, 예: 9), recipients(List[str]), office_code(str),
      sender_emp_seq_no(str), sender_member_id(str), deadline(datetime|None), test_prefix(str)
    - 반환: set_app_push 결과 딕셔너리
    - 예외: 없음
    - 예시: send_wanted_request_push(2024, 9, ["11", "22"], "OFF01", "900", "admin01")
    """
    if not recipients:
        return {"result": "fail", "message": "receiveEmpSeqNo가 없습니다."}

    if deadline:
        deadline_text = f" (마감 {deadline.strftime('%Y-%m-%d')})"
        deadline_part = f"{deadline.strftime('%m')}월 {deadline.strftime('%d')}일 까지"
    else:
        deadline_text = ""
        deadline_part = "마감일 없음"

    print('deadline : ', deadline)
    
    receive_emp_seq_no = ",".join(map(str, recipients))
    push_code = "P30"
    push_sub_code = "S02"
    
    push_message = (
        f"{year}년 {month}월 원티드 요청 ({deadline_part})"
    )
    org_push_message = push_message

    return set_app_push(
        pushCode=push_code,
        pushSubCode=push_sub_code,
        officeCode=office_code,
        sendEmpSeqNo=sender_emp_seq_no,
        sendMemberId=sender_member_id,
        receiveEmpSeqNo=receive_emp_seq_no,
        pushMessage=push_message,
        orgPushMessage=org_push_message,
        linkUrl="",
        linkCode="",
    )


def send_wanted_deadline_update_push(
    year: int,
    month: int,
    recipients: List[str],
    office_code: str,
    sender_emp_seq_no: str,
    sender_member_id: str,
    deadline: datetime.datetime | None = None,
):
    """
    원티드 마감일 변경 시 공용 푸시 메시지를 전송합니다.

    - 푸시 코드: P30 / S05
    - 메시지: "{year}년 {month}월 원티드 마감일 변경 ({마감일})"
    """
    if not recipients:
        return {"result": "fail", "message": "receiveEmpSeqNo가 없습니다."}

    deadline_part = f"{deadline.strftime('%m')}월 {deadline.strftime('%d')}일 까지" if deadline else "마감일 없음"
    receive_emp_seq_no = ",".join(map(str, recipients))
    push_message = f"{year}년 {month}월 원티드 마감일 변경 ({deadline_part})"

    return set_app_push(
        pushCode="P30",
        pushSubCode="S05",
        officeCode=office_code,
        sendEmpSeqNo=sender_emp_seq_no,
        sendMemberId=sender_member_id,
        receiveEmpSeqNo=receive_emp_seq_no,
        pushMessage=push_message,
        orgPushMessage=push_message,
        linkUrl="",
        linkCode="",
    )


def send_wanted_close_push(
    year: int,
    month: int,
    recipients: List[str],
    office_code: str,
    sender_emp_seq_no: str,
    sender_member_id: str,
):
    """
    원티드 마감 시 공용 푸시 메시지를 전송합니다.

    - 푸시 코드: P30 / S03
    - 메시지: "{year}년 {month}월 원티드 마감"
    """
    if not recipients:
        return {"result": "fail", "message": "receiveEmpSeqNo가 없습니다."}

    receive_emp_seq_no = ",".join(map(str, recipients))
    push_message = f"{year}년 {month}월 원티드 마감"

    return set_app_push(
        pushCode="P30",
        pushSubCode="S03",
        officeCode=office_code,
        sendEmpSeqNo=sender_emp_seq_no,
        sendMemberId=sender_member_id,
        receiveEmpSeqNo=receive_emp_seq_no,
        pushMessage=push_message,
        orgPushMessage=push_message,
        linkUrl="",
        linkCode="",
    )


def send_roster_republish_push(
    year: int,
    month: int,
    recipients: List[str],
    office_code: str,
    sender_emp_seq_no: str,
    sender_member_id: str,
):
    """
    근무표 마감 철회 후 재마감 시 공용 푸시 메시지를 전송합니다.

    - 푸시 코드: P30 / S04
    - 메시지: "{year}년 {month}월 근무표 재마감"
    """
    if not recipients:
        return {"result": "fail", "message": "receiveEmpSeqNo가 없습니다."}

    receive_emp_seq_no = ",".join(map(str, recipients))
    push_message = f"{year}년 {month}월 근무표 재마감"

    return set_app_push(
        pushCode="P30",
        pushSubCode="S04",
        officeCode=office_code,
        sendEmpSeqNo=sender_emp_seq_no,
        sendMemberId=sender_member_id,
        receiveEmpSeqNo=receive_emp_seq_no,
        pushMessage=push_message,
        orgPushMessage=push_message,
        linkUrl="",
        linkCode=f"ROSTER:{year}:{month:02d}",
    )


# ── assignment 관련 알림 (S06~S12) ──

_REASON_LABEL = {"파견": "파견", "병동이동": "병동이동", "휴직": "휴직", "프리셉티": "프리셉티"}


def send_assignment_created_push(
    nurse_name: str,
    reason: str,
    start_date: str,
    end_date: str,
    source_group_name: str,
    target_group_name: str | None,
    recipients: List[str],
    office_code: str,
    sender_emp_seq_no: str,
    sender_member_id: str,
):
    """배정 생성 알림 (S06). 대상 간호사 + source/target 관리자."""
    if not recipients:
        return {"result": "fail", "message": "receiveEmpSeqNo가 없습니다."}
    label = _REASON_LABEL.get(reason, reason)
    period = f"{start_date}~{end_date}"
    if target_group_name:
        push_message = f"{nurse_name} {label} 배정 ({period}, {source_group_name}→{target_group_name})"
    else:
        push_message = f"{nurse_name} {label} 배정 ({period})"
    return set_app_push(
        pushCode="P30", pushSubCode="S06",
        officeCode=office_code,
        sendEmpSeqNo=sender_emp_seq_no,
        sendMemberId=sender_member_id,
        receiveEmpSeqNo=",".join(map(str, recipients)),
        pushMessage=push_message, orgPushMessage=push_message,
        linkUrl="", linkCode="",
    )


def send_assignment_cancelled_push(
    nurse_name: str,
    reason: str,
    source_group_name: str,
    target_group_name: str | None,
    recipients: List[str],
    office_code: str,
    sender_emp_seq_no: str,
    sender_member_id: str,
):
    """배정 취소 알림 (S07). 대상 간호사 + source/target 관리자."""
    if not recipients:
        return {"result": "fail", "message": "receiveEmpSeqNo가 없습니다."}
    label = _REASON_LABEL.get(reason, reason)
    if target_group_name:
        push_message = f"{nurse_name} {label} 배정 취소 ({source_group_name}→{target_group_name})"
    else:
        push_message = f"{nurse_name} {label} 배정 취소"
    return set_app_push(
        pushCode="P30", pushSubCode="S07",
        officeCode=office_code,
        sendEmpSeqNo=sender_emp_seq_no,
        sendMemberId=sender_member_id,
        receiveEmpSeqNo=",".join(map(str, recipients)),
        pushMessage=push_message, orgPushMessage=push_message,
        linkUrl="", linkCode="",
    )


def send_transfer_completed_push(
    nurse_name: str,
    target_group_name: str,
    recipients: List[str],
    office_code: str,
    sender_emp_seq_no: str,
    sender_member_id: str,
):
    """병동이동 완료 알림 (S08). 대상 간호사 + target 관리자."""
    if not recipients:
        return {"result": "fail", "message": "receiveEmpSeqNo가 없습니다."}
    push_message = f"{nurse_name} 병동이동 완료 ({target_group_name} 배속)"
    return set_app_push(
        pushCode="P30", pushSubCode="S08",
        officeCode=office_code,
        sendEmpSeqNo=sender_emp_seq_no,
        sendMemberId=sender_member_id,
        receiveEmpSeqNo=",".join(map(str, recipients)),
        pushMessage=push_message, orgPushMessage=push_message,
        linkUrl="", linkCode="",
    )


def send_assignment_roster_created_push(
    nurse_name: str,
    group_name: str,
    year: int,
    month: int,
    recipients: List[str],
    office_code: str,
    sender_emp_seq_no: str,
    sender_member_id: str,
):
    """근무표 생성 알림 — assignment 대상자용 (S09/S10 공용)."""
    if not recipients:
        return {"result": "fail", "message": "receiveEmpSeqNo가 없습니다."}
    push_message = f"{group_name} {year}년 {month}월 근무표 생성 (배정 근무자: {nurse_name})"
    return set_app_push(
        pushCode="P30", pushSubCode="S09",
        officeCode=office_code,
        sendEmpSeqNo=sender_emp_seq_no,
        sendMemberId=sender_member_id,
        receiveEmpSeqNo=",".join(map(str, recipients)),
        pushMessage=push_message, orgPushMessage=push_message,
        linkUrl="", linkCode=f"ROSTER:{year}:{month:02d}",
    )


def send_assignment_roster_published_push(
    group_name: str,
    year: int,
    month: int,
    recipients: List[str],
    office_code: str,
    sender_emp_seq_no: str,
    sender_member_id: str,
    is_source: bool = True,
):
    """근무표 마감 알림 — 상대 그룹 관리자 + 대상 간호사용 (S11/S12).

    is_source=True: source 마감 → target 관리자에게 (S11)
    is_source=False: target 마감 → source 관리자에게 (S12)
    """
    if not recipients:
        return {"result": "fail", "message": "receiveEmpSeqNo가 없습니다."}
    sub_code = "S11" if is_source else "S12"
    push_message = f"{group_name} {year}년 {month}월 근무표 마감 (배정 근무자 포함)"
    return set_app_push(
        pushCode="P30", pushSubCode=sub_code,
        officeCode=office_code,
        sendEmpSeqNo=sender_emp_seq_no,
        sendMemberId=sender_member_id,
        receiveEmpSeqNo=",".join(map(str, recipients)),
        pushMessage=push_message, orgPushMessage=push_message,
        linkUrl="", linkCode=f"ROSTER:{year}:{month:02d}",
    )


def set_sms(userPhoneNumber: str, sendPhoneNumber: str, smsMessage: str):
    # Unique key create
    random_part = random.randint(10000, 99999)
    now = datetime.datetime.now()
    date_time_part = (
        f"{now.year}"
        f"{now.month}"
        f"{now.day}"
        f"{now.hour}"
        f"{now.minute}"
        f"{now.second}"
    )
    s_unique_id = f"{date_time_part}{random_part}_0"

    prams = (s_unique_id, s_unique_id, userPhoneNumber, sendPhoneNumber, smsMessage)
    # print("prams : ", prams)
    # print("query : ", Common.set_sms_message())

    sms_result = msdb_manager.execute(Common.set_sms_message(), params=prams)

    if not sms_result:
        return {"result": "fail", "message": "sms 발송 오류"}
    else:
        return {"result": "succeed", "message": "sms 발송 완료"}


async def save_uploaded_files(
        files: Union[List[UploadFile], UploadFile, None], # 입력 타입을 List, 단일 객체, None으로 확장
        root_upload_dir: Path,  # 파일이 저장될 루트 디렉토리 (인수로 받음)
        file_type: Literal["all", "image", "video", "document"] = "all",
        use_size_limit: bool = True,
        max_total_size_bytes: int = 100 * 1024 * 1024,  # 전체 용량 제한 (인수로 받거나, 내부 기본값 사용으로 기본 100MB)
) -> Tuple[str, List[str]]:
    # 1. 파일이 없는 경우 즉시 반환
    if files is None:
        return str(root_upload_dir), []

    # 2. 단일 UploadFile 객체가 전달된 경우, 리스트로 변환하여 for 루프를 돌 수 있도록 합니다. (핵심 수정)
    if not isinstance(files, list):
        files = [files]

    IMAGE_EXTENSIONS = [
        "jpg", "jpeg", "png", "gif", "webp", "tiff", "ico", "bmp",
        "svg", "psd", "ai", "indd", "raw", "cr2", "nef"
    ]

    VIDEO_EXTENSIONS = [
        "mp4", "mov", "avi", "webm", "mkv", "flv", "wmv", "3gp",
        "mts", "m4v"
    ]

    DOCUMENT_EXTENSIONS = [
        "pdf", "docx", "xlsx", "xls", "ppt", "pptx", "txt",
        "hwp", "odt", "ods", "odp", "csv", "rtf", "epub",
        "zip", "rar", "7z"
    ]

    # 1-2. 파일 종류별 허용 확장자 딕셔너리 구성
    ALLOWED_EXTENSIONS_BASE = {
        "image": IMAGE_EXTENSIONS,
        "video": VIDEO_EXTENSIONS,
        "document": DOCUMENT_EXTENSIONS,
    }

    # "all" 타입 정의: 모든 확장자를 합치고 중복 제거 (list comprehension과 set 사용)
    all_extensions_set = set()
    for ext_list in ALLOWED_EXTENSIONS_BASE.values():
        all_extensions_set.update(ext_list)

    ALLOWED_EXTENSIONS = ALLOWED_EXTENSIONS_BASE
    ALLOWED_EXTENSIONS["all"] = list(all_extensions_set)

    # 실행 파일로 간주하여 차단할 확장자
    FORBIDDEN_EXTENSIONS = [
        "exe", "bat", "sh", "cmd", "ps1", "vbs", "jar", "py", "php", "asp", "aspx", "jsp", "cgi"
    ]

    # 저장 경로 설정 및 생성
    target_dir = Path(root_upload_dir)
    target_dir.mkdir(parents=True, exist_ok=True)

    saved_file_names = []

    # 용량 제한 검사
    if use_size_limit:
        total_size = 0

        for file in files:
            # 전체 내용을 읽고 크기 측정
            content = await file.read()
            file_size = len(content)
            total_size += file_size

            # 파일 포인터를 다시 처음(0)으로 되돌림 (다음 루프에서 다시 읽을 수 있도록)
            await file.seek(0)

        if total_size > max_total_size_bytes:
            raise HTTPException(
                status_code=HTTP_400_BAD_REQUEST,
                detail=f"전체 파일 용량이 제한({max_total_size_bytes / 1024 / 1024:.2f}MB)을 초과했습니다."
            )

    # 파일 처리 및 저장
    for file in files:
        original_name = Path(file.filename)
        extension = original_name.suffix[1:].lower()

        # 실행 파일 차단
        if extension in FORBIDDEN_EXTENSIONS:
            raise HTTPException(
                status_code=HTTP_400_BAD_REQUEST,
                detail=f"보안 정책상 실행 파일({extension})은 업로드 할 수 없습니다."
            )

        # 파일 종류 필터링
        if extension not in ALLOWED_EXTENSIONS.get(file_type, []):
            allowed_list = ", ".join(ALLOWED_EXTENSIONS.get(file_type, []))
            raise HTTPException(
                status_code=HTTP_400_BAD_REQUEST,
                detail=f"'{file_type}' 종류에서 허용되지 않는 파일 형식({extension})입니다. 허용 목록: {allowed_list}"
            )

        # 파일명 변경 (원본 이름 + 타임스탬프)
        timestamp = int(time.time() * 1000)
        new_file_name = f"{original_name.stem}_{timestamp}{original_name.suffix}"

        # 파일 저장
        file_path = target_dir / new_file_name

        try:
            content = await file.read()
            with open(file_path, "wb") as f:
                f.write(content)
            saved_file_names.append(new_file_name)
        except Exception as e:
            raise HTTPException(
                status_code=500, detail=f"파일 저장 중 오류가 발생했습니다: {e}"
            )
        finally:
            await file.close()

    return str(target_dir), saved_file_names


def delete_files(
        file_names: Union[List[str], str], # 입력 타입을 List[str] 또는 str로 확장
        root_upload_dir: Path,
) -> Dict[str, List[str]]:

    successful_deletions = []
    failed_deletions = []

    # file_names가 리스트가 아닌 단일 문자열로 전달된 경우, 리스트로 변환합니다.
    if isinstance(file_names, str):
        file_names = [file_names]
    elif file_names is None:
        return {
            "deleted_files": [],
            "failed_to_delete": []
        }

    # 절대 경로 객체 생성
    absolute_storage_path = Path(root_upload_dir)

    # 삭제하려는 폴더가 GLOBAL_UPLOAD_ROOT의 하위에 있는지 확인
    try:
        if not absolute_storage_path.is_relative_to(root_upload_dir):
            raise HTTPException(
                status_code=HTTP_404_NOT_FOUND,
                detail="제공된 파일 경로는 허용된 루트 디렉토리 외부에 있습니다. (보안 오류)"
            )
    except AttributeError:
        pass

    # 파일 삭제 처리
    for file_name in file_names:
        file_to_delete = absolute_storage_path / file_name

        # 파일이 실제로 존재하고, 그것이 파일인지 확인 (디렉토리 삭제 방지)
        if file_to_delete.exists() and file_to_delete.is_file():
            try:
                os.remove(file_to_delete)
                successful_deletions.append(file_name)
            except Exception as e:
                failed_deletions.append(f"{file_name} (오류: {e})")
        else:
            # 파일이 존재하지 않는 경우
            failed_deletions.append(f"{file_name} (오류: 파일을 찾을 수 없거나 디렉토리입니다.)")

    return {
        "deleted_files": successful_deletions,
        "failed_to_delete": failed_deletions
    }


def download_file(
        stored_file_name: str,
        root_upload_dir: str,
        download_as: Literal["original", "stored"] = "original",
) -> FileResponse:

    # 절대 경로 객체 생성 및 파일 경로 조합
    absolute_storage_path = Path(root_upload_dir)
    file_path = absolute_storage_path / stored_file_name

    try:
        # 파일 경로가 허용된 루트 디렉토리의 하위에 있는지 확인
        if not file_path.is_relative_to(root_upload_dir):
            raise HTTPException(
                status_code=HTTP_404_NOT_FOUND,
                detail="제공된 파일 경로가 허용된 루트 디렉토리 외부에 있습니다. (보안 오류)"
            )
    except AttributeError:
        pass

    # 파일 존재 여부 및 파일 타입 확인
    if not file_path.exists() or not file_path.is_file():
        raise HTTPException(
            status_code=HTTP_404_NOT_FOUND,
            detail="요청한 파일을 찾을 수 없거나 접근할 수 없습니다."
        )

    # 다운로드 파일명 설정
    if download_as == "stored":
        # 저장된 파일명으로 다운로드
        download_name = stored_file_name
    else:
        # 파일 확장자 분리
        suffix = Path(stored_file_name).suffix

        # 확장자를 제외한 파일명
        stem_with_timestamp = stored_file_name.removesuffix(suffix)

        # 정규 표현식을 사용하여 원본 파일명 만들기
        original_stem = re.sub(r'_\d{10,}$', '', stem_with_timestamp)

        # 원본 파일명 복원
        download_name = original_stem + suffix

    return FileResponse(
        path=file_path,
        filename=download_name,
        media_type='application/octet-stream'  # 일반적인 파일 다운로드에 사용
    )