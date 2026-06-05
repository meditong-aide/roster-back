"""US-A3: UserMemoryRepo (Tier 2 user fact 저장소) — temporal validity 검증.

Scenarios:
1) ADD:             새 fact 추가 → query_valid_facts 반환 확인
2) UPDATE:          기존 fact UPDATE → 이전 row valid_to 채워짐 + 새 row 등장
3) DELETE:          fact DELETE → query_valid_facts 미반환 (valid_to 채워짐)
4) NOOP:            변경 없음 확인
5) group_id 격리:   두 다른 그룹의 동일 fact_type → 각 그룹 query는 자기 그룹 fact만 반환
6) temporal validity: valid_to가 지난 fact는 미반환 (수동 valid_to 설정 후 검증)
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from db.models import AgentUserMemory
from services.memory.user_repo import UserMemoryRepo


# ───────────────────────── fixtures ─────────────────────────


@pytest.fixture
def repo(db) -> UserMemoryRepo:
    return UserMemoryRepo(db=db)


def _fact(
    user_id: str = "user-1",
    group_id: str = "GRP001",
    fact_type: str = "nurse_pref",
    fact_text: str = "김민지는 나이트를 선호함",
    source: str = "USER_STATED",
    confidence: float = 1.0,
    evidence_session_id: str | None = None,
) -> dict:
    return {
        "user_id": user_id,
        "group_id": group_id,
        "fact_type": fact_type,
        "fact_text": fact_text,
        "source": source,
        "confidence": confidence,
        "evidence_session_id": evidence_session_id,
    }


# ──────────────────────── scenarios ────────────────────────


def test_add_fact_appears_in_query(repo, db):
    """시나리오 1: ADD — 새 fact 추가 후 query_valid_facts 에서 반환됨."""
    result = repo.apply_fact("ADD", _fact())

    assert result["action"] == "ADD"
    assert result["row"] is not None
    row = result["row"]
    assert row["fact_type"] == "nurse_pref"
    assert row["fact_text"] == "김민지는 나이트를 선호함"
    assert row["valid_to"] is None  # currently valid

    facts = repo.query_valid_facts("user-1", "GRP001")
    assert len(facts) == 1
    assert facts[0]["fact_text"] == "김민지는 나이트를 선호함"


def test_update_expires_old_row_and_inserts_new(repo, db):
    """시나리오 2: UPDATE — 이전 row valid_to 채워지고 새 row 등장."""
    # 초기 ADD
    repo.apply_fact("ADD", _fact(fact_text="초기 선호"))

    # UPDATE
    result = repo.apply_fact(
        "UPDATE",
        _fact(fact_text="갱신된 선호", source="AGENT_INFERRED"),
    )
    assert result["action"] == "UPDATE"
    new_row = result["row"]
    assert new_row["fact_text"] == "갱신된 선호"
    assert new_row["valid_to"] is None  # 새 row는 유효

    # DB에서 직접 확인 — 이전 row valid_to 채워짐
    all_rows = (
        db.query(AgentUserMemory)
        .filter(
            AgentUserMemory.user_id == "user-1",
            AgentUserMemory.group_id == "GRP001",
            AgentUserMemory.fact_type == "nurse_pref",
        )
        .order_by(AgentUserMemory.id.asc())
        .all()
    )
    assert len(all_rows) == 2
    assert all_rows[0].valid_to is not None  # 이전 row 만료
    assert all_rows[1].valid_to is None      # 새 row 유효
    assert all_rows[1].fact_text == "갱신된 선호"

    # query_valid_facts 는 새 row만 반환
    facts = repo.query_valid_facts("user-1", "GRP001")
    assert len(facts) == 1
    assert facts[0]["fact_text"] == "갱신된 선호"


def test_delete_soft_deletes_and_excludes_from_query(repo, db):
    """시나리오 3: DELETE — fact valid_to 채워짐 + query_valid_facts 미반환."""
    repo.apply_fact("ADD", _fact())

    result = repo.apply_fact("DELETE", _fact())
    assert result["action"] == "DELETE"
    deleted_row = result["row"]
    assert deleted_row["valid_to"] is not None  # 만료됨

    # query 에서 미반환
    facts = repo.query_valid_facts("user-1", "GRP001")
    assert facts == []


def test_noop_makes_no_changes(repo, db):
    """시나리오 4: NOOP — 변경 없음."""
    repo.apply_fact("ADD", _fact())

    result = repo.apply_fact("NOOP", _fact())
    assert result["action"] == "NOOP"
    assert result["row"] is None

    # 여전히 1개만 존재
    facts = repo.query_valid_facts("user-1", "GRP001")
    assert len(facts) == 1


def test_group_id_isolation(repo, db):
    """시나리오 5: group_id 격리 — 각 그룹 query는 자기 그룹 fact만 반환."""
    repo.apply_fact(
        "ADD",
        _fact(group_id="GRP_A", fact_text="A그룹 사실"),
    )
    repo.apply_fact(
        "ADD",
        _fact(group_id="GRP_B", fact_text="B그룹 사실"),
    )

    facts_a = repo.query_valid_facts("user-1", "GRP_A")
    facts_b = repo.query_valid_facts("user-1", "GRP_B")

    assert len(facts_a) == 1
    assert facts_a[0]["fact_text"] == "A그룹 사실"
    assert facts_a[0]["group_id"] == "GRP_A"

    assert len(facts_b) == 1
    assert facts_b[0]["fact_text"] == "B그룹 사실"
    assert facts_b[0]["group_id"] == "GRP_B"


def test_temporal_validity_expired_fact_excluded(repo, db):
    """시나리오 6: valid_to 가 과거인 fact는 query_valid_facts 에서 미반환."""
    repo.apply_fact("ADD", _fact())

    # DB에서 직접 valid_to를 과거 시점으로 설정
    row = (
        db.query(AgentUserMemory)
        .filter(
            AgentUserMemory.user_id == "user-1",
            AgentUserMemory.group_id == "GRP001",
            AgentUserMemory.fact_type == "nurse_pref",
        )
        .one()
    )
    row.valid_to = datetime.utcnow() - timedelta(hours=1)  # 1시간 전 만료
    db.flush()

    # query 에서 미반환
    facts = repo.query_valid_facts("user-1", "GRP001")
    assert facts == []


# ─────────────────── bonus: expire_fact / fact_type filter ───────────────────


def test_expire_fact_by_id(repo, db):
    """expire_fact — 단일 fact를 id로 만료."""
    result = repo.apply_fact("ADD", _fact())
    fact_id = result["row"]["id"]

    expired = repo.expire_fact(fact_id, group_id="GRP001", reason="테스트 만료")
    assert expired["valid_to"] is not None

    facts = repo.query_valid_facts("user-1", "GRP001")
    assert facts == []


def test_expire_fact_cross_group_blocked(repo, db):
    """expire_fact — 다른 group_id의 fact 만료 시도 시 ValueError."""
    result = repo.apply_fact("ADD", _fact(group_id="GRP_X"))
    fact_id = result["row"]["id"]

    with pytest.raises(ValueError, match="존재하지 않음"):
        repo.expire_fact(fact_id, group_id="GRP_OTHER")


def test_query_valid_facts_fact_type_filter(repo, db):
    """query_valid_facts — fact_type 필터 동작 확인."""
    repo.apply_fact("ADD", _fact(fact_type="nurse_pref", fact_text="선호 사실"))
    repo.apply_fact(
        "ADD",
        _fact(fact_type="shift_alias", fact_text="나이트=N"),
    )

    prefs = repo.query_valid_facts("user-1", "GRP001", fact_type="nurse_pref")
    aliases = repo.query_valid_facts("user-1", "GRP001", fact_type="shift_alias")
    all_facts = repo.query_valid_facts("user-1", "GRP001")

    assert len(prefs) == 1
    assert prefs[0]["fact_type"] == "nurse_pref"
    assert len(aliases) == 1
    assert aliases[0]["fact_type"] == "shift_alias"
    assert len(all_facts) == 2


def test_invalid_action_raises(repo):
    """유효하지 않은 action → ValueError."""
    with pytest.raises(ValueError, match="유효하지 않음"):
        repo.apply_fact("UNKNOWN", _fact())


def test_invalid_source_raises(repo):
    """유효하지 않은 source → ValueError."""
    with pytest.raises(ValueError, match="유효하지 않음"):
        repo.apply_fact("ADD", _fact(source="INVALID_SOURCE"))
