"""C5 — 카드·처방 하드락 필터 계약(EI-11 · `.omc/aide_docs/v2/engine_input/ei11/EI11_SPEC_v2.md` §2.1·§4.1).

11a-0 단계는 판정 부품(`services.policy.card_filter`)과 월 한도 판정(`hard_lock.release_violations`)의 순수 단위
계약이다. 배선 0 · 골든 0 — 표본은 레지스트리에서 짓는다(골든 없이 늘 돈다). 뒤 단계(11a-1·11a-2·11b·11c+11d)가
이 파일에 단언을 더한다(§4.1 ①~⑨). 기대값 표 E1·E2·E5 는 `ei11/scratch/probe_ei11.out` 실측이다.
11a-1 은 생산자 쪽을 더한다 — ① 탐색 카탈로그 전 항목 BLOCK 0·#1 클램프 cur 규칙 · ④ 원인 카드 targets · ⑤ 연속근무 레버 ·
⑥ 개인 야간 상한 카드 · ⑦(1차) 남은 카드 ≡ 가드 · ⑧(1차) E4 원인 payload 의 카드 칸 금지 처방·BLOCK delta 0.
11a-2 는 ② 처방 set cover 제외·생성 진입 관문(`assert_treatments_allowed`)을 더하고, ⑦ 에 관문을 넣고, ⑧ 범위를 payload 전체
(`policy_forbidden`·`resolution_narrative.problem_list` 밖)로 넓힌다.
11b 는 E2 의 ConsecutiveNightLimit 을 blocked 로 바꾸고(D13 ③ — 별칭은 값과 무관하게 금지), ⑦ 을 완성한다 — 가드 400 ⇔ 카드
`apply` 통로 BLOCK 을 §2.4-6 방향 표의 모든 키로 넓히고, 탐색 카탈로그 delta·극값이 가드를 통과하는지 본다.
11c 는 ⑧ 범위를 payload 전체로 넓히고(`policy_forbidden` 과 `policy_forbidden: true` 표지 붙은 `mapped_treatments` 항목만 뺀다),
정상 경로 기록에 표지 판정(`family_modes`·`policy_forbidden_for_causes`·`capped_suffix`·`mark_treatment_views`)을 더한다.
11d 는 ③ control (family, action) 전부를 더한다 — 조정 기록 표지 ≡ `family_action_policy`(BLOCK 은 적용되되 '적용 불가'·rule_nos,
기본값이면 기록 칸 없음 · 표지만 달고 적용된 설정은 그대로).
"""

from __future__ import annotations

import ast
import copy
import json
import logging
import math
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest
from fastapi import HTTPException
from services import cause_treatment_hitter as hitter
from services.constraint_impact import control
from services.cp_sat.fix_location import _FIX_BY_KEY
from services.cp_sat.undiagnosed_probe import (
    COL_LABEL_KO,
    RELAX_CATALOG,
    TRADEOFF_KO,
    _consec_work_cap,
    _raise_consec_work_delta,
    personal_night_cap_options_from_issues,
    probe_relaxations,
)
from services.engine_contract import settings as S
from services.ontology_graph.mcs_trace import FAMILY_PRESENTATION, cause_to_resolution_options
from services.policy import card_filter as cf
from services.policy import hard_lock as hl
from services.precheck.payload import build_blocking_payload, build_unrecoverable_payload
from services.roster_create_service import assert_night_caps_within_hard_lock
from services.semantics.ontology import friendly_config_key_label, get_default_ontology
from services.treatment_applicator import apply_treatments

REPO_ROOT = Path(__file__).resolve().parents[2]
_LOGGER = "services.policy.card_filter"
_FILTER_ERROR_MSG = "하드락 판정 중 오류가 나 안전하게 뺐습니다."
_PRESET_NOTE = " 이 조정은 설정 프리셋에 저장됩니다(모달에서 시작한 생성)."


# ── 표본(레지스트리에서 짓는다) ────────────────────────────────────────────────


def _keys(canonical: str) -> tuple[str, ...]:
    spec = S.lookup(canonical)
    return (spec.key, *spec.aliases)


def _parse_default(raw: str):
    return raw == "True" if raw in ("True", "False") else int(raw)


def _b2_below_cap() -> dict:
    """B2 — 2N 병동·상한 아래(연속근무 4 · 월 나이트 10 · 3N 끔 · bool 켜짐)."""
    base = cf.strict_base()
    base.update(dict.fromkeys(_keys("max_conseq_work"), 4))
    base.update(dict.fromkeys(_keys("max_nig_per_month"), 10))
    base["allow_three_consecutive_nights"] = False
    return base


def _b3_registry_defaults() -> dict:
    """B3 — 레지스트리 `current_default` 그대로(일부 꺼짐 — 같은 값·no-op 경로)."""
    return {s.key: _parse_default(s.current_default) for s in S.SETTINGS if s.hard_lock is not None}


SAMPLES = {
    "B1": cf.strict_base,
    "B2": _b2_below_cap,
    "B3": _b3_registry_defaults,
    "B4": dict,
    "B1-0": lambda: {**cf.strict_base(), "max_conseq_work": 0},
}


def test_strict_base_shape_from_registry():
    base = cf.strict_base()
    for spec in S.SETTINGS:
        lock = spec.hard_lock
        if not isinstance(lock, S.HardLock):
            continue
        if lock.kind == "int_cap":
            assert {k: base[k] for k in _keys(spec.key)} == dict.fromkeys(_keys(spec.key), lock.cap)
        elif lock.kind in ("bool_on", "derived_cap"):
            assert base[spec.key] is True, spec.key
        elif lock.kind == "boundary_switch":
            assert base[spec.key] == _parse_default(spec.current_default), spec.key
    assert "max_consecutive_nights" not in base, "derived_cap 은 정본 키만 켠다"
    assert base["off_days"] == _parse_default(S.lookup("off_days").current_default)


def test_strict_base_is_new_dict_every_call():
    first = cf.strict_base()
    first["max_conseq_work"] = 99
    assert cf.strict_base()["max_conseq_work"] == S.lookup("max_conseq_work").hard_lock.cap


def test_samples_b1_to_b4_and_zero():
    b2, b3, zero = _b2_below_cap(), _b3_registry_defaults(), SAMPLES["B1-0"]()
    assert b2["max_conseq_work"] == 4 and b2["max_night_shifts_per_month"] == 10 and b2["allow_three_consecutive_nights"] is False
    bool_keys = [s.key for s in S.SETTINGS if isinstance(s.hard_lock, S.HardLock) and s.hard_lock.kind == "bool_on"]
    assert [k for k in bool_keys if b3[k] is False], "B3 는 일부 하드락 bool 이 꺼진 표본이어야 한다"
    assert all(b2[k] is True for k in bool_keys)
    assert SAMPLES["B4"]() == {} and zero["max_conseq_work"] == 0


# ── delta · 카탈로그 항목 판정 ─────────────────────────────────────────────────


def test_delta_violations_is_override_violations_and_keeps_input():
    delta = {"two_offs_after_two_nig": False, "off_days": 5, "max_nig_per_month": 16}
    snapshot = dict(delta)
    assert cf.delta_violations(delta) == hl.override_violations(snapshot)
    assert delta == snapshot
    assert cf.delta_violations(None) == []


@pytest.mark.parametrize("sample", SAMPLES)
def test_catalog_item_violations_reads_apply_and_search_extremes(sample):
    base = SAMPLES[sample]()
    over = {"id": "fake_over", "apply": lambda c: {"max_conseq_work": int(c.get("max_conseq_work") or 0) + 99},
            "search": {"key": "max_conseq_work", "dir": "up", "hi": 99}}
    lo_only = {"id": "fake_lo", "apply": lambda c: {}, "search": {"key": "off_days", "dir": "down", "lo": 0}}
    assert [(v.kind, v.reason_code) for v in cf.catalog_item_violations(over, base)] == [(hl.BLOCK, "exceeds_cap")] * 2
    assert [(v.kind, v.key) for v in cf.catalog_item_violations(lo_only, base)] == [(hl.RELAX_MARK, "off_days")]


@pytest.mark.parametrize("item_id", ["raise_max_night_cap", "soften_team_min"])
@pytest.mark.parametrize("sample", SAMPLES)
def test_catalog_items_within_cap_have_no_block_on_every_sample(item_id, sample):
    from services.cp_sat.undiagnosed_probe import RELAX_CATALOG

    item = next(i for i in RELAX_CATALOG if i["id"] == item_id)
    assert [v for v in cf.catalog_item_violations(item, SAMPLES[sample]()) if v.kind == hl.BLOCK] == []


# ── 처방 판정(E1) ─────────────────────────────────────────────────────────────

_E1_FORBIDDEN = {
    "treatment:disable:transition_ban": (2,),
    "treatment:disable:night_recovery": (4, 5),
    "treatment:threshold:consecutive_work": (1,),
}


def test_forbidden_treatments_equal_e1_table():
    assert cf.forbidden_treatments() == _E1_FORBIDDEN


def test_marked_treatments_include_ban_night_before_fixed_off():
    marked = cf.marked_treatments()
    tid = "treatment:disable:ban_night_before_fixed_off"
    assert [m["key"] for m in marked[tid]] == ["ban_night_before_fixed_off"]
    assert not set(marked) & set(_E1_FORBIDDEN)


