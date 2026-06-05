"""US-A5: AgentMemoryAudit — audit 기록 통합 검증.

시나리오:
  1. apply_fact(ADD)    → audit 1 row (action=WRITE)
  2. apply_fact(UPDATE) → audit 2 row (EXPIRE + WRITE)
  3. apply_fact(DELETE) → audit 1 row (action=DELETE)
  4. apply_fact(NOOP)   → audit 0 row
  5. expire_fact()      → audit 1 row (action=EXPIRE)
  6. 각 audit row 필드 검증 (action, tier, row_id, who, why, timestamp 존재)
"""

from __future__ import annotations

import pytest

from db.models import AgentMemoryAudit, AgentUserMemory
from services.memory.user_repo import UserMemoryRepo


# ───────────────────────── fixtures ─────────────────────────


@pytest.fixture
def repo(db) -> UserMemoryRepo:
    return UserMemoryRepo(db=db)


def _fact(
    user_id: str = "audit-user",
    group_id: str = "AUDIT_GRP",
    fact_type: str = "nurse_pref",
    fact_text: str = "audit 테스트 사실",
    source: str = "USER_STATED",
    confidence: float = 1.0,
    evidence_session_id: str | None = "sess-audit-001",
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


def _audit_rows(db, who: str = "audit-user") -> list[AgentMemoryAudit]:
    """audit-user 로 기록된 모든 audit row 반환 (timestamp 오름차순)."""
    return (
        db.query(AgentMemoryAudit)
        .filter(AgentMemoryAudit.who == who)
        .order_by(AgentMemoryAudit.id.asc())
        .all()
    )


# ──────────────────────── scenarios ────────────────────────


def test_add_produces_write_audit(repo, db):
    """시나리오 1: ADD → audit 1 row (action=WRITE, tier=USER)."""
    result = repo.apply_fact("ADD", _fact())
    new_id = result["row"]["id"]

    rows = _audit_rows(db)
    assert len(rows) == 1

    a = rows[0]
    assert a.action == "WRITE"
    assert a.tier == "USER"
    assert a.row_id == new_id
    assert a.who == "audit-user"
    assert a.why is not None
    assert "ADD" in a.why
    assert a.agent_run_id == "sess-audit-001"
    assert a.timestamp is not None


def test_update_produces_expire_and_write_audit(repo, db):
    """시나리오 2: UPDATE → audit 2 row (EXPIRE 이전 row + WRITE 새 row)."""
    add_result = repo.apply_fact("ADD", _fact())
    old_id = add_result["row"]["id"]

    # clear ADD audit so we only see UPDATE audits
    # (actually we keep them — just slice from position 1)
    pre_count = len(_audit_rows(db))

    update_result = repo.apply_fact(
        "UPDATE",
        _fact(fact_text="갱신된 사실", source="AGENT_INFERRED"),
    )
    new_id = update_result["row"]["id"]

    all_rows = _audit_rows(db)
    update_audits = all_rows[pre_count:]  # only UPDATE-triggered audits

    assert len(update_audits) == 2

    expire_row = update_audits[0]
    assert expire_row.action == "EXPIRE"
    assert expire_row.tier == "USER"
    assert expire_row.row_id == old_id
    assert expire_row.who == "audit-user"
    assert expire_row.timestamp is not None

    write_row = update_audits[1]
    assert write_row.action == "WRITE"
    assert write_row.tier == "USER"
    assert write_row.row_id == new_id
    assert write_row.who == "audit-user"
    assert write_row.timestamp is not None


def test_delete_produces_delete_audit(repo, db):
    """시나리오 3: DELETE → audit 1 row (action=DELETE)."""
    add_result = repo.apply_fact("ADD", _fact())
    fact_id = add_result["row"]["id"]
    pre_count = len(_audit_rows(db))

    repo.apply_fact("DELETE", _fact())

    all_rows = _audit_rows(db)
    delete_audits = all_rows[pre_count:]

    assert len(delete_audits) == 1
    a = delete_audits[0]
    assert a.action == "DELETE"
    assert a.tier == "USER"
    assert a.row_id == fact_id
    assert a.who == "audit-user"
    assert a.timestamp is not None


def test_noop_produces_no_audit(repo, db):
    """시나리오 4: NOOP → audit 0 row 추가."""
    repo.apply_fact("ADD", _fact())
    pre_count = len(_audit_rows(db))

    repo.apply_fact("NOOP", _fact())

    after_count = len(_audit_rows(db))
    assert after_count == pre_count  # NOOP은 audit 미기록


def test_expire_fact_produces_expire_audit(repo, db):
    """시나리오 5: expire_fact() → audit 1 row (action=EXPIRE)."""
    add_result = repo.apply_fact("ADD", _fact())
    fact_id = add_result["row"]["id"]
    pre_count = len(_audit_rows(db))

    repo.expire_fact(fact_id, group_id="AUDIT_GRP", reason="테스트 만료 이유")

    all_rows = _audit_rows(db)
    expire_audits = all_rows[pre_count:]

    assert len(expire_audits) == 1
    a = expire_audits[0]
    assert a.action == "EXPIRE"
    assert a.tier == "USER"
    assert a.row_id == fact_id
    assert a.who == "audit-user"
    assert a.why == "테스트 만료 이유"
    assert a.timestamp is not None


def test_audit_row_fields_complete(repo, db):
    """시나리오 6: 각 audit row 필드 완결성 — action, tier, row_id, who, why, timestamp 모두 존재."""
    repo.apply_fact("ADD", _fact(evidence_session_id="sess-check"))

    rows = _audit_rows(db)
    assert len(rows) >= 1

    for row in rows:
        assert row.action is not None and row.action != ""
        assert row.tier is not None and row.tier != ""
        assert row.row_id is not None
        assert row.who is not None
        assert row.why is not None
        assert row.timestamp is not None


def test_audit_rows_ordered_by_timestamp(repo, db):
    """timestamp 기반 조회 — audit rows id 오름차순 정렬."""
    repo.apply_fact("ADD", _fact(fact_type="type_a", fact_text="첫 번째"))
    repo.apply_fact("ADD", _fact(fact_type="type_b", fact_text="두 번째"))

    rows = (
        db.query(AgentMemoryAudit)
        .filter(AgentMemoryAudit.who == "audit-user")
        .order_by(AgentMemoryAudit.timestamp.asc())
        .all()
    )
    assert len(rows) == 2
    assert rows[0].id < rows[1].id
