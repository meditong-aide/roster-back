"""케이스 JSON → 진단 스택 재현(오프라인, DB·솔버 불필요).

라이브가 쓰는 그대로의 진단 함수(explain_infeasibility_from_config + detector +
per-nurse DP + 해결카드)를 케이스 입력에 먹여, classification·certificate·카드·
per-nurse 시퀀스 판정을 출력한다. JSON 을 고쳐가며 실험하면 진단 변화가 바로 보인다.

실행:
  전체:  .venv/bin/python tools/infeasible_cases/replay.py
  하나:  .venv/bin/python tools/infeasible_cases/replay.py cases/synth-banned_4consecutive.json
  상세:  ... --verbose
"""

from __future__ import annotations

import glob
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "..", "app"))

from services.ontology_graph.lagrangian import (  # noqa: E402
    detect_banned_off_conflict,
    explain_infeasibility_from_config,
    per_nurse_night_feasible,
)
from services.ontology_graph.mcs_trace import cause_to_resolution_options  # noqa: E402

CASES_DIR = os.path.join(HERE, "cases")


def _load(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def replay(path, verbose=False):
    case = _load(path)
    cfg = case["config"]
    nurses = case["nurses"]
    nd = int(case["num_days"])
    exp = (case.get("expected") or {}).get("classification")

    e = explain_infeasibility_from_config(nurses, cfg, nd,
                                          year=case.get("year"), month=case.get("month"))
    cls = e.classification
    ok = "  " if (exp is None or exp == cls) else "≠ "
    tag = f"{ok}{cls:<22}"
    print(f"{tag} | {os.path.basename(path)}")
    if verbose:
        print(f"       family   : {e.top_family}")
        print(f"       cert     : {e.certificate}")
        if exp:
            print(f"       expected : {exp}")
        # per-nurse banned 판정 상세
        bw = detect_banned_off_conflict(nurses, cfg, nd)
        for t in bw:
            print(f"       banned   : {t['name']}({t['nurse_id']}) reason={t['reason']} "
                  f"days={t['banned_off_days']} n_only={t['is_night_only']}")
        # 해결 카드
        cards = cause_to_resolution_options(e.classification, e.top_family, e.targets)
        for c in cards:
            extra = ""
            if c.get("banned_wanted_release"):
                extra = f"  release={c['banned_wanted_release']}"
            elif c.get("allowed_shift_add"):
                extra = f"  add={c['allowed_shift_add']}"
            elif c.get("monthly_limit_release"):
                extra = f"  mlr={c['monthly_limit_release']}"
            elif c.get("weekend_off_release"):
                extra = f"  wor={c['weekend_off_release']}"
            print(f"       card     : {c.get('option_id')} — {c.get('title_ko')}{extra}")
        print()
    return cls


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    verbose = "--verbose" in sys.argv or "-v" in sys.argv
    if args:
        paths = [a if os.path.isabs(a) else os.path.join(HERE, a) for a in args]
        verbose = True
    else:
        paths = sorted(glob.glob(os.path.join(CASES_DIR, "*.json")))
    if not paths:
        print("케이스 없음. 먼저: .venv/bin/python tools/infeasible_cases/make_cases.py")
        return
    print(f"진단 재현 ({len(paths)}건)  —  '≠'=기대와 불일치\n")
    for p in paths:
        replay(p, verbose=verbose)


if __name__ == "__main__":
    main()
