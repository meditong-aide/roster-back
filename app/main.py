import sys, os
import time
import json
import sys
from datetime import datetime, timezone
import boto3


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
from routers import events
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


# --- 진단용: /roster_create 계열 요청의 실제 호출자(IP/UA) 추적 ---
# 442171 유령 job 원인 추적. SQS 재전송 경로(SQS→Lambda→worker)는 이 미들웨어를
# 절대 거치지 않으므로: 여기에 로그가 찍히면 "살아있는 HTTP 재-POST" 라는 결정적 증거이고,
# 안 찍히는데도 슬랙 실패 알림이 계속 뜨면 "순수 SQS 재전송" 이 확정된다.
# ALB access log 부재로 최초 요청 IP 는 유실됐지만, 다음 HTTP 호출은 여기서 잡는다.
@app.middleware("http")
async def _trace_roster_create_callers(request: Request, call_next):
    path = request.url.path
    if "/roster_create/" in path:
        xff = request.headers.get("x-forwarded-for", "")
        # ALB 뒤에서는 request.client.host 가 LB IP → 진짜 클라이언트는 XFF 첫 항목.
        real_ip = xff.split(",")[0].strip() if xff else (
            request.client.host if request.client else "-"
        )
        # print 사용: 워커에서 print 는 CloudWatch 에 확실히 떴다(logging.warning 은 앱
        # 로깅 설정에 따라 stdout 으로 안 나갈 수 있음). flush=True 로 버퍼링 방지.
        print(
            f"[CallerTrace] {request.method} {path} "
            f"client_ip={real_ip} xff={xff!r} "
            f"ua={request.headers.get('user-agent', '-')!r} "
            f"referer={request.headers.get('referer', '-')!r} "
            f"cookie_present={'access_token' in (request.headers.get('cookie', '') or '')}",
            flush=True,
        )
    return await call_next(request)


# --- 호출별 사용자 액션 로그 (변경요청 → 단일 JSON 이벤트 → Firehose → S3 → Athena) ---
# 감사(audit) 아님, "사용자가 무슨 액션을 했나" 분석용. 요청 흐름을 절대 막지 않도록 전 구간 try/except.
# env(CALL_HISTORY_FIREHOSE_STREAM) 설정 시 그 스트림으로 Firehose→S3 (dev=dev스트림/prod=prod스트림·테이블 분리).
# 미설정(로컬)이면 stdout 으로만. CloudWatch 는 roster 생성 백로그 전용(call_history 로 오염 안 시킴). 앱 DB 무부하.
# PII 최소화: 본문 미저장(path+query 만), 민감 경로는 아래 prefix 로 제외.
_LOG_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
_LOG_EXCLUDE_PREFIX = ("/static", "/health", "/docs", "/openapi", "/favicon", "/redoc")


def _log_user_from_cookie(request: Request) -> dict:
    """access_token 쿠키(JWT) 디코드 → 유저 필드 (DB 히트 없음). 실패 시 {}."""
    raw = request.cookies.get("access_token") or ""
    tok = raw.replace("Bearer ", "").strip()
    if not tok:
        return {}
    try:
        from routers.auth import SECRET_KEY, ALGORITHM
        from jose import jwt as _jwt
        p = _jwt.decode(tok, SECRET_KEY, algorithms=[ALGORITHM])
        return {
            "account_id": p.get("account_id"), "nurse_id": p.get("nurse_id"),
            "name": p.get("name"), "office_id": p.get("office_id"),
            "group_id": p.get("group_id"),
            "role": p.get("hn_auth") or p.get("EmpAuthGbn"),
        }
    except Exception:
        return {}


# Firehose 직송(옵션): 스트림명 env 설정 시 S3(→Athena)로 전송. 미설정이면 CloudWatch print 만.
_firehose = boto3.client("firehose", region_name="ap-northeast-2")
_CALL_HISTORY_STREAM = os.getenv("CALL_HISTORY_FIREHOSE_STREAM")


def _firehose_put(line: str) -> None:
    """이벤트 1줄을 Firehose 로 전송(백그라운드 스레드). 실패는 로그만·요청 무영향."""
    try:
        _firehose.put_record(
            DeliveryStreamName=_CALL_HISTORY_STREAM,
            Record={"Data": line.encode("utf-8")},
        )
    except Exception as e:
        print(f"[call_history][WARN] Firehose put 실패: {e}", flush=True)


@app.middleware("http")
async def _call_action_logger(request: Request, call_next):
    start = time.perf_counter()
    response = await call_next(request)
    try:
        method = request.method
        path = request.url.path
        if method in _LOG_METHODS and not path.startswith(_LOG_EXCLUDE_PREFIX):
            xff = request.headers.get("x-forwarded-for", "")
            ip = xff.split(",")[0].strip() if xff else (
                request.client.host if request.client else None
            )
            u = _log_user_from_cookie(request)
            qs = str(request.url.query) or None
            # 단일 이벤트 형식(JSON 1줄) — Firehose 가 그대로 S3(→Athena)로 전달.
            #   log="call_history" 는 CloudWatch 구독필터 {$.log="call_history"} 매칭용 태그.
            event = {
                "log": "call_history",
                "ts": datetime.now(timezone.utc).isoformat(),
                "method": method,
                "path": path[:500],
                "query": (qs[:1000] if qs else None),
                "status": getattr(response, "status_code", None),
                "dur_ms": int((time.perf_counter() - start) * 1000),
                "account_id": u.get("account_id"),
                "nurse_id": u.get("nurse_id"),
                "name": u.get("name"),
                "office_id": u.get("office_id"),
                "group_id": u.get("group_id"),
                "role": (str(u.get("role"))[:20] if u.get("role") else None),
                "ip": (str(ip)[:64] if ip else None),
                "ua": (request.headers.get("user-agent", "") or "")[:300],
                "req_id": ((request.headers.get("x-amzn-trace-id") or "")[:80] or None),
            }
            line = json.dumps(event, ensure_ascii=False)
            if _CALL_HISTORY_STREAM:  # env 스트림으로 Firehose → S3 (dev/prod 각자 스트림·테이블)
                asyncio.create_task(asyncio.to_thread(_firehose_put, line + "\n"))
            else:  # env 미설정(로컬)일 때만 stdout
                print(line, flush=True)
    except Exception as e:
        print(f"[call_history][WARN] 로거 예외(무시): {e}", flush=True)
    return response


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
app.include_router(events.router)
app.include_router(weekly_off.router)  # 추가
app.include_router(grade.router)
app.include_router(jobs.router)
app.include_router(roster_precheck.router)

from routers import constraint_impact as constraint_impact_router
app.include_router(constraint_impact_router.router)

from routers import ontology as ontology_router
app.include_router(ontology_router.router)

from routers import nurse_period as nurse_period_router
app.include_router(nurse_period_router.router)

# Agent v2 test chat UI (dev-only — 별도 페이지에서 컨텍스트 수동 선택)
from agents_v2.test_chat_router import router as agent_test_router
app.include_router(agent_test_router)

# Agent v2 production chat — 인증 사용자 컨텍스트 자동, floating widget 호출용
from agents_v2.chat_router import router as agent_chat_router
app.include_router(agent_chat_router)

import uvicorn

if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True) 
