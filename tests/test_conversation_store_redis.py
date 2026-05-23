"""US-A2: ConversationStore — SessionMemoryRepo (MSSQL SOT + Redis) 백엔드 검증.

Conversation dataclass 인터페이스 (`.id`, `.messages`, `.variable_memory`,
`.pending_approval`) 는 호환 유지 — 호출자 코드 변경 X.

Scenarios:
1) 영속성: 새 ConversationStore 인스턴스에서도 같은 conv_id load 시 동일 데이터
2) TTL: Redis 키에 24h 가 설정됨
3) group_id 격리: 같은 conv_id 를 다른 group 으로 read 시 차단
4) pending_approval 저장/복원
5) save_variable_memory 호출이 기존 pending_approval 을 erase 하지 않음
6) set_pending_approval(None) 으로 클리어
"""

from __future__ import annotations

import pytest

from agents_v2.conversation import ConversationStore, Conversation
from services.memory.session_repo import SessionMemoryRepo
from services.memory.redis_client import reset_redis_singleton


@pytest.fixture(autouse=True)
def _reset_redis():
    """각 테스트 사이 fakeredis 싱글톤 초기화 — 격리."""
    reset_redis_singleton()
    yield
    reset_redis_singleton()


# ────────────────────── Scenario 1: persistence ──────────────────────


def test_persistence_across_store_instances(db):
    """ConversationStore 가 SessionMemoryRepo 백엔드로 영속화 → 새 인스턴스에서도 read 가능."""
    store_a = ConversationStore()
    conv = store_a.create(db, user_id="u1", group_id="G1")

    messages = [
        {"role": "user", "content": "안녕"},
        {"role": "assistant", "content": "안녕하세요"},
    ]
    store_a.save_messages(
        db, conv.id, messages,
        user_id="u1", group_id="G1",
    )

    # 완전히 새 ConversationStore 인스턴스 — 메모리 dict 잔재 없음
    store_b = ConversationStore()
    loaded = store_b.get(db, conv.id, group_id="G1")
    assert loaded is not None
    assert isinstance(loaded, Conversation)
    assert loaded.id == conv.id
    assert len(loaded.messages) == 2
    assert loaded.messages[0]["content"] == "안녕"
    assert loaded.messages[1]["role"] == "assistant"


# ────────────────────── Scenario 2: TTL 24h ──────────────────────


def test_ttl_24h_applied_on_save(db):
    """save_messages 후 Redis TTL ≈ 24h 적용 확인."""
    from services.memory.redis_client import get_redis

    store = ConversationStore()
    conv = store.create(db, user_id="u1", group_id="G1")
    store.save_messages(
        db, conv.id, [{"role": "user", "content": "x"}],
        user_id="u1", group_id="G1",
    )

    r = get_redis()
    key = f"sess:G1:{conv.id}:msgs"
    ttl = r.ttl(key)
    # SessionMemoryRepo.TTL_SECONDS == 86400 (24h)
    assert ttl > 0
    assert ttl <= SessionMemoryRepo.TTL_SECONDS
    # 충분히 큰 값이어야 함 — 기존 1h(3600) 보다 큼
    assert ttl > 3600


# ────────────────────── Scenario 3: group_id 격리 ──────────────────────


def test_group_id_isolation(db):
    """같은 conv_id 를 group_a 로 만들고 group_b 로 read 시도 → 격리."""
    store = ConversationStore()
    conv = store.create(db, user_id="u1", group_id="GRP_A")
    store.save_messages(
        db, conv.id, [{"role": "user", "content": "A 그룹 메시지"}],
        user_id="u1", group_id="GRP_A",
    )

    # 다른 group_id 로 같은 conv_id 접근 — 격리되어 None
    loaded_b = store.get(db, conv.id, group_id="GRP_B")
    assert loaded_b is None

    # 같은 group_id 로는 정상 read
    loaded_a = store.get(db, conv.id, group_id="GRP_A")
    assert loaded_a is not None
    assert loaded_a.messages[0]["content"] == "A 그룹 메시지"


