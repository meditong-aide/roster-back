"""근무표 후처리(그룹별 shift 분포 균등화) 모듈.

Phase1 완료 후 그룹별(D/E/N/M 패턴별) 동일 그룹 내 간호사 간 shift 분포를
균등화하는 post-processing 스텝. CP-SAT 모델 추가 변수 없이 roster 배열만 수정.

핵심 원칙:
- 같은 allowed_shift_set (frozenset) 에 속한 간호사끼리만 swap
- 1-shift 전담 그룹은 균등 개념 없음 → 제외
- swap 수락 조건: quality(=tolerance overshoot 총합) 개선 AND hard 위반 증가 없음
"""
import numpy as np

from services.cp_sat.allowed_shift_types import normalize_allowed_shift_codes

# shift별 그룹 내 편차 허용치 (Dw≤2, Ew≤3, Nw≤3, Mw≤2)
SHIFT_TOLERANCE = {"D": 2, "E": 3, "N": 3, "M": 2}
T_TOLERANCE = 2  # 그룹 내 총 근무일수(T) 편차


def _group_key(nu, work_codes: list[str], use_mid: bool) -> frozenset:
    """간호사의 allowed shift set 반환. 제약 없으면 전체 work_codes."""
    raw = getattr(nu, "is_night_nurse", None)
    allowed = normalize_allowed_shift_codes(raw, use_mid=use_mid)
    if not allowed:
        allowed = set(work_codes)
    return frozenset(allowed & set(work_codes))


def _build_groups(nurses, work_codes: list[str], use_mid: bool) -> dict:
    """{frozenset(allowed): [nurse_idx, ...]}."""
    groups: dict[frozenset, list[int]] = {}
    for n, nu in enumerate(nurses):
        key = _group_key(nu, work_codes, use_mid)
        if not key:
            continue
        groups.setdefault(key, []).append(n)
    return groups


def _shift_counts(roster: np.ndarray, n: int, shift_idx: int) -> int:
    return int(roster[n, :, shift_idx].sum())


def _total_counts(
    roster: np.ndarray, n: int, work_shift_indices: list[int]
) -> int:
    return sum(int(roster[n, :, si].sum()) for si in work_shift_indices)


def _quality(
    roster: np.ndarray,
    groups: dict,
    shift_idx_map: dict,
    tolerance_map: dict,
    t_tol: int,
) -> int:
    """모든 그룹/shift의 tolerance 초과분 합산. 낮을수록 좋음."""
    total = 0
    for allowed_set, members in groups.items():
        if len(members) < 2 or len(allowed_set) < 2:
            continue
        for code in allowed_set:
            if code not in shift_idx_map or code not in tolerance_map:
                continue
            s_idx = shift_idx_map[code]
            vals = [_shift_counts(roster, n, s_idx) for n in members]
            rng = max(vals) - min(vals)
            total += max(0, rng - tolerance_map[code])
        work_indices = [
            shift_idx_map[c] for c in allowed_set if c in shift_idx_map
        ]
        t_vals = [_total_counts(roster, n, work_indices) for n in members]
        rng_t = max(t_vals) - min(t_vals)
        total += max(0, rng_t - t_tol)
    return total


def _range_for_shift(
    roster: np.ndarray, members: list[int], shift_idx: int
) -> int:
    vals = [_shift_counts(roster, n, shift_idx) for n in members]
    return max(vals) - min(vals)


def _try_swap(
    roster_system,
    roster: np.ndarray,
    n_hi: int,
    n_lo: int,
    d: int,
    prev_quality: int,
    prev_viol: int,
    groups: dict,
    shift_idx_map: dict,
    tolerance_map: dict,
    t_tol: int,
    hard_violation_fn,
    target_shift_idx: int,
    target_members: list[int],
    target_range_before: int,
) -> bool:
    """(n_hi, d) ↔ (n_lo, d) swap 시도. 수락 시 True.

    수락 조건(우선순위):
      1) hard 위반 비증가 (필수)
      2) global quality strict 개선 (수락), OR
         global quality 동등 AND 타겟 shift range strict 개선 (수락)
    """
    orig_hi = roster[n_hi, d, :].copy()
    orig_lo = roster[n_lo, d, :].copy()
    roster[n_hi, d, :] = orig_lo
    roster[n_lo, d, :] = orig_hi
    roster_system.roster = roster
    new_viol = hard_violation_fn()
    if new_viol > prev_viol:
        roster[n_hi, d, :] = orig_hi
        roster[n_lo, d, :] = orig_lo
        roster_system.roster = roster
        return False
    new_quality = _quality(
        roster, groups, shift_idx_map, tolerance_map, t_tol
    )
    accepted = False
    if new_quality < prev_quality:
        accepted = True
    elif new_quality == prev_quality:
        new_target_range = _range_for_shift(
            roster, target_members, target_shift_idx
        )
        if new_target_range < target_range_before:
            accepted = True
    if not accepted:
        roster[n_hi, d, :] = orig_hi
        roster[n_lo, d, :] = orig_lo
        roster_system.roster = roster
        return False
    return True


