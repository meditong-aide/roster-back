"""근무표 생성 요청 전체의 실행 마감 — 완화 사다리 재시도와 엔진 추가 풀이가 함께 쓴다.

생성은 Lambda(600초 제한)에서 돈다. 사다리 재시도(하드 복구·누적 해제·OFF 정확 해제 등)와
엔진 안 추가 풀이(월 상한 연장·전날 N 최소화·N 간격 최소화)가 저마다 새 시간 예산을 받으면
어려운 입력에서 제한을 넘겨 결과 없이 끊기고, SQS 재처리로 같은 비용을 반복한다(Codex 지적).
요청 시작에 마감을 세우고 각 지점이 남은 시간 안에서만 쓰게 한다. 마감을 세우지 않은 호출
(테스트·스크립트)은 남은 시간이 무한이라 종전과 같다.
"""
from __future__ import annotations

import contextvars
import os
import time

_DEADLINE: contextvars.ContextVar[float | None] = contextvars.ContextVar("roster_gen_deadline", default=None)


def start(budget_sec: float | None = None) -> contextvars.Token:
    """요청 시작에 마감을 세운다. 반환 토큰은 finally 에서 `reset()` 에 넘긴다."""
    if budget_sec is None:
        budget_sec = float(os.environ.get("ROSTER_GEN_BUDGET_SEC", "540") or 540)
    return _DEADLINE.set(time.monotonic() + float(budget_sec))


def reset(token: contextvars.Token) -> None:
    _DEADLINE.reset(token)


def left() -> float:
    """남은 초. 마감이 없으면 무한."""
    deadline = _DEADLINE.get()
    return float("inf") if deadline is None else deadline - time.monotonic()


def clamp(sec: float, reserve: float, floor: float = 1.0) -> float:
    """추가 풀이 시간 `sec` 를 남은 시간에서 `reserve`(뒤 단계·저장 몫)를 뺀 만큼으로 줄인다."""
    return max(floor, min(float(sec), left() - reserve))


SAVE_RESERVE_SEC = 30.0


def expired(reserve: float = SAVE_RESERVE_SEC) -> bool:
    """마감(저장 몫 제외)이 지났는가 — 지났으면 풀이를 돌리지 말고 UNKNOWN 으로 넘긴다.

    1초 상한으로라도 돌리면 풀이 횟수만큼 마감을 넘긴다(Codex 지적). 호출 쪽은 모두 UNKNOWN 을
    '해 없음/판단 불가' 로 처리해 확보한 해로 돌아가거나 실패를 낸다.
    """
    return left() - reserve <= 0


def cap_solver(solver, reserve: float = SAVE_RESERVE_SEC) -> float | None:
    """CP-SAT `Solve` 직전에 부른다 — 시간 상한을 마감(저장 몫 제외) 안으로 줄인다.

    모든 본 풀이·후속 패스·진단 풀이가 같은 마감을 지키게 하는 마지막 관문이다. 남은 시간이
    없으면 1초만 주어 UNKNOWN 으로 끝나게 하고, 호출 쪽은 확보한 해로 돌아가거나 실패를 낸다.
    줄였으면 새 상한을, 아니면 None 을 돌려준다.
    """
    remain = left()
    if remain == float("inf"):
        return None
    cur = float(getattr(solver.parameters, "max_time_in_seconds", 0) or 0)
    cap = max(1.0, remain - reserve)
    if cur <= 0 or cur > cap:
        solver.parameters.max_time_in_seconds = cap
        return cap
    return None
