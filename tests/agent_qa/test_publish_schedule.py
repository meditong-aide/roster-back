"""publish_schedule — 에이전트는 마감하지 않는다(int-0b). 안내만 하고 쓰기 0.

예전 스킬은 roster_service.publish_schedule_service(브랜치 복제본)로 직접 발행했다. dev 정본 마감의
잠금·재마감 변경 확인·장부·앵커·알림을 하나도 따르지 않아(N81·M2) 병합 직후 쓰기를 껐다.
"""
from pathlib import Path

import pytest

from agents_v2.errors import ErrorType, classify
from agents_v2.middleware import execute_skill
from agents_v2.schemas.session_context import SessionContext
from db.models import IssuedRoster, IssuedRosterSnapshot, Schedule


def _hn():
    return SessionContext(office_id="OFF001", group_id="GRP001", year=2026, month=4,
                          nurse_id="N001", nurse_name="김민지", user_role="HN")


def _statuses(db) -> list:
    return sorted(s.status for s in db.query(Schedule).filter_by(group_id="GRP001").all())


@pytest.mark.parametrize("preview_only", [True, False, None])
def test_publish_never_writes(db, seed_data, preview_only):
    """preview_only 를 무엇으로 주든(LLM 이 false 로 보내도) 마감·스냅샷·상태 변경이 없다."""
    before = _statuses(db)
    args = {"year": 2026, "month": 4}
    if preview_only is not None:
        args["preview_only"] = preview_only
    res = execute_skill(db, "publish_schedule", args, _hn())

    assert res.data.get("publish_disabled") is True, res.data
    assert db.query(IssuedRosterSnapshot).count() == 0
    assert db.query(IssuedRoster).count() == 0
    assert _statuses(db) == before


def test_publish_guidance_is_not_an_approval(db, seed_data):
    """승인 대기를 만들지 않는다 — '응' 으로 실행될 쓰기가 없다."""
    res = execute_skill(db, "publish_schedule", {"year": 2026, "month": 4}, _hn())
    assert classify(res.data) is ErrorType.OK
    assert res.data["summary"]["year"] == 2026 and res.data["summary"]["month"] == 4
    assert res.data["summary"]["already_issued"] is False
    assert res.data["navigate_target"] == "roster_create"
    assert "마감" in res.data["message"]


def test_publish_guidance_says_when_month_is_already_issued(db, seed_data):
    """대상 해석이 마감본을 고르면(이미 마감) '마감하라' 대신 이미 마감됐다고 알린다."""
    db.add(IssuedRoster(seq_no=1, version=1, office_id="OFF001", group_id="GRP001",
                        nurse_id="N001", schedule_id="SCH202604V1", is_active=True))
    db.flush()
    res = execute_skill(db, "publish_schedule", {"year": 2026, "month": 4}, _hn())
    assert res.data["summary"]["already_issued"] is True
    assert "이미 마감" in res.data["message"]


def test_no_agent_code_calls_publish_service():
    """에이전트 패키지 어디에도 브랜치 발행 복제본 호출이 없다(레지스트리의 발행 쓰기 0)."""
    agents_dir = Path(__file__).resolve().parents[2] / "app" / "agents_v2"
    hits = [str(p) for p in agents_dir.rglob("*.py")
            if "publish_schedule_service" in p.read_text(encoding="utf-8")]
    assert hits == []
