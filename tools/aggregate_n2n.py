"""병동별 다회 실행 결과 집계: n2n 간격 위반 + 회귀(severity/violations) + 품질(off_quota/range).

usage: python tools/aggregate_n2n.py <json_glob> [--logs <log_glob>] [--target 10] [--label NAME]
예) python tools/aggregate_n2n.py 'artifacts/debug_icu/icu_a300_v*.json' --logs 'artifacts/debug_icu/icu_a300_v*.log' --label ICU
"""
from __future__ import annotations
import glob
import json
import re
import statistics
import sys


def n_blocks(schedule):
    blocks, i, n = [], 0, len(schedule)
    while i < n:
        if str(schedule[i]).upper() == "N":
            j = i
            while j + 1 < n and str(schedule[j + 1]).upper() == "N":
                j += 1
            blocks.append((i, j))
            i = j + 1
        else:
            i += 1
    return blocks


def gaps_under(path, target):
    d = json.load(open(path, encoding="utf-8"))
    under, total, deficit, mins = 0, 0, 0, []
    for nu in d.get("nurses", []):
        bl = n_blocks(nu.get("schedule") or [])
        for a, b in zip(bl, bl[1:]):
            g = b[0] - a[1]
            total += 1
            mins.append(g)
            if g < target:
                under += 1
                deficit += target - g
    worst = min(mins) if mins else None
    return dict(under=under, total=total, deficit=deficit, worst=worst,
                median=(statistics.median(mins) if mins else None))


_RESP = re.compile(r"\[RosterGenerate\]\[response\] HTTP (\d+), severity=(\S+), .*violations=(\{.*\})")
_OFFQ = re.compile(r"\[Stage3 위반\] off_quota_excess = (\d+)")
_OFFRANGE = re.compile(r"lex 2-pass \(OFF range\): status=\S+ range=(\d+)")
_NRANGE = re.compile(r"lex 3-pass \(N range\): status=\S+ range=(\d+)")
_N2NPASS = re.compile(r"lex 4-pass \(n2n deficit\): status=(\S+)(?: deficit=(\d+))?")


def log_metrics(path):
    txt = open(path, encoding="utf-8", errors="replace").read()
    m = _RESP.search(txt)
    http = sev = viol = None
    if m:
        http, sev, viol = m.group(1), m.group(2), m.group(3)
    offq = _OFFQ.findall(txt)
    offr = _OFFRANGE.findall(txt)
    nr = _NRANGE.findall(txt)
    p4 = _N2NPASS.findall(txt)
    p4_status = p4[-1][0] if p4 else None
    p4_def = int(p4[-1][1]) if (p4 and p4[-1][1]) else None
    return dict(http=http, sev=sev, viol=viol,
                off_quota=int(offq[-1]) if offq else None,
                off_range=int(offr[-1]) if offr else None,
                n_range=int(nr[-1]) if nr else None,
                p4_status=p4_status, p4_def=p4_def)


def main():
    args = sys.argv[1:]
    target, label, log_glob = 10, "WARD", None
    for flag, default in (("--target", None), ("--label", None), ("--logs", None)):
        if flag in args:
            i = args.index(flag)
            val = args[i + 1]
            del args[i:i + 2]
            if flag == "--target":
                target = int(val)
            elif flag == "--label":
                label = val
            else:
                log_glob = val
    json_glob = args[0]
    jsons = sorted(glob.glob(json_glob))
    logs = sorted(glob.glob(log_glob)) if log_glob else []

    print(f"\n################ {label}  (n={len(jsons)} runs, target={target}) ################")
    unders, deficits, worsts, medians = [], [], [], []
    for p in jsons:
        g = gaps_under(p, target)
        unders.append(g["under"]); deficits.append(g["deficit"])
        if g["worst"] is not None:
            worsts.append(g["worst"])
        if g["median"] is not None:
            medians.append(g["median"])
        print(f"  {p.split('/')[-1]:40s} under={g['under']:2d}/{g['total']:2d} deficit={g['deficit']:3d} worst={g['worst']} median={g['median']}")
    if unders:
        print(f"  ── n2n 요약: under(<{target}) avg={statistics.mean(unders):.1f} min={min(unders)} max={max(unders)} | "
              f"deficit avg={statistics.mean(deficits):.1f} | worst-gap전체={min(worsts) if worsts else '-'} | median중앙={statistics.median(medians) if medians else '-'}")

    if logs:
        print(f"  ── 회귀/품질 (logs n={len(logs)}):")
        sevs, offqs, offrs, nrs, bad, p4s, p4defs = [], [], [], [], [], [], []
        for p in logs:
            m = log_metrics(p)
            sevs.append(m["sev"])
            if m["off_quota"] is not None: offqs.append(m["off_quota"])
            if m["off_range"] is not None: offrs.append(m["off_range"])
            if m["n_range"] is not None: nrs.append(m["n_range"])
            if m["p4_status"] is not None: p4s.append(m["p4_status"])
            if m["p4_def"] is not None: p4defs.append(m["p4_def"])
            if m["sev"] != "ok" or (m["viol"] and m["viol"] != "{}"):
                bad.append((p.split('/')[-1], m["sev"], m["viol"]))
        from collections import Counter
        print(f"     severity: {dict(Counter(sevs))}")
        if offqs: print(f"     off_quota_excess: min={min(offqs)} max={max(offqs)} avg={statistics.mean(offqs):.1f}")
        if offrs: print(f"     OFF range: {dict(Counter(offrs))}")
        if nrs:   print(f"     N range:   {dict(Counter(nrs))}")
        if p4s:   print(f"     lex 4-pass status: {dict(Counter(p4s))}" + (f" | solver deficit avg={statistics.mean(p4defs):.1f} (min={min(p4defs)} max={max(p4defs)})" if p4defs else ""))
        else:     print("     ⚠ lex 4-pass 미발동 (로그에 4-pass 라인 없음)")
        if bad:
            print(f"     ⚠ 비정상 run {len(bad)}건:")
            for nm, s, v in bad: print(f"        {nm}: severity={s} violations={v}")
        else:
            print("     ✓ 전 run severity=ok, violations 없음")
    return 0


if __name__ == "__main__":
    sys.exit(main())
