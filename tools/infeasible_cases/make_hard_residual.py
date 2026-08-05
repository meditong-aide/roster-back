"""Hard-residual 코퍼스 생성 — 우리 진단 스택이 **놓치는** 정수-결합 반례.

각 케이스는 다음을 **모두** 만족(=우리 스택의 모든 층이 침묵)하지만 정수 근무표는 infeasible:
  · per-nurse automaton feasible   · max-flow 커버리지 통과
  · aggregate 공급 surplus ≥ 0     · joint-N DP feasible(relaxed)
→ 독립 exact oracle(exact_oracle.is_feasible) 로만 INFEAS 가 드러난다.

이 gap 이 variable-elimination/frontier DP 엔진(미니 솔버 대체)의 **존재 이유**다.
oracle=INFEAS 이고 우리 multi_axis != INFEASIBLE 인 것만 검증 후 저장(가짜 반례 방지).
"""

from __future__ import annotations

import json
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_HERE, "..", "..", "app"))
sys.path.insert(0, _HERE)

from exact_oracle import is_feasible  # noqa: E402
from services.ontology_graph.axis_diagnose import multi_axis_diagnose  # noqa: E402

# (name, k, num_days, dsr, rules, why)
_SPECS = [
    ("hard_residual_recovery_off_starvation", 5, 6, {"D": 1, "E": 1, "N": 2},
     {"two_offs_after_two_nig": True, "not_one_night": True},
     "N2/일 + run후 2 OFF 회복이 하루 1 OFF 자리와 충돌(회복=실제 OFF). N/notN 모델은 "
     "D/E 를 회복으로 관대 인정해 못 봄."),
    ("hard_residual_three_night_recovery", 5, 6, {"D": 1, "E": 1, "N": 2},
     {"two_offs_after_three_nig": True, "not_one_night": True},
     "3연속 야간 후 2 OFF 회복 규칙 하에서 동일 결합 충돌(다른 회복 규칙 변주)."),
    ("hard_residual_night_to_day_ban", 5, 6, {"D": 1, "E": 1, "N": 2},
     {"two_offs_after_two_nig": True, "not_one_night": True, "forbid_night_to_day": True},
     "야간 다음날 주간 금지 전이가 D/E/N 배정을 결합으로 막음(N/notN 모델은 전이 못 봄)."),
]


def _case(name, k, days, dsr, rules, why):
    nurses = [{"nurse_id": f"n{i}", "name": f"N{i}", "grade": 1, "team_id": "A"}
              for i in range(k)]
    cfg = dict(rules, daily_shift_requirements=dsr)
    cfg["initial_constraints"] = {"forbidden": {}, "forced_off": {}}
    return {
        "meta": {"group_id": "synthetic", "source": "synthetic-hard-residual",
                 "name": name, "why": why},
        "year": 2026, "month": 8, "num_days": days,
        "config": cfg, "nurses": nurses,
        "expected": {"oracle": "INFEASIBLE", "our_stack": "SILENT(gap)",
                     "family": "hard_residual",
                     "note": "정수-결합 잔여 — VE/frontier DP 엔진 대상"},
    }


def main():
    out_dir = os.path.join(_HERE, "cases")
    made, skipped = 0, []
    for spec in _SPECS:
        name = spec[0]
        c = _case(*spec)
        nu, cfg, dd = c["nurses"], c["config"], c["num_days"]
        oracle = is_feasible(nu, cfg, dd)
        ours = multi_axis_diagnose(nu, cfg, dd, c["year"], c["month"]).status
        if oracle is False and ours != "INFEASIBLE_CERTIFIED":
            with open(os.path.join(out_dir, f"synth-{name}.json"), "w") as f:
                json.dump(c, f, ensure_ascii=False, indent=2)
            print(f"  ✓ 저장 synth-{name}.json  (oracle=INFEAS, 우리={ours})")
            made += 1
        else:
            skipped.append((name, oracle, ours))
            print(f"  ✗ 스킵 {name}  (oracle={oracle}, 우리={ours}) — 진짜 gap 아님")
    print(f"\n{made} 케이스 저장, {len(skipped)} 스킵.")


if __name__ == "__main__":
    main()
