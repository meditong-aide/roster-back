"""팀(Team) 기반 목적함수 항 생성 모듈.

이 모듈은 `cp_sat_basic.py` 내부에 있던 팀 정렬(같은 팀이면 같은 교대가 되도록 유도)
목적함수 항을 분리하여, CP-SAT 전체 알고리즘을 건드리지 않고도
팀 로직만 독립적으로 관리/확장할 수 있게 합니다.
"""

from __future__ import annotations

from services.roster_system import RosterSystem
from services.day_windows import iter_nurse_days


def add_team_balance_objective_terms(m, rs: RosterSystem, X, join, leave, blocked_by_nurse=None) -> list:
    """팀별 동일 교대 정렬을 유도하는 목적함수 항을 생성한다.

    개요:
        - 팀별로 날짜(d)마다 '대표 교대' Y(team,d,shift)를 1개 선택한다(ExactlyOne).
        - 팀원들은 그 대표 교대에 최대한 정렬되도록 Z = AND(X, Y) 보너스를 준다.
        - 동시에 대표 교대의 월간 분포가 전체 요구량 비율을 따르도록 편차 패널티를 준다.

    Args:
        m: OR-Tools CpModel 객체
        rs: RosterSystem
        X: 배정 변수 접근자 X(n,d,s) → BoolVar 또는 0
        join: 간호사별 근무 시작 가능 인덱스(0-based day_idx)
        leave: 간호사별 근무 종료 가능 인덱스(0-based day_idx)

    Returns:
        목적함수에 더할 선형식 항 리스트
    """
    obj_terms: list = []
    cfg = rs.config

    if not getattr(cfg, "team_balance_enable", False):
        return obj_terms
    weight = int(getattr(cfg, "team_balance_weight", 0) or 0)
    if weight <= 0:
        return obj_terms

    # 사용할 교대 집합(OFF 제외)
    focus_codes = getattr(cfg, "team_balance_focus_shifts", None)
    if focus_codes:
        shift_codes = [c for c in focus_codes if c in rs.config.daily_shift_requirements.keys()]
    else:
        shift_codes = list(rs.config.daily_shift_requirements.keys())
    shift_codes = [c for c in shift_codes if c in rs.config.shift_types and c != "O"]
    if not shift_codes:
        return obj_terms
    shift_indices = [rs.config.shift_types.index(c) for c in shift_codes]

    # 모드 기반 교대 가중치 (없으면 balanced)
    shift_weights = getattr(cfg, "team_balance_shift_weights", {}) or {"D": 1.0, "E": 1.0, "N": 1.0}

    # 팀 구성: Nurse.team_id 기반
    team_members: dict[str, list[int]] = {}
    for idx, nurse in enumerate(rs.nurses):
        team_id = getattr(nurse, "team_id", None)
        if team_id in (None, "", 0):
            continue
        team_members.setdefault(str(team_id), []).append(idx)
    if not team_members:
        return obj_terms

    # 전체 요구량 비율 계산(월간)
    total_required = {code: 0 for code in shift_codes}
    ds_by_day = getattr(cfg, "daily_shift_requirements_by_day", None)
    if isinstance(ds_by_day, list) and ds_by_day:
        for dm in ds_by_day:
            for code in shift_codes:
                total_required[code] += int(dm.get(code, cfg.daily_shift_requirements.get(code, 0)))
    else:
        for code in shift_codes:
            total_required[code] = int(cfg.daily_shift_requirements.get(code, 0)) * rs.num_days
    sum_required = max(1, sum(total_required.values()))
    ratio = {code: (total_required[code] / sum_required) for code in shift_codes}

    # 가중치 분리: 정렬 보너스(같은 shift)와 분포 패널티(비율)
    # align_w = int(weight)
    # dist_w = max(1, int(weight * 0.6))

    if getattr(rs, "is_quick_phase", False):
        align_w = int(weight * 2.5)
        dist_w  = 0
    else:
        align_w = int(weight)
        dist_w = max(1, int(weight * 0.6))

    for team_id, members in team_members.items():
        if not members:
            continue

        # Y 생성 및 일자별 ExactlyOne
        Y: dict[tuple[int, int], object] = {}
        for d in range(rs.num_days):
            ys = []
            for s in shift_indices:
                y = m.NewBoolVar(f"y_{team_id}_{d}_{s}")
                Y[(d, s)] = y
                ys.append(y)
            m.AddExactlyOne(ys)

        # (D) 대표 교대 day-to-day diff 페널티 — team_min hard 위주 정책에서는 의미 약하므로 비활성.
        #     향후 필요 시 ENABLE_TEAM_BALANCE_DIFF=True 로 다시 켜면 된다.
        ENABLE_TEAM_BALANCE_DIFF = False
        if ENABLE_TEAM_BALANCE_DIFF:
            for d in range(rs.num_days - 1):
                for s in shift_indices:
                    diff = m.NewBoolVar(f"y_diff_{team_id}_{d}_{s}")
                    m.Add(diff >= Y[(d, s)] - Y[(d+1, s)])
                    m.Add(diff >= Y[(d+1, s)] - Y[(d, s)])
                    obj_terms.append(-int(align_w * 0.3) * diff)

        # (C) 분포 패널티 — 팀별 D/E/N 비율을 ward 평균에 맞추는 항. team_min 과 충돌 가능하므로 비활성.
        ENABLE_TEAM_BALANCE_DIST = False
        if ENABLE_TEAM_BALANCE_DIST:
            for code in shift_codes:
                s_idx = rs.config.shift_types.index(code)
                cnt = m.NewIntVar(0, rs.num_days, f"yCnt_{team_id}_{code}")
                m.Add(cnt == sum(Y[(d, s_idx)] for d in range(rs.num_days)))
                target = int(round(rs.num_days * ratio.get(code, 0.0)))
                dev_pos = m.NewIntVar(0, rs.num_days, f"yDevP_{team_id}_{code}")
                dev_neg = m.NewIntVar(0, rs.num_days, f"yDevN_{team_id}_{code}")
                m.Add(dev_pos - dev_neg == cnt - target)
                w_code = float(shift_weights.get(code, 1.0))
                obj_terms.append(-int(dist_w * w_code) * dev_pos)
                obj_terms.append(-int(dist_w * w_code) * dev_neg)

        # 정렬 보너스: 팀원들이 대표 교대 Y를 따라가도록 Z=AND(X,Y) 보너스
        # Y는 D_phys(=rs.num_days)만큼만 생성되므로 lookahead 일자는 스킵
        for n in members:
            for d in iter_nurse_days(n, join, leave, blocked_by_nurse):
                if d >= rs.num_days:
                    continue
                for code in shift_codes:
                    s = rs.config.shift_types.index(code)
                    y = Y[(d, s)]
                    z = m.NewBoolVar(f"tz_{team_id}_{n}_{d}_{s}")
                    m.Add(z <= X(n, d, s))
                    m.Add(z <= y)
                    m.Add(z >= X(n, d, s) + y - 1)
                    w_code = float(shift_weights.get(code, 1.0))
                    obj_terms.append(int(align_w * w_code) * z)
        # (B) 팀원 간 직접 정렬 z2 — 위 Z 보너스와 중복 인센티브이고 가장 큰 변수/제약 폭증의 원인이라 제거.
        # team_min hard 가 충분히 팀 커버리지를 보장하므로 z2 는 불필요.

    return obj_terms


