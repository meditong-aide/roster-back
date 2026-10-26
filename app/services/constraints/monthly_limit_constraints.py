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
from services.cp_sat.rule_tag import aux_add, hard_add


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
    """nurse_monthly_limit 의 n_max == 1 인 nurse 의 nurse_index set 을 반환.

    1N 금지(ban_single_night) hard 제약에서 면제 대상 — 운영자가 월간 한도로 N=1 을
    명시한 의도는 default 1N 금지 정책보다 우선한다.

    면제 조건:
        - nurse.n_max == 1 (또는 n_exact == 1 로 인해 정규화 시 max==1)
    """
    out: set[int] = set()
    for n_idx, nu in enumerate(rs.nurses):
        mn_raw = getattr(nu, "n_min", None)
        mx_raw = getattr(nu, "n_max", None)
        ex_raw = getattr(nu, "n_exact", None)
        _, mx = _norm_bounds(mn_raw, mx_raw, ex_raw)
        if mx == 1:
            out.add(n_idx)
    return out


def has_upper_caps(rs: RosterSystem) -> bool:
    """정확값이 아닌 월 상한(`*_max`)이 하나라도 있는가 — stage1 초과칸 재시도의 대상 판정."""
    for nu in rs.nurses:
        for prefix, _code in _SHIFT_PREFIX_TO_CODE:
            if getattr(nu, f"{prefix}_max", None) is not None and getattr(nu, f"{prefix}_exact", None) is None:
                return True
    return False


def add_monthly_limit_constraints(
    m,
    rs: RosterSystem,
    X,
    join: list[int],
    leave: list[int],
    upper_slack: bool = False,
) -> int:
    """nurse별 월간 D/E/N/O 한도 hard 제약 추가.

    upper_slack=True 면 정확값이 아닌 **상한**(`*_max`)만 `합 <= 상한 + 초과칸` 으로 걸고
    초과칸 변수를 `m._monthly_cap_slacks` 에 모은다. 호출 측(fallback_lex stage1)이 초과칸을
    최우선으로 최소화하고 **0 이 아니면 그 해를 버린다** — 하드 의미는 그대로다.
    ★ 왜 필요한가: 상한이 빡빡하면(성남 중환자실-RN 2026-11 · N 6~7 · OFF 정확히 9)
      하드로는 첫 해까지 53~82초가 걸려 60초 생성의 stage1 시도(13초)가 늘 UNKNOWN 이었다.
      같은 모델을 초과칸 최소화로 바꾸면 3초 안에 초과 0 인 최적해가 나온다(실측).

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

    # MUS 추출용 hard assumption registry — 모델에 attach 된 경우만 wrap.
    _registry = getattr(m, "_cpsat_assumption_registry", None)
    _add_hard_fn = None
    if _registry is not None:
        try:
            from services.cp_sat.hard_assumption import add_hard as _add_hard_fn
        except Exception:
            _add_hard_fn = None

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

            # MUS용 wrap — n_min/n_max/n_exact 모두 같은 nurse의 같은 shift 정책이라
            # 하나의 assumption literal로 묶음 (per-(nurse, shift))
            if _add_hard_fn is not None and _registry is not None:
                _is_exact = ex_raw is not None
                _meta_value = ex_raw if _is_exact else {"min": mn, "max": mx}
                _label = f"{code_upper}_exact" if _is_exact else f"{code_upper} ∈ [{mn}, {mx}]"
                _node_id = f"monthly_limit:{code_upper.lower()}:nurse_{n_idx}"
                _type = "MonthlyNExactNode" if (code_upper == "N" and _is_exact) else "MonthlyLimitNode"
                _hint = (
                    f"이 간호사의 {code_upper}={ex_raw} 정확치를 풀거나 범위로 바꾸세요."
                    if _is_exact
                    else f"이 간호사의 {code_upper} 범위 [{mn}, {mx}]를 완화하세요."
                )
                _assume_name = f"MonthlyLimit:{code_upper}:nurse_{n_idx}"
                _meta = {
                    "node_id": _node_id,
                    "type": _type,
                    "label": _label,
                    "value": _meta_value,
                    "scope": "nurse",
                    "scope_key": f"nurse_{n_idx}",
                    "pattern": f"monthly_limit_{code_upper.lower()}",
                    "nurse_id": str(getattr(nu, "nurse_id", n_idx)),
                    "human_message_ko": f"이 간호사의 월 {code_upper} 한도 ({_label})",
                    "resolution_hint": _hint,
                }
                if mn is not None:
                    _add_hard_fn(m, _registry, name=_assume_name, constraint_expr=(sumv >= mn), meta=_meta)
                if mx is not None:
                    _add_hard_fn(m, _registry, name=_assume_name, constraint_expr=(sumv <= mx), meta=_meta)
            else:
                if mn is not None:
                    hard_add(m, f"monthly_limit_{code_upper.lower()}", sumv >= mn)
                if mx is not None and upper_slack and ex_raw is None:
                    _cap_slack = m.NewIntVar(0, len(phys_range), f"capslack_{n_idx}_{code_upper}")
                    aux_add(m, "monthly_cap_slack", sumv <= mx + _cap_slack)
                    if not hasattr(m, "_monthly_cap_slacks"):
                        m._monthly_cap_slacks = []
                    m._monthly_cap_slacks.append(_cap_slack)
                elif mx is not None:
                    hard_add(m, f"monthly_limit_{code_upper.lower()}", sumv <= mx)
            added += 1
    return added
