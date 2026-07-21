"""Langfuse 관측 — 턴을 trace, LLM 호출을 generation, Stage 를 span, 결과를 score 로 적재.

**키 게이팅**: LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY / LANGFUSE_HOST 셋 다 있고 langfuse
설치돼 있을 때만 활성. 하나라도 없으면 **완전 no-op**(프로덕션·테스트 무영향, 오버헤드 0).

**절대 턴을 깨지 않음**: 모든 Langfuse 호출은 try/except 로 감싸고, 실패해도 에이전트 흐름에
영향 주지 않는다(record_llm_usage 와 동일한 graceful 원칙).

**self-host 전제**: 트레이스에 간호사 실명·근무표가 원문으로 담기므로 LANGFUSE_HOST 는
병원 내부 인스턴스여야 한다(의료 데이터 외부 유출 없음).

설계: docs/AGENT_OBSERVABILITY_LANGFUSE.md.
"""

from __future__ import annotations

import contextlib
import contextvars
import logging
import os
from typing import Any

logger = logging.getLogger(__name__)

# 현재 턴의 trace 와 LLM 호출 용도(purpose) — 컨텍스트 격리(동시 요청 안전).
_current_trace: contextvars.ContextVar[Any] = contextvars.ContextVar("lf_trace", default=None)
_current_purpose: contextvars.ContextVar[str] = contextvars.ContextVar("lf_purpose", default="llm")

_client: Any = None
_init_done = False


def _get_client() -> Any:
    """Langfuse 클라이언트 lazy 초기화. 키 없거나 실패면 None(비활성)."""
    global _client, _init_done
    if _init_done:
        return _client
    _init_done = True
    pk = os.getenv("LANGFUSE_PUBLIC_KEY")
    sk = os.getenv("LANGFUSE_SECRET_KEY")
    host = os.getenv("LANGFUSE_HOST")
    if not (pk and sk and host):
        return None  # 키 미설정 → 완전 no-op
    try:
        from langfuse import Langfuse

        _client = Langfuse(public_key=pk, secret_key=sk, host=host)
        logger.info("[obs] Langfuse 활성 — host=%s", host)
    except Exception as e:  # noqa: BLE001
        logger.warning("[obs] Langfuse 초기화 실패 → 비활성: %s", e)
        _client = None
    return _client


def enabled() -> bool:
    return _get_client() is not None


@contextlib.contextmanager
def turn(*, conversation_id: str | None, user_id: str | None,
         group_id: str | None, user_message: str, name: str = "agent_turn"):
    """턴 1개를 trace 로. 내부 LLM 호출·Stage 가 이 trace 아래 매달린다. 종료 시 flush.

    yield 값은 trace(or None). 비활성이면 no-op 통과.
    """
    client = _get_client()
    if client is None:
        yield None
        return
    trace = None
    try:
        trace = client.trace(
            name=name, session_id=conversation_id, user_id=user_id,
            input=user_message, metadata={"group_id": group_id},
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("[obs] trace 생성 실패: %s", e)
    tok = _current_trace.set(trace)
    try:
        yield trace
    finally:
        _current_trace.reset(tok)
        try:
            client.flush()
        except Exception:  # noqa: BLE001
            pass


@contextlib.contextmanager
def purpose(name: str):
    """이 블록 내 LLM 호출 generation 이름을 name 으로(router/planner/turn 구분)."""
    tok = _current_purpose.set(name)
    try:
        yield
    finally:
        _current_purpose.reset(tok)


def finish_turn(answer: str | None, stages: list | None = None,
                score_name: str | None = None, score_value: Any = None) -> None:
    """턴 종료 — trace 에 출력(answer) + Stage span 들 + 결과 score 부착. 실패 무시."""
    trace = _current_trace.get()
    if trace is None:
        return
    try:
        trace.update(output=answer)
    except Exception:  # noqa: BLE001
        pass
    for st in (stages or []):
        try:
            d = st.to_dict() if hasattr(st, "to_dict") else dict(st)
            trace.span(name=str(d.get("name") or d.get("stage") or "stage"),
                       metadata=d).end()
        except Exception:  # noqa: BLE001
            pass
    if score_name is not None:
        score(score_name, score_value)


def score(name: str, value: Any, comment: str | None = None) -> None:
    """현재 trace 에 평가 점수 부착(예: 검증결과 outcome, L1/L2 통과여부). 실패 무시."""
    trace = _current_trace.get()
    if trace is None:
        return
    try:
        trace.score(name=name, value=value, comment=comment)
    except Exception:  # noqa: BLE001
        pass


def wrap_client(client: Any) -> Any:
    """client.chat 을 감싸 매 LLM 호출을 현재 trace 아래 generation 으로 적재.

    비활성(키 없음)이면 원본 그대로 반환(오버헤드 0). 중복 래핑 방지.
    trace 가 없는 호출(턴 밖)은 그냥 통과 — generation 안 만듦.
    """
    if getattr(client, "_lf_wrapped", False):
        return client
    if not enabled():
        return client  # no-op — 래핑조차 안 함
    orig = client.chat

    def chat(messages, tools, **kw):
        resp = orig(messages, tools, **kw)
        trace = _current_trace.get()
        if trace is not None:
            try:
                out = (resp.text if getattr(resp, "type", None) == "text"
                       else [tc.name for tc in (resp.tool_calls or [])])
                trace.generation(
                    name=_current_purpose.get(),
                    model=getattr(resp, "model", None),
                    input=messages, output=out,
                    usage={"input": getattr(resp, "input_tokens", 0) or 0,
                           "output": getattr(resp, "output_tokens", 0) or 0,
                           "unit": "TOKENS"},
                ).end()
            except Exception:  # noqa: BLE001
                pass
        return resp

    client.chat = chat
    client._lf_wrapped = True
    return client
