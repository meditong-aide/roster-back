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
                flush_expired_dispatches,
                flush_expired_leaves,
                flush_pending_permanent_changes,
                reconcile_nurse_attrs,
            )
            count = flush_all_pending_transfers(db)
            if count > 0:
                _scheduler_logger.info("[Scheduler] 병동이동 자동 flush: %d건", count)
            # [도려내기] 프리셉티 만료 flush 제거 — nurse_preceptee_period as-of resolver 가 자동 처리.
            disp_count = flush_expired_dispatches(db)
            if disp_count > 0:
                _scheduler_logger.info("[Scheduler] 파견 자동 디엑티브: %d건", disp_count)
            leave_count = flush_expired_leaves(db)
            if leave_count > 0:
                _scheduler_logger.info("[Scheduler] 휴직 자동 디엑티브: %d건", leave_count)
            pc_count = flush_pending_permanent_changes(db)
            if pc_count > 0:
                _scheduler_logger.info("[Scheduler] 영구 속성변경 발효: %d건", pc_count)
            # [퇴사자 삭제 비활성화] 퇴사자는 nurses.resignation_date 로만 관리하고 레코드는 보존한다.
            #   월 명단 노출/미노출은 group_members_in_month 가 resignation_date 로 판정(퇴사月=표시,
            #   다음 달=제외). hard delete 하면 퇴사月 표시도 사라지고 데이터도 잃으므로 호출하지 않는다.
            # res_count = flush_resigned_nurses(db)
            # if res_count > 0:
            #     _scheduler_logger.info("[Scheduler] 퇴사자 자동 삭제: %d건", res_count)
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
# ★ 위 methods 는 "성공 요청" 기준이다. **실패(4xx/5xx·예외)는 method 무관하게 남긴다** —
#   GET 을 전량 남기면 이 로그의 목적(액션 분석)에 비해 양이 압도적이고 Firehose·S3 비용이
#   따라 붙는데, 정작 조사에 필요한 건 실패분이다.
#   2026-08-27 실측: 미인증 GET 7종이 401 아닌 **500** 으로 나가는데(가드 누락) 전부 기록이
#   없었다 — GET 미포함 + `call_next` 가 try 밖이라는 두 겹 때문이었다.
_LOG_FAILURE_FROM = 400

# 경로→(page/section/action) 라벨 + 요청본문 화이트리스트 카탈로그(의미 보강용).
import call_action_catalog as _catalog

