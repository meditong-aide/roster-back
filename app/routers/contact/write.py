import datetime
from typing import Optional

from fastapi import APIRouter, Depends, Request, Form, HTTPException, status, File, UploadFile, BackgroundTasks
from fastapi.templating import Jinja2Templates
from fastapi_mail import MessageType  # MessageType도 필요합니다.

from datalayer.contact import Contact
from datalayer.member import Member
from db.client2 import msdb_manager
from routers.auth import get_current_user_from_cookie
from schemas.auth_schema import User as UserSchema
from utils.email import email_sender, EmailSchema
from utils.utils import save_uploaded_files

router = APIRouter()
templates = Jinja2Templates(directory="templates")

# 메세지 화면 출력
@router.get("/write", summary="고객문의 화면처리", description="고객문의 화면처리")
def message_write_form(request: Request,current_user: UserSchema = Depends(get_current_user_from_cookie)):
    OfficeCode = current_user.office_id
    account_id = current_user.account_id

    rows = msdb_manager.fetch_all(Member.member_view(), params=account_id)

    return templates.TemplateResponse("contact_write.html", {"request": request, "user_data" : rows[0]})

@router.post("/write", summary="고객문의 등록")
async def set_message(background_tasks: BackgroundTasks,current_user: UserSchema = Depends(get_current_user_from_cookie),
        # 기본 정보 (읽기 전용 포함)
        username: str = Form(...),
        PortableTel: str = Form(...),
        Email: str = Form(...),
        title: str = Form(...),
        contents: str = Form(...),
        # 첨부 파일 처리
        files: Optional[UploadFile] = File(None)
):
    """
    * 입력 항목
        * username: 이름
        * PortableTel: 연락처
        * Email: 이메일
        * title: 제목
        * contents: 내용
        * files: 첨부파일
    * 실 운영서버 반영 시 발송되는 이메일의 주소를 변경해서 반영해야 함.
    """
    if not title:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="제목을 입력해 주세요")

    if not contents:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="내용을 입력해 주세요")

    OfficeCode = current_user.office_id
    account_id = current_user.account_id

    rows = msdb_manager.fetch_all(Member.member_view(), params=account_id)

    if rows :
        office_name = rows[0]['office_name']
        mb_partName = rows[0]['mb_partName']
        OfficialTitleName = rows[0]['OfficialTitleName']
        ManageNo = rows[0]['manageno']
    else :
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="사용자 정보가 없습니다.")

    Category = "23"

    # 게시물 등록일자
    now = datetime.datetime.now()
    WriteDate = now.strftime("%Y-%m-%d %H:%M")

    #첨부파일
    GLOBAL_UPLOAD_ROOT = 'downloads/' + OfficeCode + '/contact'
    use_size_limit = 100 * 1024 * 1024  # 100MB

    uploaded_files = []
    storage_path = ""

    # 폼에서 파일이 첨부되었는지 확인
    if files and files.filename:
        try:
            # 파일 저장
            storage_path, uploaded_files = await save_uploaded_files(
                files=files,
                root_upload_dir=GLOBAL_UPLOAD_ROOT,
                file_type='all',
                use_size_limit=use_size_limit
            )
            file_name = uploaded_files
        except HTTPException as e:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="첨부파일 저장시 오류가 발생했습니다.")
        except Exception as e:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="첨부파일 저장시 오류가 발생했습니다.")
    else:
        file_name = ""

    # 데이터 입력
    params=(ManageNo, WriteDate, username, account_id, Category, username, PortableTel, contents, file_name, Email, title)
    insert_rows = msdb_manager.execute(Contact.set_contact(), params=params)

    if not insert_rows or insert_rows == 0:
        return {"result": "fail", "message": "오류가 발생하였습니다."}
    else:
        #이메일 발송하기
        message = f"""
                    <p>작성자 : {username}</p><br />
                    <p>작성일 : {WriteDate}</p><br />
                    <p>연락처 : {PortableTel}</p><br />
                    <p>이메일 : {Email}</p><br />
                    <p>요청사항 : {contents}</p><br />
                """
        body_data = {
            "message": message
        }
        email_object = EmailSchema(
            #실서버 반영시에는 이메일 주소는 변경해야 함.
            #email=["eun@eunkorea.com"],
            email=["dhsung@meditong.com"],
            body=body_data
        )

        try:
            subject = f"[고객문의 AI근무표] {title}"
            html_body = email_sender.create_email_body(email_object.body)

            # 클래스 인스턴스의 메서드 호출
            email_sender.send_in_background(
                background_tasks,
                subject=subject,
                recipients=email_object.email,
                html_body=html_body,
                subtype=MessageType.html
            )
        except Exception as e:
            # SMTP 연결 오류 등이 발생하면 500 에러를 반환
            raise HTTPException(status_code=500, detail=f"Error processing email request: {e}")

        return {"result": "success", "message": "고객문의가 등록되었습니다."}


