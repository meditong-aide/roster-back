"""L1 read-back 검증기 — 실행 후 DB 를 되읽어 '의도한 상태'가 실제로 반영됐는지 대조.

postcondition(결과 dict 의 shape 만 봄)과 다르다. read-back 은 db+params 로 **실제 DB 상태**를
조회해, 스킬이 error 없이 ok 를 반환했어도(=classify OK) 반영이 안 됐으면 잡는다.
'조용한 거짓완료'(예외 없이 무효/미반영 상태를 완료라 보고)를 차단하는 층.

- 스킬별로 `@readback(skill_name)` 으로 등록한다.
- 등록 안 된 스킬은 무해하게 통과(VerifyResult(True)).
- verifier 내부 예외는 '검증 실패'로 간주(안전측: 못 믿으면 통과시키지 않는다).

미들웨어 ④c 에서 실행되며, 실패 시 결과를 VERIFICATION_FAILED 로 승격한다.
설계 근거: LLM-Modulo(LLM 생성 → 외부 verifier 검증) 의 최소 read-back 구현.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from sqlalchemy.orm import Session


@dataclass
class VerifyResult:
    ok: bool
    reason: str = ""


Verifier = Callable[[Session, dict, Any], VerifyResult]

_READBACK: dict[str, Verifier] = {}


def readback(skill_name: str) -> Callable[[Verifier], Verifier]:
    """스킬 실행 결과를 DB 로 되읽어 검증하는 함수를 등록하는 데코레이터."""

    def deco(fn: Verifier) -> Verifier:
        _READBACK[skill_name.replace("-", "_")] = fn
        return fn

    return deco


def run_readback(db: Session, skill_name: str, params: dict, result: Any) -> VerifyResult:
    """등록된 read-back 을 실행. 없으면 통과. 내부 예외는 검증 실패로 간주."""
    fn = _READBACK.get(skill_name.replace("-", "_"))
    if fn is None:
        return VerifyResult(True)
    try:
        return fn(db, params, result)
    except Exception as e:  # noqa: BLE001
        return VerifyResult(False, f"read-back 예외: {e}")
