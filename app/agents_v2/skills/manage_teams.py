"""manage-teams skill — 팀 CRUD (add / rename / delete) + 조회.

operation:
  read   — 팀 + 멤버 목록 (서비스 list_teams_with_members 그대로)
  add    — 새 팀 생성 (team_name 만 필요)
  rename — 기존 팀 이름 변경 (team_name 매칭으로 찾아 new_name 적용)
  delete — 팀 soft delete (team_name 으로 찾음)

정책:
  - HN/ADM only (mutation 은 middleware._MUTATION_SKILLS 게이트).
  - mutation 은 preview→apply 두 단계.
  - 멤버 이동(add/remove)은 본 스킬 범위 밖 — 별도 흐름. 본 스킬은 팀 자체의 라이프사이클.
  - 사용자에게 internal team_id 노출 금지. 항상 team_name 으로 말하기.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from agents_v2.skills.registry import register
from services.team_service import apply_team_ops, list_teams_with_members


def _find_team(teams: list[dict], team_name: str) -> dict | None:
    """대소문자 무시 정확 일치 → 부분 일치 폴백."""
    if not team_name:
        return None
    cand = team_name.strip()
    lower = cand.lower()
    for t in teams:
        if str(t.get("team_name", "")).lower() == lower:
            return t
    for t in teams:
        if lower in str(t.get("team_name", "")).lower():
            return t
    return None


def _team_clarification(teams: list[dict], typed: str) -> dict:
    names = [t.get("team_name") for t in teams]
    return {
        "needs_clarification": True,
        "question": (
            f"'{typed}' 라는 팀을 찾지 못했어요. 어느 팀인지 정확히 알려주세요."
        ),
        "options": names,
    }


@register("manage-teams")
def manage_teams(db: Session, params: dict) -> Any:
    """팀 CRUD.

    params:
      operation: read | add | rename | delete (필수)
      group_id, office_id (필수 — RBAC)
      team_name (add/rename/delete): 대상 또는 신규 이름
      new_name (rename): 새 이름
      preview_only (mutation): 기본 True
    """
    op = (params.get("operation") or "").lower()
    group_id = params.get("group_id")
    office_id = params.get("office_id")

    if not group_id or not office_id:
        return {"error": "group_id and office_id required (RBAC scope)"}
    if op not in ("read", "add", "rename", "delete"):
        return {
            "needs_clarification": True,
            "question": "팀을 어떻게 처리할까요?",
            "options": [
                "팀 목록 보기 (read)",
                "팀 추가 (add)",
                "이름 바꾸기 (rename)",
                "삭제 (delete)",
            ],
        }

    teams = list_teams_with_members(db, office_id, group_id)

    if op == "read":
        # internal id 는 노출하지 않고 이름·멤버 수만.
        display = [
            {
                "team_name": t.get("team_name"),
                "member_count": len(t.get("team_members") or []),
                "members": t.get("team_members") or [],
            }
            for t in teams
        ]
        return {
            "operation": "read",
            "team_count": len(display),
            "teams": display,
            "message": (
                f"현재 팀 {len(display)}개가 있어요." if display
                else "아직 등록된 팀이 없어요."
            ),
        }

    # ── mutations ────────────────────────────────────
    preview_only = params.get("preview_only", True)
    team_name = (params.get("team_name") or "").strip()

    if op == "add":
        if not team_name:
            return {
                "needs_clarification": True,
                "question": "새 팀 이름을 알려주세요. (예 '신생아실', 'B팀')",
                "options": [],
            }
        # 중복 이름 차단.
        if _find_team(teams, team_name):
            return {
                "error": "duplicate_name",
                "message": f"'{team_name}' 이라는 팀이 이미 있어요.",
            }
        change = {"type": "팀 추가", "after": team_name}
        if preview_only:
            return {
                "preview": True,
                "operation": "add",
                "changes": [change],
                "message": f"'{team_name}' 팀을 새로 추가합니다. 진행할까요?",
            }
        # 적용 — TeamOps create
        payload = [{"team_name": team_name, "add": [], "remove": []}]
        apply_team_ops(db, office_id, group_id, payload)
        return {
            "preview": False,
            "applied": True,
            "operation": "add",
            "changes": [change],
            "message": f"'{team_name}' 팀을 추가했어요.",
        }

    if op == "rename":
        new_name = (params.get("new_name") or "").strip()
        if not team_name or not new_name:
            return {
                "needs_clarification": True,
                "question": "어느 팀을 어떤 이름으로 바꿀까요?",
                "options": [t.get("team_name") for t in teams],
            }
        target = _find_team(teams, team_name)
        if not target:
            return _team_clarification(teams, team_name)
        if target.get("team_name") == new_name:
            return {
                "error": "noop",
                "message": f"이미 '{new_name}' 이에요.",
            }
        if _find_team(teams, new_name):
            return {
                "error": "duplicate_name",
                "message": f"'{new_name}' 이라는 팀이 이미 있어요.",
            }
        change = {
            "type": "팀 이름 변경",
            "before": target.get("team_name"),
            "after": new_name,
        }
        if preview_only:
            return {
                "preview": True,
                "operation": "rename",
                "changes": [change],
                "message": (
                    f"'{target.get('team_name')}' → '{new_name}' 으로 이름을 바꿉니다. 진행할까요?"
                ),
            }
        payload = [{
            "team_id": target.get("team_id"),
            "team_name": new_name,
            "add": [], "remove": [],
        }]
        apply_team_ops(db, office_id, group_id, payload)
        return {
            "preview": False,
            "applied": True,
            "operation": "rename",
            "changes": [change],
            "message": f"'{target.get('team_name')}' 을 '{new_name}' 으로 바꿨어요.",
        }

    # op == "delete"
    if not team_name:
        return {
            "needs_clarification": True,
            "question": "어느 팀을 삭제할까요?",
            "options": [t.get("team_name") for t in teams],
        }
    target = _find_team(teams, team_name)
    if not target:
        return _team_clarification(teams, team_name)
    member_count = len(target.get("team_members") or [])
    change = {
        "type": "팀 삭제",
        "before": target.get("team_name"),
        "member_count": member_count,
    }
    if preview_only:
        warn = ""
        if member_count > 0:
            warn = f" 멤버 {member_count}명이 팀 미지정 상태가 됩니다."
        return {
            "preview": True,
            "operation": "delete",
            "changes": [change],
            "message": f"'{target.get('team_name')}' 팀을 삭제합니다.{warn} 진행할까요?",
        }
    apply_team_ops(
        db, office_id, group_id, payload=[],
        delete_team_ids=[target.get("team_id")],
    )
    return {
        "preview": False,
        "applied": True,
        "operation": "delete",
        "changes": [change],
        "message": f"'{target.get('team_name')}' 팀을 삭제했어요.",
    }
