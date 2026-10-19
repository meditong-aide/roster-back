"""생성 결과 등급 최소 미달 보고(constraint_impact `_evaluate_grade`)가 엔진과 같게 세는지.

성남 61병동-RN 2026-11: 고년차를 1등급(A·B팀)·2등급(C팀)으로 나누고 2등급 최소 1명을 넣었다.
엔진은 2등급 요구를 1·2등급 누적으로 채우는데 보고는 2등급만 세고, 엔진이 덮어쓰는
1등급 기본 floor 까지 세어 66건(실제 고년차 빈 근무 1건)을 '하드 위반' 으로 알렸다.
"""
from types import SimpleNamespace

from services.constraint_impact.simulation import _evaluate_grade
from services.roster_create_service import _ensure_grade1_default, _fetch_grade_config_dict, _violation_counts_by_family
from utils.roster_quality import _ontology_violations

CONFIGURED = {"D": {"1": 0, "2": 1, "3": 0}}


def _snapshot(grade_config, grades=(1, 2, 3), need=2):
    return SimpleNamespace(
        config_payload={"grade_strategy": "COMBINED", "grade_config": grade_config,
                        "daily_shift_requirements": {"D": need}},
        nurses=[SimpleNamespace(nurse_index=i, grade=g) for i, g in enumerate(grades)],
    )


def _atoms(cells):
    """cells: {(nurse_index, day): shift_code}"""
    return {k: SimpleNamespace(shift_code=v) for k, v in cells.items()}


def _engine_config(configured=CONFIGURED, keep_configured=True):
    cfg = {"use_dynamic_scaling": True, "allow_soft_fallback": False,
           "constraints_json": _ensure_grade1_default(configured), "constraints_max_json": {}}
    if keep_configured:
        cfg["configured_constraints_json"] = configured
    return cfg


def _bad(evals):
    return sorted((e.details["day"], e.details["grade"]) for e in evals if not e.valid)


def test_higher_grade_fills_lower_grade_min_like_engine():
    # day0: 1등급(nurse0)+3등급 → 2등급 요구 충족(누적) · day1: 3등급만 → 미달
    atoms = _atoms({(0, 0): "D", (2, 0): "D", (2, 1): "D"})
    evals = _evaluate_grade(_snapshot(_engine_config()), atoms, {0, 1})
    assert _bad(evals) == [(2, 2)]
    miss = [e for e in evals if not e.valid][0]
    assert miss.details["counted_grades"] == [1, 2]


def test_engine_default_floor_not_reported_when_hospital_set_zero():
    # 2등급(nurse1)만 선 날 — 1등급 floor 는 못 채웠지만 병원 요구(2등급 누적)는 충족
    atoms = _atoms({(1, 0): "D", (2, 0): "D"})
    evals = _evaluate_grade(_snapshot(_engine_config()), atoms, {0})
    assert _bad(evals) == []
    assert {e.details["grade"] for e in evals} == {2}


def test_legacy_config_without_configured_key_reports_floor_too():
    atoms = _atoms({(1, 0): "D", (2, 0): "D"})
    evals = _evaluate_grade(_snapshot(_engine_config(keep_configured=False)), atoms, {0})
    assert _bad(evals) == [(1, 1)]


def test_target_uses_engine_dynamic_scaling():
    # 필요 2명인데 병원 최소 2·3등급 각 2 → 엔진은 비율 2/4 로 각 1명을 요구한다
    configured = {"D": {"2": 2, "3": 2}}
    cfg = {"use_dynamic_scaling": True, "constraints_json": configured,
           "constraints_max_json": {}, "configured_constraints_json": configured}
    atoms = _atoms({(1, 0): "D", (2, 0): "D", (2, 1): "D"})
    evals = _evaluate_grade(_snapshot(cfg, need=2), atoms, {0, 1})
    assert {(e.details["day"], e.details["grade"]): e.details["need"] for e in evals} == {
        (1, 2): 1, (1, 3): 1, (2, 2): 1, (2, 3): 1}
    # day1(인덱스 1): 3등급만 → 2등급 누적 미달, 3등급 누적(1~3)은 충족
    assert _bad(evals) == [(2, 2)]


