"""카드·처방 하드락 필터 — EI-11 §2.0(`.omc/aide_docs/v2/engine_input/ei11/EI11_SPEC_v2.md`).

해결 카드(`resolution_options`·`_sole_option`)와 온톨로지 처방이 CLAUDE.md 하드락 8종을 완화하는지
`policy.hard_lock` 판정 하나로 본다(P-1 — 카드·처방 필터, 생성 진입 관문, REST 가드가 같은 판정을 쓴다).
숫자(5·15·3)는 다시 적지 않는다 — 상한은 레지스트리(`engine_contract.settings`)에서 읽는다(R3-06).

모듈 규칙(§2.0.1)
  - 머리 import 는 `services.policy.hard_lock`·`services.engine_contract.settings`(+표준 `logging`)뿐이다.
    온톨로지·`treatment_applicator`·control·mcs 는 함수 안에서 지연 import 한다(순환 import 회피 ·
    `control → card_filter → control`).
  - env 를 읽지 않는다. `lru_cache` 를 쓰지 않는다 — 레지스트리 monkeypatch·사용자 온톨로지에서 낡은
    결과가 나오지 않게 호출마다 다시 계산한다(온톨로지 적재는 `get_default_ontology` 가 이미 캐시한다).
  - 기록은 모듈 logger 로만 남긴다(print 0). 정상 경로(처방 적용 예외 → 금지 아님)는 DEBUG 이고,
    WARNING 이상은 막는 쪽(fail-closed)이 실제로 걸릴 때만 쓴다 — 주 골든의 `logs`·`swallowed_failures`
    를 지킨다.

실패 계약(§2.0.6) — `finalize_infeasibility`·`filter_options`·`option_violations` 는 예외를 밖으로 내지
않는다. 판정이 실패하면 막는 쪽이다(카드 한 장 실패 → 그 카드 제거 · 전역 실패 → 설정을 바꾸는 카드와
완화 권고 카드를 모두 제거). 생성 진입 관문(`assert_treatments_allowed` — 11a-2)은 400(`HTTPException`)만 내고,
판정이 실패하면 하드락 family 처방을 막는다(④).

카드가 아닌 출구 칸의 '정책상 금지' 표지(11c — §2.5)는 `family_modes`(family 단위 판정)·`capped_suffix`·
`mode_suffix`(상한 문구)·`policy_forbidden_for_causes`(원인을 덮는 금지 처방 기록)로 짓고, payload 빌더 후처리는
`mark_treatment_views`(처방 rationale 상한 문구·problem_list 표지)를 부른다. 표지를 다는 곳은 생산자 쪽
(`precheck.payload`·`precheck.fix_plan`)이다.
"""

from __future__ import annotations

import logging

from services.engine_contract import settings as _settings
from services.engine_contract.settings import ADJACENT, AFFECTS, HardLock
from services.policy.hard_lock import (
    BLOCK,
    Violation,
    override_violations,
    release_violations,
    rule_cap,
    rule_cap_unit,
    rule_label,
)

logger = logging.getLogger(__name__)

#: 판정이 실패했을 때만 쓰는 고정 하드락 family 집합(§2.0.3). 정상 판정은 레지스트리에서 파생하고,
#: C5 가 "파생 결과 ⊆ 이 집합"을 단언해 둘의 어긋남을 잡는다.
HARD_LOCK_FAMILIES_FALLBACK: frozenset[str] = frozenset({
    "BoundaryTransitionBan", "NightRecovery", "ConsecutiveWorkLimit", "ConsecutiveNightLimit", "MonthlyNightCap",
})

#: `family_action_policy` 표본·scope·목표값(§2.0.3). 구조 키는 처방이 고칠 대상이 있게 채운 값이다.
_POLICY_SCOPE = {"team": "T1", "shift": "D", "day": 1}
_POLICY_THRESHOLD_TARGETS = (0, 10**6)
_APPLIED_KEY = "_applied_constraint_adjustments"
_POLICY_DIFF_EXCLUDE = frozenset({
    _APPLIED_KEY, "daily_shift_requirements", "daily_shift_requirements_by_day", "team_min_by_team", "grade_config",
})

#: mcs 원인 가족 중 `FAMILY_CONFIG_RELAX` 에 효과가 없는 둘의 보충 효과(§2.0.3).
_CAUSE_FAMILY_EXTRA_EFFECT = {
    "cross_month": {"cross_month_hard_rules_enable": False},
    "preceptee": {"preceptee_on": False},
}

#: 카드가 설정을 바꾸는 통로 — 전역 판정 실패 때 이 중 하나라도 비지 않은 카드는 뺀다(§2.0.6 ②).
_CONFIG_CHANNELS = ("apply", "treatment_ids", "monthly_limit_release", "weekend_off_release",
                    "banned_wanted_release", "allowed_shift_add")

_FILTER_ERROR_MSG = "하드락 판정 중 오류가 나 안전하게 뺐습니다."
#: `apply` 통로 relax_mark 문구 끝에 붙인다 — 모달에서 시작한 생성은 카드 delta 를 프리셋에 저장한다(Q18).
_PRESET_NOTE = " 이 조정은 설정 프리셋에 저장됩니다(모달에서 시작한 생성)."


# ── 표본·delta 판정 ─────────────────────────────────────────────────────────


def _registry_default(raw: object) -> tuple[bool, object]:
    """레지스트리 `current_default`(대개 문자열) → (읽힘 여부, bool·int 값)."""
    if isinstance(raw, int):  # bool 포함
        return True, raw
    if raw in ("True", "False"):
        return True, raw == "True"
    try:
        return True, int(str(raw))
    except (TypeError, ValueError):
        return False, None


def _put_hard_lock_sample(base: dict, spec, lock: HardLock) -> None:
    if lock.kind == "int_cap":
        for key in (spec.key, *spec.aliases):
            base[key] = int(lock.cap)
    elif lock.kind in ("bool_on", "derived_cap"):
        base[spec.key] = True
    elif lock.kind == "boundary_switch":
        ok, value = _registry_default(spec.current_default)
        if not ok:
            raise ValueError(f"월경계 스위치 {spec.key} 의 current_default 를 읽을 수 없습니다: {spec.current_default!r}")
        base[spec.key] = value


