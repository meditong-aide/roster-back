"""팀(Team) 기반 최소 시프트 커버리지 제약 모듈.

`team_min_by_team[team_id]` 맵에 따라 **팀마다 개별**로 일일 최소 시프트 커버리지를
보장한다. 예: 팀 "1" = {D≥1, E≥1, N≥0}, 팀 "2" = {D≥2, E≥1, N≥1}.
use_mid=True 일 때는 'M' 키도 사용 가능.

활성 조건:
    - grade_strategy ∈ {"TEAM", "COMBINED"}
    - cfg.team_min_by_team 이 비어있지 않고, 1개 이상 팀이 최소값을 가짐
    - 해당 팀에 배정된 간호사가 존재

소프트 폴백(team_min_soft_fallback=True, 기본):
    각 (팀, 일자, 시프트)에 슬랙을 두고 penalty_weight * slack 을 목적함수에서 차감.

하드(team_min_soft_fallback=False):
    `sum(X(n,d,s) for n in active team members) >= team_min[s]` 를 직접 강제.
"""

from __future__ import annotations

from services.roster_system import RosterSystem


def _clean_team_min(raw, shift_types: list[str], use_mid: bool) -> dict[str, int]:
    """한 팀의 min_shift dict 를 정제: 유효한 시프트 키 + 양수 값만 남긴다."""
    out: dict[str, int] = {}
    if not isinstance(raw, dict):
        return out
    for code, v in raw.items():
        try:
            iv = int(v or 0)
        except (TypeError, ValueError):
            continue
        if iv <= 0:
            continue
        if code == "O":
            continue
        if code == "M" and not use_mid:
            continue
        if code not in shift_types:
            continue
        out[code] = iv
    return out


