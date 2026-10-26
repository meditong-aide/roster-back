"""EI-12 12c — C7: 서버가 만드는 출력 키는 정본 키만이다(별칭 0 · 설계 §2.6 C7 · D9).

'출력'은 카드·처방·control 조정·탐색 카탈로그·표(라벨·fix_location·mcs)가 내는 설정 키다. 요청을 되울리는 400 문구와 런타임 금지
표지가 가리키는 요청 키(`runtime_key_forbidden`)는 입력 이름 그대로 둔다. 별칭 목록은 레지스트리에서 계산한다 — 새 별칭이 생기면
이 시험이 자동으로 따라온다.
"""

from __future__ import annotations

import copy

import pytest
from services.constraint_impact.control import _FAMILY_ACTION_MATRIX, apply_adjustments_to_config
from services.cp_sat import undiagnosed_probe as probe
from services.cp_sat.fix_location import _FIX_BY_KEY
from services.engine_contract.settings import SETTINGS
from services.ontology_graph.mcs_trace import FAMILY_CONFIG_RELAX
from services.policy import card_filter as cf
from services.semantics.ontology import get_default_ontology
from services.treatment_applicator import apply_treatments

ALIASES = frozenset(a for spec in SETTINGS for a in spec.aliases)
_APPLIED = "_applied_constraint_adjustments"
_DEMAND = {"D": 3, "E": 3, "N": 2}
BASES = {
    "strict": cf.strict_base(),
    "typical": {"max_conseq_work": 4, "max_nig_per_month": 8, "allow_three_consecutive_nights": True, "off_days": 8, "daily_shift_requirements": _DEMAND},
    "two_n": {"max_conseq_work": 3, "max_nig_per_month": 6, "allow_three_consecutive_nights": False, "off_days": 8, "daily_shift_requirements": _DEMAND},
    "empty": {},
}


def _changed(before: dict, after: dict) -> set[str]:
    return {k for k in {*before, *after} if k != _APPLIED and before.get(k) != after.get(k)}


def test_alias_set_is_the_registry_alias_set():
    assert {"max_consecutive_work_days", "max_consecutive_nights", "max_night_shifts_per_month"} <= ALIASES


@pytest.mark.parametrize("name", sorted(BASES))
def test_relax_catalog_emits_canonical_keys_only(name):
    keys = {(item.get("search") or {}).get("key") for item in probe.RELAX_CATALOG}
    keys |= {k for item in probe.RELAX_CATALOG for k in item["apply"](copy.deepcopy(BASES[name]))}
    assert not keys & ALIASES


def test_ontology_treatment_config_keys_are_canonical():
    keys = {tid: t.config_key for tid, t in get_default_ontology().treatments.items() if t.config_key in ALIASES}
    assert keys == {}


@pytest.mark.parametrize("name", sorted(BASES))
def test_treatment_patches_write_canonical_keys_only(name):
    onto = get_default_ontology()
    for tid in sorted(onto.treatments):
        try:
            patched = apply_treatments([tid], copy.deepcopy(BASES[name])).patched_config
        except ValueError:
            continue  # team_min_remove — scope 없는 정상 경로 적용 예외
        assert not _changed(BASES[name], patched) & ALIASES, tid


@pytest.mark.parametrize("name", ["strict", "typical", "two_n"])
def test_control_adjustments_write_canonical_keys_only(name):
    scope = {"team": "T1", "shift": "D", "day": 1}
    base = {**BASES[name], "team_min_by_team": {"T1": {"D": 1}}, "grade_config": {"constraints": {"D": {"1": 1}}}}
    for family, actions in sorted(_FAMILY_ACTION_MATRIX.items()):
        for action in sorted(actions):
            for target in ((0, 2, 3, 6, 15, 16, 10**6) if action == "set_threshold" else (None,)):
                adj = {"family": family, "action": action, "target_value": target, "scope_filter": scope}
                after = apply_adjustments_to_config(copy.deepcopy(base), [adj])
                assert not _changed(base, after) & ALIASES, (family, action, target)


def test_static_tables_are_canonical():
    assert not set(probe.COL_LABEL_KO) & ALIASES
    assert not set(_FIX_BY_KEY) & ALIASES
    assert not {k for effect in FAMILY_CONFIG_RELAX.values() for k in effect} & ALIASES


def test_policy_marks_name_canonical_keys():
    """판정 dict 의 `key`·처방 금지 키·표시 키가 정본이다(`policy_forbidden[].keys` · `hard_lock_relax_marks[].key` 의 출처)."""
    modes = {k: v for k, v in cf.family_action_policy().items() if v.get("key")}
    assert modes[("ConsecutiveWorkLimit", "set_threshold")]["key"] == "max_conseq_work"
    assert modes[("MonthlyNightCap", "set_threshold")]["key"] == "max_nig_per_month"
    _forbidden, marked, block_keys = cf._treatment_verdicts()
    keys = {k for ks in block_keys.values() for k in ks} | {m["key"] for ms in marked.values() for m in ms}
    assert not keys & ALIASES


def test_no_treatment_apply_hint_emits_an_alias_key():
    """12s — 예외 0: `build_apply_hint().config_overrides` 의 어떤 키도 별칭이 아니다."""
    from services.semantics.ontology import build_apply_hint

    onto = get_default_ontology()
    leaked = {}
    for tid, t in onto.treatments.items():
        hint = build_apply_hint(t) or {}
        keys = set(hint.get("config_overrides") or {}) & ALIASES
        if keys:
            leaked[tid] = next(iter(keys))
    assert leaked == {}


def test_treatment_target_inference_refuses_alias_keys():
    """12r — 처방 값 추정은 정본 키만 읽는다: 별칭 키만 싣고 있으면 None(수동 분류), 정본 키면 ±1."""
    from services.treatment_applicator import _infer_target_value

    assert _infer_target_value(config_dict={"max_night_shifts_per_month": 5}, config_key="max_nig_per_month",
                               direction="increase") is None
    assert _infer_target_value(config_dict={"max_nig_per_month": 5}, config_key="max_nig_per_month",
                               direction="increase") == 6


def test_consecutive_night_fold_without_forbidden_mark(monkeypatch):
    """12r — 금지 표지가 없을 때의 접어 쓰기 값(3 이상 True · 2 이하 False)을 고정한다(레지스트리 `to_canonical` x>=3)."""
    from services.constraint_impact import control
    from services.policy import hard_lock

    monkeypatch.setattr(hard_lock, "runtime_forbidden_violation", lambda key: None)
    for value, expected in ((3, True), (9, True), (2, False), (0, False)):
        cfg: dict = {}
        control._apply_consec_nights(cfg, control.ConstraintAdjustment(
            family="ConsecutiveNightLimit", action="set_threshold", target_value=value))
        assert cfg == {"allow_three_consecutive_nights": expected}
