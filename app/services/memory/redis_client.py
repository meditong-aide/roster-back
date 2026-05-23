"""Redis 클라이언트 (싱글톤) — fakeredis fallback 포함 + production env-gate.

env REDIS_URL 이 설정되어 있고 ping 성공 시 real Redis, 그렇지 않으면
fakeredis 로 자동 전환한다.

production env-gate:
  ENVIRONMENT=production 환경에서 REDIS_URL 미설정 또는 연결 실패 시
  fakeredis 로 silent fallback 하지 않고 RuntimeError 발생.
  multi-worker uvicorn 에서 worker 별 fakeredis 인스턴스 분리로 인한
  silent 데이터 corruption 방지.
"""

from __future__ import annotations

import logging
import os
from typing import Optional

import redis as _redis

logger = logging.getLogger(__name__)

_redis_singleton: Optional[_redis.Redis] = None

_PRODUCTION_ENVS = frozenset({"production", "prod"})


def _is_production() -> bool:
    return os.getenv("ENVIRONMENT", "dev").strip().lower() in _PRODUCTION_ENVS


def _build_real_redis(url: str) -> Optional[_redis.Redis]:
    try:
        client = _redis.Redis.from_url(url, decode_responses=True)
        client.ping()
        return client
    except Exception as exc:
        if _is_production():
            raise RuntimeError(
                f"[redis_client] production REDIS_URL connection failed "
                f"(url={url}): {exc}. fakeredis fallback 차단 — "
                f"REDIS_URL 또는 Redis 서버 상태를 확인하십시오."
            ) from exc
        logger.warning(
            "[redis_client] real redis connection failed (url=%s): %s — falling back to fakeredis",
            url,
            exc,
        )
        return None


def _build_fake_redis() -> _redis.Redis:
    import fakeredis

    return fakeredis.FakeRedis(decode_responses=True)


def get_redis() -> _redis.Redis:
    """REDIS_URL 기반 connection 싱글톤. 실패 시 fakeredis fallback (production 제외)."""
    global _redis_singleton
    if _redis_singleton is not None:
        return _redis_singleton

    url = os.getenv("REDIS_URL", "").strip()
    client: Optional[_redis.Redis] = None

    if url:
        client = _build_real_redis(url)
    else:
        if _is_production():
            raise RuntimeError(
                "[redis_client] ENVIRONMENT=production 인데 REDIS_URL 이 설정되지 않았습니다. "
                "multi-worker 환경에서 fakeredis 로 silent fallback 시 worker 별 "
                "데이터 분리로 인한 corruption 위험. REDIS_URL 환경 변수를 설정하십시오."
            )
        logger.warning(
            "[redis_client] REDIS_URL not set — using fakeredis (in-memory)."
        )

    if client is None:
        client = _build_fake_redis()

    _redis_singleton = client
    return _redis_singleton


def reset_redis_singleton() -> None:
    """테스트 격리용 — 싱글톤 초기화."""
    global _redis_singleton
    _redis_singleton = None
