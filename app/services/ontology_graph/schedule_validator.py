"""독립 ScheduleValidator — 운영 solver 가 낸 **구체 근무표**를 규칙에 대해 독립 검증.

피드백 step2: production CP-SAT 이 생성한 근무표를 별도 validator 로 검사(인코딩 버그·모델
오류를 잡는 독립 검증기). graph 진단과 같은 원문 규칙을 **다른 방식**(구체 배열 replay)으로 검사.

지원 범위(scope_manifest)와 동일한 제약만 검증. 미지원 제약은 검증 못 함(스킵 + 표시).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from services.ontology_graph.frontier_dp import _options, _prep, _req, _step
from services.ontology_graph.lagrangian import _night_rules
from services.ontology_graph.scope_manifest import unmodeled_active


@dataclass
class ValidationResult:
    valid: bool
    violations: list = field(default_factory=list)
    unchecked: list = field(default_factory=list)   # 미지원이라 검증 못 한 제약


def validate_schedule(schedule: dict, nurses: list, config: dict,
                      num_days: int) -> ValidationResult:
    """schedule={nurse_id: [shift per day]} 를 검증. shift ∈ {D,E,N,O}.

    검사: ① 개인 시퀀스(회복·max run·not_one_night·전이·max연속근무 = 공통 automaton)
         ② per-day D/E/N 커버리지  ③ 셀 제약(banned/forced_off).
    """
    prepped = _prep(nurses, config)
    max_run, _, min_run = _night_rules(config)
    track_w = config.get("max_consecutive_work") is not None
    track_prev = bool(config.get("forbid_night_to_day"))
    reqD, reqE, reqN = _req(config, "D"), _req(config, "E"), _req(config, "N")
    viol: list = []

    # ① 개인 시퀀스 + ③ 셀 제약(_options 가 banned/forced_off 반영)
    for n in prepped:
        seq = schedule.get(n["nid"])
        if seq is None:
            viol.append({"kind": "missing_nurse", "nurse": n["nid"]})
            continue
        state = (0, 0, 0, "")
        for d in range(num_days):
            x = str(seq[d]).strip().upper()
            opts = _options(n, state, d, config, max_run, min_run)
            if x not in opts:
                viol.append({"kind": "sequence_or_cell", "nurse": n["nid"], "day": d,
                             "shift": x, "allowed": opts})
                break
            state = _step(state, x, config, track_w, track_prev)

    # ② 커버리지
    for d in range(num_days):
        cD = cE = cN = 0
        for n in prepped:
            seq = schedule.get(n["nid"])
            if not seq:
                continue
            x = str(seq[d]).strip().upper()
            cD += x == "D"
            cE += x == "E"
            cN += x == "N"
        for s, cnt, rq in (("D", cD, reqD), ("E", cE, reqE), ("N", cN, reqN)):
            if cnt < rq:
                viol.append({"kind": "coverage", "day": d, "shift": s,
                             "have": cnt, "need": rq})

    return ValidationResult(valid=not viol, violations=viol,
                            unchecked=unmodeled_active(nurses, config))
