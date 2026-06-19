"""Wave-3: resolve_infeasibility skill 검증.

흐름:
  - 최근 FAILED RosterJob 이 없으면 found=False, 친화 메시지
  - FAILED 인데 payload 가 raw 문자열이면 옵션 없음 + 안내
  - FAILED + JSON payload 면 action_levers → options, trade_offs join,
    apply_hint 가 _internal 격리, 한국어 message 합성
"""

from __future__ import annotations

import json

from agents_v2.skills.registry import SKILL_REGISTRY, _ensure_loaded, run_skill

_ensure_loaded()


def test_skill_registered():
    assert (
        "resolve-infeasibility" in SKILL_REGISTRY
        or "resolve_infeasibility" in SKILL_REGISTRY
    )


# ── gates ────────────────────────────────────────────────


def test_missing_rbac_returns_error(db):
    res = run_skill(db, "resolve-infeasibility", {})
    assert "error" in res


def test_unknown_operation_returns_clarification(db, seed_data):
    res = run_skill(db, "resolve-infeasibility", {
        "group_id": seed_data["group_id"], "operation": "apply_option",
    })
    assert res.get("needs_clarification") is True


# ── empty paths ──────────────────────────────────────────


def test_no_recent_job(db, seed_data):
    res = run_skill(db, "resolve-infeasibility", {"group_id": seed_data["group_id"]})
    assert res["found"] is False
    assert "기록이 없" in res["message"]


def test_recent_job_not_failed(db, seed_data):
    from db.models import RosterJob
    job = RosterJob(
        job_id="job-success",
        office_id=seed_data["office_id"],
        group_id=seed_data["group_id"],
        nurse_id="N001",
        status="SUCCESS",
        progress=100,
    )
    db.add(job)
    db.flush()
    res = run_skill(db, "resolve-infeasibility", {"group_id": seed_data["group_id"]})
    assert res["found"] is False
    assert "실패하지 않았" in res["message"]


def test_failed_job_with_plain_string_returns_empty_options(db, seed_data):
    """payload 가 JSON 아니면 옵션 없음, 안전 fallback 메시지."""
    from db.models import RosterJob
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
    res = run_skill(db, "resolve-infeasibility", {"group_id": seed_data["group_id"]})
    assert res["found"] is True
    assert res["options"] == []
    assert "확인이 필요" in res["message"]


# ── happy path ───────────────────────────────────────────


def _payload_with_levers():
    """worker.py 가 저장하는 unrecoverable_payload 의 축약 형태."""
    return {
        "infeasibility": {
            "severity": "hard",
            "resolution_narrative": {
                "summary_ko": "A팀 최소 인원이 전체 수요를 초과합니다",
                "verified": True,
                "action_levers": [
                    {
                        "treatment_id": "lower_team_min_A",
                        "target_family": "TeamMin",
                        "config_key": "team_min",
                        "direction": "decrease",
                        "rationale_ko": "A팀 최소 인원을 1명으로 낮추기",
                        "covers_causes": ["TEAM_MIN_EXCEEDS_GLOBAL_NEED"],
                    },
                    {
                        "treatment_id": "increase_team_size_A",
                        "target_family": "TeamSize",
                        "config_key": "team_size",
                        "direction": "increase",
                        "rationale_ko": "A팀 활성 인원 보강",
                        "covers_causes": ["TEAM_MIN_EXCEEDS_GLOBAL_NEED"],
                    },
                ],
                "trade_offs": [
                    {
                        "treatment_id": "lower_team_min_A",
                        "trade_off_ko": "A팀 커버리지가 빠듯해질 수 있어요",
                    },
                ],
                "problem_list": [],
            },
            "treatment_recommendations": [
                {
                    "treatment_id": "lower_team_min_A",
                    "apply_hint": {
                        "action": "prefill",
                        "target": "nurse_management",
                        "sub": "team_setting",
                        "values": {"team": "A팀", "min": 1},
                    },
                },
                {
                    "treatment_id": "increase_team_size_A",
                    "apply_hint": {
                        "action": "navigate",
                        "target": "nurse_management",
                        "sub": "team_setting",
                    },
                },
            ],
        }
    }


def test_failed_job_lists_options_from_payload(db, seed_data):
    from db.models import RosterJob
    job = RosterJob(
        job_id="job-fail-json",
        office_id=seed_data["office_id"],
        group_id=seed_data["group_id"],
        nurse_id="N001",
        status="FAILED",
        progress=100,
        error_message=json.dumps(_payload_with_levers(), ensure_ascii=False),
    )
    db.add(job)
    db.flush()

    res = run_skill(db, "resolve-infeasibility", {"group_id": seed_data["group_id"]})
    assert res["found"] is True
    assert res["operation"] == "list_options"
    assert res["verified"] is True
    assert res["summary_ko"] == "A팀 최소 인원이 전체 수요를 초과합니다"

    opts = res["options"]
    assert len(opts) == 2
    ids = [o["treatment_id"] for o in opts]
    assert "lower_team_min_A" in ids and "increase_team_size_A" in ids

    # trade_off 가 treatment_id 로 조인됐는지
    lower = next(o for o in opts if o["treatment_id"] == "lower_team_min_A")
    assert lower["config_key"] == "team_min"
    assert lower["direction"] == "decrease"
    assert "빠듯" in lower["trade_off_ko"]
    inc = next(o for o in opts if o["treatment_id"] == "increase_team_size_A")
    assert "trade_off_ko" not in inc  # 매칭 없음

    # message 친화 텍스트 — raw enum 미노출
    msg = res["message"]
    assert "A팀 최소 인원을 1명으로 낮추기" in msg
    assert "TEAM_MIN_EXCEEDS_GLOBAL_NEED" not in msg
    assert "어떤 옵션으로 진행할까요" in msg

    # apply_hints 는 _internal 격리
    hints = res["_internal"]["apply_hints"]
    assert hints["lower_team_min_A"]["values"]["team"] == "A팀"
    assert hints["increase_team_size_A"]["action"] == "navigate"

    # treatment_id 같은 시스템 식별자는 top-level options 에는 노출되지만
    # raw payload 는 _internal 격리.
    assert "debug_payload" in res["_internal"]
    assert "debug_payload" not in res

    # ontology 부착: TeamMin family + runtime_lever=False (yaml 마킹된 stale lever)
    for opt in opts:
        if opt["target_family"] == "TeamMin":
            ont = opt.get("constraint")
            assert ont is not None
            assert ont["constraint_id"] == "TeamMin"
            assert ont["group"] == "CoverageConstraint"
            assert ont["default_severity"] == "hard"
            # treatment 단(또는 constraint 단)에서 runtime_lever=False 가 와야 함
            assert ont["runtime_lever"] is False
            # 사용자 노출용 라벨
            assert opt.get("engine_self_resolves") is True
