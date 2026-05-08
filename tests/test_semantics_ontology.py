from __future__ import annotations

from services.semantics import (
    attach_constraint_ontology,
    attach_reason_code_ontology,
    extract_reason_code,
    get_default_ontology,
)
from services.semantics.ontology import OntologyConflictScenario
from services.precheck.team_grade_precheck import PrecheckInput, PrecheckNurse, run_precheck


def test_ontology_loader_resolves_aliases_and_modes():
    onto = get_default_ontology()
    entry = onto.get_constraint("TEAM_MIN_EXCEEDS_GLOBAL_NEED")
    assert entry is not None
    assert entry.constraint_id == "TeamMin"
    assert onto.get_parent("TeamMin") == "CoverageConstraint"
    assert onto.can_bypass("FixedWanted", "BoundaryTransitionBan") is True
    assert onto.can_bypass("FixedWanted", "ConsecutiveWorkLimit") is False


def test_attach_reason_code_ontology_from_message():
    payload = attach_reason_code_ontology(
        message="[reason_code=DAY_ZERO_COVERAGE] Infeasible 진단: 1일 필수 근무 미배정",
        severity="hard",
        evidence={"day": 1},
    )
    assert payload["reason_code"] == "DAY_ZERO_COVERAGE"
    assert payload["ontology"]["constraint_id"] == "CoverageMin"
    assert payload["ontology"]["group"] == "CoverageConstraint"


def test_attach_constraint_ontology_unknown_family_passthrough():
    fact = {"reason_code": "SOMETHING_UNKNOWN", "mode": "enforced"}
    attached = attach_constraint_ontology(fact)
    assert attached == fact


def test_precheck_issue_contains_ontology_metadata():
    inp = PrecheckInput(
        num_days=30,
        nurses=[
            PrecheckNurse(
                nurse_id="N1",
                team_id=1,
                join_day=0,
                leave_day=29,
            )
        ],
        teams=[1],
        roster_config={"use_mid": True, "daily_shift_requirements": {"D": 1, "E": 1, "N": 1}},
        team_coverage={},
        grade_constraints={},
    )
    result = run_precheck(inp)
    assert result["status"] == "HAS_ISSUES"
    issue = result["issues"][0]
    assert issue["reason_code"] == "MID_REQUIRED_MISSING"
    assert issue["ontology"]["constraint_id"] == "ConfigIntegrity"
    assert issue["ontology"]["mode"] == "precheck_blocked"


def test_extract_reason_code_returns_none_for_plain_message():
    assert extract_reason_code("plain message") is None


def test_relaxation_priority_present_for_known_families():
    onto = get_default_ontology()
    # nurse welfare 류는 5
    assert onto.get_relaxation_priority("OffCap") == 5
    assert onto.get_relaxation_priority("NightRecovery") == 5
    # 풀기 쉬운 류는 낮음
    assert onto.get_relaxation_priority("TeamGradeHandoff") == 1
    assert onto.get_relaxation_priority("TeamMin") == 2
    assert onto.get_relaxation_priority("GradeMax") == 2


def test_scope_explosion_present_for_known_families():
    onto = get_default_ontology()
    assert onto.get_scope_explosion("BoundaryTransitionBan") == "high"
    assert onto.get_scope_explosion("OffCap") == "low"
    assert onto.get_scope_explosion("CoverageMin") == "medium"


def test_unknown_family_returns_none_for_new_fields():
    onto = get_default_ontology()
    assert onto.get_relaxation_priority("ImaginaryFamily") is None
    assert onto.get_scope_explosion("ImaginaryFamily") is None


def test_conflict_scenarios_loaded_with_required_fields():
    onto = get_default_ontology()
    assert len(onto.conflict_scenarios) >= 4
    for s in onto.conflict_scenarios:
        assert isinstance(s, OntologyConflictScenario)
        assert s.scenario_id
        assert s.involved_families  # non-empty
        assert s.trigger_condition
        assert s.why_infeasible
        assert s.suggested_relaxation
        assert s.detection_hint
    ids = {s.scenario_id for s in onto.conflict_scenarios}
    assert "BTBAN_VS_FIXED_SEQUENCE" in ids
    assert "TEAM_MIN_VS_GRADE_MAX_INTERSECTION" in ids


def test_scenarios_involving_filters_correctly():
    onto = get_default_ontology()
    fixed_wanted_scenarios = onto.scenarios_involving("FixedWanted")
    assert len(fixed_wanted_scenarios) >= 2
    assert any(s.scenario_id == "BTBAN_VS_FIXED_SEQUENCE" for s in fixed_wanted_scenarios)
    # alias resolution은 family id로 해석되어야 함
    via_alias = onto.scenarios_involving("TEAM_MIN_EXCEEDS_GLOBAL_NEED")
    assert any("TeamMin" in s.involved_families for s in via_alias)
    # 알려지지 않은 family
    assert onto.scenarios_involving("ImaginaryFamily") == []