def test_apply_exception_team_min_remove_is_not_forbidden():
    tid = "treatment:scope:team_min_remove"
    with pytest.raises(ValueError, match="narrow_scope requires scope_filter.team"):
        apply_treatments([tid], cf.strict_base())  # 정상 경로에서도 매번 나는 적용 예외
    assert tid not in cf.forbidden_treatments() and tid not in cf.marked_treatments()


def test_apply_exception_on_hard_lock_family_stays_forbidden(monkeypatch, caplog):
    def _boom(cfg, adj):
        raise RuntimeError("주입 — NightRecovery 적용 예외")

    monkeypatch.setitem(control._DISPATCH, "NightRecovery", _boom)
    forbidden = cf.forbidden_treatments()
    assert "treatment:disable:night_recovery" in forbidden
    assert forbidden["treatment:disable:transition_ban"] == (2,)
    assert any(r.levelno >= logging.WARNING and r.name == _LOGGER for r in caplog.records)


def test_forbidden_treatments_use_the_given_ontology():
    from services.semantics.ontology import ConstraintOntology

    onto = ConstraintOntology()
    del onto.treatments["treatment:disable:transition_ban"]
    assert set(cf.forbidden_treatments(ontology=onto)) == set(_E1_FORBIDDEN) - {"treatment:disable:transition_ban"}
    assert "treatment:disable:transition_ban" in cf.forbidden_treatments()


def test_hard_lock_family_treatment_ids_cover_forbidden():
    onto = get_default_ontology()
    ids = cf.hard_lock_family_treatment_ids(onto)
    assert set(_E1_FORBIDDEN) <= ids
    assert all(onto.get_treatment(t).target_family in cf.HARD_LOCK_FAMILIES_FALLBACK for t in ids)
    assert ids == cf.hard_lock_family_treatment_ids()


# ── control 매트릭스 판정(E2) ─────────────────────────────────────────────────


def test_family_action_policy_matches_e2_table():
    policy = cf.family_action_policy()
    assert set(policy) == {(f, a) for f, acts in control._FAMILY_ACTION_MATRIX.items() for a in acts}
    expected = {
        ("BoundaryTransitionBan", "disable_module"): {"mode": "blocked", "rule_nos": [2]},
        ("NightRecovery", "disable_module"): {"mode": "blocked", "rule_nos": [4, 5]},
        ("ConsecutiveWorkLimit", "set_threshold"):
            {"mode": "capped", "key": "max_conseq_work", "cap": 5, "rule_nos": [1]},
        # 11b(D13 ③) — 별칭 max_consecutive_nights 는 값과 무관하게 금지라 0 도 10⁶ 도 BLOCK → blocked(옛 capped·cap 3).
        ("ConsecutiveNightLimit", "set_threshold"): {"mode": "blocked", "rule_nos": [3]},
        ("MonthlyNightCap", "set_threshold"):
            {"mode": "capped", "key": "max_nig_per_month", "cap": 15, "rule_nos": [6]},
    }
    for pair, want in expected.items():
        assert policy[pair] == want, pair
    marked = policy[("BanNightBeforeFixedOff", "disable_module")]
    assert marked["mode"] == "marked" and [m["key"] for m in marked["marks"]] == ["ban_night_before_fixed_off"]
    rest = set(policy) - set(expected) - {("BanNightBeforeFixedOff", "disable_module")}
    assert {pair: policy[pair] for pair in rest} == dict.fromkeys(rest, {"mode": "ok", "rule_nos": []})


def test_family_action_policy_apply_exception_is_blocked_with_error(monkeypatch, caplog):
    monkeypatch.setitem(control._DISPATCH, "WeekendOffOnly", lambda cfg, adj: 1 / 0)
    entry = cf.family_action_policy()[("WeekendOffOnly", "disable_module")]
    assert entry == {"mode": "blocked", "rule_nos": [], "error": True}
    assert any(r.levelno >= logging.WARNING and r.name == _LOGGER for r in caplog.records)


def test_derived_hard_lock_families_within_fallback():
    """어긋남 감시 — 파생된 하드락 family(blocked·capped 정책 ∪ 금지 처방의 target) ⊆ 고정 대체 집합."""
    onto = get_default_ontology()
    derived = {f for (f, _a), e in cf.family_action_policy().items() if e["mode"] in ("blocked", "capped")}
    derived |= {onto.get_treatment(t).target_family for t in cf.forbidden_treatments()}
    assert derived <= cf.HARD_LOCK_FAMILIES_FALLBACK


# ── mcs 원인 가족 효과(E5) ────────────────────────────────────────────────────

#: family → (blocks 의 rule_nos 목록, marks 의 key 목록) — 버킷은 `FAMILY_PRESENTATION` 그대로.
_E5 = {
    "2n2off": ([[5]], []),
    "night_recovery": ([[4], [5]], []),
    "not_one_night": ([[7]], []),
    "consecutive": ([[1]], []),
    "transition": ([[2], [2], [2]], []),
    "cross_month": ([[1, 2, 3, 4, 5, 7]], []),
    "preceptee": ([], ["preceptee_follows_preceptor"]),
    "off_budget": ([], ["off_days"]),
}


@pytest.mark.parametrize("family", FAMILY_PRESENTATION)
def test_cause_family_effect_matches_e5_table(family):
    blocks, marks = cf.cause_family_effect(family, FAMILY_PRESENTATION[family]["bucket"])
    want_blocks, want_marks = _E5.get(family, ([], []))  # night_cap(action)·advisory·효과 없음 → 빈 결과
    assert [b["rule_nos"] for b in blocks] == want_blocks
    assert [m["key"] for m in marks] == want_marks


def test_cause_family_effect_consecutive_message_and_bucket_rules():
    (block,), _ = cf.cause_family_effect("consecutive", "tradeoff")
    assert block["message_ko"] == "연속근무 상한을 5일 넘게 올리는 완화는 정책상 금지(#1) — 상한 안 조정은 다른 카드"
    assert cf.cause_family_effect("consecutive", "action") == ([], [])  # action 의 BLOCK 은 버린다
    assert cf.cause_family_effect("night_recovery", "advisory") == ([], [])
    assert cf.cause_family_effect("night_recovery", None) == ([], [])


# ── 카드 한 장 판정(통로 4개 + 판정 안 하는 통로 3개) ─────────────────────────


def test_option_violations_apply_channel():
    blocks, marks = cf.option_violations({"option_id": "a", "apply": {"two_offs_after_two_nig": False}})
    assert [(b["rule_nos"], b["key"], b["channel"]) for b in blocks] == [([5], "two_offs_after_two_nig", "apply")]
    blocks, marks = cf.option_violations({"option_id": "b", "apply": {"off_days": 6}})
    assert blocks == [] and [m["key"] for m in marks] == ["off_days"]
    assert marks[0]["message_ko"].endswith(_PRESET_NOTE), "apply 통로 표지에는 프리셋 저장 문구가 붙는다(Q18)"


def test_option_violations_treatment_channel():
    blocks, _ = cf.option_violations({"treatment_ids": ["treatment:soft:grade_max", "treatment:disable:night_recovery"]})
    assert [(b["rule_nos"], b["key"], b["channel"]) for b in blocks] == [
        ([4, 5], "treatment:disable:night_recovery", "treatment_ids")]
    blocks, marks = cf.option_violations({"treatment_ids": ["treatment:disable:ban_night_before_fixed_off"]})
    assert blocks == [] and [(m["key"], m["channel"]) for m in marks] == [("ban_night_before_fixed_off", "treatment_ids")]
    assert not marks[0]["message_ko"].endswith(_PRESET_NOTE)
    assert cf.option_violations({"treatment_ids": ["treatment:soft:grade_max"]}) == ([], [])


def test_option_violations_release_channel():
    rel = [{"nurse_id": "N1", "field": "n_max", "value": 16}, {"nurse_id": "N2", "field": "n_max", "value": 15}]
    blocks, marks = cf.option_violations({"monthly_limit_release": rel})
    assert [(b["rule_nos"], b["key"], b["channel"]) for b in blocks] == [([6], "N1:n_max", "monthly_limit_release")]
    assert marks == []


def test_option_violations_cause_family_channel():
    tradeoff = {"option_id": "cause:night_recovery", "source": "cause", "family": "night_recovery", "bucket": "tradeoff"}
    assert [b["rule_nos"] for b in cf.option_violations(tradeoff)[0]] == [[4], [5]]
    action = {"option_id": "cause:off_budget", "source": "cause", "family": "off_budget", "bucket": "action"}
    blocks, marks = cf.option_violations(action)
    assert blocks == [] and [m["key"] for m in marks] == ["off_days"]
    assert not marks[0]["message_ko"].endswith(_PRESET_NOTE), "Q18 문구는 apply 통로만"
    assert cf.option_violations({**tradeoff, "source": "probe"}) == ([], []), "family 통로는 source=='cause' 만"


@pytest.mark.parametrize("option", [
    {"option_id": "w", "weekend_off_release": ["N1"]},
    {"option_id": "a", "allowed_shift_add": [{"nurse_id": "N1", "add": ["D", "E"]}]},
    {"option_id": "b", "banned_wanted_release": [{"nurse_id": "N1", "days": [3]}]},
    {"id": "overlay"},
    "not-a-dict",
])
def test_option_violations_unjudged_channels_are_empty(option):
    assert cf.option_violations(option) == ([], [])


