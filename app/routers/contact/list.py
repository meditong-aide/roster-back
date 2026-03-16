from fastapi import APIRouter, HTTPException, Depends, status
from sqlalchemy.orm import Session

from datalayer.contact import Contact
from db.client2 import msdb_manager, get_db
from db.models import Notice
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

    * 리턴값 :
    *   "No": 번호
    *   "title": 제목
    *   "context": 내용
    *   "Writer": 작성자
    *    "WriterID": 아이디
    *    "writeDate": 작성일
    *    "filename": 첨부파일
    *    "Tel": 연락처
    *    "wEmail": 이메일
    *    "replycontent": 답변내용
    *    "jobState": 답변 상태 초기값 ("접수")
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
        "Comment": row['Comment'],         # 관리자 답변 내용
        "Manager": row['Manager'],         # 담당자
        "jobState": row['jobState']
    } for row in rows]

@router.get("/download", summary="파일 다운로드")
def message_view(filename: str, current_user: UserSchema = Depends(get_current_user_from_cookie)):
    """
    * filename -> get방식으로 전달
    * 호출방식 : /contact/download?filename=easysetting_division (1)_1762755447223.xls
    """
    OfficeCode = current_user.office_id

    GLOBAL_UPLOAD_ROOT = 'downloads/' + OfficeCode + '/contact'

    if not filename:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="첨부파일이 없습니다.")

    return download_file(
        stored_file_name=filename,
        root_upload_dir=GLOBAL_UPLOAD_ROOT,
        download_as="original"
    )


# ─── 공지사항 ─────────────────────────────────────────────

@router.get("/notice/list", summary="공지사항 목록")
def get_notice_list(
    page: int = 1,
    pagesize: int = 10,
    current_user: UserSchema = Depends(get_current_user_from_cookie),
    db: Session = Depends(get_db),
):
    if not current_user:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="인증이 필요합니다.")

    offset = (page - 1) * pagesize
    notices = (
        db.query(Notice)
        .order_by(Notice.is_pinned.desc(), Notice.created_at.desc())
        .offset(offset)
        .limit(pagesize)
        .all()
    )
    total = db.query(Notice).count()

    return {
        "total": total,
        "items": [
            {
                "id": n.id,
                "title": n.title,
                "author_name": n.author_name,
                "is_pinned": n.is_pinned,
                "created_at": n.created_at,
                "updated_at": n.updated_at,
            }
            for n in notices
        ],
    }


@router.get("/notice/detail/{notice_id}", summary="공지사항 상세")
def get_notice_detail(
    notice_id: int,
    current_user: UserSchema = Depends(get_current_user_from_cookie),
    db: Session = Depends(get_db),
):
    if not current_user:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="인증이 필요합니다.")

    notice = db.query(Notice).filter(Notice.id == notice_id).first()
    if not notice:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="공지사항을 찾을 수 없습니다.")

    return {
        "id": notice.id,
        "title": notice.title,
        "content": notice.content,
        "author_name": notice.author_name,
        "is_pinned": notice.is_pinned,
        "created_at": notice.created_at,
        "updated_at": notice.updated_at,
    }