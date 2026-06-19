"""UNDIAGNOSED 블랙박스 probe — 분석(MUS/산술/max-flow)이 원인을 못 짚을 때,
결합제약을 하나씩 완화해 엔진을 재실행하고 "무엇을 풀면 feasible 해지는가"를 실측한다.

배경: MUS(SufficientAssumptionsForInfeasibility)는 assumption 미래핑 제약에서 침묵하고,
산술/max-flow 는 월 단위 결합(2N2OFF 회복·연속·월N상한·OFF예산)을 못 본다. 그 결과
UNDIAGNOSED(원인 미식별)가 난다. 이 모듈은 assumption 의존 없이 **config override 로
완화→재생성→feasible?** 만 보므로 그런 케이스에도 작동한다(black-box, verified resolution).

설계:
  - resolve_fn(relaxed_config) -> (feasible: bool, info: dict) 를 주입받는다(엔진 비의존).
  - 완화는 결합제약 노브들(=폴백이 자동 soft 안 하는 hard 규칙)을 하나씩.
  - verify-mode(FB_VERIFY_SKIP_STAGE3) 로 빠르게. feasible 만들면 = 범인이자 해결책.
  - 단일완화로 못 풀면 found=False (조합 필요 — 상위에서 처리/표시).
"""
from __future__ import annotations

import os
from typing import Any, Callable


# 결합제약 완화 카탈로그. apply(cfg)->delta(config_dict 키 기준, DB 컬럼명).
# label_ko 는 사용자 노출용, family 는 그룹핑용. 침습도 낮은(=현실적인) 순서로.
RELAX_CATALOG: list[dict[str, Any]] = [
    {"id": "raise_max_night_cap", "family": "night_cap", "label_ko": "월 야간 상한 완화",
     "apply": lambda c: {"max_nig_per_month": int(c.get("max_nig_per_month") or 0) + 8}},
    {"id": "disable_2n2off", "family": "night_recovery", "label_ko": "2N→2OFF 회복 규칙 해제",
     "apply": lambda c: {"two_offs_after_two_nig": False}},
    {"id": "disable_3n2off", "family": "night_recovery", "label_ko": "3N→2OFF 회복 규칙 해제",
     "apply": lambda c: {"two_offs_after_three_nig": False}},
    {"id": "raise_max_consec_work", "family": "consecutive", "label_ko": "연속근무 상한 완화",
     "apply": lambda c: {"max_conseq_work": int(c.get("max_conseq_work") or 5) + 3}},
    {"id": "relax_consecutive_nights", "family": "night_consecutive", "label_ko": "연속 야간 상한 완화(+1)",
     "apply": lambda c: {"max_consecutive_nights": int(c.get("max_consecutive_nights") or (3 if c.get("three_seq_nig") else 2)) + 1}},
    {"id": "disable_not_one_night", "family": "night_pattern", "label_ko": "단일 야간 금지 해제",
     "apply": lambda c: {"not_one_night": False}},
    {"id": "disable_ban_n_before_fixed_off", "family": "night_pattern", "label_ko": "고정OFF 직전 야간 금지 해제",
     "apply": lambda c: {"ban_night_before_fixed_off": False}},
    {"id": "disable_banned_day_after_eve", "family": "transition", "label_ko": "E→D 전이 금지 해제",
     "apply": lambda c: {"banned_day_after_eve": False}},
    {"id": "lower_off_days", "family": "off_budget", "label_ko": "월 OFF 요구일수 완화(-3)",
     "apply": lambda c: {"off_days": max(0, int(c.get("off_days") or 0) - 3)}},
    {"id": "disable_preceptee_sync", "family": "coupling", "label_ko": "프리셉티 동반(팔로우) 해제",
     "apply": lambda c: {"preceptee_on": False}},
]


# 사용자 노출용: 완화별 트레이드오프 / config 컬럼 한글 라벨
TRADEOFF_KO: dict[str, str] = {
    "raise_max_night_cap": "야간 근무가 개인별로 더 몰릴 수 있습니다.",
    "disable_2n2off": "야간 2회 후 2일 휴식 보장이 약해집니다.",
    "disable_3n2off": "야간 3회 후 2일 휴식 보장이 약해집니다.",
    "raise_max_consec_work": "연속 근무일이 늘어날 수 있습니다.",
    "relax_consecutive_nights": "야간을 더 길게 연속으로 서게 될 수 있습니다.",
    "disable_not_one_night": "단일 야간(1N) 근무가 생길 수 있습니다.",
    "disable_ban_n_before_fixed_off": "고정 휴무 직전에 야간이 배치될 수 있습니다.",
    "disable_banned_day_after_eve": "이브닝 다음날 데이 전이가 생길 수 있습니다.",
    "lower_off_days": "월 휴무일이 줄어듭니다.",
    "disable_preceptee_sync": "프리셉티가 프리셉터와 동반(팔로우)하지 않게 됩니다(교육 동반 약화).",
}
COL_LABEL_KO: dict[str, str] = {
    "max_nig_per_month": "월 야간 상한", "two_offs_after_two_nig": "2N→2OFF 회복",
    "two_offs_after_three_nig": "3N→2OFF 회복", "max_conseq_work": "연속근무 상한",
    "not_one_night": "단일 야간 금지", "ban_night_before_fixed_off": "고정OFF 전 야간 금지",
    "banned_day_after_eve": "E→D 전이 금지", "off_days": "월 OFF 요구일수",
    "max_consecutive_nights": "연속 야간 상한", "preceptee_on": "프리셉티 동반(팔로우)",
}


