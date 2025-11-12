import datetime
import os

import numpy as np
import pandas as pd
import requests
from fastapi import APIRouter, Depends, Request, UploadFile, File, HTTPException
from fastapi.templating import Jinja2Templates
from starlette.responses import FileResponse

from datalayer.setting import Setting
from db.client2 import msdb_manager
from routers.auth import get_current_user_from_cookie
from schemas.auth_schema import User as UserSchema
from utils import utils
from utils.security import create_access_token

router = APIRouter()

templates = Jinja2Templates(directory="templates")
DOWNLOAD_FOLDER = "downloads"


@router.get("/member_upload", summary="회원 엑셀 업로드 화면을 출력합니다.")
def excelupload_form(request: Request, current_user: UserSchema = Depends(get_current_user_from_cookie)):
    OfficeCode = current_user.office_id
    EmpSeqNo = current_user.nurse_id

    filename = 'easysetting_member.xls'
    rows = msdb_manager.fetch_all(Setting.list_member(), params=(OfficeCode, EmpSeqNo))

    return templates.TemplateResponse("member_excel.html", {"request": request, "filename": filename})


@router.post("/member_upload", summary="회원 엑셀을 DB에 저장합니다.")
async def create_upload_file(current_user: UserSchema = Depends(get_current_user_from_cookie), file: UploadFile = File(...)):
    """
    **회원 엑셀파일을 업로드 하여 DB에 저장합니다.**
    - 양식 엑셀파일 : easysetting_member.xls
    - 사번, 회원 아이디, 이름, 성별, 생년월일, 입사년월, 전화번호, 휴대폰 번호, 이메일, 주소, 부서장, 상위부서, 하위부서1, 하위부서2, 직위, 경력, 직무, 수간호사여부, 킵여부 으로 구성된 엑셀파일을 업로드 합니다.
    - 사번 중복체크 하여 첫번째 내용을 제외하고 나머지는 제거
    - 회원 아이디 중복체크 하여 첫번째 내용을 제외하고 나머지는 제거
    """

    # 쿠키값에서 가져오도록 수정
    OfficeCode = current_user.office_id
    EmpSeqNo = current_user.nurse_id
    RegDate = datetime.datetime.now()

    # excel type을 확인해서 pandas로 변환해주는 함수 : excel_to_pandas
    df = await utils.excel_to_pandas(file)
    df['num'] = range(1, len(df) + 1)

    # 엑셀컬럼을 영문으로 수정
    df = df.rename(columns={'사번': 'EmpNum', '회원 아이디': 'MemberID', '이름': 'EmployeeName', '성별': 'Gender', '생년월일': 'Birthday', '입사년월': 'JoinDate', '전화번호': 'Tel'
        , '휴대폰 번호': 'PortableTel', '이메일': 'Ck_Email', '주소': 'address', '부서장': 'Manager', '상위부서': 'Depth1', '하위부서1': 'Depth2', '하위부서2': 'Depth3', '직위': 'position'
        , '경력': 'career', '직무': 'duty', '수간호사여부': 'headnurse', '킵여부': 'nightkeep'})
    df['Birthday'] = df['Birthday'].astype(int)
    df['career'] = df['career'].astype(int)

    # 중복데이터 체크
    duplicates = df[df.duplicated()]
    # 필수값 Null 체크
    null_df = df[['EmpNum', 'MemberID', 'EmployeeName', 'Gender', 'Birthday', 'JoinDate', 'Tel', 'PortableTel', 'Ck_Email',
         'address', 'Depth1', 'position', 'career', 'duty', 'headnurse']].isna().any(axis=1)
    filtered_df = df[null_df]

    # 전화번호 패턴
    pattern_tel = r'^\d{3}-\d{4}-\d{4}$'
    no_phone_mask = ~df[['Tel', 'PortableTel']].astype(str).apply(lambda x: x.str.match(pattern_tel)).any(axis=1)
    filtered_phone_df = df[no_phone_mask]

    # 이메일 패턴
    pattern_email = r"^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$"
    no_email_mask = ~df[['Ck_Email']].astype(str).apply(lambda x: x.str.match(pattern_email)).any(axis=1)
    filtered_email_df = df[no_email_mask]

    # 생일패턴
    pattern_birth = r"^\d{8}$"
    no_birth_mask = ~df[['Birthday']].astype(str).apply(lambda x: x.str.match(pattern_birth)).any(axis=1)
    filtered_birth_df = df[no_birth_mask]

    # 날짜패턴
    pattern_date = r'^\d{4}-\d{2}-\d{2}$'
    no_date_mask = ~df[['JoinDate']].astype(str).apply(lambda x: x.str.match(pattern_date)).any(axis=1)
    filtered_date_df = df[no_date_mask]

    # 성별
    no_gender_mask = ~df['Gender'].isin(['남', '여'])
    filtered_gender_df = df[no_gender_mask]

    # 부서장
    manager_mask = ~(df['Manager'].isin(['부서장']) | df['Manager'].isnull())
    filtered_manager_df = df[manager_mask]

    # Y,N만
    df['headnurse'] = df['headnurse'].str.upper()
    no_headnurse_mask = ~df['headnurse'].isin(['Y', 'N'])
    filtered_headnurse_df = df[no_headnurse_mask]

    #숫자만
    pattern_num = r"^\d+$"
    no_num_mask = ~df[['career']].astype(str).apply(lambda x: x.str.match(pattern_num)).any(axis=1)
    filtered_num_df = df[no_num_mask]

    # depth1 체크
    depth1_data = msdb_manager.fetch_all(Setting.select_division_depth1(), params=OfficeCode)
    depth1_chdata = [item['depth1'] for item in depth1_data]
    no_depth1_mask = ~df['Depth1'].isin(depth1_chdata)
    filtered_depth1_df = df[no_depth1_mask]

    # depth2 체크
    depth2_data = msdb_manager.fetch_all(Setting.select_division_depth2(), params=OfficeCode)
    depth2_chdata = [item['depth2'] for item in depth2_data]
    no_depth2_mask = ~df['Depth2'].isin(depth2_chdata)
    filtered_depth2_df = df[no_depth2_mask]
    filtered_depth2_df = filtered_depth2_df[~filtered_depth2_df['Depth2'].isna()]

    # depth3 체크
    depth3_data = msdb_manager.fetch_all(Setting.select_division_depth3(), params=OfficeCode)
    depth3_chdata = [item['depth3'] for item in depth3_data]
    no_depth3_mask = ~df['Depth3'].isin(depth3_chdata)
    filtered_depth3_df = df[no_depth3_mask]
    filtered_depth3_df = filtered_depth3_df[~filtered_depth3_df['Depth3'].isna()]

    # 아이디 중복체크
    failed_id_list = []
    for row in df.itertuples():
        id_check = msdb_manager.fetch_one(Setting.member_id_check(), params=row.MemberID)

        if id_check:
            failed_id_list.append(row)
    failed_id_df = pd.DataFrame(failed_id_list)

    if not failed_id_df.empty:
        failed_id_df['Birthday'] = failed_id_df['Birthday'].astype(int)
        failed_id_df['career'] = failed_id_df['career'].astype(int)
        failed_id_df.drop('Index', axis=1, inplace=True)

    #위반사항 내용 전체 체크하기 위해서 병합 및 중복값 정리
    failed_combined_df = pd.concat(
        [duplicates, filtered_df, filtered_phone_df, filtered_email_df, filtered_birth_df, filtered_date_df,
         filtered_gender_df, filtered_manager_df, filtered_headnurse_df, filtered_num_df, filtered_depth1_df,
         filtered_depth2_df, filtered_depth3_df, failed_id_df], ignore_index=True)
    failed_combined_df = failed_combined_df.replace({np.nan: ''})
    failed_combined_df.drop_duplicates(inplace=True)

    # Bulk insert를 위해서 데이터 변환
    df = utils.clean_non_printable_chars(df)
    df = df.replace({np.nan: ''})
    df['OfficeCode'] = OfficeCode
    df['EmpSeqNo'] = EmpSeqNo
    df['RegDate'] = RegDate

    df = df[['num','OfficeCode', 'EmpSeqNo', 'EmpNum', 'MemberID', 'EmployeeName', 'Gender', 'Birthday', 'JoinDate',
       'Tel', 'PortableTel', 'Ck_Email', 'address', 'Manager', 'Depth1',
       'Depth2', 'Depth3', 'position', 'RegDate', 'career', 'duty', 'headnurse',
       'nightkeep']]

    data_to_insert = [tuple(row) for row in df.itertuples(index=False)]
    params = (OfficeCode, EmpSeqNo)

    if not failed_combined_df.empty:
        json_string = failed_combined_df.to_json(orient='records', force_ascii=False)
    else:
        # 기존에 Temp에 들어간 데이터 삭제
        delete_rows = msdb_manager.execute(Setting.delete_member(), params=params)
        if delete_rows is not None:
            rows_affected = msdb_manager.bulk_execute(Setting.insert_member(), data_to_insert)

            if rows_affected is not None:
                # 토큰 발행
                _clientId = os.getenv("CLINET_ID")
                _clientSecret = os.getenv("CLINET_SECRET")
                token = create_access_token(data={"clientSecret": _clientSecret, "clientId": _clientId})

                # API 호출 (엠웍스)
                #url = "http://localwgw.meditong.com/bizadmin/setting/member_excel_ai_ok.asp"
                url = "http://gw.meditong.com/bizadmin/setting/member_excel_ai_ok.asp"
                payload = "officeCode=" + OfficeCode + "&EmpSeqNo=" + EmpSeqNo + "&Token=" + token

                headers = {'Content-Type': 'application/x-www-form-urlencoded'}
                response = requests.post(url, data=payload, headers=headers)

                #결과 내용 확인
                print("test : ", response.text)
                if response.status_code == 200:
                    json_string = '{"result": succeed}'
                else:
                    json_string = '{"result": tmp inserted}'
            else:
                json_string = '{"result": insert fail}'
        else:
            json_string = '{"result": delete fail}'

    print(json_string)
    return json_string

@router.get("/member_download/{filename}", summary="회원 엑셀양식을 다운로드 합니다.")
async def download_file(filename: str):
    file_path = os.path.join(DOWNLOAD_FOLDER, filename)
    if os.path.exists(file_path):
        return FileResponse(
            path=file_path,
            media_type="application/octet-stream", # A generic binary file type
            filename=filename  # The name the file will be saved as
        )
    return HTTPException(status_code=404, detail=f"File not found")






