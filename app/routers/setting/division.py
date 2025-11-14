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

@router.get("/division_upload", summary="부서 엑셀 업로드 화면을 출력합니다.")
def excelupload_form(request: Request):

    filename = 'easysetting_division.xls'
    return templates.TemplateResponse("division_excel.html", {"request": request, "filename": filename})


@router.post("/division_upload", summary="부서 엑셀을 DB에 저장합니다.")
async def division_upload(
    current_user: UserSchema = Depends(get_current_user_from_cookie),
    file: UploadFile = File(...)
):
    OfficeCode = current_user.office_id
    EmpSeqNo = current_user.EmpSeqNo
    RegDate = datetime.datetime.now()

    # ===== 엑셀 → pandas =====
    df = await utils.excel_to_pandas(file)
    df["num"] = range(1, len(df) + 1)

    # 컬럼명 변경
    df = df.rename(columns={
        '부서명 (1depth)': 'Depth1',
        '부서명 (2depth)': 'Depth2',
        '부서명 (3depth)': 'Depth3'
    })

    # 문자열화 + NaN 제거 대비
    df = df.replace({np.nan: ''})

    # 오류 저장 리스트
    error_rows = []

    # 헬퍼: 오류 라벨링하여 리스트에 넣기
    def add_error(df_error, label):
        if df_error.empty:
            return
        temp = df_error.copy()
        temp["errorType"] = label
        error_rows.append(temp)
    # ---------------------------------------------------
    # 1) Depth1 없음 → 오류
    mask_depth1_null = df['Depth1'] == ''
    add_error(df[mask_depth1_null], "depth1_null")
    # ---------------------------------------------------
    # 2) Depth3은 있는데 Depth2 없음 → 오류
    mask_depth23_mismatch = (df['Depth3'] != '') & (df['Depth2'] == '')
    add_error(df[mask_depth23_mismatch], "depth23_mismatch")
    # ---------------------------------------------------
    # 3) 중복행 체크 (전체 row 기준)
    duplicates = df[df.duplicated(subset=['Depth1', 'Depth2', 'Depth3'], keep='first')]
    add_error(duplicates, "duplicate_row")
    # ---------------------------------------------------
    # 4) mb_partName 생성
    df['mb_partName'] = df['Depth1']
    df['mb_partName'] = np.where(df['Depth2'] != '',
                                 df['mb_partName'] + ',' + df['Depth2'],
                                 df['mb_partName'])
    df['mb_partName'] = np.where(df['Depth3'] != '',
                                 df['mb_partName'] + ',' + df['Depth3'],
                                 df['mb_partName'])
    # ---------------------------------------------------
    # 5) 이미 DB에 있는 mb_partName 중복
    exist = msdb_manager.fetch_all(Setting.list_division_exsist(), params=OfficeCode)
    exist_df = pd.DataFrame(exist)
    if not exist_df.empty:
        mask_exist = df['mb_partName'].isin(exist_df['mb_partName'])
        add_error(df[mask_exist], "already_exist")
    # ---------------------------------------------------
    # 6) Depth1 DB 중복 체크
    depth1_db = [x['depth1'] for x in msdb_manager.fetch_all(Setting.select_division_depth1(), params=OfficeCode)]
    mask_depth1_exist = df['Depth1'].isin(depth1_db)
    add_error(df[mask_depth1_exist], "depth1_exist")
    # ---------------------------------------------------
    # 7) Depth2 DB 중복 체크
    depth2_db = [x['depth2'] for x in msdb_manager.fetch_all(Setting.select_division_depth2(), params=OfficeCode)]
    mask_depth2_exist = (df['Depth2'] != '') & (df['Depth2'].isin(depth2_db))
    add_error(df[mask_depth2_exist], "depth2_exist")
    # ---------------------------------------------------
    # 8) Depth3 DB 중복 체크
    depth3_db = [x['depth3'] for x in msdb_manager.fetch_all(Setting.select_division_depth3(), params=OfficeCode)]
    mask_depth3_exist = (df['Depth3'] != '') & (df['Depth3'].isin(depth3_db))
    add_error(df[mask_depth3_exist], "depth3_exist")
    # ---------------------------------------------------
    # 9) 오류가 있다면 반환
    if error_rows:
        error_df = pd.concat(error_rows, ignore_index=True)
        error_df = error_df.replace({np.nan: ''})
        error_df.drop_duplicates(inplace=True)
        error_df = error_df[['num', 'Depth1', 'Depth2', 'Depth3', 'errorType']]
        return error_df.to_dict(orient='records')
    # ---------------------------------------------------
    # 10) 오류 없음 → DB 저장
    df['OfficeCode'] = OfficeCode
    df['EmpSeqNo'] = EmpSeqNo
    df['RegDate'] = RegDate
    df = df.replace({np.nan: ''})  # NaN 방어
    insert_cols = ['num', 'OfficeCode', 'EmpSeqNo', 'Depth1', 'Depth2', 'Depth3', 'RegDate']
    insert_df = df[insert_cols]
    params = (OfficeCode, EmpSeqNo)

    # 기존 데이터 삭제
    msdb_manager.execute(Setting.delete_division(), params=params)
    # bulk insert
    data_list = [tuple(row) for row in insert_df.itertuples(index=False)]
    rows = msdb_manager.bulk_execute(Setting.insert_division(), data_list)
    if not rows:
        return {"result": "insert_fail"}
    # 외부 API 호출
    token = create_access_token(data={
        "clientSecret": os.getenv("CLIENT_SECRET"),
        "clientId": os.getenv("CLIENT_ID")
    })
    response = requests.post(
        "https://gw.meditong.com/bizadmin/setting/division_excel_ai_ok.asp",
        data=f"officeCode={OfficeCode}&EmpSeqNo={EmpSeqNo}&Token={token}",
        headers={'Content-Type': 'application/x-www-form-urlencoded'}
    )
    if response.status_code == 200:
        return {"result": "succeed"}
    return {"result": "external_api_fail"}

