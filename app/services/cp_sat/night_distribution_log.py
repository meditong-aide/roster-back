"""야간(N) 균등 분배 결과 로깅 유틸.

해가 구해진 뒤 roster 행렬을 기준으로, 비야간전담 간호사별 N 개수와
목표(target) 대비 편차를 로그로 남긴다. N 전일 금지 간호사는 target 계산에서 제외.
"""

from __future__ import annotations

from typing import List, Optional

from services.cp_sat.objective_terms import _n_forbid_n_set


def log_n_even_distribution(
    roster_system,
    logger_prefix: str = "[CP-SAT]",
    join: Optional[List[int]] = None,
    leave: Optional[List[int]] = None,
) -> None:
    """현재 roster에서 N(야간) 균등 분배 결과를 한 줄 요약 + 간호사별 N 개수로 출력한다.

    비야간전담 중 N 가능 간호사만 대상으로 target = total_need_n // len(normals_can_n),
    N 전일 금지 간호사는 target에서 제외하고 편차는 N가능자만 표시(금지자는 target=0 기준).

    Args:
        roster_system: RosterSystem (roster, nurses, config, num_days 등 사용)
        logger_prefix: 로그 접두 문자열
        join: 간호사별 입사일 인덱스 (None이면 0..num_days-1 가정)
        leave: 간호사별 퇴사일 인덱스 (None이면 0..num_days-1 가정)
    """
    try:
        cfg = roster_system.config
        if not getattr(cfg, "even_nights", False):
            return
        night_idx = cfg.shift_types.index("N")
        D = roster_system.num_days
        N = len(roster_system.nurses)
        if join is None:
            join = [0] * N
        if leave is None:
            leave = [D - 1] * N

        normals: list[int] = []
        for i, nu in enumerate(roster_system.nurses):
            is_n_only = False
            raw = getattr(nu, "is_night_nurse", None)
            if isinstance(raw, list):
                allowed = {str(x).strip().upper() for x in raw if str(x).strip()}
                is_n_only = allowed == {"N"}
            elif raw == 3 or (raw is not None and raw not in (0, False)):
                is_n_only = True
            if not is_n_only:
                normals.append(i)

        n_forbid_n = _n_forbid_n_set(roster_system, join, leave)
        normals_can_n = [n for n in normals if n not in n_forbid_n]

        if not normals:
            print(f"{logger_prefix} [N균등-결과] 비야간전담 간호사 0명 → 스킵")
            return

        if (
            hasattr(cfg, "daily_shift_requirements_by_day")
            and isinstance(cfg.daily_shift_requirements_by_day, list)
            and len(cfg.daily_shift_requirements_by_day) == D
        ):
            daily_need_n = [
                int((cfg.daily_shift_requirements_by_day[d] or {}).get("N", 0) or 0)
                for d in range(D)
            ]
        else:
            base_n = int((cfg.daily_shift_requirements or {}).get("N", 0) or 0)
            daily_need_n = [base_n for _ in range(D)]

        total_need_n = sum(max(0, daily_need_n[d]) for d in range(D))
        target_can_n = total_need_n // len(normals_can_n) if normals_can_n else 0

        counts: list[tuple[int, str, int, int, int]] = []  # n, nid, actual, target_used, dev
        for n in normals:
            actual = int(
                sum(
                    roster_system.roster[n, d, night_idx]
                    for d in range(roster_system.num_days)
                )
            )
            target_used = target_can_n if n in normals_can_n else 0
            dev = actual - target_used
            nurse_id = getattr(
                roster_system.nurses[n], "db_id", getattr(roster_system.nurses[n], "nurse_id", "?")
            )
            counts.append((n, str(nurse_id), actual, target_used, dev))

        can_n_counts = [c for c in counts if c[3] == target_can_n and target_can_n > 0]
        min_n = min((c[2] for c in can_n_counts), default=0)
        max_n = max((c[2] for c in can_n_counts), default=0)
        dev_sum = sum(abs(c[4]) for c in counts)
        print(
            f"{logger_prefix} [N균등-결과] target={target_can_n}, total_need_n={total_need_n}, "
            f"normals_can_N={len(normals_can_n)}, n_forbid_N={len(n_forbid_n)}, "
            f"N개수 범위(가능자)=[{min_n}..{max_n}], 편차합(절대)={dev_sum}"
        )
        for n, nid, actual, target_used, dev in counts:
            name = getattr(roster_system.nurses[n], "name", "?")
            forbid_tag = " (N전일금지)" if n in n_forbid_n else ""
            print(
                f"{logger_prefix} [N균등-결과]   n={n}, id={nid}, name={name}{forbid_tag}, "
                f"N={actual}, target={target_used}, dev={dev:+d}"
            )
    except Exception as exc:
        print(f"{logger_prefix} [N균등-결과] 로그 출력 실패: {exc}")
