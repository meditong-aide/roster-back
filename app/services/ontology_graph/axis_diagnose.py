"""다축 surplus 진단 — N-시퀀스 너머로 certificate 프레임 일반화.

기존 max-flow(presolve)의 per-shift 부족(증명된 하한)을 Certificate 로 감싸고, N-시퀀스
branch-infer 와 합쳐 **argmin-surplus(=최대 deficit) 그룹**을 지목한다. "어느 그룹에서
문제인가"를 D/E/N 커버리지 + N 시퀀스·결합에 걸쳐 sound 하게 국소화(솔버 불필요).

경계(정직): 커버리지 certificate 는 max-flow 하한(용량형, sound). 다축 **결합-정수** 잔여
(분수는 되는데 정수 안 됨, D/E/grade/team 얽힘)는 여기 안 잡히고 → 완전 leaf checker(솔버)
= branch-and-check 로 이관. 이 모듈은 그 앞단(용량·시퀀스 축) 통합까지.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from services.ontology_graph.branch_infer import diagnose_night_axis
from services.ontology_graph.certificate import INFEASIBLE, Certificate, ProofNode

_SHIFT_KO = {"D": "주간", "E": "이브닝", "N": "야간"}


@dataclass
class AxisDiagnosis:
    status: str                                   # INFEASIBLE / FEASIBLE / UNKNOWN
    primary: Certificate | None = None            # argmin surplus(=최대 deficit)
    certificates: list[Certificate] = field(default_factory=list)
    night: ProofNode | None = None                # N축 proof-tree(있으면)


def coverage_certificates(nurses: list, config: dict, year: int, month: int) -> list[Certificate]:
    """max-flow(presolve) per-shift 부족 → coverage_deficit certificate(증명된 하한)."""
    try:
        from services.ontology_graph.presolve_diagnosis import presolve_shortage_diagnosis
        diag = presolve_shortage_diagnosis(nurses, config, year, month)
    except Exception:
        return []
    out: list[Certificate] = []
    for s in diag.get("shortages", []):
        dfc = int(s.get("monthly_shortage_lower_bound") or 0)
        if dfc <= 0:
            continue
        sh = str(s.get("shift"))
        out.append(Certificate(
            kind="coverage_deficit", group_id=f"shift:{sh}",
            capacity=int(s.get("monthly_fillable") or 0),
            demand=int(s.get("monthly_required") or 0), deficit=dfc,
            antecedents=[f"{_SHIFT_KO.get(sh, sh)} 자격 {s.get('eligible_nurses')}명 · "
                         f"{s.get('reason')}"],
            witness={"shift": sh, "reason": s.get("reason")}))
    return out


def multi_axis_diagnose(nurses: list, config: dict, num_days: int,
                        year: int, month: int) -> AxisDiagnosis:
    """커버리지(D/E/N) + N-시퀀스 + {D,E,N,O} exact frontier DP 통합 → argmin-surplus 지목."""
    certs = coverage_certificates(nurses, config, year, month)
    night = diagnose_night_axis(nurses, config, num_days)
    if night.status == INFEASIBLE and night.certificate is not None:
        certs.append(night.certificate)
    # exact 결합 tier: 완화 층(위)이 침묵할 때만, 정수-결합 잔여를 frontier DP 로 판정.
    # (회복=실제 OFF·동시 커버리지·전이 — N/notN 이 놓치는 것. 대형은 예산초과→UNKNOWN.)
    if not certs:
        from services.ontology_graph.frontier_dp import diagnose_frontier
        fr = diagnose_frontier(nurses, config, num_days)
        if fr.status == INFEASIBLE and fr.certificate is not None:
            certs.append(fr.certificate)
    if not certs and night.status != INFEASIBLE:
        # 모든 층 침묵 → 이 축들로는 못 봄
        return AxisDiagnosis(status=night.status, night=night)
    primary = max(certs, key=lambda c: c.deficit) if certs else None
    return AxisDiagnosis(
        status=INFEASIBLE,
        primary=primary, certificates=sorted(certs, key=lambda c: -c.deficit),
        night=night if night.status == INFEASIBLE else None)


def render_axis(diag: AxisDiagnosis) -> str:
    """다축 진단 → 사람 설명. primary(가장 빡센 그룹) 중심 + 다른 축 요약."""
    if diag.status != INFEASIBLE:
        return {"FEASIBLE_WITNESS": "이 축들은 충족 가능",
                "UNKNOWN": "이 축들로는 판정 못 함(솔버로 이관)"}.get(diag.status, "")
    from services.ontology_graph.certificate import _cert_phrase, render_explanation
    lines = []
    if diag.night is not None and diag.night.status == INFEASIBLE and diag.night.children:
        lines.append(render_explanation(diag.night))          # N축 proof-tree 서사 우선
    if diag.primary is not None:
        lines.append("가장 빡센 그룹: " + _cert_phrase(diag.primary))
    others = [c for c in diag.certificates if c is not diag.primary]
    if others:
        lines.append("추가 병목: " + "; ".join(_cert_phrase(c) for c in others[:3]))
    return " / ".join(lines)
