"""A2 역방향 커버리지 — 소비자 4표 테스트(설계서 §2.6 A2 · §5.3 'EI-4 역방향 감사').

RULES(+ RULE_PREFIXES 접두 2개)의 모든 rule id 가 온톨로지·원인·카드·precheck 네 소비자
표에 행을 갖는지, 그 값이 이 표의 `NONE_REASONS` 안에서만 '없음: 이유'를 쓰는지, 값이
처리 id 라면 그 식별자가 실제로 소비자 코드 안에 있는지를 본다.

이 모듈은 검사만 한다 — DB·ortools·엔진 실행을 import 하지 않는다. 네 소비자 표 자신도
데이터만 담은 모듈이라 안전하게 import 한다. '실제 존재' 검사는 되도록 그 값을 실제로 쓰는
정본 코드를 import 하거나(예: RELAX_CATALOG·axis_registry), 그래도 식별자가 소스 리터럴로만
있는 경우는(예: REASON_CODE 문자열) `app/services/` 전체를 텍스트로만 읽어 찾는다(AST 실행도
DB 접속도 아니다 — 파일을 열어 문자열을 찾을 뿐이다).
"""

from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path

import pytest
import yaml

from services.cp_sat.rule_card import RULE_CARD
from services.cp_sat.rule_card import NONE_REASONS as CARD_NONE_REASONS
from services.engine_contract.rules import RULE_PREFIXES, RULES
from services.precheck.rule_cause import RULE_CAUSE
from services.precheck.rule_cause import NONE_REASONS as CAUSE_NONE_REASONS
from services.precheck.rule_precheck import RULE_PRECHECK
from services.precheck.rule_precheck import NONE_REASONS as PRECHECK_NONE_REASONS
from services.semantics.engine_rule_family import ENGINE_RULE_FAMILY
from services.semantics.engine_rule_family import NONE_REASONS as ONTOLOGY_NONE_REASONS

APP_ROOT = Path(__file__).resolve().parents[2] / "app"

#: RULES 의 모든 id + RULE_PREFIXES 의 동적 접두 id(fixed_assignment:* · monthly_limit_*).
EXPECTED_IDS: frozenset[str] = frozenset(RULES) | frozenset(spec.id for spec in RULE_PREFIXES.values())

#: (표 이름, dict, NONE_REASONS) — 네 소비자 공통 검증 루프에 쓴다.
_TABLES: dict[str, tuple[dict[str, str], frozenset[str]]] = {
    "ontology(ENGINE_RULE_FAMILY)": (ENGINE_RULE_FAMILY, ONTOLOGY_NONE_REASONS),
    "cause(RULE_CAUSE)": (RULE_CAUSE, CAUSE_NONE_REASONS),
    "card(RULE_CARD)": (RULE_CARD, CARD_NONE_REASONS),
    "precheck(RULE_PRECHECK)": (RULE_PRECHECK, PRECHECK_NONE_REASONS),
}


# ── ① 키 집합 = RULES ∪ 접두 id(빠짐·남는 키 모두 실패) ──────────────────────


@pytest.mark.parametrize("table_name", sorted(_TABLES))
def test_table_keys_equal_rules(table_name: str) -> None:
    table, _ = _TABLES[table_name]
    keys = frozenset(table)
    missing = EXPECTED_IDS - keys
    extra = keys - EXPECTED_IDS
    assert not missing, (
        f"{table_name}: 다음 rule id 에 행이 없다 — rules.py 에 새 RuleSpec/접두를 넣었다면 "
        f"이 표에도 1행(없으면 '없음: 미구현')을 추가하라 — {sorted(missing)}"
    )
    assert not extra, (
        f"{table_name}: 다음 id 는 RULES/RULE_PREFIXES 에 없다(죽은 행 — rules.py 에서 지웠는데 "
        f"이 표에는 남았거나, 오타) — {sorted(extra)}"
    )