def add_team_min_constraints(
    m,
    rs: RosterSystem,
    X,
    join,
    leave,
    *,
    grade_strategy: str = "BASE",
    blocked_by_nurse=None,
) -> list:
    obj_terms: list = []
    cfg = rs.config
    _impact_modes = getattr(rs, "_constraint_impact_constraint_modes", None)
    if _impact_modes is None:
        _impact_modes = []
        setattr(rs, "_constraint_impact_constraint_modes", _impact_modes)

    team_min_by_team = dict(getattr(cfg, "team_min_by_team", {}) or {})
    if not team_min_by_team:
        print("[TeamMin] skip: cfg.team_min_by_team is empty")
        return obj_terms

    use_mid = bool(getattr(cfg, "use_mid", False))
    allow_soft = bool(getattr(cfg, "team_min_soft_fallback", False))
    base_penalty_weight = int(getattr(cfg, "team_min_penalty_weight", 80000) or 0)
    # grade_strategy=TEAM 이면 team_min weight 가중치를 끌어올림(선호 신호).
    gs = str(grade_strategy or "").upper()
    penalty_weight = base_penalty_weight * 4 if gs == "TEAM" else base_penalty_weight

    # 팀별 멤버 인덱스 집합
    team_members: dict[str, list[int]] = {}
    for idx, nurse in enumerate(rs.nurses):
        tid = getattr(nurse, "team_id", None)
        if tid in (None, "", 0):
            continue
        team_members.setdefault(str(tid), []).append(idx)
    if not team_members:
        print("[TeamMin] skip: no nurses have team_id")
        return obj_terms

    shift_types = list(rs.config.shift_types)
    added_cnt = 0
    # soft 슬랙 변수 수집 — lex 폴백(fallback_lex)이 Stage 목적함수에 직접 주입할 수 있도록
    # rs 에 노출한다. (add_team_min_constraints 반환 obj_terms 는 Maximize 용 -w*slack 인데,
    # fallback_lex Stage1/2 는 Minimize 라 반환값이 버려져 왔음 → 슬랙 자체를 넘겨 재가중.)
    cover_slacks: list = []

    # per-day 요구 인원(need=자리 수) 조회 — daily_shift_requirements_by_day 우선, 없으면 기본.
    by_day = getattr(cfg, "daily_shift_requirements_by_day", None)
    base_need = getattr(cfg, "daily_shift_requirements", {}) or {}

    def _need_for(d: int, code: str) -> int:
        if isinstance(by_day, list) and 0 <= d < len(by_day) and by_day[d]:
            return int((by_day[d] or {}).get(code, 0) or 0)
        return int(base_need.get(code, 0) or 0)

    # 팀별 min(코드→명수) 정제 + team_min 이 걸리는 시프트 코드 집합
    team_min_clean: dict[str, dict[str, int]] = {}
    codes_with_min: set[str] = set()
    for tid, members in team_members.items():
        if not members:
            continue
        tm = _clean_team_min(team_min_by_team.get(tid), shift_types, use_mid)
        if tm:
            team_min_clean[tid] = tm
            codes_with_min.update(tm.keys())

    # ── 핵심 규칙: (일, 시프트)마다 "서로 다른 팀"을 target=min(need, 팀수)개 커버 ──
    #   - need(자리 수) >= 팀수 : 모든 팀 커버 요구(평일 D=3, 3팀 → 3팀 전부 1명씩).
    #   - need < 팀수          : need 개 팀만 커버해도 충분(주말 D=2, 3팀 → 2팀). 남는 1팀 공백은 정상.
    #   즉 "3자리에 3팀 못 넣음"만 위반이고, "2자리에 2팀"은 위반이 아니다.
    #   미지정(team_id 없는) 인원은 팀 커버 카운트에서 빠지므로 자연히 잔여 자리(N/OFF)로 밀린다.
    #   present_t=1 ⟺ 팀 t 가 이 시프트에 min_t 명 이상 배치(자기 팀원으로). covered=Σ present_t.
    for d in range(rs.num_days):
        for code in codes_with_min:
            s_idx = shift_types.index(code)
            present_vars = []
            for tid, tm in team_min_clean.items():
                min_t = int(tm.get(code, 0) or 0)
                if min_t <= 0:
                    continue
                active = [n for n in team_members[tid] if join[n] <= d <= leave[n]]
                # capacity 가드: 활성 멤버가 min_t 보다 적으면 이 팀은 그날 자기 몫을
                # 채울 수 없으므로 target 카운트에서 제외(hard 모드 헛 infeasible 방지).
                # min_t=1 이면 `len(active) < 1` == `not active` 로 기존 동작과 동일.
                if len(active) < min_t:
                    continue
                member_sum = sum(X(n, d, s_idx) for n in active)
                present = m.NewBoolVar(f"tmin_present_t{tid}_d{d}_s{s_idx}")
                # present=1 → 이 팀이 이 시프트에 min_t 명 이상 배치. present=0 → 제약 없음.
                m.Add(member_sum >= min_t * present)
                present_vars.append(present)
            num_teams = len(present_vars)
            if num_teams == 0:
                continue
            need = _need_for(d, code)
            target = min(need, num_teams)
            if target <= 0:
                continue
            covered = sum(present_vars)
            if allow_soft:
                slack = m.NewIntVar(0, target, f"tmin_cover_slack_d{d}_s{s_idx}")
                m.Add(covered + slack >= target)
                cover_slacks.append(slack)
                if penalty_weight > 0:
                    obj_terms.append(-penalty_weight * slack)
                eff_mode = "soft_fallback"
            else:
                m.Add(covered >= target)
                eff_mode = "enforced"
            added_cnt += 1
            _impact_modes.append({
                "family": "team_min",
                "key": f"team_min:cover:{d}:{code}",
                "configured_mode": "soft" if allow_soft else "hard",
                "effective_mode": eff_mode,
                "source_file": "app/services/constraints/team_constraints.py",
                "reason": "distinct-team coverage target = min(need, num_teams)",
                "evidence": {"day": d + 1, "shift": code, "need": need, "num_teams": num_teams, "target": target},
            })

    # lex 폴백이 읽을 수 있도록 이번 호출(=이번 stage 모델)의 soft 슬랙을 노출.
    setattr(rs, "_team_min_cover_slacks", cover_slacks)

    mode = "soft" if allow_soft else "hard"
    print(f"[TeamMin] mode={mode} teams={len(team_min_clean)} added={added_cnt} rule=min(need,num_teams)")

    return obj_terms
