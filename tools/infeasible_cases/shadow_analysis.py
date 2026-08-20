"""Shadow 로그 offline 분석 — (request_id, attempt_id) join, mismatch 사유 분류.

피드백 fix1·5: input_hash 만으로 join 하면 같은 입력 재실행 시 잘못 붙는다 → (request_id,
attempt_id) 로 join, input_hash 는 "입력 동일" 검증 보조. 그리고 production FEASIBLE + graph
INFEASIBLE 을 무조건 false certificate 라 부르지 않는다 → **동일 stage·입력·in-scope·raw status**
확인된 경우만 GRAPH_FALSE_CERTIFICATE, 아니면 사유별 candidate_mismatch.

  MODEL_SCOPE_MISMATCH     — 미지원 제약 있음
  FALLBACK_STAGE_MISMATCH  — graph stage ≠ production attempt
  INPUT_VERSION_MISMATCH   — input_hash 불일치
  PRODUCTION_STATUS_INFERRED — production status_source≠raw(약한 근거) → 확정 못 함
  GRAPH_FALSE_CERTIFICATE  — 위 전부 통과했는데도 불일치 = 진짜 치명(반드시 0)

비교는 **primary_hard vs primary_hard**(graph 는 primary_hard 모델 분석)로만.
"""

from __future__ import annotations

import json
import sys
from collections import Counter


def parse_log(lines) -> list[dict]:
    out = []
    for ln in lines:
        i = ln.find("[Shadow] ")
        if i < 0:
            continue
        try:
            out.append(json.loads(ln[i + len("[Shadow] "):]))
        except Exception:
            pass
    return out


def join_by_run(records: list[dict]) -> dict:
    """(request_id, attempt_id, attempt_seq) → {graph, production}. 실행·solve 단위 join.

    한 generation 에서 solve 가 여러 번(재시도·probe) 기록돼도 attempt_seq 로 분리(피드백 fix1).
    """
    j: dict = {}
    for r in records:
        key = (r.get("request_id"), r.get("attempt_id"), r.get("attempt_seq", 1))
        if r.get("request_id") is None:
            continue
        slot = j.setdefault(key, {"graph": None, "production": None})
        if r.get("kind") == "production":
            slot["production"] = r
        elif "graph_status" in r:
            slot["graph"] = r
    return j


def _same_model(g: dict, p: dict) -> bool:
    gs, ps = g.get("model_signature"), p.get("model_signature")
    return bool(gs and ps and gs == ps)


def _classify_mismatch(g: dict, p: dict) -> str:
    """graph INFEASIBLE ∧ production primary_hard FEASIBLE 일 때 사유(엄격 순서)."""
    if g.get("unmodeled"):
        return "MODEL_SCOPE_MISMATCH"                # 미지원 제약(graph 가 볼 수 없는 것 존재)
    if not _same_model(g, p):
        return "MODEL_SIGNATURE_MISMATCH"            # 같은 hard model 비교 아님
    if g.get("input_hash") != p.get("input_hash"):
        return "INPUT_VERSION_MISMATCH"              # 입력 스냅샷 불일치
    if p.get("status_source") != "raw":
        return "PRODUCTION_STATUS_INFERRED"          # raw 아님 → 확정 못 함
    if p.get("production_validator_pass") is not True:
        return "GRAPH_FALSE_CERTIFICATE_UNVALIDATED"  # validator 미통과/미실행 → 아직 확정 못 함
    return "GRAPH_FALSE_CERTIFICATE"                 # 동일모델·입력·raw·validator PASS → 진짜 치명


