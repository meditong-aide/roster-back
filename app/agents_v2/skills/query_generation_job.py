"""query-generation-job skill — 근무표 생성 job 상태 조회 (read-only).

사용자 발화: "근무표 생성 어디까지?", "생성 됐어?", "마지막 job 상태"
정책: 그룹 스코프 RBAC. office/group 없으면 빈 답.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from agents_v2.skills.registry import register
from agents_v2.tools import generation_tools


_HUMAN_STATUS = {
    "QUEUED": "대기 중",
    "RUNNING": "실행 중",
    "SUCCESS": "성공",
    "FAILED": "실패",
}


def _compose_message(status: str, progress: int | None, year, month) -> str:
    period = ""
    if year and month:
        period = f"{int(year)}년 {int(month)}월 "
    if status == "QUEUED":
        return f"{period}근무표 생성이 대기열에 들어가 있어요. 곧 시작됩니다."
    if status == "RUNNING":
        if isinstance(progress, int) and progress > 0:
            return f"{period}근무표를 만들고 있어요. ({progress}% 진행)"
        return f"{period}근무표를 만들고 있어요."
    if status == "SUCCESS":
        return f"{period}근무표 생성을 완료했어요."
    if status == "FAILED":
        return (
            f"{period}근무표 생성이 실패했어요. "
            "원인은 사유 메시지를 확인해주세요."
        )
    return f"{period}근무표 생성 상태를 확인 중이에요."


@register("query-generation-job")
def query_generation_job(db: Session, params: dict) -> Any:
    """가장 최근 근무표 생성 job 상태 반환.

    params:
      group_id (필수, RBAC)
      office_id (선택)
    """
    group_id = params.get("group_id")
    if not group_id:
        return {"error": "group_id required (RBAC scope)"}

    job = generation_tools.get_latest_job(
        db, group_id, office_id=params.get("office_id"),
    )
    if not job:
        return {
            "found": False,
            "message": "최근 근무표 생성 기록이 없어요.",
        }
    status = job.get("status") or ""
    human = _HUMAN_STATUS.get(status, status)
    year = job.get("year")
    month = job.get("month")
    return {
        "found": True,
        "status": status,
        "status_human": human,
        "progress": job.get("progress"),
        "created_at": job.get("created_at"),
        "completed_at": job.get("completed_at"),
        "error_message": job.get("error_message"),
        "year": year,
        "month": month,
        "message": _compose_message(status, job.get("progress"), year, month),
        # job_id 는 system-internal — 사용자에게 노출 금지.
        "_internal": {"job_id": job.get("job_id")},
    }
