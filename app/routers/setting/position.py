import datetime
import os

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

# 메세지 리스트 조회 : mariadb_manager
@router.get("/position_upload", summary="직위 엑셀 업로드 화면을 출력합니다.")
def excelupload_form(request: Request, current_user: UserSchema = Depends(get_current_user_from_cookie)):
    OfficeCode = current_user.OfficeCode
    EmpSeqNo = current_user.EmpSeqNo

    nurse_id = current_user.nurse_id
    account_id = current_user.account_id
    group_id = current_user.group_id
    is_head_nurse = current_user.is_head_nurse
    name = current_user.name
    EmpAuthGbn = current_user.EmpAuthGbn


    print("OfficeCode :", OfficeCode)
    print("EmpSeqNo :", EmpSeqNo)
    print("nurse_id :", nurse_id)
    print("account_id :", account_id)
    print("group_id :", group_id)
    print("is_head_nurse :", is_head_nurse)
    print("name :", name)
    print("EmpAuthGbn :", EmpAuthGbn)


    filename = 'easysetting_position.xls'
    rows = msdb_manager.fetch_all(Setting.list_position(), params=(OfficeCode, EmpSeqNo))

    return templates.TemplateResponse("position_excel.html", {"request": request, "filename": filename})


@router.post("/position_upload", summary="직위 엑셀을 DB에 저장합니다.")
async def create_upload_file(current_user: UserSchema = Depends(get_current_user_from_cookie), file: UploadFile = File(...)):
    """
    **회원 엑셀파일을 업로드 하여 DB에 저장합니다.**
    - 양식 엑셀파일 : easysetting_member.xls
    - 직위명 으로 구성된 엑셀파일을 업로드 합니다.
    - 중복체크 하여 첫번째 내용을 제외하고 나머지는 제거
    - 회원 아이디 중복체크 하여 첫번째 내용을 제외하고 나머지는 제거
    """

    # 쿠키값에서 가져오도록 수정
    OfficeCode = current_user.OfficeCode
    EmpSeqNo = current_user.EmpSeqNo
    RegDate = datetime.datetime.now()

    # excel type을 확인해서 pandas로 변환해주는 함수 : excel_to_pandas
    df = await utils.excel_to_pandas(file)
    df['num'] = range(1, len(df) + 1)
    df = df.rename(columns={'직위명': 'Title'})

    # 직위명 null 인경우
    condition1 = df['Title'].isna()
    non_matching_data = df[~condition1]

    duplicates = non_matching_data[non_matching_data.duplicated()]

    # position 체크
    position_data = msdb_manager.fetch_all(Setting.position_check(), params=OfficeCode)
    position_chdata = [item['positionTitle'] for item in position_data]
    no_position_mask = df['Title'].isin(position_chdata)
    filtered_position_df = df[no_position_mask]

    # 위반사항 내용 전체 체크하기 위해서 병합 및 중복값 정리
    failed_combined_df = pd.concat([duplicates, filtered_position_df], ignore_index=True)
    failed_combined_df.drop_duplicates(inplace=True)

    if not failed_combined_df.empty:
        json_string = failed_combined_df.to_json(orient='records', force_ascii=False)
    else:
        non_matching_data['OfficeCode'] = OfficeCode
        non_matching_data['EmpSeqNo'] = EmpSeqNo
        non_matching_data['RegDate'] = RegDate

        new_order = ['num', 'OfficeCode', 'EmpSeqNo', 'Title', 'RegDate']
        non_matching_data = non_matching_data[new_order]
        non_matching_data = utils.clean_non_printable_chars(non_matching_data)

        # 데이터프레임을 bulkinsert할 형식으로 변환
        data_to_insert = [tuple(row) for row in non_matching_data.itertuples(index=False)]
        delete_rows = msdb_manager.execute(Setting.delete_position(), params=(OfficeCode, EmpSeqNo))

        if delete_rows is not None:
            rows_affected = msdb_manager.bulk_execute(Setting.insert_position(), data_to_insert)

            if rows_affected is not None:
                # 토큰 발행
                _clientId = os.getenv("CLIENT_ID")
                _clientSecret = os.getenv("CLIENT_SECRET")
                token = create_access_token(data={"clientSecret": _clientSecret, "clientId": _clientId})

                # API 호출 (엠웍스)
                #url = "http://localwgw.meditong.com/bizadmin/setting/position_excel_ai_ok.asp"
                url = "http://gw.meditong.com/bizadmin/setting/position_excel_ai_ok.asp"
                payload = "officeCode=" + OfficeCode + "&EmpSeqNo=" + EmpSeqNo + "&Token=" + token

                headers = {'Content-Type': 'application/x-www-form-urlencoded'}
                response = requests.post(url, data=payload, headers=headers)

                # 결과 내용 확인
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

@router.get("/position_download/{filename}", summary="회원 엑셀양식을 다운로드 합니다.")
async def download_file(filename: str):
    file_path = os.path.join(DOWNLOAD_FOLDER, filename)
    if os.path.exists(file_path):
        return FileResponse(
            path=file_path,
            media_type="application/octet-stream", # A generic binary file type
            filename=filename  # The name the file will be saved as
        )
    return HTTPException(status_code=404, detail=f"File not found")





