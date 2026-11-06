"""원티드 월별 메모 조회 — query_schedule(wanted_memo) 역할별 스코프.

dev 신규(d0e39d8)의 에이전트 노출. 개인이 쓴 글이라 HN 은 병동 전체, 일반 간호사는
본인 것만 보여야 한다(관리보드 엔드포인트의 403 게이트와 같은 의미).
"""
from datetime import datetime

import pytest

from agents_v2.middleware import execute_skill
from agents_v2.schemas.session_context import SessionContext
from db.models import WantedMonthlyMemo


def _ctx(role="HN", nurse_id="N001", nurse_name="김민지"):
    return SessionContext(office_id="OFF001", group_id="GRP001", year=2026, month=8,
                          nurse_id=nurse_id, nurse_name=nurse_name, user_role=role)


@pytest.fixture
def memos(db, seed_data):
    db.add_all([
        WantedMonthlyMemo(nurse_id="N002", group_id="GRP001", year=2026, month=8,
                          memo="이번 달 시험이라 나이트 힘들어요", updated_at=datetime(2026, 7, 20)),
        WantedMonthlyMemo(nurse_id="N003", group_id="GRP001", year=2026, month=8,
                          memo="셋째 주 결혼식", updated_at=datetime(2026, 7, 21)),
        # 다른 달 · 메모 삭제 상태(NULL) — 결과에 섞이면 안 된다.
        WantedMonthlyMemo(nurse_id="N002", group_id="GRP001", year=2026, month=7,
                          memo="지난달 메모", updated_at=datetime(2026, 6, 20)),
        WantedMonthlyMemo(nurse_id="N004", group_id="GRP001", year=2026, month=8,
                          memo=None, updated_at=datetime(2026, 7, 22)),
    ])
    db.flush()


def test_hn_sees_whole_ward(db, memos):
    res = execute_skill(db, "query_schedule",
                        {"scope": "wanted_memo", "year": 2026, "month": 8}, _ctx("HN"))
    d = res.data
    assert d["count"] == 2 and d["scope_kind"] == "ward"
    assert {m["nurse"] for m in d["memos"]} == {"박지은", "이수정"}


def test_null_memo_and_other_month_excluded(db, memos):
    res = execute_skill(db, "query_schedule",
                        {"scope": "wanted_memo", "year": 2026, "month": 8}, _ctx("HN"))
    texts = {m["memo"] for m in res.data["memos"]}
    assert "지난달 메모" not in texts
    assert all(m["memo"] for m in res.data["memos"])


def test_hn_can_scope_to_one_nurse(db, memos):
    res = execute_skill(db, "query_schedule",
                        {"scope": "wanted_memo", "year": 2026, "month": 8,
                         "nurse_name": "박지은"}, _ctx("HN"))
    assert res.data["count"] == 1 and res.data["scope_kind"] == "nurse"
    assert res.data["memos"][0]["nurse"] == "박지은"


def test_general_nurse_sees_only_own(db, memos):
    res = execute_skill(db, "query_schedule",
                        {"scope": "wanted_memo", "year": 2026, "month": 8},
                        _ctx("RN", nurse_id="N002", nurse_name="박지은"))
    assert res.data["count"] == 1
    assert res.data["memos"][0]["nurse"] == "박지은"


def test_general_nurse_blocked_from_others(db, memos):
    """이름을 지정해도 남의 메모로 넘어가지 않는다."""
    res = execute_skill(db, "query_schedule",
                        {"scope": "wanted_memo", "year": 2026, "month": 8,
                         "nurse_name": "이수정"},
                        _ctx("RN", nurse_id="N002", nurse_name="박지은"))
    assert "error" in res.data
    assert "볼 수 없습니다" in res.data["error"]


def test_empty_month_message(db, seed_data):
    res = execute_skill(db, "query_schedule",
                        {"scope": "wanted_memo", "year": 2026, "month": 9}, _ctx("HN"))
    assert res.data["count"] == 0 and "없어요" in res.data["message"]


def test_scope_declared_in_schema():
    from agents_v2.skills.descriptions import SKILL_TOOLS

    qs = next(t for t in SKILL_TOOLS if t.get("name") == "query_schedule")
    assert "wanted_memo" in qs["parameters"]["properties"]["scope"]["enum"]
    assert "leave_summary" in qs["parameters"]["properties"]["scope"]["enum"]


# ── 배포 순서 가드 (코드 먼저 · DDL 나중) ────────────────


class _Q:
    def outerjoin(self, *a, **k): return self
    def filter(self, *a, **k): return self
    def order_by(self, *a, **k): return self
    def all(self): raise RuntimeError("no such table: wanted_monthly_memo")


class _OtherQ(_Q):
    def all(self): raise RuntimeError("connection reset by peer")


class _StubSession:
    def __init__(self, q): self._q, self.rolled = q, False
    def query(self, *a, **k): return self._q
    def rollback(self): self.rolled = True


def test_missing_table_reads_as_empty_not_500():
    """테이블이 아직 없으면 '메모 없음' 으로 넘어간다 — 한마디에 대화가 끊기면 안 된다."""
    from agents_v2.tools import wanted_tools

    s = _StubSession(_Q())
    assert wanted_tools.read_monthly_memos(s, "GRP001", 2026, 8) == []
    assert s.rolled is True, "실패 쿼리로 오염된 트랜잭션을 되돌려야 뒤 쿼리가 안 죽는다"


def test_other_db_errors_still_raise():
    """권한·커넥션 오류까지 삼키면 설정이 조용히 무시된 채 '왜 안 되지' 로 남는다."""
    import pytest as _pytest

    from agents_v2.tools import wanted_tools

    with _pytest.raises(RuntimeError):
        wanted_tools.read_monthly_memos(_StubSession(_OtherQ()), "GRP001", 2026, 8)