# ── ② 값은 비어 있지 않고, '없음:' 이면 그 표의 NONE_REASONS 안 ──────────────


@pytest.mark.parametrize("table_name", sorted(_TABLES))
def test_table_values_nonempty_and_known_none_reason(table_name: str) -> None:
    table, none_reasons = _TABLES[table_name]
    for rule_id, value in table.items():
        assert value, f"{table_name}.{rule_id}: 값이 비어 있다"
        if value.startswith("없음:"):
            assert value in none_reasons, (
                f"{table_name}.{rule_id}: 알 수 없는 '없음: 이유' 어휘 {value!r} — "
                f"이 표의 NONE_REASONS 에 먼저 추가하라(새 어휘를 만드는 것 자체가 설계 판단이다)"
            )


# ── ③ 값이 대상 식별자이면 그 대상이 실제로 있다 ─────────────────────────────


@lru_cache(maxsize=1)
def _services_source_text() -> str:
    """app/services/ 전체를 텍스트로만 읽어 이어붙인다(import 아님 — REASON_CODE·def 이름을
    문자열로만 찾는 용도). DB·LLM·ortools 어느 것도 건드리지 않는다."""
    parts = []
    for path in sorted(APP_ROOT.joinpath("services").rglob("*.py")):
        parts.append(path.read_text(encoding="utf-8"))
    return "\n".join(parts)


@lru_cache(maxsize=1)
def _services_def_names() -> frozenset[str]:
    return frozenset(re.findall(r"^def (\w+)\(", _services_source_text(), flags=re.MULTILINE))


@lru_cache(maxsize=1)
def _services_module_stems() -> frozenset[str]:
    return frozenset(p.stem for p in APP_ROOT.joinpath("services").rglob("*.py"))


def _reason_code_exists(code: str) -> bool:
    """REASON_CODE 문자열이 소스에 있는지. 여러 REASON_CODE 를 한 칸에 '·' 로 이을 때
    조사표가 공통 접두(예: `MONTHLY_LIMIT_`)를 한 번만 적고 뒤 코드는 줄이는 경우가 있어
    (`validate_monthly_limit_row` 행), 정확 일치 외에 '<word 접두>+ code' 꼴 리터럴도
    인정한다 — `MONTHLY_LIMIT_SUM_MAX_BELOW_SUM_MIN` 이 `SUM_MAX_BELOW_SUM_MIN` 을 맞춘다."""
    if f'"{code}"' in _services_source_text():
        return True
    return re.search(r'"\w*' + re.escape(code) + r'"', _services_source_text()) is not None


@lru_cache(maxsize=1)
def _ontology_yaml() -> dict:
    text = APP_ROOT.joinpath("services/semantics/ontology.yaml").read_text(encoding="utf-8")
    return yaml.safe_load(text)


@lru_cache(maxsize=1)
def _ontology_family_ids() -> frozenset[str]:
    return frozenset(_ontology_yaml()["constraints"])


@lru_cache(maxsize=1)
def _ontology_treatment_ids() -> frozenset[str]:
    return frozenset(_ontology_yaml()["treatments"])


@lru_cache(maxsize=1)
def _cause_ids() -> frozenset[str]:
    from services.precheck.cause_inferer import _PATTERN_TO_CAUSE

    return frozenset(_PATTERN_TO_CAUSE.values())


@lru_cache(maxsize=1)
def _relax_catalog_probe_ids() -> frozenset[str]:
    from services.cp_sat.undiagnosed_probe import RELAX_CATALOG

    return frozenset(entry["id"] for entry in RELAX_CATALOG)


@lru_cache(maxsize=1)
def _lever_config_keys() -> frozenset[str]:
    from services.cp_sat.undiagnosed_probe import _LEVER_BY_EVIDENCE_KEY

    return frozenset(config_key for config_key, _title, _tradeoff in _LEVER_BY_EVIDENCE_KEY.values())


