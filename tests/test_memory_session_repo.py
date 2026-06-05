"""US-A1: SessionMemoryRepo (MSSQL SOT + Redis hot cache) — write-through 검증.

Scenarios:
1) write-through: save_messages 호출 후 (Redis List + MSSQL row) 둘 다 존재
2) cache miss:    Redis 비운 뒤 load_messages → MSSQL fetch + Redis 재충전
3) TTL refresh:   save_messages 호출마다 Redis TTL ≈ 24h 갱신
4) group_id 격리: 다른 group_id 로 같은 session_id 접근 시 read 분리
"""

from __future__ import annotations

import json

import fakeredis
import pytest

from db.models import AgentConversation, AgentConversationMessage
from services.memory.session_repo import SessionMemoryRepo


@pytest.fixture
def redis_client():
    return fakeredis.FakeRedis(decode_responses=True)


@pytest.fixture
def repo(db, redis_client) -> SessionMemoryRepo:
    return SessionMemoryRepo(db=db, redis_client=redis_client)


# ───────────────────────── helpers ─────────────────────────


def _msg(role: str, content: str, **extra) -> dict:
    m = {"role": role, "content": content}
    m.update(extra)
    return m


# ──────────────────────── scenarios ────────────────────────


def test_write_through_persists_to_mssql_and_redis(repo, db, redis_client):
    """save_messages → Redis List + MSSQL row 둘 다 존재 검증."""
    session_id = "sess-A1-write-through"
    user_id = "user-1"
    group_id = "GRP001"

    messages = [
        _msg("system", "you are an agent"),
        _msg("user", "안녕"),
        _msg("assistant", "안녕하세요"),
    ]

    repo.save_messages(session_id, user_id, group_id, messages)

    # (a) Redis List 존재
    key = f"sess:{group_id}:{session_id}:msgs"
    raw = redis_client.lrange(key, 0, -1)
    assert len(raw) == 3
    decoded = [json.loads(x) for x in raw]
    assert decoded[0]["role"] == "system"
    assert decoded[1]["content"] == "안녕"
    assert decoded[2]["role"] == "assistant"

    # (b) MSSQL row 존재
    rows = (
        db.query(AgentConversationMessage)
        .filter(AgentConversationMessage.session_id == session_id)
        .order_by(AgentConversationMessage.turn_idx.asc())
        .all()
    )
    assert len(rows) == 3
    assert [r.role for r in rows] == ["system", "user", "assistant"]
    assert rows[1].content == "안녕"

    # Conversation row 도 존재 + group_id 격리
    conv = (
        db.query(AgentConversation)
        .filter(AgentConversation.session_id == session_id)
        .one()
    )
    assert conv.group_id == group_id
    assert conv.user_id == user_id


def test_cache_miss_falls_back_to_mssql_and_repopulates(repo, db, redis_client):
    """Redis 비운 뒤 load_messages → MSSQL fetch + Redis 재충전 검증."""
    session_id = "sess-A1-cache-miss"
    user_id = "user-1"
    group_id = "GRP001"

    messages = [
        _msg("user", "원티드 미제출자 보여줘"),
        _msg("assistant", "이수정 1명입니다."),
    ]
    repo.save_messages(session_id, user_id, group_id, messages)

    key = f"sess:{group_id}:{session_id}:msgs"
    # Redis hot cache 강제 삭제 (cache miss 시뮬레이션)
    redis_client.delete(key)
    assert redis_client.lrange(key, 0, -1) == []

    # load → MSSQL fallback + Redis 재충전
    loaded = repo.load_messages(session_id, group_id=group_id)
    assert len(loaded) == 2
    assert loaded[0]["role"] == "user"
    assert loaded[0]["content"] == "원티드 미제출자 보여줘"
    assert loaded[1]["content"] == "이수정 1명입니다."

    # Redis 재충전 확인
    raw_after = redis_client.lrange(key, 0, -1)
    assert len(raw_after) == 2
    # TTL 도 함께 세팅 (≤ 24h, > 0)
    ttl_after = redis_client.ttl(key)
    assert 0 < ttl_after <= SessionMemoryRepo.TTL_SECONDS