def test_option_violations_never_raises_and_fails_closed():
    for bad in ({"apply": "x"}, {"treatment_ids": "treatment:disable:night_recovery"}, {"monthly_limit_release": 5}):
        blocks, marks = cf.option_violations(bad)
        assert [b.get("filter_error") for b in blocks] == [True] and marks == [], bad


# ── 가드 ① ≡ release_violations ──────────────────────────────────────────────

_ADVERSARIAL_RELEASES = [
    {"nurse_id": "N1", "field": "n_min", "value": 16},
    {"nurse_id": "N1", "field": "n_max", "value": "abc"},
    {"nurse_id": "N1", "field": "n_exact", "value": math.inf},
    {"nurse_id": "N1", "field": "n_exact", "value": math.nan},
    {"nurse_id": "N1", "field": "n_max", "value": " 16 "},
    {"nurse_id": "N1", "field": "n_max", "value": True},
    {"nurse_id": "N1", "field": "n_max", "value": 16.9},
    {"nurse_id": "N1", "field": "n_max", "value": 15},
    {"nurse_id": "N1", "field": "n_max", "value": None},
    {"nurse_id": "N1", "field": "d_max", "value": 99},
    "not-a-dict",
    ["n_max", 99],
    None,
]


def _guard_rejects(config_delta, release) -> bool:
    try:
        assert_night_caps_within_hard_lock(config_delta, release)
    except HTTPException as exc:
        assert exc.status_code == 400
        return True
    return False


@pytest.mark.parametrize("rel", _ADVERSARIAL_RELEASES, ids=repr)
def test_guard_one_equals_release_violations(rel):
    assert _guard_rejects(None, [rel]) == bool(hl.release_violations([rel]))


@pytest.mark.parametrize("value", [" 16 ", 16, 15, "abc", True, None, math.inf])
@pytest.mark.parametrize("key", _keys("max_nig_per_month"))
def test_guard_400_iff_option_violations_block_for_monthly_night_cap(key, value):
    blocks, _ = cf.option_violations({"apply": {key: value}})
    assert _guard_rejects({key: value}, None) == bool(blocks)


def test_release_violations_kinds_keys_and_order():
    rel = [{"nurse_id": "A", "field": "n_exact", "value": 20}, "skip", {"nurse_id": "B", "field": "n_min", "value": "x"},
           {"nurse_id": "C", "field": "n_max", "value": 15}, {"field": "n_max", "value": 16}]
    got = hl.release_violations(rel)
    assert [(v.key, v.reason_code, v.kind, v.rule_nos) for v in got] == [
        ("A:n_exact", "exceeds_cap", hl.BLOCK, (6,)), ("B:n_min", "non_numeric", hl.BLOCK, (6,)),
        ("None:n_max", "exceeds_cap", hl.BLOCK, (6,))]
    assert hl.release_violations(None) == [] and hl.release_violations([]) == []


def test_public_rule_label_and_cap_are_the_private_ones():
    for n in range(1, 9):
        assert hl.rule_label(n) == hl._rule_label(n) and hl.rule_cap(n) == hl._rule_cap(n)
    assert hl.rule_label(1, 4) == hl._rule_label(1, 4)
    assert (hl.rule_cap(1), hl.rule_cap(3), hl.rule_cap(6)) == (5, 3, 15)


# ── filter_options · finalize_infeasibility ──────────────────────────────────


def _card(option_id: str, **fields) -> dict:
    return {"option_id": option_id, **fields}


def test_filter_options_keeps_no_effect_synthetic_card_unchanged():
    overlay = {"id": "overlay"}
    kept, blocked = cf.filter_options([overlay, "raw"])
    assert kept == [{"id": "overlay"}, "raw"] and kept[0] is overlay and blocked == []


def test_filter_options_returns_policy_forbidden_items():
    kept, blocked = cf.filter_options([
        _card("probe:disable_2n2off", apply={"two_offs_after_two_nig": False}),
        {"id": "x", "source": "cause", "family": "consecutive", "bucket": "tradeoff"},
        _card("keep", fix={"mode": "manual"}),
    ])
    assert [o["option_id"] for o in kept] == ["keep"]
    assert blocked == [
        {"kind": "card", "id": "probe:disable_2n2off", "rule_nos": [5], "keys": ["two_offs_after_two_nig"],
         "message_ko": f"하드락 규칙({hl.rule_label(5)})을 완화하는 해결책이라 내보내지 않았습니다."},
        {"kind": "cause_card", "id": "x", "rule_nos": [1], "keys": ["max_conseq_work"],
         "message_ko": "연속근무 상한을 5일 넘게 올리는 완화는 정책상 금지(#1) — 상한 안 조정은 다른 카드"},
    ]


def test_filter_options_uses_given_treatment_maps():
    card = _card("bundle:g", treatment_ids=["treatment:soft:grade_max"])
    kept, blocked = cf.filter_options([card], forbidden={"treatment:soft:grade_max": (2,)}, marked={})
    assert kept == [] and [(b["id"], b["rule_nos"]) for b in blocked] == [("bundle:g", [2])]


def test_finalize_relax_marks_overwritten_not_appended():
    inf = {"resolution_options": [_card("probe:lower_off_days", apply={"off_days": 5})]}
    cf.finalize_infeasibility(inf)
    cf.finalize_infeasibility(inf)
    marks = inf["resolution_options"][0]["hard_lock_relax_marks"]
    assert len(marks) == 1 and marks[0]["key"] == "off_days" and marks[0]["message_ko"].endswith(_PRESET_NOTE)
    assert (inf["policy_forbidden"], inf["blocked_by_hard_lock"], inf["policy_filter_error"]) == ([], 0, 0)


def test_finalize_filters_resolution_options_in_place():
    opts = [_card("probe:disable_3n2off", apply={"two_offs_after_three_nig": False}), {"id": "overlay"}]
    inf = {"resolution_options": opts}
    assert cf.finalize_infeasibility(inf) is inf
    assert inf["resolution_options"] is opts and opts == [{"id": "overlay"}]
    assert inf["blocked_by_hard_lock"] == 1 and inf["policy_forbidden"][0]["id"] == "probe:disable_3n2off"


def test_finalize_sole_option_dict_shares_marks_and_blocks():
    shared = _card("cause:fix_all_personal", source="cause", apply={"off_days": 6})
    inf = {"resolution_options": [shared, _card("other")], "_sole_option": shared}
    cf.finalize_infeasibility(inf)
    assert inf["_sole_option"] is inf["resolution_options"][0] and "hard_lock_relax_marks" in shared
    blocked = _card("cause:fix_all_personal", source="cause",
                    monthly_limit_release=[{"nurse_id": "N1", "field": "n_max", "value": 16}])
    inf = {"resolution_options": [blocked, _card("other")], "_sole_option": blocked}
    cf.finalize_infeasibility(inf)
    assert "_sole_option" not in inf and inf["resolution_options"] == []
    assert [(p["kind"], p["id"]) for p in inf["policy_forbidden"]] == [("cause_card", "cause:fix_all_personal")]


def test_finalize_sole_option_list_partial_and_all_blocked():
    ok, bad = _card("cause:banned_release", source="cause"), _card("cause:x", apply={"ban_single_night": False})
    inf = {"resolution_options": [ok, bad], "_sole_option": [ok, bad]}
    cf.finalize_infeasibility(inf)
    assert inf["_sole_option"] == [ok] and inf["resolution_options"] == [ok]
    inf = {"resolution_options": [ok], "_sole_option": [bad]}
    cf.finalize_infeasibility(inf)
    assert "_sole_option" not in inf and inf["resolution_options"] == []


def test_finalize_policy_forbidden_is_kind_id_union_after_previous():
    prev = [{"kind": "card", "id": "lever:max_conseq_work", "rule_nos": [1], "keys": ["max_conseq_work"],
             "message_ko": "앞 기록"}]
    inf = {"policy_forbidden": copy.deepcopy(prev), "resolution_options": [
        _card("lever:max_conseq_work", apply={"max_conseq_work": 6}),
        _card("probe:disable_not_one_night", apply={"ban_single_night": False})]}
    cf.finalize_infeasibility(inf)
    assert [(p["kind"], p["id"]) for p in inf["policy_forbidden"]] == [
        ("card", "lever:max_conseq_work"), ("card", "probe:disable_not_one_night")]
    assert inf["policy_forbidden"][0]["message_ko"] == "앞 기록" and inf["blocked_by_hard_lock"] == 2


def test_finalize_policy_filter_error_never_decreases():
    inf = {"resolution_options": [_card("manual", fix={"mode": "manual"})], "policy_filter_error": 2}
    cf.finalize_infeasibility(inf)
    assert inf["policy_filter_error"] == 2, "앞 호출의 판정 오류 수는 줄지 않는다(max)"


def _rich_payload() -> dict:
    shared = _card("cause:fix_all_personal", source="cause", apply={"off_days": 6})
    return {"summary_message_ko": "s", "resolution_options": [
        shared, _card("probe:disable_2n2off", apply={"two_offs_after_two_nig": False}),
        _card("bundle:t", treatment_ids=["treatment:disable:transition_ban", "treatment:data:add_nurse"]),
        _card("cause:off_budget", source="cause", family="off_budget", bucket="action"),
        {"id": "overlay"}], "_sole_option": shared,
        "policy_forbidden": [{"kind": "card", "id": "lever:max_conseq_work", "rule_nos": [1], "keys": [],
                              "message_ko": "앞"}]}


