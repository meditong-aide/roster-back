"""EI-12 12a — 별칭 → 정본 정규화(`engine_contract.aliases.normalize_aliases` · 설계 D9).

입력(요청 `config_override`)만 바꾼다. 어느 이름이 별칭인지는 레지스트리 `aliases` 가 정하고, 값 영역이 다른 파생 입력
(`derived_inputs`)과 다른 표 컬럼 설정(정본 키에 '.')은 건드리지 않는다.
"""

from __future__ import annotations

import copy

import pytest
from services.engine_contract.aliases import normalize_aliases
from services.engine_contract.settings import SETTINGS

#: 이 목록이 달라지면 별칭 인벤토리가 바뀐 것이다 — 새 별칭은 EI-12 판정(정규화 대상인가)을 거쳐 여기에 올린다.
_EXPECTED_SIMPLE = {
    "max_consecutive_work_days": "max_conseq_work",
    "max_consecutive_work": "max_conseq_work",
    "max_night_shifts_per_month": "max_nig_per_month",
    "standard_personal_off_days": "off_days",
    "min_experience_per_shift": "min_exp_per_shift",
    "required_experienced_nurses": "req_exp_nurses",
    "enforce_two_offs_per_week": "two_offs_per_week",
    "shift_requirement_priority": "shift_priority",
}


def _registry_simple() -> dict[str, str]:
    return {alias: spec.key for spec in SETTINGS if "." not in spec.key
            for alias in spec.aliases if alias not in (spec.derived_inputs or {})}


def test_alias_inventory_is_pinned():
    assert _registry_simple() == _EXPECTED_SIMPLE
    derived = {a for spec in SETTINGS for a in (spec.derived_inputs or {})}
    assert derived == {"max_consecutive_nights"}
    assert {a for spec in SETTINGS if "." in spec.key for a in spec.aliases} == {"constraints", "constraints_max"}


@pytest.mark.parametrize("alias,canon", sorted(_EXPECTED_SIMPLE.items()))
def test_each_simple_alias_becomes_its_canonical_key(alias, canon):
    assert normalize_aliases({alias: 3, "n2n_min_gap": 0}) == {canon: 3, "n2n_min_gap": 0}


def test_canonical_key_wins_and_alias_is_dropped():
    assert normalize_aliases({"max_conseq_work": 4, "max_consecutive_work_days": 5}) == {"max_conseq_work": 4}


def test_first_registry_alias_wins_between_aliases():
    assert normalize_aliases({"max_consecutive_work": 3, "max_consecutive_work_days": 4}) == {"max_conseq_work": 4}


def test_none_alias_is_dropped_not_renamed():
    assert normalize_aliases({"max_consecutive_work_days": None, "x": 1}) == {"x": 1}
    assert normalize_aliases({"max_consecutive_work_days": None, "max_consecutive_work": 3}) == {"max_conseq_work": 3}
    assert normalize_aliases({"max_conseq_work": None, "max_consecutive_work_days": 4}) == {"max_conseq_work": None}


def test_derived_alias_and_other_table_columns_are_left_alone():
    given = {"max_consecutive_nights": 3, "constraints": {"D": {"1": 1}}, "constraints_max": {}}
    assert normalize_aliases(given) is given


def test_nothing_to_do_returns_the_same_object_and_input_is_never_mutated():
    for given in (None, {}, {"n2n_min_gap": 0}, "not-a-mapping"):
        assert normalize_aliases(given) is given
    src = {"max_night_shifts_per_month": 12, "x": 1}
    keep = copy.deepcopy(src)
    out = normalize_aliases(src)
    assert src == keep and out == {"max_nig_per_month": 12, "x": 1} and out is not src


def test_non_numeric_alias_values_are_dropped_not_renamed():
    """12r — 문자열·리스트·딕트 별칭은 정본으로 옮기지 않는다(옮기면 엔진이 TypeError 로 500 — 별칭일 땐 무시됐다)."""
    for bad in ("5.9", [1], {"a": 1}):
        assert normalize_aliases({"shift_requirement_priority": bad, "x": 1}) == {"x": 1}
    assert normalize_aliases({"shift_requirement_priority": 0.5}) == {"shift_priority": 0.5}
    assert normalize_aliases({"enforce_two_offs_per_week": False}) == {"two_offs_per_week": False}
