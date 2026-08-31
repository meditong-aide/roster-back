import logging

from fastapi import APIRouter, Depends, Form, HTTPException
from starlette import status

from datalayer.sticker import sticker
from db.client2 import msdb_manager
from routers.auth import get_current_user_from_cookie
from schemas.auth_schema import User as UserSchema

router = APIRouter()

logger = logging.getLogger(__name__)

#: 저장 실패 시 프론트가 한글 안내로 바꿔 쓰는 코드. 문구가 아니라 **코드**를 내려
#: 안내 문안이 바뀌어도 프론트 분기가 깨지지 않게 한다.
_ERR_SAVE_FAILED = "sticker_save_failed"


@router.post("/insert", summary="회원 해당 달 스티커 입력")
def sticker_in(current_user: UserSchema = Depends(get_current_user_from_cookie),
stcker_date: str = Form(...),
sticker_contents: str = Form(...)
):
    """
    * stcker_date: 스티커 날짜 ex)2025-07
    * sticker_contents: 스티커 데이터 ex) 0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0"
    * 반환값 : result, message
    *       result : success -> 성공

    실패 시 `detail` 은 **코드**(`sticker_save_failed`)다. DB 예외 원문을 그대로
    내보내면 쿼리·바인딩 값이 클라이언트로 새어 나가므로 서버 로그에만 남긴다.
    """

    # get_current_user_from_cookie 는 토큰이 없거나 만료면 예외가 아니라 None 을 준다.
    # 가드가 없으면 아래 역참조에서 AttributeError → 401 이어야 할 자리에 500 이 나간다.
    if not current_user:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="인증이 필요합니다.")

    EmpSeqNo = current_user.nurse_id
    OfficeCode = current_user.office_id

    if not stcker_date:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="스티커 날짜가 없습니다.")

    if not sticker_contents:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="스티커 데이터가 없습니다.")
    try:
        # 클릭할 때마다 스티커가 생기고 사라지도록 FrontEnd 가 **배열 전체**를 다시 보낸다.
        # 예전에는 DELETE 후 INSERT 로 처리했는데, 두 호출이 각각 커밋돼서
        # INSERT 가 실패하면 기존 스티커가 사라졌다. 한 배치 upsert 로 바꿔 그 창을 없앴다.
        msdb_manager.execute(
            sticker.upsert_sticker(),
            params=(sticker_contents, OfficeCode, EmpSeqNo, stcker_date,
                    OfficeCode, EmpSeqNo, stcker_date, sticker_contents),
        )

        return {"result": "success", "message": "스티커가 저장되었습니다."}

    except HTTPException:
        raise
    except Exception:
        logger.error(
            "스티커 저장 실패 office_id=%s nurse_id=%s stcker_date=%s",
            OfficeCode, EmpSeqNo, stcker_date, exc_info=True,
        )
        raise HTTPException(status_code=500, detail=_ERR_SAVE_FAILED)
