"""resolve-resignation skill — 퇴사자 대응 부분 재생성 준비(grounding).

퇴사자 이름·퇴사일·(선택)대체 프리셉터를 DB 그라운딩해서 부분 재생성 액션을 준비한다.
실제 실행(솔버 재생성)은 current_user 권한이 필요하므로 상위 레이어가
`services.resignation_partial_resolve_service.partial_resolve_on_resignation` 를 호출한다
(generate-schedule 의 `_sqs_dispatch_required` 패턴과 동일하게 `_partial_resolve_required`
플래그 + 그라운딩된 파라미터를 반환).

grounding은 이 스킬 내부에서 수행한다:
- 퇴사자/대체 프리셉터: 이름 → nurse_id (search_nurses_by_name, group scope)
- 대상 근무표: (group, year, month) → schedule_id (resolve_target_schedule)
- 퇴사일: ISO('2026-03-16') 우선, 한국어('3월 16일')·일자만('16')도 방어적 파싱
"""
from __future__ import annotations

import re
from datetime import date, datetime
from typing import Any

from sqlalchemy.orm import Session

from agents_v2.skills.registry import register
from agents_v2.tools import nurse_tools, schedule_tools


def _parse_cutoff(raw: Any, year: int | None, month: int | None) -> date | None:
    """cutoff_date를 date로 정규화한다. ISO 우선, 한국어/일자만 폴백."""
    if raw is None:
        return None
    if isinstance(raw, datetime):
        return raw.date()
    if isinstance(raw, date):
        return raw
    s = str(raw).strip()
    if not s:
        return None
    try:
        return date.fromisoformat(s[:10])
    except ValueError:
        pass
    # "3월 16일" / "16일" / "16" — year/month 컨텍스트가 있을 때만
    if year and month:
        m = re.search(r"(?:(\d{1,2})\s*월)?\s*(\d{1,2})\s*일?", s)
        if m:
            mm = int(m.group(1)) if m.group(1) else int(month)
            dd = int(m.group(2))
            try:
                return date(int(year), mm, dd)
            except ValueError:
                return None
    return None


@register("resolve-resignation")
def resolve_resignation(db: Session, params: dict) -> Any:
    """퇴사자 부분 재생성을 준비한다(grounding). 실제 실행은 상위 레이어가 수행.

    필요 params: group_id, year, month, resigned_nurse(이름), cutoff_date.
    선택 params: replacement_preceptor(이름).
    """
    group_id = params.get("group_id")
    year = params.get("year")
    month = params.get("month")
    resigned_query = params.get("resigned_nurse") or params.get("nurse_name")
    cutoff_raw = params.get("cutoff_date")
    replacement_query = params.get("replacement_preceptor")

    if not group_id:
        return {"error": "group_id가 필요합니다."}
    if not resigned_query:
        return {"error": "퇴사자 이름(resigned_nurse)이 필요합니다."}

    # 1. 대상 근무표 해석
    meta = schedule_tools.resolve_target_schedule(db, group_id, year, month)
    if not meta:
        return {"error": f"{year}/{month} 근무표를 찾을 수 없습니다."}
    schedule_id = meta["schedule_id"]

    # 2. 퇴사자 이름 → id
    candidates = nurse_tools.search_nurses_by_name(db, group_id, resigned_query)
    if not candidates:
        return {"error": f"'{resigned_query}' 간호사를 찾을 수 없습니다."}
    if len(candidates) > 1:
        return {
            "error": f"'{resigned_query}' 동명이인이 있습니다. 특정해 주세요.",
            "candidates": candidates,
        }
    resigned = candidates[0]

    # 3. 퇴사일 파싱
    cutoff = _parse_cutoff(cutoff_raw, year, month)
    if cutoff is None:
        return {"error": f"퇴사일자를 해석할 수 없습니다: {cutoff_raw!r} (예: '2026-03-16')"}

    # 4. 대체 프리셉터(선택)
    replacement_id = None
    replacement_name = None
    if replacement_query:
        rc = nurse_tools.search_nurses_by_name(db, group_id, replacement_query)
        if not rc:
            return {"error": f"대체 프리셉터 '{replacement_query}'를 찾을 수 없습니다."}
        if len(rc) > 1:
            return {
                "error": f"대체 프리셉터 '{replacement_query}' 동명이인이 있습니다.",
                "candidates": rc,
            }
        replacement_id = rc[0]["nurse_id"]
        replacement_name = rc[0].get("name")

    return {
        "_partial_resolve_required": True,
        "_partial_resolve_params": {
            "schedule_id": schedule_id,
            "resigned_nurse_id": resigned["nurse_id"],
            "cutoff_date": cutoff.isoformat(),
            "replacement_preceptor_id": replacement_id,
        },
        "schedule_id": schedule_id,
        "resigned_nurse_id": resigned["nurse_id"],
        "resigned_nurse_name": resigned.get("name"),
        "cutoff_date": cutoff.isoformat(),
        "replacement_preceptor_id": replacement_id,
        "replacement_preceptor_name": replacement_name,
        "message": (
            f"{resigned.get('name')} 퇴사({cutoff.isoformat()}부터) 부분 재생성 준비 완료. "
            f"cutoff 이전은 동결하고 이후만 최소 변경으로 재조정합니다."
        ),
    }
