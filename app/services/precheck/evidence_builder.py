"""EvidenceNode 빌더.

설계 원칙 (사용자 핵심 요구):
  EvidenceNode 는 '적용된 treatment 의 검증 결과' 캡슐이다.
  cause_id / reason_code / human_message 를 evidence 안에 절대 포함하지 않는다 (테스트로 검증).
  evidence 가 cause 처럼 노출되면 안 된다.

필드:
  status            : FEASIBLE | INFEASIBLE | PARTIAL | NOT_ATTEMPTED
  applied_treatments: 적용된 relaxation/treatment id list (예: ["grade_hard_to_soft"])
  proof_type        : 검증의 강도
                        - cp_sat_unsat_core_heuristic : sufficient_assumptions, minimized-not-minimal
                        - re_solve_witness            : 재실행으로 schedule 생성 성공
                        - arithmetic_certificate      : precheck 의 closed-form 증명 (minimal 보장)
                        - not_attempted               : 시도 안 함
  is_minimal        : true 면 core 가 진정한 minimal MUS. CP-SAT heuristic 은 false.
  core_size         : unsat core 의 member 수 합계
  witness_schedule_id: 성공 시 생성된 schedule_id. 실패면 None.
  violation_delta   : cause/symptom 별 {before, after} count. 0 으로 떨어진 cause 가 진짜 해결된 것.
  verified          : status==FEASIBLE AND witness_schedule_id 있고 모든 cause violation 이 0.

EvidenceNode 가 cause-bucket 과 절대 섞이지 않도록, 본 함수 출력은 reason_code/cause_id key 를 절대 포함하지 않는다.
"""

from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional


EvidenceStatus = Literal["FEASIBLE", "INFEASIBLE", "PARTIAL", "NOT_ATTEMPTED"]
ProofType = Literal[
    "cp_sat_unsat_core_heuristic",
    "re_solve_witness",
    "arithmetic_certificate",
    "not_attempted",
]


# 출력에 절대 들어가면 안 되는 키 — cause/symptom 이라는 의미가 evidence 에 새는 것 차단.
_FORBIDDEN_KEYS = frozenset({"reason_code", "cause", "causes", "cause_id", "symptom", "symptoms", "human_message_ko"})


def _count_core_members(conflict_cores: List[Dict[str, Any]] | None) -> int:
    total = 0
    for c in conflict_cores or []:
        if not isinstance(c, dict):
            continue
        members = c.get("members") or []
        if isinstance(members, list):
            total += len(members)
    return total


def build_evidence_node(
    *,
    applied_relaxations: Optional[List[str]] = None,
    conflict_cores: Optional[List[Dict[str, Any]]] = None,
    status: EvidenceStatus = "INFEASIBLE",
    proof_type: ProofType = "cp_sat_unsat_core_heuristic",
    witness_schedule_id: Optional[str] = None,
    violation_delta: Optional[Dict[str, Dict[str, int]]] = None,
) -> Dict[str, Any]:
    core_size = _count_core_members(conflict_cores)

    # is_minimal: arithmetic_certificate 만 minimal. CP-SAT sufficient_assumptions 는 minimized-not-minimal.
    is_minimal = (proof_type == "arithmetic_certificate")

    # verified: FEASIBLE 이고 witness 있고, violation_delta 가 비어있거나 모두 after==0.
    verified = False
    if status == "FEASIBLE" and witness_schedule_id:
        if not violation_delta:
            verified = True
        else:
            verified = all(
                int((d or {}).get("after") or 0) == 0
                for d in (violation_delta.values() if isinstance(violation_delta, dict) else [])
            )

    out: Dict[str, Any] = {
        "status": status,
        "applied_treatments": list(applied_relaxations or []),
        "proof_type": proof_type,
        "is_minimal": is_minimal,
        "core_size": core_size,
        "witness_schedule_id": witness_schedule_id,
        "violation_delta": dict(violation_delta or {}),
        "verified": verified,
    }

    # 금지 키가 새어들어왔는지 방어적 검증 (개발자 실수 차단).
    for k in _FORBIDDEN_KEYS:
        if k in out:
            raise ValueError(f"EvidenceNode 출력에 금지 키가 포함됨: {k}")

    return out
