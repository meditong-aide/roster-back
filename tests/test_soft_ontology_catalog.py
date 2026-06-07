"""US-002 — soft 제약 온톨로지 등재 + penalty 매핑 검증.

objective weight 로만 존재하던 소프트 제약이 온톨로지 1급 노드로 로드되고,
각 weight_constant 가 hardcoded_weights.py 의 실제 상수에 매핑되는지 확인.
"""

import app.services.cp_sat.hardcoded_weights as hw
from services.semantics.ontology import ConstraintOntology, OntologySoftConstraint


def _ont():
    return ConstraintOntology()


def test_soft_constraints_loaded_min_five():
    ont = _ont()
    assert len(ont.soft_constraints) >= 5
    for sid, entry in ont.soft_constraints.items():
        assert isinstance(entry, OntologySoftConstraint)
        assert entry.severity == "soft"


def test_expected_soft_families_present():
    ont = _ont()
    expected = {"night_deviation", "isolated_work", "isolated_off", "nod_noe", "experience_short"}
    assert expected.issubset(set(ont.soft_constraints.keys()))


def test_weight_constant_resolves_to_real_constant():
    """null 이 아닌 weight_constant 는 hardcoded_weights.py 에 실제 존재해야 한다."""
    ont = _ont()
    checked = 0
    for sid, entry in ont.soft_constraints.items():
        if entry.weight_constant:
            assert hasattr(hw, entry.weight_constant), (
                f"{sid}: weight_constant {entry.weight_constant} 가 hardcoded_weights 에 없음"
            )
            assert isinstance(getattr(hw, entry.weight_constant), (int, float))
            checked += 1
    assert checked >= 4  # 최소 4개 family 가 실제 weight 상수에 매핑


def test_every_soft_has_control_knob():
    """각 soft family 는 런타임 control 노브(config_key)를 가진다 (set_threshold 대상)."""
    ont = _ont()
    for sid, entry in ont.soft_constraints.items():
        assert entry.config_key, f"{sid}: config_key (control 노브) 누락"


def test_lex_stage_is_safety_or_quality():
    ont = _ont()
    for sid, entry in ont.soft_constraints.items():
        assert entry.lex_stage in {"safety", "quality"}, f"{sid}: 잘못된 lex_stage {entry.lex_stage}"


def test_soft_alias_resolves():
    ont = _ont()
    # alias 대문자가 soft_id 로 resolve 되는지 (예: NIGHT_DEVIATION → night_deviation)
    rid = ont.resolve_alias("NIGHT_DEVIATION")
    assert rid == "night_deviation"
