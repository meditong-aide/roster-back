"""프론트 UI 행동 이벤트 수집 라우터 — 자체 계측 → Firehose(roster-ui-events) → S3 → Athena.

GTM/GA 미사용(데이터 전부 자사 AWS·제3자 전송 없음). 분석 비콘이라 요청을 절대 막지 않는다:
인증 soft(실패해도 진행)·전송 실패는 로그만·항상 204. content-type 관대
(fetch JSON / sendBeacon text/plain 모두 수용 — 언로드 유실 방지).
PII 최소화: 요청 본문 저장 안 하고 지정 필드만. 이벤트별 데이터는 props(JSON 문자열).
"""
import os
import json
import asyncio
import boto3
from datetime import datetime, timezone

from fastapi import APIRouter, Request, Response

# 기존 SQS 클라이언트(roster_create.py)와 동일 패턴 — 모듈 레벨·no-I/O.
# env 미설정이면 전송 스킵(dev/local 무해). prod .env 에 UI_EVENTS_FIREHOSE_STREAM 주입.
_firehose = boto3.client("firehose", region_name="ap-northeast-2")
_UI_STREAM = os.getenv("UI_EVENTS_FIREHOSE_STREAM")
_MAX_BATCH = 500

router = APIRouter(prefix="/events", tags=["events"])


def _soft_user(request: Request) -> dict:
    """access_token 쿠키(JWT) soft 디코드 → 유저 필드. 실패해도 막지 않고 {} 반환."""
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
            "office_id": p.get("office_id"), "group_id": p.get("group_id"),
        }
    except Exception:
        return {}


def _client_ip(request: Request) -> str | None:
    """ALB 뒤 실제 클라이언트 IP (X-Forwarded-For 첫 값)."""
    xff = request.headers.get("x-forwarded-for", "")
    if xff:
        return xff.split(",")[0].strip()
    return request.client.host if request.client else None


def _build_records(events: list, user: dict, ip: str | None, ua: str) -> list:
    """프론트 이벤트 목록 → Firehose 레코드(JSON 1줄) 목록. 서버가 ts/유저/ip 보강."""
    server_ts = datetime.now(timezone.utc).isoformat()
    out = []
    for e in events[:_MAX_BATCH]:
        if not isinstance(e, dict):
            continue
        rec = {
            "log": "ui_event", "ts": server_ts,
            "event": str(e.get("event") or "")[:100],
            "page": (str(e.get("page") or "")[:300] or None),
            "section": (str(e.get("section") or "")[:200] or None),
            "session_id": (str(e.get("session_id") or "")[:100] or None),
            "account_id": user.get("account_id"), "nurse_id": user.get("nurse_id"),
            "office_id": user.get("office_id"), "group_id": user.get("group_id"),
            "props": json.dumps(e.get("props") or {}, ensure_ascii=False)[:4000],
            "ip": (str(ip)[:64] if ip else None), "ua": (ua or None),
        }
        out.append({"Data": (json.dumps(rec, ensure_ascii=False) + "\n").encode("utf-8")})
    return out


def _put_records(records: list) -> None:
    """Firehose 배치 전송(블로킹 → to_thread 로 오프로드). 실패는 로그만."""
    try:
        _firehose.put_record_batch(DeliveryStreamName=_UI_STREAM, Records=records)
    except Exception as ex:
        print(f"[ui_events][WARN] Firehose put 실패: {ex}", flush=True)


@router.post("", status_code=204)
async def collect_ui_events(request: Request) -> Response:
    """UI 이벤트 배치 수집 → Firehose. 분석 비콘이라 항상 204(인증/전송 실패로 안 막음)."""
    if not _UI_STREAM:
        return Response(status_code=204)
    try:
        raw = await request.body()
        data = json.loads(raw or b"{}")
        events = data.get("events") if isinstance(data, dict) else None
    except Exception:
        return Response(status_code=204)
    if not isinstance(events, list) or not events:
        return Response(status_code=204)
    ua = (request.headers.get("user-agent", "") or "")[:300]
    records = _build_records(events, _soft_user(request), _client_ip(request), ua)
    if records:
        await asyncio.to_thread(_put_records, records)
    return Response(status_code=204)
