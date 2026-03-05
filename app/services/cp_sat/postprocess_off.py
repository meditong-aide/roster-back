"""근무표 후처리(OFF 재배치/불필요 OFF 제거) 모듈.

이 모듈은 `services/cp_sat_basic.py`에 있던 후처리 로직을 기능 단위로 분리한 것입니다.
해결(솔브) 이후 로스터를 미세 조정하는 용도로만 사용하며, 하드 제약 위반이 증가하면 롤백합니다.
"""

from __future__ import annotations

from datetime import timedelta

import numpy as np

from db.nurse_config import Nurse
from services.roster_system import RosterSystem
from services.cp_sat.hardcoded_weights import (
    GRADE_POSTPROCESS_BONUS,
    TEAM_BALANCE_POSTPROCESS_BONUS,
)


def postprocess_rebalance_off(
    *,
    roster_system: RosterSystem,
    logger_prefix: str = "[CP-SAT-Basic]",
    max_attempts: int = 30,
) -> None:
    """후처리로 불필요한 O를 당겨와 연속근무 위반을 완화합니다.

    수식(연속근무 길이): run_len = work_days 연속 길이 (예: DDDDDEE → run_len=6)
    수식 예시: D D D D E E (6일 연속 근무) → run_len=6

    전략:
        - 한 간호사 내에서 O가 있는 다른 날짜와 장시간 연속근무 구간의 근무를 교환
        - 밤근무(N)는 2N2O/야간 연속 규칙을 해치지 않도록 스왑 대상에서 제외
        - 하드 제약 위반 수가 늘어나면 롤백

    Args:
        roster_system: 근무표 시스템
        logger_prefix: 로그 접두사
        max_attempts: 최대 스왑 시도 횟수
    """
    cfg = roster_system.config
    off_idx = cfg.shift_types.index("O") if "O" in cfg.shift_types else None
    night_idx = cfg.shift_types.index("N") if "N" in cfg.shift_types else None
    if off_idx is None:
        return

    fixed_cells = {
        (c["nurse_index"], c["day_index"])
        for c in (getattr(roster_system, "fixed_cells", []) or [])
    }
    K = getattr(cfg, "max_consecutive_work_days", None)
    if not isinstance(K, int) or K <= 0:
        return

    def _max_run(n_idx: int) -> tuple[int, int, int] | None:
        """가장 긴 연속근무 구간(start, end, length)을 반환."""
        best = None
        run_start = None
        run_len = 0
        D = roster_system.num_days
        for d in range(D):
            is_off = roster_system.roster[n_idx, d, off_idx] == 1
            if is_off:
                if run_len > 0 and (best is None or run_len > best[2]):
                    best = (run_start, d - 1, run_len)
                run_start, run_len = None, 0
            else:
                if run_start is None:
                    run_start = d
                run_len += 1
        if run_len > 0 and (best is None or run_len > best[2]):
            best = (run_start, D - 1, run_len)
        return best

    def _swap_off(n_idx: int, work_day: int, off_day: int, shift_idx: int) -> None:
        roster_system.roster[n_idx, work_day, shift_idx] = 0
        roster_system.roster[n_idx, work_day, off_idx] = 1
        roster_system.roster[n_idx, off_day, off_idx] = 0
        roster_system.roster[n_idx, off_day, shift_idx] = 1

    def _find_shift_idx(n_idx: int, day: int) -> int | None:
        vec = roster_system.roster[n_idx, day]
        ones = np.where(vec == 1)[0]
        if len(ones) == 1:
            return int(ones[0])
        return None

    base_viol = len(roster_system._find_violations())
    accepted = 0
    N = len(roster_system.nurses)

    for n_idx in range(N):
        if accepted >= max_attempts:
            break
        best_run = _max_run(n_idx)
        if not best_run or best_run[2] <= K:
            continue
        start, end, run_len = best_run
        # 연속근무 중앙부부터 완화 시도
        target_days = list(range(start, end + 1))
        target_days.sort(key=lambda x: abs(x - (start + end) // 2))

        # O 후보: 동일 간호사의 O 날짜 중 고정 아닌 날
        off_candidates = [
            d
            for d in range(roster_system.num_days)
            if roster_system.roster[n_idx, d, off_idx] == 1 and (n_idx, d) not in fixed_cells
        ]
        if not off_candidates:
            continue

        for work_day in target_days:
            if (n_idx, work_day) in fixed_cells:
                continue
            shift_idx = _find_shift_idx(n_idx, work_day)
            if shift_idx is None or shift_idx == off_idx:
                continue
            # N 이동은 2N2O 리스크가 크므로 제외
            if night_idx is not None and shift_idx == night_idx:
                continue

            for off_day in off_candidates:
                if off_day == work_day:
                    continue
                if (n_idx, off_day) in fixed_cells:
                    continue

                # 스왑 적용
                _swap_off(n_idx, work_day, off_day, shift_idx)
                new_run = _max_run(n_idx)
                new_viol = len(roster_system._find_violations())

                ok = True
                if new_run and new_run[2] > K:
                    ok = False
                if new_viol > base_viol:
                    ok = False

                if ok:
                    accepted += 1
                    base_viol = new_viol
                    print(
                        f"{logger_prefix} [PostOff] swap accepted n={n_idx}, "
                        f"work_day={work_day+1}→O, off_day={off_day+1}→{cfg.shift_types[shift_idx]}, "
                        f"max_run {run_len}->{new_run[2] if new_run else 0}, viol {new_viol}"
                    )
                    break
                # 롤백
                _swap_off(n_idx, off_day, work_day, shift_idx)
            if accepted >= max_attempts:
                break
    final_viol = len(roster_system._find_violations())
    print(
        f"{logger_prefix} [PostOff] 완료: 수용 스왑 {accepted}건, "
        f"위반 {base_viol}->{final_viol}, max_attempts={max_attempts}"
    )


def postprocess_trim_extra_offs(
    *,
    roster_system: RosterSystem,
    logger_prefix: str = "[CP-SAT-Basic]",
    max_changes: int = 80,
    prefer_shortage: bool = True,
) -> int:
    """필수/강제 OFF를 보존하면서 불필요한 OFF를 근무로 교체한다.

    Notes:
        - N-only 간호사는 제외한다.
        - 개인 OFF 상한은 `global_monthly_off_days + standard_personal_off_days + max_extra_off_days`
          를 기본으로 하되, 가능한 10을 넘지 않도록 클램프한다.
        - 회복 OFF(2N/3N→2O), 고정/예외/주휴 OFF는 건드리지 않는다.

    Args:
        roster_system: 근무표 시스템
        logger_prefix: 로그 접두사
        max_changes: 최대 변경 횟수
        prefer_shortage: 커버리지 부족(need-assigned)을 우선 메우는 방향으로 교체할지 여부

    Returns:
        적용된 교체 횟수
    """
    cfg = roster_system.config
    if "O" not in cfg.shift_types:
        return 0

    off_idx = cfg.shift_types.index("O")
    night_idx = cfg.shift_types.index("N") if "N" in cfg.shift_types else None
    # W를 후처리 교체 대상에서 제외하고, 커버리지 코드(D/E/N 등)만 사용
    work_codes = [
        code
        for code in (cfg.daily_shift_requirements or {}).keys()
        if code in cfg.shift_types and code not in {"O", "W"}
    ]
    work_shift_indices = [cfg.shift_types.index(code) for code in work_codes]
    if not work_shift_indices:
        # fallback: shift_types에서 O/W 제외
        work_shift_indices = [
            i for i, code in enumerate(cfg.shift_types) if code not in {"O", "W"}
        ]
    if not work_shift_indices:
        return 0

    ############## preceptee 추가 ##############
    preceptee_follow = bool(getattr(cfg, 'preceptee_on', False))
    preceptee_indices = set()
    if preceptee_follow:
        id_to_idx = {nu.db_id: n for n, nu in enumerate(roster_system.nurses)}
        for n, nu in enumerate(roster_system.nurses):
            pid = getattr(nu, 'preceptor_id', None)
            if pid and pid in id_to_idx:
                preceptee_indices.add(n)
        if preceptee_indices:
            print(f"{logger_prefix} [POSTPROCESS PROTECT] 프리셉티 보호 적용: {len(preceptee_indices)}명 (후처리 OFF 변경 스킵)")
    ############## preceptee 추가 ##############

    # 현재 로스터 스냅샷을 보존하여 회복 OFF(2N/3N→2O)를 안전하게 식별한다.
    roster_snapshot = roster_system.roster.copy()

    num_days = roster_system.num_days
    weekend_days = {
        d
        for d in range(num_days)
        if (roster_system.target_month + timedelta(days=d)).weekday() >= 5
    }

    fixed_cells = getattr(roster_system, "fixed_cells", []) or []
    fixed_off_cells = {
        (c.get("nurse_index"), c.get("day_index"))
        for c in fixed_cells
        if str(c.get("shift", "")).upper() == "O"
    }
    off_exception_cells = set(getattr(cfg, "off_exception_cells", []) or [])
    weekly_off_map = (
        getattr(roster_system, "weekly_off_by_idx", {})
        if isinstance(getattr(roster_system, "weekly_off_by_idx", {}), dict)
        else {}
    )
    weekly_off_cells = {(n, d) for n, days in weekly_off_map.items() for d in (days or [])}

    # 주휴 인접 OFF 셀 식별 (off_placement_mode가 활성화된 경우)
    weekly_off_adjacent_cells: set[tuple[int, int]] = set()
    # raw_off_placement_mode = int(getattr(cfg, "off_placement_mode", 0) or 0)
    # if raw_off_placement_mode != 0:
    #     print("[PostOff] OffPlacementMode deprecated: forcing off_placement_mode=0")
    # off_placement_mode = 0
    # if off_placement_mode > 0 and weekly_off_map:
    #     for n_idx, day_list in weekly_off_map.items():
    #         if n_idx >= len(roster_system.nurses):
    #             continue
    #         if bool(getattr(roster_system.nurses[n_idx], "is_weekend_off", False)):
    #             continue
    #         for d_raw in day_list or []:
    #             try:
    #                 d = int(d_raw)
    #             except Exception:
    #                 continue
    #             if 0 <= d < num_days:
    #                 # 주휴 앞/뒤 OFF 셀 보호
    #                 if off_placement_mode == 1:  # 앞/뒤
    #                     if d - 1 >= 0:
    #                         weekly_off_adjacent_cells.add((n_idx, d - 1))
    #                     if d + 1 < num_days:
    #                         weekly_off_adjacent_cells.add((n_idx, d + 1))
    #                 elif off_placement_mode == 2:  # 앞만
    #                     if d - 1 >= 0:
    #                         weekly_off_adjacent_cells.add((n_idx, d - 1))

    # 휴가 코드 식별 (생, 휴 등)
    vacation_codes = {code for code in cfg.shift_types if code not in {"D", "E", "N", "O", "W"} and code in {"생", "휴"}}

    pref_matrix = getattr(roster_system, "preference_matrix", None)
    grade_config = getattr(roster_system, "grade_config", None)
    grade_strategy = str(getattr(roster_system, "grade_strategy", "BASE") or "BASE").upper()
    team_map = {n_idx: getattr(n, "team_id", None) for n_idx, n in enumerate(roster_system.nurses)}

    base_min_off = int(
        getattr(cfg, "global_monthly_off_days", 0) + getattr(cfg, "standard_personal_off_days", 0)
    )
    extra_off = int(getattr(cfg, "max_extra_off_days", 0))
    target_cap_global = max(base_min_off, min(base_min_off + extra_off, 10))

    def is_n_only(nurse: Nurse) -> bool:
        raw = getattr(nurse, "is_night_nurse", None)
        if isinstance(raw, list):
            allowed = {str(x).strip().upper() for x in raw if str(x).strip()}
            return allowed == {"N"}
        return raw == 3 or (raw is not None and raw != 0 and raw is not False)

    def count_vacation_days(n_idx: int) -> int:
        """휴가 일수를 카운트한다."""
        if not vacation_codes:
            return 0
        count = 0
        for d in range(num_days):
            vec = roster_system.roster[n_idx, d]
            for vac_code in vacation_codes:
                if vac_code in cfg.shift_types:
                    vac_idx = cfg.shift_types.index(vac_code)
                    if vec[vac_idx] == 1:
                        count += 1
                        break
        return count

    def min_off_required(n_idx: int = None) -> int:
        """최소 주휴일(O) 개수를 반환한다.
        
        Args:
            n_idx: 간호사 인덱스. None이면 기본값 반환, 지정되면 해당 간호사의 휴가를 고려.
        
        Returns:
            최소 주휴일 개수. 휴가가 많은 경우에도 최소 주휴일은 보장되어야 함.
        """
        if n_idx is None:
            return min(base_min_off, num_days)
        # 개인별 휴가 일수 고려
        vacation_cnt = count_vacation_days(n_idx)
        # 최소 주휴일 = base_min_off (휴가는 별도로 계산되므로 O는 항상 base_min_off 이상이어야 함)
        return min(base_min_off, num_days)

    def min_o_required(n_idx: int) -> int:
        """최소 주휴일(O) 개수를 반환한다. 휴가는 별도로 계산되므로 O는 항상 base_min_off 이상이어야 함.
        
        휴가가 많아도 최소 주휴일(O)은 base_min_off 이상이어야 함.
        예: base_min_off=8, 휴가=4개 → 최소 O=8개 (총 휴무일=12개)
        """
        return min(base_min_off, num_days)

    def max_off_allowed(_nurse: Nurse) -> int:
        """후처리에서 적용할 개인별 OFF 상한(주말 휴무자는 평일만 제한)."""
        base_cap = max(base_min_off, min(base_min_off + extra_off, target_cap_global))
        if bool(getattr(_nurse, "is_weekend_off", False)):
            weekend_cnt = sum(1 for d in weekend_days if 0 <= d < num_days)
            return max(0, base_cap - weekend_cnt)
        return base_cap

    # def current_off_count(n_idx: int, _nurse: Nurse) -> int:
    #     """후처리 기준으로 OFF 개수를 계산한다."""
    #     if bool(getattr(_nurse, "is_weekend_off", False)):
    #         return sum(
    #             1
    #             for d in range(num_days)
    #             if roster_system.roster[n_idx, d, off_idx] == 1 and d not in weekend_days
    #         )
    #     return int(np.sum(roster_system.roster[n_idx, :, off_idx]))

    def current_off_count(n_idx: int, nurse: Nurse) -> int:
        cnt = 0
        for d in range(num_days):
            # 순수 O
            if roster_system.roster[n_idx, d, off_idx] == 1:
                # 휴가/공가/생리는 OFF 아님
                if (n_idx, d) in off_exception_cells:
                    continue
                cnt += 1
            # 주휴는 OFF로 포함
            elif (n_idx, d) in weekly_off_cells:
                cnt += 1
        return cnt
    
    def current_o_count(n_idx: int, _nurse: Nurse) -> int:
        """후처리 기준으로 주휴일(O) 개수만 계산한다 (휴가 제외)."""
        if bool(getattr(_nurse, "is_weekend_off", False)):
            return sum(
                1
                for d in range(num_days)
                if roster_system.roster[n_idx, d, off_idx] == 1 and d not in weekend_days
            )
        return int(np.sum(roster_system.roster[n_idx, :, off_idx]))


    def build_recovery_off_cells() -> set[tuple[int, int]]:
        cells: set[tuple[int, int]] = set()
        if night_idx is None:
            return cells
        for n_idx, _n in enumerate(roster_system.nurses):
            d = 0
            while d < num_days:
                if roster_snapshot[n_idx, d, night_idx] != 1:
                    d += 1
                    continue
                while d + 1 < num_days and roster_snapshot[n_idx, d + 1, night_idx] == 1:
                    d += 1
                end = d
                length = 1
                start = end
                while start - 1 >= 0 and roster_snapshot[n_idx, start - 1, night_idx] == 1:
                    start -= 1
                    length += 1
                if cfg.two_offs_after_three_nig and length >= 3:
                    for off_d in (end + 1, end + 2):
                        if off_d < num_days:
                            cells.add((n_idx, off_d))
                if cfg.two_offs_after_two_nig and length >= 2:
                    for off_d in (end + 1, end + 2):
                        if off_d < num_days:
                            cells.add((n_idx, off_d))
                d += 1
        return cells

    recovery_off_cells = build_recovery_off_cells()

    def is_recovery_off(n_idx: int, d_idx: int) -> bool:
        return (n_idx, d_idx) in recovery_off_cells

    def day_deficits() -> dict[int, dict[int, int]]:
        deficits: dict[int, dict[int, int]] = {}
        for d_idx in range(num_days):
            if (
                hasattr(cfg, "daily_shift_requirements_by_day")
                and isinstance(cfg.daily_shift_requirements_by_day, list)
                and d_idx < len(cfg.daily_shift_requirements_by_day)
            ):
                need_map = cfg.daily_shift_requirements_by_day[d_idx]
            else:
                need_map = cfg.daily_shift_requirements
            for code, req in (need_map or {}).items():
                if code not in cfg.shift_types or code == "O":
                    continue
                s_idx = cfg.shift_types.index(code)
                assigned = int(np.sum(roster_system.roster[:, d_idx, s_idx]))
                need = int(req or 0)
                if assigned < need:
                    deficits.setdefault(d_idx, {})[s_idx] = need - assigned
        return deficits

    deficits_by_day = day_deficits()
    base_viol = len(roster_system._find_violations())
    changes = 0

    def team_shift_count(d_idx: int, s_idx: int, team_id) -> int:
        if team_id is None:
            return 0
        cnt = 0
        for n_i, nurse in enumerate(roster_system.nurses):
            if getattr(nurse, "team_id", None) != team_id:
                continue
            if roster_system.roster[n_i, d_idx, s_idx] == 1:
                cnt += 1
        return cnt

    def grade_bonus(n_idx: int, d_idx: int, s_idx: int) -> float:
        """GRADE 전략 시 소프트 보너스(가벼운 휴리스틱)."""
        if grade_strategy != "GRADE" or not isinstance(grade_config, dict):
            return 0.0
        grade_val = getattr(roster_system.nurses[n_idx], "grade", None)
        try:
            grade_int = int(grade_val) if grade_val is not None else None
        except Exception:
            grade_int = None
        constraints_map = (
            grade_config.get("constraints")
            or grade_config.get("constraints_json")
            or {}
        )
        shift_code = cfg.shift_types[s_idx]
        req_map = constraints_map.get(shift_code) if isinstance(constraints_map, dict) else None
        if not isinstance(req_map, dict) or grade_int is None:
            return 0.0
        need = int(req_map.get(str(grade_int), 0) or 0)
        if need <= 0:
            return 0.0
        assigned = 0
        for n_i, _ in enumerate(roster_system.nurses):
            if int(getattr(roster_system.nurses[n_i], "grade", grade_int) or grade_int) != grade_int:
                continue
            if roster_system.roster[n_i, d_idx, s_idx] == 1:
                assigned += 1
        gap = need - assigned
        return GRADE_POSTPROCESS_BONUS if gap > 0 else 0.0

    def is_off_like(n_idx: int, d_idx: int) -> bool:
        if d_idx < 0 or d_idx >= num_days:
            return False
        if roster_system.roster[n_idx, d_idx, off_idx] == 1:
            return True
        if (n_idx, d_idx) in fixed_off_cells or (n_idx, d_idx) in off_exception_cells or (n_idx, d_idx) in weekly_off_cells:
            return True
        if is_recovery_off(n_idx, d_idx):
            return True
        return False

    def assigned_shift_idx(n_idx: int, d_idx: int) -> int | None:
        if d_idx < 0 or d_idx >= num_days:
            return None
        vec = roster_system.roster[n_idx, d_idx]
        ones = np.where(vec == 1)[0]
        return int(ones[0]) if len(ones) == 1 else None

    def violates_transition(n_idx: int, d_idx: int, s_idx: int) -> bool:
        """E→D, N→E, D→N, N→D 금지 설정을 양옆 모두에서 검사."""
        ban_n_to_d = bool(getattr(cfg, "ban_n_to_d", True))
        ban_e_to_d = bool(getattr(cfg, "ban_e_to_d", True))
        ban_n_to_e = bool(getattr(cfg, "ban_n_to_e", True))
        ban_d_to_n = bool(getattr(cfg, "ban_d_to_n", True))

        day_idx = cfg.shift_types.index("D") if "D" in cfg.shift_types else None
        eve_idx = cfg.shift_types.index("E") if "E" in cfg.shift_types else None
        _night_idx = cfg.shift_types.index("N") if "N" in cfg.shift_types else None
        if day_idx is None or eve_idx is None or _night_idx is None:
            return False

        prev_idx = assigned_shift_idx(n_idx, d_idx - 1)
        next_idx = assigned_shift_idx(n_idx, d_idx + 1)

        if prev_idx is not None:
            if ban_e_to_d and prev_idx == eve_idx and s_idx == day_idx:
                return True
            if ban_n_to_e and prev_idx == _night_idx and s_idx == eve_idx:
                return True
            if ban_n_to_d and prev_idx == _night_idx and s_idx == day_idx:
                return True
            if ban_d_to_n and prev_idx == day_idx and s_idx == _night_idx:
                return True
        if next_idx is not None:
            if ban_e_to_d and s_idx == eve_idx and next_idx == day_idx:
                return True
            if ban_n_to_e and s_idx == _night_idx and next_idx == eve_idx:
                return True
            if ban_n_to_d and s_idx == _night_idx and next_idx == day_idx:
                return True
            if ban_d_to_n and s_idx == day_idx and next_idx == _night_idx:
                return True
        return False

    def would_create_single_night(n_idx: int, d_idx: int, s_idx: int) -> bool:
        """후처리에서 N 배정 시 1N 금지를 위반하는지 검사한다.

        수식: 1N 금지 → N(d) ≤ N(d-1) + N(d+1) (예: d=5이면 N5 ≤ N4+N6)
        수식 예시: N5=1, N4=0, N6=0 → 1 ≤ 0+0(불가)
        """
        if not bool(getattr(cfg, "not_one_night", False)):
            return False
        if "N" not in cfg.shift_types:
            return False
        night_idx_local = cfg.shift_types.index("N")
        if s_idx != night_idx_local:
            return False
        prev_is_n = d_idx - 1 >= 0 and roster_system.roster[n_idx, d_idx - 1, night_idx_local] == 1
        next_is_n = (
            d_idx + 1 < num_days and roster_system.roster[n_idx, d_idx + 1, night_idx_local] == 1
        )
        return not (prev_is_n or next_is_n)

    def would_violate_recovery_off(n_idx: int, d_idx: int, s_idx: int) -> bool:
        """후처리에서 N 배정 시 2N/3N→2OFF 규칙을 위반하는지 검사한다.

        규칙:
            - 2N→2OFF: 연속 N이 2일 이상이면, 마지막 N 다음 2일은 O여야 함.
            - 3N→2OFF: 연속 N이 3일 이상이면, 마지막 N 다음 2일은 O여야 함.
        """
        if "N" not in cfg.shift_types:
            return False
        night_idx_local = cfg.shift_types.index("N")
        if s_idx != night_idx_local:
            return False
        if not bool(getattr(cfg, "two_offs_after_two_nig", False)) and not bool(
            getattr(cfg, "two_offs_after_three_nig", False)
        ):
            return False

        def is_n(d_idx_check: int) -> bool:
            if d_idx_check < 0 or d_idx_check >= num_days:
                return False
            if d_idx_check == d_idx:
                return True
            return roster_system.roster[n_idx, d_idx_check, night_idx_local] == 1

        # 연속 N 블록 길이 계산 (가상으로 d_idx에 N을 배치한 상태)
        start = d_idx
        while start - 1 >= 0 and is_n(start - 1):
            start -= 1
        end = d_idx
        while end + 1 < num_days and is_n(end + 1):
            end += 1
        length = end - start + 1

        need_two_off = False
        if bool(getattr(cfg, "two_offs_after_three_nig", False)) and length >= 3:
            need_two_off = True
        if bool(getattr(cfg, "two_offs_after_two_nig", False)) and length >= 2:
            need_two_off = True
        if not need_two_off:
            return False

        for off_d in (end + 1, end + 2):
            if off_d >= num_days:
                continue
            if not is_off_like(n_idx, off_d):
                return True
        return False

    for n_idx, nurse in enumerate(roster_system.nurses):
        if n_idx in preceptee_indices:
            continue  # 프리셉티는 프리셉터 팔로우 → 후처리 OFF 변경 스킵
        if is_n_only(nurse):
            continue

        current_off = current_off_count(n_idx, nurse)
        max_allowed = max_off_allowed(nurse)
        if current_off <= max_allowed:
            continue

        # 최소 주휴일(O) 개수 체크 (휴가는 별도로 계산되므로 O는 항상 base_min_off 이상이어야 함)
        required_min_o = min_o_required(n_idx)
        current_o = current_o_count(n_idx, nurse)
        
        candidates: list[tuple[int, int, float, int]] = []
        for d_idx in range(num_days):
            if roster_system.roster[n_idx, d_idx, off_idx] != 1:
                continue
            if (n_idx, d_idx) in fixed_off_cells:
                continue
            if (n_idx, d_idx) in off_exception_cells or (n_idx, d_idx) in weekly_off_cells:
                continue
            if (n_idx, d_idx) in weekly_off_adjacent_cells:
                continue  # 주휴 인접 OFF 보호
            if is_recovery_off(n_idx, d_idx):
                continue
            if bool(getattr(nurse, "is_weekend_off", False)) and d_idx in weekend_days:
                continue

            adj_off = int(is_off_like(n_idx, d_idx - 1)) + int(is_off_like(n_idx, d_idx + 1))
            has_deficit = int(d_idx in deficits_by_day and bool(deficits_by_day[d_idx]))
            best_pref = 0.0
            if pref_matrix is not None:
                best_pref = max(pref_matrix[n_idx, d_idx, s_idx] for s_idx in work_shift_indices)

            candidates.append((adj_off, -has_deficit, -best_pref, d_idx))

        candidates.sort()

        for _, _, _, d_idx in candidates:
            if changes >= max_changes:
                break

            target_shift_idx = None
            move_reason = "unknown"
            if prefer_shortage and d_idx in deficits_by_day and deficits_by_day[d_idx]:
                target_shift_idx = max(deficits_by_day[d_idx].items(), key=lambda x: x[1])[0]
                move_reason = "shortage"
            else:
                best_score = None
                for s_idx in work_shift_indices:
                    if s_idx == off_idx:
                        continue
                    score = pref_matrix[n_idx, d_idx, s_idx] if pref_matrix is not None else 0
                    if grade_strategy == "TEAM":
                        team_id = team_map.get(n_idx)
                        score += TEAM_BALANCE_POSTPROCESS_BONUS * team_shift_count(
                            d_idx, s_idx, team_id
                        )
                        move_reason = "team_balance"
                    elif grade_strategy == "GRADE":
                        score += grade_bonus(n_idx, d_idx, s_idx)
                        move_reason = "grade_bonus"
                    if best_score is None or score > best_score:
                        best_score = score
                        target_shift_idx = s_idx
                if move_reason == "unkonwn":
                    move_reason = "preference" if pref_matrix is not None else "default"
            if target_shift_idx is None:
                continue

            if would_create_single_night(n_idx, d_idx, target_shift_idx):
                continue
            if would_violate_recovery_off(n_idx, d_idx, target_shift_idx):
                continue
            if violates_transition(n_idx, d_idx, target_shift_idx):
                continue

            roster_system.roster[n_idx, d_idx, off_idx] = 0
            roster_system.roster[n_idx, d_idx, target_shift_idx] = 1
            new_off = current_off_count(n_idx, nurse)
            new_o = current_o_count(n_idx, nurse)
            new_viol = len(roster_system._find_violations())

            # 최소 주휴일(O) 개수 체크: 휴가가 많아도 최소 주휴일(O)은 보장되어야 함
            if new_o < required_min_o or new_viol > base_viol:
                roster_system.roster[n_idx, d_idx, target_shift_idx] = 0
                roster_system.roster[n_idx, d_idx, off_idx] = 1
                continue

            print(
                f"{logger_prefix} [TrimExtraOff] move n={n_idx}, "
                f"name={getattr(nurse, 'name', '?')}, "
                f"day={d_idx + 1}, O->"
                f"{cfg.shift_types[target_shift_idx]}, "
                f"reason={move_reason}, off={current_off}->{new_off}, "
                f"viol={base_viol}->{new_viol}"
            )
            changes += 1
            base_viol = new_viol
            current_off = new_off

            if d_idx in deficits_by_day and target_shift_idx in deficits_by_day[d_idx]:
                deficits_by_day[d_idx][target_shift_idx] = max(
                    0, deficits_by_day[d_idx][target_shift_idx] - 1
                )
                if deficits_by_day[d_idx][target_shift_idx] == 0:
                    deficits_by_day[d_idx].pop(target_shift_idx, None)
                if not deficits_by_day[d_idx]:
                    deficits_by_day.pop(d_idx, None)

            if current_off <= max_allowed:
                break

    if changes:
        print(f"{logger_prefix} [TrimExtraOff] 불필요 OFF 교체 {changes}건")
    return changes

