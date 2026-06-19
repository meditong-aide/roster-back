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


def test_failed_job_with_json_payload_attaches_ontology(db, seed_data):
    """worker.py 가 저장하는 JSON unrecoverable payload → query_generation_job 응답에
    1) 한국어 narrative (message)
    2) _internal.failure_ontology (reason_code → Constraint 매핑)
    가 잘 흘러나오는지 가드.
    """
    import json
    from db.models import RosterJob
    from agents_v2.skills.registry import run_skill

    payload = {
        "infeasibility": {
            "severity": "hard",
            "causes": [
                {"reason_code": "TEAM_MIN_EXCEEDS_GLOBAL_NEED", "node_id": "A팀"},
            ],
            "resolution_narrative": {
                "summary_ko": "A팀 최소 인원이 전체 수요를 초과합니다.",
                "problem_list": [
                    {"rendered_ko": "A팀 최소 2명 × 5일 = 10명 필요"},
                ],
                "action_levers": [
                    {"rationale_ko": "A팀 최소 인원을 1명으로 낮추기"},
                ],
                "trade_offs": [],
            },
            "hard_case": {"is_hard": False},
        }
    }
    job = RosterJob(
        job_id="job-fail-json",
        office_id=seed_data["office_id"],
        group_id=seed_data["group_id"],
        nurse_id="N001",
        status="FAILED",
        progress=100,
        error_message=json.dumps(payload, ensure_ascii=False),
    )
    db.add(job)
    db.flush()

    res = run_skill(db, "query-generation-job", {"group_id": seed_data["group_id"]})
    assert res["status"] == "FAILED"

    # 1) narrative 가 메시지에 반영됨 (raw enum 미노출).
    msg = res["message"]
    assert "근무표 생성이 실패" in msg
    assert "TEAM_MIN_EXCEEDS_GLOBAL_NEED" not in msg  # raw 코드 누출 금지

    # 2) infeasibility narrative dict 가 노출됨.
    assert "infeasibility" in res
    assert res["infeasibility"]["severity"] == "hard"

    # 3) ontology 부착이 _internal 에 격리됨.
    ont = res["_internal"]["failure_ontology"]
    assert ont["reason_code"] == "TEAM_MIN_EXCEEDS_GLOBAL_NEED"
    assert ont["severity"] == "hard"
    assert ont["ontology"]["constraint_id"] == "TeamMin"
    assert ont["ontology"]["mode"] == "precheck_blocked"

    # 4) raw debug_payload 도 _internal 격리.
    assert "debug_payload" in res["_internal"]
    assert "debug_payload" not in res  # top-level 금지


def test_failed_job_with_plain_string_does_not_attach_ontology(db, seed_data):
    """error_message 가 JSON 아닌 단순 문자열이면 ontology 부착 없이도 안전 fallback."""
    from db.models import RosterJob
    from agents_v2.skills.registry import run_skill

    job = RosterJob(
        job_id="job-fail-plain",
        office_id=seed_data["office_id"],
        group_id=seed_data["group_id"],
        nurse_id="N001",
        status="FAILED",
        progress=100,
        error_message="DB connection lost",
    )
    db.add(job)
    db.flush()

    res = run_skill(db, "query-generation-job", {"group_id": seed_data["group_id"]})
    assert res["status"] == "FAILED"
    # narrative 없음 → fallback 문구.
    assert "사유 메시지를 확인" in res["message"]
    # ontology 부착 없음.
    assert "failure_ontology" not in res["_internal"]
    assert "debug_payload" not in res["_internal"]
    assert "infeasibility" not in res


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