def to_resolution_options(probe_result: dict[str, Any], base_config: dict[str, Any]) -> list[dict[str, Any]]:
    """probe 결과 → 프론트가 그대로 렌더링/echo 할 수 있는 통합 옵션 카드 리스트.

    각 옵션: option_id, kind(relax_constraint|combo), source, verified, title_ko,
    changes[{config_key,label_ko,from,to}], trade_off_ko, apply{config delta}.
    apply 를 그대로 /roster_create/apply-resolution 로 보내면 재생성된다.
    """
    base = base_config or {}

    def _changes(delta: dict[str, Any]) -> list[dict[str, Any]]:
        return [{"config_key": k, "label_ko": COL_LABEL_KO.get(k, k),
                 "from": base.get(k), "to": v} for k, v in (delta or {}).items()]

    opts: list[dict[str, Any]] = []
    for r in (probe_result.get("resolutions") or []):
        delta = dict(r.get("delta") or {})
        opts.append({
            "option_id": "probe:" + r["id"], "kind": "relax_constraint", "source": "probe",
            "verified": True, "title_ko": r.get("label_ko") or r["id"],
            "changes": _changes(delta), "trade_off_ko": TRADEOFF_KO.get(r["id"], ""),
            "apply": delta,
        })
    cb = probe_result.get("combo")
    if cb:
        merged: dict[str, Any] = {}
        changes: list[dict[str, Any]] = []
        tradeoffs: list[str] = []
        for m in cb.get("members", []):
            d = dict(m.get("delta") or {})
            merged.update(d)
            changes.extend(_changes(d))
            if TRADEOFF_KO.get(m["id"]):
                tradeoffs.append(TRADEOFF_KO[m["id"]])
        opts.append({
            "option_id": cb["id"], "kind": "combo", "source": "probe", "verified": True,
            "title_ko": cb.get("label_ko"), "changes": changes,
            "trade_off_ko": " / ".join(tradeoffs), "apply": merged,
        })
    return opts


