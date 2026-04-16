"""야간(N) 및 D/E/N 균등 분배 결과 로깅 유틸.

해가 구해진 뒤 roster 행렬을 기준으로, 비야간전담 간호사별 N 개수와
목표(target) 대비 편차를 로그로 남긴다. N 전일 금지 간호사는 target 계산에서 제외.
"""

from __future__ import annotations

from typing import List, Optional

from services.cp_sat.objective_terms import _n_forbid_n_set
from services.cp_sat.allowed_shift_types import normalize_allowed_shift_codes, is_n_only_profile


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
            raw = getattr(nu, "is_night_nurse", None)
            is_n_only = is_n_only_profile(raw, use_mid=bool(getattr(cfg, "use_mid", False)))
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


def log_shift_distribution(
    roster_system,
    logger_prefix: str = "[CP-SAT]",
    join: Optional[List[int]] = None,
    leave: Optional[List[int]] = None,
    blocked_by_nurse: Optional[dict] = None,
) -> None:
    """D/E/N(/M) 균등 분배 결과를 shift별 요약 + 간호사별 상세로 출력한다."""
    try:
        cfg = roster_system.config
        D = roster_system.num_days
        N_nurses = len(roster_system.nurses)
        use_mid = bool(getattr(cfg, "use_mid", False))

        if join is None:
            join = [0] * N_nurses
        if leave is None:
            leave = [D - 1] * N_nurses

        target_codes = [c for c in ["D", "E", "N"] if c in cfg.shift_types]
        if use_mid and "M" in cfg.shift_types:
            target_codes.append("M")
        if not target_codes:
            return

        S = cfg.num_shifts
        normals: list[int] = []
        for i, nu in enumerate(roster_system.nurses):
            raw = getattr(nu, "is_night_nurse", None)
            if not is_n_only_profile(raw, use_mid=use_mid):
                normals.append(i)

        need_map: dict[str, int] = {}
        eligible_map: dict[str, int] = {}
        band_map: dict[str, tuple[int, int]] = {}
        for code in target_codes:
            s_idx = cfg.shift_types.index(code)
            if (
                hasattr(cfg, "daily_shift_requirements_by_day")
                and isinstance(cfg.daily_shift_requirements_by_day, list)
                and len(cfg.daily_shift_requirements_by_day) == D
            ):
                total = sum(
                    max(0, int((cfg.daily_shift_requirements_by_day[d] or {}).get(code, 0) or 0))
                    for d in range(D)
                )
            else:
                base = int((cfg.daily_shift_requirements or {}).get(code, 0) or 0)
                total = base * D
            need_map[code] = total
            eligible = [
                n for n in normals
                if code in (
                    normalize_allowed_shift_codes(
                        getattr(roster_system.nurses[n], "is_night_nurse", None),
                        use_mid=use_mid,
                    ) or {"D", "E", "N"}
                )
            ]
            eligible_map[code] = len(eligible) if eligible else 1
            low = total // max(1, eligible_map[code])
            high = low + (1 if (total % max(1, eligible_map[code])) else 0)
            band_map[code] = (low, high)

        nurse_info: list[dict] = []
        for n in normals:
            nu = roster_system.nurses[n]
            blocked_set = (
                blocked_by_nurse.get(n, set()) if blocked_by_nurse else set()
            )
            n_blocked = sum(1 for d in blocked_set if join[n] <= d <= leave[n])
            active = leave[n] - join[n] + 1 - n_blocked
            if active <= 0:
                continue
            allowed = normalize_allowed_shift_codes(
                getattr(nu, "is_night_nurse", None), use_mid=use_mid
            )
            if not allowed:
                allowed = {"D", "E", "N"}

            counts: dict[str, int] = {}
            bands: dict[str, tuple[int, int]] = {}
            for code in target_codes:
                s_idx = cfg.shift_types.index(code)
                actual = int(
                    sum(roster_system.roster[n, d, s_idx] for d in range(D))
                )
                counts[code] = actual
                if code in allowed:
                    low, high = band_map[code]
                    if n_blocked > 0 and active < D:
                        ratio = max(0.0, active / max(1, D))
                        low = max(0, round(low * ratio))
                        high = max(low, round(high * ratio))
                    bands[code] = (low, high)
                else:
                    bands[code] = (0, 0)

            name = getattr(nu, "name", "?")
            nid = getattr(nu, "db_id", getattr(nu, "nurse_id", "?"))
            allowed_str = "/".join(sorted(allowed)) if allowed else "DEN"
            nurse_info.append(
                {
                    "n": n,
                    "nid": nid,
                    "name": name,
                    "allowed": allowed_str,
                    "counts": counts,
                    "bands": bands,
                }
            )

        for code in target_codes:
            eligible = [
                ni
                for ni in nurse_info
                if code in ni["counts"]
                and ni["bands"].get(code, (0, 0))[1] > 0
            ]
            if not eligible:
                continue
            actuals = [ni["counts"][code] for ni in eligible]
            mn, mx = min(actuals), max(actuals)
            avg = sum(actuals) / len(actuals)
            band_sample = eligible[0]["bands"][code]
            print(
                f"{logger_prefix} [균등분배-결과] {code}: "
                f"nurses={len(eligible)}, "
                f"range=[{mn}..{mx}](편차{mx - mn}), "
                f"avg={avg:.1f}, band_sample={list(band_sample)}"
            )

        for ni in nurse_info:
            parts = []
            for code in target_codes:
                if code in ni["counts"]:
                    band = ni["bands"].get(code, (0, 0))
                    actual = ni["counts"][code]
                    in_band = "V" if band[0] <= actual <= band[1] else "X"
                    parts.append(f"{code}={actual}({list(band)}){in_band}")
            print(
                f"{logger_prefix} [균등분배-결과]   "
                f"n={ni['n']}, {ni['name']}({ni['nid']}), "
                f"allowed={ni['allowed']}, {', '.join(parts)}"
            )
    except Exception as exc:
        print(f"{logger_prefix} [균등분배-결과] 로그 출력 실패: {exc}")
