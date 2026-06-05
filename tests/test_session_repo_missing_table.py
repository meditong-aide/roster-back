"""SessionMemoryRepo — agent_conversation 테이블 부재 시 Redis-only graceful degrade."""

from __future__ import annotations

import fakeredis
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from services.memory.session_repo import SessionMemoryRepo


@pytest.fixture
def empty_db_session():
    """metadata create 를 안 한 빈 SQLite — agent_conversation 자체가 없음."""
    engine = create_engine("sqlite:///:memory:")
    Session = sessionmaker(bind=engine)
    s = Session()
    yield s
    s.close()


@pytest.fixture
def redis_client():
    return fakeredis.FakeRedis(decode_responses=True)


@pytest.fixture(autouse=True)
def _reset_sot_flag():
    """각 테스트 사이 class-level flag 초기화 — 다른 테스트 오염 방지."""
    SessionMemoryRepo._sot_disabled = False
    yield
    SessionMemoryRepo._sot_disabled = False


def test_save_messages_no_table_falls_back_to_redis(empty_db_session, redis_client):
    repo = SessionMemoryRepo(db=empty_db_session, redis_client=redis_client)
    repo.save_messages(
        "sess-A", "user-1", "GRP001",
        [{"role": "user", "content": "안녕"}, {"role": "assistant", "content": "안녕하세요"}],
    )
    # SOT 비활성으로 전환됨
    assert SessionMemoryRepo._sot_disabled is True
    # Redis 에는 정상 저장됨
    cached = redis_client.lrange("sess:GRP001:sess-A:msgs", 0, -1)
    assert len(cached) == 2


def test_load_messages_no_table_returns_redis_cache(empty_db_session, redis_client):
    repo = SessionMemoryRepo(db=empty_db_session, redis_client=redis_client)
    repo.save_messages(
        "sess-B", "user-1", "GRP001",
        [{"role": "user", "content": "테스트"}],
    )
    # save 가 SOT 를 비활성화한 상태에서 load
    loaded = repo.load_messages("sess-B", group_id="GRP001")
    assert loaded == [{"role": "user", "content": "테스트"}]


def test_load_messages_unknown_session_returns_empty(empty_db_session, redis_client):
    repo = SessionMemoryRepo(db=empty_db_session, redis_client=redis_client)
    # 캐시도 없고 테이블도 없음 — 빈 리스트
    assert repo.load_messages("ghost-session", group_id="GRP001") == []


def test_variable_memory_round_trip_no_table(empty_db_session, redis_client):
    repo = SessionMemoryRepo(db=empty_db_session, redis_client=redis_client)
    repo.save_variable_memory(
        "sess-C", "user-1", "GRP001", {"last_scope": "wanted", "year": 2026},
    )
    assert SessionMemoryRepo._sot_disabled is True
    vm = repo.load_variable_memory("sess-C", group_id="GRP001")
    assert vm == {"last_scope": "wanted", "year": 2026}


def test_touch_ttl_no_table_only_refreshes_redis(empty_db_session, redis_client):
    repo = SessionMemoryRepo(db=empty_db_session, redis_client=redis_client)
    repo.save_messages("sess-D", "user-1", "GRP001", [{"role": "user", "content": "x"}])
    # touch 가 예외 없이 통과해야 함
    repo.touch_ttl("sess-D", group_id="GRP001")
    # Redis TTL 갱신됨
    assert redis_client.ttl("sess:GRP001:sess-D:msgs") > 0