_BODY_TAP_CAP = 32 * 1024  # 본문 캡처 상한(대용량 생성 payload 방어)


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
    # ★ `call_next` 를 try 안에 둔다. 밖에 두면 핸들러가 예외를 던졌을 때 **아래 기록 코드에
    #   도달조차 못 해** 정작 봐야 할 500 이 흔적 없이 사라진다(2026-08-27 실측).
    #   잡은 예외는 기록만 하고 **반드시 그대로 재전파**한다 — 여기서 삼키면 FastAPI 의
    #   예외 핸들러가 돌지 않아 응답 모양이 바뀐다.
    response = None
    exc: BaseException | None = None
    try:
        response = await call_next(request)
    except asyncio.CancelledError:
        raise  # 클라이언트 중단. 노이즈라 기록하지 않고 즉시 전파.
    except BaseException as e:
        exc = e
    try:
        method = request.method
        path = request.url.path
        status = getattr(response, "status_code", None) if response is not None else 500
        # 예외로 빠졌으면 아직 응답이 없다 — 실제로 나갈 값(500)으로 기록한다.
        failed = exc is not None or (status is not None and status >= _LOG_FAILURE_FROM)
        if ((method in _LOG_METHODS or failed)
                and not path.startswith(_LOG_EXCLUDE_PREFIX)):
            xff = request.headers.get("x-forwarded-for", "")
            ip = xff.split(",")[0].strip() if xff else (
                request.client.host if request.client else None
            )
            u = _log_user_from_cookie(request)
            qs = str(request.url.query) or None
            # ★ 이번 변경으로 **새로** 기록되는 요청(GET 등 비변경 메서드의 실패)은
            #   값을 버리고 **키 이름만** 남긴다. 정의되지 않은 경로도 404 로 여기 걸리므로
            #   `/foo?password=...` 처럼 **임의로 붙인 query 가 그대로 S3 에 쌓일 수 있다.**
            #   진단에는 "어떤 파라미터가 왔나" 로 충분하다(누락·오타는 키만으로 잡힌다).
            #   기존 변경 메서드(POST 등)는 손대지 않는다 — 동작이 바뀌면 회귀다.
            if qs and method not in _LOG_METHODS:
                qs = "&".join(sorted({p.split("=", 1)[0] for p in qs.split("&") if p})) or None
            # 단일 이벤트 형식(JSON 1줄) — Firehose 가 그대로 S3(→Athena)로 전달.
            #   log="call_history" 는 CloudWatch 구독필터 {$.log="call_history"} 매칭용 태그.
            event = {
                "log": "call_history",
                "ts": datetime.now(timezone.utc).isoformat(),
                "method": method,
                "path": path[:500],
                "query": (qs[:1000] if qs else None),
                "status": status,
                "dur_ms": int((time.perf_counter() - start) * 1000),
                # 예외로 끝난 요청만 채워진다. ★**타입만** 남긴다 —
                # 예외 메시지에는 쿼리 바인딩 값·요청 데이터가 그대로 실릴 수 있어(pymssql 등)
                # 이 로그의 "본문 미저장" 원칙과 충돌한다. 길이 제한은 방어가 못 된다.
                # 타입만으로도 분류는 된다(AttributeError=인증 가드 누락 등).
                # 원문은 같은 시각 stdout 의 traceback 에서 본다.
                "error": (type(exc).__name__ if exc is not None else None),
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
            # 의미 보강: 경로가 카탈로그에 있으면 page/section/action + 본문 화이트리스트(changes) 부착.
            #   본문은 아래 _CallBodyTapMiddleware 가 매칭 경로에 한해 tee 로 캡처(라우트 흐름 무영향).
            _m = _catalog.match(method, path)
            if _m is not None:
                _entry, _pp = _m
                _chunks = request.scope.get("_call_body_chunks")
                _body_obj = None
                if _chunks:
                    try:
                        _body_obj = json.loads(b"".join(_chunks) or b"null")
                    except Exception:
                        _body_obj = None
                try:
                    event.update(_catalog.enrich(_entry, _pp, _body_obj))
                except Exception:
                    pass
            line = json.dumps(event, ensure_ascii=False)
            if _CALL_HISTORY_STREAM:  # env 스트림으로 Firehose → S3 (dev/prod 각자 스트림·테이블)
                asyncio.create_task(asyncio.to_thread(_firehose_put, line + "\n"))
            else:  # env 미설정(로컬)일 때만 stdout
                print(line, flush=True)
    except Exception as e:
        print(f"[call_history][WARN] 로거 예외(무시): {e}", flush=True)
    if exc is not None:
        raise exc  # 기록만 하고 원래 예외를 그대로 올린다(응답 모양 무변경).
    return response


class _CallBodyTapMiddleware:
    """카탈로그 매칭 변경요청에 한해 요청 본문을 non-consuming tee 로 캡처.
    라우트는 본문을 정상적으로 읽고, 사본(bytes 청크)만 scope['_call_body_chunks'] 에 남긴다.
    미매칭/비변경 요청은 손대지 않음(오버헤드 0). 캡처 상한 _BODY_TAP_CAP."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            return await self.app(scope, receive, send)
        method = scope.get("method", "")
        path = scope.get("path", "")
        if method not in _LOG_METHODS or _catalog.match(method, path) is None:
            return await self.app(scope, receive, send)
        chunks: list = []
        state = {"size": 0}
        scope["_call_body_chunks"] = chunks

        async def _tap_receive():
            message = await receive()
            if message.get("type") == "http.request":
                body = message.get("body", b"")
                if body and state["size"] < _BODY_TAP_CAP:
                    chunks.append(body)
                    state["size"] += len(body)
            return message

        return await self.app(scope, _tap_receive, send)


app.add_middleware(_CallBodyTapMiddleware)

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