# @router.post("/division_upload", summary="부서 엑셀을 DB에 저장합니다.")
# async def create_upload_file(current_user: UserSchema = Depends(get_current_user_from_cookie), file: UploadFile = File(...)):
#     """
#     **부서 엑셀파일을 업로드 하여 DB에 저장합니다.**
#     - 양식 엑셀파일 : easysetting_division.xls
#     - 부서명 (1depth), 부서명 (2depth), 부서명 (3depth) 으로 구성된 엑셀파일을 업로드 합니다.
#     - 부서명 (1depth) 컬럼에 내용이 없는 경우 제외
#     - 부서명 (3depth) 컬럼에 내용이 있는데 부서명 (2depth)에 내용이 없는 경우 제외
#     - 중복체크 하여 첫번째 내용을 제외하고 나머지는 제거
#     """
#     # 쿠키값에서 가져오도록 수정
#     OfficeCode = current_user.office_id
#     EmpSeqNo = current_user.EmpSeqNo
#     RegDate = datetime.datetime.now()

#     #print("OfficeCode : ", OfficeCode)

#     # excel type을 확인해서 pandas로 변환해주는 함수 : excel_to_pandas
#     df = await utils.excel_to_pandas(file)
#     df['num'] = range(1, len(df) + 1)

#     # 컬럼명 변경, DB입력하기 위해 이름 매칭
#     df = df.rename(columns={'부서명 (1depth)': 'Depth1', '부서명 (2depth)': 'Depth2', '부서명 (3depth)': 'Depth3'})

#     # 첫번째 부서명이 null 인경우
#     condition1 = df['Depth1'].isna()
#     # 세번째 부서명이 있는데 두번째 부서명이 null인 경우
#     condition2 = df['Depth3'].notna() & df['Depth2'].isna()

#     combined_condition = condition1 | condition2
#     failed_data = df[combined_condition]  # 문제되는 데이터
#     successful_data = df[~combined_condition]  # 정상 데이터

#     # 중복데이터 체크
#     duplicates = successful_data[successful_data.duplicated()]

#     # 문제데이터에 중복데이터 병합
#     failed_combined_df = pd.concat([failed_data, duplicates], ignore_index=True)

#     new_order = ['num', 'Depth1', 'Depth2', 'Depth3']
#     failed_combined_df = failed_combined_df[new_order]
#     failed_combined_df = failed_combined_df.replace({np.nan: ''})

#     # data = df_cleaned.to_dict(orient="records")
#     df['mb_partName'] = df['Depth1']
#     df['mb_partName'] = np.where(df['Depth2'] != '', df['mb_partName'] + ',' + df['Depth2'], df['mb_partName'])
#     df['mb_partName'] = np.where(df['Depth3'] != '', df['mb_partName'] + ',' + df['Depth3'], df['mb_partName'])

