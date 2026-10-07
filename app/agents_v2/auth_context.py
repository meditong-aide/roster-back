"""스킬 실행 중의 인증 사용자 — 정본 서비스의 ``current_user`` 로만 넘긴다.

스킬 시그니처는 ``(db, params)`` 라 사용자 객체를 받을 자리가 없다. params 에 넣으면
감사 기록(agent_skill_invocation)·승인 대기(pending_approval → Redis·MSSQL)로 직렬화돼
새어 나가고, LLM 이 같은 키를 인자로 덮어쓸 수도 있다. 그래서 미들웨어가 스킬을
실행하는 동안에만 contextvar 로 묶는다.

★ 사용자 객체는 nurses 행에서 다시 만들지 않는다. 토큰에서 온 객체를 그대로 쓴다
  (ADM 은 간호사 행이 없고 ``is_master_admin`` 이 빠진다).
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

_AUTH_USER: ContextVar[Any] = ContextVar("aide_auth_user", default=None)


@contextmanager
def bind_auth_user(user: Any) -> Iterator[None]:
    """블록 안에서만 ``current_auth_user()`` 가 ``user`` 를 돌려준다."""
    token = _AUTH_USER.set(user)
    try:
        yield
    finally:
        _AUTH_USER.reset(token)


def current_auth_user() -> Any:
    """지금 실행 중인 스킬의 인증 사용자. 묶인 값이 없으면 None(정본 서비스가 401)."""
    return _AUTH_USER.get()
