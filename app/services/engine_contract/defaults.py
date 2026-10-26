"""설정이 비었을 때의 기본값 단일 출처 — 레지스트리 `current_default` 를 값으로 푼다(EI-15 · D5·D24).

생성 밖 소비자(대체근무 추천·피저빌리티 경고)와 GET 무행 시드가 하드락 키 기본값을 각자 리터럴로 들지 않고
여기서 읽는다. 엔진 기본(`create_config_from_db` 의 `config_data.get(키, 기본)`)과 레지스트리가 같은 값임은
`tests/engine_input/test_ei15_replacement_defaults.py` 가 고정한다.
"""

from __future__ import annotations

import ast

from .effective import canonical_key
from .settings import SETTINGS_BY_KEY


def registry_default(key: str) -> object:
    """레지스트리 `current_default`(문자열 표기)를 값으로 푼다. 정본 키·별칭 모두 받는다.

    값 표기가 아닌 문자열은 그대로, 문자열이 아닌 값(None 등)도 그대로 돌려준다.
    """
    raw = SETTINGS_BY_KEY[canonical_key(key)].current_default
    if isinstance(raw, str):
        try:
            return ast.literal_eval(raw)
        except (ValueError, SyntaxError):
            return raw
    return raw


__all__ = ["registry_default"]
