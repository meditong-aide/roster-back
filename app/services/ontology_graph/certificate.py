"""Typed 도메인 infeasibility certificate + proof tree.

각 진단기(max-flow / 개인 automaton / 시간창·N-pool DP)가 단순 True/False 가 아니라
**동일 구조의 수치 certificate** 를 반환한다:
  group(어디) · capacity(최대공급) · demand(수요) · deficit(부족) · antecedents(왜 줄었나)
  · witness(누구·언제).
정수 분기에서 나온 leaf certificate 들을 ProofNode 로 묶고, 형제 분기를 **부모 설명으로
병합**한다. IIS 최소화 없이 sound·행동가능한 원인 설명을 만드는 게 목적.

soundness 규율: certificate 는 **증명된 것만**(max-flow min-cut, automaton 경로소멸,
시간창 최대공급, 완전배정 위반). 휴리스틱 점수는 certificate 가 아니다. 상태는 3개:
INFEASIBLE_CERTIFIED / FEASIBLE_WITNESS / UNKNOWN — UNKNOWN 을 FEASIBLE 로 취급 금지.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

INFEASIBLE = "INFEASIBLE_CERTIFIED"
FEASIBLE = "FEASIBLE_WITNESS"
UNKNOWN = "UNKNOWN"


@dataclass
class Certificate:
    kind: str                       # sequence_path_empty | night_coverage_deficit | ...
    group_id: str                   # "nurse:298699:days:10-13" | "night:days:13-15"
    capacity: float                 # 그 그룹이 낼 수 있는 최대(공급)
    demand: float                   # 요구
    deficit: float                  # demand - capacity (>0 이면 증명된 병목)
    antecedents: list[str] = field(default_factory=list)   # 공급이 준 이유(sound)
    witness: dict[str, Any] = field(default_factory=dict)  # nurses/days/shift

    def one_line(self) -> str:
        w = ""
        if self.deficit > 0:
            w = f" (필요 {self.demand:g}, 최대 {self.capacity:g}, 부족 {self.deficit:g})"
        return f"[{self.group_id}]{w}"


@dataclass
class ProofNode:
    status: str                                   # INFEASIBLE / FEASIBLE / UNKNOWN
    branch_literal: Optional[str] = None          # 이 노드가 분기한 결정(예: "298699@13=N")
    certificate: Optional[Certificate] = None     # leaf 인증서
    children: list["ProofNode"] = field(default_factory=list)  # [literal=0, literal=1]

    def depth(self) -> int:
        return 1 + max((c.depth() for c in self.children), default=0)

    def size(self) -> int:
        return 1 + sum(c.size() for c in self.children)


def _cert_phrase(c: Certificate) -> str:
    """leaf certificate → 사람이 읽는 짧은 사유."""
    if c.kind == "sequence_path_empty":
        who = c.witness.get("name") or c.group_id
        return f"{who} 개인 근무 배열이 야간 회복 규칙과 충돌(가능한 배열 없음)"
    if c.kind == "night_coverage_deficit":
        d = c.witness.get("day")
        day = f"{d + 1}일 " if isinstance(d, int) else ""
        return f"{day}야간 인원 {int(c.deficit)}명 부족(필요 {int(c.demand)}, 가능 {int(c.capacity)})"
    if c.kind == "night_supply_deficit":
        return (f"야간 가능 인원의 월 최대 공급 {int(c.capacity)} < 필요 {int(c.demand)} "
                f"→ {int(c.deficit)} 부족")
    if c.kind == "joint_frontier_empty":
        d = c.witness.get("day")
        day = f"{d + 1}일" if isinstance(d, int) else "특정일"
        return (f"결합 배열이 {day}에 붕괴 — 그날 낼 수 있는 최대 야간 {int(c.capacity)} "
                f"< 필요 {int(c.demand)}(각자는 가능하나 함께는 불가)")
    if c.kind == "recovery_off_starvation":
        d = c.witness.get("day")
        day = f"{d + 1}일" if isinstance(d, int) else "특정일"
        return (f"{day}: 야간 회복(실제 OFF)·강제 OFF 가 인원을 잠식 — 근무 가능 "
                f"{int(c.capacity)}명 < 필요 슬롯 {int(c.demand)}(회복 OFF vs 커버리지 충돌)")
    if c.kind == "joint_sequencing_collapse":
        d = c.witness.get("day")
        day = f"{d + 1}일" if isinstance(d, int) else "특정일"
        return (f"{day}: 인원은 충분하나 개인 시퀀스·전이 규칙과 D/E/N 을 **동시**에 "
                f"만족하는 배정이 없음(정수 결합 붕괴)")
    return c.one_line()


def render_explanation(node: ProofNode) -> str:
    """ProofNode → 사용자 설명. 분기 노드는 형제 사유를 '어느 쪽이든' 으로 병합."""
    if node.status != INFEASIBLE:
        return {"FEASIBLE_WITNESS": "이 축은 충족 가능(원인 아님)",
                "UNKNOWN": "이 축으로는 판정 못 함(솔버로 이관)"}.get(node.status, "")
    # leaf
    if node.certificate is not None and not node.children:
        return _cert_phrase(node.certificate)
    # 분기: children[0]=결정 0, children[1]=결정 1
    lit = node.branch_literal or "그 배정"
    parts = []
    for ch, side in zip(node.children, ("안 하면", "하면")):
        sub = render_explanation(ch)
        parts.append(f"{lit} {side} — {sub}")
    body = "; 반대로 ".join(parts) if len(parts) == 2 else "; ".join(parts)
    return f"{body}. 따라서 어느 선택도 불가능합니다."