def strict_base() -> dict:
    """하드락 8종이 전부 켜지고 정수는 상한인 표본 — 레지스트리에서 매번 새 dict 로 짓는다(§2.0.3).

    int_cap → 정본 키·별칭 모두 cap · bool_on·derived_cap → 정본 키 True · boundary_switch →
    current_default · 인접·영향 키는 current_default 가 bool·int 로 읽힐 때만 그 값."""
    base: dict = {}
    for spec in _settings.SETTINGS:
        lock = spec.hard_lock
        if isinstance(lock, HardLock):
            _put_hard_lock_sample(base, spec, lock)
        elif lock in (ADJACENT, AFFECTS):
            ok, value = _registry_default(spec.current_default)
            if ok:
                base[spec.key] = value
    return base


def delta_violations(delta: dict | None) -> list:
    """설정 delta(정본 키·별칭 → 새 값)의 하드락 판정 — `override_violations` 그대로(P-1)."""
    return override_violations(dict(delta or {}))


def _config_diff(before: dict, after: dict, exclude) -> dict:
    keys = dict.fromkeys([*before, *after])
    return {k: after.get(k) for k in keys if k not in exclude and before.get(k) != after.get(k)}


def _positive_rule_nos(rule_nos) -> list[int]:
    return sorted({int(n) for n in rule_nos if n})


def _rule_nos_of(violations) -> list[int]:
    return _positive_rule_nos(n for v in violations for n in v.rule_nos)


def _mark_items(violations) -> list[dict]:
    return [{"key": v.key, "message_ko": v.message_ko} for v in violations]


def _rules_text(rule_nos) -> str:
    return "·".join(rule_label(n) for n in rule_nos) or "하드락"


def catalog_item_violations(item: dict, base: dict | None) -> list:
    """탐색 카탈로그 항목 하나 — `apply(base)` delta 와 탐색 극값(`search.hi`·`search.lo`)의 판정(C5 ①·A2 ④)."""
    out = list(delta_violations(item["apply"](dict(base or {}))))
    search = item.get("search") or {}
    key = search.get("key")
    for bound in ("hi", "lo"):
        if key and search.get(bound) is not None:
            out.extend(delta_violations({key: search[bound]}))
    return out


# ── control 매트릭스 판정 ───────────────────────────────────────────────────


def _policy_base() -> dict:
    base = strict_base()
    base.update({
        "daily_shift_requirements": {"D": 5, "E": 3, "N": 2},
        "team_min_by_team": {"T1": {"D": 1}},
        "grade_config": {"constraints": {"D": {"1": 1}}, "allow_soft_fallback": False},
    })
    return base


def _adjustment_violations(family: str, action: str, target, base: dict, ontology) -> list:
    from services.constraint_impact.control import apply_adjustments_to_config

    adj = {"family": family, "action": action, "target_value": target, "scope_filter": dict(_POLICY_SCOPE)}
    after = apply_adjustments_to_config(base, [adj], ontology=ontology)
    found = delta_violations(_config_diff(base, after, _POLICY_DIFF_EXCLUDE))
    record = (after.get(_APPLIED_KEY) or [{}])[-1]  # 설정을 안 쓰고 '적용 불가'만 기록한 조정(런타임 금지 설정 — D13)
    seen = {v.key for v in found}
    return found + [Violation(b["rule_nos"][0], b["key"], None, "runtime_key_forbidden", b["message_ko"], kind=BLOCK,
                              rule_nos=tuple(b["rule_nos"]))
                    for b in record.get("blocked_by_hard_lock") or () if b["key"] not in seen]


def _policy_mode(verdicts: list[list]) -> dict:
    """목표값별 판정 → 모드. 큰 값만 BLOCK 이면 capped, 작은 값만 BLOCK(정의 밖)은 막는 쪽(blocked)."""
    blocks = [[v for v in vs if v.kind == BLOCK] for vs in verdicts]
    big = blocks[-1]
    if all(blocks) or (any(blocks) and not big):
        return {"mode": "blocked", "rule_nos": _rule_nos_of(v for b in blocks for v in b)}
    if big:
        return {"mode": "capped", "key": big[0].key, "cap": rule_cap(big[0].rule_no), "rule_nos": _rule_nos_of(big)}
    marks = [v for vs in verdicts for v in vs if v.kind != BLOCK]
    if marks:
        return {"mode": "marked", "rule_nos": [], "marks": _mark_items(marks)}
    return {"mode": "ok", "rule_nos": []}


def _action_policy(family: str, action: str, base: dict, ontology) -> dict:
    targets = _POLICY_THRESHOLD_TARGETS if action == "set_threshold" else (None,)
    try:
        verdicts = [_adjustment_violations(family, action, t, base, ontology) for t in targets]
    except Exception:
        logger.warning("하드락 판정: control %s/%s 적용 예외 — 막는 쪽(blocked)으로 봅니다.", family, action,
                       exc_info=True)
        return {"mode": "blocked", "rule_nos": [], "error": True}
    return _policy_mode(verdicts)


def family_action_policy(ontology=None) -> dict[tuple[str, str], dict]:
    """control `_FAMILY_ACTION_MATRIX` 의 (family, action) 마다 하드락 판정 dict(§2.0.3).

    모드 — blocked(`rule_nos`) · capped(`key`·`cap`·`rule_nos`) · marked(`marks`) · ok. 표본은
    `strict_base()` + 구조 키 3개, 목표값은 set_threshold 면 0 과 10⁶. 적용 예외는 blocked + `error`."""
    from services.constraint_impact.control import _FAMILY_ACTION_MATRIX

    base = _policy_base()
    return {(family, action): _action_policy(family, action, base, ontology)
            for family in sorted(_FAMILY_ACTION_MATRIX) for action in sorted(_FAMILY_ACTION_MATRIX[family])}


# ── 온톨로지 처방 판정 ───────────────────────────────────────────────────────


