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


# ── 엔진 실행 카드(resolution_options) 배선 ───────────────
# 갭: 엔진은 per-nurse 로 "누구의 무엇을 어떻게" 까지 특정한 카드를 내는데(프론트 모달이
# 원클릭 적용하는 그 목록), 에이전트는 family 수준 action_levers 만 읽어 화면보다 덜
# 아는 답을 냈다. 아래는 그 배선과 폴백 보존을 못 박는다.


def _banned_card_payload():
    """금지근무×강제OFF 개인모순 → 대안 2장 (mcs_trace.cause_to_resolution_options 형태)."""
    return {
        "_year": 2026, "_month": 8,
        "infeasibility": {
            "severity": "hard",
            "causes": [{"reason_code": "PERSONAL_INFEASIBLE"}],
            "resolution_options": [
                {"option_id": "cause:banned_release", "kind": "relax_constraint",
                 "source": "cause", "verified": False,
                 "title_ko": "문제 간호사 2명 금지근무 해제하고 다시 만들기",
                 "trade_off_ko": "겹치는 날의 금지근무(OFF 금지)를 풀어 필수 휴무가 가능해집니다.",
                 "changes": [{"nurse_id": "N002", "config_key": "banned_wanted",
                              "label_ko": "박지은 금지근무", "from": None, "to": "해제"}],
                 "banned_wanted_release": [{"nurse_id": "N002", "days": [15, 16]}],
                 "fix": {"mode": "auto_apply", "where": "nurse.banned_wanted",
                         "where_label_ko": "원티드 조정판 > 금지근무"}},
                {"option_id": "cause:allowed_add", "kind": "relax_constraint",
                 "source": "cause", "verified": False,
                 "title_ko": "문제 간호사 2명 근무유형에 D/E 추가하고 다시 만들기",
                 "trade_off_ko": "야간 전담 대신 주간/이브닝도 가능해져 병목이 풀립니다(역할 변경).",
                 "changes": [{"nurse_id": "N002", "config_key": "allowed_shifts",
                              "label_ko": "박지은 근무유형", "from": "N", "to": "D·E 추가"}],
                 "allowed_shift_add": [{"nurse_id": "N002", "add": ["D", "E"]}],
                 "fix": {"mode": "auto_apply", "where": "nurse.allowed_shifts",
                         "where_label_ko": "간호사 관리 > 근무 유형"}},
            ],
            "resolution_narrative": {
                "summary_ko": "개인 조건이 서로 모순됩니다.",
                # family 수준 lever 도 함께 있지만 카드가 이긴다.
                "action_levers": [{"treatment_id": "t9", "target_family": "CoverageMin",
                                   "config_key": "daily_shift_requirements",
                                   "direction": "decrease", "rationale_ko": "수요 낮추기",
                                   "covers_causes": []}],
                "trade_offs": [], "problem_list": [],
            },
            "hard_case": {"is_hard": True},
        },
    }


def _add_failed_job(db, seed_data, payload, job_id="job-cards"):
    from db.models import RosterJob

    db.add(RosterJob(
        job_id=job_id, office_id=seed_data["office_id"], group_id=seed_data["group_id"],
        nurse_id="N001", status="FAILED", progress=100,
        error_message=json.dumps(payload, ensure_ascii=False),
    ))
    db.flush()


def test_resolution_options_win_over_action_levers(db, seed_data):
    _add_failed_job(db, seed_data, _banned_card_payload())
    res = run_skill(db, "resolve-infeasibility", {"group_id": seed_data["group_id"]})

    assert res["options_source"] == "resolution_options"
    assert len(res["options"]) == 2
    titles = [o["title_ko"] for o in res["options"]]
    assert "문제 간호사 2명 금지근무 해제하고 다시 만들기" in titles
    # family 수준 lever 로 떨어지지 않았는지
    assert all("target_family" not in o for o in res["options"])


