"""Outcome taxonomy — 스킬 실행 결과(data)를 단일 분류로 통합.

기존 agent_v3 루프에 흩어져 있던 outcome 판별(_is_error / _needs_clarification /
_is_preview_result / apply_hint 추출)을 한 곳으로 모은다.

**동작 동치 리팩터**: 각 판별식의 기존 semantics 를 그대로 보존한다. classify() 는
새 통합 진입점이고, is_error/needs_clarification/is_preview 는 원본과 1:1 동치인
호환 헬퍼다(원본 body 를 그대로 옮겨 divergence 위험 0).

축(axis) 주의 — 아래 taxonomy 는 '실행 *후* 결과(data) 분류'만 담당한다:
- 중복 호출 감지(dup)는 실행 *전* 호출 이력 기반이라 별개 축 → agent_v3 루프에 유지.
- mutation 후 proactive validation 은 outcome 이 아니라 후속 action → _execute_approval 유지.
- apply_hint(INFEASIBLE 동의)는 skill_name + 중첩 payload 의존이라 data-only classify 로는
  못 잡는다 → extract_apply_hint_question() 로 분리 제공(루프에서 preview 앞에 검사).

ADR: docs/adr/0003-agent-orchestration-contracts.md §3.C
"""

from __future__ import annotations

from enum import Enum
from typing import Any


class ErrorType(str, Enum):
    """스킬 결과 outcome 타입.

    (엄밀히는 error 뿐 아니라 preview/ok 도 포함하는 'outcome' 이지만, ADR 0003 §3.C
    의 명명을 따라 ErrorType 로 둔다.)

    1급 승격(ADR 결정 3): CLARIFICATION, PERMISSION_DENIED.
    """

    OK = "ok"                                # 정상 (조회 성공, list 결과 등)
    CLARIFICATION = "clarification"          # 1급 — 모호, 사용자 선택 필요
    PERMISSION_DENIED = "permission_denied"  # 1급 — 권한 차단
    PREVIEW = "preview"                      # mutation 미리보기 (승인 대기)
    GENERIC_ERROR = "generic_error"          # 그 외 "error" 키를 가진 실패


def classify(data: Any) -> ErrorType:
    """스킬 결과 data → outcome 타입 (data 기반 단일 분류).

    우선순위: permission_denied → generic error → clarification → preview → ok.
    permission_denied 결과는 {"error": ..., "permission_denied": True} 형태(=error 이면서
    동시에 permission)라 permission 을 먼저 본다. 이 순서가 기존 루프 분기 순서와 동치.
    """
    if not isinstance(data, dict):
        return ErrorType.OK
    if data.get("permission_denied") is True:
        return ErrorType.PERMISSION_DENIED
    if "error" in data:
        return ErrorType.GENERIC_ERROR
    if data.get("needs_clarification") is True:
        return ErrorType.CLARIFICATION
    if data.get("preview_only") is True or data.get("preview") is True:
        return ErrorType.PREVIEW
    return ErrorType.OK


# ── 호환 헬퍼 (원본 _is_error / _needs_clarification / _is_preview_result 와 1:1 동치) ──
# classify() 에서 파생하지 않고 원본 body 를 그대로 유지한다: permission_denied 가 "error"
# 키 없이 오는 (현재는 없지만 미래에 생길 수 있는) 케이스에서 divergence 가 나지 않도록.


def is_error(data: Any) -> bool:
    return isinstance(data, dict) and "error" in data


def needs_clarification(data: Any) -> bool:
    return isinstance(data, dict) and data.get("needs_clarification") is True


def is_preview(data: Any) -> bool:
    return isinstance(data, dict) and (
        data.get("preview_only") is True or data.get("preview") is True
    )


def extract_apply_hint_question(skill_name: str, data: Any) -> str | None:
    """generate_schedule 결과에 user_actionable apply_hint 가 있으면 재시도 질문 반환.

    None 반환 시 apply_hint 흐름 미진입. (INFEASIBLE 동의 경로 — data-only classify 로는
    못 잡는 skill+payload 의존이라 별도 함수로 분리.)
    """
    if skill_name not in ("generate_schedule", "generate-schedule"):
        return None
    if not isinstance(data, dict):
        return None

    infeasibility = data.get("infeasibility")
    if not isinstance(infeasibility, dict):
        return None

    apply_hint = infeasibility.get("apply_hint")
    if not isinstance(apply_hint, dict):
        return None

    # user_consent_required=True 인 경우만 사용자에게 질의
    if not apply_hint.get("user_consent_required", False):
        return None

    human_msg = apply_hint.get("human_message_ko") or ""
    if human_msg:
        return human_msg
    return "제약 조건을 조정하고 근무표 생성을 재시도하시겠습니까?"