def _apply_error_rule_nos(onto, tid: str, exc: Exception, policy_box: list) -> tuple[int, ...] | None:
    """처방 적용 예외 — target family 정책이 blocked·capped 면 금지(fail-closed), 그 밖이면 금지 아님(None)."""
    from services.treatment_applicator import _ACTION_MAP

    t = onto.get_treatment(tid)
    family = onto.resolve_alias(t.target_family) or t.target_family
    action = _ACTION_MAP.get(t.action_type)
    if not policy_box:
        policy_box.append(family_action_policy())  # 기본 온톨로지 — 호출 안 한 번만(캐시 아님)
    entry = policy_box[0].get((family, action)) or {}
    if entry.get("mode") in ("blocked", "capped"):
        logger.warning("처방 %s 적용 예외 — %s/%s 가 하드락(%s)이라 금지로 봅니다(fail-closed).",
                       tid, family, action, entry.get("mode"), exc_info=True)
        return tuple(entry.get("rule_nos") or ())
    logger.debug("처방 %s 적용 예외(%s: %s) — %s/%s 는 하드락 family 가 아니라 금지 아님.",
                 tid, type(exc).__name__, exc, family, action)
    return None


def _treatment_verdicts(ontology=None) -> tuple[dict[str, tuple[int, ...]], dict[str, list[dict]],
                                                dict[str, list[str]]]:
    """처방마다 엄격 표본에 적용한 diff 판정 → (금지 `{tid: rule_nos}`, 표시 `{tid: [{key, message_ko}]}`,
    금지 키 `{tid: [BLOCK 키]}` — 적용 예외로 금지한 처방은 키를 모른다(빈 목록))."""
    from services.semantics.ontology import get_default_ontology
    from services.treatment_applicator import apply_treatments

    onto = ontology if ontology is not None else get_default_ontology()
    base = strict_base()
    forbidden: dict[str, tuple[int, ...]] = {}
    marked: dict[str, list[dict]] = {}
    block_keys: dict[str, list[str]] = {}
    policy_box: list = []
    for tid in sorted(onto.treatments):
        try:
            res = apply_treatments([tid], base, ontology=onto)
        except Exception as exc:
            nos = _apply_error_rule_nos(onto, tid, exc, policy_box)
            if nos is not None:
                forbidden[tid], block_keys[tid] = nos, []
            continue
        vs = delta_violations(_config_diff(base, res.patched_config, (_APPLIED_KEY,)))
        blocks = [v for v in vs if v.kind == BLOCK]
        if blocks:
            forbidden[tid] = tuple(_rule_nos_of(blocks))
            block_keys[tid] = list(dict.fromkeys(str(v.key) for v in blocks))
        elif vs:
            marked[tid] = _mark_items(vs)
    return forbidden, marked, block_keys


def _treatment_maps(ontology=None) -> tuple[dict[str, tuple[int, ...]], dict[str, list[dict]]]:
    """처방마다 엄격 표본에 적용한 diff 판정 → (금지 `{tid: rule_nos}`, 표시 `{tid: [{key, message_ko}]}`)."""
    forbidden, marked, _block_keys = _treatment_verdicts(ontology)
    return forbidden, marked


def forbidden_treatments(ontology=None) -> dict[str, tuple[int, ...]]:
    """엄격 표본에 적용하면 하드락을 BLOCK 하는 처방 `{tid: rule_nos}`(§2.0.3)."""
    return _treatment_maps(ontology)[0]


def marked_treatments(ontology=None) -> dict[str, list[dict]]:
    """같은 계산에서 diff 가 relax_mark 만인 처방 `{tid: [{key, message_ko}]}`(§2.0.3)."""
    return _treatment_maps(ontology)[1]


def hard_lock_family_treatment_ids(onto=None) -> frozenset[str]:
    """`HARD_LOCK_FAMILIES_FALLBACK` family 를 겨냥하는 처방 id — 판정이 실패했을 때만 쓴다."""
    if onto is None:
        from services.semantics.ontology import get_default_ontology

        onto = get_default_ontology()
    return frozenset(tid for tid, t in onto.treatments.items()
                     if (onto.resolve_alias(t.target_family) or t.target_family) in HARD_LOCK_FAMILIES_FALLBACK)


# ── 카드 한 장 판정 ──────────────────────────────────────────────────────────


def _violation_item(v, channel: str) -> dict:
    return {"rule_nos": _positive_rule_nos(v.rule_nos), "key": v.key, "message_ko": v.message_ko, "channel": channel}


def _consecutive_family_message(v) -> str:
    return (f"연속근무 상한을 {rule_cap(v.rule_no)}일 넘게 올리는 완화는 정책상 금지(#{v.rule_no})"
            " — 상한 안 조정은 다른 카드")


def cause_family_effect(family: str, bucket: str | None) -> tuple[list[dict], list[dict]]:
    """mcs 원인 가족 카드의 하드락 효과 `(blocks, marks)`(§2.0.3).

    효과 = `FAMILY_CONFIG_RELAX[family]` 또는 보충 2개. tradeoff 는 BLOCK→blocks·relax_mark→marks,
    action 은 relax_mark 만 marks(BLOCK 은 진단용 일괄 값 — `night_cap` 40 오탐이라 버린다), 그 밖은 빈 결과."""
    from services.ontology_graph.mcs_trace import FAMILY_CONFIG_RELAX

    effect = FAMILY_CONFIG_RELAX[family] if family in FAMILY_CONFIG_RELAX else _CAUSE_FAMILY_EXTRA_EFFECT.get(family)
    if not effect or bucket not in ("tradeoff", "action"):
        return [], []
    blocks: list[dict] = []
    marks: list[dict] = []
    for v in delta_violations(effect):
        item = _violation_item(v, "cause_family")
        if v.kind != BLOCK:
            marks.append(item)
        elif bucket == "tradeoff":
            if family == "consecutive":
                item["message_ko"] = _consecutive_family_message(v)
            blocks.append(item)
    return blocks, marks


def _apply_channel(apply, blocks: list, marks: list) -> None:
    if not apply:
        return
    if not isinstance(apply, dict):
        raise TypeError(f"카드 apply 가 dict 가 아닙니다: {type(apply).__name__}")
    for v in delta_violations(apply):
        item = _violation_item(v, "apply")
        if v.kind == BLOCK:
            blocks.append(item)
        else:
            item["message_ko"] += _PRESET_NOTE
            marks.append(item)