# ────────────────────── Scenario 4: pending_approval ──────────────────────


def test_pending_approval_roundtrip(db):
    """pending_approval 저장/복원 — Conversation.pending_approval 로 노출."""
    store = ConversationStore()
    conv = store.create(db, user_id="u1", group_id="G1")

    preview = {
        "skill_name": "bulk_mutation",
        "preview_only": True,
        "args": {"scope": "schedule", "nurse_ids": ["N001"]},
        "affected_count": 3,
    }
    store.set_pending_approval(
        db, conv.id, preview,
        user_id="u1", group_id="G1",
    )

    # 새 인스턴스에서 load 후 pending_approval 확인
    loaded = ConversationStore().get(db, conv.id, group_id="G1")
    assert loaded is not None
    assert loaded.pending_approval is not None
    assert loaded.pending_approval["skill_name"] == "bulk_mutation"
    assert loaded.pending_approval["affected_count"] == 3
    assert loaded.pending_approval["args"]["scope"] == "schedule"

    # variable_memory 에는 pending_approval reserved key 가 노출되지 않아야 함
    assert "__pending_approval__" not in loaded.variable_memory


# ────────────────────── Scenario 5: VM save preserves pending ──────────────────────


def test_save_variable_memory_preserves_pending_approval(db):
    """save_variable_memory 호출이 기존 pending_approval 을 지우지 않음."""
    store = ConversationStore()
    conv = store.create(db, user_id="u1", group_id="G1")

    # 먼저 pending_approval 세팅
    store.set_pending_approval(
        db, conv.id, {"skill_name": "x", "args": {}},
        user_id="u1", group_id="G1",
    )

    # 이후 VM 만 업데이트
    store.save_variable_memory(
        db, conv.id, {"last_query": "wanted"},
        user_id="u1", group_id="G1",
    )

    loaded = store.get(db, conv.id, group_id="G1")
    assert loaded is not None
    assert loaded.variable_memory.get("last_query") == "wanted"
    # pending_approval 이 보존되어야 함
    assert loaded.pending_approval is not None
    assert loaded.pending_approval["skill_name"] == "x"


# ────────────────────── Scenario 6: pending clear ──────────────────────


def test_set_pending_approval_none_clears(db):
    """set_pending_approval(None) 으로 클리어 — pending_approval == None."""
    store = ConversationStore()
    conv = store.create(db, user_id="u1", group_id="G1")
    store.set_pending_approval(
        db, conv.id, {"skill_name": "y"},
        user_id="u1", group_id="G1",
    )

    # 클리어
    store.set_pending_approval(
        db, conv.id, None,
        user_id="u1", group_id="G1",
    )

    loaded = store.get(db, conv.id, group_id="G1")
    assert loaded is not None
    assert loaded.pending_approval is None


# ────────────────────── Scenario 7: get_or_create 재사용 ──────────────────────


def test_get_or_create_returns_existing(db):
    """동일 conv_id 로 get_or_create 시 새로 만들지 않고 기존 row 반환."""
    store = ConversationStore()
    conv1 = store.get_or_create(db, None, user_id="u1", group_id="G1")
    store.save_messages(
        db, conv1.id, [{"role": "user", "content": "hello"}],
        user_id="u1", group_id="G1",
    )

    # 같은 conv_id 로 호출 — 기존 데이터 그대로
    conv2 = store.get_or_create(db, conv1.id, user_id="u1", group_id="G1")
    assert conv2.id == conv1.id
    assert len(conv2.messages) == 1
    assert conv2.messages[0]["content"] == "hello"

    # None 으로 호출 — 새 ID
    conv3 = store.get_or_create(db, None, user_id="u1", group_id="G1")
    assert conv3.id != conv1.id
    assert conv3.messages == []
