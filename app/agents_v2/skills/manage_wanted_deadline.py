"""manage-wanted-deadline skill — 원티드 마감일 변경 + 즉시 마감.

operation:
  read   — 현재 wanted 상태/마감일 조회
  set_deadline — 마감일 변경 (preview/apply)
  close  — 즉시 마감 (preview/apply)

정책:
  - HN 권한 검증은 chat_router(permission check) 가 처리. 본 스킬은 group_id-scoped DB 작업.
  - exp_date None → '마감일 없음' 으로 해석.
  - 이미 마감된(closed) 원티드는 마감일 변경 불가 — clarify 응답.
  - apply 시 push 알림은 본 스킬에서 보내지 않음 (chat 흐름은 통상 호출 빈도 낮음 + 광범위 알림은
    위험. 향후 명시적 옵션으로 분리).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session

from agents_v2.skills.registry import register
from db.models import Wanted


def _format_date(d) -> str | None:
    if d is None:
        return None
    if isinstance(d, datetime):
        return d.strftime("%Y-%m-%d")
    return str(d)


def _load_wanted(db: Session, group_id: str, year: int, month: int):
    return (
        db.query(Wanted)
        .filter(
            Wanted.group_id == group_id,
            Wanted.year == year,
            Wanted.month == month,
        )
        .first()
    )


def _coerce_date(value: Any):
    """문자열/datetime → datetime. None/빈 → None ('마감일 없음')."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        # YYYY-MM-DD 또는 ISO 8601 수용
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            try:
                return datetime.strptime(value, "%Y-%m-%d")
            except ValueError:
                return "INVALID"
    return "INVALID"


@register("manage-wanted-deadline")
def manage_wanted_deadline(db: Session, params: dict) -> Any:
    """원티드 마감일 / 마감 상태 관리.

    params:
      operation: read | set_deadline | close (필수)
      group_id, year, month (필수)
      exp_date (set_deadline): 'YYYY-MM-DD' 또는 None (마감일 없음)
      preview_only (set_deadline | close): True 면 변경 안 함
    """
    op = (params.get("operation") or "").lower()
    group_id = params.get("group_id")
    year = params.get("year")
    month = params.get("month")

    if not group_id:
        return {"error": "group_id required (RBAC scope)"}
    if year is None or month is None:
        return {"error": "year and month required"}
    if op not in ("read", "set_deadline", "close"):
        return {
            "needs_clarification": True,
            "question": "원티드를 어떻게 하시겠어요?",
            "options": [
                "현재 상태 보기 (read)",
                "마감일 변경 (set_deadline)",
                "즉시 마감 (close)",
            ],
        }

    wanted = _load_wanted(db, group_id, int(year), int(month))
    if not wanted:
        return {
            "error": "not_found",
            "message": f"{year}년 {month}월 원티드 요청이 없습니다.",
        }

    current_status = wanted.status
    current_exp = _format_date(wanted.exp_date)

    if op == "read":
        return {
            "year": int(year),
            "month": int(month),
            "status": current_status,
            "exp_date": current_exp,
            "display_exp_date": current_exp or "마감일 없음",
        }

    if op == "set_deadline":
        if current_status == "closed":
            return {
                "error": "already_closed",
                "message": "이미 마감된 원티드의 마감일은 변경할 수 없습니다.",
            }
        new_exp_raw = params.get("exp_date")
        new_exp = _coerce_date(new_exp_raw)
        if new_exp == "INVALID":
            return {
                "needs_clarification": True,
                "question": "마감일은 'YYYY-MM-DD' 형식으로 입력해주세요. (예: 2026-07-10)",
                "options": [],
            }
        new_exp_str = _format_date(new_exp)
        change = {
            "type": "원티드 마감일",
            "before": current_exp or "마감일 없음",
            "after": new_exp_str or "마감일 없음",
        }
        after_text = new_exp_str or "마감일 없음"
        before_text = current_exp or "마감일 없음"
        if params.get("preview_only", False):
            return {
                "preview": True,
                "year": int(year),
                "month": int(month),
                "changes": [change],
                "message": (
                    f"{year}년 {month}월 원티드 마감일을 "
                    f"'{before_text}' → '{after_text}' 로 바꿉니다. 진행할까요?"
                ),
            }
        wanted.exp_date = new_exp
        db.commit()
        return {
            "preview": False,
            "applied": True,
            "year": int(year),
            "month": int(month),
            "changes": [change],
            "current_exp_date": new_exp_str,
            "message": (
                f"{year}년 {month}월 원티드 마감일을 '{after_text}' 로 변경했어요."
                if new_exp_str
                else f"{year}년 {month}월 원티드 마감일을 없앴어요."
            ),
        }

    # op == "close"
    if current_status == "closed":
        return {
            "preview": False,
            "applied": False,
            "message": f"{year}년 {month}월 원티드는 이미 마감된 상태예요.",
            "status": "closed",
        }
    change = {"type": "원티드 상태", "before": current_status, "after": "closed"}
    if params.get("preview_only", False):
        return {
            "preview": True,
            "year": int(year),
            "month": int(month),
            "changes": [change],
            "message": f"{year}년 {month}월 원티드를 지금 마감합니다. 진행할까요?",
        }
    wanted.status = "closed"
    db.commit()
    return {
        "preview": False,
        "applied": True,
        "year": int(year),
        "month": int(month),
        "changes": [change],
        "status": "closed",
        "message": f"{year}년 {month}월 원티드를 마감했어요.",
    }