def _treatment_channel(treatment_ids, forbidden, marked, blocks: list, marks: list) -> None:
    if not treatment_ids:
        return
    if not isinstance(treatment_ids, (list, tuple)):
        raise TypeError(f"카드 treatment_ids 가 목록이 아닙니다: {type(treatment_ids).__name__}")
    if forbidden is None or marked is None:
        maps = _treatment_maps()
        forbidden = maps[0] if forbidden is None else forbidden
        marked = maps[1] if marked is None else marked
    for tid in treatment_ids:
        if tid in forbidden:
            nos = _positive_rule_nos(forbidden[tid])
            blocks.append({"rule_nos": nos, "key": tid, "channel": "treatment_ids",
                           "message_ko": f"처방 {tid} 는 {_rules_text(nos)} 규칙을 완화합니다."})
        for m in marked.get(tid) or ():
            marks.append({"rule_nos": [], "key": m["key"], "message_ko": m["message_ko"], "channel": "treatment_ids"})


def _option_violations(option: dict, forbidden, marked) -> tuple[list[dict], list[dict]]:
    blocks: list[dict] = []
    marks: list[dict] = []
    _apply_channel(option.get("apply"), blocks, marks)
    _treatment_channel(option.get("treatment_ids"), forbidden, marked, blocks, marks)
    blocks += [_violation_item(v, "monthly_limit_release") for v in release_violations(option.get("monthly_limit_release"))]
    if option.get("source") == "cause" and option.get("family"):
        fam_blocks, fam_marks = cause_family_effect(option["family"], option.get("bucket"))
        blocks += fam_blocks
        marks += fam_marks
    return blocks, marks


def _option_id(option: dict):
    oid = option.get("option_id")
    return oid if oid is not None else option.get("id")


def _error_item() -> dict:
    return {"rule_nos": [], "key": None, "message_ko": _FILTER_ERROR_MSG, "channel": "error", "filter_error": True}


def option_violations(option, *, forbidden=None, marked=None) -> tuple[list[dict], list[dict]]:
    """카드 한 장의 효과 통로를 모두 보고 `(blocks, marks)` — 항목 `{rule_nos, key, message_ko, channel}`(§2.0.3).

    ① `apply` → `override_violations` · ② `treatment_ids` → 금지·표시 처방 · ③ `monthly_limit_release` →
    `hard_lock.release_violations`(가드 ①과 같은 함수) · ④ `source=="cause"` + `family` → `cause_family_effect`.
    `weekend_off_release`·`allowed_shift_add`·`banned_wanted_release` 는 판정 미구현(EI-16). 예외를 내지 않는다 —
    판정이 실패하면 BLOCK(`filter_error`)으로 본다(§2.0.6 ①)."""
    if not isinstance(option, dict):
        return [], []
    try:
        return _option_violations(option, forbidden, marked)
    except Exception:
        logger.warning("하드락 카드 판정 중 예외 — 이 카드를 뺍니다(id=%r).", option.get("option_id", option.get("id")),
                       exc_info=True)
        return [_error_item()], []


# ── 카드 목록 거르기 ─────────────────────────────────────────────────────────


def _card_kind(option: dict) -> str:
    return "cause_card" if option.get("source") == "cause" else "card"


def _card_message(option: dict, blocks: list[dict], rule_nos: list[int]) -> str:
    if option.get("family") == "consecutive":
        special = [b["message_ko"] for b in blocks if b.get("channel") == "cause_family"]
        if special:
            return special[0]
    return f"하드락 규칙({_rules_text(rule_nos)})을 완화하는 해결책이라 내보내지 않았습니다."


def _filter_error_entry(option: dict) -> dict:
    return {"kind": _card_kind(option), "id": _option_id(option), "rule_nos": [], "keys": [],
            "message_ko": _FILTER_ERROR_MSG, "filter_error": True}


def _policy_item(option: dict, blocks: list[dict]) -> dict:
    """BLOCK 카드 → `policy_forbidden` 항목(§2.0.5)."""
    if any(b.get("filter_error") for b in blocks):
        return _filter_error_entry(option)
    nos = _positive_rule_nos(n for b in blocks for n in b["rule_nos"])
    return {"kind": _card_kind(option), "id": _option_id(option), "rule_nos": nos,
            "keys": list(dict.fromkeys(str(b["key"]) for b in blocks if b.get("key") is not None)),
            "message_ko": _card_message(option, blocks, nos)}


def _set_relax_marks(option: dict, marks: list[dict]) -> None:
    """relax_mark 표지를 매 호출 다시 계산해 덮어쓴다(append 금지 — 멱등). 표지가 없으면 키를 더하지 않는다."""
    if marks:
        option["hard_lock_relax_marks"] = [{"key": m["key"], "message_ko": m["message_ko"]} for m in marks]
    else:
        option.pop("hard_lock_relax_marks", None)


def _changes_settings_or_relaxes(option: dict) -> bool:
    return (any(option.get(ch) for ch in _CONFIG_CHANNELS)
            or (option.get("source") == "cause" and option.get("bucket") == "tradeoff"))


def _fail_closed_options(options: list) -> tuple[list, list[dict]]:
    """전역 판정 실패(§2.0.6 ②) — 설정을 바꾸는 카드·완화 권고 카드를 모두 빼고 수동·안내 카드만 남긴다."""
    kept: list = []
    blocked: list[dict] = []
    for opt in options:
        if isinstance(opt, dict) and _changes_settings_or_relaxes(opt):
            blocked.append(_filter_error_entry(opt))
        else:
            kept.append(opt)
    return kept, blocked


def _resolve_maps(options: list, forbidden, marked) -> tuple[dict, dict]:
    """처방 지도는 `treatment_ids` 가 있는 카드가 있을 때만, 한 번 짓는다(§2.0.4)."""
    if forbidden is not None and marked is not None:
        return forbidden, marked
    if not any(isinstance(o, dict) and o.get("treatment_ids") for o in options):
        return forbidden or {}, marked or {}
    maps = _treatment_maps()
    return (maps[0] if forbidden is None else forbidden), (maps[1] if marked is None else marked)