def analyze(records: list[dict]) -> dict:
    joined = join_by_run(records)
    from services.ontology_graph.short_circuit import ALLOWED_SHORT_CIRCUIT_CERTS
    a = {
        "runs": len(joined), "paired": 0, "comparable_raw_pairs": 0,
        "graph_status": Counter(), "certificate": Counter(), "unknown_reason": Counter(),
        "prod_primary_status": Counter(), "mismatch_reason": Counter(),
        "agree_infeasible": 0, "graph_infeasible": 0, "prod_primary_infeasible": 0,
        "structurally_eligible": 0,
        # canary 판단용 3분할(피드백):
        "validated_infeasible_pairs": 0,       # graph INF ∧ prod raw INF ∧ 허용cert (validator 불필요)
        "unresolved_feasible_mismatches": 0,   # graph INF ∧ prod raw FEAS ∧ 미확정 → canary 차단
        "graph_false_certificate": 0,          # 위 + validator PASS = 진짜 치명
        "false_certificate_runs": [], "unresolved_runs": [],
    }
    for key, slot in joined.items():
        g, p = slot["graph"], slot["production"]
        struct = False
        if g:
            gs = g.get("graph_status", "?")
            a["graph_status"][gs] += 1
            if gs.startswith("UNKNOWN"):
                a["unknown_reason"][gs] += 1
            if g.get("certificate"):
                a["certificate"][g["certificate"]] += 1
            if gs == "INFEASIBLE_CERTIFIED":
                a["graph_infeasible"] += 1
                struct = (g.get("certificate") in ALLOWED_SHORT_CIRCUIT_CERTS
                          and not g.get("unmodeled"))
                if struct:
                    a["structurally_eligible"] += 1
        if p:
            a["prod_primary_status"][p.get("primary_hard_status", "?")] += 1
            if p.get("primary_hard_status") == "INFEASIBLE":
                a["prod_primary_infeasible"] += 1
        comparable = bool(
            g and p
            and g.get("input_hash") == p.get("input_hash")
            and _same_model(g, p)
            and p.get("status_source") == "raw")
        if g and p:
            a["paired"] += 1
        if comparable:
            a["comparable_raw_pairs"] += 1
        if g and p and g.get("graph_status") == "INFEASIBLE_CERTIFIED":
            ps = p.get("primary_hard_status")
            if ps == "INFEASIBLE":
                a["agree_infeasible"] += 1
                # short-circuit 검증 표본: 둘 다 INFEASIBLE = Validator 불필요.
                # (선택) certificate_replay_ok 로깅됐으면 True 인 것만.
                replay = g.get("certificate_replay_ok")
                if struct and comparable and replay is not False:
                    a["validated_infeasible_pairs"] += 1
            elif ps == "FEASIBLE":
                reason = _classify_mismatch(g, p)
                a["mismatch_reason"][reason] += 1
                if reason == "GRAPH_FALSE_CERTIFICATE":
                    a["graph_false_certificate"] += 1
                    a["false_certificate_runs"].append(key)
                else:
                    # 미확정 FEASIBLE 불일치도 canary 차단(무시 금지) — 원인 미확정이지 안전 아님
                    a["unresolved_feasible_mismatches"] += 1
                    a["unresolved_runs"].append((key, reason))
    return a


def report(a: dict) -> None:
    print(f"shadow 실행 {a['runs']} (페어 {a['paired']}, 비교가능 raw 페어 {a['comparable_raw_pairs']})\n")
    print("판정 정확성(동일 primary_hard·raw 기준):")
    print(f"  ★ GRAPH_FALSE_CERTIFICATE(동일모델·in-scope·raw 인데 불일치): {a['graph_false_certificate']}  ← 반드시 0")
    if a["false_certificate_runs"]:
        print(f"     반례 (run_id, attempt): {a['false_certificate_runs'][:5]}")
    print(f"  mismatch 사유 분류: {dict(a['mismatch_reason'])}")
    print(f"  graph INFEASIBLE ∧ prod primary INFEASIBLE 일치: {a['agree_infeasible']}/{a['graph_infeasible']}")
    print(f"\ngraph status 분포: {dict(a['graph_status'])}")
    print(f"UNKNOWN 사유: {dict(a['unknown_reason'])}  ← 재귀 hybrid 는 UNKNOWN_WIDTH 일 때만")
    print(f"production primary_hard 분포: {dict(a['prod_primary_status'])}")
    print(f"certificate 종류: {dict(a['certificate'])}")
    print("\nshort-circuit canary 판단(3분할):")
    print(f"  structurally_eligible(구조 후보): {a['structurally_eligible']}")
    print(f"  validated_infeasible_pairs(둘 다 INFEASIBLE=검증표본): {a['validated_infeasible_pairs']}")
    print(f"  ★ unresolved_feasible_mismatches(원인 미확정 FEASIBLE 불일치): "
          f"{a['unresolved_feasible_mismatches']}  ← canary 차단(0 이어야)")
    if a["unresolved_runs"]:
        print(f"     미확정 사례: {a['unresolved_runs'][:5]}")
    n = a["validated_infeasible_pairs"]
    canary_ok = (a["graph_false_certificate"] == 0
                 and a["unresolved_feasible_mismatches"] == 0 and n >= 300)
    if a["graph_false_certificate"] == 0 and n > 0:
        print(f"  실패 0건 기준 오류율 95% 상한 ≈ {3.0 / n:.1%} (rule of three, N={n})")
    print(f"  canary 착수 가능: {'예' if canary_ok else '아니오'} "
          f"(false-cert 0 ∧ unresolved 0 ∧ 검증표본≥300 ∧ 병동·규칙 분포 확인)")


def main(path):
    with open(path) as f:
        report(analyze(parse_log(f)))


if __name__ == "__main__":
    import os
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "app"))
    if len(sys.argv) < 2:
        print("usage: shadow_analysis.py <shadow.jsonl>")
    else:
        main(sys.argv[1])