@lru_cache(maxsize=1)
def _fix_plan_action_ids() -> frozenset[str]:
    text = APP_ROOT.joinpath("services/precheck/fix_plan.py").read_text(encoding="utf-8")
    return frozenset(re.findall(r'"action_id":\s*"([a-zA-Z_]+)"', text))


@lru_cache(maxsize=1)
def _axis_ids() -> frozenset[str]:
    from services.semantics.axis_registry import get_default_axis_registry

    return frozenset(axis.axis_id for axis in get_default_axis_registry().all_axes())


@lru_cache(maxsize=1)
def _cause_family_tails() -> frozenset[str]:
    """mcs_trace 의 `cause:<tail>` 카드 — family 꼬리(`_FAMILY_KO` 키) + 고정 리터럴
    option_id(banned_release 등, dict 밖 — 소스에서 문자열로만 수집)."""
    from services.ontology_graph.mcs_trace import _FAMILY_KO

    text = APP_ROOT.joinpath("services/ontology_graph/mcs_trace.py").read_text(encoding="utf-8")
    literal = frozenset(re.findall(r'"option_id":\s*"cause:([a-zA-Z_]+)"', text))
    return frozenset(_FAMILY_KO) | literal


def _check_cause_value(value: str) -> str | None:
    return None if value in _cause_ids() else f"cause_inferer._PATTERN_TO_CAUSE 에 없는 cause id {value!r}"


def _check_ontology_value(value: str) -> str | None:
    return None if value in _ontology_family_ids() else f"ontology.yaml constraints: 에 없는 family {value!r}"


def _check_card_token(token: str) -> str | None:
    if token == "personal_night_cap:*":
        return None
    if token.startswith("probe:"):
        pid = token[len("probe:") :]
        return None if pid in _relax_catalog_probe_ids() else f"RELAX_CATALOG 에 없는 probe id {pid!r}"
    if token.startswith("lever:"):
        key = token[len("lever:") :]
        return None if key in _lever_config_keys() else f"_LEVER_BY_EVIDENCE_KEY 에 없는 config_key {key!r}"
    if token.startswith("fix_plan:axis:"):
        axis_id = token[len("fix_plan:axis:") :]
        return None if axis_id in _axis_ids() else f"axis_registry 에 없는 axis_id {axis_id!r}"
    if token.startswith("fix_plan:"):
        action_id = token[len("fix_plan:") :]
        return None if action_id in _fix_plan_action_ids() else f"fix_plan.py 에 없는 action_id {action_id!r}"
    if token.startswith("fix_reason:"):
        code = token[len("fix_reason:") :]
        return None if _reason_code_exists(code) else f"app/services/ 소스에 없는 REASON_CODE {code!r}"
    if token.startswith("cause:"):
        tail = token[len("cause:") :]
        return None if tail in _cause_family_tails() else f"mcs_trace 에 없는 cause family/옵션 {tail!r}"
    if token.startswith("treatment:"):
        return None if token in _ontology_treatment_ids() else f"ontology.yaml treatments: 에 없는 처방 {token!r}"
    return f"알 수 없는 레버 접두 — {token!r}"


_PRECHECK_SEGMENT_RE = re.compile(r"^(?P<funcs>[^:\[\]]+):(?P<reasons>[^:\[\]]+?)(?:\[(?P<meta>[^\]]*)\])?$")


def _check_precheck_segment(segment: str) -> str | None:
    m = _PRECHECK_SEGMENT_RE.match(segment.strip())
    if not m:
        return f"'함수명:REASON_CODE[시점·효과]' 형식이 아니다 — {segment!r}"
    def_names = _services_def_names()
    mod_stems = _services_module_stems()
    for fn in m.group("funcs").split("·"):
        fn = fn.strip()
        if fn not in def_names and fn not in mod_stems:
            return f"app/services/ 에 정의가 없는 함수/모듈 {fn!r}"
    for code in m.group("reasons").split("·"):
        code = code.strip()
        if not _reason_code_exists(code):
            return f"app/services/ 소스에 없는 REASON_CODE {code!r}"
    return None


