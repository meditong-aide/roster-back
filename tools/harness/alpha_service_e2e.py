"""α service-level E2E — 실제 ICU 데이터를 base 로 4 신규 cause 도달 + payload 검증.

흐름:
  1. /tmp/icu_nurses.json 의 33 nurses 데이터 로드 (real ICU schema)
  2. 4 시나리오 동적 변형 — capacity_total / daily_night / shift_eligible / preceptee_sync
  3. run_runtime_precheck → build_unrecoverable_payload (or build_blocking_payload)
  4. payload.infeasibility.causes / treatment_recommendations / resolution_narrative 검증
  5. naive 패턴 (X명 줄이) 검출 0건 확인

zero DB write — HTTP 우회 service-level. 사용자 정책 부합.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any, Dict, List

# app path
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "app"))

from services.precheck import (  # noqa: E402
    build_blocking_payload,
    build_unrecoverable_payload,
    has_blocking_issues,
    run_runtime_precheck,
)


NURSES_PATH = Path("/tmp/icu_nurses.json")
GROUP_ID = "10135857f9f9"  # ICU
YEAR = 2026
MONTH = 7

NAIVE_PATTERN = re.compile(r"(간호사|인원)\s*(을|를)?\s*(\d+\s*명\s*만?\s*)?(줄이|감축|줄이세요)")


def _load_base_nurses() -> List[Dict[str, Any]]:
    if not NURSES_PATH.exists():
        print("[ERR] /tmp/icu_nurses.json 없음 — 먼저 fetch.")
        sys.exit(2)
    data = json.loads(NURSES_PATH.read_text())
    # ICU group_id 필터
    icu = [n for n in data if n.get("group_id") == GROUP_ID]
    print(f"[base] {len(icu)} ICU nurses loaded")
    return icu


def _base_config(daily_demand: Dict[str, int] | None = None) -> Dict[str, Any]:
    """ICU 의 평균 demand 추정 (baseline 호출에서 본 'need: 6' 야간 + 적당한 D/E).

    실제 ICU config 에 있을 법한 한도 키도 포함해 closed-loop apply 자동 적용
    경로를 검증할 수 있게 한다.
    """
    return {
        "use_mid": False,
        "daily_shift_requirements": daily_demand or {"D": 8, "E": 6, "N": 6},
        "global_monthly_off_days": 9,
        "standard_personal_off_days": 0,
        # closed-loop apply target keys
        "max_night_shifts_per_month": 8,
        "max_consecutive_work": 5,
    }


def _summarize(payload: Dict[str, Any], tag: str) -> Dict[str, Any]:
    inf = payload.get("infeasibility", {})
    causes = inf.get("causes") or []
    symptoms = inf.get("observed_symptoms") or []
    treatments = inf.get("treatment_recommendations") or []
    narrative = inf.get("resolution_narrative") or {}
    problem_list = narrative.get("problem_list") or []
    action_levers = narrative.get("action_levers") or []
    trade_offs = narrative.get("trade_offs") or []

    # naive 검사
    all_text = " ".join(
        [p.get("message", "") if isinstance(p, dict) else str(p) for p in problem_list]
        + [a.get("description", "") if isinstance(a, dict) else str(a) for a in action_levers]
    )
    naive_hits = NAIVE_PATTERN.findall(all_text)

    summary = {
        "tag": tag,
        "severity": inf.get("severity"),
        "causes_count": len(causes),
        "cause_ids": [c.get("node_id") or c.get("reason_code") for c in causes],
        "symptoms_count": len(symptoms),
        "treatments_count": len(treatments),
        "problem_list_count": len(problem_list),
        "action_levers_count": len(action_levers),
        "trade_offs_count": len(trade_offs),
        "naive_hits": naive_hits,
    }
    return summary


# ─────────────────────────────────────────────────────────────────────────────
# Scenario builders — 각 시나리오는 base ICU 33 nurses 의 어느 attribute 만 변형
# ─────────────────────────────────────────────────────────────────────────────


def scenario_capacity_total_shortage(base: List[dict]) -> tuple[List[dict], Dict[str, Any]]:
    """모든 nurse 에 personal_off_adjustment ↑ → capacity 부족."""
    nurses = [dict(n) for n in base]
    for n in nurses:
        n["personal_off_adjustment"] = 18
    cfg = _base_config({"D": 10, "E": 8, "N": 8})  # demand 강화
    cfg["global_monthly_off_days"] = 12
    return nurses, cfg


def scenario_daily_night_shortage(base: List[dict]) -> tuple[List[dict], Dict[str, Any]]:
    """야간 가능 인원을 의도적으로 1명만 남김 → daily N demand 6 가 불가능."""
    nurses = [dict(n) for n in base]
    keep_n = 0
    for n in nurses:
        if keep_n < 1:
            n["is_night_nurse"] = ["N"]
            keep_n += 1
        else:
            # N 자격 박탈 — D, E 만
            n["is_night_nurse"] = ["D", "E"]
    cfg = _base_config({"D": 6, "E": 5, "N": 6})
    return nurses, cfg


def scenario_shift_eligible_shortage(base: List[dict]) -> tuple[List[dict], Dict[str, Any]]:
    """모든 nurse 가 D 자격이 없도록 (E/N만) → D demand 발생 시 자격자 0."""
    nurses = [dict(n) for n in base]
    for n in nurses:
        n["is_night_nurse"] = ["E", "N"]  # D 제외
    cfg = _base_config({"D": 4, "E": 4, "N": 4})  # D demand 가 있는 상태
    return nurses, cfg


def scenario_preceptee_sync_mismatch(base: List[dict]) -> tuple[List[dict], Dict[str, Any]]:
    """기존 preceptor 관계를 강제로 shift/team mismatch 가 되게 변형."""
    nurses = [dict(n) for n in base]
    if len(nurses) < 2:
        return nurses, _base_config()
    # 첫 2명에 강제 preceptor pair 주입
    ptor = nurses[0]
    ptee = nurses[1]
    ptee["preceptor_id"] = ptor.get("nurse_id")
    # shift mismatch
    ptor["is_night_nurse"] = ["D"]
    ptee["is_night_nurse"] = ["N"]
    # team mismatch
    ptor["team_id"] = 1
    ptee["team_id"] = 2
    cfg = _base_config()
    return nurses, cfg


def scenario_marginal_monthly_night(base: List[dict]) -> tuple[List[dict], Dict[str, Any]]:
    """경미한 infeasible — monthly N capacity 가 단 1 step 부족.

    ICU 33명, monthly_N_need = 31일 × demand_N.
    night_capacity = 33명 × (31 - 9 off) = 726 정도 — 충분.
    그러나 demand_N 을 capacity 바로 근처에 세팅해서 1-step delta 로 해소.
    """
    nurses = [dict(n) for n in base]
    for n in nurses:
        n["is_night_nurse"] = ["D", "E", "N"]
    # global_off=22 → working capacity = 31-22 = 9 / nurse. 33×9 = 297.
    # N demand_per_day = 10 → 31×10 = 310 → shortage 13. cap +1 (22→21 off) → 33×10 = 330 OK.
    # 다만 우리는 monthly_night_cap 키 +1 검증. demand=N 6 / day → 186.
    # max_night_shifts_per_month=5 일 때 cap = 33×5=165 < 186 → +1=198 충분.
    cfg = _base_config({"D": 6, "E": 5, "N": 6})
    cfg["max_night_shifts_per_month"] = 5  # 1 step (5→6) 으로 해소되는 경계
    cfg["global_monthly_off_days"] = 4  # off 적게 → night capable 충분
    return nurses, cfg


SCENARIOS = {
    "capacity_total": scenario_capacity_total_shortage,
    "daily_night": scenario_daily_night_shortage,
    "shift_eligible": scenario_shift_eligible_shortage,
    "preceptee_sync": scenario_preceptee_sync_mismatch,
    "marginal_monthly_night": scenario_marginal_monthly_night,
}


def run_one(tag: str, base: List[dict]) -> Dict[str, Any]:
    nurses, cfg = SCENARIOS[tag](base)
    pre = run_runtime_precheck(
        nurses_dict=nurses,
        config_dict=cfg,
        grade_config=None,
        fixed_cells=None,
        year=YEAR,
        month=MONTH,
        stop_on_config_error=False,
    )
    issue_codes = sorted({i.get("reason_code", "?") for i in pre.get("issues", [])})
    print(f"\n=== {tag} ===")
    print(f"  precheck status: {pre.get('status')} | issues: {len(pre.get('issues', []))}")
    print(f"  reason_codes: {issue_codes[:8]}")

    if has_blocking_issues(pre):
        payload = build_blocking_payload(pre)
        summary = _summarize(payload, tag)
        print(f"  [BLOCKING] {summary}")
        return summary

    # blocking 아니면 unrecoverable 경로로 simulate (가짜 last_error)
    payload = build_unrecoverable_payload(
        precheck_result=pre,
        applied_relaxations=[],
        last_error_reason=f"e2e simulated {tag}",
        violated_constraints=[],
        conflict_cores=[],
        pool_snapshot={},
    )
    summary = _summarize(payload, tag)
    print(f"  [UNRECOVERABLE-sim] {summary}")
    return summary


def main():
    base = _load_base_nurses()
    if not base:
        print("[ERR] no ICU nurses")
        sys.exit(2)
    results = []
    for tag in SCENARIOS:
        results.append(run_one(tag, base))
    print("\n=== SUMMARY ===")
    for r in results:
        print(f"  {r['tag']}: severity={r['severity']} causes={r['causes_count']} "
              f"treatments={r['treatments_count']} naive_hits={len(r['naive_hits'])}")
    # 결과 저장
    Path("/tmp/alpha_e2e_results.json").write_text(json.dumps(results, ensure_ascii=False, indent=2))


def closed_loop_iterate(tag: str, max_steps: int = 5) -> Dict[str, Any]:
    """N-step closed-loop — 매 step apply 후 causes 감소까지 반복."""
    from services.cause_treatment_hitter import propose_bundles
    from services.treatment_applicator import apply_treatments

    base = _load_base_nurses()
    nurses, cfg = SCENARIOS[tag](base)
    trace = []
    for step in range(1, max_steps + 1):
        pre = run_runtime_precheck(
            nurses_dict=nurses, config_dict=cfg, grade_config=None,
            fixed_cells=None, year=YEAR, month=MONTH, stop_on_config_error=False,
        )
        payload = build_blocking_payload(pre) if has_blocking_issues(pre) else None
        causes = (payload or {}).get("infeasibility", {}).get("causes", [])
        cause_codes = [c.get("reason_code") for c in causes]
        if not causes:
            trace.append({"step": step, "causes": 0, "applied": [], "done": True})
            break
        cause_ids = [c.get("reason_code") for c in causes if c.get("reason_code")]
        bundles = propose_bundles(active_causes=cause_ids, max_alternatives=1)
        if not bundles:
            trace.append({"step": step, "causes": len(causes), "applied": [], "done": False})
            break
        tids = [t.treatment_id for t in bundles[0].treatments]
        try:
            r = apply_treatments(tids, cfg)
            cfg = r.patched_config
            trace.append({"step": step, "causes": len(causes), "codes": cause_codes,
                          "applied": list(r.applied_treatment_ids),
                          "manual": list(r.manual_required)})
            if not r.applied_treatment_ids:
                break  # 자동 apply 불가 — 멈춤
        except Exception as exc:
            trace.append({"step": step, "causes": len(causes), "error": str(exc)})
            break
    print(f"\n=== closed_loop_iterate {tag} ===")
    for t in trace:
        print(f"  step{t['step']}: causes={t['causes']} applied={t.get('applied')} "
              f"{'DONE' if t.get('done') else ''}")
    return {"tag": tag, "trace": trace,
            "final_causes": trace[-1]["causes"] if trace else None}


def closed_loop(tag: str) -> Dict[str, Any]:
    """폐루프 검증 — apply_treatments(primary bundle) 후 issues 감소 확인.

    1) before: precheck → causes (via build_blocking_payload)
    2) primary bundle 의 treatment_ids 추출
    3) apply_treatments → patched config
    4) after: 새 config 로 다시 precheck → causes
    5) before > after 검증
    """
    from services.cause_treatment_hitter import propose_bundles
    from services.treatment_applicator import apply_treatments

    base = _load_base_nurses()
    nurses, cfg = SCENARIOS[tag](base)
    pre1 = run_runtime_precheck(
        nurses_dict=nurses, config_dict=cfg, grade_config=None,
        fixed_cells=None, year=YEAR, month=MONTH, stop_on_config_error=False,
    )
    payload1 = build_blocking_payload(pre1) if has_blocking_issues(pre1) else None
    causes_before = (payload1 or {}).get("infeasibility", {}).get("causes", [])
    before_n = len(causes_before)

    cause_ids = [c.get("reason_code") for c in causes_before if c.get("reason_code")]
    bundles = propose_bundles(active_causes=cause_ids, max_alternatives=1) if cause_ids else []
    primary = bundles[0] if bundles else None
    treatment_ids = [t.treatment_id for t in primary.treatments] if primary else []

    if not treatment_ids:
        print(f"[{tag}] no treatment to apply")
        return {"tag": tag, "before": before_n, "after": before_n, "applied": []}

    try:
        applied = apply_treatments(treatment_ids, cfg)
        patched_cfg = applied.patched_config
        applied_ids = list(applied.applied_treatment_ids)
        manual_required = list(applied.manual_required)
        apply_error = None
    except Exception as exc:
        # set_threshold target_value 등이 채워지지 않은 treatment — manual operator 필요
        patched_cfg = cfg
        applied_ids = []
        manual_required = list(treatment_ids)
        apply_error = f"{type(exc).__name__}: {exc}"

    pre2 = run_runtime_precheck(
        nurses_dict=nurses, config_dict=patched_cfg, grade_config=None,
        fixed_cells=None, year=YEAR, month=MONTH, stop_on_config_error=False,
    )
    payload2 = build_blocking_payload(pre2) if has_blocking_issues(pre2) else None
    causes_after = (payload2 or {}).get("infeasibility", {}).get("causes", [])
    after_n = len(causes_after)

    print(f"\n=== closed_loop {tag} ===")
    print(f"  before causes={before_n} ({[c.get('reason_code') for c in causes_before]})")
    print(f"  applied treatments={applied_ids}")
    print(f"  manual_required={manual_required}")
    if apply_error:
        print(f"  apply_error={apply_error}")
    print(f"  after  causes={after_n} ({[c.get('reason_code') for c in causes_after]})")
    return {
        "tag": tag,
        "before": before_n,
        "after": after_n,
        "applied": applied_ids,
        "manual": manual_required,
        "apply_error": apply_error,
    }


def dump_narrative(tag: str):
    """디버그 — 특정 tag 시나리오의 full payload 를 /tmp/alpha_e2e_<tag>.json 으로."""
    base = _load_base_nurses()
    nurses, cfg = SCENARIOS[tag](base)
    pre = run_runtime_precheck(
        nurses_dict=nurses, config_dict=cfg, grade_config=None,
        fixed_cells=None, year=YEAR, month=MONTH, stop_on_config_error=False,
    )
    if has_blocking_issues(pre):
        payload = build_blocking_payload(pre)
    else:
        payload = build_unrecoverable_payload(
            precheck_result=pre, applied_relaxations=[],
            last_error_reason=f"e2e {tag}",
            violated_constraints=[], conflict_cores=[], pool_snapshot={},
        )
    out = Path(f"/tmp/alpha_e2e_{tag}.json")
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2))
    print(f"saved {out}")


if __name__ == "__main__":
    main()
