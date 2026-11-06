"""log_feedback — 처리 불가 불만/건의/버그 접수 (agent_feedback 적재 + 정직 안내)."""
from agents_v2.middleware import execute_skill
from agents_v2.schemas.session_context import SessionContext
from db.models import AgentFeedback


def _ctx(role="HN"):
    return SessionContext(office_id="OFF001", group_id="GRP001", year=2026, month=8,
                          nurse_id="N001", nurse_name="김민지", user_role=role)


def test_bug_logged(db, seed_data):
    res = execute_skill(db, "log_feedback",
                        {"content": "관리자 권한 사용 제한 생겼습니다", "kind": "bug"}, _ctx())
    assert res.data.get("ok") is True and res.data["kind"] == "bug"
    rows = db.query(AgentFeedback).filter_by(group_id="GRP001").all()
    assert len(rows) == 1
    assert rows[0].kind == "bug" and "권한" in rows[0].content and rows[0].status == "open"
    # 정직 안내: '고쳤다'가 아니라 '접수/전달'
    assert "접수" in res.data["message"] and "직접 처리" in res.data["message"]


def test_suggestion_default_kind(db, seed_data):
    res = execute_skill(db, "log_feedback", {"content": "이런 화면 있으면 좋겠어요"}, _ctx())
    assert res.data.get("ok") is True
    # kind 미지정 → complaint 기본
    assert db.query(AgentFeedback).filter_by(group_id="GRP001").first().kind == "complaint"


def test_empty_content_clarifies(db, seed_data):
    res = execute_skill(db, "log_feedback", {"kind": "bug"}, _ctx())
    assert res.data.get("needs_clarification") is True
    assert db.query(AgentFeedback).count() == 0


def test_non_hn_can_submit(db, seed_data):
    # 피드백 접수는 일반 간호사도 가능(HN 전용 mutation 아님)
    res = execute_skill(db, "log_feedback", {"content": "버그요", "kind": "bug"}, _ctx(role="RN"))
    assert res.data.get("ok") is True and not res.data.get("permission_denied")


def test_collects_office_query_summary(db, seed_data):
    # 병원/원 발화/결론 수집 — office_id 는 ctx 주입, original_query/summary 는 LLM 전달
    res = execute_skill(db, "log_feedback", {
        "content": "관리자 권한 사용 제한",
        "kind": "bug",
        "original_query": "관리자 권한이 제한됐고 근무자 최대/최소도 못 바꿔요",
        "summary": "권한/계정 설정 문제 — 에이전트 처리 불가, 담당팀 확인 필요",
    }, _ctx())
    assert res.data.get("ok") is True
    row = db.query(AgentFeedback).filter_by(group_id="GRP001").first()
    assert row.office_id == "OFF001"
    assert row.original_query and "근무자 최대" in row.original_query
    assert row.summary and "담당팀" in row.summary
