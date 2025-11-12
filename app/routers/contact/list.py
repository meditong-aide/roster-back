from fastapi import APIRouter, HTTPException, Depends, status

from datalayer.contact import Contact
from db.client2 import msdb_manager
from routers.auth import get_current_user_from_cookie
from schemas.auth_schema import User as UserSchema
from utils.utils import download_file

router = APIRouter()

@router.get("/listcnt", summary="총 게시물수")
def message_view(current_user: UserSchema = Depends(get_current_user_from_cookie)):
    """
    * 호출방식 : /contact/listcnt
    * 리턴값 : total_count
    """
    account_id = current_user.account_id

    rows = msdb_manager.fetch_all(Contact.get_contact_list_cnt(), params=account_id)

    if rows is None:
        raise HTTPException(status_code=500, detail="요청을 찾을 수 없습니다.")

    return [{
        "total_count": row['total_count']
    } for row in rows]


@router.get("/list", summary="메세지 리스트")
def message_view(page: int, pagesize: int, current_user: UserSchema = Depends(get_current_user_from_cookie)):
    """
    * page, pagesize -> get방식으로 전달
    * 호출방식 : /contact/list?page=1&pagesize=10
    """
    account_id = current_user.account_id

    rows = msdb_manager.fetch_all(Contact.get_contact_list(page, pagesize), params=account_id)

    if rows is None:
        raise HTTPException(status_code=500, detail="요청을 찾을 수 없습니다.")

    return [{
        "No": row['No'],
        "title": row['title'],
        "context": row['context'],
        "Writer": row['Writer'],
        "WriterID": row['WriterID'],
        "writeDate": row['writeDate'],
        "filename": row['filename'],
        "Tel": row['Tel'],
        "wEmail": row['wEmail'],
        "replycontent": row['replycontent'],
        "jobState": row['jobState']
    } for row in rows]

@router.get("/download", summary="파일 다운로드")
def message_view(filename: str, current_user: UserSchema = Depends(get_current_user_from_cookie)):
    """
    * filename -> get방식으로 전달
    * 호출방식 : /contact/download?filename=easysetting_division (1)_1762755447223.xls
    """
    OfficeCode = current_user.OfficeCode

    GLOBAL_UPLOAD_ROOT = 'downloads/' + OfficeCode + '/contact'

    if not filename:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="첨부파일이 없습니다.")

    return download_file(
        stored_file_name=filename,
        root_upload_dir=GLOBAL_UPLOAD_ROOT,
        download_as="original"
    )