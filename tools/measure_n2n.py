"""N블록 간 간격(n2n interval) 측정. 산출물 JSON(들)을 받아 블록간 간격 분포를 출력한다.

gap = (다음 N블록 첫 N의 날) - (앞 N블록 마지막 N의 날)  ── 솔버 n2n 항과 동일 정의.
target=10 미만 간격이 "한쪽 페널티"가 잡으려는 위반이다.

usage: python tools/measure_n2n.py <result.json> [<result2.json> ...] [--target 10]
"""
from __future__ import annotations
import json
import sys


def n_blocks(schedule: list[str]) -> list[tuple[int, int]]:
    """N의 maximal run을 (start_day, end_day) 0-index로 반환."""
    blocks = []
    i = 0
    n = len(schedule)
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


def measure(path: str, target: int):
    d = json.load(open(path, encoding="utf-8"))
    nurses = d.get("nurses", [])
    gaps = []  # (nurse_name, gap)
    n_block_counts = []
    for nu in nurses:
        sched = nu.get("schedule") or []
        blocks = n_blocks(sched)
        n_block_counts.append(len(blocks))
        for a, b in zip(blocks, blocks[1:]):
            gap = b[0] - a[1]  # next first N - prev last N
            gaps.append((nu.get("name", "?"), gap))
    return d, gaps, n_block_counts


def main() -> int:
    args = [a for a in sys.argv[1:]]
    target = 10
    if "--target" in args:
        i = args.index("--target")
        target = int(args[i + 1])
        del args[i : i + 2]
    paths = args
    if not paths:
        print("usage: python tools/measure_n2n.py <result.json> [...] [--target 10]")
        return 2

    for path in paths:
        d, gaps, nbc = measure(path, target)
        total_blocks = sum(nbc)
        nurses_with_n = sum(1 for c in nbc if c > 0)
        under = [(nm, g) for nm, g in gaps if g < target]
        within_win = [(nm, g) for nm, g in gaps if g <= 15]
        print(f"\n=== {path} ===")
        print(f"간호사 {len(d.get('nurses', []))}명, N블록 보유 {nurses_with_n}명, 총 N블록 {total_blocks}개")
        print(f"블록간 간격(gap) 표본 {len(gaps)}개 (이 중 window<=15: {len(within_win)}개)")
        if gaps:
            gvals = sorted(g for _, g in gaps)
            print(f"  gap min/median/max = {gvals[0]} / {gvals[len(gvals)//2]} / {gvals[-1]}")
            from collections import Counter
            dist = Counter(g for _, g in gaps)
            print("  gap 분포:", dict(sorted(dist.items())))
        print(f"  ▶ target({target}) 미만 간격 = {len(under)}개" + (f"  (deficit합={sum(target-g for _,g in under)})" if under else ""))
        for nm, g in sorted(under, key=lambda x: x[1])[:20]:
            print(f"      {nm}: gap={g} (target-{target-g})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