def test_ontology_values_reference_existing_family() -> None:
    for rule_id, value in ENGINE_RULE_FAMILY.items():
        if value.startswith("없음:"):
            continue
        reason = _check_ontology_value(value)
        assert reason is None, f"ENGINE_RULE_FAMILY.{rule_id} = {value!r}: {reason}"


def test_cause_values_reference_existing_cause_ids() -> None:
    for rule_id, value in RULE_CAUSE.items():
        if value.startswith("없음:"):
            continue
        reason = _check_cause_value(value)
        assert reason is None, f"RULE_CAUSE.{rule_id} = {value!r}: {reason}"


def test_card_values_reference_existing_levers() -> None:
    for rule_id, value in RULE_CARD.items():
        if value.startswith("없음:"):
            continue
        for token in value.split(" · "):
            reason = _check_card_token(token.strip())
            assert reason is None, f"RULE_CARD.{rule_id} 토큰 {token!r}: {reason}"


def test_precheck_values_reference_existing_checks() -> None:
    for rule_id, value in RULE_PRECHECK.items():
        if value.startswith("없음:"):
            continue
        for segment in value.split(" | "):
            reason = _check_precheck_segment(segment)
            assert reason is None, f"RULE_PRECHECK.{rule_id} 세그먼트 {segment!r}: {reason}"


# ── ④ 하드락 규칙 행의 토큰이 하드락을 완화하는 레버와 겹치면 결함으로 표시돼야 한다(EI-11 토큰 술어) ──
#
# EI-11 뒤에는 하드락 행에도 정당한(상한 안) 레버가 남는다(예: probe:raise_max_consec_work — 연속근무 상한을 5 까지만
# 올린다). 그래서 '하드락 행 = 없음: 하드락' 이 아니라 **토큰 단위**로 본다 — 하드락 규칙 행(`hard_lock_rule_nos` 직접 ·
# `hard_lock_indirect` 간접, RULE_PREFIXES 포함)의 토큰 ∩ {금지 처방 · BLOCK 원인 가족 · 막힌 축 · BLOCK 탐색 항목} = ∅.
# 판정은 카드 필터(`policy.card_filter`)와 같은 함수로 짓는다(레지스트리 파생 — 숫자를 여기 다시 적지 않는다).

#: 하드락 규칙 행 가운데 아직 '더러운' 토큰(`_dirty_card_tokens`)이 남은 id. 이 집합이 **늘면** 새 결함이라 실패한다.
#: 11c 에서 fix_plan 상한형 축(월간 N 한도 '+1')에 상한 문구('(최대 15 — 하드락 #6)')가 붙어 마지막 `max_night` 가 깨끗해졌다
#: — 그래서 비어 있다. 역방향 단언(`test_known_hardlock_card_gaps_equal_the_dirty_rows`)이 이 집합 == 실제 공백인지도 본다.
KNOWN_HARDLOCK_CARD_GAPS: frozenset[str] = frozenset()


def _probe_samples() -> tuple[dict, ...]:
    """BLOCK 탐색 항목 판정 표본 — C5(`test_card_hard_lock_contract.SAMPLES`)의 B1~B4·0 과 같은 규칙(레지스트리에서 짓는다)."""
    from services.engine_contract.settings import SETTINGS, lookup
    from services.policy.card_filter import strict_base

    def _keys(canonical: str) -> tuple[str, ...]:
        spec = lookup(canonical)
        return (spec.key, *spec.aliases)

    below = {**strict_base(), **dict.fromkeys(_keys("max_conseq_work"), 4),
             **dict.fromkeys(_keys("max_nig_per_month"), 10), "three_seq_nig": False}
    defaults = {s.key: (s.current_default == "True" if s.current_default in ("True", "False") else int(s.current_default))
                for s in SETTINGS if s.hard_lock is not None}
    return strict_base(), below, defaults, {}, {**strict_base(), "max_conseq_work": 0}


