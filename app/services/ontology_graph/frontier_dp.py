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


# ── BoundaryState 계약 + relation-form message passing (hybrid 의 전제) ───────────
#
# BoundaryState(간호사별) = (r, k, w, prev):
#   r=연속 야간(consecutive_nights), k=회복 OFF 잔여(recovery_off_debt),
#   w=연속 근무(consecutive_work), prev=직전 shift(전이 규칙용).
# joint BoundaryState = 간호사별 상태의 튜플. component 를 자를 때 separator 가 나르는 것은
# 단순 근무값이 아니라 **이 joint BoundaryState** 다(그래야 양쪽이 이어짐). remaining_quota 는
# 아직 미모델(월 quota 미구현) — 추가 시 상태에 편입 필요.
#
# 월초 진입 = fresh (r=k=w=0). 월말 terminal 은 아래 terminal_ok 로 규정.

def fresh_joint(k: int) -> tuple:
    return tuple((0, 0, 0, "") for _ in range(k))


def terminal_ok(joint_state: tuple, config: dict) -> bool:
    """월말(또는 자기완결 구간) 종료 허용 상태? strict 모델: 회복빚 없고 너무 짧은 열린 run 없음."""
    _, _, min_run = _night_rules(config)
    for (r, k, w, prev) in joint_state:
        if k > 0:                       # 회복 OFF 미상환
            return False
        if 0 < r < min_run:             # min_run 미만 열린 야간 run
            return False
    return True


@dataclass
class RejectionStats:
    """붕괴 지점 최소 proof trace(처음부터 심음 — 나중에 설명 붙이기 위해)."""
    day: int
    dead_states: int          # 시퀀스로 사망한(옵션 0) 진입 상태 수
    live_states: int
    best_cov: tuple           # (maxD, maxE, maxN) 그날 달성 가능 최대 커버리지
    reqs: tuple
    binding: tuple            # 부족했던 shift 들 (예: ("N",))
    frontier: frozenset = frozenset()   # 붕괴 직전 생존 joint 상태
    terminal: bool = False


@dataclass
class MessageResult:
    """component interface message: 진입 frontier → 출구 frontier 관계(exact)."""
    exit_frontier: frozenset
    collapse: RejectionStats | None
    width_max: int
    overflow: bool = False


