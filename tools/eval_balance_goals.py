"""중환자실1 (시화병원) 6월 X축/Y축 균등 목표 검증 하네스.

목표 (정아영 제외 — fixed_wanted/특수 케이스):
- Y축: per-nurse N count ≤ ceil(N_target × 1.5)
  · N_target = 일별 N demand × days / nurses (= 6 for 시화 6월)
  · 자동 상한 = 9
- X축: per-nurse max(D, E) ≤ 1.5 × min(D, E)
  · 0/0이면 skip. min(D,E)=0이고 max>0이면 위반 (자동 fail)

입력: DB의 schedule version 또는 log 파일.
출력: 위반 nurse 리스트 + 합/불 판정.

사용:
    python tools/eval_balance_goals.py             # 최신 schedule
    python tools/eval_balance_goals.py --version N # 특정 version
    python tools/eval_balance_goals.py --log PATH  # 로그 파일에서 추출
"""
from __future__ import annotations

import argparse
import math
import os
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Iterable

ROOT = Path(__file__).resolve().parent.parent
APP = ROOT / "app"
sys.path.insert(0, str(APP))

EXCLUDE_NAMES = ("정아영",)  # fixed_wanted/특수 케이스
GROUP_ID = "10135857f9f9"  # 시화병원
X_RATIO = 1.5  # max(D,E) ≤ 1.5 × min(D,E)
Y_RATIO = 1.5  # N ≤ ceil(N_target × 1.5)


def extract_from_log(path: Path) -> dict[str, dict[str, int]]:
    text = path.read_text()
    pat = re.compile(r"배정표\s+([^()]+)\((\d+)\):\s*((?:[A-Z주]\s*){20,})")
    by_id: dict[str, dict[str, int]] = {}
    for name, nid, codes in pat.findall(text):
        c = Counter(codes.split())
        by_id[name.strip()] = {
            "D": c.get("D", 0),
            "E": c.get("E", 0),
            "N": c.get("N", 0),
            "O": c.get("O", 0),
            "주": c.get("주", 0),
        }
    return by_id


def extract_from_db(version: int | None) -> dict[str, dict[str, int]]:
    import dotenv
    dotenv.load_dotenv(ROOT / ".env")
    dotenv.load_dotenv(APP / ".env")
    from db.client2 import SessionLocal
    from db.models import Schedule, ScheduleEntry, Shift, Nurse

    db = SessionLocal()
    try:
        q = (db.query(Schedule)
             .filter(Schedule.group_id == GROUP_ID,
                     Schedule.year == 2026, Schedule.month == 6,
                     Schedule.dropped == False))  # noqa
        if version is not None:
            target = q.filter(Schedule.version == version).first()
        else:
            target = q.order_by(Schedule.version.desc()).first()
        if target is None:
            raise SystemExit(f"schedule version={version} not found")
        print(f"[eval] schedule v{target.version} id={target.schedule_id} created={target.created_at}")

        shifts = {sh.id: str(sh.default_shift or sh.shift_id or "").upper()
                  for sh in db.query(Shift).filter(Shift.group_id == GROUP_ID).all()}
        entries = db.query(ScheduleEntry).filter(
            ScheduleEntry.schedule_id == target.schedule_id
        ).all()
        nurses = {str(n.nurse_id): n.name for n in db.query(Nurse).filter(Nurse.group_id == GROUP_ID).all()}

        by_name: dict[str, list[str]] = {}
        for e in entries:
            code = shifts.get(e.id, "")
            nm = nurses.get(str(e.nurse_id), str(e.nurse_id))
            by_name.setdefault(nm, []).append(code)
        result: dict[str, dict[str, int]] = {}
        for nm, codes in by_name.items():
            c = Counter(codes)
            result[nm] = {
                "D": c.get("D", 0),
                "E": c.get("E", 0),
                "N": c.get("N", 0),
                "O": c.get("O", 0),
                "주": c.get("주", 0),
            }
        return result
    finally:
        db.close()


