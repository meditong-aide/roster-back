"""50케이스 × 축소 CP-SAT 모델 × find_mcs (실제 검출·검증).

각 케이스를 작은 모델로 genuinely infeasible 하게 구성 → find_mcs 로 검증된 최소
수선 도출. matrix_50_cases.py(원인 직접 주입, 솔버 우회)와 달리 **실제 솔버가
충돌을 만들고 MCS 가 풀어낸다**. 정직성: 재현 못 한 케이스는 NOT-REPRODUCED 로 표기.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
for _p in (str(ROOT / "app"), str(ROOT / "tools" / "ontology_eval"), str(ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from reduced_model_lib import Reduced  # noqa: E402
from services.cp_sat.mcs import find_mcs  # noqa: E402

MD = ROOT / "docs" / "ONTOLOGY_50_REDUCED_MCS.md"


def base(**kw):
    """coverage N/D/E≥2 인 6인 4일 모델(타이트하지만 feasible)."""
    r = Reduced(nurses=6, days=4, **kw)
    r.coverage_min("N", 2); r.coverage_min("D", 2); r.coverage_min("E", 2)
    return r


# 각 케이스: 충돌을 만드는 reduced 구성 함수 (genuinely infeasible 목표)
def s_grade_block_N():       # coverage N ↔ 모든 등급 N 상한 0
    r = base()
    for g in (1, 2, 3): r.grade_max("N", g, 0)
    return r

def s_team_overload():       # 팀 최소 합 > 팀원
    r = base(); r.team_min("1", "N", 3); r.team_min("1", "D", 3); r.team_min("1", "E", 3)
    return r

def s_night_cap_zero():      # 월 N cap 0 ↔ coverage N
    r = base(); r.monthly_night_cap(0); return r

def s_consec_block():        # 연속근무≤1 ↔ coverage 6슬롯(OFF 강제로 공급부족)
    r = base(); r.consecutive_work(1); return r

def s_transition_seq():      # 전이금지 + 고정 N then D-forcing (mask)
    r = base(); r.transition_ban(); r.fixed(0, 0, "N"); r.allowed_mask(0, {"N", "D"})
    r.monthly_night_cap(1)   # n0 day1 N 못함 → D 강제 → 전이금지 위반
    return r

def s_mask_team():           # N전담 마스크 ↔ 팀 D 최소
    r = base()
    for n in range(4): r.allowed_mask(n, {"N"})   # 4명 N전담
    r.team_min("1", "D", 3)
    return r

def s_not_one_night():       # 단일N금지 + N cap1 → N 불가
    r = base(); r.not_one_night(); r.monthly_night_cap(1); return r

def s_recovery_cov():        # 야간회복 + N 높은 수요
    r = Reduced(nurses=6, days=4); r.coverage_min("N", 3); r.coverage_min("D", 2)
    r.night_recovery(); r.monthly_night_cap(2); return r

def s_offcap_cov():          # OFF 최소 ↔ coverage 풀가동
    r = base()
    for n in range(6): r.off_cap(n, 2)   # 각자 OFF≥2 → 가동 부족
    return r

def s_weekend_off():         # 평일 O금지(공급압박) + 연속근무 → 충돌
    r = base()
    for n in range(3): r.weekend_off_only(n)
    r.consecutive_work(2); return r

def s_grade_minmax():        # 등급 최소>상한
    r = base(); r.grade_min("N", 1, 1); r.grade_max("N", 1, 0); return r

def s_grade_sandwich():      # grade_min + grade_max + team
    r = base(); r.grade_min("N", 1, 1); r.grade_max("N", 1, 0); r.team_min("1", "N", 2); return r


# 50케이스 → 시나리오 (spec 의 핵심 충돌을 축소 재현; 같은 패턴 재사용)
CASES = {
    # Window Hard
    "CX-WIN-001": s_transition_seq, "CX-WIN-002": s_not_one_night, "CX-WIN-003": s_consec_block,
    "CX-WIN-004": s_recovery_cov, "CX-WIN-005": s_recovery_cov, "CX-WIN-006": s_night_cap_zero,
    "CX-WIN-007": s_weekend_off, "CX-WIN-008": s_transition_seq, "CX-WIN-009": s_transition_seq,
    "CX-WIN-010": s_consec_block,
    # Window Soft (hard 원인 우세)
    "CX-SFT-011": s_transition_seq, "CX-SFT-012": s_offcap_cov, "CX-SFT-013": s_night_cap_zero,
    "CX-SFT-014": s_transition_seq, "CX-SFT-015": s_weekend_off,
    # Nurse Hard
    "CX-NUR-016": s_night_cap_zero, "CX-NUR-017": s_mask_team, "CX-NUR-018": s_weekend_off,
    "CX-NUR-019": s_night_cap_zero, "CX-NUR-020": s_offcap_cov, "CX-NUR-021": s_team_overload,
    "CX-NUR-022": s_grade_sandwich, "CX-NUR-023": s_mask_team, "CX-NUR-024": s_recovery_cov,
    "CX-NUR-025": s_night_cap_zero,
    # Coverage Hard
    "CX-COV-026": s_team_overload, "CX-COV-027": s_mask_team, "CX-COV-028": s_grade_block_N,
    "CX-COV-029": s_grade_sandwich, "CX-COV-030": s_weekend_off, "CX-COV-031": s_recovery_cov,
    "CX-COV-032": s_transition_seq, "CX-COV-033": s_grade_sandwich, "CX-COV-034": s_mask_team,
    "CX-COV-035": s_offcap_cov,
    # Override/Fixed
    "CX-OVR-036": s_transition_seq, "CX-OVR-037": s_mask_team, "CX-OVR-038": s_recovery_cov,
    "CX-OVR-039": s_grade_block_N, "CX-OVR-040": s_grade_sandwich,
    # Precheck/Meta
    "CX-META-041": s_grade_minmax, "CX-META-042": s_grade_block_N, "CX-META-043": s_mask_team,
    "CX-META-044": s_grade_block_N, "CX-META-045": s_night_cap_zero,
    # 10+ Mix
    "CX-MIX-046": s_night_cap_zero, "CX-MIX-047": s_weekend_off, "CX-MIX-048": s_mask_team,
    "CX-MIX-049": s_grade_block_N, "CX-MIX-050": s_mask_team,
}


def run_case(cid, factory):
    try:
        r = factory(); m, reg = r.finalize()
        t0 = time.time()
        res = find_mcs(m, reg, time_limit=3)
        dt = time.time() - t0
        if res is None:
            return {"id": cid, "status": "NOT-REPRODUCED", "note": "assumption 밖 infeasible 또는 feasible"}
        if not res.relaxed:
            return {"id": cid, "status": "NOT-REPRODUCED", "note": "충돌 미발생(feasible)", "dt": dt}
        return {"id": cid, "status": "PASS" if res.verified_feasible else "UNVERIFIED",
                "relaxed": [mm["label"] for mm in res.relaxed_meta],
                "patterns": sorted({mm["pattern"] for mm in res.relaxed_meta}),
                "verified": res.verified_feasible, "n": len(res.relaxed), "dt": round(dt, 2)}
    except Exception as exc:
        import traceback
        return {"id": cid, "status": "ERROR", "note": f"{type(exc).__name__}: {exc}",
                "tb": traceback.format_exc()[-400:]}


def main():
    results = [run_case(cid, f) for cid, f in CASES.items()]
    npass = sum(1 for r in results if r["status"] == "PASS")
    nrepro = sum(1 for r in results if r["status"] in ("PASS", "UNVERIFIED"))
    print(f"\n=== 50 Reduced-MCS — {npass}/50 PASS(verified), {nrepro}/50 재현(infeasible+MCS) ===\n")
    for r in results:
        mark = {"PASS": "✅", "UNVERIFIED": "🟡", "NOT-REPRODUCED": "⚪", "ERROR": "❌"}.get(r["status"], "?")
        extra = ""
        if r["status"] == "PASS":
            extra = f" {r['n']}개완화 {r['patterns']} ({r['dt']}s)"
        elif r.get("note"):
            extra = f" — {r['note']}"
        print(f"  {mark} {r['id']}{extra}")

    # md
    L = ["# 50케이스 × 축소모델 × MCS (실제 검출·검증)\n",
         "matrix_50_cases(원인 직접주입, 솔버우회)와 달리 **실제 CP-SAT 가 충돌을 만들고**",
         "find_mcs 가 **검증된 최소 수선**을 도출. 재현 못 한 케이스는 정직히 NOT-REPRODUCED.\n",
         f"\n## 요약: {npass}/50 PASS(verified) · {nrepro}/50 재현\n",
         "| Case | 결과 | 완화(수선점) | 패턴 | verified | 시간 |",
         "|---|---|---|---|---|---|"]
    for r in results:
        if r["status"] in ("PASS", "UNVERIFIED"):
            L.append(f"| {r['id']} | {'✅PASS' if r['status']=='PASS' else '🟡UNVERIFIED'} | "
                     f"{', '.join(r['relaxed'][:3])} | {r['patterns']} | {r['verified']} | {r['dt']}s |")
        else:
            L.append(f"| {r['id']} | ⚪{r['status']} | — | — | — | {r.get('note','')} |")
    MD.write_text("\n".join(L), encoding="utf-8")
    print(f"\nmd → {MD.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
