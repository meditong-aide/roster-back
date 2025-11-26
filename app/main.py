import sys, os
import sys
from datetime import datetime


sys.path.append(os.path.dirname(os.path.abspath(__file__)))
from routers import teams, groups
from routers import daily_shift as daily_shift_router

import os
import sys

from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

sys.path.append(os.path.dirname(os.path.abspath(__file__)))
from routers import (
    roster,
    auth,
    nurses,
    dates,
    wanted,
    preferences,
    roster_create,
    shifts,
    health,
    dashboard,
    token,
    teams,
    groups,
    push,
)
from routers.contact import contact_router
from routers.message import message_router
from routers.sticker import sticker_router
from routers.setting import router as setting_router
from routers.member import member_router
import uvicorn
import warnings
from starlette.responses import RedirectResponse
from starlette import status

from db.client2 import SessionLocal
from services.wanted_service import close_expired_wanted

app = FastAPI()


origins = [
    "http://localhost:5173",
    "http://127.0.0.1:5173",
    "http://192.168.0.162:5173",
    "http://192.168.0.162:8000",
    "http://52.79.220.92:8000",
    "https://aide-om.meditong.com",
    "https://m-aide-om.meditong.com",
    "http://aide-om.meditong.com",
    "http://m-aide-om.meditong.com"
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,         # "*" 쓰지 말 것 (credentials 쓰면 불가)
    allow_credentials=True,        # 쿠키/세션 쓰면 True
    allow_methods=["*"],           # 또는 ["POST","GET","OPTIONS",...]
    allow_headers=["*"],           # Authorization, Content-Type 등 허용
)


app.mount("/static", StaticFiles(directory="app/static"), name="static")

app.include_router(contact_router)
app.include_router(message_router)
app.include_router(sticker_router)
app.include_router(setting_router)
app.include_router(member_router)
app.include_router(push.router)
app.include_router(token.router)
app.include_router(auth.router)
app.include_router(nurses.router)
# app.include_router(schedules.router)
app.include_router(roster.router)
app.include_router(dates.router) 
app.include_router(wanted.router) 
app.include_router(preferences.router) 
app.include_router(roster_create.router) 
app.include_router(shifts.router)
app.include_router(health.router)
app.include_router(dashboard.router)
app.include_router(daily_shift_router.router)
app.include_router(teams.router)
app.include_router(groups.router)

def close_expired_wanted_job() -> None:
    """
    APScheduler에서 주기적으로 호출되어 만료된 Wanted 상태를 'closed'로 변경합니다.

    exp_date가 현재 서버 시각보다 과거인 모든 Wanted 레코드를 찾아
    status를 'requested'에서 'closed'로 일괄 변경합니다.
    예를 들어 만료된 건이 5건이면 한 번의 실행에서 5건이 갱신됩니다.
    """
    db = SessionLocal()
    try:
        updated = close_expired_wanted(db)
        print(
            f"[{datetime.now().isoformat()}] Wanted 자동 마감 작업 실행, "
            f"갱신 건수: {updated}"
        )
    except Exception as exc:
        print(f"[{datetime.now().isoformat()}] Wanted 자동 마감 작업 오류: {exc}")
    finally:
        db.close()


@app.on_event("startup")
def start_scheduler() -> None:
    """
    FastAPI 앱 시작 시 APScheduler를 초기화하고 Wanted 자동 마감 잡을 등록합니다.

    현재 설정은 매일 00:00 서버 시각에 `close_expired_wanted_job`을 실행합니다.
    예를 들어 서버 시간이 2025-11-26 00:00이 되면 그 시점 기준으로 exp_date가 지난
    Wanted 들이 모두 'closed'로 변경됩니다.
    """
    scheduler = BackgroundScheduler()
    scheduler.add_job(
        close_expired_wanted_job,
        "cron",
        hour=0,
        minute=0,
    )
    scheduler.start()


import uvicorn

if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True) 
