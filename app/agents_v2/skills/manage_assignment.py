"""manage-assignment skill — 병동 파견 조회·등록·취소 (v1: 파견 focus).

매니페스트(@skill)의 첫 시민. 선언 하나로 스키마·카테고리·권한(HN 전용 mutation)이
자동 구성된다. 병동 이름→group_id 해석은 스킬 내부(resolve_group)에서 수행하고,
생성/취소는 preview_only 로 미리보기→승인 플로우(outcome taxonomy)에 물린다.

v1 범위(사용자 확정): 파견(dispatch)만. 병동이동(영구)은 target_team/grade 이관 정책이
필요해 후속. 서비스단(assignment_service)이 퇴사/중복/office경계/팀정합을 이미 검증하므로
스킬은 grounding + 얇은 래핑만 담당한다.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from sqlalchemy.orm import Session

from agents_v2.grounding.internal import resolve_date, resolve_group
from agents_v2.skills.manifest import skill
from services import assignment_service

_REASON = "파견"  # 기본(하위호환)


def _reason_of(params: dict) -> str:
    """kind 파라미터 → reason. '이동'/'transfer' 포함이면 병동이동, 그 외 파견."""
    k = str(params.get("kind") or "").strip().lower()
    return "병동이동" if ("이동" in k or "transfer" in k or "move" in k) else "파견"


MANAGE_ASSIGNMENT_SCHEMA: dict = {
    "name": "manage_assignment",
    "description": (
        "간호사 **파견**(임시 근무) 또는 **병동이동**(영구 소속 변경) 배정을 조회·등록·취소합니다. (HN/ADM 전용)\n\n"
        "kind 로 구분: `파견`=다른 병동에서 임시 근무(종료일 있음, 소속 유지) / "
        "`병동이동`=소속 병동을 영구 변경(종료일 없음, **월 단위로 발효**).\n"
        "⚠️ '병동이동/병동 옮기기/소속 변경'은 반드시 이 스킬(kind=병동이동)로 처리하라. "
        "간호사 속성(update_person_attr)의 group_id 직접 변경으로 하지 마라 — 그건 월 발효·팀/등급 이관을 못 한다.\n"
        "⚠️ 병동이동은 **어느 달부터 옮길지(발효월)** 를 반드시 확인해야 한다. 사용자가 월을 말하지 않았으면 "
        "start_date 를 임의로 채우지 말고 비워서 호출하라(스킬이 되묻는다). 발효월은 그 달 1일로 발효된다.\n"
        "⚠️ 등록/취소는 preview_only=true 로 먼저 호출해 미리보기를 만들고, 사용자 확인 후 실행됩니다.\n\n"

        "─────────── operation ───────────\n"
        "- `list` — 파견/이동 현황 조회. 간호사 이름 주면 그 사람 이력, 없으면 이번 달 현황.\n"
        "- `create` — 등록. nurse_name + target_ward + start_date 필요. (파견은 end_date 도 가능)\n"
        "- `cancel` — 활성 배정 취소. nurse_name 으로 대상을 찾습니다.\n\n"

        "─────────── 파라미터 ───────────\n"
        "- `kind` — '파견' 또는 '병동이동'. 기본 '파견'.\n"
        "- `nurse_name` — 간호사 이름(그대로). 내부에서 id 로 해석.\n"
        "- `target_ward` — 보낼/옮길 병동 이름(예: '중환자실2', '9B'). 내부에서 해석.\n"
        "- `start_date` — YYYY-MM-DD. 발효 시작일. '8월부터'=2026-08-01. "
        "**병동이동은 월 단위** — 아무 날이나 줘도 그 달 1일로 정규화된다. 월을 모르면 비워라(되묻는다).\n"
        "- `end_date` — 파견 종료일(선택). **병동이동은 무시**(영구).\n"
        "- `note` — 사유/메모(선택).\n\n"

        "예) '신솔희 8월부터 중환자실2로 병동이동' → operation=create, kind=병동이동, nurse_name=신솔희, "
        "target_ward=중환자실2, start_date=2026-08-01\n"
        "예) '김민지 8/1~8/31 중환자실2 파견' → operation=create, kind=파견, start_date=2026-08-01, end_date=2026-08-31\n"
        "예) '이번 달 파견 나간 사람' → operation=list"
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "operation": {
                "type": "string",
                "enum": ["list", "create", "cancel"],
                "description": "list=조회, create=등록, cancel=취소. 기본 list",
            },
            "kind": {
                "type": "string",
                "enum": ["파견", "병동이동"],
                "description": "파견=임시근무 / 병동이동=영구 소속변경. 기본 파견",
            },
            "nurse_name": {"type": "string", "description": "간호사 이름 (한글). 내부에서 id 로 해석"},
            "target_ward": {"type": "string", "description": "보낼/옮길 병동 이름 (예: '중환자실2')"},
            "start_date": {"type": "string", "description": "발효 시작일 YYYY-MM-DD ('8월부터'=2026-08-01)"},
            "end_date": {"type": "string", "description": "파견 종료일 YYYY-MM-DD (선택, 병동이동은 무시)"},
            "note": {"type": "string", "description": "사유/메모 (선택)"},
            "preview_only": {"type": "boolean", "default": True},
        },
        "required": ["operation"],
    },
}


# ── helpers ──────────────────────────────────────────────────


def _group_name_map(db: Session, office_id: str) -> dict[str, str]:
    from db.models import Group

    rows = db.query(Group).filter(Group.office_id == office_id).all()
    return {g.group_id: g.group_name for g in rows}


def _iso(value: Any, params: dict) -> str | None:
    """YYYY-MM-DD 로 정규화. 이미 ISO 면 그대로, 자연어면 resolve_date."""
    if not value:
        return None
    s = str(value)
    if len(s) >= 10 and s[4] == "-" and s[7] == "-":
        return s[:10]
    return resolve_date(s, params.get("year", date.today().year), params.get("month", 1))


def _transfer_month_start(value: Any, params: dict) -> str | None:
    """병동이동 발효 시점 → 그 '달의 1일' ISO(YYYY-MM-01).

    병동이동은 assignment 테이블에 **월 단위**로 발효(야간 flush)되므로 일(日)이 아니라
    '월'만 유효하다. 어떤 날을 받아도 그 달 1일로 정규화한다. 다양한 표기 수용:
      '2026-08-01' / '2026-08-15' / '2026-08' → '2026-08-01'
      '8월' / '8' (+ context year)           → '2026-08-01'
      '8월 15일' 등 자연어                     → resolve_date 후 월 추출
    월을 특정할 수 없으면 None(→ 호출부에서 clarify).
    """
    import re

    if not value:
        return None
    s = str(value).strip()
    yr = params.get("year") or date.today().year
    m = re.match(r"(\d{4})-(\d{1,2})", s)  # ISO YYYY-MM(-DD)
    if m:
        return f"{int(m.group(1)):04d}-{int(m.group(2)):02d}-01"
    m = re.search(r"(\d{1,2})\s*월", s) or re.fullmatch(r"(\d{1,2})", s)  # '8월' / '8'
    if m:
        return f"{yr:04d}-{int(m.group(1)):02d}-01"
    iso = resolve_date(s, yr, params.get("month", 1))  # 자연어 → 월 추출
    return f"{iso[:7]}-01" if iso else None


def _humanize_month(iso: str) -> str:
    """YYYY-MM-01 → '2026년 8월'."""
    return f"{iso[:4]}년 {int(iso[5:7])}월"


def _humanize_period(start: Any, end: Any) -> str:
    s = start.isoformat() if hasattr(start, "isoformat") else str(start)
    if not end:
        return f"{s}부터 (종료일 미정)"
    e = end.isoformat() if hasattr(end, "isoformat") else str(end)
    return f"{s} ~ {e}"


def _humanize_assignment(db: Session, row: Any, gmap: dict[str, str]) -> dict:
    """NurseAssignmentResponse(pydantic) 또는 NurseAssignment(ORM) 를 사람이 읽는 dict 로."""
    nurse_name = getattr(row, "nurse_name", None)
    nurse_id = getattr(row, "nurse_id", None)
    if not nurse_name and nurse_id:
        from db.models import Nurse

        n = db.query(Nurse).filter(Nurse.nurse_id == nurse_id).first()
        nurse_name = n.name if n else nurse_id
    end = getattr(row, "end_date", None) or getattr(row, "expected_end_date", None)
    return {
        "nurse": nurse_name,
        "reason": getattr(row, "reason", None),
        "from_ward": gmap.get(getattr(row, "source_group_id", None), getattr(row, "source_group_id", None)),
        "to_ward": gmap.get(getattr(row, "target_group_id", None), getattr(row, "target_group_id", None)),
        "period": _humanize_period(getattr(row, "start_date", None), end),
        "status": getattr(row, "status", None),
    }


def _humanize_impact(impact: dict, gmap: dict[str, str]) -> dict:
    """preview_assignment_impact 결과를 id 없이 요약."""
    return {
        "겹치는_기존배정": len(impact.get("conflicts") or []),
        "영향받는_월한도_건수": len(impact.get("nml_affected") or []),
        "재검토_필요_근무표": len(impact.get("schedules_to_check") or []),
        "알림_대상": impact.get("notifications") or [],
    }


def _clean_error(exc: Exception) -> str:
    detail = getattr(exc, "detail", None)
    return str(detail) if detail else str(exc)


# ── operations ───────────────────────────────────────────────


def _list(db: Session, params: dict) -> Any:
    office_id = params.get("office_id")
    gmap = _group_name_map(db, office_id)
    nurse_ids = params.get("nurse_ids") or []

    if nurse_ids:
        rows = assignment_service.get_assignments(db, office_id=office_id, nurse_id=nurse_ids[0])
        return {
            "operation": "list",
            "scope": "nurse",
            "assignments": [_humanize_assignment(db, r, gmap) for r in rows],
        }

    group_id = params.get("group_id")
    year, month = params.get("year"), params.get("month")
    recs = assignment_service.get_active_assignments_for_month(db, group_id, year, month)
    dispatches = [r for r in recs if getattr(r, "reason", None) == _REASON]
    return {
        "operation": "list",
        "scope": "month",
        "year": year,
        "month": month,
        "dispatches": [_humanize_assignment(db, r, gmap) for r in dispatches],
    }


def _resolve_nurse_id(params: dict) -> str | None:
    nurse_ids = params.get("nurse_ids") or []
    return nurse_ids[0] if nurse_ids else None


def _create(db: Session, params: dict) -> Any:
    reason = _reason_of(params)
    is_transfer = reason == "병동이동"
    verb = "병동이동" if is_transfer else "파견"

    office_id = params.get("office_id")

    # ── clarify 1: 대상 간호사 ──
    nurse_id = _resolve_nurse_id(params)
    if not nurse_id:
        return {
            "needs_clarification": True,
            "question": f"어느 간호사를 {'이동' if is_transfer else '파견'}하나요? (예: '김민지')",
            "options": [],
        }

    # ── clarify 2: 대상 병동 (미지정) ──
    target_ward = params.get("target_ward")
    if not target_ward:
        return {
            "needs_clarification": True,
            "question": f"어느 병동으로 {'이동' if is_transfer else '파견'}하나요? (예: '중환자실2')",
            "options": [],
        }

    # ── clarify 3: 병동명 → group_id (모호/미존재는 resolve_group 이 clarify/error) ──
    src_group = params.get("group_id")
    rg = resolve_group(db, office_id, target_ward, exclude_group_id=src_group)
    if rg.needs_clarification:
        return rg.to_clarification_dict()
    if rg.error:
        return {"error": rg.error}
    target_group_id = rg.value

    from db.models import Nurse

    nurse = db.query(Nurse).filter(Nurse.nurse_id == nurse_id).first()
    if not nurse:
        return {"error": "간호사를 찾을 수 없습니다."}
    source_group_id = nurse.group_id
    gmap = _group_name_map(db, office_id)

    # ── guard: 이미 그 병동 소속이면 병동이동 무의미 ──
    if is_transfer and source_group_id == target_group_id:
        return {"error": f"{nurse.name} 간호사는 이미 {gmap.get(target_group_id, target_group_id)} 소속입니다."}

    # ── clarify 4: 발효 시점 ──
    #   병동이동: '월'만 유효(월 단위 발효) → 그 달 1일로 정규화, 종료일 없음(영구).
    #   파견: 시작일(+선택 종료일).
    if is_transfer:
        start = _transfer_month_start(params.get("start_date"), params)
        if not start:
            return {
                "needs_clarification": True,
                "question": f"{nurse.name} 간호사를 몇 월부터 {gmap.get(target_group_id, target_group_id)}(으)로 이동하나요? (예: 2026년 8월)",
                "options": [],
            }
        end = None
    else:
        start = _iso(params.get("start_date"), params)
        if not start:
            return {"needs_clarification": True, "question": "파견 시작일을 알려주세요 (예: 8월 1일).", "options": []}
        end = _iso(params.get("end_date"), params)

    period_str = f"{_humanize_month(start)}부터 (영구 이동)" if is_transfer else _humanize_period(start, end)
    preview_only = params.get("preview_only", True)

    if preview_only:
        try:
            impact = assignment_service.preview_assignment_impact(
                db,
                nurse_id=nurse_id,
                reason=reason,
                start_date=date.fromisoformat(start),
                target_group_id=target_group_id,
                expected_end_date=date.fromisoformat(end) if end else None,
            )
        except Exception as e:  # noqa: BLE001
            return {"error": _clean_error(e)}
        summary = {
            "nurse": nurse.name,
            "reason": reason,
            "from_ward": gmap.get(source_group_id, source_group_id),
            "to_ward": gmap.get(target_group_id, target_group_id),
            "period": period_str,
        }
        if is_transfer:
            summary["effective_month"] = start[:7]  # 발효월(확인용)
        return {"preview": True, "operation": "create", "summary": summary,
                "impact": _humanize_impact(impact, gmap)}

    from schemas.roster_schema import NurseAssignmentCreate

    req = NurseAssignmentCreate(
        nurse_id=nurse_id,
        source_group_id=source_group_id,
        office_id=office_id,
        target_group_id=target_group_id,  # 이름이 아니라 resolve 된 group_id
        start_date=date.fromisoformat(start),  # 병동이동은 발효월 1일
        expected_end_date=date.fromisoformat(end) if end else None,
        reason=reason,
        note=params.get("note"),
    )
    try:
        assignment_service.create_assignment(req, db, current_user=None, notify=True)
    except Exception as e:  # noqa: BLE001
        return {"error": _clean_error(e)}
    to_ward = gmap.get(target_group_id, target_group_id)
    msg = (f"{nurse.name} 간호사를 {_humanize_month(start)}부터 {to_ward}(으)로 병동이동 처리했습니다."
           if is_transfer else
           f"{nurse.name} 간호사를 {to_ward}(으)로 파견 등록했습니다.")
    return {"ok": True, "message": msg, "period": period_str}


def _cancel(db: Session, params: dict) -> Any:
    reason = _reason_of(params)
    verb = "병동이동" if reason == "병동이동" else "파견"
    office_id = params.get("office_id")
    nurse_id = _resolve_nurse_id(params)
    if not nurse_id:
        return {"error": f"{verb}을 취소할 간호사를 지정해 주세요."}

    rows = assignment_service.get_assignments(
        db, office_id=office_id, nurse_id=nurse_id, status="active"
    )
    dispatches = [r for r in rows if getattr(r, "reason", None) == reason]
    if not dispatches:
        return {"error": f"취소할 활성 {verb}이(가) 없습니다."}

    gmap = _group_name_map(db, office_id)
    if len(dispatches) > 1:
        return {
            "needs_clarification": True,
            "question": f"취소할 {verb}이(가) 여러 건입니다. 어느 것인가요?",
            "options": [
                f"{gmap.get(r.target_group_id, r.target_group_id)} ({_humanize_period(r.start_date, r.end_date or r.expected_end_date)})"
                for r in dispatches
            ],
        }

    target = dispatches[0]
    preview_only = params.get("preview_only", True)
    if preview_only:
        return {
            "preview": True,
            "operation": "cancel",
            "summary": _humanize_assignment(db, target, gmap),
        }

    try:
        assignment_service.cancel_assignment(target.id, db, current_user=None)
    except Exception as e:  # noqa: BLE001
        return {"error": _clean_error(e)}
    return {
        "ok": True,
        "message": f"{getattr(target, 'nurse_name', '')} 간호사의 {verb}을(를) 취소했습니다.",
    }


@skill(
    "manage_assignment",
    MANAGE_ASSIGNMENT_SCHEMA,
    # settings_people(주) + mutate(파견 등록/취소는 본질이 mutation → '취소'가 mutate 로
    # 분류돼도 tool 이 스코프에 있도록). recall A/B(2026-07-08)에서 '취소'가 mutate 로
    # 새던 문제를 이 다중배선 + trigger_hint 로 함께 잡는다.
    categories=["settings_people", "mutate"],
    mutation=True,
    hn_only=True,
    grounds=["nurse_name", "target_ward"],
    trigger_hint=("간호사 파견(임시 근무) 등록·취소·조회, "
                  "병동이동/병동 옮기기/소속 병동 변경(영구), 파견자·이동자 명단/현황, 병동 배정"),
    # 검증(5요소): 완료 결과는 조회(assignments/dispatches) 또는 실행 성공(ok=True) 이어야.
    # error/preview/clarification 은 classify 단계에서 이미 걸러져 여기 안 옴.
    postcondition=lambda d: isinstance(d, dict)
    and (d.get("ok") is True or "assignments" in d or "dispatches" in d),
)
def manage_assignment(db: Session, params: dict) -> Any:
    op = (params.get("operation") or "list").lower()
    if op == "list":
        return _list(db, params)
    if op == "create":
        return _create(db, params)
    if op == "cancel":
        return _cancel(db, params)
    return {"error": f"지원하지 않는 작업입니다: {op}"}
