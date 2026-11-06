"""스킬 한 번 실행 — AIDE `agents_v2.middleware.execute_skill` 의 얇은 껍데기(S5 · 11-06).

단계(⓪ 범위 → ① 권한 → ② 문맥 주입 → ③ 이름 확정 → ④ 실행 → 감사)는 전부 AIDE 가 한다. 수정 도우미 쪽 단계 코드는 없다:
  · 에이전트 프로필(`schedule-edit`)과 스펙의 sources 가 부른 곳(LLM·예시·버튼)별로 부를 수 있는 도구와 쓰기(적용 = 버튼만)를 가른다.
  · 스펙 훅(context_first·grounder — `aide_skills`)이 고칠 첫날 정하기와 이름 확정을 한다.
  · 사람·병동 권한(수간호사·그룹관리자·ADM + 그 병동 접근)은 경로가 턴마다 먼저 확인한다(`spec.authorize_schedule`).
여기서는 SessionContext 를 지어 부르고, 결과 dict 를 `aide_skills.from_aide_result` 로 `Outcome` 으로 되돌릴 뿐이다.
단계 기록(`MiddlewareStep`)은 AIDE 것이 `turn.trace` 로 걸려 이 턴 첫 결과 기록(schedule_edit_log payload 의 steps)에 남는다.
"""

from __future__ import annotations

import dataclasses
import logging
from typing import Any, Dict

from agents_v2.middleware import MiddlewareStep, SkillResult, execute_skill as aide_execute_skill
from agents_v2.profiles import SCHEDULE_EDIT
from agents_v2.schemas.session_context import SessionContext
from schedule_edit_agent.aide_skills import UNKNOWN_ACTION, UNKNOWN_TOOL, from_aide_result
from schedule_edit_agent.skills import Turn

logger = logging.getLogger(__name__)

__all__ = ["MiddlewareStep", "SkillResult", "UNKNOWN_ACTION", "UNKNOWN_TOOL", "execute_skill"]


def execute_skill(turn: Turn, skill_name: str, args: Dict[str, Any], *, source: str) -> SkillResult:
    """AIDE 실행 경로로 스킬을 돌린다. 결과의 data 는 `Outcome` — 권한 거절은 blocked=True(수정 도우미에 없는 도구·버튼을
    골라 흔적을 그대로 둔다), 그 밖의 예상 밖 예외는 RuntimeError 로 올린다(500)."""
    session_ctx = turn.session.ctx
    account = getattr(turn.user, "account_id", None)
    ctx = SessionContext(
        office_id=session_ctx.office_id, group_id=session_ctx.group_id, year=session_ctx.year, month=session_ctx.month,
        nurse_id=str(account) if account is not None else None,
        user_role="HN", agent_id=SCHEDULE_EDIT, source=source, turn=turn, auth_user=turn.user,
        today=turn.today.isoformat(), conversation_id=turn.conversation_id,
    )
    try:
        result = aide_execute_skill(turn.db, skill_name, dict(args or {}), ctx)
    finally:
        _commit_audit(turn.db)
    return dataclasses.replace(result, data=from_aide_result(result.data, source))


def _commit_audit(db: Any) -> None:
    """AIDE 감사 행(agent_skill_invocation)은 add·flush 까지만 한다 — 이 경로엔 턴 끝 커밋이 없어 요청 세션이 닫히며 롤백되므로
    여기서 커밋한다(R7). 실패하면 rollback 하고 경고만 남긴다.
    전제: 이 시점에 미완 쓰기가 없다 — 기록(`journal.record_event`)은 건마다 커밋하고, 적용(`services.schedule_edit.apply`)은
    실패하면 스스로 되돌린다. 스킬 안에 직접 add·flush 하는 쓰기가 생기면 이 커밋·rollback 이 그것까지 다루므로 다시 본다."""
    if db is None:
        return
    try:
        db.commit()
    except Exception as exc:   # noqa: BLE001 — 감사 때문에 요청을 깨뜨리지 않는다
        logger.warning("[schedule-edit] skill audit commit failed: %s", exc)
        try:
            db.rollback()
        except Exception:      # noqa: BLE001
            pass
