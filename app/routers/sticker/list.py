from fastapi import APIRouter, Depends, Request, Form
from db.client2 import msdb_manager
from db.client2 import mariadb_manager
from datalayer.message import Message

from routers.auth import get_current_user_from_cookie
from schemas.auth_schema import User as UserSchema

from fastapi.templating import Jinja2Templates
from starlette.responses import RedirectResponse
from starlette import status

router = APIRouter()

templates = Jinja2Templates(directory="templates")

# 메세지 리스트 조회 : mariadb_manager
@router.get("/list", summary="메세지 목록을 조회합니다.")
def sticker_list(request: Request):
    return templates.TemplateResponse("sticker.html", {"request": request})