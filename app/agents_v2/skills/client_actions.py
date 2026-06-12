"""Client-action tools — 에이전트가 프론트에 위임하는 '선언형' UI 액션.

server-tool(skill)과 달리 백엔드에서 실행되지 않는다. 에이전트 루프가 이 tool
호출을 감지하면 DB를 건드리지 않고 ui_action 으로 변환해 응답에 싣고, 프론트
(roster_front)가 이름 붙은 의도를 실제 route/tab/modal 로 해석·실행한다.

설계: docs/AGENT_CLIENT_ACTION_TOOL_SPEC_2026-05-28.md
- target 은 closed enum (route SSOT). enum 밖이면 emit 불가 → 텍스트 안내.
- 백엔드는 컴포넌트/route 문자열을 모른다. 논리적 target/sub 만 emit.
- role-aware: HN 전용 화면은 일반 간호사에게 navigate 금지.
"""

from __future__ import annotations

from typing import Any

# target → 화면 메타.
#   subs:    허용 섹션/탭/모달 (closed)
#   hn_only: HN/ADM 전용 화면 여부
# route 문자열은 프론트(SSOT)가 소유하므로 여기에 담지 않는다.
NAVIGATE_TARGETS: dict[str, dict[str, Any]] = {
    "home": {"subs": set(), "hn_only": False},
    "dashboard": {"subs": set(), "hn_only": False},
    "wanted": {"subs": set(), "hn_only": False},
    "roster_view": {"subs": set(), "hn_only": False},
    "roster_view_my": {"subs": set(), "hn_only": False},
    "nurse_management": {"subs": {"team_setting", "grade_setting"}, "hn_only": True},
    "roster_create": {"subs": set(), "hn_only": True},
    # month_off: 월 오프수 제한(off_days 등 RosterConfig 정책). 전용 화면이 아직 없어
    # 근무표 설정 탭(tab 0)에 묻혀 있는 상태 — LLM 이 의미적으로 정확히 지정할 수 있도록
    # sub 추가하고 프론트에선 tab 0 으로 라우팅. (B 시리즈 후속, 2026-06-01)
    "config": {"subs": {"shift_codes", "weekoff", "wanted_setting", "month_off"}, "hn_only": True},
    "mypage": {"subs": set(), "hn_only": False},
    "support": {"subs": set(), "hn_only": False},
}

# client-action tool 이름 (registry skill 과 구분). hyphen/underscore 모두 허용.
_CLIENT_ACTION_NAMES = frozenset({"navigate", "prefill"})


def normalize_action_name(name: str) -> str:
    return (name or "").replace("-", "_")


def is_client_action(name: str) -> bool:
    """name 이 client-action tool(navigate/prefill)인지."""
    return normalize_action_name(name) in _CLIENT_ACTION_NAMES


def _is_hn(ctx: Any) -> bool:
    return getattr(ctx, "user_role", "") in ("HN", "ADM")


def target_permission_error(target: Any, ctx: Any) -> str | None:
    """HN 전용 target 을 일반 간호사가 navigate/prefill 하려 하면 차단 메시지.

    알 수 없는 target 은 None (권한 문제 아님 — build 단계에서 '화면 없음' 처리).
    """
    meta = NAVIGATE_TARGETS.get(target)
    if not meta:
        return None
    if meta["hn_only"] and not _is_hn(ctx):
        return "해당 화면은 수간호사(HN) 또는 관리자(ADM) 전용입니다."
    return None


def build_ui_action(name: str, args: dict) -> tuple[dict | None, str | None]:
    """client-action tool 호출 → ui_action dict. ``(ui_action, error)`` 반환.

    error 가 있으면 ui_action 은 None — 에이전트가 텍스트로 사용자에게 안내한다.
    내부 id/route 문자열은 담지 않고 논리적 target/sub/query/values 만 담는다.
    """
    norm = normalize_action_name(name)
    args = args or {}

    target = args.get("target")
    meta = NAVIGATE_TARGETS.get(target)
    if meta is None:
        return None, f"'{target}'에 해당하는 화면이 없습니다."

    sub = args.get("sub")
    if sub is not None and sub not in meta["subs"]:
        allowed = ", ".join(sorted(meta["subs"])) or "없음"
        return None, f"'{target}' 화면에는 '{sub}' 섹션이 없습니다. (가능: {allowed})"

    action: dict[str, Any] = {"action": norm, "target": target}
    if sub is not None:
        action["sub"] = sub
    query = args.get("query")
    if query:
        action["query"] = query
    if norm == "prefill":
        values = args.get("values")
        if values:
            action["values"] = values
    return action, None
