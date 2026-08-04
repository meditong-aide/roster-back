"""Graph repair → CP-SAT verify — certificate 기반 복구 후보를 이중 검증.

피드백 커밋6(최고 가치): infeasibility certificate 의 antecedent 로 복구 후보를 만들고,
graph(domain_verified) + verifier 로 이중검증. 3-tier:
  suggested → domain_verified(graph, FEASIBLE_WITNESS 일 때만) → solver_verified(운영 exact verifier).

교정(피드백): ① domain_verified 는 FEASIBLE_WITNESS 만 True(UNKNOWN=None, INFEASIBLE=False).
② 근사 shadow CP-SAT 결과는 solver_verified 가 아니라 **shadow_cpsat** 로 분리. solver_verified
는 운영 exact verifier(FeasibilityVerifier.exact=True)로만 부여.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass


@dataclass
class RepairCandidate:
    action: str
    target: dict
    domain_status: str = "UNKNOWN"       # graph 재판정 status(FEASIBLE/INFEASIBLE/UNKNOWN)
    domain_verified: bool | None = None  # True=FEASIBLE만, False=INFEASIBLE, None=UNKNOWN
    shadow_cpsat: bool | None = None     # 근사 shadow 결과(≠ solver_verified). production 검증 필요
    solver_verified: bool | None = None  # 운영 exact verifier 통과 시에만 True


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


def verify_repairs(nurses: list, config: dict, num_days: int, max_candidates: int = 12,
                   verifier=None) -> list[RepairCandidate]:
    """infeasible 이면 복구 후보 → graph(domain_verified: FEASIBLE만) + verifier 이중검증.

    verifier: FeasibilityVerifier. None=ShadowIRVerifier(근사→shadow_cpsat 채움). production
    exact verifier(exact=True) 주입 시에만 solver_verified 를 채운다.
    """
    from services.ontology_graph.hybrid_solver import solve_hybrid
    from services.ontology_graph.verifier import ShadowIRVerifier
    v = verifier or ShadowIRVerifier()
    base = solve_hybrid(nurses, config, num_days)
    if base.status != "INFEASIBLE_CERTIFIED":
        return []
    cands = _candidates(config, base.certificate)[:max_candidates]
    out: list[RepairCandidate] = []
    for c in cands:
        cfg2 = _apply(config, c)
        st = solve_hybrid(nurses, cfg2, num_days).status
        # 비대칭/UNKNOWN 락: FEASIBLE_WITNESS 만 domain_verified=True, UNKNOWN=None
        dv = True if st == "FEASIBLE_WITNESS" else (False if st == "INFEASIBLE_CERTIFIED" else None)
        vr = v.check(nurses, cfg2, num_days)
        rc = RepairCandidate(c["action"], c["target"], domain_status=st, domain_verified=dv)
        if vr.exact:                              # 운영 exact verifier → 진짜 solver_verified
            rc.solver_verified = (vr.feasible is True)
        else:                                     # 근사 shadow → shadow_cpsat 만
            rc.shadow_cpsat = (vr.feasible is True) if vr.feasible is not None else None
        out.append(rc)
    # 확정순: solver_verified → domain_verified → shadow
    out.sort(key=lambda r: (r.solver_verified is not True, r.domain_verified is not True,
                            r.shadow_cpsat is not True))
    return out