def test_finalize_twice_equals_once():
    once = cf.finalize_infeasibility(_rich_payload())
    twice = cf.finalize_infeasibility(cf.finalize_infeasibility(_rich_payload()))
    assert copy.deepcopy(twice) == copy.deepcopy(once)
    assert once["blocked_by_hard_lock"] == 3 and once["policy_filter_error"] == 0


def test_finalize_json_roundtrip_dict_gives_same_result():
    direct = cf.finalize_infeasibility(_rich_payload())
    roundtrip = cf.finalize_infeasibility(json.loads(json.dumps(_rich_payload(), ensure_ascii=False)))
    assert json.dumps(roundtrip, sort_keys=True, ensure_ascii=False) == json.dumps(direct, sort_keys=True, ensure_ascii=False)


@pytest.mark.parametrize("value", [None, "x", {"a": 1}, ({"option_id": "t", "apply": {"ban_single_night": False}},)])
def test_finalize_leaves_non_list_resolution_options_untouched(value):
    inf = {"resolution_options": value}
    cf.finalize_infeasibility(inf)
    assert inf["resolution_options"] == value
    assert (inf["policy_forbidden"], inf["blocked_by_hard_lock"], inf["policy_filter_error"]) == ([], 0, 0)


@pytest.mark.parametrize("value", [None, [], "x", 3])
def test_finalize_returns_non_dict_as_is(value):
    assert cf.finalize_infeasibility(value) == value


def test_finalize_builds_treatment_maps_once_and_only_when_needed(monkeypatch):
    calls = []
    real = cf._treatment_maps
    monkeypatch.setattr(cf, "_treatment_maps", lambda ontology=None: calls.append(1) or real(ontology))
    cf.finalize_infeasibility({"resolution_options": [_card("p", apply={"off_days": 6}), {"id": "overlay"}]})
    assert calls == []
    cf.finalize_infeasibility({"resolution_options": [_card("t1", treatment_ids=["treatment:soft:grade_max"]),
                                                      _card("t2", treatment_ids=["treatment:disable:night_recovery"])],
                               "_sole_option": _card("t3", treatment_ids=["treatment:soft:team_min"])})
    assert calls == [1]


# ── 고장 주입(⑨) ──────────────────────────────────────────────────────────────


def test_fault_one_card_judgement_fails_only_that_card(monkeypatch):
    real = cf.override_violations

    def _flaky(delta):
        if delta.get("off_days") == 7:
            raise RuntimeError("주입 — 이 카드만")
        return real(delta)

    monkeypatch.setattr(cf, "override_violations", _flaky)
    inf = {"resolution_options": [_card("boom", apply={"off_days": 7}), _card("ok", apply={"off_days": 6}),
                                  _card("manual", fix={"mode": "manual"})]}
    cf.finalize_infeasibility(inf)
    assert [o["option_id"] for o in inf["resolution_options"]] == ["ok", "manual"]
    assert inf["policy_forbidden"] == [{"kind": "card", "id": "boom", "rule_nos": [], "keys": [],
                                        "message_ko": _FILTER_ERROR_MSG, "filter_error": True}]
    assert (inf["policy_filter_error"], inf["blocked_by_hard_lock"]) == (1, 1)


def _mixed_cards() -> list:
    return [
        _card("bundle:t", treatment_ids=["treatment:soft:grade_max"]),
        _card("lever", apply={"max_conseq_work": 5}),
        _card("rel", monthly_limit_release=[{"nurse_id": "N1", "field": "n_max", "value": 10}]),
        _card("wk", weekend_off_release=["N1"]),
        _card("ban", banned_wanted_release=[{"nurse_id": "N1", "days": [2]}]),
        _card("add", allowed_shift_add=[{"nurse_id": "N1", "add": ["D"]}]),
        _card("cause:night_recovery", source="cause", family="night_recovery", bucket="tradeoff"),
        _card("cause:coverage", source="cause", family="coverage", bucket="advisory", fix={"mode": "manual"}),
        _card("manual", kind="manual_fix", fix={"mode": "manual"}),
    ]


def test_fault_global_judgement_failure_keeps_only_manual_cards(monkeypatch):
    def _boom():
        raise RuntimeError("주입 — 레지스트리 표본 실패")

    monkeypatch.setattr(cf, "strict_base", _boom)
    inf = {"resolution_options": _mixed_cards()}
    cf.finalize_infeasibility(inf)
    assert [o["option_id"] for o in inf["resolution_options"]] == ["cause:coverage", "manual"]
    assert all(p["filter_error"] is True for p in inf["policy_forbidden"])
    assert inf["policy_filter_error"] >= 1 and inf["blocked_by_hard_lock"] == 7
    kept, blocked = cf.filter_options(_mixed_cards())
    assert [o["option_id"] for o in kept] == ["cause:coverage", "manual"] and len(blocked) == 7


def test_fault_last_resort_when_filtering_path_itself_fails(monkeypatch):
    def _boom(*args, **kwargs):
        raise RuntimeError("주입 — 거르기 경로 실패")

    monkeypatch.setattr(cf, "filter_options", _boom)
    inf = {"resolution_options": _mixed_cards(), "_sole_option": _card("manual"), "policy_filter_error": 0}
    cf.finalize_infeasibility(inf)
    assert inf["resolution_options"] == [] and "_sole_option" not in inf
    assert inf["policy_filter_error"] >= 1 and inf["blocked_by_hard_lock"] == len(inf["policy_forbidden"])


@pytest.mark.parametrize("inf", [
    {"resolution_options": [None, 1, "s", ["a"], {"apply": [("off_days", 1)]}]},
    {"resolution_options": [{"option_id": ["unhashable"], "apply": {"ban_single_night": False}}]},
    {"resolution_options": [{"treatment_ids": [["x"]]}], "_sole_option": "weird"},
    {"policy_forbidden": "garbage", "policy_filter_error": "x", "resolution_options": [{"family": 3, "source": "cause"}]},
    {"_sole_option": [None, {"apply": {"use_mid": False}}]},
], ids=range(5))
def test_fault_finalize_never_raises(inf):
    out = cf.finalize_infeasibility(inf)
    assert out is inf and isinstance(inf["policy_forbidden"], list)
    assert inf["blocked_by_hard_lock"] == len(inf["policy_forbidden"]) and inf["policy_filter_error"] >= 0


# ── 캐시 없음 · 정상 경로 기록 · import 위생 ──────────────────────────────────


def test_no_cache_registry_change_is_seen_immediately(monkeypatch):
    pair = ("ConsecutiveNightLimit", "set_threshold")
    assert cf.family_action_policy()[pair]["mode"] == "blocked"  # 11b(D13 ③) — 옛 capped
    spec = S.lookup("allow_three_consecutive_nights")
    patched = replace(spec, hard_lock=None)
    new_settings = tuple(patched if s is spec else s for s in S.SETTINGS)
    monkeypatch.setattr(S, "SETTINGS", new_settings)
    monkeypatch.setattr(hl, "SETTINGS", new_settings)
    monkeypatch.setitem(S.SETTINGS_BY_KEY, "allow_three_consecutive_nights", patched)
    assert cf.family_action_policy()[pair] == {"mode": "ok", "rule_nos": []}
    assert "allow_three_consecutive_nights" not in cf.strict_base()


def test_normal_path_leaves_no_warning_or_failure_line(caplog, capsys):
    """정상 경로(§2.0.1) — team_min_remove 적용 예외가 지나가도 WARNING 이상 0 · '실패'·'error:' 줄 0.
    11a-2 — 생성 관문(정당 처방)·set cover 금지 처방 빼기(UNDIAGNOSED)도 같은 판정을 지나며 기록은 DEBUG 뿐이다.
    11c — 표지 판정(`family_modes`·`policy_forbidden_for_causes`·`capped_suffix`)과 payload 후처리(`mark_treatment_views`)도 같다."""
    caplog.set_level(logging.DEBUG, logger=_LOGGER)
    cf.forbidden_treatments()
    cf.marked_treatments()
    cf.family_action_policy()
    cf.finalize_infeasibility({"resolution_options": [
        _card("bundle:a", treatment_ids=["treatment:soft:grade_max", "treatment:threshold:monthly_night_cap"]),
        _card("bundle:b", treatment_ids=["treatment:disable:night_recovery"])]})
    cf.assert_treatments_allowed(["treatment:soft:grade_max"])
    assert hitter.propose_bundles(active_causes=["UNDIAGNOSED"])
    # 11c — 표지 판정(family 접기·원인을 덮는 금지 처방·상한 문구)과 그 후처리가 든 빌더도 정상 경로는 DEBUG 뿐이다.
    assert cf.family_modes()["MonthlyNightCap"]["mode"] == "capped"
    assert cf.policy_forbidden_for_causes(["RECOVERY_2N2OFF_BLOCKS", "UNDIAGNOSED"])
    assert cf.capped_suffix("MonthlyNightCap", "set_threshold")
    assert build_blocking_payload({"issues": [{"reason_code": "MONTHLY_NIGHT_CAPACITY_SHORTAGE", "severity": "hard",
                                               "evidence": {"n_required": 60, "n_capacity": 30}}]})
    out = capsys.readouterr().out
    assert [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING] == []
    assert [ln for ln in out.splitlines() if "실패" in ln or ln.startswith("error:")] == []
    assert any(r.levelno == logging.DEBUG and "team_min_remove" in r.getMessage() for r in caplog.records)


