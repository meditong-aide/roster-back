import sys, os
import sys
from datetime import datetime


sys.path.append(os.path.dirname(os.path.abspath(__file__)))
from routers import teams, groups, managed_groups
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
    roster_precheck,
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
            from services.assignment_service import (
                flush_all_pending_transfers,
                flush_expired_preceptees,
                flush_expired_dispatches,
                flush_expired_leaves,
                flush_pending_permanent_changes,
                reconcile_nurse_attrs,
            )
            from services.nurse_service import flush_resigned_nurses
            count = flush_all_pending_transfers(db)
            if count > 0:
                _scheduler_logger.info("[Scheduler] 병동이동 자동 flush: %d건", count)
            pte_count = flush_expired_preceptees(db)
            if pte_count > 0:
                _scheduler_logger.info("[Scheduler] 프리셉티 자동 해제: %d건", pte_count)
            disp_count = flush_expired_dispatches(db)
            if disp_count > 0:
                _scheduler_logger.info("[Scheduler] 파견 자동 디엑티브: %d건", disp_count)
            leave_count = flush_expired_leaves(db)
            if leave_count > 0:
                _scheduler_logger.info("[Scheduler] 휴직 자동 디엑티브: %d건", leave_count)
            pc_count = flush_pending_permanent_changes(db)
            if pc_count > 0:
                _scheduler_logger.info("[Scheduler] 영구 속성변경 발효: %d건", pc_count)
            res_count = flush_resigned_nurses(db)
            if res_count > 0:
                _scheduler_logger.info("[Scheduler] 퇴사자 자동 삭제: %d건", res_count)
            # Nurses 캐시 vs NurseAssignment effective 값 정합성 점검 (read-only)
            recon = reconcile_nurse_attrs(db)
            if recon.get("mismatch_count", 0) > 0:
                _scheduler_logger.warning(
                    "[Scheduler] reconcile 불일치 %d건 (검사 %d명) — 자동 동기화 없음, 운영자 확인 필요",
                    recon["mismatch_count"], recon["total_checked"],
                )
        except Exception as e:
            _scheduler_logger.error("[Scheduler] 자동 flush 실패: %s", e, exc_info=True)
        finally:
            db.close()


def _expire_stale_status(db, status_in: str, cutoff_seconds: int, message: str) -> int:
    """주어진 status 의 created_at + cutoff_seconds 초과 job 을 FAILED 로 전환.

    인자:
        db: DB 세션
        status_in: 대상 status (QUEUED 또는 RUNNING)
        cutoff_seconds: 경과 시간 임계 (초)
        message: error_message 에 기록할 사유
    반환:
        변경된 행 수
    """
    from datetime import timedelta
    from db.models import RosterJob

    now = datetime.now()
    return (
        db.query(RosterJob)
        .filter(
            RosterJob.status == status_in,
            RosterJob.created_at < now - timedelta(seconds=cutoff_seconds),
        )
        .update(
            {
                "status": "FAILED",
                "error_message": message,
                "updated_at": now,
            },
            synchronize_session=False,
        )
    )


def _sweep_stale_jobs(db) -> tuple[int, int]:
    """QUEUED 5분 / RUNNING 15분 초과 job 일괄 FAILED. (queued_count, running_count) 반환."""
    queued = _expire_stale_status(
        db, "QUEUED", 300, "QUEUED timeout (Lambda 미호출 추정)"
    )
    running = _expire_stale_status(
        db, "RUNNING", 900, "RUNNING timeout (worker 중단 추정)"
    )
    db.commit()
    return queued, running


async def _stale_jobs_janitor():
    """1분 주기로 stale job 정리. 무한 polling 차단용 안전장치.

    QUEUED 5분: Lambda 가 호출되지 않은 케이스 (SQS event source 이슈 등) 추정.
    RUNNING 15분: worker 가 RUNNING 진입 후 죽은 케이스 추정 (Lambda timeout 600s + retry 1회 마진).
    """
    while True:
        await asyncio.sleep(60)
        db = SessionLocal()
        try:
            queued, running = _sweep_stale_jobs(db)
            if queued or running:
                _scheduler_logger.warning(
                    "[Scheduler] stale jobs swept: queued→failed=%d, running→failed=%d",
                    queued,
                    running,
                )
        except Exception as e:
            _scheduler_logger.error(
                "[Scheduler] stale jobs sweep 실패: %s", e, exc_info=True
            )
        finally:
            db.close()


def _run_nurse_sync():
    """신규 간호사 동기화 1회 실행."""
    from services.nurse_sync_service import sync_new_nurses
    db = SessionLocal()
    try:
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


async def _daily_nurse_sync_scheduler():
    """서버 시작 시 즉시 1회 실행 후, 매일 새벽 3시에 반복 실행."""
    from datetime import timedelta

    _run_nurse_sync()

    while True:
        now = datetime.now()
        target = now.replace(hour=3, minute=0, second=0, microsecond=0)
        if target <= now:
            target += timedelta(days=1)
        await asyncio.sleep((target - now).total_seconds())
        _run_nurse_sync()


@asynccontextmanager
async def lifespan(app):
    flush_task = asyncio.create_task(_daily_flush_scheduler())
    sync_task = asyncio.create_task(_daily_nurse_sync_scheduler())
    janitor_task = asyncio.create_task(_stale_jobs_janitor())
    yield
    flush_task.cancel()
    sync_task.cancel()
    janitor_task.cancel()


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
app.include_router(managed_groups.router)
app.include_router(weekly_off.router)  # 추가
app.include_router(grade.router)
app.include_router(jobs.router)
app.include_router(roster_precheck.router)

from routers import constraint_impact as constraint_impact_router
app.include_router(constraint_impact_router.router)

from routers import ontology as ontology_router
app.include_router(ontology_router.router)

# Agent v2 test chat UI (dev-only — 별도 페이지에서 컨텍스트 수동 선택)
from agents_v2.test_chat_router import router as agent_test_router
app.include_router(agent_test_router)

# Agent v2 production chat — 인증 사용자 컨텍스트 자동, floating widget 호출용
from agents_v2.chat_router import router as agent_chat_router
app.include_router(agent_chat_router)

import uvicorn

if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True) 
