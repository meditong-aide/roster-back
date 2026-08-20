"""Per-nurse 시퀀스 feasibility — 개인축 조기진단 (solve 전, blocking).

한 간호사만 놓고, 그 사람의 고정셀 + 시퀀스 규칙(전이금지/연속근무/연속야간/회복/1N)과
월 N 범위가 서로 물려 **혼자서도 완성 불가능**한지를 30일 DP 로 판정한다.
혼자 불가능하면 전체도 불가능(증명) → aggregate/max-flow 가 못 보는
'고정셀 × 시퀀스' 배치 충돌과 'n_exact vs 회복상한' 을 solve 전에 잡는다.

한계(설계): 개인 단독 판정만 한다(cross-nurse 커버리지는 max-flow 층 담당).
OFF 예산 최소는 aggregate capacity 층이 담당하므로 여기선 다루지 않는다.
DP 는 상한만 조이는 '증명된 불가능'만 반환 → false positive 없음.

상태: (day, prev, consec_work, consec_night, recovery_off_rem, need_night_next, nights)
- prev: 직전 근무 코드(전이금지 판정용)
- recovery_off_rem: 야간 블록 후 강제 OFF 잔여
- need_night_next: 1N 금지 시 방금 시작한 단독 N 이 다음날 N 을 요구하는지
- nights: 지금까지 N 수(월 상/하한 판정용, n_cap 초과분은 캡)
"""
from __future__ import annotations

from functools import lru_cache
from typing import Any, Dict, Optional, Set

_WORK = ("D", "E", "N")


def nurse_sequence_infeasible(
    *,
    num_days: int,
    allowed: Set[str],            # 허용 근무(work) 집합. 비면 D/E/N 전부. O 는 항상 가능.
    fixed: Dict[int, str],        # {day: shift} 고정셀 (0-based day)
    max_consecutive_work: Optional[int] = None,
    max_consecutive_nights: Optional[int] = None,
    ban_n_to_d: bool = False,
    ban_e_to_d: bool = False,
    ban_n_to_e: bool = False,
    two_offs_after_two_nig: bool = False,
    two_offs_after_three_nig: bool = False,
    not_one_night: bool = False,
    n_min: int = 0,
    n_max: Optional[int] = None,
) -> bool:
    """혼자서 모든 규칙을 지키는 완성이 **존재하지 않으면 True**(=infeasible), 있으면 False."""
    if num_days <= 0:
        return False
    work_allowed = set(allowed) if allowed else set(_WORK)
    rec_block = 2 if two_offs_after_two_nig else (3 if two_offs_after_three_nig else 0)
    K = int(max_consecutive_work) if max_consecutive_work and max_consecutive_work >= 1 else None
    L = int(max_consecutive_nights) if max_consecutive_nights and max_consecutive_nights >= 1 else None
    n_cap = int(n_max) if n_max is not None else num_days
    fixed = {int(k): str(v).upper() for k, v in (fixed or {}).items()}

    @lru_cache(maxsize=None)
    def go(day: int, prev: str, cw: int, cn: int, off_rem: int, need_n: bool, nights: int) -> bool:
        if day == num_days:
            return (not need_n) and (nights >= n_min) and (nights <= n_cap)
        # 후보 근무: 고정셀이면 그 코드만, 아니면 허용 work ∪ {O}
        cands = {fixed[day]} if day in fixed else (work_allowed | {"O"})
        for s in cands:
            if need_n and s != "N":
                continue                       # 1N: 시작한 단독 N 은 다음날 N 필수
            if off_rem > 0 and s != "O":
                continue                       # 회복: 강제 OFF 구간
            if s == "D" and ((prev == "N" and ban_n_to_d) or (prev == "E" and ban_e_to_d)):
                continue
            if s == "E" and prev == "N" and ban_n_to_e:
                continue
            is_work = s != "O"
            ncw = cw + 1 if is_work else 0
            if K is not None and is_work and ncw > K:
                continue                       # 연속근무 상한
            ncn = cn + 1 if s == "N" else 0
            if L is not None and s == "N" and ncn > L:
                continue                       # 연속야간 상한
            nnights = nights + (1 if s == "N" else 0)
            if nnights > n_cap:
                continue                       # 월 N 상한(가지치기)
            # 회복: block 연속 N 도달 → 다음 2일 강제 OFF
            n_off_rem = max(0, off_rem - 1)
            if rec_block and s == "N" and ncn == rec_block:
                n_off_rem = 2
                ncn = 0                        # 블록 종료(회복 후 새 블록)
            # 1N: 단독 N 시작(prev 가 N 아님) → 다음날 N 필요
            n_need = bool(not_one_night and s == "N" and prev != "N")
            if go(day + 1, s, ncw, ncn, n_off_rem, n_need, nnights):
                return True
        return False

    feasible = go(0, "O", 0, 0, 0, False, 0)
    go.cache_clear()
    return not feasible


def check_per_nurse_sequence(nurse: Dict[str, Any], cfg: Dict[str, Any], num_days: int) -> Optional[Dict[str, Any]]:
    """단일 간호사 dict + config → infeasible 이면 {reason_code, evidence}, 아니면 None.

    nurse: {nurse_id, allowed_shifts, fixed_shift_assignments{day:shift}, n_min, n_max}
    """
    from services.cp_sat.allowed_shift_types import normalize_allowed_shift_codes

    use_mid = bool(cfg.get("use_mid", False))
    allowed = normalize_allowed_shift_codes(nurse.get("allowed_shifts"), use_mid=use_mid) or set(_WORK)
    infeasible = nurse_sequence_infeasible(
        num_days=num_days,
        allowed={c for c in allowed if c in _WORK},
        fixed={int(k): v for k, v in (nurse.get("fixed_shift_assignments") or {}).items()},
        max_consecutive_work=cfg.get("max_conseq_work"),
        max_consecutive_nights=(3 if cfg.get("three_seq_nig") else 2),
        ban_n_to_d=bool(cfg.get("ban_n_to_d", True)),
        ban_e_to_d=bool(cfg.get("banned_day_after_eve", True)),
        ban_n_to_e=bool(cfg.get("ban_n_to_e", True)),
        two_offs_after_two_nig=bool(cfg.get("two_offs_after_two_nig")),
        two_offs_after_three_nig=bool(cfg.get("two_offs_after_three_nig")),
        not_one_night=bool(cfg.get("not_one_night")),
        n_min=int(nurse.get("n_min", 0) or 0),
        n_max=(int(nurse["n_max"]) if nurse.get("n_max") is not None else None),
    )
    if not infeasible:
        return None
    return {
        "reason_code": "PER_NURSE_SEQUENCE_INFEASIBLE",
        "evidence": {
            "nurse_id": str(nurse.get("nurse_id")),
            "n_min": int(nurse.get("n_min", 0) or 0),
            "n_max": nurse.get("n_max"),
            "fixed_cells": len(nurse.get("fixed_shift_assignments") or {}),
        },
    }
