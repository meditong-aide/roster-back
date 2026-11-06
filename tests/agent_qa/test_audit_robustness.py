"""US-R5: skill audit 가 세션을 오염시키지 않음 (agent_skill_invocation 부재 시 500 방지).

재현 버그: 운영 MSSQL 에 agent_skill_invocation 테이블이 없을 때 _write_skill_audit 의
실패한 INSERT flush 가 SQLAlchemy 세션을 PendingRollbackError 로 오염 → 같은 턴의
다음 스킬 쿼리가 500. 픽스: has_table 프로브로 부재 시 INSERT 자체를 회피(+savepoint 격리).

주의: conftest 의 pytest `db` 는 모든 모델 테이블(agent_skill_invocation 포함)을 만든다.
'테이블 부재' 시나리오는 테이블을 안 만든 별도 인메모리 세션으로 모사한다.
"""

from __future__ import annotations

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import agents_v2.middleware as mw
from agents_v2.schemas.session_context import SessionContext


def _ctx() -> SessionContext:
    return SessionContext(
        office_id="OFF", group_id="GRP", year=2026, month=5,
        nurse_id="n1", conversation_id="c1",
    )


def _session_without_audit_table():
    """audit 테이블이 없는 격리된 인메모리 세션 (공유 conftest 엔진 안 건드림)."""
    eng = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    return sessionmaker(bind=eng)()


def test_missing_audit_table_does_not_poison_session():
    """테이블 부재 → audit 호출 후에도 같은 세션 쿼리가 정상 (PendingRollbackError 없음)."""
    s = _session_without_audit_table()
    try:
        assert s.execute(text("SELECT 1")).scalar() == 1  # baseline
        mw._write_skill_audit(
            s, _ctx(), "query_schedule", {"scope": "nurse_info"}, "SUCCESS", None, 1.0
        )
        # 핵심: 세션 미오염 → 후속 쿼리 정상
        assert s.execute(text("SELECT 1")).scalar() == 1
        assert mw._audit_table_present is False  # 부재 1회 감지 → 캐시
    finally:
        s.close()


def test_disabled_audit_short_circuits_without_db_access():
    """부재 감지 후(=False)엔 db 를 건드리지 않고 즉시 반환 (db=None 이어도 무탈)."""
    mw._audit_table_present = False
    mw._write_skill_audit(
        None, _ctx(), "query_schedule", {"scope": "x"}, "SUCCESS", None, 1.0
    )  # raise 하면 실패


def test_two_audits_one_session_no_cascade():
    """한 세션에서 audit 2회(미존재) → 둘 다 무탈 + 사이/후 쿼리 정상 (턴 내 연쇄 500 방지)."""
    s = _session_without_audit_table()
    try:
        assert s.execute(text("SELECT 1")).scalar() == 1
        mw._write_skill_audit(s, _ctx(), "query_schedule", {"a": 1}, "SUCCESS", None, 1.0)
        assert s.execute(text("SELECT 1")).scalar() == 1  # 첫 audit 후
        mw._write_skill_audit(s, _ctx(), "update_person_attr", {"b": 2}, "SUCCESS", None, 1.0)
        assert s.execute(text("SELECT 1")).scalar() == 1  # 둘째 audit 후
    finally:
        s.close()


def test_audit_writes_row_when_table_exists(db):
    """테이블이 존재하면 기존처럼 audit row 가 기록된다(회귀 없음). conftest db 사용(테이블 존재)."""
    from db.models import AgentSkillInvocation

    db.execute(text("SELECT 1"))  # 트랜잭션 활성화 (begin_nested 전제)
    before = db.query(AgentSkillInvocation).count()
    try:
        mw._write_skill_audit(
            db, _ctx(), "query_schedule", {"scope": "nurse_info"}, "SUCCESS", None, 2.5
        )
        assert mw._audit_table_present is True
        assert db.query(AgentSkillInvocation).count() == before + 1
        assert db.execute(text("SELECT 1")).scalar() == 1  # 세션 정상
    finally:
        db.rollback()  # 기록 row 원복 (다른 테스트 오염 방지)
