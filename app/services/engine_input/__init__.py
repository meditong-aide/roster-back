"""EngineInput 패키지 — 생성 경로의 입력 조립을 단계별로 함수화한 모듈 모음(EI-9).

이 `__init__.py` 는 docstring 만 둔다. `roster_create_service` 를 여기서 import 하면
순환(rcs → engine_input 패키지 → rcs)이 생기므로, 하위 모듈이 rcs 함수가 필요할 때는
각 함수 **안에서** 지연 import 한다(D23 — 로더는 rcs 에 그대로 둔다).
"""
from __future__ import annotations