def _judge_options(options: list, forbidden: dict, marked: dict) -> tuple[list, list[dict]]:
    kept: list = []
    blocked: list[dict] = []
    for opt in options:
        if not isinstance(opt, dict):
            kept.append(opt)
            continue
        blocks, marks = option_violations(opt, forbidden=forbidden, marked=marked)
        if blocks:
            blocked.append(_policy_item(opt, blocks))
            continue
        _set_relax_marks(opt, marks)
        kept.append(opt)
    return kept, blocked


def filter_options(options, *, forbidden=None, marked=None) -> tuple[list, list[dict]]:
    """BLOCK 카드를 지우고 `(kept, blocked)` 를 돌려준다(§2.0.3). `blocked` 는 `policy_forbidden` 항목 모양이다.

    relax_mark 만 있는 카드는 `hard_lock_relax_marks` 를 덮어쓰고, 효과 통로가 없는 카드는 키 추가 0·순서
    유지다. id 는 `option_id` 또는 `id`(합성 카드 허용). dict 가 아닌 항목은 그대로 둔다. 예외를 내지 않는다."""
    try:
        opts = list(options or [])
    except TypeError:
        logger.warning("하드락 카드 목록을 읽을 수 없어 모두 뺍니다(fail-closed).", exc_info=True)
        return [], [_filter_error_entry({})]
    try:
        forbidden, marked = _resolve_maps(opts, forbidden, marked)
        return _judge_options(opts, forbidden, marked)
    except Exception:
        logger.warning("하드락 판정 기준을 지을 수 없어 설정을 바꾸는 카드를 모두 뺍니다(fail-closed).", exc_info=True)
        return _fail_closed_options(opts)


# ── 싱크 ─────────────────────────────────────────────────────────────────────


def _hashable(value):
    try:
        hash(value)
    except TypeError:
        return repr(value)
    return value


def _item_key(item) -> tuple:
    if isinstance(item, dict):
        return item.get("kind"), _hashable(item.get("id"))
    return "", repr(item)


def _drop_sole_option(inf: dict) -> None:
    """'유일 옵션'이 다 걸렸다 — 키를 지우고 `resolution_options` 를 비운다(Q15)."""
    inf.pop("_sole_option", None)
    opts = inf.get("resolution_options")
    if isinstance(opts, list):
        opts[:] = []
    else:
        inf["resolution_options"] = []


def _prev_filter_error(inf: dict) -> int:
    prev = inf.get("policy_filter_error")
    return int(prev) if isinstance(prev, int) else 0


def _store_policy_fields(inf: dict, new_items: list[dict], global_error: int) -> None:
    """`policy_forbidden`(앞 기록과 (kind, id) 합집합 — 앞 기록 뒤에 새 항목) · `blocked_by_hard_lock` ·
    `policy_filter_error`(줄지 않는다)를 싣는다(§2.0.4)."""
    prev = inf.get("policy_forbidden")
    merged = list(prev) if isinstance(prev, list) else []
    seen = dict.fromkeys(_item_key(it) for it in merged)
    for it in new_items:
        key = _item_key(it)
        if key not in seen:
            seen[key] = None
            merged.append(it)
    errors = sum(1 for it in merged if isinstance(it, dict) and it.get("filter_error") is True)
    inf["policy_forbidden"] = merged
    inf["blocked_by_hard_lock"] = len(merged)
    inf["policy_filter_error"] = max(_prev_filter_error(inf), errors, global_error)


def _filter_or_fail_closed(cards: list, maps) -> tuple[list, list[dict]]:
    if maps is None:
        return _fail_closed_options(cards)
    return filter_options(cards, forbidden=maps[0], marked=maps[1])


def _finalize(inf: dict) -> None:
    opts = inf.get("resolution_options")
    sole = inf.get("_sole_option")
    sole_cards = [sole] if isinstance(sole, dict) else (sole if isinstance(sole, list) else [])
    global_error = 0
    try:
        maps = _resolve_maps([*(opts if isinstance(opts, list) else ()), *sole_cards], None, None)
    except Exception:
        logger.warning("하드락 처방 판정 지도를 지을 수 없어 설정을 바꾸는 카드를 모두 뺍니다(fail-closed).", exc_info=True)
        maps, global_error = None, 1
    new_items: list[dict] = []
    if isinstance(opts, list):
        kept, blocked = _filter_or_fail_closed(opts, maps)
        opts[:] = kept
        new_items += blocked
    if isinstance(sole, (dict, list)):
        kept, blocked = _filter_or_fail_closed(sole_cards, maps)
        new_items += blocked
        if not kept:
            _drop_sole_option(inf)
        elif isinstance(sole, list):
            sole[:] = kept
    _store_policy_fields(inf, new_items, global_error)


def _finalize_last_resort(inf: dict) -> None:
    """거르기 경로 자체가 실패 — 카드를 모두 빼고 판정 오류만 표시한다(§2.0.6 ②)."""
    try:
        _drop_sole_option(inf)
        prev = inf.get("policy_forbidden")
        inf["policy_forbidden"] = prev if isinstance(prev, list) else []
        inf["blocked_by_hard_lock"] = len(inf["policy_forbidden"])
        inf["policy_filter_error"] = max(_prev_filter_error(inf), 1)
    except Exception:
        logger.error("하드락 카드 거르기의 마지막 처리도 예외 — payload 를 그대로 둡니다.", exc_info=True)


def finalize_infeasibility(inf):
    """싱크가 부르는 하나(§2.0.4) — `resolution_options`(제자리)·`_sole_option` 의 하드락 카드를 거르고
    `policy_forbidden`·`blocked_by_hard_lock`·`policy_filter_error` 를 늘 싣는다.

    dict 가 아니면 그대로 돌려준다. 멱등이고 JSON 으로 역직렬화한 dict 에도 같게 동작하며, 예외를 밖으로
    내지 않는다(§2.0.6)."""
    if not isinstance(inf, dict):
        return inf
    try:
        _finalize(inf)
    except Exception:
        logger.warning("하드락 카드 거르기 중 예외 — 카드를 모두 빼고 판정 오류를 표시합니다(fail-closed).", exc_info=True)
        _finalize_last_resort(inf)
    return inf


# ── 생성 진입 관문(11a-2) ─────────────────────────────────────────────────────