#     # 실제에 mb_partName과 비교하기 위해 처리
#     division_exsist = msdb_manager.fetch_all(Setting.list_division_exsist(), params=OfficeCode)
#     division_df = pd.DataFrame(division_exsist)
#     if not division_df.empty:
#         df_filtered = df[df['mb_partName'].isin(division_df['mb_partName'])]
#     else:
#         df_filtered = pd.DataFrame()

#     # depth1 체크
#     depth1_data = msdb_manager.fetch_all(Setting.select_division_depth1(), params=OfficeCode)
#     depth1_chdata = [item['depth1'] for item in depth1_data]
#     no_depth1_mask = df['Depth1'].isin(depth1_chdata)
#     filtered_depth1_df = df[no_depth1_mask]

#     # depth2 체크
#     depth2_data = msdb_manager.fetch_all(Setting.select_division_depth2(), params=OfficeCode)
#     depth2_chdata = [item['depth2'] for item in depth2_data]
#     no_depth2_mask = df['Depth2'].isin(depth2_chdata)
#     filtered_depth2_df = df[no_depth2_mask]

#     # depth3 체크
#     depth3_data = msdb_manager.fetch_all(Setting.select_division_depth3(), params=OfficeCode)
#     depth3_chdata = [item['depth3'] for item in depth3_data]
#     no_depth3_mask = df['Depth3'].isin(depth3_chdata)
#     filtered_depth3_df = df[no_depth3_mask]

#     # 위반사항 내용 전체 체크하기 위해서 병합 및 중복값 정리
#     failed_combined_df = pd.concat([failed_combined_df, df_filtered, filtered_depth1_df,
#          filtered_depth2_df, filtered_depth3_df], ignore_index=True)
#     failed_combined_df.drop_duplicates(inplace=True)

#     df['OfficeCode'] = OfficeCode
#     df['EmpSeqNo'] = EmpSeqNo
#     df['RegDate'] = RegDate
#     df = utils.clean_non_printable_chars(df)

#     if not failed_combined_df.empty:
#         json_string = failed_combined_df.to_json(orient='records', force_ascii=False)
#     else:
#         new_order = ['num', 'OfficeCode', 'EmpSeqNo', 'Depth1', 'Depth2', 'Depth3', 'RegDate']
#         matching_data = df[new_order]
#         matching_data = matching_data.replace({np.nan: ''})

#         # 기존에 Temp에 들어간 데이터 삭제
#         params = (OfficeCode, EmpSeqNo)
#         delete_rows = msdb_manager.execute(Setting.delete_division(), params=params)

#         if delete_rows is not None:
#             data_to_insert = [tuple(row) for row in matching_data.itertuples(index=False)]
#             rows_affected = msdb_manager.bulk_execute(Setting.insert_division(), data_to_insert)

#             #print("data_to_insert : ", data_to_insert)

#             if rows_affected is not None:
#                 # 토큰 발행
#                 _clientId = os.getenv("CLIENT_ID")
#                 _clientSecret = os.getenv("CLIENT_SECRET")
#                 token = create_access_token(data={"clientSecret": _clientSecret, "clientId": _clientId})

#                 # API 호출 (엠웍스)
#                 #url = "http://localwgw.meditong.com/bizadmin/setting/division_excel_ai_ok.asp"
#                 url = "https://gw.meditong.com/bizadmin/setting/division_excel_ai_ok.asp"
#                 payload = "officeCode=" + OfficeCode + "&EmpSeqNo=" + EmpSeqNo + "&Token=" + token
#                 headers = {'Content-Type': 'application/x-www-form-urlencoded'}
#                 response = requests.post(url, data=payload, headers=headers)

#                 # 결과 내용 확인
#                 print("test : ", response.text)
#                 if response.status_code == 200:
#                     json_string = '{"result": "succeed"}'
#                 else:
#                     json_string = '{"result": tmp inserted}'
#             else:
#                 json_string = '{"result": insert fail}'
#         else:
#             json_string = '{"result": delete fail}'

#     print(json_string)
#     return json_string

@router.get("/division_download/{filename}", summary="부서 엑셀양식을 다운로드 합니다.")
async def download_file(filename: str):
    file_path = os.path.join(DOWNLOAD_FOLDER, filename)
    if os.path.exists(file_path):
        return FileResponse(
            path=file_path,
            media_type="application/octet-stream", # A generic binary file type
            filename=filename  # The name the file will be saved as
        )
    return HTTPException(status_code=404, detail=f"File not found")


    



