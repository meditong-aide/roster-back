import sys, os
import sys
from datetime import datetime


sys.path.append(os.path.dirname(os.path.abspath(__file__)))
from routers import teams, groups
from routers import daily_shift as daily_shift_router

import os
import sys

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
    weekly_off,  # 추가
    grade,
    jobs,
)
from routers.contact import contact_router
from routers import messages
from routers.sticker import sticker_router
from routers.setting import router as setting_router
from routers.member import member_router
import uvicorn
import warnings
from starlette.responses import RedirectResponse
from starlette import status

from contextlib import asynccontextmanager
import asyncio
import logging

from db.client2 import SessionLocal
from services.wanted_service import close_expired_wanted

_scheduler_logger = logging.getLogger("scheduler")


async def _daily_flush_scheduler():
    """매일 자정에 병동이동 flush 실행."""
    from datetime import timedelta
    while True:
        now = datetime.now()
        tomorrow = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        await asyncio.sleep((tomorrow - now).total_seconds())

        db = SessionLocal()
        try:
            from services.assignment_service import flush_all_pending_transfers, flush_expired_preceptees
            count = flush_all_pending_transfers(db)
            if count > 0:
                _scheduler_logger.info("[Scheduler] 병동이동 자동 flush: %d건", count)
            pte_count = flush_expired_preceptees(db)
            if pte_count > 0:
                _scheduler_logger.info("[Scheduler] 프리셉티 자동 해제: %d건", pte_count)
        except Exception as e:
            _scheduler_logger.error("[Scheduler] 자동 flush 실패: %s", e, exc_info=True)
        finally:
            db.close()


async def _daily_nurse_sync_scheduler():
    """매일 새벽 3시에 신규 간호사 자동 동기화 실행."""
    from datetime import timedelta
    while True:
        now = datetime.now()
        target = now.replace(hour=3, minute=0, second=0, microsecond=0)
        if target <= now:
            target += timedelta(days=1)
        await asyncio.sleep((target - now).total_seconds())

        db = SessionLocal()
        try:
            from services.nurse_sync_service import sync_new_nurses
            result = sync_new_nurses(db)
            if result["added"] > 0:
                _scheduler_logger.info(
                    "[Scheduler] 신규 간호사 동기화: %d건 추가", result["added"]
                )
            if result["errors"]:
                _scheduler_logger.warning(
                    "[Scheduler] 신규 간호사 동기화 오류: %s", result["errors"]
                )
        except Exception as e:
            _scheduler_logger.error(
                "[Scheduler] 신규 간호사 동기화 실패: %s", e, exc_info=True
            )
        finally:
            db.close()


@asynccontextmanager
async def lifespan(app):
    flush_task = asyncio.create_task(_daily_flush_scheduler())
    sync_task = asyncio.create_task(_daily_nurse_sync_scheduler())
    yield
    flush_task.cancel()
    sync_task.cancel()


app = FastAPI(lifespan=lifespan)


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
app.include_router(messages.router)
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
app.include_router(weekly_off.router)  # 추가
app.include_router(grade.router)
app.include_router(jobs.router)

import uvicorn

if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True) 
