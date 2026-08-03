"""Frontier DP 진단 엔진 — {D,E,N,O} exact 결합 추론 (미니 솔버 대체 PoC).

우리 N/notN 오토마톤이 놓치는 **정수-결합 잔여**(회복=실제 OFF, D/E/N 동시 커버리지,
전이 금지)를 exact 하게 판정한다. nurse×day 격자에서 **일별 joint 상태 frontier** 를
날짜순 전개(=variable elimination / bucket DP)해 붕괴하면 그 지점에서 **원인 certificate**
를 backpointer 로 추출한다.

- treewidth ≈ 병목에 얽힌 상태폭. |frontier| 이 cap 이하면 exact, 넘으면 UNKNOWN
  (→ 향후 separator 컴포넌트 분해로 폭을 낮춰 exact 유지).
- 판정만이 아니라 **왜**(회복 OFF 잠식 / 동시 커버리지 불가)를 낸다 = sound·행동가능.
- exact_oracle(DFS)와 **동일 semantics·독립 구현** → 교차검증(같은 결론이어야).

상태(간호사별): (r=야간run, k=회복 OFF 빚, w=연속근무, prev=직전shift).
w/prev 는 해당 규칙 활성 시에만 추적(미사용 시 0/'' 로 접어 폭 축소).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from itertools import product

from services.ontology_graph.certificate import (
    FEASIBLE,
    INFEASIBLE,
    UNKNOWN,
    Certificate,
    ProofNode,
)
from services.ontology_graph.lagrangian import _night_rules, _nurse_attr

_CAP = 200_000            # |frontier| 상한 — 넘으면 separator 폭 초과 → UNKNOWN
_EXPAND_BUDGET = 3_000_000  # 총 combo 전개 예산 — 초과 시 UNKNOWN(대형=컴포넌트 분해 대상)


@dataclass
class FrontierResult:
    status: str
    certificate: Certificate | None = None
    collapse_day: int | None = None
    width_max: int = 0
    reqs: tuple = field(default_factory=tuple)     # (D,E,N)


def _off_after(run: int, config: dict) -> int:
    if config.get("two_offs_after_three_nig") and run >= 3:
        return 2
    if config.get("two_offs_after_two_nig") and run >= 2:
        return 2
    return 1 if run >= 1 else 0


def _req(config: dict, shift: str) -> int:
    dsr = config.get("daily_shift_requirements") or {}
    if isinstance(dsr, dict) and dsr:
        return int(dsr.get(shift, 0) or 0)
    if shift == "N":
        return int(config.get("nig_req", 0) or 0)
    return 0


def _prep(nurses: list, config: dict) -> list[dict]:
    ic = config.get("initial_constraints") or {}
    fb = ic.get("forbidden") or {}
    fo = ic.get("forced_off") or {}
    out = []
    for nu in nurses:
        nid = str(_nurse_attr(nu, "nurse_id"))
        allowed = {str(x).strip().upper() for x in (_nurse_attr(nu, "allowed_shifts") or [])}
        work = (allowed & {"D", "E", "N"}) or {"D", "E", "N"}
        banned = {int(d): {str(c).strip().upper() for c in (codes or [])}
                  for d, codes in (fb.get(nid) or {}).items()}
        out.append({"nid": nid, "name": _nurse_attr(nu, "name") or nid, "work": work,
                    "banned": banned, "foff": {int(d) for d in (fo.get(nid) or [])}})
    return out


def _options(n: dict, state: tuple, day: int, config: dict, max_run: int, min_run: int):
    r, k, w, prev = state
    banned = n["banned"].get(day, set())
    must_work = "O" in banned
    forced_off = day in n["foff"]
    max_work = config.get("max_consecutive_work")
    if k > 0:
        return [] if (must_work or forced_off) else ["O"]
    if forced_off:
        if r > 0 and r < min_run:
            return []
        return [] if must_work else ["O"]
    if r > 0:
        opts = []
        if r + 1 <= max_run and "N" in n["work"] and "N" not in banned \
                and not (max_work and w + 1 > max_work):
            opts.append("N")
        if r >= min_run and not must_work:
            opts.append("O")
        return opts
    opts = []
    for s in ("D", "E", "N"):
        if s in n["work"] and s not in banned and not (max_work and w + 1 > max_work):
            opts.append(s)
    if not must_work and "O" not in banned:
        opts.append("O")
    if config.get("forbid_night_to_day") and prev == "N" and "D" in opts:
        opts.remove("D")
    return opts


def _step(state: tuple, s: str, config: dict, track_w: bool, track_prev: bool):
    r, k, w, prev = state
    w2 = (w + 1) if track_w else 0
    if s == "N":
        return (r + 1, 0, w2, "N" if track_prev else "")
    if s in ("D", "E"):
        return (0, 0, w2, s if track_prev else "")
    if k > 0:
        return (0, k - 1, 0, "")
    if r > 0:
        return (0, max(0, _off_after(r, config) - 1), 0, "")
    return (0, 0, 0, "")


def _can_work(n: dict, state: tuple, day: int, config: dict, max_run: int, min_run: int) -> bool:
    """그 날 이 간호사가 (OFF 아닌) 근무를 낼 수 있는가 — 회복빚/강제OFF면 불가."""
    return any(o != "O" for o in _options(n, state, day, config, max_run, min_run))


def _collapse_cert(frontier: set, day: int, config: dict, prepped: list,
                   reqs: tuple, max_run: int, min_run: int) -> Certificate:
    """붕괴일 day 에서 **왜** 를 backpointer 로 추출.

    ① 회복 강제 OFF 가 가용 인원을 잠식해 필요 슬롯 미달 → recovery_off_starvation
    ② 개별 슬롯은 되나 D∧E∧N 동시 배정 불가 → joint_sequencing_collapse
    surviving frontier 상태 전체에서 **가장 유리한**(가용 최대) 경우로 하한을 잡아 sound.
    """
    reqD, reqE, reqN = reqs
    total_req = reqD + reqE + reqN
    best_avail = -1
    best_off = None
    for js in frontier:
        avail = sum(1 for i, n in enumerate(prepped)
                    if _can_work(n, js[i], day, config, max_run, min_run))
        if avail > best_avail:
            best_avail = avail
            best_off = len(prepped) - avail
    if best_avail < total_req:                        # ① 인원 잠식(회복 OFF)
        return Certificate(
            kind="recovery_off_starvation", group_id=f"day:{day + 1}",
            capacity=best_avail, demand=total_req, deficit=total_req - best_avail,
            antecedents=[f"{day + 1}일: 회복(야간 후 실제 OFF)·강제 OFF 로 근무 가능 {best_avail}명 "
                         f"< 필요 슬롯 {total_req}(D{reqD}·E{reqE}·N{reqN})",
                         f"최소 {best_off}명이 그날 OFF 로 묶임"],
            witness={"day": day, "req": {"D": reqD, "E": reqE, "N": reqN}})
    # ② 인원은 되나 시퀀싱 결합으로 동시충족 불가
    return Certificate(
        kind="joint_sequencing_collapse", group_id=f"day:{day + 1}",
        capacity=best_avail, demand=total_req, deficit=1,
        antecedents=[f"{day + 1}일: 근무가능 {best_avail}명이나 D{reqD}·E{reqE}·N{reqN} 를 "
                     f"개인 시퀀스/전이 규칙과 **동시**에 만족하는 배정이 없음"],
        witness={"day": day, "req": {"D": reqD, "E": reqE, "N": reqN}})


def _interchangeable(prepped: list, day_lo: int, day_hi: int) -> bool:
    """[day_lo, day_hi) 구간에서 모든 간호사가 교환가능한가 — **동일 in-window 시그니처**.

    무제약일 필요는 없다. 같은 work-set + 같은 in-window banned/forced 패턴이면
    (동일하게 제약돼도) 교환가능 → 상태 정렬 축소가 sound.
    """
    def sig(n):
        work = frozenset(n["work"])
        banned = tuple(sorted((d, tuple(sorted(n["banned"][d])))
                              for d in n["banned"] if day_lo <= d < day_hi))
        foff = tuple(sorted(d for d in n["foff"] if day_lo <= d < day_hi))
        return (work, banned, foff)

    return len({sig(n) for n in prepped}) <= 1


def diagnose_frontier(nurses: list, config: dict, num_days: int,
                      cap: int = _CAP, symmetry: bool | None = None) -> FrontierResult:
    """{D,E,N,O} exact frontier DP 판정 + 붕괴 certificate.

    symmetry: 교환가능 간호사의 상태를 정렬(multiset)로 접어 순열 폭발 제거(sound).
              None=자동 감지(구간 전체 교환가능 시 활성).
    """
    prepped = _prep(nurses, config)
    max_run, rec_trig, min_run = _night_rules(config)
    track_w = config.get("max_consecutive_work") is not None
    track_prev = bool(config.get("forbid_night_to_day"))
    reqD, reqE, reqN = _req(config, "D"), _req(config, "E"), _req(config, "N")
    reqs = (reqD, reqE, reqN)
    k = len(prepped)
    if symmetry is None:
        symmetry = _interchangeable(prepped, 0, num_days)
    canon = (lambda s: tuple(sorted(s))) if symmetry else (lambda s: s)
    frontier: set = {canon(tuple((0, 0, 0, "") for _ in range(k)))}
    width_max = 1
    spent = 0
    for d in range(num_days):
        nxt: set = set()
        for js in frontier:
            per = [_options(prepped[i], js[i], d, config, max_run, min_run) for i in range(k)]
            if any(not o for o in per):
                continue                              # 이 joint 상태 사망
            span = 1
            for o in per:
                span *= len(o)
            spent += span
            if spent > _EXPAND_BUDGET:                # 전개 예산 초과(대형) → UNKNOWN
                return FrontierResult(UNKNOWN, width_max=width_max, reqs=reqs)
            for combo in product(*per):
                if sum(c == "D" for c in combo) < reqD:
                    continue
                if sum(c == "E" for c in combo) < reqE:
                    continue
                if sum(c == "N" for c in combo) < reqN:
                    continue
                nxt.add(canon(tuple(_step(js[i], combo[i], config, track_w, track_prev)
                                    for i in range(k))))
                if len(nxt) > cap:
                    return FrontierResult(UNKNOWN, width_max=len(nxt), reqs=reqs)
        if not nxt:
            cert = _collapse_cert(frontier, d, config, prepped, reqs, max_run, min_run)
            return FrontierResult(INFEASIBLE, certificate=cert, collapse_day=d,
                                  width_max=width_max, reqs=reqs)
        width_max = max(width_max, len(nxt))
        frontier = nxt
    return FrontierResult(FEASIBLE, width_max=width_max, reqs=reqs)


def diagnose_frontier_node(nurses: list, config: dict, num_days: int) -> ProofNode:
    """ProofNode 어댑터(certificate 프레임 통합)."""
    r = diagnose_frontier(nurses, config, num_days)
    if r.status == INFEASIBLE:
        return ProofNode(INFEASIBLE, certificate=r.certificate)
    return ProofNode(r.status)