def _balance_shift(
    roster_system,
    roster: np.ndarray,
    members: list[int],
    shift_idx: int,
    fixed_set: set,
    tol: int,
    groups: dict,
    shift_idx_map: dict,
    tolerance_map: dict,
    t_tol: int,
    hard_violation_fn,
    max_iters: int = 80,
) -> int:
    """단일 shift에 대해 그룹 내 편차 축소 swap 반복.

    개선점:
      - top-bottom 외 모든 (hi, lo) pair 를 gap 내림차순으로 시도
      - 동일 quality라도 타겟 shift range가 개선되면 수락
    """
    D = roster.shape[1]
    applied = 0
    for _ in range(max_iters):
        counts = [(_shift_counts(roster, n, shift_idx), n) for n in members]
        counts.sort()
        rng = counts[-1][0] - counts[0][0]
        if rng <= tol:
            break
        pairs: list[tuple[int, int, int]] = []
        for i in range(len(counts) - 1, -1, -1):
            for j in range(i):
                gap = counts[i][0] - counts[j][0]
                if gap <= 0:
                    continue
                pairs.append((gap, counts[i][1], counts[j][1]))
        pairs.sort(key=lambda x: -x[0])
        prev_q = _quality(
            roster, groups, shift_idx_map, tolerance_map, t_tol
        )
        prev_v = hard_violation_fn()
        moved = False
        for _gap, n_hi, n_lo in pairs:
            for d in range(D):
                if (n_hi, d) in fixed_set or (n_lo, d) in fixed_set:
                    continue
                if roster[n_hi, d, shift_idx] != 1:
                    continue
                if roster[n_lo, d, shift_idx] == 1:
                    continue
                if _try_swap(
                    roster_system, roster, n_hi, n_lo, d,
                    prev_q, prev_v, groups, shift_idx_map,
                    tolerance_map, t_tol, hard_violation_fn,
                    shift_idx, members, rng,
                ):
                    applied += 1
                    moved = True
                    break
            if moved:
                break
        if not moved:
            break
    return applied


def apply_phase1_post_swap(
    roster_system,
    roster: np.ndarray,
    hard_violation_fn,
    logger_prefix: str = "",
) -> tuple[np.ndarray, int]:
    """Phase1 roster에 대해 그룹별/shift별 편차 축소 swap 적용.

    Returns:
        (roster, swaps_applied)
    """
    cfg = roster_system.config
    use_mid = bool(getattr(cfg, "use_mid", False))
    shift_types = list(getattr(cfg, "shift_types", []) or [])
    work_codes = [c for c in ["D", "E", "N", "M"] if c in shift_types]
    if not work_codes:
        return roster, 0
    shift_idx_map = {c: shift_types.index(c) for c in work_codes}

    fixed_set: set = set()
    for fc in getattr(roster_system, "fixed_cells", []) or []:
        try:
            fixed_set.add((int(fc["nurse_index"]), int(fc["day_index"])))
        except Exception:
            continue

    groups = _build_groups(roster_system.nurses, work_codes, use_mid)
    original = roster.copy()
    init_viol = hard_violation_fn()
    init_quality = _quality(
        roster, groups, shift_idx_map, SHIFT_TOLERANCE, T_TOLERANCE
    )

    total_swaps = 0
    for allowed_set, members in groups.items():
        if len(members) < 2 or len(allowed_set) < 2:
            continue
        for shift_code in sorted(allowed_set):
            if shift_code not in shift_idx_map:
                continue
            if shift_code not in SHIFT_TOLERANCE:
                continue
            tol = SHIFT_TOLERANCE[shift_code]
            total_swaps += _balance_shift(
                roster_system, roster, members,
                shift_idx_map[shift_code], fixed_set, tol,
                groups, shift_idx_map, SHIFT_TOLERANCE, T_TOLERANCE,
                hard_violation_fn,
            )

    final_viol = hard_violation_fn()
    if final_viol > init_viol:
        roster[:] = original
        roster_system.roster = roster
        print(
            f"{logger_prefix}[Phase1-PostSwap] 위반 증가 감지 "
            f"({init_viol}→{final_viol}) → 전체 롤백"
        )
        return roster, 0

    final_quality = _quality(
        roster, groups, shift_idx_map, SHIFT_TOLERANCE, T_TOLERANCE
    )
    print(
        f"{logger_prefix}[Phase1-PostSwap] swaps={total_swaps} "
        f"quality={init_quality}→{final_quality} "
        f"viol={init_viol}→{final_viol} "
        f"groups={len(groups)}"
    )
    return roster, total_swaps
