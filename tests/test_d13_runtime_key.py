"""EI-11 11b — D13 ①③(`.omc/aide_docs/v2/engine_input/ei11/EI11_SPEC_v2.md` §2.4 · 사용자 결정 D13=①, 10-20).

`max_consecutive_nights`(정본 `three_seq_nig` 의 int 별칭)는 런타임에 금지한다.
- ① 월경계(`build_cross_month_constraints`)·월중 경계(`build_mid_month_boundary_constraints`)는 정본 `three_seq_nig` 만 읽는다
  (`L = 3 if three_seq_nig else 2` · 부재 기본값 False→2 는 그대로 — 엔진 부재 기본 True 와의 비대칭은 EI-16 큐).
- ③ 요청(`config_override`)에 이 별칭이 있으면 값과 무관하게 400(`runtime_key_forbidden` — 레지스트리 표지
  `derived_inputs['max_consecutive_nights']['runtime'] == 'forbidden'` 를 판정이 매번 읽는다).
- ② 카드가 이 키를 내지 않는 것은 11a-1 몫이다 — 여기서는 카드·처방·control·fix_location 어디서도 못 낸다는 것을 다시 본다.
"""

from __future__ import annotations

import ast
import math
import textwrap
from dataclasses import replace
from pathlib import Path

import pytest
from fastapi import HTTPException
from services.cp_sat.fix_location import _FIX_BY_KEY
from services.cp_sat.undiagnosed_probe import RELAX_CATALOG
from services.engine_contract import settings as S
from services.ontology_graph.mcs_trace import FAMILY_CONFIG_RELAX
from services.policy import card_filter as cf
from services.policy import hard_lock as hl
from services.roster_create_service import assert_night_caps_within_hard_lock
from services.semantics.ontology import get_default_ontology
from services.treatment_applicator import apply_treatments

ALIAS = "max_consecutive_nights"
_SRC = Path(__file__).resolve().parents[1] / "app" / "services" / "roster_create_service.py"
_BOUNDARY_FUNCS = ("build_cross_month_constraints", "build_mid_month_boundary_constraints")
_MESSAGE = "max_consecutive_nights 는 요청으로 보낼 수 없는 키입니다(나이트 연속 상한은 설정의 나이트 3연속으로 정합니다)."
_GUARD_VALUES = (1, 2, 3, 4, 7, "x", None, True)


def _boundary_func(name: str) -> tuple[ast.FunctionDef, list[str]]:
    src = _SRC.read_text(encoding="utf-8")
    fn = next(n for n in ast.walk(ast.parse(src)) if isinstance(n, ast.FunctionDef) and n.name == name)
    return fn, src.splitlines()


def _boundary_l(name: str, config_dict: dict) -> int:
    """경계 함수 원문의 `def _safe_int` 부터 `L = ` 줄까지를 떼어 실행한 L(`tests/engine_contract/test_effective.py` 방식)."""
    fn, lines = _boundary_func(name)
    start = next(i for i in range(fn.lineno - 1, fn.end_lineno) if lines[i].strip().startswith("def _safe_int"))
    end = next(i for i in range(start, fn.end_lineno) if lines[i].strip().startswith("L = "))
    ns: dict = {"config_dict": config_dict}
    exec(textwrap.dedent("\n".join(lines[start:end + 1])), ns)  # noqa: S102 — 경계 원문 그대로 실행
    return ns["L"]


def _guard_detail(delta: dict) -> str | None:
    try:
        assert_night_caps_within_hard_lock(delta, None)
    except HTTPException as exc:
        assert exc.status_code == 400
        return exc.detail
    return None


# ── ① 경계는 정본만 읽는다 ────────────────────────────────────────────────────────


@pytest.mark.parametrize("name", _BOUNDARY_FUNCS)
def test_boundary_functions_have_no_alias_literal(name):
    fn, _lines = _boundary_func(name)
    literals = [n.lineno for n in ast.walk(fn) if isinstance(n, ast.Constant) and n.value == ALIAS]
    assert literals == [], f"{name}: 별칭 리터럴이 남았다(줄 {literals})"


def test_registry_and_rule_tags_say_the_canonical_key_wins():
    """레지스트리 표지(정본 우선 · 런타임 금지)와 규칙 이름표가 ① 과 같은 사실을 말한다 — 경계를 쓰는 두 규칙
    (`initial_forbidden`·`fixed_assignment:*`)의 governing_keys 는 정본 `three_seq_nig` 를 들고 별칭 런타임 키는 없다."""
    from services.engine_contract.rules import RULE_PREFIXES, RULES

    info = S.lookup("three_seq_nig").derived_inputs[ALIAS]
    assert (info["precedence"], info["runtime"]) == ("canonical_wins", "forbidden")
    specs = [*RULES.values(), *RULE_PREFIXES.values()]
    assert [s.id for s in specs if f"runtime.{ALIAS}" in s.governing_keys] == []
    boundary_rules = [s for s in specs if s.id in ("initial_forbidden", "fixed_assignment:*")]
    assert len(boundary_rules) == 2 and all("three_seq_nig" in s.governing_keys for s in boundary_rules)


