import json


def normalize_allowed_shift_codes(raw, use_mid: bool = False) -> set[str]:
    if raw is None:
        return set()
    if isinstance(raw, str):
        txt = raw.strip()
        if not txt:
            return set()
        parsed = None
        if txt.startswith("[") and txt.endswith("]"):
            try:
                parsed = json.loads(txt)
            except Exception:
                parsed = None
        if parsed is None:
            raw = [p.strip() for p in txt.split(",") if p.strip()]
        else:
            raw = parsed
    if isinstance(raw, (tuple, set)):
        raw = list(raw)
    if not isinstance(raw, list):
        return set()

    valid = {"D", "E", "N"}
    if bool(use_mid):
        valid.add("M")
    out: set[str] = set()
    for x in raw:
        code = str(x).strip().upper()
        if code in valid:
            out.add(code)
    return out


def is_n_only_profile(raw, use_mid: bool = False) -> bool:
    return normalize_allowed_shift_codes(raw, use_mid=use_mid) == {"N"}


def is_code_blocked_by_profile(raw, code: str, use_mid: bool = False) -> bool:
    allowed = normalize_allowed_shift_codes(raw, use_mid=use_mid)
    if not allowed:
        return False
    return str(code).strip().upper() not in allowed


#: 월 나이트 횟수 상한 — **하드락 #6**(CLAUDE.md · 간호사 1인당 월 최대 15회). 해결 카드가 병동 공통
#: 상한(`max_nig_per_month`)이나 개인 한도(n_max)를 이보다 높게 **제안하면 안 된다**(하드락 완화 금지 —
#: 못 풀면 infeasible 보고). N전담은 엔진에서 이 값으로 **고정**된다(OFF ≤ 근무가능일−상한 · N ≤ 상한).
HARD_MAX_NIGHTS_PER_MONTH = 15


def effective_night_cap(nu, global_max_night: int) -> int:
    """야간 전담(N-only) 간호사의 '실효 N 상한'.

    N-only 간호사는 매일 N 또는 O뿐이라 OFF = avail_days - N 이 항등식이다.
    따라서 OFF 상한(off-cap)은 곧 N 하한이며, off-cap = avail_days - (달성 가능한 최대 N)
    으로 계산해야 한다. 달성 가능한 최대 N 은 글로벌 max_night 와 per-nurse 월간 N 한도
    (n_exact 우선, 없으면 n_max)의 더 작은 값이다.

    예) global_max_night=15, n_exact=11 → 11. off-cap = avail - 11 (= forced OFF 20일 인정).
    n 한도가 없으면 global_max_night 그대로 → 기존 동작(avail-15)과 동일(무회귀).

    솔버(fallback_lex / cp_sat_basic)와 온톨로지(conflict_detector)가 이 함수를 공유해
    cap 정의를 영구 정합시킨다.
    """
    cap = int(global_max_night or 0)
    try:
        nx = getattr(nu, "n_exact", None)
        nm = getattr(nu, "n_max", None)
        if nx is not None:
            cap = min(cap, int(nx))
        elif nm is not None:
            cap = min(cap, int(nm))
    except Exception:
        pass
    return max(0, cap)