#: 처방 카드 거절 문구(11a-2 · 11r 고침) — 비동기 생성은 큐에 넣은 뒤 모달이 닫혀 이 문구가 '근무표 생성 실패' 토스트로만 뜬다.
#: 새로 고쳐도 해결책이 다시 뜨지 않으므로 '다시 생성하면 적용할 수 있는 조정만 보인다'로 안내한다(리뷰 LOW).
_GATE_DETAIL = ("선택한 해결책 {labels}은(는) {rules} 규칙을 완화해 적용할 수 없습니다 — 근무표를 다시 생성하면 "
                "지금 적용할 수 있는 조정 방법만 보여 드립니다.")
_GATE_ERROR_DETAIL = "선택한 해결책 {labels}의 하드락 판정을 지금 할 수 없어 적용하지 않았습니다 — 잠시 뒤 다시 시도해 주세요."


def _requested_treatment_ids(treatment_ids) -> list:
    """요청 처방 id — 순서를 지키고 겹침을 뺀다. 문자열 하나면 id 하나로, 목록으로 못 읽으면 값 전체를 id 하나로 본다."""
    if isinstance(treatment_ids, str):
        return [treatment_ids]
    try:
        return list(dict.fromkeys(_hashable(t) for t in treatment_ids))
    except TypeError:
        return [repr(treatment_ids)]


def _treatment_label(onto, tid) -> str:
    """처방 이름 — 사용자가 누른 해결 카드 제목과 같은 출처다(EI-11 11r · 리뷰 MEDIUM).

    카드 제목은 `undiagnosed_probe.treatments_to_resolution_options` 의 `_lbl` 이 `config_key_label_ko`
    (`cause_treatment_hitter` 가 `friendly_config_key_label(config_key)` 로 채움)로 짓는다. 그 라벨이 없으면 yaml `label`,
    그것도 없으면 id. 예전엔 yaml `label`('야간 회복 OFF 강제 비활성화' 등)을 써 누른 카드('2N 후 2일 OFF 규칙')와 이름이 달랐다."""
    t = onto.get_treatment(tid) if onto is not None and isinstance(tid, str) else None
    if t is None:
        return str(tid)
    from services.semantics.ontology import friendly_config_key_label  # onto 가 있으면 이미 적재된 모듈

    return friendly_config_key_label(t.config_key) or t.label or str(tid)


def _reject_treatments(onto, tids: list, template: str, **fields) -> None:
    from fastapi import HTTPException

    labels = ", ".join(f"'{_treatment_label(onto, t)}'" for t in tids)
    raise HTTPException(status_code=400, detail=template.format(labels=labels, **fields))


def _gate_fallback(tids: list) -> None:
    """관문 판정 실패(§2.0.6 ④) — 하드락 family 처방이 있으면 400, 없으면 통과. 처방 라벨(온톨로지)을 못 읽으면
    요청 처방을 모두 하드락 처방으로 본다."""
    try:
        from services.semantics.ontology import get_default_ontology

        onto = get_default_ontology()
        family_ids = hard_lock_family_treatment_ids(onto)
        hits = [t for t in tids if t in family_ids]
    except Exception:
        logger.warning("생성 진입 관문: 처방 라벨도 읽을 수 없어 요청 처방을 모두 막습니다(fail-closed).", exc_info=True)
        onto, hits = None, list(tids)
    if hits:
        _reject_treatments(onto, hits, _GATE_ERROR_DETAIL)


def assert_treatment_patch_allowed(patched_config) -> None:
    """처방 적용 직후 재검(EI-12 · BR-3) — 적용 기록(`_applied_constraint_adjustments`)에 하드락 BLOCK 표지가 있으면 400.

    진입 관문(`assert_treatments_allowed`)은 엄격 표본으로 미리 거른다. 이쪽은 실제 설정에 적용한 결과를 다시 본다 — 별칭 정본화로
    살아난 처방·레버가 관문을 새로 빠져나가도 엔진에는 닿지 않는다. 판정 자체가 실패한 기록(`hard_lock_check_error`)은 하드락
    family 조정일 때만 막는다(§2.0.6 ④ 와 같은 규칙)."""
    records = (patched_config or {}).get(_APPLIED_KEY) or []
    hits = [r for r in records if isinstance(r, dict) and (
        r.get("blocked_by_hard_lock") or (r.get("hard_lock_check_error") and r.get("family") in HARD_LOCK_FAMILIES_FALLBACK))]
    if not hits:
        return
    from fastapi import HTTPException

    nos = _positive_rule_nos(n for r in hits for b in r.get("blocked_by_hard_lock") or () for n in b.get("rule_nos") or ())
    raise HTTPException(status_code=400, detail=f"처방 적용 결과가 {_rules_text(nos)} 규칙을 완화해 적용하지 않았습니다.")


def assert_treatments_allowed(treatment_ids) -> None:
    """생성 진입 관문(§2.3) — 요청 처방 중 하드락 8종을 완화하는 처방(`forbidden_treatments()`)이 있으면 400.

    `treatment_ids` 가 비면(None·빈 목록) 판정 없이 돌아간다. 온톨로지에 없는 id 는 통과한다(적용기가 unresolved 로
    둔다). 판정이 실패하면 고정 family 집합으로 다시 본다(§2.0.6 ④ — 하드락 family 처방이면 400, 아니면 통과 + WARNING).
    """
    if not treatment_ids:
        return
    tids = _requested_treatment_ids(treatment_ids)
    try:
        from services.semantics.ontology import get_default_ontology

        onto = get_default_ontology()
        forbidden = forbidden_treatments(ontology=onto)
    except Exception:
        logger.warning("생성 진입 관문: 처방 하드락 판정 실패 — 하드락 family 처방이면 막습니다(fail-closed).", exc_info=True)
        _gate_fallback(tids)
        return
    hits = [t for t in tids if t in forbidden]
    if hits:
        rules = _rules_text(_positive_rule_nos(n for t in hits for n in forbidden[t]))
        _reject_treatments(onto, hits, _GATE_DETAIL, rules=rules)


# ── '정책상 금지' 표지 — 카드가 아닌 출구 칸(11c) ─────────────────────────────

