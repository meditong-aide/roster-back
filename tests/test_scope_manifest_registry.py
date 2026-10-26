"""EI-14a — 범위 선언의 미지원 마커 이름이 실제 키(레지스트리)다.

옛 표는 어느 config 에도 없는 가짜 이름 13개를 들고 있어 그 개념이 켜져도 UNKNOWN 강등이 안 걸렸다.
"""
from __future__ import annotations

import pytest
from services.engine_contract.settings import RUNTIME_DATA_NOT_SETTINGS, SETTINGS_BY_KEY
from services.ontology_graph import scope_manifest as sm

from tests.engine_input.golden import read_golden
from tests.engine_input.golden_gate import GOLDEN, require_golden
from tests.engine_input.test_precheck_golden_verdicts import GOLDEN_NAMES

_REMOVED_FAKE_NAMES = (
    "min_nig_per_month", "nig_max_month", "nig_min_month", "min_off_days", "weekend_off", "weekend_off_ids",
    "grade_requirements", "grade_min", "team_requirements", "team_min", "preceptor_pairs", "mutual_exclusion", "pairing",
)
_RUNTIME_NAMES = {name for name, _ in RUNTIME_DATA_NOT_SETTINGS}


def test_every_concept_key_is_a_registry_name():
    for key in sm._UNSUPPORTED_CONCEPT_KEYS:
        assert key in SETTINGS_BY_KEY or key in _RUNTIME_NAMES, key


def test_derived_tuple_covers_canonical_keys_and_aliases():
    for key in sm._UNSUPPORTED_CONCEPT_KEYS:
        assert key in sm._UNSUPPORTED_CONFIG
        spec = SETTINGS_BY_KEY.get(key)
        for alias in (spec.aliases if spec is not None else ()):
            assert alias in sm._UNSUPPORTED_CONFIG, (key, alias)
    assert len(set(sm._UNSUPPORTED_CONFIG)) == len(sm._UNSUPPORTED_CONFIG)


def test_fake_names_are_gone():
    assert not set(_REMOVED_FAKE_NAMES) & set(sm._UNSUPPORTED_CONFIG)


@pytest.mark.parametrize("key", sm._UNSUPPORTED_CONFIG)
@pytest.mark.parametrize("value", [1, {"x": 1}, True])
def test_each_marker_flags_when_active(key, value):
    assert f"config.{key}" in sm.unmodeled_active([], {key: value})


def test_use_mid_is_unsupported_marker():
    assert "config.use_mid" in sm.unmodeled_active([], {"use_mid": True})
    assert sm.unmodeled_active([], {"use_mid": False}) == []


def test_preceptee_on_alone_stays_in_scope():
    """test_scope_manifest.py:87 — preceptee_on 단독은 지원 범위(표에 넣지 않는다)."""
    assert sm.unmodeled_active([], {"preceptee_on": True}) == []


def test_grade_constraints_marker_is_inactive_in_config_dict():
    """roster_grade_config.constraints_json 은 레지스트리에 있으나 config dict 에 안 실린다(등급은 grade_config 인자로 따로 간다).
    2단계 14b-2b 에서 뷰가 싣는 안을 정할 때 이 시험을 바꾼다."""
    key = "roster_grade_config.constraints_json"
    assert key in SETTINGS_BY_KEY
    assert f"config.{key}" not in sm.unmodeled_active([], {})


@pytest.mark.parametrize("name", GOLDEN_NAMES)
def test_golden_l0_keeps_unmodeled_active_non_empty(name):
    require_golden(GOLDEN)
    call = read_golden(name)["layers"]["L0"][0]
    um = sm.unmodeled_active(call["nurses_dict"], call["config_dict"])
    if name == "S3":  # S3 는 off_days·max_nig_per_month 가 None 인 설정 — 전(옛 표)에도 비어 있었다(판정 불변)
        assert "config.off_days" not in um
        return
    assert um, name
    assert "config.off_days" in um, (name, um)
