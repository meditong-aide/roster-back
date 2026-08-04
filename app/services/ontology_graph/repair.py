"""Graph repair → CP-SAT verify — certificate 기반 복구 후보를 이중 검증.

피드백 커밋6(최고 가치): infeasibility certificate 의 antecedent 로 복구 후보를 만들고,
**graph 로 domain_verified**, **CP-SAT 로 solver_verified** 로 구분한다. 사용자에게는
solver_verified 만 확정 해결책으로 노출(3-tier: suggested → domain_verified → solver_verified).

주의: 여기 solver_verified 는 IR shadow CP-SAT(지원 제약만). 실서비스는 운영 CP-SAT(전 제약)
으로 최종 검증해야 미지원 제약까지 반영된 진짜 solver_verified 다.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass


@dataclass
class RepairCandidate:
    action: str
    target: dict
    domain_verified: bool = False       # graph 재판정 통과
    solver_verified: bool | None = None  # CP-SAT 통과(None=미실행/미지원)


def _apply(config: dict, cand: dict) -> dict:
    cfg = copy.deepcopy(config)
    ic = cfg.setdefault("initial_constraints", {"forbidden": {}, "forced_off": {}})
    act = cand["action"]
    if act == "reduce_coverage":
        dsr = dict(cfg.get("daily_shift_requirements") or {})
        s = cand["target"]["shift"]
        dsr[s] = max(0, int(dsr.get(s, 0)) - 1)
        cfg["daily_shift_requirements"] = dsr
    elif act == "release_banned":
        fb = {k: dict(v) for k, v in (ic.get("forbidden") or {}).items()}
        nid, d = cand["target"]["nurse_id"], cand["target"]["day"]
        if nid in fb:
            fb[nid].pop(d, None)
            if not fb[nid]:
                fb.pop(nid)
        ic["forbidden"] = fb
    elif act == "release_forced_off":
        fo = {k: [x for x in v if x != cand["target"]["day"]]
              for k, v in (ic.get("forced_off") or {}).items()}
        ic["forced_off"] = {k: v for k, v in fo.items() if v}
    return cfg


def _candidates(config: dict, cert) -> list[dict]:
    out: list[dict] = []
    rej = (cert.witness.get("rejection") if cert and cert.witness else {}) or {}
    binding = rej.get("binding") or []
    for s in (binding or ["N"]):                 # 부족한 shift 수요 -1
        out.append({"action": "reduce_coverage", "target": {"shift": s}})
    ic = config.get("initial_constraints") or {}
    for nid, dm in (ic.get("forbidden") or {}).items():   # 금지 셀 해제
        for d in dm:
            out.append({"action": "release_banned", "target": {"nurse_id": str(nid), "day": int(d)}})
    for nid, days in (ic.get("forced_off") or {}).items():  # 강제 OFF 해제
        for d in days:
            out.append({"action": "release_forced_off",
                        "target": {"nurse_id": str(nid), "day": int(d)}})
    return out


def verify_repairs(nurses: list, config: dict, num_days: int,
                   max_candidates: int = 12, use_solver: bool = True) -> list[RepairCandidate]:
    """infeasible 이면 복구 후보 생성 → graph(domain) + CP-SAT(solver) 이중 검증. 아니면 []."""
    from services.ontology_graph.hybrid_solver import solve_hybrid
    base = solve_hybrid(nurses, config, num_days)
    if base.status != "INFEASIBLE_CERTIFIED":
        return []
    cands = _candidates(config, base.certificate)[:max_candidates]
    out: list[RepairCandidate] = []
    for c in cands:
        cfg2 = _apply(config, c)
        dv = solve_hybrid(nurses, cfg2, num_days).status != "INFEASIBLE_CERTIFIED"
        sv: bool | None = None
        if use_solver:
            from services.ontology_graph.roster_ir import compile_cpsat, parse_to_ir
            cp = compile_cpsat(parse_to_ir(nurses, cfg2, num_days))
            sv = (cp is True) if cp is not None else None
        out.append(RepairCandidate(c["action"], c["target"], domain_verified=dv,
                                   solver_verified=sv))
    # solver_verified 우선, 그다음 domain_verified
    out.sort(key=lambda r: (r.solver_verified is not True, not r.domain_verified))
    return out