def evaluate(by_name: dict[str, dict[str, int]], exclude: Iterable[str] = EXCLUDE_NAMES) -> dict:
    n_total = sum(d["N"] for d in by_name.values())
    n_nurses = len(by_name)
    n_target = n_total / max(1, n_nurses)
    y_cap = math.floor(n_target * Y_RATIO)
    y_floor = math.floor(n_target / Y_RATIO)

    y_upper_v: list[tuple[str, int]] = []
    y_lower_v: list[tuple[str, int]] = []
    pair_v: list[tuple[str, str, str, int, int, float]] = []
    excl = set(exclude)
    pairs = [("D", "E"), ("D", "N"), ("E", "N")]

    for name, c in sorted(by_name.items()):
        if name in excl:
            continue
        n = c["N"]
        if n > y_cap:
            y_upper_v.append((name, n))
        if n < y_floor:
            y_lower_v.append((name, n))
        for s1, s2 in pairs:
            v1, v2 = c[s1], c[s2]
            if v1 == 0 and v2 == 0:
                continue
            lo, hi = min(v1, v2), max(v1, v2)
            if lo == 0:
                pair_v.append((name, s1, s2, v1, v2, float("inf")))
            else:
                ratio = hi / lo
                if ratio > X_RATIO:
                    pair_v.append((name, s1, s2, v1, v2, ratio))

    passed = not (y_upper_v or y_lower_v or pair_v)
    return {
        "n_target_avg": n_target,
        "y_cap": y_cap,
        "y_floor": y_floor,
        "y_upper_violations": y_upper_v,
        "y_lower_violations": y_lower_v,
        "pair_ratio_max_allowed": X_RATIO,
        "pair_violations": pair_v,
        "passed": passed,
        "excluded": list(excl),
        "total_nurses": n_nurses,
    }


def report(res: dict, by_name: dict[str, dict[str, int]]):
    print(f"\n[eval] exclude={res['excluded']}  total_nurses={res['total_nurses']}")
    print(f"[eval] Y-axis: N_target_avg={res['n_target_avg']:.2f}, y_floor={res['y_floor']}, y_cap={res['y_cap']} (×{Y_RATIO})")
    print(f"[eval] Pair ratios: D-E, D-N, E-N — max/min ≤ {X_RATIO}")
    print(f"\n--- Y upper violations (N > {res['y_cap']}) ---")
    if not res["y_upper_violations"]:
        print("  (none)")
    for name, n in res["y_upper_violations"]:
        print(f"  {name}: N={n}")
    print(f"\n--- Y lower violations (N < {res['y_floor']}) ---")
    if not res["y_lower_violations"]:
        print("  (none)")
    for name, n in res["y_lower_violations"]:
        print(f"  {name}: N={n}")
    print(f"\n--- Pair ratio violations (max/min > {X_RATIO}) ---")
    if not res["pair_violations"]:
        print("  (none)")
    for name, s1, s2, v1, v2, r in res["pair_violations"]:
        ratio_s = "∞" if r == float("inf") else f"{r:.2f}"
        print(f"  {name}: {s1}={v1}, {s2}={v2}, ratio={ratio_s}")
    y_u, y_l, pv = len(res["y_upper_violations"]), len(res["y_lower_violations"]), len(res["pair_violations"])
    verdict = "PASS ✓" if res["passed"] else f"FAIL ✗ (Y_upper={y_u}, Y_lower={y_l}, pair={pv})"
    print(f"\n[eval] {verdict}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--version", type=int, default=None)
    ap.add_argument("--log", type=str, default=None)
    args = ap.parse_args()

    if args.log:
        by_name = extract_from_log(Path(args.log))
    else:
        by_name = extract_from_db(args.version)

    if not by_name:
        print("[eval] no data extracted")
        return 1

    print(f"\n{'name':<10} {'D':>3} {'E':>3} {'N':>3} {'O':>3} {'주':>3}")
    for name, c in sorted(by_name.items()):
        print(f"{name:<10} {c['D']:>3} {c['E']:>3} {c['N']:>3} {c['O']:>3} {c['주']:>3}")

    res = evaluate(by_name)
    report(res, by_name)
    return 0 if res["passed"] else 2


if __name__ == "__main__":
    sys.exit(main())
