"""LLM 비용 산출 — 모델별 단가 + 토큰→USD 계산.

단가는 **한 곳(MODEL_PRICING)**에서만 관리한다. 값은 2026-05 기준 공개 가격
추정치이며 정확한 최신가는 openai.com/api/pricing 으로 갱신할 것.
(src=검색 확인 / est=추정 — 주석 표기)

쓰임: 라우터 모델 벤치마크(scripts/router_model_bench.py) + 병동·간호사별 사용량
기록(usage.record_llm_usage) 양쪽에서 공유.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# 모델 → (input USD/1M tok, output USD/1M tok)
MODEL_PRICING: dict[str, tuple[float, float]] = {
    "gpt-5.5": (1.25, 10.00),        # est (flagship)
    "gpt-5.4": (1.25, 10.00),        # est
    "gpt-5.4-mini": (0.75, 4.50),    # src
    "gpt-5.4-nano": (0.20, 1.25),    # src
    "gpt-5-mini": (0.25, 2.00),      # est
    "gpt-4.1-mini": (0.40, 1.60),    # src
    "gpt-4.1": (2.00, 8.00),         # est
    "o4-mini": (1.10, 4.40),         # est
    "claude-haiku-4-5": (1.00, 5.00),  # est
}

_PER_TOKEN = 1_000_000.0
_warned_unknown: set[str] = set()


def price_for(model: str | None) -> tuple[float, float] | None:
    """모델 id → (input, output) 단가. 정확 일치 우선, 없으면 **최장 prefix** 매칭.

    API 가 'gpt-5.4-mini-2026-03-17' 처럼 날짜 suffix 를 붙여 반환하는 경우를 흡수.
    """
    if not model:
        return None
    if model in MODEL_PRICING:
        return MODEL_PRICING[model]
    # 최장 prefix 매칭 (가장 구체적인 키 우선)
    for key in sorted(MODEL_PRICING, key=len, reverse=True):
        if model.startswith(key):
            return MODEL_PRICING[key]
    return None


def compute_cost(model: str | None, input_tokens: int, output_tokens: int) -> float:
    """토큰 사용량 → USD. 단가 미등록 모델은 0.0 + 1회 경고."""
    price = price_for(model)
    if price is None:
        if model and model not in _warned_unknown:
            _warned_unknown.add(model)
            logger.warning(
                "[cost] 단가 미등록 모델 '%s' — 비용 0 처리. cost.MODEL_PRICING 에 추가 필요.",
                model,
            )
        return 0.0
    in_price, out_price = price
    return (input_tokens * in_price + output_tokens * out_price) / _PER_TOKEN