def test_card_exposes_who_and_what_not_just_family(db, seed_data):
    """카드의 값어치는 '누구의 무엇' — changes 를 사람이 읽는 줄로 내려야 한다."""
    _add_failed_job(db, seed_data, _banned_card_payload())
    res = run_skill(db, "resolve-infeasibility", {"group_id": seed_data["group_id"]})

    first = res["options"][0]
    assert first["changes_ko"] == ["박지은 금지근무: 해제"]
    assert first["where_ko"] == "원티드 조정판 > 금지근무"
    assert "금지근무" in first["trade_off_ko"]

    second = res["options"][1]
    assert second["changes_ko"] == ["박지은 근무유형: N → D·E 추가"]


def test_apply_payloads_isolated_to_internal(db, seed_data):
    """적용 페이로드는 사용자 노출용이 아니라 다음 행동의 재료 — _internal 격리."""
    _add_failed_job(db, seed_data, _banned_card_payload())
    res = run_skill(db, "resolve-infeasibility", {"group_id": seed_data["group_id"]})

    payloads = res["_internal"]["apply_payloads"]
    assert payloads["cause:banned_release"]["banned_wanted_release"] == [
        {"nurse_id": "N002", "days": [15, 16]}
    ]
    assert payloads["cause:allowed_add"]["allowed_shift_add"] == [
        {"nurse_id": "N002", "add": ["D", "E"]}
    ]
    for opt in res["options"]:
        assert not set(opt) & {"banned_wanted_release", "allowed_shift_add", "apply"}


def test_message_names_the_concrete_action(db, seed_data):
    _add_failed_job(db, seed_data, _banned_card_payload())
    res = run_skill(db, "resolve-infeasibility", {"group_id": seed_data["group_id"]})
    assert "금지근무 해제" in res["message"]
    assert "해결 옵션 2개" in res["message"]


def test_probe_card_apply_delta_captured(db, seed_data):
    """probe 카드는 apply(config delta) 를 싣는다 — 그것도 _internal 로."""
    payload = {
        "_year": 2026, "_month": 8,
        "infeasibility": {
            "severity": "hard", "causes": [{"reason_code": "Z"}],
            "resolution_options": [
                {"option_id": "probe:relax_night", "kind": "relax_constraint",
                 "source": "probe", "verified": True, "title_ko": "야간 필요인원 1 낮추기",
                 "changes": [{"config_key": "max_nig", "label_ko": "야간 최대", "from": 5, "to": 6}],
                 "trade_off_ko": "야간 부담이 늘어납니다.", "apply": {"max_nig": 6}},
            ],
            "resolution_narrative": {"summary_ko": None, "action_levers": [],
                                     "trade_offs": [], "problem_list": []},
            "hard_case": {"is_hard": False},
        },
    }
    _add_failed_job(db, seed_data, payload, job_id="job-probe")
    res = run_skill(db, "resolve-infeasibility", {"group_id": seed_data["group_id"]})

    assert res["options"][0]["verified"] is True
    assert res["options"][0]["changes_ko"] == ["야간 최대: 5 → 6"]
    assert res["_internal"]["apply_payloads"]["probe:relax_night"]["apply"] == {"max_nig": 6}


def test_falls_back_to_action_levers_when_no_cards(db, seed_data):
    """카드가 없으면 기존 경로 그대로 — 비회귀."""
    payload = {
        "_year": 2026, "_month": 8,
        "infeasibility": {
            "severity": "hard", "causes": [{"reason_code": "Y"}],
            "resolution_narrative": {
                "summary_ko": "수요 초과",
                "action_levers": [{"treatment_id": "t1", "target_family": "CoverageMin",
                                   "config_key": "daily_shift_requirements",
                                   "direction": "decrease", "rationale_ko": "일별 수요 1 낮추기",
                                   "covers_causes": ["Y"]}],
                "trade_offs": [], "problem_list": [],
            },
            "hard_case": {"is_hard": False},
        },
    }
    _add_failed_job(db, seed_data, payload, job_id="job-levers")
    res = run_skill(db, "resolve-infeasibility", {"group_id": seed_data["group_id"]})

    assert res["options_source"] == "action_levers"
    assert res["options"][0]["target_family"] == "CoverageMin"
    assert "apply_payloads" not in res["_internal"]