def test_ontology_version_bumped_to_2():
    onto = get_default_ontology()
    assert onto.version >= 2


def test_monthly_night_cap_is_separate_family_from_night_recovery():
    onto = get_default_ontology()
    # 분리된 family 둘 다 존재
    assert onto.get_constraint("NightRecovery") is not None
    assert onto.get_constraint("MonthlyNightCap") is not None
    # alias 가 NightRecovery 가 아닌 MonthlyNightCap 으로 해석
    assert onto.resolve_alias("MONTHLY_NIGHT_CAPACITY_SHORTAGE") == "MonthlyNightCap"
    # NightRecovery 의 aliases 에서 MONTHLY_* 제거됨
    nr = onto.get_constraint("NightRecovery")
    assert "MONTHLY_NIGHT_CAPACITY_SHORTAGE" not in nr.aliases
    # 진단 메시지가 다름
    assert nr.explanation_template != onto.get_constraint("MonthlyNightCap").explanation_template


def test_ban_night_before_fixed_off_registered():
    onto = get_default_ontology()
    e = onto.get_constraint("BanNightBeforeFixedOff")
    assert e is not None
    assert e.parent == "NurseLocalConstraint"
    assert e.relaxation_priority == 4
    # 휴가/공가 trigger 명시
    assert "휴가" in e.notes or "공가" in e.notes


def test_ban_n_before_vacation_scenario_removed_because_bypass_handles_it():
    # BAN_N_BEFORE_VACATION_VS_FIXED_N 시나리오는 거짓이라 제거됨.
    # 코드가 (n, prev_d) in fixed 일 때 ban 룰을 자동 bypass 하므로
    # 사용자가 fixed_wanted N + 휴가/공가 fixed 조합을 둬도 INFEASIBLE 발생 X.
    onto = get_default_ontology()
    sids = {s.scenario_id for s in onto.conflict_scenarios}
    assert "BAN_N_BEFORE_VACATION_VS_FIXED_N" not in sids


def test_ban_night_before_fixed_off_notes_documents_bypass():
    onto = get_default_ontology()
    e = onto.get_constraint("BanNightBeforeFixedOff")
    assert "bypass" in (e.notes or "").lower() or "스킵" in (e.notes or "")


def test_off_window_is_derived_family_with_no_direct_actions():
    onto = get_default_ontology()
    ow = onto.get_constraint("OffWindow")
    assert ow is not None
    assert ow.parent == "NurseLocalConstraint"
    assert ow.relaxation_priority == 5
    # notes 에 derived 임이 명시
    assert ow.notes is not None
    assert "upstream" in ow.notes.lower() or "derived" in ow.notes.lower()


def test_off_window_supported_actions_is_empty():
    from services.constraint_impact.control import supported_actions_for
    assert supported_actions_for("OffWindow") == []


def test_fixed_wanted_bypasses_cleaned_no_dangling_names():
    onto = get_default_ontology()
    fw = onto.get_override("FixedWanted")
    assert fw is not None
    # dangling 이름 제거되었는지 확인
    assert "InitialForbiddenOverride" not in fw.bypasses
    assert "ProfileShiftLimit" not in fw.bypasses
    # 모든 bypasses 항목이 실제 ontology 에 정의된 family 인지 확인
    for fam in fw.bypasses:
        assert onto.get_constraint(fam) is not None, f"bypass target {fam} 가 등록 안 됨"
    for fam in fw.does_not_bypass:
        assert onto.get_constraint(fam) is not None, f"does_not_bypass target {fam} 가 등록 안 됨"


def test_boundary_transition_ban_notes_documents_dual_mechanism():
    onto = get_default_ontology()
    btb = onto.get_constraint("BoundaryTransitionBan")
    assert btb.notes is not None
    assert "cross-month" in btb.notes.lower() or "cross_month" in btb.notes.lower()
    assert "within-month" in btb.notes.lower() or "within_month" in btb.notes.lower()


def test_off_cap_notes_documents_o_exact_fallback():
    onto = get_default_ontology()
    cap = onto.get_constraint("OffCap")
    assert cap.notes is not None
    assert "o_exact" in cap.notes
    assert "off_days" in cap.notes


def test_weekend_off_only_registered_as_standalone_family():
    onto = get_default_ontology()
    wf = onto.get_constraint("WeekendOffOnly")
    assert wf is not None
    assert wf.parent == "NurseLocalConstraint"
    assert "weekday" in wf.scope
    # OFFCAP scenario 에 WeekendOffOnly 포함됨
    scen = next((s for s in onto.conflict_scenarios if s.scenario_id == "OFFCAP_VS_WEEKEND_OFF_NARROW_WINDOW"), None)
    assert scen is not None
    assert "WeekendOffOnly" in scen.involved_families
