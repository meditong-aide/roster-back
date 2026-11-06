"""Dev 모드 쿼리 캡처 — agent 개발 방향 레퍼런스용.

`/api/agent/chat/send` 가 라우터를 통과할 때마다 사용자 발화를 router 9 카테고리
중 하나의 markdown 에 append. 카테고리는 라우터가 이미 분류했으므로 추가 비용 0.
캡처 실패는 절대 agent 흐름을 깨지 않는다(silent best-effort).
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

_LOG_DIR = Path(__file__).resolve().parent / "dev_queries"

_ROUTER_CATEGORIES = {
    "navigation", "read", "mutate", "generate",
    "validate_repair", "analyze", "recommend",
    "settings_rules", "settings_people",
}


def log_dev_query(
    categories: list[str] | str | None,
    message: str,
    conv_id: str | None = None,
) -> None:
    """라우터가 분류한 모든 카테고리 MD 에 timestamp + quoted user message append.

    multi-category 쿼리는 각 카테고리 파일에 모두 기록(cross-reference). 분류 없거나
    알 수 없는 카테고리만이면 unrouted.md 로 fallback.
    """
    if not message or not message.strip():
        return
    if isinstance(categories, str):
        cats: list[str] = [categories]
    elif categories:
        cats = list(categories)
    else:
        cats = []
    targets = [c for c in cats if c in _ROUTER_CATEGORIES] or ["unrouted"]
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        _LOG_DIR.mkdir(parents=True, exist_ok=True)
        for cat in targets:
            path = _LOG_DIR / f"{cat}.md"
            new_file = not path.exists()
            with path.open("a", encoding="utf-8") as f:
                if new_file:
                    f.write(f"# dev queries — {cat}\n\n")
                    f.write(
                        "router 가 이 카테고리로 분류한 사용자 발화 로그. "
                        "agent 개발 방향(스킬 보강·description·테스트 케이스) 참고용.\n\n"
                    )
                f.write(f"## {ts}")
                if conv_id:
                    f.write(f" — conv `{conv_id[:8]}`")
                if len(targets) > 1:
                    f.write(f" — also: {', '.join(c for c in targets if c != cat)}")
                f.write("\n\n")
                for line in (message.splitlines() or [message]):
                    f.write(f"> {line}\n")
                f.write("\n")
    except Exception:  # noqa: BLE001
        pass