def test_card_filter_import_stays_light_in_fresh_subprocess():
    heavy = ("ortools", "sqlalchemy", "services.semantics.ontology", "services.treatment_applicator",
             "services.constraint_impact.control", "services.ontology_graph.mcs_trace", "fastapi")
    code = ("import sys; sys.path.insert(0, 'app'); import services.policy.card_filter; "
            f"print(sorted(m for m in {heavy!r} if m in sys.modules))")
    out = subprocess.run([sys.executable, "-c", code], cwd=REPO_ROOT, capture_output=True, text=True, check=True)
    assert out.stdout.strip() == "[]"


def test_card_filter_module_head_imports():
    """모듈 머리 import 는 hard_lock·레지스트리(+표준 logging) 뿐이다(§2.0.1)."""
    tree = ast.parse((REPO_ROOT / "app/services/policy/card_filter.py").read_text(encoding="utf-8"))
    heads = {a.name for n in tree.body if isinstance(n, ast.Import) for a in n.names}
    heads |= {n.module for n in tree.body if isinstance(n, ast.ImportFrom)}
    assert heads == {"__future__", "logging", "services.engine_contract", "services.engine_contract.settings",
                     "services.policy.hard_lock"}


# ── 11a-1 ① 탐색 카탈로그 — 전 항목 BLOCK 0 · #1 클램프 ─────────────────────────────

#: 11a-1 이 카탈로그에서 지운 하드락 항목(D13 ② 의 relax_consecutive_nights 포함).
_DELETED_PROBE_IDS = frozenset({
    "disable_2n2off", "disable_3n2off", "relax_consecutive_nights", "disable_not_one_night",
    "disable_banned_day_after_eve", "disable_ban_n_to_d", "disable_ban_n_to_e",
})
_CATALOG_IDS = [item["id"] for item in RELAX_CATALOG]


def _catalog_item(item_id: str) -> dict:
    return next(item for item in RELAX_CATALOG if item["id"] == item_id)


@pytest.mark.parametrize("sample", SAMPLES)
@pytest.mark.parametrize("item_id", _CATALOG_IDS)
def test_relax_catalog_every_item_has_no_block_on_every_sample(item_id, sample):
    """① 카탈로그 전 항목 × 표본 B1~B4·0 — `apply(base)` delta 와 탐색 극값 어디에도 BLOCK 0."""
    violations = cf.catalog_item_violations(_catalog_item(item_id), SAMPLES[sample]())
    assert [v for v in violations if v.kind == hl.BLOCK] == []


def test_relax_catalog_drops_hard_lock_items_labels_and_d13_key():
    """하드락 7항목이 카탈로그·문구 표에서 빠졌고, 카드가 `max_consecutive_nights` 를 못 낸다(D13 ②)."""
    assert not _DELETED_PROBE_IDS & set(_CATALOG_IDS)
    assert set(TRADEOFF_KO) == set(_CATALOG_IDS)
    keys = {(item.get("search") or {}).get("key") for item in RELAX_CATALOG}
    keys |= {key for make in SAMPLES.values() for item in RELAX_CATALOG for key in item["apply"](make())}
    assert "max_consecutive_nights" not in keys
    assert "max_consecutive_nights" not in COL_LABEL_KO and "max_consecutive_nights" not in _FIX_BY_KEY


@pytest.mark.parametrize(("config", "want"), [
    ({}, {}),
    ({"max_conseq_work": None}, {}),
    ({"max_conseq_work": "abc"}, {}),
    ({"max_conseq_work": math.inf}, {}),
    ({"max_conseq_work": 0}, {"max_conseq_work": 5}),
    ({"max_conseq_work": -1}, {"max_conseq_work": 5}),
    ({"max_conseq_work": 1}, {"max_conseq_work": 4}),
    ({"max_conseq_work": 2}, {"max_conseq_work": 5}),
    ({"max_conseq_work": 4}, {"max_conseq_work": 5}),
    ({"max_conseq_work": 5}, {}),
    ({"max_conseq_work": 8}, {}),
], ids=["absent", "none", "text", "inf", "zero", "negative", "low", "cur3_hits_cap", "near_cap", "at_cap", "over_cap"])
def test_raise_consec_work_delta_cur_rules(config, want):
    """`_raise_consec_work_delta` cur 규칙(N31 — #6 과 0 의 뜻이 다르다): 부재·None·숫자 아님 → {} · ≤0 → 상한 ·
    0<cur<상한 → min(cur+3, 상한) · ≥상한 → {}. 상한은 레지스트리 `max_conseq_work` cap(5) 이고 탐색 상한도 같다."""
    assert _consec_work_cap() == S.lookup("max_conseq_work").hard_lock.cap == 5
    assert _catalog_item("raise_max_consec_work")["search"]["hi"] == _consec_work_cap()
    assert _raise_consec_work_delta(config) == want


@pytest.mark.parametrize(("cur", "feasible_from", "found"), [(5, None, None), (3, 5, 5), (0, 4, 4)],
                         ids=["at_cap_skipped", "below_cap", "zero"])
def test_probe_never_resolves_consec_work_above_cap(cur, feasible_from, found):
    """탐색 기계는 무수정(P-3) — 상한 이상이면 항목을 건너뛰고(재계산 0), 그 밖에는 상한 안 값만 재계산한다."""
    seen: list = []

    def resolve(cfg):
        seen.append(cfg.get("max_conseq_work"))
        return feasible_from is not None and int(cfg.get("max_conseq_work")) >= feasible_from, {}

    res = probe_relaxations({"max_conseq_work": cur}, resolve, catalog=[_catalog_item("raise_max_consec_work")],
                            verify=False, budget_sec=0, logger=lambda _msg: None)
    assert seen[0] == cur and max(seen) <= _consec_work_cap()
    assert [r["delta"] for r in res["resolutions"]] == ([] if found is None else [{"max_conseq_work": found}])
    if found is None:
        assert seen == [cur], "상한 이상이면 기준 재현 1회 뒤 탐색·조합 없이 건너뛴다"


# ── 11a-1 ④ 원인 카드 targets · ⑤ 레버 · ⑥ 개인 상한 · ⑦(1차) 카드 ≡ 가드 ──────────────────


def _assert_cards_pass_guard(inf: dict) -> None:
    """⑦ — 필터를 통과한 카드는 하드락 가드(`assert_night_caps_within_hard_lock`)와 생성 진입 관문
    (`assert_treatments_allowed` — 11a-2)에서 400 이 나지 않는다(P-1)."""
    for card in inf["resolution_options"]:
        assert_night_caps_within_hard_lock(card.get("apply"), card.get("monthly_limit_release"))
        cf.assert_treatments_allowed(card.get("treatment_ids"))


def _finalized(cards: list) -> dict:
    inf = {"resolution_options": list(cards)}
    cf.finalize_infeasibility(inf)
    return inf


#: 원인 카드 targets 표본(§2.2 ④) — 일반 간호사 cap 20 · 야간전담 current 14·cap 8 · 야간전담 current 16 · 금지근무 대상.
_CAUSE_TARGETS = {
    "regular_cap20": [{"family": "monthly_limit", "nurse_id": "R1", "name": "일반", "cap": 20, "current": 18}],
    "night_only_14": [{"family": "night_cap", "nurse_id": "N1", "name": "야간", "cap": 8, "current": 14,
                       "is_night_only": True}],
    "night_only_16": [{"family": "night_cap", "nurse_id": "N2", "name": "야간2", "cap": 8, "current": 16,
                       "is_night_only": True}],
    "banned": [{"family": "banned_wanted", "nurse_id": "B1", "name": "금지", "clash_days": [3],
                "allowed_shifts": ["N"]}],
}


def test_cause_cards_with_targets_carry_values_within_hard_lock():
    """④ 원인 카드 값 — 일반 cap 20 → n_max 15(#6 안) · 야간전담 14 → 병동 상한 14 · 야간전담 16 → 수동 정정 카드 ·
    금지근무 → 대안 2카드(banned_release·allowed_add)."""
    cards = {case: cause_to_resolution_options("personal_infeasible", None, targets)
             for case, targets in _CAUSE_TARGETS.items()}
    assert [c["option_id"] for c in cards["regular_cap20"]] == ["cause:fix_all_personal"]
    assert cards["regular_cap20"][0]["monthly_limit_release"] == [{"nurse_id": "R1", "field": "n_max", "value": 15}]
    assert cards["night_only_14"][0]["apply"] == {"max_nig_per_month": 14}
    assert [(c["option_id"], c["fix"]["mode"]) for c in cards["night_only_16"]] == [
        ("cause:night_only_over_hard_cap", "manual")]
    assert [c["option_id"] for c in cards["banned"]] == ["cause:banned_release", "cause:allowed_add"]


@pytest.mark.parametrize("case", _CAUSE_TARGETS)
def test_cause_cards_with_targets_pass_the_filter_and_guard(case):
    """④ finalize 뒤 BLOCK 카드 0 — targets 카드는 모두 남고 하드락 가드도 통과한다(⑦)."""
    cards = cause_to_resolution_options("personal_infeasible", None, _CAUSE_TARGETS[case])
    inf = _finalized(cards)
    assert inf["resolution_options"] == cards
    assert (inf["policy_forbidden"], inf["policy_filter_error"]) == ([], 0)
    _assert_cards_pass_guard(inf)