@pytest.mark.parametrize("name", _BOUNDARY_FUNCS)
@pytest.mark.parametrize(("config_dict", "want"), [
    ({"three_seq_nig": False, ALIAS: 7}, 2),
    ({"three_seq_nig": True, ALIAS: 2}, 3),
    ({}, 2),
    ({ALIAS: 7}, 2),
], ids=["alias_cannot_loosen", "alias_cannot_tighten", "absent_default_2", "alias_only_ignored"])
def test_boundary_l_reads_only_the_canonical_key(name, config_dict, want):
    assert _boundary_l(name, config_dict) == want


# ── ③ 가드 — 값과 무관하게 400 · 전용 문구 / 정본은 통과 ─────────────────────────────────


@pytest.mark.parametrize("value", _GUARD_VALUES, ids=repr)
def test_guard_rejects_alias_with_dedicated_message(value):
    assert _guard_detail({ALIAS: value}) == _MESSAGE


def test_guard_passes_canonical_three_seq_nig():
    assert _guard_detail({"three_seq_nig": True}) is None
    assert _guard_detail({"three_seq_nig": False}) is None


# ── ④ 판정 — override 는 runtime_key_forbidden · config_change 는 그대로 ─────────────────────


@pytest.mark.parametrize("value", (*_GUARD_VALUES, math.inf, [1]), ids=repr)
def test_override_violations_reason_is_runtime_key_forbidden(value):
    (violation,) = hl.override_violations({ALIAS: value})
    assert (violation.reason_code, violation.rule_nos, violation.kind, violation.key) == (
        "runtime_key_forbidden", (3,), hl.BLOCK, ALIAS)
    assert violation.message_ko == _MESSAGE


def test_override_violations_takes_the_alias_out_before_the_scan():
    """같은 키를 두 번 내지 않는다 — 다른 키의 판정은 그대로 붙는다(비숫자 별칭도 `boundary_switch_off` 로 다시 안 낸다)."""
    got = hl.override_violations({ALIAS: "x", "two_offs_after_two_nig": False, "off_days": 3})
    assert [(v.key, v.reason_code) for v in got] == [
        (ALIAS, "runtime_key_forbidden"), ("two_offs_after_two_nig", "turned_off"), ("off_days", "adjacent_changed")]


def test_config_change_violations_keep_the_old_classification():
    """설정 변경 판정은 그대로다 — 숫자 4 는 exceeds_cap BLOCK, 'x' 는 boundary_switch_off relax_mark, 3 은 통과
    (설정 저장은 `RosterConfigCreate` 가 이 키를 버려 효과가 없다)."""
    (over,) = hl.config_change_violations({ALIAS: 4})
    assert (over.reason_code, over.kind) == ("exceeds_cap", hl.BLOCK)
    (bad,) = hl.config_change_violations({ALIAS: "x"})
    assert (bad.reason_code, bad.kind) == ("boundary_switch_off", hl.RELAX_MARK)
    assert hl.config_change_violations({ALIAS: 3}) == []


# ── ⑤ 레지스트리 표지를 지우면 ③④ 가 깨진다(캐시 없음 — 즉시) ────────────────────────────────


def test_removing_the_registry_marker_breaks_the_rule_immediately(monkeypatch):
    spec = S.lookup("three_seq_nig")
    info = dict(spec.derived_inputs[ALIAS])
    assert info.pop("runtime") == "forbidden"
    patched = replace(spec, derived_inputs={ALIAS: info})
    new_settings = tuple(patched if s is spec else s for s in S.SETTINGS)
    monkeypatch.setattr(S, "SETTINGS", new_settings)
    monkeypatch.setattr(hl, "SETTINGS", new_settings)
    monkeypatch.setitem(S.SETTINGS_BY_KEY, "three_seq_nig", patched)
    assert _guard_detail({ALIAS: 2}) is None, "표지가 없으면 상한 안 값은 통과(옛 동작)"
    assert [v.reason_code for v in hl.override_violations({ALIAS: 7})] == ["exceeds_cap"]
    assert _guard_detail({ALIAS: 7}) != _MESSAGE


# ── ⑥ 카드·처방·control·fix_location 어디서도 이 키를 못 낸다 ─────────────────────────────────


def test_no_producer_emits_the_alias():
    samples = [cf.strict_base(), {}, {**cf.strict_base(), "three_seq_nig": False, "max_conseq_work": 3}]
    catalog_keys = {(item.get("search") or {}).get("key") for item in RELAX_CATALOG}
    catalog_keys |= {key for base in samples for item in RELAX_CATALOG for key in item["apply"](dict(base))}
    assert ALIAS not in catalog_keys
    onto = get_default_ontology()
    assert all(getattr(t, "config_key", None) != ALIAS for t in onto.treatments.values())
    for tid in sorted(onto.treatments):
        try:
            patched = apply_treatments([tid], cf.strict_base()).patched_config
        except ValueError:
            continue  # team_min_remove — scope 없는 정상 경로 적용 예외
        assert ALIAS not in patched, tid
    assert all(ALIAS not in effect for effect in FAMILY_CONFIG_RELAX.values())
    assert ALIAS not in _FIX_BY_KEY
    assert cf.family_action_policy()[("ConsecutiveNightLimit", "set_threshold")] == {"mode": "blocked", "rule_nos": [3]}