def test_ttl_is_refreshed_on_each_save(repo, redis_client):
    """save_messages 두 번 호출 → Redis TTL 매번 갱신 검증."""
    session_id = "sess-A1-ttl-refresh"
    user_id = "user-1"
    group_id = "GRP001"

    key = f"sess:{group_id}:{session_id}:msgs"

    repo.save_messages(session_id, user_id, group_id, [_msg("user", "first")])
    ttl1 = redis_client.ttl(key)
    assert ttl1 > 0
    assert ttl1 <= SessionMemoryRepo.TTL_SECONDS

    # TTL 을 임의로 깎아둔 뒤 두번째 save 가 다시 24h 로 끌어올리는지 확인
    redis_client.expire(key, 60)
    assert redis_client.ttl(key) <= 60

    repo.save_messages(
        session_id,
        user_id,
        group_id,
        [_msg("user", "first"), _msg("assistant", "second")],
    )
    ttl2 = redis_client.ttl(key)
    # 24h 근처로 복원
    assert ttl2 > 60
    assert ttl2 <= SessionMemoryRepo.TTL_SECONDS


def test_group_id_isolation_on_read(repo, db, redis_client):
    """다른 group_id 로 같은 session_id 접근 시 read 분리 검증.

    실제로는 동일 session_id 를 다른 group 에 재할당하면 _ensure_conversation 이
    ValueError 를 던지므로, 격리 검증은 (a) 다른 group_id 로 load 시 빈 결과
    (b) 같은 session_id 를 다른 group 으로 재사용 차단 두 측면에서 확인한다.
    """
    session_id = "sess-A1-iso"
    user_id = "user-1"
    group_a = "GRPA"
    group_b = "GRPB"

    repo.save_messages(
        session_id,
        user_id,
        group_a,
        [_msg("user", "A 그룹 메시지")],
    )

    # (a) group_b 로 load 시도 → 격리되어 빈 결과
    other = repo.load_messages(session_id, group_id=group_b)
    assert other == []

    # group_a 로는 정상 read
    same = repo.load_messages(session_id, group_id=group_a)
    assert len(same) == 1
    assert same[0]["content"] == "A 그룹 메시지"

    # (b) 같은 session_id 를 다른 group 으로 재사용 시도 → ValueError
    with pytest.raises(ValueError, match="group_id mismatch"):
        repo.save_messages(
            session_id,
            user_id,
            group_b,
            [_msg("user", "B 그룹 hijack 시도")],
        )

    # Redis key 도 group 별로 분리
    key_a = f"sess:{group_a}:{session_id}:msgs"
    key_b = f"sess:{group_b}:{session_id}:msgs"
    assert redis_client.lrange(key_a, 0, -1) != []
    assert redis_client.lrange(key_b, 0, -1) == []


# ─────────────────── bonus: variable_memory / touch_ttl ───────────────────


def test_variable_memory_write_through(repo, db, redis_client):
    """VM dict 저장/조회 + cache miss fallback 검증."""
    session_id = "sess-A1-vm"
    user_id = "user-1"
    group_id = "GRP001"

    vm = {"last_query_scope": "wanted", "selected_nurses": ["N001", "N002"]}
    repo.save_variable_memory(session_id, user_id, group_id, vm)

    # Redis Hash
    key = f"sess:{group_id}:{session_id}:vm"
    cached = redis_client.hgetall(key)
    assert json.loads(cached["last_query_scope"]) == "wanted"
    assert json.loads(cached["selected_nurses"]) == ["N001", "N002"]

    # MSSQL vm_json
    conv = (
        db.query(AgentConversation)
        .filter(AgentConversation.session_id == session_id)
        .one()
    )
    assert json.loads(conv.vm_json)["last_query_scope"] == "wanted"

    # cache miss fallback
    redis_client.delete(key)
    loaded = repo.load_variable_memory(session_id, group_id=group_id)
    assert loaded == vm
    # 재충전 확인
    assert redis_client.hgetall(key) != {}


def test_touch_ttl_refreshes_both_keys(repo, redis_client):
    session_id = "sess-A1-touch"
    user_id = "user-1"
    group_id = "GRP001"

    repo.save_messages(session_id, user_id, group_id, [_msg("user", "hi")])
    repo.save_variable_memory(session_id, user_id, group_id, {"k": "v"})

    msgs_key = f"sess:{group_id}:{session_id}:msgs"
    vm_key = f"sess:{group_id}:{session_id}:vm"
    redis_client.expire(msgs_key, 30)
    redis_client.expire(vm_key, 30)

    repo.touch_ttl(session_id, group_id=group_id)
    assert redis_client.ttl(msgs_key) > 30
    assert redis_client.ttl(vm_key) > 30