@pytest.mark.parametrize("family", [f for f, (blocks, _marks) in _E5.items() if blocks])
def test_tradeoff_hard_lock_family_card_goes_to_policy_forbidden(family):
    """④ 하드락 가족 원인 카드(tradeoff — 2N 휴식·야간 회복·1N·연속근무·전이·전월 경계)는 빠지고 `cause_card` 로 남는다."""
    inf = _finalized(cause_to_resolution_options("coupled_sequence", family, []))
    assert inf["resolution_options"] == []
    assert [(p["kind"], p["id"]) for p in inf["policy_forbidden"]] == [("cause_card", f"cause:{family}")]
    assert (inf["blocked_by_hard_lock"], inf["policy_filter_error"]) == (1, 0)


def _capacity_issue(suggested_value: int) -> dict:
    """연속근무 상한이 용량을 누르는 precheck 이슈(`conseq_cap_binding` — team_grade_precheck 모양)."""
    return {"reason_code": "CAPACITY_TOTAL_SHORTAGE", "severity": "hard",
            "evidence": {"conseq_cap_binding": {"config_key": "max_conseq_work", "current": 3,
                                                "suggested_value": suggested_value, "capacity_uncapped": 420}}}


def _built(builder: str, issues: list[dict]) -> dict:
    if builder == "blocking":
        return build_blocking_payload({"issues": issues})["infeasibility"]
    return build_unrecoverable_payload(precheck_result={"issues": issues})["infeasibility"]


def _capacity_cause(inf: dict) -> dict:
    (cause,) = [c for c in inf["causes"] if c.get("reason_code") == "CAPACITY_TOTAL_SHORTAGE"]
    return cause


@pytest.mark.parametrize("builder", ["blocking", "unrecoverable"])
def test_conseq_lever_within_cap_stays_and_replaces_manual_fix(builder):
    """⑤ 제안값 5(상한 안) — 레버 카드가 남고, 원인의 '간호사 추가' 수동 안내(fix)는 지운다(지금 동작 그대로)."""
    inf = _built(builder, [_capacity_issue(5)])
    assert "lever:max_conseq_work" in [o.get("option_id") for o in inf["resolution_options"]]
    assert "fix" not in _capacity_cause(inf) and inf["policy_forbidden"] == []
    _assert_cards_pass_guard(inf)


@pytest.mark.parametrize("builder", ["blocking", "unrecoverable"])
def test_conseq_lever_over_cap_is_blocked_and_keeps_manual_fix(builder):
    """⑤ 제안값 6(#1 초과) — 레버 카드가 빠져 `policy_forbidden` 에 card 로 남고, 원인의 수동 안내(fix)는 그대로다."""
    inf = _built(builder, [_capacity_issue(6)])
    assert "lever:max_conseq_work" not in [o.get("option_id") for o in inf["resolution_options"]]
    assert _capacity_cause(inf)["fix"]["mode"] == "manual"
    assert [(p["kind"], p["id"], p["rule_nos"]) for p in inf["policy_forbidden"]] == [
        ("card", "lever:max_conseq_work", [1])]
    assert inf["blocked_by_hard_lock"] == 1
    _assert_cards_pass_guard(inf)


def _night_capacity_issue(rows: list[dict], need: int) -> dict:
    return {"reason_code": "MONTHLY_NIGHT_CAPACITY_SHORTAGE", "severity": "hard",
            "evidence": {"n_required": need, "night_capable_nurses": rows}}


def test_personal_night_cap_card_passes_the_filter_and_guard():
    """⑥ 개인 야간 상한 카드(`personal_night_cap_options_from_issues`)는 값이 #6 안이라 그대로 나간다."""
    rows = [{"nurse_id": f"P{i}", "name": f"개인{i}", "capped_by": "personal", "capped_by_all": ["personal"],
             "personal_night_cap": 2, "night_cap": 2, "capacity_days": 20,
             "cap_by_axis": {"working": 20, "recovery": 21, "config": 15, "personal": 2}} for i in (1, 2)]
    (card,) = personal_night_cap_options_from_issues([_night_capacity_issue(rows, 20)])
    assert [r["value"] for r in card["monthly_limit_release"]] == [10, 10]
    inf = build_blocking_payload({"issues": [_night_capacity_issue(rows, 20)]})["infeasibility"]
    assert card["option_id"] in [o.get("option_id") for o in inf["resolution_options"]]
    _assert_cards_pass_guard(inf)


def test_treatment_sizing_apply_card_passes_the_guard():
    """⑦(1차) 처방 사이징 apply(`treatments_to_resolution_options` 의 monthly_night_cap `suggested_value`)도 통과한다."""
    rows = [{"nurse_id": f"N{i}", "capacity_days": 21} for i in range(23)]
    inf = build_blocking_payload({"issues": [_night_capacity_issue(rows, 180)]})["infeasibility"]
    sized = [o["apply"] for o in inf["resolution_options"] if (o.get("apply") or {}).get("max_nig_per_month")]
    assert sized == [{"max_nig_per_month": 8}]
    _assert_cards_pass_guard(inf)


# ── ⑧ E4 원인 payload — 금지 처방 id 0(11c 부터 payload 전체 − policy_forbidden·표지 붙은 mapped_treatments) · BLOCK delta 0 ──

#: E4 원인 6개(§2.2 ⑧) — 자동 처방이 하드락 처방뿐이거나 그것을 포함하는 원인. 별칭은 온톨로지에서 읽는다.
_E4_CAUSES = (
    "cause:capacity:daily_night_shortage", "cause:carryover:prev_month_n_tail_blocks_start",
    "cause:consecutive:work_limit_blocks_coverage", "cause:recovery:three_n_two_off_blocks_demand",
    "cause:recovery:two_n_two_off_blocks_demand", "cause:transition:nod_pattern_forces_infeasibility",
)


def _outside_marked_views(payload: dict) -> str:
    """⑧ 범위(11c) — payload 전체에서 `policy_forbidden`(막은 기록)과 `policy_forbidden: true` 표지가 붙은 problem_list
    `mapped_treatments` 항목(진단 기록 — 원인에 대응하는 처방이지만 정책으로 막았다)만 뺀 JSON."""
    stripped = copy.deepcopy(payload)
    inf = stripped.get("infeasibility") or {}
    inf.pop("policy_forbidden", None)
    for item in (inf.get("resolution_narrative") or {}).get("problem_list") or []:
        item["mapped_treatments"] = [m for m in item.get("mapped_treatments") or [] if m.get("policy_forbidden") is not True]
    return json.dumps(stripped, ensure_ascii=False)


def _problem_list_treatments(payload: dict) -> set[str]:
    narrative = (payload.get("infeasibility") or {}).get("resolution_narrative") or {}
    return {m.get("treatment_id") for item in narrative.get("problem_list") or [] for m in item.get("mapped_treatments") or []}


def _unmarked_forbidden_in_problem_list(payload: dict, forbidden) -> list[str]:
    narrative = (payload.get("infeasibility") or {}).get("resolution_narrative") or {}
    return [m.get("treatment_id") for item in narrative.get("problem_list") or [] for m in item.get("mapped_treatments") or []
            if m.get("treatment_id") in forbidden and m.get("policy_forbidden") is not True]


def _e4_payloads(cause_id: str, alias: str) -> dict[str, dict]:
    """같은 원인을 세 출구로 — precheck 이슈(싱크 a) · 사후 위반(싱크 b) · 라우터 폴백 인자(싱크 b 경유)."""
    from routers.roster_create import _fallback_unrecoverable_from_exception

    violation = {"reason_code": alias, "node_id": cause_id, "details": {}, "human_message_ko": f"matrix synthesis: {cause_id}"}
    return {
        "issue": build_blocking_payload({"issues": [{"reason_code": alias, "severity": "hard", "evidence": {}}]}),
        "violation": build_unrecoverable_payload(violated_constraints=[violation]),
        "router": _fallback_unrecoverable_from_exception(f"생성 실패 [reason_code={alias}]"),
    }


@pytest.mark.parametrize("cause_id", _E4_CAUSES)
def test_e4_payload_carries_no_forbidden_treatment_or_block_delta(cause_id):
    """⑧ — E4 원인 별칭을 세 출구로 넣어 지은 payload 전체에서 `policy_forbidden` 과 표지 붙은 `mapped_treatments` 항목만 빼면
    금지 처방 id 0(11c — 처방 목록·서술·problem_list·그래프·카드 전부) · 카드 BLOCK delta 0. BLOCK delta 는 카드 효과 통로 두
    칸만 본다(`apply` → override_violations · `monthly_limit_release` → release_violations, §2.2 ⑧). 표본이 무의미하지 않게
    별칭마다 한 출구 이상의 problem_list 가 금지 처방을 싣고(진단은 그대로), problem_list 의 금지 처방은 모두 표지가 붙는다."""
    forbidden = sorted(cf.forbidden_treatments())
    aliases = get_default_ontology().get_cause(cause_id).aliases
    assert aliases
    for alias in aliases:
        payloads = _e4_payloads(cause_id, alias)
        assert any(_problem_list_treatments(p) & set(forbidden) for p in payloads.values()), f"{alias}: 표본 무의미"
        for kind, payload in payloads.items():
            cards = payload["infeasibility"]["resolution_options"]
            text = _outside_marked_views(payload)
            assert [t for t in forbidden if t in text] == [], (alias, kind)
            assert _unmarked_forbidden_in_problem_list(payload, forbidden) == [], (alias, kind)
            assert [v for c in cards for v in hl.override_violations(dict(c.get("apply") or {})) if v.kind == hl.BLOCK] == []
            assert [v for c in cards for v in hl.release_violations(c.get("monthly_limit_release"))] == []
            _assert_cards_pass_guard(payload["infeasibility"])