_POLICY_TREATMENT_MSG = "'{label}' 처방은 {rules} 규칙을 완화해 정책상 내보내지 않습니다(원인 진단은 그대로 남깁니다)."
_HARD_LOCK_ONLY_SOLUTION_KO = "하드락 규칙이라 자동 조정이 없습니다 — 인원·고정 근무를 직접 조정해 주세요."
_MARKS_ERROR_MSG = "하드락 판정 중 오류가 나 하드락 가족 처방을 모두 정책상 금지로 표지했습니다."


def _fold_modes(entries: list[dict]) -> dict:
    """family 의 action 판정들 → family 하나의 판정. blocked(rule_nos 합집합) → capped → marked → ok 순."""
    blocked = [e for e in entries if e.get("mode") == "blocked"]
    if blocked:
        out = {"mode": "blocked", "rule_nos": _positive_rule_nos(n for e in blocked for n in e.get("rule_nos") or ())}
        if any(e.get("error") for e in blocked):
            out["error"] = True
        return out
    for mode in ("capped", "marked"):
        hit = next((e for e in entries if e.get("mode") == mode), None)
        if hit is not None:
            return dict(hit)
    return {"mode": "ok", "rule_nos": []}


def family_modes(ontology=None) -> dict[str, dict]:
    """`family_action_policy()` 를 family 단위로 접는다(§2.0.3 — 11c). 하나라도 blocked 면 blocked, 아니면 capped(그 항목의
    `key`·`cap`·`rule_nos`), 아니면 marked, 아니면 ok. 지원 action 이 없는 family 는 싣지 않는다(fix_plan 축 판정이 쓴다)."""
    by_family: dict[str, list[dict]] = {}
    for (family, _action), entry in family_action_policy(ontology).items():
        by_family.setdefault(family, []).append(entry)
    return {family: _fold_modes(entries) for family, entries in by_family.items()}


def mode_suffix(entry: dict | None) -> str:
    """판정 dict(`family_action_policy`·`family_modes` 항목)가 capped 면 `" (최대 {cap}{단위} — 하드락 #{n})"`, 아니면 `""`.
    상한·번호는 판정 dict(레지스트리 파생)에서, 단위('일'·'회')는 규칙 라벨(`hard_lock.rule_cap_unit`)에서 읽는다 — 숫자·단위를
    다시 적지 않는다(R3-06 · 단위는 EI-11 11r 리뷰 LOW)."""
    entry = entry or {}
    nos = _positive_rule_nos(entry.get("rule_nos") or ())
    if entry.get("mode") != "capped" or entry.get("cap") is None or not nos:
        return ""
    return f" (최대 {entry['cap']}{rule_cap_unit(nos[0])} — 하드락 {'·'.join(f'#{n}' for n in nos)})"


def capped_suffix(target_family: str, action: str | None, *, policy: dict | None = None) -> str:
    """`family_action_policy()` 가 capped 인 (family, action) 이면 `" (최대 {cap} — 하드락 #{n})"`, 아니면 `""`(§2.0.3 — 11c).
    `policy` 를 넘기면 그 판정을 쓴다(호출부가 한 번 지은 것 — 캐시가 아니다)."""
    table = policy if policy is not None else family_action_policy()
    return mode_suffix(table.get((target_family, action)))


def _capped_treatment_suffixes(onto, treatment_ids) -> dict[str, str]:
    """처방마다 `capped_suffix(target_family, 처방 action 의 control action)` — 비지 않은 처방만 담는다."""
    from services.treatment_applicator import _ACTION_MAP

    policy = family_action_policy(onto)
    out: dict[str, str] = {}
    for tid in dict.fromkeys(t for t in treatment_ids if isinstance(t, str)):
        t = onto.get_treatment(tid)
        if t is None:
            continue
        family = onto.resolve_alias(t.target_family) or t.target_family
        suffix = capped_suffix(family, _ACTION_MAP.get(t.action_type), policy=policy)
        if suffix:
            out[tid] = suffix
    return out


def _covering_forbidden(onto, cause_ids, forbidden: dict) -> dict[str, set[str]]:
    """금지 처방 → 그 처방이 덮는(`applies_to_causes`) 원인 id 집합 — 원인 순서 → 온톨로지 처방 순서."""
    covered: dict[str, set[str]] = {}
    for raw in cause_ids or ():
        cid = onto.resolve_cause_alias(raw) if isinstance(raw, str) else None
        for t in onto.treatments_for_cause(cid) if cid else ():
            if t.treatment_id in forbidden:
                covered.setdefault(t.treatment_id, set()).add(cid)
    return covered


def policy_forbidden_for_causes(cause_ids, *, ontology=None) -> list[dict]:
    """원인 id(별칭이면 `resolve_cause_alias`)마다 그 원인을 덮는 금지 처방의 `kind="treatment"` 항목(§2.0.3·§2.0.5 — 11c).

    `causes` 는 덮는 원인 id 목록(정렬)이다. 금지 처방은 set cover 에서 이미 빠졌다(11a-2) — 이 항목은 '원인 진단은 남기고
    처방만 정책으로 막았다'는 기록이라 `blocked_by_hard_lock` 에 함께 센다."""
    from services.semantics.ontology import get_default_ontology

    onto = ontology if ontology is not None else get_default_ontology()
    forbidden, _marked, block_keys = _treatment_verdicts(onto)
    items: list[dict] = []
    for tid, causes in _covering_forbidden(onto, cause_ids, forbidden).items():
        nos = _positive_rule_nos(forbidden[tid])
        message = _POLICY_TREATMENT_MSG.format(label=_treatment_label(onto, tid), rules=_rules_text(nos))
        items.append({"kind": "treatment", "id": tid, "rule_nos": nos, "keys": list(block_keys.get(tid) or []),
                      "causes": sorted(causes), "message_ko": message})
    return items


def _with_cap_suffix(text: str, suffix: str) -> str:
    """첫 '.' 바로 앞에 상한 문구를 넣는다('.' 가 없으면 끝에). 이미 들어 있으면 그대로(멱등)."""
    if not suffix or suffix.strip() in text:
        return text
    cut = text.find(".")
    return f"{text[:cut]}{suffix}{text[cut:]}" if cut >= 0 else f"{text}{suffix}"


