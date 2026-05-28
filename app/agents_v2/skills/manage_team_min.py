"""manage-team-min skill — 팀별 시프트 최소 인원(teams.min_shift) 조회/수정.

확정 스코프 (manage_grade 와 동일 철학):
  - 팀별 시프트 최소 인원 조회 / 설정 / 해제

설계 메모:
  - UX 규칙: 사용자에게 team_id(숫자)·JSON 원형 비노출. 입출력은 팀 '이름' + 시프트 자연어.
  - 재사용: services.team_service (list_teams_with_members read, apply_team_ops write).
  - preview: apply_team_ops 는 즉시 commit + 멤버이동/팀CRUD까지 하는 god-function이므로
    preview 단계에선 호출하지 않는다. 대신 validate_team_min_shift_capacity 로 가능 여부만
    검증하고 변경 요약을 반환. 확인(confirm) 시 agent_v3._execute_approval 이 preview_only=False
    로 재호출 → apply_team_ops.
  - ⚠️ payload 최소화: confirm 시 apply_team_ops 에 **{team_id, min_shift} 만** 담는다.
    team_name 을 넣으면 동명 팀 매칭 실패 시 신규 팀 생성/이름변경 부작용이 생긴다.
  - read-modify-write: min_shift 는 팀당 단일 dict. 한 시프트만 바꿀 때 타 시프트 보존,
    해제 시 해당 키 제거(남으면 부분 dict, 비면 {} → 서비스가 NULL 클리어).
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from agents_v2.skills._shift_category import CATEGORY_LABEL, resolve_shift_category
from agents_v2.skills.registry import register
from agents_v2.tools import constraint_tools
from services.precheck.team_min_shift_capacity_validator import (
    validate_team_min_shift_capacity,
)
from services.team_service import apply_team_ops, list_teams_with_members

_APPLY_NOTE = "다음 근무표 생성부터 반영됩니다."


@register("manage-team-min")
def manage_team_min(db: Session, params: dict) -> Any:
    """팀별 시프트 최소 인원 조회/수정. operation 에 따라 분기."""
    operation = str(params.get("operation") or "read").lower()
    group_id = params["group_id"]
    office_id = params.get("office_id")

    if operation == "read":
        return _read(db, office_id, group_id)
    if operation == "set_min":
        return _set_min(db, office_id, group_id, params)
    if operation == "clear_min":
        return _clear_min(db, office_id, group_id, params)
    return {"error": f"지원하지 않는 operation: {operation}"}


# ── read ─────────────────────────────────────────────────────


def _read(db: Session, office_id: str, group_id: str) -> dict:
    teams = list_teams_with_members(db, office_id, group_id)
    out = []
    for t in teams:
        ms = t.get("min_shift") if isinstance(t.get("min_shift"), dict) else {}
        reqs = [
            {"shift": CATEGORY_LABEL.get(k, k), "min_count": int(v)}
            for k, v in (ms or {}).items()
            if _is_int(v) and int(v) > 0
        ]
        out.append({"team": t["team_name"], "min_requirements": reqs})
    return {"teams": out}


# ── set_min ──────────────────────────────────────────────────


def _set_min(db: Session, office_id: str, group_id: str, params: dict) -> dict:
    preview_only = params.get("preview_only", True)
    use_mid = _use_mid(db, group_id)

    code = resolve_shift_category(params.get("shift_name"), use_mid)
    if code is None:
        return _shift_clarification(params.get("shift_name"), use_mid)

    teams = list_teams_with_members(db, office_id, group_id)
    team_id, team_name, clar = _resolve_team(params.get("team_name"), teams)
    if clar is not None:
        return clar

    min_count = _as_int(params.get("min_count"))
    if min_count is None or min_count < 0:
        return {
            "needs_clarification": True,
            "question": f"'{team_name}' 팀의 {CATEGORY_LABEL[code]} 최소 인원을 몇 명으로 할까요?",
            "options": [],
        }

    current = _team_min(teams, team_id)
    new_ms = {k: int(v) for k, v in current.items() if _is_int(v)}
    before = new_ms.get(code)
    new_ms[code] = min_count

    err = _check_capacity(db, office_id, group_id, team_id, new_ms)
    if err is not None:
        return err

    change = {
        "team": team_name, "shift": CATEGORY_LABEL[code],
        "before": before, "after": min_count,
    }
    if preview_only:
        return {"preview": True, "changes": [change], "note": _APPLY_NOTE}

    apply_team_ops(db, office_id, group_id, [_min_shift_payload(team_id, new_ms)])
    return {"preview": False, "applied": True, "changes": [change], "note": _APPLY_NOTE}


# ── clear_min ────────────────────────────────────────────────


def _clear_min(db: Session, office_id: str, group_id: str, params: dict) -> dict:
    preview_only = params.get("preview_only", True)
    use_mid = _use_mid(db, group_id)

    teams = list_teams_with_members(db, office_id, group_id)
    team_id, team_name, clar = _resolve_team(params.get("team_name"), teams)
    if clar is not None:
        return clar

    current = _team_min(teams, team_id)
    shift_name = params.get("shift_name")
    if shift_name:
        code = resolve_shift_category(shift_name, use_mid)
        if code is None:
            return _shift_clarification(shift_name, use_mid)
        new_ms = {k: int(v) for k, v in current.items() if _is_int(v) and k != code}
        change = {
            "team": team_name, "shift": CATEGORY_LABEL[code],
            "before": current.get(code), "after": "해제",
        }
    else:
        # 시프트 미지정 → 팀 전체 최소 인원 해제
        new_ms = {}
        change = {"team": team_name, "shift": "전체", "after": "해제"}

    if preview_only:
        return {"preview": True, "changes": [change], "note": _APPLY_NOTE}

    apply_team_ops(db, office_id, group_id, [_min_shift_payload(team_id, new_ms)])
    return {"preview": False, "applied": True, "changes": [change], "note": _APPLY_NOTE}


# ── helpers ──────────────────────────────────────────────────


def _min_shift_payload(team_id: int, min_shift: dict) -> dict:
    """apply_team_ops 용 **최소** payload. team_id 와 min_shift 만 — team_name/add/remove/
    handoff_policy 는 절대 넣지 않는다(넣으면 팀 생성·이름변경·멤버이동 부작용).
    min_shift={} 는 서비스 레이어가 NULL(제약 없음)로 클리어.
    """
    return {"team_id": team_id, "min_shift": min_shift}


def _check_capacity(db, office_id, group_id, team_id, new_ms) -> dict | None:
    """팀 인원 대비 min_shift 가능 여부 검증. 불가 시 친화적 에러 dict, 가능하면 None.

    멤버 변동이 없으므로 projected_member_ids=None (현재 DB 소속 기준).
    """
    vr = validate_team_min_shift_capacity(
        db, office_id=office_id, group_id=group_id, team_id=team_id,
        new_min_shift=new_ms, projected_member_ids=None,
    )
    if vr.get("saveable", True):
        return None
    issues = vr.get("issues") or []
    hard = [i for i in issues if i.get("severity") == "hard"]
    first = hard[0] if hard else (issues[0] if issues else {})
    return {
        "error": "team_min_capacity",
        "message": first.get("message", "팀 인원이 부족해 해당 최소 인원을 설정할 수 없습니다."),
    }


def _resolve_team(team_name: Any, teams: list[dict]) -> tuple[int | None, str | None, dict | None]:
    """팀 이름 → (team_id, team_name, None) / 실패 시 (None, None, clarification)."""
    names = [t["team_name"] for t in teams]
    if not team_name:
        return None, None, {
            "needs_clarification": True,
            "question": "어느 팀인가요?",
            "options": names,
        }
    key = str(team_name).strip().lower()
    matches = [t for t in teams if str(t["team_name"]).strip().lower() == key]
    if len(matches) == 1:
        return matches[0]["team_id"], matches[0]["team_name"], None
    if len(matches) > 1:
        return None, None, {
            "needs_clarification": True,
            "question": f"'{team_name}'에 해당하는 팀이 여러 개입니다. 어느 팀인가요?",
            "options": names,
        }
    return None, None, {
        "needs_clarification": True,
        "question": f"'{team_name}' 팀을 찾지 못했습니다. 어느 팀인가요?",
        "options": names,
    }


def _team_min(teams: list[dict], team_id: int) -> dict:
    for t in teams:
        if t["team_id"] == team_id:
            ms = t.get("min_shift")
            return dict(ms) if isinstance(ms, dict) else {}
    return {}


def _shift_clarification(name: Any, use_mid: bool) -> dict:
    options = ["데이", "이브닝", "나이트"] + (["미드"] if use_mid else [])
    return {
        "needs_clarification": True,
        "question": f"'{name}' 근무를 인식하지 못했습니다. 어떤 근무인가요?",
        "options": options,
    }


def _use_mid(db: Session, group_id: str) -> bool:
    try:
        cfg = constraint_tools.get_roster_config(db, group_id)
    except Exception:  # noqa: BLE001
        return False
    return bool(cfg.get("use_mid")) if isinstance(cfg, dict) else False


def _is_int(v: Any) -> bool:
    try:
        int(v)
        return True
    except (TypeError, ValueError):
        return False


def _as_int(v: Any) -> int | None:
    if v is None:
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None