# ── 11a-2 ② 처방 set cover 제외 · 생성 진입 관문 ───────────────────────────────────────────────

#: EI5(presolve 골든) 원인 3종 — 3번째 번들이 night_recovery+coverage_min → coverage_min 단독이 된다([실측 E4]).
_EI5_CAUSES = ("GLOBAL_DAY_CAPACITY_SHORTAGE", "CAPACITY_TOTAL_SHORTAGE", "MONTHLY_NIGHT_CAPACITY_SHORTAGE")
#: 자동 처방이 하드락 처방뿐인 원인 4개(§2.3 — rec_2n2off·trans_nod·consec_work 에 rec_3n2off 를 더했다).
_HARD_LOCK_ONLY_CAUSES = {
    "rec_2n2off": ("cause:recovery:two_n_two_off_blocks_demand", "RECOVERY_2N2OFF_BLOCKS"),
    "rec_3n2off": ("cause:recovery:three_n_two_off_blocks_demand", "RECOVERY_3N2OFF_BLOCKS"),
    "trans_nod": ("cause:transition:nod_pattern_forces_infeasibility", "TRANSITION_BAN_NOD_CHAIN"),
    "consec_work": ("cause:consecutive:work_limit_blocks_coverage", "CONSECUTIVE_WORK_LIMIT_BLOCKS"),
}


@pytest.mark.parametrize("sample", SAMPLES)
def test_treatments_outside_forbidden_apply_without_block_on_every_sample(sample):
    """② `forbidden_treatments()` 밖 처방은 표본 B1~B4·0 어디에 적용해도 diff 가 BLOCK 0(엄격 표본 파생이 놓친 처방 없음).
    적용 예외는 team_min_remove(scope 없음 — 정상 경로)뿐이다."""
    forbidden = cf.forbidden_treatments()
    errors, blocks = [], []
    for tid in sorted(get_default_ontology().treatments):
        if tid in forbidden:
            continue
        base = SAMPLES[sample]()
        try:
            res = apply_treatments([tid], base)
        except ValueError:
            errors.append(tid)
            continue
        diff = {k: res.patched_config.get(k) for k in dict.fromkeys([*base, *res.patched_config])
                if k != "_applied_constraint_adjustments" and base.get(k) != res.patched_config.get(k)}
        blocks += [(tid, v.key) for v in hl.override_violations(diff) if v.kind == hl.BLOCK]
    assert blocks == [] and errors == ["treatment:scope:team_min_remove"]


def test_set_cover_never_proposes_a_forbidden_treatment_for_any_cause():
    """② 온톨로지 원인 각각에서 `propose_bundles`·brute force 결과에 금지 처방 0 — 그 처방을 낼 수 있는 원인이 실재한다."""
    onto = get_default_ontology()
    forbidden = set(cf.forbidden_treatments())
    reachable = {t.treatment_id for cid in onto.causes for t in onto.treatments_for_cause(cid)} & forbidden
    assert reachable == forbidden, "표본 무의미 — 금지 처방을 덮는 원인이 있어야 한다"
    hits = []
    for cid in sorted(onto.causes):
        bundles = [*hitter.propose_bundles(active_causes=[cid], max_alternatives=3),
                   *hitter.enumerate_minimal_hitting_sets_brute_force(active_causes=[cid])]
        hits += [(cid, t.treatment_id) for b in bundles for t in b.treatments if t.treatment_id in forbidden]
    assert hits == []


def test_ei5_third_bundle_becomes_coverage_min_alone():
    """② [실측 E4] EI5 원인 3종 — 1·2번째 번들은 그대로, 3번째(night_recovery+coverage_min)는 coverage_min 단독."""
    bundles = hitter.propose_bundles(active_causes=list(_EI5_CAUSES), max_alternatives=3)
    assert [(b.bundle_id, b.uncovered_causes) for b in bundles] == [
        ("bundle:treatment:data:add_nurse,treatment:threshold:coverage_min,treatment:threshold:monthly_night_cap", []),
        ("bundle:treatment:threshold:coverage_min,treatment:threshold:monthly_night_cap",
         ["cause:capacity:monthly_total_shortage"]),
        ("bundle:treatment:threshold:coverage_min",
         ["cause:capacity:monthly_night_shortage", "cause:capacity:monthly_total_shortage"]),
    ]


@pytest.mark.parametrize("case", _HARD_LOCK_ONLY_CAUSES)
def test_hard_lock_only_cause_leaves_empty_primary_bundle_and_hard_case(case):
    """② 처방이 하드락 처방뿐인 원인 — 1순위 번들 `bundle:empty`(원인이 하드락뿐이라는 신호) · hard_case C-UNCOVER(의도된 변화) ·
    카드 0(싱크가 거를 카드도 생산되지 않는다) · 원인 진단(problem_list)은 그대로 남는다. 11c 부터 `policy_forbidden` 에는 그
    원인을 덮는 금지 처방 기록(`kind="treatment"`)만 실린다(카드 기록 0)."""
    cause_id, alias = _HARD_LOCK_ONLY_CAUSES[case]
    violation = {"reason_code": alias, "node_id": cause_id, "details": {}, "human_message_ko": f"matrix synthesis: {cause_id}"}
    payload = build_unrecoverable_payload(violated_constraints=[violation])
    inf = payload["infeasibility"]
    forbidden = cf.forbidden_treatments()
    covering = [t.treatment_id for t in get_default_ontology().treatments_for_cause(cause_id) if t.treatment_id in forbidden]
    assert inf["treatment_recommendations"][0]["bundle_id"] == "bundle:empty"
    assert inf["hard_case"]["is_hard"] is True and "C-UNCOVER" in inf["hard_case"]["criteria_matched"]
    assert inf["resolution_options"] == []
    assert [(p["kind"], p["id"], p["causes"]) for p in inf["policy_forbidden"]] == [
        ("treatment", tid, [cause_id]) for tid in covering]
    assert inf["blocked_by_hard_lock"] == len(covering) >= 1
    assert inf["resolution_narrative"]["action_levers"] == []
    assert _problem_list_treatments(payload) & set(forbidden)


def test_generation_gate_rejects_exactly_the_forbidden_treatments():
    """② 생성 관문 — 처방마다 `assert_treatments_allowed([tid])` 가 400 ⇔ `forbidden_treatments()` 에 든 처방. 문구에 처방 라벨과
    걸린 하드락 규칙 라벨이 든다."""
    onto = get_default_ontology()
    forbidden = cf.forbidden_treatments()
    rejected = {}
    for tid in sorted(onto.treatments):
        try:
            cf.assert_treatments_allowed([tid])
        except HTTPException as exc:
            assert exc.status_code == 400
            rejected[tid] = exc.detail
    assert set(rejected) == set(forbidden)
    for tid, detail in rejected.items():
        t = onto.get_treatment(tid)  # 11r — 처방 이름은 누른 카드 제목과 같은 출처(friendly 라벨 → yaml label)
        assert f"'{friendly_config_key_label(t.config_key) or t.label}'" in detail
        assert all(hl.rule_label(n) in detail for n in forbidden[tid]), detail


@pytest.mark.parametrize(("value", "rejected"), [
    ("treatment:disable:night_recovery", True),
    (["treatment:disable:night_recovery", "treatment:disable:night_recovery"], True),
    (("treatment:disable:transition_ban",), True),
    (["treatment:no_such"], False),
    ([123, ["x"]], False),
    (5, False),
], ids=["bare_str", "dup", "tuple", "unknown", "non_str", "not_list"])
def test_generation_gate_reads_odd_inputs_without_other_exceptions(value, rejected):
    """관문은 400 밖의 예외를 내지 않는다(입력 오류를 코드 버그로 보이게 하지 않는다) — 문자열 하나는 id 하나, 겹친 id 는 한 번,
    온톨로지에 없는 id·목록이 아닌 값은 통과(적용기도 적용하지 않는다)."""
    try:
        cf.assert_treatments_allowed(value)
    except HTTPException as exc:
        assert rejected and exc.status_code == 400
        assert exc.detail.count("'2N 후 2일 OFF 규칙'") <= 1
        return
    assert not rejected


@pytest.mark.parametrize("value", [None, [], ()])
def test_generation_gate_without_treatments_skips_judgement(monkeypatch, value):
    """관문은 처방이 비면(None·빈 목록) 판정을 계산하지 않는다 — 처방 없는 생성(주 골든 14개)에 판정 기록이 번지지 않게."""
    monkeypatch.setattr(cf, "forbidden_treatments", lambda ontology=None: pytest.fail("판정을 계산했다"))
    cf.assert_treatments_allowed(value)