def test_case_variant_shift_keys_do_not_hide_hospital_requirement():
    # 'd' 의 양수 요구를 'D' 의 0 이 덮어쓰면 안 된다(엔진은 두 키를 모두 D 로 적용)
    configured = {"d": {"2": 1}, "D": {"2": 0}}
    cfg = {"use_dynamic_scaling": True, "constraints_json": configured,
           "constraints_max_json": {}, "configured_constraints_json": configured}
    atoms = _atoms({(2, 0): "D"})
    evals = _evaluate_grade(_snapshot(cfg), atoms, {0})
    assert _bad(evals) == [(1, 2)]


def test_case_variant_keys_with_engine_floor_report_once():
    # 'd' 의 병원 값이 'D' 에 붙은 floor(1등급 부재 → 2등급으로 이양)까지 보고하게 만들면 안 된다
    configured = {"d": {"2": 1}, "D": {"2": 0}}
    atoms = _atoms({(1, 0): "D"})
    evals = _evaluate_grade(_snapshot(_engine_config(configured), grades=(2, 3), need=1), atoms, {0})
    assert [e.node_id for e in evals if not e.valid] == ["grade_min:2:0:D"]


def test_legacy_non_cascade_reports_missing_grade_population(monkeypatch):
    from services.constraints import grade_constraints

    monkeypatch.setattr(grade_constraints, "_GRADE_CASCADE_ENABLED", False)
    configured = {"D": {"1": 1}}
    cfg = {"use_dynamic_scaling": True, "constraints_json": configured,
           "constraints_max_json": {}, "configured_constraints_json": configured}
    atoms = _atoms({(0, 0): "D"})
    evals = _evaluate_grade(_snapshot(cfg, grades=(None, None)), atoms, {0})
    assert _bad(evals) == [(1, 1)]
    monkeypatch.setattr(grade_constraints, "_GRADE_CASCADE_ENABLED", True)
    assert _evaluate_grade(_snapshot(cfg, grades=(None, None)), atoms, {0}) == []


def test_fetch_grade_config_keeps_hospital_original():
    row = SimpleNamespace(use_dynamic_scaling=1, allow_soft_fallback=0,
                          constraints_json_text='{"D": {"1": 0, "2": 1}}', constraints_max_json_text="{}")
    db = SimpleNamespace(execute=lambda *a, **k: SimpleNamespace(fetchone=lambda: row))
    out = _fetch_grade_config_dict(db, "o", "g")
    assert out["constraints_json"]["D"] == {"1": 1, "2": 1}
    assert out["constraints_json"]["N"] == {"1": 1}
    assert out["configured_constraints_json"] == {"D": {"1": 0, "2": 1}}


def test_quality_alert_counts_full_total_not_capped_list():
    vlist = [{"node_id": f"grade_min:2:{d}:D", "details": {"day": d + 1, "shift": "D", "need": 1, "assigned": 0}}
             for d in range(50)]
    counts = _violation_counts_by_family(
        [SimpleNamespace(node_id=f"grade_min:2:{d}:D") for d in range(66)]
        + [SimpleNamespace(node_id="coverage:min:3:E")]
    )
    assert counts == {"grade_min": 66, "coverage:min": 1}
    onto = _ontology_violations({"constraint_impact": {
        "enabled": True, "violated_constraints": vlist, "violated_counts_by_family": counts}})
    assert onto["total"] == 66
    assert onto["by_family"] == {"등급 최소인원 부족": 66}
    assert onto["examples"]["등급 최소인원 부족"] == "day1 D 필요1/배치0"


def test_quality_alert_old_payload_without_counts_uses_list():
    vlist = [{"node_id": "grade_min:2:0:D", "details": {}}]
    onto = _ontology_violations({"constraint_impact": {"enabled": True, "violated_constraints": vlist}})
    assert onto["total"] == 1
