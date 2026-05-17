"""개인별 월간 시프트 한도(Monthly Limit) 제약 모듈.

`Nurse` 객체에 부착된 `d_min/d_max/d_exact, e_*, n_*, o_*` 값을 읽어
당월 물리 일수(`D_phys`) 범위에서 nurse별 합산을 hard 제약으로 추가한다.

활성 조건:
    - nurse 객체에 `<shift_prefix>_min/max/exact` 속성이 하나 이상 부착되어 있음
    - 해당 시프트 코드(D/E/N/O)가 `cfg.shift_types`에 존재
    - join/leave 범위가 비어있지 않음

사용:
    from services.constraints.monthly_limit_constraints import add_monthly_limit_constraints
    add_monthly_limit_constraints(m, rs, X, join, leave)

이 모듈을 통해 cp_sat_basic primary path와 fallback_lex 양쪽에서 동일한
nurse-level 월간 한도가 적용된다.
"""

from __future__ import annotations

from typing import Any

from services.cp_sat.lookahead_helpers import month_total_day_range
from services.roster_system import RosterSystem


_SHIFT_PREFIX_TO_CODE = (
    ("d", "D"),
    ("e", "E"),
    ("n", "N"),
    ("o", "O"),
)


def _norm_bounds(min_v: Any, max_v: Any, exact_v: Any) -> tuple[int | None, int | None]:
    if exact_v is not None:
        try:
            v = int(exact_v)
            return v, v
        except (TypeError, ValueError):
            pass
    mn = int(min_v) if min_v is not None else None
    mx = int(max_v) if max_v is not None else None
    return mn, mx


def collect_single_n_allowed_nurse_indices(rs: RosterSystem) -> set[int]:
    """nurse_monthly_limit 에서 N=1 가능성이 명시된 nurse 의 nurse_index set.

    1N 금지(not_one_night) hard 제약에서 면제 대상 — 운영자가 월간 한도로 N=1 을
    어느 필드든 명시한 의도는 default 1N 금지 정책보다 우선한다.

    면제 조건 (셋 중 어느 하나):
        - n_exact == 1  → 정규화 시 (1, 1)
        - n_max   == 1  → 상한이 1 (강제 N=0 or 1)
        - n_min   == 1  → 하한이 1 (1 가능, 더 많을 수도)

    사용자 정책: "n_min=1 이든 n_max=1 이든 n_exact=1 이든 알고리즘에서 1이
    나오는 경우" 해당 nurse 는 1N ban 면제.
    """
    out: set[int] = set()
    for n_idx, nu in enumerate(rs.nurses):
        mn_raw = getattr(nu, "n_min", None)
        mx_raw = getattr(nu, "n_max", None)
        ex_raw = getattr(nu, "n_exact", None)
        mn, mx = _norm_bounds(mn_raw, mx_raw, ex_raw)
        if mn == 1 or mx == 1:
            out.add(n_idx)
    return out


def add_monthly_limit_constraints(
    m,
    rs: RosterSystem,
    X,
    join: list[int],
    leave: list[int],
) -> int:
    """nurse별 월간 D/E/N/O 한도 hard 제약 추가.

    Returns:
        추가된 (nurse × shift) 제약 개수.
    """
    cfg = rs.config
    shift_types = list(getattr(cfg, "shift_types", []) or [])
    if not shift_types:
        return 0
    D_phys = rs.num_days

    code_idx: dict[str, int] = {}
    for _, code_upper in _SHIFT_PREFIX_TO_CODE:
        if code_upper in shift_types:
            code_idx[code_upper] = shift_types.index(code_upper)
    if not code_idx:
        return 0

    added = 0
    for n_idx, nu in enumerate(rs.nurses):
        T0 = int(join[n_idx])
        T1 = int(leave[n_idx])
        phys_range = month_total_day_range(T0, T1, D_phys)
        if not phys_range:
            continue
        for prefix, code_upper in _SHIFT_PREFIX_TO_CODE:
            if code_upper not in code_idx:
                continue
            mn_raw = getattr(nu, f"{prefix}_min", None)
            mx_raw = getattr(nu, f"{prefix}_max", None)
            ex_raw = getattr(nu, f"{prefix}_exact", None)
            mn, mx = _norm_bounds(mn_raw, mx_raw, ex_raw)
            if mn is None and mx is None:
                continue
            c_idx = code_idx[code_upper]
            sumv = sum(X(n_idx, d, c_idx) for d in phys_range)
            if mn is not None:
                m.Add(sumv >= mn)
            if mx is not None:
                m.Add(sumv <= mx)
            added += 1
    return added