def _blocking_probe_tokens(samples: tuple[dict, ...]) -> set[str]:
    from services.cp_sat.undiagnosed_probe import RELAX_CATALOG
    from services.policy.card_filter import catalog_item_violations
    from services.policy.hard_lock import BLOCK

    return {f"probe:{item['id']}" for item in RELAX_CATALOG
            if any(v.kind == BLOCK for base in samples for v in catalog_item_violations(item, base))}


def _fix_plan_axis_message(axis_id: str) -> str | None:
    """그 축 하나만 걸리게(검증기 실패 셀의 `blocking_axes`) 지은 fix_plan 의 안내 문구 — 안내(axis_actions)에 없으면 None."""
    from services.precheck.fix_plan import build_fix_plan

    cells = [{"blocking_axes": [axis_id]}]
    plan = build_fix_plan(structural_diagnosis={}, preflight_issues=[], conflict_cores=[], pool_snapshot={},
                          violated_constraints=[{"reason_code": "A2_PROBE",
                                                 "details": {"validator_evidence": {"top_failed_cells": cells}}}])
    return next((a["human_message_ko"] for a in plan["axis_actions"] if a["axis_id"] == axis_id), None)


def _stuck_axis_tokens() -> set[str]:
    """막힌 축 — family 가 `blocked` 인 축 + `capped` 인데 fix_plan 안내(`_build_axis_action`)에 상한 문구
    (`card_filter.mode_suffix` — '(최대 N — 하드락 #n)')가 안 붙은 축(11c 규칙). 안내에 안 나오는 축(보호)은 깨끗하다."""
    from services.policy import card_filter as cf
    from services.semantics.axis_registry import get_default_axis_registry

    modes = cf.family_modes()
    stuck: set[str] = set()
    for axis in get_default_axis_registry().all_axes():
        entry = modes.get(axis.family) or {}
        if entry.get("mode") == "blocked":
            stuck.add(f"fix_plan:axis:{axis.axis_id}")
        elif entry.get("mode") == "capped":
            message = _fix_plan_axis_message(axis.axis_id)
            if message is not None and not message.endswith(cf.mode_suffix(entry)):
                stuck.add(f"fix_plan:axis:{axis.axis_id}")
    return stuck


def _dirty_card_tokens() -> frozenset[str]:
    """하드락을 완화하는 레버 토큰 — 금지 처방 · BLOCK 원인 가족 · 막힌 축 · BLOCK 탐색 항목(EI-11 §2.2 A2).

    막힌 축은 family 가 `blocked` 인 축과, `capped` 인데 fix_plan 안내에 상한 문구가 안 붙은 축이다(`_stuck_axis_tokens`)."""
    from services.ontology_graph.mcs_trace import FAMILY_PRESENTATION
    from services.policy import card_filter as cf

    dirty = set(cf.forbidden_treatments())  # 처방 id 가 그대로 토큰(`treatment:<id>`)이다
    for tail in _cause_family_tails():
        bucket = (FAMILY_PRESENTATION.get(tail) or {}).get("bucket")
        if cf.cause_family_effect(tail, bucket)[0]:
            dirty.add(f"cause:{tail}")
    dirty |= _stuck_axis_tokens()
    dirty |= _blocking_probe_tokens(_probe_samples())
    return frozenset(dirty)


def _hardlock_rule_ids() -> list[str]:
    all_specs = dict(RULES)
    all_specs.update({spec.id: spec for spec in RULE_PREFIXES.values()})
    return [rule_id for rule_id, spec in all_specs.items() if spec.hard_lock_rule_nos or spec.hard_lock_indirect]


