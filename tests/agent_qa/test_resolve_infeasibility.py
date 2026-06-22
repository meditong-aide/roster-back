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


def test_year_month_filter_finds_failed_job_for_month(db, seed_data):
    """year/month 지정 시 그 달의 FAILED job 만 식별. 다른 month FAILED 무시.

    worker._year/_month 메타 → _job_dict 의 year/month 노출 → 여기서 활용.
    """
    import json
    from db.models import RosterJob

    # 8월 FAILED — 식별 대상이 아님
    payload_aug = {
        "_year": 2026, "_month": 8,
        "infeasibility": {
            "severity": "hard", "causes": [{"reason_code": "X"}],
            "resolution_narrative": {"summary_ko": "8월 다른 실패", "action_levers": [], "trade_offs": [], "problem_list": []},
            "hard_case": {"is_hard": False},
        },
    }
    # 7월 FAILED — 진짜 대상
    payload_jul = {
        "_year": 2026, "_month": 7,
        "infeasibility": {
            "severity": "hard", "causes": [{"reason_code": "Y"}],
            "resolution_narrative": {
                "summary_ko": "7월 A팀 최소 초과",
                "action_levers": [{"treatment_id": "t1", "target_family": "CoverageMin",
                                   "config_key": "daily_shift_requirements", "direction": "decrease",
                                   "rationale_ko": "일별 수요 1 낮추기", "covers_causes": ["Y"]}],
                "trade_offs": [], "problem_list": [],
            },
            "hard_case": {"is_hard": False},
        },
    }
    db.add(RosterJob(
        job_id="job-aug", office_id=seed_data["office_id"], group_id=seed_data["group_id"],
        nurse_id="N001", status="FAILED", progress=100,
        error_message=json.dumps(payload_aug, ensure_ascii=False),
    ))
    db.add(RosterJob(
        job_id="job-jul", office_id=seed_data["office_id"], group_id=seed_data["group_id"],
        nurse_id="N001", status="FAILED", progress=100,
        error_message=json.dumps(payload_jul, ensure_ascii=False),
    ))
    db.flush()

    res = run_skill(db, "resolve-infeasibility", {
        "group_id": seed_data["group_id"], "year": 2026, "month": 7,
    })
    assert res["found"] is True
    assert res["year"] == 2026
    assert res["month"] == 7
    assert res["summary_ko"] == "7월 A팀 최소 초과"
    assert len(res["options"]) == 1
    assert res["options"][0]["target_family"] == "CoverageMin"


def test_year_month_filter_empty_when_no_failed_for_month(db, seed_data):
    """year/month 지정했는데 그 달 FAILED 없으면 친화 안내."""
    import json
    from db.models import RosterJob

    # 8월만 FAILED 있고, 사용자는 7월 요청
    payload = {
        "_year": 2026, "_month": 8,
        "infeasibility": {
            "severity": "hard", "causes": [{"reason_code": "X"}],
            "resolution_narrative": {"summary_ko": "...", "action_levers": [], "trade_offs": [], "problem_list": []},
            "hard_case": {"is_hard": False},
        },
    }
    db.add(RosterJob(
        job_id="job-aug", office_id=seed_data["office_id"], group_id=seed_data["group_id"],
        nurse_id="N001", status="FAILED", progress=100,
        error_message=json.dumps(payload, ensure_ascii=False),
    ))
    db.flush()

    res = run_skill(db, "resolve-infeasibility", {
        "group_id": seed_data["group_id"], "year": 2026, "month": 7,
    })
    assert res["found"] is False
    assert res["year"] == 2026
    assert res["month"] == 7
    assert "2026" in res["message"] and "7월" in res["message"]
    assert "찾지 못했" in res["message"]


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

    # D1-A: runtime_lever=False 인 TeamMin 옵션은 auto_resolved_options 로 분리.
    actionable = res["options"]
    auto_resolved = res["auto_resolved_options"]
    assert len(actionable) == 1
    assert actionable[0]["treatment_id"] == "increase_team_size_A"
    assert len(auto_resolved) == 1
    assert auto_resolved[0]["treatment_id"] == "lower_team_min_A"

    # trade_off 가 treatment_id 로 조인됐는지 (auto_resolved 쪽 검증)
    lower = auto_resolved[0]
    assert lower["config_key"] == "team_min"
    assert lower["direction"] == "decrease"
    assert "빠듯" in lower["trade_off_ko"]
    inc = actionable[0]
    assert "trade_off_ko" not in inc  # 매칭 없음

    # message 친화 텍스트 — raw enum 미노출
    msg = res["message"]
    # actionable 옵션 1건의 rationale 만 message 에 노출 (TeamSize 보강)
    assert "A팀 활성 인원 보강" in msg
    # auto_resolved 1건은 카운트만 노출
    assert "1건은 엔진이 자동으로 처리" in msg
    assert "TEAM_MIN_EXCEEDS_GLOBAL_NEED" not in msg
    assert "어떤 옵션으로 진행할까요" in msg

    # apply_hints 는 _internal 격리 (auto_resolved 도 포함됨 — 디버깅용)
    hints = res["_internal"]["apply_hints"]
    assert hints["lower_team_min_A"]["values"]["team"] == "A팀"
    assert hints["increase_team_size_A"]["action"] == "navigate"

    # raw payload 는 _internal 격리.
    assert "debug_payload" in res["_internal"]
    assert "debug_payload" not in res

    # ontology 부착: auto_resolved 의 TeamMin 옵션은 runtime_lever=False + engine_self_resolves=True
    ont = lower["constraint"]
    assert ont["constraint_id"] == "TeamMin"
    assert ont["group"] == "CoverageConstraint"
    assert ont["default_severity"] == "hard"
    assert ont["runtime_lever"] is False
    assert lower["engine_self_resolves"] is True
    # actionable 옵션(TeamSize)은 ontology 에 없어 constraint 메타 없음 또는 runtime_lever 미지정.
    assert inc.get("engine_self_resolves") is not True
