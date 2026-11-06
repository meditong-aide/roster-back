"""스킬 실행 중의 파생 에이전트 턴 — `auth_context` 와 같은 모양(S4 · 11-06).

스킬 시그니처는 ``(db, params)`` 라 대화 상태를 든 턴(근무표 수정 도우미의 ``Turn``)을 받을 자리가 없다.
params 에 넣으면 감사 기록·직렬화로 새어 나가므로, 미들웨어가 스펙 훅과 스킬을 실행하는 동안에만 contextvar 로 묶는다.
턴 밖에서는 ``current_turn()`` 이 None 이다 — 스킬은 None 이면 분명한 예외를 낸다.

이 모듈은 턴이 무엇인지 모른다(Any) — 파생 에이전트 쪽 모듈이 모양을 안다(층 분리).
``current_call()`` 은 같은 블록 안의 호출 출처(llm·example·button)와 미들웨어가 받은 원래 인자다 — 파생 에이전트가
예외를 기록할 때 "어디서 온 어떤 인자였나"를 남기는 데 쓴다.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

_TURN: ContextVar[Any] = ContextVar("aide_turn", default=None)
_CALL: ContextVar[tuple[str, dict]] = ContextVar("aide_turn_call", default=("", {}))


@contextmanager
def bind_turn(turn: Any, *, source: str = "", args: dict | None = None) -> Iterator[None]:
    """블록 안에서만 ``current_turn()`` 이 ``turn`` 을 돌려준다."""
    token_turn = _TURN.set(turn)
    token_call = _CALL.set((source, dict(args or {})))
    try:
        yield
    finally:
        _CALL.reset(token_call)
        _TURN.reset(token_turn)


def current_turn() -> Any:
    """지금 실행 중인 스킬의 턴. 묶인 값이 없으면 None."""
    return _TURN.get()


def current_call() -> tuple[str, dict]:
    """지금 호출의 (출처, 미들웨어가 받은 원래 인자). 묶인 값이 없으면 ("", {})."""
    return _CALL.get()
