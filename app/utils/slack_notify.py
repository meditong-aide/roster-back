# app/utils/slack_notify.py
"""근무표 생성 결과를 Slack Incoming Webhook 으로 알리는 fire-and-forget 알림.

설계 원칙:
    - 의존성 zero: 표준 라이브러리 urllib 만 사용 (worker.py 의 top-level stdlib-only 정책 준수).
      requests/httpx 를 쓰지 않아 Lambda 패키징/cold-start 부하가 없다.
    - fire-and-forget: 알림 전송이 실패해도 절대 예외를 전파하지 않는다.
      모니터링 알림 때문에 실제 job 처리(SUCCESS/FAILED 기록)가 깨지면 안 된다.
    - opt-in: SLACK_WEBHOOK_URL 환경변수가 없으면 조용히 no-op.

환경변수:
    SLACK_WEBHOOK_URL : Slack Incoming Webhook URL. 미설정 시 알림 비활성(no-op).
    ENV / STAGE       : (선택) 메시지 머리말에 표시할 환경 이름(prod/staging 등).
"""
from __future__ import annotations

import json
import os
import sys
import urllib.request

_TIMEOUT_SEC = 3


def _post(webhook_url: str, payload: dict) -> None:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        webhook_url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    urllib.request.urlopen(req, timeout=_TIMEOUT_SEC).read()


def notify_roster_result(
    *,
    ok: bool,
    job_id: str | None,
    group_id: str | None = None,
    office_id: str | None = None,
    office_name: str | None = None,
    nurse_id: str | None = None,
    year: int | None = None,
    month: int | None = None,
    result_roster_id: str | None = None,
    error_message: str | None = None,
    quality_lines: list[str] | None = None,
) -> None:
    """근무표 생성 성공/실패를 Slack 으로 알린다.

    절대 예외를 raise 하지 않는다. webhook 미설정이면 no-op.

    인자:
        ok: 성공 여부.
        job_id: RosterJob ID.
        group_id/office_id/office_name/nurse_id: 어느 병동/요청자인지 식별.
        year/month: 생성 대상 연/월.
        result_roster_id: 성공 시 생성된 근무표 ID.
        error_message: 실패 시 에러 메시지(infeasibility JSON 포함 가능).
        quality_lines: 성공 시 품질 요약 라인(roster_quality.summarize 결과). 선택.
    """
    webhook_url = os.getenv("SLACK_WEBHOOK_URL")
    if not webhook_url:
        return

    try:
        env_name = os.getenv("ENV") or os.getenv("STAGE") or ""
        env_prefix = f"[{env_name}] " if env_name else ""
        status_emoji = "✅" if ok else "🚨"
        status_word = "완료" if ok else "실패"

        office_label = office_name or office_id or "-"
        if office_name and office_id:
            office_label = f"{office_name}({office_id})"
        ym = f"{year}-{month:02d}" if (year and month) else "-"
        lines = [
            f"{status_emoji} {env_prefix}근무표 생성 {status_word}",
            f"• 병동(group): {group_id or '-'}  |  office: {office_label}",
            f"• 대상: {ym}  |  요청자(nurse): {nurse_id or '-'}",
            f"• job_id: {job_id or '-'}",
        ]
        if ok:
            lines.append(f"• roster_id: {result_roster_id or '-'}")
            if quality_lines:
                lines.append("")  # 구분 빈 줄
                lines.extend(quality_lines)
        else:
            # 에러 메시지는 너무 길면 잘라낸다 (Slack 가독성).
            msg = (error_message or "").strip()
            if len(msg) > 1500:
                msg = msg[:1500] + " …(생략)"
            lines.append(f"• error: {msg or '-'}")

        _post(webhook_url, {"text": "\n".join(lines)})
    except Exception as exc:  # noqa: BLE001 — 알림 실패가 job 처리를 깨면 안 됨
        print(f"[slack_notify] 알림 전송 실패(무시): {exc}", file=sys.stderr)
