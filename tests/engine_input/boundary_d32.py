"""D32 판정기 — 바깥 칸·확정 칸(given)만으로 된 위반은 면제, 엔진이 고른 칸이 하나라도 걸리면 위반(EI-11 11w-3 · D32).

회복(#4·#5)은 '옮긴 OFF' — 바깥·확정 칸만으로 된 N 블록 뒤로는 OFF 2개를 받을 때까지 엔진 칸이 O 여야 한다(확정 비-O 칸은 건너뛴다).
블록에 엔진 칸이 하나라도 있으면 보통 규칙(블록 바로 뒤 두 칸 O — 그 칸이 확정 비-O 면 엔진이 그 블록을 만들면 안 된다).
"""

from __future__ import annotations

import itertools

from .boundary_cases import CODES, NDAYS, WORK, _rules, _span


def hard_violations_d32(outside: dict[int, str], mine: dict[int, str], cfg: dict, given: frozenset = frozenset()) -> list[str]:
    mine = {k: v for k, v in mine.items() if v != "-"}
    if not mine:
        return []
    tl = {**outside, **mine}
    lo, hi = min(tl), max(tl)
    idx = list(range(lo, hi + 1))
    seq = [tl.get(i, "-") for i in idx]
    eng = {i for i, day in enumerate(idx) if day in mine and day not in given}
    r = _rules(cfg)
    out: list[str] = []

    def has_eng(a: int, b: int) -> bool:
        return any(i in eng for i in range(max(a, 0), min(b, len(seq) - 1) + 1))

    for i in range(len(seq) - r["K"]):
        win = range(i, i + r["K"] + 1)
        if all(seq[k] in WORK for k in win) and any(k in eng for k in win):
            out.append(f"#1 연속근무>{r['K']} @{idx[i]}")
            break
    out += [f"#2 {seq[i - 1]}->{seq[i]} @{idx[i]}" for i in range(1, len(seq))
            if (seq[i - 1], seq[i]) in r["bans"] and (i - 1 in eng or i in eng)]
    for i in range(len(seq) - r["L"]):
        win = range(i, i + r["L"] + 1)
        if all(seq[k] == "N" for k in win) and any(k in eng for k in win):
            out.append(f"#3 N>{r['L']} @{idx[i]}")
            break
    i = 0
    while i < len(seq):
        if seq[i] != "N":
            i += 1
            continue
        j = i
        while j < len(seq) and seq[j] == "N":
            j += 1
        n = j - i
        rule = "#4" if (n >= 3 and r["two3"]) else ("#5" if (n >= 2 and r["two2"]) else None)
        block_eng = any(k in eng for k in range(i, j))
        if rule and block_eng:
            nxt = seq[j:j + 2]
            if len(nxt) == 2 and "-" not in nxt and nxt != ["O", "O"]:
                out.append(f"{rule} N{n} 뒤 {''.join(nxt)} @{idx[j]}")
        elif rule and any(seq[k] not in ("O", "-") and idx[k] in outside and idx[k] not in mine for k in range(j, min(j + 2, len(seq)))):
            pass    # 블록 바로 뒤 두 칸에 바깥의 비-O 가 이미 있다 — 바깥이 먼저 어겼으니 이 병동 몫의 옮긴 OFF 빚은 없다(11w-r)
        elif rule:
            got = 0
            for k in range(j, len(seq)):
                if got >= 2 or seq[k] == "-":
                    break
                if seq[k] == "O":
                    got += 1
                elif k in eng:
                    out.append(f"{rule}(옮긴 OFF) N{n} 뒤 {seq[k]} @{idx[k]}")
                    break
        if r["one"] and n == 1 and i in eng and i > 0 and j < len(seq) and "-" not in (seq[i - 1], seq[j]):
            out.append(f"#7 1N @{idx[i]}")
        i = j
    month_n = sum(1 for k, v in tl.items() if 0 <= k < NDAYS and v == "N")
    if any(0 <= k < NDAYS and v == "N" and k not in given for k, v in mine.items()) and month_n > r["n_cap"]:
        out.append(f"#6 월N {month_n}>{r['n_cap']}")
    return out


def boundary_only_d32(outside: dict[int, str], boundaries, cfg: dict, fixed: dict[int, str],
                      max_len: int = 6, active: set[int] | None = None) -> list[dict[int, str]]:
    """경계만의 위반 이어짐(D32) — 확정 날은 그 코드로 고정하고 나머지 날만 바꿔 본다. 돌려주는 것은 '엔진 칸'만(탐침용)."""
    given = frozenset(fixed)
    found: list[dict[int, str]] = []
    for direction, day in boundaries:
        for length in range(1, max_len + 1):
            days = _span(direction, day, length)
            if active is not None and not set(days) <= active:
                break
            free = [x for x in days if x not in fixed]
            if not free:
                continue
            for codes in itertools.product(CODES, repeat=len(free)):
                cont = {**{x: fixed[x] for x in days if x in fixed}, **dict(zip(free, codes, strict=True))}
                if hard_violations_d32(outside, cont, cfg, given) and not hard_violations_d32({}, cont, cfg, given):
                    found.append({x: cont[x] for x in free})
    return found


def minimal(conts: list[dict[int, str]]) -> list[dict[int, str]]:
    keep: list[dict[int, str]] = []
    for cont in conts:
        if not any(all(cont.get(k) == v for k, v in short.items()) for short in keep):
            keep.append(cont)
    return keep
