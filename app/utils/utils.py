import datetime
import io
import os
import random
import shutil
import urllib.parse
from typing import List

import numpy as np
import pandas as pd
import requests
from fastapi import UploadFile, HTTPException

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
    비표시 문자를 찾는 정규 표현식:
    [^] - (Not)
    \x20-\x7E - 일반적인 인쇄 가능한 ASCII 문자 (공백부터 틸데까지)
    \t\n\r - 탭, 개행, 캐리지 리턴 (일반적으로 유지하고 싶은 문자)
    따라서, 이 범위에 속하지 않는 모든 문자를 찾습니다.

    좀 더 엄격하게 비표시 문자를 제거하려면, \p{C}에 해당하는 유니코드 범위를 사용합니다.
    일반적으로 [^\x00-\x7F] (확장 ASCII 및 유니코드) 또는
    [\x00-\x1F\x7F] (ASCII 제어 문자)를 제거하는 방법을 사용합니다.

    널리 사용되는 방법: ASCII 제어 문자 제거 (유지하고 싶은 \t, \n, \r 제외)
    \x00-\x08 (NULL, SOH, STX, ETX, EOT, ENQ, ACK, BEL, BS)
    \x0B-\x0C (VT, FF)
    \x0E-\x1F (SO, SI, DLE, DC1, DC2, DC3, DC4, NAK, SYN, ETB, CAN, EM, SUB, ESC, FS, GS, RS, US)
    \x7F (DEL)

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

        # push변수관련
        # data = {}
        # data["message"] = pushMessage
        # data["param"] = "code"

        receive_empseq_no = receiveEmpSeqNo.split(',')
        device_keys = []

        for emp_seq_no in receive_empseq_no:
            # 받는사람 저장
            params = (max_idx, officeCode, emp_seq_no)
            push_user_result = msdb_manager.execute(Common.set_push_receiver(), params=params)

            if not push_user_result:
                return {"result": "fail", "message": "user 입력 오류"}

            # 받는사람 로그인 아이디 찾기
            receive_member_id = msdb_manager.fetch_one(Common.set_push_receiver_member_id(), params=emp_seq_no)

            if receive_member_id:
                push_setting_result = msdb_manager.fetch_all(Common.set_push_receiver_pushyn(),
                                                             params=receive_member_id)

                for push_setting in push_setting_result:
                    push_yn = push_setting["PushYN"]
                    push_time_yn = push_setting["pushTimeYn"]
                    stime = push_setting["stime"]
                    etime = push_setting["etime"]
            else:
                push_yn = 'N'
                push_time_yn = 'N'
                stime = ''
                etime = ''

            # print("stime : ", stime)
            # print("etime : ", etime)

            if stime and etime and push_yn == "Y" and push_time_yn == "Y":
                try:
                    # stime/etime을 사용하여 시작 시간 객체 생성
                    start_time_obj = datetime.strptime(f"{stime}:00:00", "%H:%M:%S").time()

                    # etime을 사용하여 종료 시간 객체 생성
                    temp_end_dt = datetime.strptime(f"{etime}:00:00", "%H:%M:%S")

                    # 1초 빼기 (EndTime을 1초 뺀 값으로 설정)
                    end_time_adjusted_dt = temp_end_dt - datetime.timedelta(seconds=1)
                    end_time_obj = end_time_adjusted_dt.time()

                    # 현재 시간을 time 객체로 변환 (실제 사용 시에는 datetime.now().time() 사용 권장)
                    current_time_obj = datetime.now().time()

                    # 4. 시간 비교 (If currentTimeValue >= startTimeValue AND currentTimeValue <= endTimeValue)
                    if start_time_obj <= current_time_obj <= end_time_obj:
                        # 시간이 시간대 내에 있으면 PushYN은 'Y'를 유지
                        pass
                    else:
                        # Else
                        push_yn = "N"

                except ValueError as e:
                    # stime 또는 etime 형식이 잘못된 경우 처리
                    print(f"시간 형식 오류 발생: {e}")
                    push_yn = "N"

            # print("start_time_obj : ", start_time_obj)
            # print("end_time_obj : ", end_time_obj)

            if push_yn == "Y":
                # 받는사람 스마트 기기 Key값 가져오기
                user_device_key = msdb_manager.fetch_one(Common.get_user_device_key(), params=receive_member_id)

                if user_device_key:
                    device_keys.append(user_device_key)

        if device_keys:
            for gcm_id in device_keys:
                if gcm_id:
                    m_status = 'N'
                    params = (sendEmpSeqNo, officeCode, pushMessage, gcm_id, max_idx, pushCode, pushSubCode, m_status)
                    # print("sendEmpSeqNo : ", sendEmpSeqNo)
                    # print("officeCode : ", officeCode)
                    # print("pushMessage : ", pushMessage)
                    # print("gcm_id : ", gcm_id)
                    # print("max_idx : ", max_idx)
                    # print("pushCode : ", pushCode)
                    # print("pushSubCode : ", pushSubCode)
                    # print("m_status : ", m_status)

                    # push FCM 테이블에 데이터 입력
                    set_push_result = msdb_manager.execute(Common.set_push_message(), params=params)

                    if not set_push_result:
                        return {"result": "fail", "message": "push 입력 오류"}
                    else :
                        return {"result": "succees", "message": "push 발송 완료"}

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
