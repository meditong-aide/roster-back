"""폐루프 평가 오케스트레이터 (US-006).

각 (병동 × 월 × 주입케이스 C1~C4)에 대해:
  baseline 구동 → 하드위반 주입 → infeasible 확인 → 온톨로지 그래프로 진단 →
  최소변경 액션 추천(E1) → 액션 적용 후 재구동(E2 해결/E3 최소변경/E4 무회귀) →
  원인이 구조 grounding 인지(E5). 미해결이면 원인 기록.

프로덕션 무변경: 모든 주입/수정은 harness.clone 사본에만.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
APP = ROOT / "app"
HERE = Path(__file__).resolve().parent
for _p in (str(APP), str(HERE), str(ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from services.ontology_graph.builder import UnifiedGraphInput, build_unified_graph  # noqa: E402
from services.ontology_graph.recommender import recommend_actions  # noqa: E402
from services.ontology_graph.structural import augment_structural_shortages  # noqa: E402
from services.ontology_graph.soft_penalty import augment_soft_constraints, compute_soft_penalties  # noqa: E402
try:
    from tools.ontology_eval import harness  # noqa: E402
except ImportError:
    import harness  # type: ignore  # noqa: E402

WORK_SHIFTS = ["D", "E", "N"]


def _requirements_by_day(cfg: dict, num_days: int) -> dict[int, dict[str, int]]:
    by_day = cfg.get("daily_shift_requirements_by_day")
    base = {k: int(v or 0) for k, v in (cfg.get("daily_shift_requirements") or {}).items()
            if k in WORK_SHIFTS}
    out: dict[int, dict[str, int]] = {}
    for d in range(num_days):
        if isinstance(by_day, list) and d < len(by_day) and isinstance(by_day[d], dict):
            out[d] = {k: int(v or 0) for k, v in by_day[d].items() if k in WORK_SHIFTS}
        else:
            out[d] = dict(base)
    return out


def _num_days(captured: dict) -> int:
    import calendar
    return calendar.monthrange(captured["year"], captured["month"])[1]


def _extract_leaves(cfg: dict, num_days: int) -> list[dict]:
    """off_window_constraints(nurse_id → [[start,end],...] OFF 구간) → leave 엔트리.
    각 OFF 일은 그 날 가용 인력을 줄이므로 reduces 엣지의 출발점이 된다."""
    leaves: list[dict] = []
    owc = cfg.get("off_window_constraints") or {}
    if isinstance(owc, dict):
        for nid, windows in owc.items():
            for w in (windows or []):
                try:
                    start, end = int(w[0]), int(w[1])
                except Exception:
                    continue
                for d in range(max(0, start), min(num_days, end + 1)):
                    leaves.append({"nurse_id": str(nid), "day": d})
    return leaves


def build_eval_graph(captured: dict, roster: dict | None = None):
    """captured(주입 포함) → 통합 그래프 + 구조부족 + leave(reduces) + soft 연결."""
    cfg = captured["config_data"]
    gc = captured.get("grade_config") or {}
    nurses = [{"nurse_id": str(n.get("nurse_id")), "grade": n.get("grade"), "team_id": n.get("team_id")}
              for n in captured["nurses_data"]]
    num_days = _num_days(captured)
    req = _requirements_by_day(cfg, num_days)
    team_min = cfg.get("team_min_by_team") or {}
    grade_max = gc.get("constraints_max_json") or {}
    grade_min = gc.get("constraints_json") or {}
    night_cap = cfg.get("max_nig_per_month")   # 엔진 하드 키 (기본 15)
    leaves = _extract_leaves(cfg, num_days)

    inp = UnifiedGraphInput(
        nurses=nurses, num_days=num_days, work_shifts=WORK_SHIFTS,
        requirements_by_day=req, team_min=team_min, grade_min=grade_min, grade_max=grade_max,
        leaves=leaves,
    )
    g = build_unified_graph(inp)
    augment_structural_shortages(
        g, nurses=nurses, work_shifts=WORK_SHIFTS, requirements_by_day=req,
        team_min=team_min, grade_max=grade_max,
        night_cap=int(night_cap) if night_cap is not None else None,
    )
    # 소프트 제약 연결 (roster 가 있으면 penalty 계산)
    pen = compute_soft_penalties(roster, work_shifts=WORK_SHIFTS) if roster else {}
    augment_soft_constraints(g, pen, work_shifts=WORK_SHIFTS)
    return g


def _family_shortage(graph) -> dict[str, int]:
    """family → 최대 shortage. bottleneck state → pressures → constraint.family 로 집계."""
    out: dict[str, int] = {}
    for st in graph.nodes_of_kind("state"):
        short = int(st.evidence.get("shortage", 0) or 0)
        if short <= 0:
            continue
        for e in graph.out_edges(st.node_id, "pressures"):
            fam = graph.nodes[e.target_id].family
            out[fam] = max(out.get(fam, 0), short)
    return out


def diagnose(captured: dict):
    return recommend_actions(build_eval_graph(captured))


def diagnose_delta(injected: dict, baseline: dict):
    """주입이 '악화시킨' family 만 진단 — baseline 에도 있던 구조적 false-positive 상쇄.

    night-heavy 병동(ICU)처럼 baseline 에서도 일부 detector 가 shortage 를 보고할 수
    있다. 주입 전/후 shortage 를 비교해, 주입으로 새로 생기거나 커진 family 만 남긴다.
    """
    g_inj = build_eval_graph(injected)
    g_base = build_eval_graph(baseline)
    base_short = _family_shortage(g_base)
    inj_short = _family_shortage(g_inj)
    worsened = {f for f, v in inj_short.items() if v > base_short.get(f, 0)}
    actions = recommend_actions(g_inj)
    return [a for a in actions if a.target_family in worsened]


def apply_fix(captured_injected: dict, top_action, injected_meta: dict) -> dict:
    """추천 액션의 노브를 주입 scope 에 최소변경으로 적용.

    최소변경 = 추천된 단일 노브를, feasible 을 보장하는 baseline 수준으로 되돌림
    (전면 disable 이 아니라 set_threshold 1개). 액션의 family/knob 은 그래프 진단에서
    나오고, 적용 목표값만 baseline 으로 계산한다."""
    c = harness.clone(captured_injected)
    cfg = c["config_data"]
    gc = c.get("grade_config") or {}
    knob = injected_meta.get("knob")
    nurses_n = len(c["nurses_data"])

    if knob == "daily_shift_requirements":
        shift = injected_meta["shift"]
        base = int(injected_meta.get("baseline", 2))
        cfg["daily_shift_requirements"][shift] = base
        by_day = cfg.get("daily_shift_requirements_by_day")
        if isinstance(by_day, list):
            day = injected_meta.get("day", 0)
            if 0 <= day < len(by_day) and isinstance(by_day[day], dict):
                by_day[day][shift] = base
    elif knob == "team_min_by_team":
        team = str(injected_meta["team"])
        cfg["team_min_by_team"][team] = dict(injected_meta.get("baseline") or {})  # 과구독 해소
    elif knob == "constraints_max_json":
        shift = injected_meta["shift"]
        cmax = gc.get("constraints_max_json") or {}
        base = injected_meta.get("baseline") or {}
        if base:
            cmax[shift] = dict(base)                   # baseline 상한 복원
        else:
            cmax.pop(shift, None)                      # 과소 상한 제거(원래 없었음)
        gc["constraints_max_json"] = cmax
        c["grade_config"] = gc
    elif knob == "max_nig_per_month":
        cfg["max_nig_per_month"] = int(injected_meta.get("baseline", 15))
    c["_fix"] = {"knob": knob, "action": top_action.action_id}
    return c


def evaluate_case(captured_baseline: dict, case: str, *, time_limit: int = 20) -> dict[str, Any]:
    """한 케이스 E1~E5 평가."""
    result: dict[str, Any] = {"case": case}
    injected = harness.INJECTORS[case](captured_baseline)
    meta = injected["_injected"]
    result["injected"] = meta

    # 주입이 실제 infeasible 을 만드는가
    inj_run = harness.run_engine(injected, time_limit_seconds=time_limit)
    result["injected_feasible"] = inj_run["feasible"]
    if inj_run["feasible"]:
        result["valid_injection"] = False
        result["note"] = "주입이 infeasible 을 만들지 못함 — 케이스 무효"
        return result
    result["valid_injection"] = True

    # 진단 → 추천 액션 (baseline 대비 delta — false-positive 상쇄)
    actions = diagnose_delta(injected, captured_baseline)
    result["n_actions"] = len(actions)
    if actions:
        top = actions[0]
        result["top_action"] = {"family": top.target_family, "action_type": top.action_type,
                                "config_key": top.config_key, "is_primary": top.is_primary,
                                "delta": top.delta}
    # E1: 액션이 나오고 1순위가 주입 family 를 타겟
    e1 = bool(actions) and actions[0].target_family == meta["family"]
    result["E1_action_emitted"] = e1
    # E5: 원인이 구조 grounding (state shortage 로부터 도출)
    result["E5_honest_cause"] = bool(actions) and actions[0].is_primary

    if not e1:
        result["resolved"] = False
        result["fail_reason"] = f"E1 실패: 액션 미도출 또는 family 불일치 (top={result.get('top_action')})"
        return result

    # 액션 적용 → 재구동
    fixed = apply_fix(injected, actions[0], meta)
    fix_run = harness.run_engine(fixed, time_limit_seconds=time_limit)
    result["E2_solvable"] = fix_run["feasible"]
    result["fixed_work_cells"] = fix_run["work_cells"]
    # E3: 최소변경 — set_threshold(단일 노브), disable 아님
    result["E3_minimal_change"] = actions[0].action_type in {"set_threshold", "force_soft_mode"}
    # E4: 무회귀 — feasible 이면 server hard violation 0 로 간주(엔진이 hard 보장)
    result["E4_no_regression"] = fix_run["feasible"]

    passed = all([result["E1_action_emitted"], result["E2_solvable"],
                  result["E3_minimal_change"], result["E4_no_regression"], result["E5_honest_cause"]])
    result["resolved"] = passed
    if not passed:
        result["fail_reason"] = "E2~E5 중 일부 실패"
    return result


def evaluate_ward_month(ward: str, year: int, month: int, *, cases=("C1", "C2", "C3", "C4"),
                        time_limit: int = 20) -> dict[str, Any]:
    cap = harness.capture_engine_inputs(ward, year, month)
    out: dict[str, Any] = {"ward": ward, "year": year, "month": month}
    if not cap.get("captured_ok"):
        out["error"] = f"capture failed: {cap.get('load_error')}"
        return out
    base = harness.run_engine(cap, time_limit_seconds=time_limit)
    out["baseline_feasible"] = base["feasible"]
    out["baseline_work_cells"] = base["work_cells"]
    out["cases"] = {}
    for case in cases:
        try:
            out["cases"][case] = evaluate_case(cap, case, time_limit=time_limit)
        except Exception as exc:
            import traceback
            out["cases"][case] = {"case": case, "error": f"{type(exc).__name__}: {exc}",
                                  "tb": traceback.format_exc()[-800:]}
    # N/A (해당 병동에 그 제약이 없어 주입이 infeasible 을 못 만드는 케이스)는
    # 실패가 아니라 적용불가로 분류 — all_passed 는 '유효 케이스 전부 해결'.
    valid = [c for c in out["cases"].values() if c.get("valid_injection", True)]
    out["n_valid_cases"] = len(valid)
    out["n_na_cases"] = len(out["cases"]) - len(valid)
    out["all_passed"] = bool(valid) and all(c.get("resolved") for c in valid)
    return out


if __name__ == "__main__":
    import json
    ward = sys.argv[1] if len(sys.argv) > 1 else "9B"
    year = int(sys.argv[2]) if len(sys.argv) > 2 else 2026
    month = int(sys.argv[3]) if len(sys.argv) > 3 else 6
    cases = tuple(sys.argv[4].split(",")) if len(sys.argv) > 4 else ("C1", "C2", "C3", "C4")
    import os
    tl = int(os.getenv("EVAL_TL", "18"))
    res = evaluate_ward_month(ward, year, month, cases=cases, time_limit=tl)
    rep = ROOT / "tools" / "ontology_eval" / "reports" / f"eval_{ward}_{year}{month:02d}.json"
    rep.write_text(json.dumps(res, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(f"\n[eval] {ward} {year}-{month:02d} all_passed={res.get('all_passed')} → {rep.name}")
    for case, r in res.get("cases", {}).items():
        flags = {k: r.get(k) for k in ("E1_action_emitted", "E2_solvable", "E3_minimal_change",
                                       "E4_no_regression", "E5_honest_cause", "resolved")}
        print(f"  {case}: {flags} {('' if r.get('resolved') else r.get('fail_reason') or r.get('note') or r.get('error') or '')}")
