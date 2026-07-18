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


# ── L2 answer-consistency judge ──────────────────────────────
# read-back(L1)이 'DB↔의도'를 본다면, 이건 '답변↔데이터'를 본다. tool 데이터는 맞는데
# 최종 자연어 답변이 그걸 틀리게 말하는 것(환각 숫자, "3명인데 5명", 실패인데 "완료")을 잡는다.
# reference-grounded: judge 에게 진실 데이터를 주고 대조시킨다(자유 판단 아님).
# 조언적 층 — judge 실패/예외는 통과(답변을 깨지 않는다). 확실히 어긋날 때만 INCONSISTENT.


@dataclass
class ConsistencyResult:
    consistent: bool
    reason: str = ""


def judge_answer_consistency(llm: Any, question: str, data: Any, answer: str) -> ConsistencyResult:
    """답변의 사실 주장이 tool 데이터로 뒷받침되는지 nano judge 로 대조."""
    import json

    if not answer or not answer.strip():
        return ConsistencyResult(True)
    try:
        data_str = json.dumps(data, ensure_ascii=False, default=str)
    except Exception:  # noqa: BLE001
        data_str = str(data)
    data_str = data_str[:4000]  # 토큰 상한(초과 시 부분 — judge 는 확실한 모순만 잡음)

    sys = (
        "너는 답변 검증기다. 사용자 질문, 도구가 반환한 데이터(=진실), 에이전트의 답변이 주어진다.\n"
        "답변의 사실 주장(숫자·이름·상태·완료여부)이 데이터로 뒷받침되는지 판정하라.\n"
        "- 데이터에 없거나 데이터와 어긋나는 주장이 있으면 INCONSISTENT.\n"
        "- 표현 차이·요약·자연스러운 말투·데이터를 그대로 옮긴 것은 문제없음(CONSISTENT).\n"
        "- 데이터가 일부만 주어졌을 수 있으니, 확실히 모순될 때만 INCONSISTENT.\n"
        "반드시 첫 줄에 'CONSISTENT' 또는 'INCONSISTENT: <무엇이 어긋났는지 한 줄>' 만 출력."
    )
    user = f"[질문]\n{question}\n\n[데이터(진실)]\n{data_str}\n\n[답변]\n{answer}"
    try:
        resp = llm.chat(
            [{"role": "system", "content": sys}, {"role": "user", "content": user}],
            tools=[],
        )
        text = (getattr(resp, "text", None) or "").strip()
    except Exception as e:  # noqa: BLE001
        return ConsistencyResult(True, f"judge 예외(무해통과): {e}")

    if text.upper().startswith("INCONSISTENT"):
        reason = text.split(":", 1)[1].strip() if ":" in text else text
        return ConsistencyResult(False, reason or "답변이 데이터와 어긋남")
    return ConsistencyResult(True)
