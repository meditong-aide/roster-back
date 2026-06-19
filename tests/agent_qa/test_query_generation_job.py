"""Wave-1: query_generation_job skill 검증.

읽기 전용 — generation_tools.get_latest_job 위임.
"""

from __future__ import annotations

import pytest

from agents_v2.skills.registry import SKILL_REGISTRY, _ensure_loaded

_ensure_loaded()


def test_skill_registered():
    assert "query-generation-job" in SKILL_REGISTRY or "query_generation_job" in SKILL_REGISTRY


def test_missing_group_id_returns_error(db):
    from agents_v2.skills.registry import run_skill
    res = run_skill(db, "query-generation-job", {})
    assert isinstance(res, dict)
    assert "error" in res


def test_no_job_record_returns_not_found(db, seed_data):
    """seed_data 는 job 을 만들지 않음 → found=False."""
    from agents_v2.skills.registry import run_skill
    res = run_skill(db, "query-generation-job", {"group_id": seed_data["group_id"]})
    assert isinstance(res, dict)
    assert res.get("found") is False
    assert "message" in res


def test_returns_job_status_when_present(db, seed_data):
    """RosterJob 1건 생성 후 호출하면 found=True 와 human status."""
    from db.models import RosterJob
    from agents_v2.skills.registry import run_skill

    job = RosterJob(
        job_id="job-test-1",
        office_id=seed_data["office_id"],
        group_id=seed_data["group_id"],
        nurse_id="N001",
        status="RUNNING",
        progress=42,
    )
    db.add(job)
    db.flush()

    res = run_skill(db, "query-generation-job", {"group_id": seed_data["group_id"]})
    assert res["found"] is True
    # UX 가드: job_id 는 top-level 노출 금지(_internal 로 격리).
    assert "job_id" not in res
    assert res["_internal"]["job_id"] == "job-test-1"
    assert res["status"] == "RUNNING"
    assert res["status_human"] == "실행 중"
    assert res["progress"] == 42
    # 자연어 message + 진행률 포함.
    assert "message" in res and "%" in res["message"] and "42" in res["message"]


@pytest.mark.parametrize(
    "raw_status,expected_human",
    [
        ("QUEUED", "대기 중"),
        ("RUNNING", "실행 중"),
        ("SUCCESS", "성공"),
        ("FAILED", "실패"),
        ("UNKNOWN", "UNKNOWN"),  # unknown 은 그대로 노출
    ],
)
def test_human_status_mapping(db, seed_data, raw_status, expected_human):
    from db.models import RosterJob
    from agents_v2.skills.registry import run_skill

    job = RosterJob(
        job_id=f"job-{raw_status.lower()}",
        office_id=seed_data["office_id"],
        group_id=seed_data["group_id"],
        nurse_id="N001",
        status=raw_status,
        progress=100,
    )
    db.add(job)
    db.flush()

    res = run_skill(db, "query-generation-job", {"group_id": seed_data["group_id"]})
    assert res["found"] is True
    assert res["status"] == raw_status
    assert res["status_human"] == expected_human
    # 모든 status 에 자연어 message — UX 보장.
    assert isinstance(res.get("message"), str) and res["message"].strip()
    # 시스템 식별자는 _internal 격리.
    assert "job_id" not in res