def test_propose_bundles_fail_closed_when_forbidden_judgement_fails(monkeypatch, caplog):
    """② §2.0.6 ③ — set cover 안 금지 목록 계산이 실패하면 하드락 family 처방을 모두 빼고 계속한다(예외 0 · WARNING).
    payload 빌더의 except 가 처방·서술·번들을 통째로 비우지 않는다."""
    def _boom(ontology=None):
        raise RuntimeError("주입 — 금지 처방 판정 실패")

    monkeypatch.setattr(cf, "forbidden_treatments", _boom)
    onto = get_default_ontology()
    family_ids = cf.hard_lock_family_treatment_ids(onto)
    tids = [t.treatment_id for cid in sorted(onto.causes)
            for b in [*hitter.propose_bundles(active_causes=[cid]),
                      *hitter.enumerate_minimal_hitting_sets_brute_force(active_causes=[cid])]
            for t in b.treatments]
    assert tids and not set(tids) & family_ids
    inf = build_unrecoverable_payload(violated_constraints=[{
        "reason_code": "N_CAPACITY_SHORTAGE", "node_id": "cause:capacity:daily_night_shortage", "details": {},
        "human_message_ko": "matrix synthesis"}])["infeasibility"]
    assert inf["treatment_recommendations"] and inf["resolution_narrative"] is not None
    assert any(r.levelno == logging.WARNING and r.name == "services.cause_treatment_hitter" for r in caplog.records)


# ── 11b ⑦ 완성 — 가드 400 ⇔ 카드 `apply` 통로 BLOCK(§2.4-6 방향 표의 모든 키) ─────────────────────────────

#: 적대 값 — 상한 경계·문자열 숫자·truthy 문자열·비숫자·Infinity·NaN·list·dict·'1e309'(int 변환 실패).
_ADVERSARIAL_VALUES = (None, True, False, 0, 1, -1, 2, 3, 4, 5, 6, 15, 16, " 16 ", "15", "abc", "", "false", 16.9,
                       math.inf, -math.inf, math.nan, [1], {}, "1e309")


def _guard_table_keys() -> list[str]:
    """§2.4-6 방향 표의 키 — 레지스트리 하드락 행(8종·월경계 스위치)과 인접·영향 행의 정본·별칭·바깥(옛) 이름(NM) + 엔진 내부 금지 키."""
    keys = [key for spec in S.SETTINGS if spec.hard_lock is not None
            for key in (spec.key, *spec.aliases, *((spec.column,) if spec.column else ()))]
    return [*keys, *hl._FORBIDDEN_INTERNAL_KEYS]


def test_guard_table_keys_cover_every_row_of_the_direction_table():
    """표본 무의미 방지 — 키 목록에 8종 정본·별칭·D13 별칭·월경계 스위치·내부 키·인접 키가 모두 든다."""
    keys = set(_guard_table_keys())
    assert {"max_conseq_work", "max_consecutive_work_days", "max_consecutive_work", "ban_shift_transitions", "ban_e_to_d",
            "ban_n_to_d", "ban_n_to_e", "two_offs_after_three_nig", "two_offs_after_two_nig", "ban_single_night", "use_mid",
            "allow_three_consecutive_nights", "max_consecutive_nights", "max_nig_per_month", "max_night_shifts_per_month",
            "cross_month_hard_rules_enable", "cross_month_lookback_days", "_inbound_source_map", "off_days",
            "preceptee_follows_preceptor", "n2n_min_gap"} <= keys


@pytest.mark.parametrize("key", _guard_table_keys())
def test_guard_400_iff_option_violations_block_for_every_hard_lock_key(key):
    """⑦ 완성 — 키마다 적대 값 전부에서 '가드(`assert_night_caps_within_hard_lock`) 400 ⇔ `option_violations` 의 `apply`
    통로 BLOCK'. 필터를 통과한 카드는 가드에서 400 이 나지 않고, 가드가 막는 값은 카드로 나가지 않는다(P-1).
    N전담 가드(`assert_release_not_n_only` — DB 필요)는 이 불변식 밖이다."""
    mismatched = []
    for value in _ADVERSARIAL_VALUES:
        blocks, _ = cf.option_violations({"apply": {key: value}})
        if _guard_rejects({key: value}, None) != bool(blocks):
            mismatched.append(value)
    assert mismatched == [], (key, mismatched)


#: `preceptee_on`·`three_seq_nig` 는 바깥(옛) 이름(요청 X2) — 끝의 두 항목은 파이썬 이름이다. 옛·새 이름이 섞인 delta 에서도 같다(NM).
_MIXED_ITEMS = (("off_days", 3), ("preceptee_on", False), ("max_nig_per_month", 15), ("max_conseq_work", 5),
                ("three_seq_nig", True), ("two_offs_after_two_nig", False), ("max_consecutive_nights", 3),
                ("cross_month_lookback_days", 2), ("max_nig_per_month", 16), ("use_mid", "false"),
                ("allow_three_consecutive_nights", True), ("preceptee_follows_preceptor", False))


@pytest.mark.parametrize(("first", "second"), [(a, b) for i, a in enumerate(_MIXED_ITEMS) for b in _MIXED_ITEMS[i + 1:]
                                               if a[0] != b[0]], ids=repr)
def test_guard_400_iff_option_violations_block_for_mixed_deltas(first, second):
    """⑦ — 키 두 개를 섞은 delta 에서도 같다(인접·영향 표지가 섞여도 BLOCK 만 본다)."""
    delta = dict([first, second])
    blocks, _ = cf.option_violations({"apply": dict(delta)})
    assert _guard_rejects(dict(delta), None) == bool(blocks), delta


@pytest.mark.parametrize("sample", SAMPLES)
def test_relax_catalog_apply_deltas_and_extremes_pass_the_guard(sample):
    """⑦ — 탐색 카탈로그 카드의 `apply` delta 와 탐색 극값(`search.hi`·`search.lo`)은 표본 B1~B4·0 어디서나 11b 가드를
    통과한다. 옛 특성화(`test_hard_lock_guard_characterization.py` 의 `…pass_through_the_guard_unrejected` — 기준 dict 에
    `max_consecutive_nights` 를 섞어 D13 뒤 400)를 카드가 실제로 싣는 delta 로 옮겼다."""
    for item in RELAX_CATALOG:
        assert_night_caps_within_hard_lock(item["apply"](SAMPLES[sample]()), None)
        search = item.get("search") or {}
        for bound in ("hi", "lo"):
            if search.get("key") and search.get(bound) is not None:
                assert_night_caps_within_hard_lock({search["key"]: search[bound]}, None)


# ── 11d ③ control (family, action) 전부 — BLOCK 조정은 적용되되 기록에 '적용 불가'·rule_nos, 기본값이면 기록 칸 없음 ──

_CONTROL_PAIRS = sorted((f, a) for f, acts in control._FAMILY_ACTION_MATRIX.items() for a in acts)
_RECORD_KEYS = {"family", "action", "scope_filter", "target_value", "reason"}
_POLICY_BIG = 10**6  # `family_action_policy` 의 큰 목표값과 같다


def _control_record(family: str, action: str, target) -> tuple[dict, dict]:
    """`family_action_policy` 와 같은 표본·scope 로 조정 하나를 적용 → (적용 뒤 설정, 그 조정 기록)."""
    base = cf._policy_base()
    adj = {"family": family, "action": action, "target_value": target, "scope_filter": {"team": "T1", "shift": "D", "day": 1}}
    out = control.apply_adjustments_to_config(base, [adj])
    (record,) = out["_applied_constraint_adjustments"]
    return out, record


@pytest.mark.parametrize("pair", _CONTROL_PAIRS, ids="/".join)
def test_control_record_marks_follow_the_family_action_policy(pair, monkeypatch):
    """③ 조정 기록 표지 ≡ `family_action_policy` — blocked·capped(큰 값)는 `applicable: False`·같은 rule_nos, marked 는
    `hard_lock_relax_marks` 만, ok 는 기록 칸이 늘지 않는다. 표지만 단다 — 적용된 설정은 표지를 끈 것과 같다(모드 없음)."""
    family, action = pair
    policy = cf.family_action_policy()[pair]
    target = _POLICY_BIG if action == "set_threshold" else None
    out, record = _control_record(family, action, target)
    extra = {k: v for k, v in record.items() if k not in _RECORD_KEYS}
    if policy["mode"] in ("blocked", "capped"):
        assert extra["applicable"] is False and set(extra) <= {"applicable", "blocked_by_hard_lock", "hard_lock_relax_marks"}
        assert sorted({n for b in extra["blocked_by_hard_lock"] for n in b["rule_nos"]}) == policy["rule_nos"]
    elif policy["mode"] == "marked":
        assert set(extra) == {"hard_lock_relax_marks"}
        assert [m["key"] for m in extra["hard_lock_relax_marks"]] == [m["key"] for m in policy["marks"]]
    else:
        assert extra == {}
    monkeypatch.setattr(control, "_hard_lock_record_marks", lambda _before, _after: {})
    unmarked, _ = _control_record(family, action, target)
    assert {k: v for k, v in out.items() if k != "_applied_constraint_adjustments"} == {
        k: v for k, v in unmarked.items() if k != "_applied_constraint_adjustments"}


@pytest.mark.parametrize("pair", [p for p in _CONTROL_PAIRS if p[1] == "set_threshold"], ids="/".join)
def test_control_record_within_cap_adds_no_fields(pair):
    """③ 기본값 — 상한 안 목표값(capped 는 그 상한, 나머지는 0)이면 기록 칸이 늘지 않는다. blocked(D13 ConsecutiveNightLimit —
    별칭은 값과 무관하게 금지)는 작은 값이어도 '적용 불가'다."""
    policy = cf.family_action_policy()[pair]
    _out, record = _control_record(*pair, policy.get("cap", 0))
    if policy["mode"] == "blocked":
        assert record["applicable"] is False and record["blocked_by_hard_lock"][0]["rule_nos"] == policy["rule_nos"]
    else:
        assert set(record) == _RECORD_KEYS