def test_dirty_token_predicate_is_not_vacuous() -> None:
    """술어가 실제로 하드락 완화 레버를 집어낸다(빈 술어로 통과하는 회귀 방지) — 금지 처방·원인 가족·막힌 축 표본.
    상한형 축(월간 N 한도·연속근무)은 fix_plan 안내에 상한 문구가 붙어 깨끗하다(11c)."""
    dirty = _dirty_card_tokens()
    expected = {"treatment:disable:night_recovery", "treatment:disable:transition_ban", "cause:night_recovery",
                "cause:transition", "cause:consecutive", "fix_plan:axis:carryover_transition"}
    assert expected <= dirty, sorted(expected - dirty)
    clean = {"probe:raise_max_consec_work", "lever:max_conseq_work", "fix_plan:axis:monthly_n_cap",
             "fix_plan:axis:consecutive_work_limit", "fix_plan:axis:carryover_consecutive_work"}
    assert not clean & dirty, sorted(clean & dirty)


def test_capped_axis_rule_catches_an_axis_message_without_the_cap(monkeypatch) -> None:
    """capped 축 규칙이 헛돌지 않는다 — fix_plan 이 상한 문구를 안 붙이면(11c 전 동작으로 되돌림) 상한형 축이 더러워지고,
    `max_night` 행(`fix_plan:axis:monthly_n_cap`)이 공백으로 잡힌다."""
    from services.precheck import fix_plan

    monkeypatch.setattr(fix_plan, "_split_hard_lock_axes", lambda t2_axes: (t2_axes, [], {}))
    stuck = _stuck_axis_tokens()
    assert {"fix_plan:axis:monthly_n_cap", "fix_plan:axis:consecutive_work_limit",
            "fix_plan:axis:carryover_consecutive_work"} <= stuck
    assert "max_night" in _hardlock_card_gaps()


def _hardlock_card_gaps() -> dict[str, list[str]]:
    """하드락 규칙 행(직접·간접 — RULE_PREFIXES 포함) 가운데 더러운 토큰을 가진 행 → 그 토큰."""
    dirty = _dirty_card_tokens()
    gaps = {}
    for rule_id in _hardlock_rule_ids():
        value = RULE_CARD.get(rule_id) or ""
        hits = sorted({token.strip() for token in value.split(" · ")} & dirty) if not value.startswith("없음:") else []
        if hits:
            gaps[rule_id] = hits
    return gaps


def test_known_hardlock_card_gaps_equal_the_dirty_rows() -> None:
    """역방향 — 하드락 행 중 토큰이 더러운 행 == `KNOWN_HARDLOCK_CARD_GAPS`(11c 뒤 ∅). 공백을 고치고도 집합을 안 비우면
    (헛 허용) 여기서 걸린다."""
    assert set(_hardlock_card_gaps()) == set(KNOWN_HARDLOCK_CARD_GAPS)


def test_hardlock_rule_card_tokens_that_relax_hard_locks_are_known_gaps() -> None:
    """하드락 규칙 행의 토큰 ∩ 더러운 토큰 = ∅ 이어야 한다 — 겹치는 행 ⊆ `KNOWN_HARDLOCK_CARD_GAPS`(`<=`).

    ★(2026-10-14 반박 검토) 범위는 직접(`hard_lock_rule_nos`)과 간접(`hard_lock_indirect`) 둘 다다 — RULE_PREFIXES
    (fixed_assignment:* 등)도 같은 필드를 가지므로 함께 본다."""
    gaps = _hardlock_card_gaps()
    new_gaps = {rule_id: hits for rule_id, hits in gaps.items() if rule_id not in KNOWN_HARDLOCK_CARD_GAPS}
    assert not new_gaps, (
        "하드락 규칙 행에 하드락을 완화하는 레버 토큰이 새로 생겼다(카드 필터가 거르는 레버를 표가 다시 적었거나, 새 레버가 "
        "하드락을 푼다) — 레버를 없애거나 상한 안으로 자르고, 남는 레버가 없으면 값을 '없음: 하드락' 으로 고쳐라 — "
        f"{new_gaps}"
    )