def treatments_to_resolution_options(treatment_recommendations: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """ontology treatment_recommendations(번들) → 프론트용 통합 옵션 카드.

    각 번들의 자동적용 가능(force_soft_mode/disable_module/set_threshold/narrow_scope)
    treatment 들을 하나의 옵션으로 묶는다. data_correction_required(수동)는 manual_required 로
    분리. probe 옵션과 달리 verified=False(클릭 시 재생성으로 실검증), apply(컬럼) 대신
    treatment_ids 로 적용한다.
    """
    def _lbl(t: dict[str, Any]) -> str:
        return t.get("config_key_label_ko") or t.get("target_family") or t.get("treatment_id") or "?"

    opts: list[dict[str, Any]] = []
    for b in (treatment_recommendations or []):
        treatments = b.get("treatments") or []
        auto = [t for t in treatments if t.get("action_type") != "data_correction_required"]
        manual = [t for t in treatments if t.get("action_type") == "data_correction_required"]
        if not auto:
            continue  # 전부 수동(data_correction_required)이면 자동 적용 불가 → 옵션 제외
        _bid = str(b.get("bundle_id") or "?")
        opts.append({
            "option_id": _bid if _bid.startswith("bundle") else "bundle:" + _bid,
            "kind": "treatment_bundle", "source": "ontology", "verified": False,
            "title_ko": " + ".join(_lbl(t) for t in auto),
            "changes": [{"config_key": t.get("config_key"), "label_ko": _lbl(t),
                         "direction": t.get("direction_label_ko") or t.get("direction"),
                         "rationale_ko": t.get("rationale_ko")} for t in auto],
            "trade_off_ko": " / ".join([t.get("trade_off_ko") for t in auto if t.get("trade_off_ko")]),
            "treatment_ids": [t.get("treatment_id") for t in auto],
            "manual_required": [t.get("treatment_id") for t in manual],
            "apply": {},
        })
    return opts


def _apply_set(base: dict[str, Any], items: list[dict[str, Any]]) -> dict[str, Any]:
    """완화 집합을 base 위에 적용(각 delta 는 base 기준으로 계산 — 누적 드리프트 방지)."""
    cfg = dict(base)
    for it in items:
        try:
            cfg.update(it["apply"](base))
        except Exception:
            pass
    return cfg


def _find_combo(
    base: dict[str, Any],
    resolve_fn: Callable[[dict[str, Any]], tuple[bool, dict[str, Any]]],
    catalog: list[dict[str, Any]],
    *,
    max_size: int,
    logger: Callable[[str], None],
) -> list[dict[str, Any]] | None:
    """단일완화로 못 풀 때: 크기 2부터 max_size 까지 **전수(combinations)** 로 탐색.
    단일이 모두 실패했으므로, 가장 작은 크기에서 feasible 한 조합은 자동으로 irreducible
    최소조합이다(black-box MCS). 같은 크기에서 여럿이면 침습도(카탈로그 순서) 합이 가장
    낮은 것을 고른다. greedy-순서 누적은 뒤쪽 레버가 필요한 조합을 놓치므로 쓰지 않는다."""
    import itertools

    # 실제 변화가 있는(non-noop) 후보만
    cands = [it for it in catalog
             if not all(base.get(k) == v for k, v in it["apply"](base).items())]
    idx = {id(it): i for i, it in enumerate(cands)}  # 침습도 proxy = 카탈로그 순서
    for size in range(2, max(2, max_size) + 1):
        feasible_combos: list[tuple[int, tuple]] = []
        for combo in itertools.combinations(cands, size):
            ok, _ = resolve_fn(_apply_set(base, list(combo)))
            logger(f"[UndiagProbe][combo-{size}] {[c['id'] for c in combo]} feasible={ok}")
            if ok:
                feasible_combos.append((sum(idx[id(c)] for c in combo), combo))
        if feasible_combos:
            feasible_combos.sort(key=lambda x: x[0])  # 최소 침습 조합
            return list(feasible_combos[0][1])
    return None


def probe_relaxations(
    base_config: dict[str, Any],
    resolve_fn: Callable[[dict[str, Any]], tuple[bool, dict[str, Any]]],
    *,
    catalog: list[dict[str, Any]] | None = None,
    verify: bool = True,
    try_combo: bool = True,
    max_combo: int = 3,
    logger: Callable[[str], None] = print,
) -> dict[str, Any]:
    """결합제약을 하나씩 완화해 resolve_fn 으로 feasible 여부를 실측.

    Returns:
        {
          "found": bool,                 # 단일완화로 푸는 게 하나라도 있나
          "resolutions": [ {id,family,label_ko,delta,info} ... ],  # 푸는 완화들(=원인+해결책)
          "all_probed": [ {..., feasible} ... ],
          "probed_count": int,
        }
    """
    cat = catalog if catalog is not None else RELAX_CATALOG
    prev_env = os.environ.get("FB_VERIFY_SKIP_STAGE3")
    if verify:
        os.environ["FB_VERIFY_SKIP_STAGE3"] = "1"
    all_probed: list[dict[str, Any]] = []
    resolutions: list[dict[str, Any]] = []
    combo: dict[str, Any] | None = None
    try:
        for item in cat:
            try:
                delta = item["apply"](base_config)
            except Exception as exc:  # 카탈로그 항목 자체 오류는 건너뜀
                logger(f"[UndiagProbe] {item['id']} delta build 실패: {exc}")
                continue
            # 실제 변화가 없으면(이미 그 값) 스킵
            if all(base_config.get(k) == v for k, v in delta.items()):
                continue
            relaxed = dict(base_config)
            relaxed.update(delta)
            try:
                feasible, info = resolve_fn(relaxed)
            except Exception as exc:
                feasible, info = False, {"error": str(exc)[:120]}
            logger(f"[UndiagProbe] {item['id']:30s} feasible={feasible} delta={delta}")
            all_probed.append({
                "id": item["id"], "family": item["family"], "label_ko": item["label_ko"],
                "delta": delta, "feasible": bool(feasible), "info": info,
            })
        resolutions = [
            {k: r[k] for k in ("id", "family", "label_ko", "delta", "info")}
            for r in all_probed if r["feasible"]
        ]
        # 단일완화로 못 풀면 최소조합(black-box MCS) 탐색
        if not resolutions and try_combo:
            members = _find_combo(base_config, resolve_fn, cat, max_size=max_combo, logger=logger)
            if members:
                combo = {
                    "id": "combo:" + "+".join(m["id"] for m in members),
                    "label_ko": " + ".join(m["label_ko"] for m in members),
                    "members": [
                        {"id": m["id"], "family": m["family"], "label_ko": m["label_ko"],
                         "delta": m["apply"](base_config)}
                        for m in members
                    ],
                }
    finally:
        if verify:
            if prev_env is None:
                os.environ.pop("FB_VERIFY_SKIP_STAGE3", None)
            else:
                os.environ["FB_VERIFY_SKIP_STAGE3"] = prev_env
    return {
        "found": bool(resolutions or combo),
        "resolutions": resolutions,
        "combo": combo,
        "all_probed": all_probed,
        "probed_count": len(all_probed),
    }