def frontier_message(prepped: list, config: dict, day_lo: int, day_hi: int,
                     entry: set, *, symmetry: bool | None = None, cap: int = _CAP,
                     budget: list | None = None, terminal: str = "lenient",
                     day_reqs: dict | None = None) -> MessageResult:
    """[day_lo, day_hi) 를 sweep — **진입 BoundaryState 집합 → 출구 BoundaryState 집합**.

    이것이 component 를 잇는 message(M(entry, sep) → exit). 붕괴 시 exit 비고 RejectionStats.
    budget 은 공유 가능한 mutable([int]). day_reqs: {day: (rD,rE,rN)} per-day 커버리지 override
    (component 가 담당하는 날만 부과; 없으면 config 균일값). hybrid 에서 component 별로 씀.
    """
    max_run, rec_trig, min_run = _night_rules(config)
    track_w = config.get("max_consecutive_work") is not None
    track_prev = bool(config.get("forbid_night_to_day"))
    reqD0, reqE0, reqN0 = _req(config, "D"), _req(config, "E"), _req(config, "N")
    reqs = (reqD0, reqE0, reqN0)
    k = len(prepped)
    if symmetry is None:
        symmetry = _interchangeable(prepped, day_lo, day_hi)
    canon = (lambda s: tuple(sorted(s))) if symmetry else (lambda s: s)
    frontier: set = {canon(s) for s in entry}
    width_max = len(frontier)
    if budget is None:
        budget = [_EXPAND_BUDGET]
    for d in range(day_lo, day_hi):
        reqD, reqE, reqN = day_reqs[d] if (day_reqs and d in day_reqs) else (reqD0, reqE0, reqN0)
        nxt: set = set()
        dead = live = 0
        maxD = maxE = maxN = 0
        for js in frontier:
            per = [_options(prepped[i], js[i], d, config, max_run, min_run) for i in range(k)]
            if any(not o for o in per):
                dead += 1
                continue
            live += 1
            span = 1
            for o in per:
                span *= len(o)
            budget[0] -= span
            if budget[0] < 0:
                return MessageResult(frozenset(), None, width_max, overflow=True)
            for combo in product(*per):
                cD = sum(c == "D" for c in combo)
                cE = sum(c == "E" for c in combo)
                cN = sum(c == "N" for c in combo)
                if cD > maxD:
                    maxD = cD
                if cE > maxE:
                    maxE = cE
                if cN > maxN:
                    maxN = cN
                if cD >= reqD and cE >= reqE and cN >= reqN:
                    nxt.add(canon(tuple(_step(js[i], combo[i], config, track_w, track_prev)
                                        for i in range(k))))
                    if len(nxt) > cap:
                        return MessageResult(frozenset(), None, len(nxt), overflow=True)
        if not nxt:
            binding = tuple(s for s, mx, rq in (("D", maxD, reqD), ("E", maxE, reqE),
                                                ("N", maxN, reqN)) if mx < rq)
            rej = RejectionStats(d, dead, live, (maxD, maxE, maxN), (reqD, reqE, reqN),
                                 binding, frozenset(frontier))
            return MessageResult(frozenset(), rej, width_max)
        width_max = max(width_max, len(nxt))
        frontier = nxt
    if terminal == "strict":
        filt = {s for s in frontier if terminal_ok(s, config)}
        if not filt:
            rej = RejectionStats(day_hi - 1, 0, len(frontier), (0, 0, 0), reqs, (),
                                 frozenset(frontier), terminal=True)
            return MessageResult(frozenset(), rej, width_max)
        frontier = filt
    return MessageResult(frozenset(frontier), None, width_max)


def diagnose_frontier(nurses: list, config: dict, num_days: int,
                      cap: int = _CAP, symmetry: bool | None = None,
                      terminal: str = "lenient") -> FrontierResult:
    """{D,E,N,O} exact frontier DP 판정 + 붕괴 certificate. (frontier_message 의 fresh→end 특수화.)

    symmetry: 교환가능 간호사 상태 정렬 축소(None=자동). terminal: lenient(cross-month)|strict(자기완결).
    """
    prepped = _prep(nurses, config)
    max_run, rec_trig, min_run = _night_rules(config)
    reqs = (_req(config, "D"), _req(config, "E"), _req(config, "N"))
    k = len(prepped)
    mr = frontier_message(prepped, config, 0, num_days, {fresh_joint(k)},
                          symmetry=symmetry, cap=cap, terminal=terminal)
    if mr.overflow:
        return FrontierResult(UNKNOWN, width_max=mr.width_max, reqs=reqs)
    if mr.collapse is not None:
        rej = mr.collapse
        cert = _collapse_cert(rej.frontier, rej.day, config, prepped, reqs, max_run, min_run)
        cert.witness["rejection"] = {"day": rej.day, "dead_states": rej.dead_states,
                                     "live_states": rej.live_states, "best_cov": rej.best_cov,
                                     "binding": list(rej.binding), "terminal": rej.terminal}
        return FrontierResult(INFEASIBLE, certificate=cert, collapse_day=rej.day,
                              width_max=mr.width_max, reqs=reqs)
    return FrontierResult(FEASIBLE, width_max=mr.width_max, reqs=reqs)


def diagnose_frontier_node(nurses: list, config: dict, num_days: int) -> ProofNode:
    """ProofNode 어댑터(certificate 프레임 통합)."""
    r = diagnose_frontier(nurses, config, num_days)
    if r.status == INFEASIBLE:
        return ProofNode(INFEASIBLE, certificate=r.certificate)
    return ProofNode(r.status)