def _suffix_treatment_row(row: dict, suffix: str) -> None:
    """상한형 처방 한 줄 — rationale 과 동의 문구(`apply_hint.human_message_ko`)에 같은 상한 문구를 넣는다(멱등).
    동의 문구는 EI-11 11r(리뷰 LOW — 같은 처방의 rationale 에만 상한이 붙고 동의 문구에는 없었다)."""
    if isinstance(row.get("rationale_ko"), str):
        row["rationale_ko"] = _with_cap_suffix(row["rationale_ko"], suffix)
    hint = row.get("apply_hint")
    if isinstance(hint, dict) and isinstance(hint.get("human_message_ko"), str):
        hint["human_message_ko"] = _with_cap_suffix(hint["human_message_ko"], suffix)


def _problem_items(narrative) -> list[dict]:
    items = narrative.get("problem_list") if isinstance(narrative, dict) else None
    return [p for p in items or [] if isinstance(p, dict)]


def _view_treatments(treatment_recommendations, narrative) -> list[dict]:
    """처방 rationale 칸 — 처방 목록(`treatments[*]`) · 서술 `action_levers[*]` · problem_list `mapped_treatments[*]`."""
    rows = [t for b in treatment_recommendations or [] if isinstance(b, dict)
            for t in b.get("treatments") or [] if isinstance(t, dict)]
    levers = narrative.get("action_levers") if isinstance(narrative, dict) else None
    rows += [a for a in levers or [] if isinstance(a, dict)]
    rows += [m for p in _problem_items(narrative) for m in p.get("mapped_treatments") or [] if isinstance(m, dict)]
    return rows


def _solution_ko(rendered: str, mapped: list[dict]) -> str:
    """원인 + 해결책 한 문장 — 금지 아닌 처방 앞 2개(`resolution_narrative.build_narrative` 와 같은 식)."""
    allowed = [m for m in mapped if not m.get("policy_forbidden")]
    if not allowed:
        return f"{rendered} → 해결책: {_HARD_LOCK_ONLY_SOLUTION_KO}"
    return f"{rendered} → 해결책: {' 또는 '.join(str(m.get('rationale_ko') or '').split('.')[0] for m in allowed[:2])}."


def _mark_problem_list(narrative, forbidden: dict | None, suffixes: dict[str, str]) -> None:
    """금지 처방에 `policy_forbidden`·`rule_nos` 표지(`forbidden` 이 None 이면 전부 금지로 본다) — 표지나 상한 문구가 생긴
    원인만 `cause_solution_ko` 를 다시 짓는다. 원인 진단(`rendered_ko`)은 그대로다."""
    for item in _problem_items(narrative):
        mapped = [m for m in item.get("mapped_treatments") or [] if isinstance(m, dict)]
        touched = False
        for m in mapped:
            tid = m.get("treatment_id")
            if forbidden is None or tid in forbidden:
                m["policy_forbidden"] = True
                m["rule_nos"] = _positive_rule_nos((forbidden or {}).get(tid) or ())
                touched = True
            elif tid in suffixes:
                touched = True
        if touched:
            item["cause_solution_ko"] = _solution_ko(str(item.get("rendered_ko") or ""), mapped)


def _fail_closed_marks(narrative, cause_ids) -> list[dict]:
    """§2.0.6 ⑤ — 하드락 가족 처방(`HARD_LOCK_FAMILIES_FALLBACK`)을 모두 표지하고 판정 오류 항목 하나를 돌려준다
    (`policy_filter_error` 가 1 이 된다). 가족 처방도 못 읽으면 원인의 처방을 모두 표지한다."""
    try:
        fallback: dict | None = dict.fromkeys(hard_lock_family_treatment_ids(), ())
    except Exception:
        logger.warning("하드락 가족 처방도 읽을 수 없어 원인의 처방을 모두 정책상 금지로 표지합니다(fail-closed).", exc_info=True)
        fallback = None
    try:
        _mark_problem_list(narrative, fallback, {})
    except Exception:
        logger.error("problem_list 표지의 대체 경로도 예외 — 서술을 그대로 둡니다.", exc_info=True)
    causes = sorted({c for c in cause_ids or () if isinstance(c, str)})
    return [{"kind": "treatment", "id": None, "rule_nos": [], "keys": [], "causes": causes,
             "message_ko": _MARKS_ERROR_MSG, "filter_error": True}]


def mark_treatment_views(treatment_recommendations, resolution_narrative, cause_ids) -> list[dict]:
    """카드가 아닌 출구 칸의 '정책상 금지' 표지(§2.5-11c-2 — payload 빌더 후처리가 부른다)를 제자리에서 단다.

    ① 상한형 처방(금지 아님 + `capped_suffix` 가 비지 않음 — 지금은 월 나이트 상한 +1)의 rationale·동의 문구(`apply_hint`)에
    상한 문구 — 처방 목록·서술 `action_levers`·problem_list `mapped_treatments` ② problem_list 금지 처방 표지와 그 원인의
    `cause_solution_ko` 재작성. 돌려주는 값은 `policy_forbidden_for_causes(cause_ids)` 처방 항목이다. 예외를 내지 않는다 —
    판정이 실패하면 하드락 가족 처방을 모두 표지하고 `filter_error` 항목 하나를 돌려준다(§2.0.6 ⑤)."""
    try:
        from services.semantics.ontology import get_default_ontology

        forbidden = forbidden_treatments()
        rows = _view_treatments(treatment_recommendations, resolution_narrative)
        suffixes = {tid: s for tid, s in _capped_treatment_suffixes(
            get_default_ontology(), [r.get("treatment_id") for r in rows]).items() if tid not in forbidden}
        items = policy_forbidden_for_causes(cause_ids)
        for row in rows:
            if row.get("treatment_id") in suffixes:
                _suffix_treatment_row(row, suffixes[row["treatment_id"]])
        _mark_problem_list(resolution_narrative, forbidden, suffixes)
        return items
    except Exception:
        logger.warning("처방 '정책상 금지' 표지 판정 실패 — 하드락 가족 처방을 모두 표지합니다(fail-closed).", exc_info=True)
        return _fail_closed_marks(resolution_narrative, cause_ids)
