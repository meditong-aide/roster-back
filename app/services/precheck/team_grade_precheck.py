"""Team × Grade × Common Pool — Infeasibility Precheck.

docs/TEAM_GRADE_INFEASIBILITY_PRECHECK.md 의 18개 reason_code 를 deterministic
검사로 구현. 프론트/백엔드 양측에서 공용으로 호출한다.

모든 검사는 랜덤성/솔버 호출 없이, 주어진 입력만으로 수학적으로 확정적인
infeasibility 를 감지한다. 결과는 `{reason_code, severity, evidence}` 딕셔너리
리스트로 반환된다.

검사 순서는 docs §3 권장을 따른다:
    1. D-1, D-2           (설정 정합성)
    2. E-2                (개인 allowed_shifts 공집합)
    3. B-2                (팀 크기 부족)
    4. C-1                (grade min 합계)
    5. F-1, F-2           (고정 배정 정합성)
    6. A-1, A-2, A-3      (전역 커버리지/공급 부족)
    7. B-1, B-3, B-4      (팀 일자별)
    8. C-2, C-3, C-4      (grade 일자별)
    9. F-3                (팀 × 고정 배정 교차)
    10. E-1               (공통 풀 N 월간 용량)

설정성 오류(D, E-2, F-1/F-2)는 즉시 중단 옵션 제공(stop_on_config_error=True).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Tuple, Any

from services.semantics import attach_reason_code_ontology


# ---------------------------------------------------------------------------
# Input dataclasses
# ---------------------------------------------------------------------------

@dataclass
class PrecheckNurse:
    nurse_id: str
    grade: Optional[int] = None
    team_id: Optional[Any] = None  # None → common pool
    allowed_shifts: Optional[List[str]] = None  # None/빈값 → S 전체
    join_day: int = 0  # 0-based day index
    leave_day: int = 0  # inclusive
    personal_off_adjustment: int = 0
    fixed_off_days: Set[int] = field(default_factory=set)
    fixed_shift_assignments: Dict[int, str] = field(default_factory=dict)  # {day: shift}
    # 프리셉티 — preceptor 와의 동기 페어링.
    # None 이면 일반 nurse, 값이 있으면 본인이 preceptee 이고 가리키는 사람이 preceptor.
    preceptor_id: Optional[str] = None
    # 동기화 기간 (0-based day index). None → [join_day, leave_day] 전체로 간주.
    sync_window_start: Optional[int] = None
    sync_window_end: Optional[int] = None
    # per-nurse 야간 상한 (n_exact 우선, 없으면 n_max). None → 전역 상한만 적용.
    night_cap: Optional[int] = None


@dataclass
class PrecheckInput:
    num_days: int
    nurses: List[PrecheckNurse]
    teams: List[Any]  # team_id 리스트 또는 {team_id,members} — team_coverage 키로 추론도 가능
    roster_config: Dict[str, Any]
    team_coverage: Dict[Any, Dict[str, int]]  # {team_id: {shift: min}}
    grade_constraints: Dict[str, Any]  # {minimum_by_shift, max_by_shift}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _apply_shifts(use_mid: bool) -> List[str]:
    return ["D", "E", "N", "M"] if use_mid else ["D", "E", "N"]


def _need(cfg: Dict[str, Any], shift: str, day: int) -> int:
    by_day = cfg.get("daily_shift_requirements_by_day")
    if isinstance(by_day, list) and 0 <= day < len(by_day) and by_day[day]:
        v = by_day[day].get(shift, 0)
    else:
        v = cfg.get("daily_shift_requirements", {}).get(shift, 0)
    try:
        return int(v or 0)
    except (TypeError, ValueError):
        return 0


def _allowed_set(nurse: PrecheckNurse, S: List[str]) -> Set[str]:
    raw = nurse.allowed_shifts
    if raw is None:
        # 미지정 → universe 전체 가능 (기본값).
        return set(S)
    if len(raw) == 0:
        # 명시적 빈 list = 명시적 lockout (∅). ALLOWED_SHIFTS_ISOLATES_NURSE 발급 가능.
        return set()
    return {str(x) for x in raw if x in S}


def _active(nurse: PrecheckNurse, d: int) -> bool:
    if not (nurse.join_day <= d <= nurse.leave_day):
        return False
    if d in nurse.fixed_off_days:
        return False
    return True


def _required_off_days(nurse: PrecheckNurse, cfg: Dict[str, Any]) -> int:
    return (
        int(cfg.get("global_monthly_off_days", 0) or 0)
        + int(cfg.get("standard_personal_off_days", 0) or 0)
        + int(nurse.personal_off_adjustment or 0)
    )


def _working_capacity(nurse: PrecheckNurse, cfg: Dict[str, Any]) -> int:
    span = max(0, nurse.leave_day - nurse.join_day + 1)
    cap = max(0, span - _required_off_days(nurse, cfg))
    # 연속근무 상한(max_consecutive_work=C)은 실제 근무가능일을 추가로 조인다:
    # C일 근무 후 최소 1일 휴식 → span 내 최대 근무일 = span - span//(C+1) (상한).
    # cap 은 항상 상한이어야 하므로(하한이면 false positive) min 으로 결합한다.
    mcw = cfg.get("max_consecutive_work")
    if mcw is not None:
        try:
            c = int(mcw)
            if c >= 1:
                cap = min(cap, max(0, span - span // (c + 1)))
        except (TypeError, ValueError):
            pass
    return cap


def _issue(code: str, evidence: Dict[str, Any], severity: str = "hard") -> Dict[str, Any]:
    return attach_reason_code_ontology(
        reason_code=code,
        evidence=evidence,
        severity=severity,
    )


# ---------------------------------------------------------------------------
# Individual checks
# ---------------------------------------------------------------------------

def check_mid_required_missing(inp: PrecheckInput) -> List[Dict]:
    cfg = inp.roster_config
    if not bool(cfg.get("use_mid", False)):
        return []
    dsr = cfg.get("daily_shift_requirements") or {}
    base_m = int(dsr.get("M", 0) or 0)
    # base(shift_manages 파생)에 M 이 없어도, per-day 요구치(DailyShift)에 M>0 인 날이
    # 하나라도 있으면 MID 가 실제로 요구된 것 → 통과. (형제 디텍터들과 동일하게 per-day 참조)
    by_day = cfg.get("daily_shift_requirements_by_day") or []
    per_day_m = any(int((d or {}).get("M", 0) or 0) > 0 for d in by_day if isinstance(d, dict))
    if base_m <= 0 and not per_day_m:
        # use_mid=True 인데 M 수요가 전무 = 모순 설정이지만, 차단(hard) 대신 경고(warning)로
        # 흡수한다. M coverage=0 이라 솔버는 M 없이 정상 생성 가능 → 유저가 use_mid 끄는 걸
        # 깜빡해도 생성이 막히지 않는다. (의도: 오설정을 막지 말고 안내만)
        return [_issue("MID_REQUIRED_MISSING", {"daily_shift_requirements": dict(dsr)}, severity="warning")]
    return []


def check_mid_disabled_but_used(inp: PrecheckInput) -> List[Dict]:
    if bool(inp.roster_config.get("use_mid", False)):
        return []
    offending: List[str] = []
    for tid, tmin in (inp.team_coverage or {}).items():
        if "M" in (tmin or {}):
            offending.append(f"team[{tid}].M")
    gc = inp.grade_constraints or {}
    for key in ("minimum_by_shift", "max_by_shift"):
        if "M" in (gc.get(key) or {}):
            offending.append(f"grade_constraints.{key}.M")
    if offending:
        return [_issue("MID_DISABLED_BUT_USED", {"offending_keys": offending})]
    return []


def check_allowed_shifts_isolates_nurse(inp: PrecheckInput) -> List[Dict]:
    S = _apply_shifts(bool(inp.roster_config.get("use_mid", False)))
    issues: List[Dict] = []
    for n in inp.nurses:
        allowed = _allowed_set(n, S)
        if allowed:
            continue  # 공집합이 아니면 스킵
        span = max(0, n.leave_day - n.join_day + 1)
        req_off = _required_off_days(n, inp.roster_config)
        if span > req_off:
            issues.append(
                _issue(
                    "ALLOWED_SHIFTS_ISOLATES_NURSE",
                    {
                        "nurse_id": n.nurse_id,
                        "allowed": list(n.allowed_shifts or []),
                        "active_days": span,
                        "required_off_days": req_off,
                    },
                )
            )
    return issues


def check_team_size_insufficient(inp: PrecheckInput) -> List[Dict]:
    S = _apply_shifts(bool(inp.roster_config.get("use_mid", False)))
    issues: List[Dict] = []
    members_by_team: Dict[Any, List[PrecheckNurse]] = {}
    for n in inp.nurses:
        if n.team_id in (None, "", 0):
            continue
        members_by_team.setdefault(n.team_id, []).append(n)
    for tid, tmin in (inp.team_coverage or {}).items():
        if not tmin:
            continue
        need_sum = sum(int(tmin.get(s, 0) or 0) for s in S)
        size = len(members_by_team.get(tid, []))
        if size < need_sum:
            # team_min 은 soft(min(need,팀수)+slack, auto-soft 포함)라 팀 인원 부족은
            # 최종 infeasibility 가 아니라 벌점으로 흡수된다 → 선차단하지 않고 warning.
            issues.append(
                _issue(
                    "TEAM_SIZE_INSUFFICIENT",
                    {"team_id": tid, "team_size": size, "team_min_sum": need_sum},
                    severity="warning",
                )
            )
    return issues


def check_grade_min_sum_exceeds_need(inp: PrecheckInput) -> List[Dict]:
    S = _apply_shifts(bool(inp.roster_config.get("use_mid", False)))
    cfg = inp.roster_config
    gc_min = (inp.grade_constraints or {}).get("minimum_by_shift") or {}
    issues: List[Dict] = []
    for s in S:
        per_grade = gc_min.get(s) or {}
        if not per_grade:
            continue
        min_sum = sum(int(v or 0) for v in per_grade.values())
        if min_sum <= 0:
            continue
        for d in range(inp.num_days):
            nd = _need(cfg, s, d)
            if min_sum > nd:
                issues.append(
                    _issue(
                        "GRADE_MIN_SUM_EXCEEDS_NEED",
                        {"shift": s, "day": d, "min_sum": min_sum, "need": nd},
                    )
                )
                break  # 같은 (s) 반복 정보는 day 한 건이면 충분
    return issues


def check_grade_min_exceeds_max(inp: PrecheckInput) -> List[Dict]:
    """Grade min/max 산술 모순(min > max) 즉시 탐지.

    같은 shift, 같은 grade에서 minimum_by_shift 값이 max_by_shift 값을
    초과하면 솔버 실행 전 설정 모순으로 간주한다.
    """
    gc = inp.grade_constraints or {}
    gc_min = gc.get("minimum_by_shift") or {}
    gc_max = gc.get("max_by_shift") or {}
    issues: List[Dict] = []

    for shift, per_grade_min in gc_min.items():
        if not isinstance(per_grade_min, dict):
            continue
        per_grade_max = gc_max.get(shift) or {}
        if not isinstance(per_grade_max, dict):
            continue
        for g_raw, mn_raw in per_grade_min.items():
            if g_raw not in per_grade_max:
                continue
            try:
                mn = int(mn_raw or 0)
                mx = int(per_grade_max.get(g_raw) or 0)
            except (TypeError, ValueError):
                continue
            if mn > mx:
                issues.append(
                    _issue(
                        "GRADE_MIN_EXCEEDS_MAX",
                        {
                            "shift": shift,
                            "grade": int(g_raw),
                            "min": mn,
                            "max": mx,
                        },
                    )
                )
    return issues


def check_fixed_assign_exceeds_need(inp: PrecheckInput) -> List[Dict]:
    S = set(_apply_shifts(bool(inp.roster_config.get("use_mid", False))))
    counts: Dict[Tuple[str, int], int] = {}
    for n in inp.nurses:
        for d, sh in (n.fixed_shift_assignments or {}).items():
            if sh in S:
                counts[(sh, d)] = counts.get((sh, d), 0) + 1
    issues: List[Dict] = []
    for (sh, d), c in counts.items():
        nd = _need(inp.roster_config, sh, d)
        if c > nd:
            issues.append(
                _issue(
                    "FIXED_ASSIGN_EXCEEDS_NEED",
                    {"shift": sh, "day": d, "fixed_count": c, "need": nd},
                )
            )
    return issues


def check_fixed_assign_violates_allowed(inp: PrecheckInput) -> List[Dict]:
    S = _apply_shifts(bool(inp.roster_config.get("use_mid", False)))
    issues: List[Dict] = []
    for n in inp.nurses:
        allowed = _allowed_set(n, S) | {"O"}
        for d, sh in (n.fixed_shift_assignments or {}).items():
            if sh not in allowed:
                issues.append(
                    _issue(
                        "FIXED_ASSIGN_VIOLATES_ALLOWED",
                        {
                            "nurse_id": n.nurse_id,
                            "day": d,
                            "assigned_shift": sh,
                            "allowed": sorted(allowed),
                        },
                    )
                )
    return issues


def check_global_day_capacity_shortage(inp: PrecheckInput) -> List[Dict]:
    S = _apply_shifts(bool(inp.roster_config.get("use_mid", False)))
    issues: List[Dict] = []
    for d in range(inp.num_days):
        required_total = sum(_need(inp.roster_config, s, d) for s in S)
        avail = sum(1 for n in inp.nurses if _active(n, d))
        if required_total > avail:
            issues.append(
                _issue(
                    "GLOBAL_DAY_CAPACITY_SHORTAGE",
                    {"day": d, "required_total": required_total, "available_nurses": avail},
                )
            )
    return issues


def check_global_shift_allowed_shortage(inp: PrecheckInput) -> List[Dict]:
    S = _apply_shifts(bool(inp.roster_config.get("use_mid", False)))
    issues: List[Dict] = []
    for d in range(inp.num_days):
        for s in S:
            nd = _need(inp.roster_config, s, d)
            if nd <= 0:
                continue
            eligible_ids: List[str] = []
            for n in inp.nurses:
                if not _active(n, d):
                    continue
                if s in _allowed_set(n, S):
                    eligible_ids.append(n.nurse_id)
            avail = len(eligible_ids)
            if nd <= avail:
                continue

            # 인접 shift 자격자 풀 — 'D 부족할 때 N 가능한 사람 몇명?' 같은 컨텍스트
            cross_pool: Dict[str, List[str]] = {}
            for other in S:
                if other == s:
                    continue
                cross = [
                    n.nurse_id for n in inp.nurses
                    if _active(n, d) and (other in _allowed_set(n, S)) and (n.nurse_id not in eligible_ids)
                ]
                if cross:
                    cross_pool[other] = cross

            issues.append(
                _issue(
                    "GLOBAL_SHIFT_ALLOWED_SHORTAGE",
                    {
                        # ontology template keys (day 는 1-based 로 노출)
                        "shift": s,
                        "day": d + 1,
                        "required": nd,
                        "eligible": avail,
                        "shortage": nd - avail,
                        # legacy
                        "allowed_nurses": avail,
                        # 풍부 디테일
                        "eligible_nurses": eligible_ids,
                        "cross_shift_eligible_pool": {k: v for k, v in cross_pool.items()},
                        "cross_shift_eligible_counts": {k: len(v) for k, v in cross_pool.items()},
                    },
                )
            )
    return issues


def check_capacity_total_shortage(inp: PrecheckInput) -> List[Dict]:
    S = _apply_shifts(bool(inp.roster_config.get("use_mid", False)))
    # 일별 demand 분포 — bottleneck 식별용
    daily_demand = [
        {"day": d + 1, "by_shift": {s: _need(inp.roster_config, s, d) for s in S}}
        for d in range(inp.num_days)
    ]
    for dd in daily_demand:
        dd["demand"] = sum(dd["by_shift"].values())

    total_need = sum(dd["demand"] for dd in daily_demand)
    if total_need <= 0:
        return []

    per_nurse_caps = [
        {"nurse_id": n.nurse_id, "capacity_days": _working_capacity(n, inp.roster_config),
         "personal_off_adjustment": n.personal_off_adjustment}
        for n in inp.nurses
    ]
    total_cap = sum(c["capacity_days"] for c in per_nurse_caps)

    if total_need <= total_cap:
        return []

    shortage = total_need - total_cap
    avg_daily = total_need / inp.num_days if inp.num_days else 0.0

    # 평균 초과 day → bottleneck (동적 임계값)
    bottleneck_days = sorted(
        [dd for dd in daily_demand if dd["demand"] > avg_daily],
        key=lambda x: x["demand"], reverse=True,
    )

    # shift 별 share
    by_shift_total: Dict[str, int] = {s: 0 for s in S}
    for dd in daily_demand:
        for s, v in dd["by_shift"].items():
            by_shift_total[s] = by_shift_total.get(s, 0) + v

    # 가장 capacity 낮은 nurse 상위 — shortage 와 동일 수 (단 nurse_count 이하)
    per_nurse_caps.sort(key=lambda x: x["capacity_days"])
    top_n = min(shortage, len(per_nurse_caps))
    lowest_capacity_nurses = per_nurse_caps[:top_n]

    return [
        _issue(
            "CAPACITY_TOTAL_SHORTAGE",
            {
                # ontology template keys
                "required": total_need,
                "capacity": total_cap,
                "shortage": shortage,
                # legacy keys (1 릴리즈 유지)
                "required_total": total_need,
                "capacity_total": total_cap,
                "nurse_count": len(inp.nurses),
                "num_days": inp.num_days,
                # 풍부 디테일 — narrative 가 problem_list/action_lever 구성에 사용
                "avg_daily_demand": round(avg_daily, 2),
                "demand_by_shift": by_shift_total,
                "bottleneck_days": bottleneck_days,
                "lowest_capacity_nurses": lowest_capacity_nurses,
                "demand_uniform": len(bottleneck_days) == 0,
            },
        )
    ]


def check_team_min_exceeds_global_need(inp: PrecheckInput) -> List[Dict]:
    # 규칙: (일, 시프트)마다 요구 인원(need=자리 수)만큼 '서로 다른 팀'을 커버한다
    #   → target = min(need, 팀수). "2자리·3팀 → 2팀만" 은 정상이므로 팀 min '합계'가
    #   need 를 넘는 것은 더 이상 검출하지 않는다(주말 등에서 거짓 경고 방지).
    #
    # team_min 은 이제 완전 soft(team_constraints: covered+slack>=target, 미달=벌점)라
    #   어떤 경우도 hard infeasibility 를 만들지 않는다 → 절대 선차단하지 않는다.
    #   '한 팀의 min 하나가 자리 수(need)보다 큰' 경우만(그 팀은 자기 몫을 다 못 채움)
    #   정보성 warning 으로 알린다. (dev d511f62 의 'warning 강등' 의도 + 정확한 조건 결합.)
    S = _apply_shifts(bool(inp.roster_config.get("use_mid", False)))
    issues: List[Dict] = []
    for s in S:
        per_team = [int((tm or {}).get(s, 0) or 0) for tm in (inp.team_coverage or {}).values()]
        max_team_min = max(per_team) if per_team else 0
        if max_team_min <= 0:
            continue
        for d in range(inp.num_days):
            nd = _need(inp.roster_config, s, d)
            if max_team_min > nd:
                issues.append(
                    _issue(
                        "TEAM_MIN_EXCEEDS_GLOBAL_NEED",
                        {"shift": s, "day": d, "single_team_min": max_team_min, "global_need": nd},
                        severity="warning",
                    )
                )
                break
    return issues


def check_team_active_members_insufficient(inp: PrecheckInput) -> List[Dict]:
    S = _apply_shifts(bool(inp.roster_config.get("use_mid", False)))
    issues: List[Dict] = []
    members_by_team: Dict[Any, List[PrecheckNurse]] = {}
    for n in inp.nurses:
        if n.team_id in (None, "", 0):
            continue
        members_by_team.setdefault(n.team_id, []).append(n)
    for tid, tmin in (inp.team_coverage or {}).items():
        if not tmin:
            continue
        need_sum = sum(int(tmin.get(s, 0) or 0) for s in S)
        if need_sum <= 0:
            continue
        mems = members_by_team.get(tid, [])
        for d in range(inp.num_days):
            active_cnt = sum(1 for n in mems if _active(n, d))
            if active_cnt < need_sum:
                # team_min soft → 팀 미달은 벌점으로 흡수. 선차단하지 않고 warning.
                issues.append(
                    _issue(
                        "TEAM_ACTIVE_MEMBERS_INSUFFICIENT",
                        {
                            "team_id": tid,
                            "day": d,
                            "active_count": active_cnt,
                            "required_min_sum": need_sum,
                        },
                        severity="warning",
                    )
                )
    return issues


def check_team_shift_allowed_shortage(inp: PrecheckInput) -> List[Dict]:
    S = _apply_shifts(bool(inp.roster_config.get("use_mid", False)))
    issues: List[Dict] = []
    members_by_team: Dict[Any, List[PrecheckNurse]] = {}
    for n in inp.nurses:
        if n.team_id in (None, "", 0):
            continue
        members_by_team.setdefault(n.team_id, []).append(n)
    for tid, tmin in (inp.team_coverage or {}).items():
        if not tmin:
            continue
        mems = members_by_team.get(tid, [])
        for s in S:
            req = int(tmin.get(s, 0) or 0)
            if req <= 0:
                continue
            for d in range(inp.num_days):
                count = 0
                for n in mems:
                    if _active(n, d) and s in _allowed_set(n, S):
                        count += 1
                if req > count:
                    # team_min soft → 팀의 해당 시프트 가용 부족도 벌점 흡수. warning.
                    issues.append(
                        _issue(
                            "TEAM_SHIFT_ALLOWED_SHORTAGE",
                            {
                                "team_id": tid,
                                "shift": s,
                                "day": d,
                                "required": req,
                                "allowed_count": count,
                            },
                            severity="warning",
                        )
                    )
    return issues


def check_grade_max_sum_below_need(inp: PrecheckInput) -> List[Dict]:
    S = _apply_shifts(bool(inp.roster_config.get("use_mid", False)))
    gc_max = (inp.grade_constraints or {}).get("max_by_shift") or {}
    issues: List[Dict] = []
    for s in S:
        per_grade_max = gc_max.get(s) or {}
        if not per_grade_max:
            continue
        capped_grades = {int(g): int(v or 0) for g, v in per_grade_max.items() if v is not None}
        if not capped_grades:
            continue
        capped_sum = sum(capped_grades.values())
        for d in range(inp.num_days):
            nd = _need(inp.roster_config, s, d)
            if nd <= 0:
                continue
            free_cap = 0
            for n in inp.nurses:
                if not _active(n, d):
                    continue
                if s not in _allowed_set(n, S):
                    continue
                g = n.grade
                if g is None or int(g) not in capped_grades:
                    free_cap += 1
            if capped_sum + free_cap < nd:
                issues.append(
                    _issue(
                        "GRADE_MAX_SUM_BELOW_NEED",
                        {
                            "shift": s,
                            "day": d,
                            "capped_sum": capped_sum,
                            "free_capacity": free_cap,
                            "need": nd,
                        },
                    )
                )
    return issues


def check_grade_min_available_shortage(inp: PrecheckInput) -> List[Dict]:
    S = _apply_shifts(bool(inp.roster_config.get("use_mid", False)))
    gc_min = (inp.grade_constraints or {}).get("minimum_by_shift") or {}
    issues: List[Dict] = []
    # 그룹에 해당 grade 간호사가 0명이면 GRADE_DEFAULT_111(강제 grade-1 floor)는
    # 솔버 _add_minimum_constraints cascade 가 soft/하위 등급으로 흘려보내는 것이 정책 의도이므로
    # (roster_create_service._ensure_grade1_default 주석 참조) precheck 에서 하드 차단하지 않는다.
    # 차단하면 grade 미등록 병동(해당 grade 0명)은 생성 자체가 불가해진다.
    grades_present = {int(n.grade) for n in inp.nurses if n.grade is not None}
    for s in S:
        per_grade = gc_min.get(s) or {}
        for g_raw, req_raw in per_grade.items():
            try:
                g = int(g_raw)
                req = int(req_raw or 0)
            except (TypeError, ValueError):
                continue
            if req <= 0:
                continue
            if g not in grades_present:
                continue
            for d in range(inp.num_days):
                avail = 0
                for n in inp.nurses:
                    if n.grade is None or int(n.grade) != g:
                        continue
                    if not _active(n, d):
                        continue
                    if s in _allowed_set(n, S):
                        avail += 1
                if req > avail:
                    issues.append(
                        _issue(
                            "GRADE_MIN_AVAILABLE_SHORTAGE",
                            {
                                "shift": s,
                                "day": d,
                                "grade": g,
                                "required": req,
                                "available": avail,
                            },
                        )
                    )
    return issues


def check_grade_antipair_forces_shortage(inp: PrecheckInput) -> List[Dict]:
    S = _apply_shifts(bool(inp.roster_config.get("use_mid", False)))
    gc_max = (inp.grade_constraints or {}).get("max_by_shift") or {}
    issues: List[Dict] = []
    for s in S:
        per_grade_max = gc_max.get(s) or {}
        for g_raw, max_raw in per_grade_max.items():
            try:
                g = int(g_raw)
                max_t = int(max_raw or 0)
            except (TypeError, ValueError):
                continue
            if max_raw is None:
                continue
            for d in range(inp.num_days):
                nd = _need(inp.roster_config, s, d)
                if nd <= 0:
                    continue
                non_g = 0
                for n in inp.nurses:
                    if n.grade is not None and int(n.grade) == g:
                        continue
                    if not _active(n, d):
                        continue
                    if s in _allowed_set(n, S):
                        non_g += 1
                if nd - max_t > non_g:
                    issues.append(
                        _issue(
                            "GRADE_ANTIPAIR_FORCES_SHORTAGE",
                            {
                                "shift": s,
                                "day": d,
                                "grade": g,
                                "max": max_t,
                                "non_grade_available": non_g,
                                "need": nd,
                            },
                        )
                    )
    return issues


def check_fixed_assign_breaks_team_min(inp: PrecheckInput) -> List[Dict]:
    S = _apply_shifts(bool(inp.roster_config.get("use_mid", False)))
    issues: List[Dict] = []
    members_by_team: Dict[Any, List[PrecheckNurse]] = {}
    for n in inp.nurses:
        if n.team_id in (None, "", 0):
            continue
        members_by_team.setdefault(n.team_id, []).append(n)
    for tid, tmin in (inp.team_coverage or {}).items():
        if not tmin:
            continue
        need_sum = sum(int(tmin.get(s, 0) or 0) for s in S)
        if need_sum <= 0:
            continue
        mems = members_by_team.get(tid, [])
        for d in range(inp.num_days):
            remaining = 0
            for n in mems:
                if not (n.join_day <= d <= n.leave_day):
                    continue
                if d in n.fixed_off_days:
                    continue
                remaining += 1
            if remaining < need_sum:
                # team_min soft → 고정 OFF 로 팀이 min 미달이어도 벌점 흡수. warning.
                issues.append(
                    _issue(
                        "FIXED_ASSIGN_BREAKS_TEAM_MIN",
                        {
                            "team_id": tid,
                            "day": d,
                            "remaining_members": remaining,
                            "required_min_sum": need_sum,
                        },
                        severity="warning",
                    )
                )
    return issues


def check_fixed_off_exceeds_span(inp: PrecheckInput) -> List[Dict]:
    """Fix 4: 개인 단위 fixed_off 과다.

    fixed_off_days ∩ [join, leave] 가 근무 기간 전체를 점유하면 근무 불가.
    (E-2 의 fixed_off 버전)
    """
    issues: List[Dict] = []
    for n in inp.nurses:
        span = max(0, n.leave_day - n.join_day + 1)
        if span <= 0:
            continue
        fixed_off = sum(
            1 for d in n.fixed_off_days if n.join_day <= d <= n.leave_day
        )
        if fixed_off >= span:
            issues.append(
                _issue(
                    "FIXED_OFF_EXCEEDS_SPAN",
                    {
                        "nurse_id": n.nurse_id,
                        "span": span,
                        "fixed_off_count": fixed_off,
                    },
                )
            )
    return issues


def check_monthly_night_capacity(inp: PrecheckInput) -> List[Dict]:
    """월간 N 공급 vs 월간 N need.

    Fix 1: 공통풀에 국한하지 않고 N 허용 간호사 전체의 working capacity 를 합산한다.
    팀 여부/team_min[N] 값과 무관 — N 할 수 있는 모든 사람이 공급원이다.

    Fix 2 (α): cfg.max_night_shifts_per_month 한도도 동시에 적용 — 이 값이 명시되어
    있으면 nurse 당 night 가용일이 두 값 중 작은 쪽으로 제약됨.
    `cap = Σ_n min(working_capacity[n], max_night_shifts_per_month)`.
    """
    S = _apply_shifts(bool(inp.roster_config.get("use_mid", False)))
    n_capable = [n for n in inp.nurses if "N" in _allowed_set(n, S)]
    cfg_max_night = inp.roster_config.get("max_night_shifts_per_month")
    try:
        cfg_max_night = int(cfg_max_night) if cfg_max_night is not None else None
    except (TypeError, ValueError):
        cfg_max_night = None

    def _night_cap_for_nurse(n: PrecheckNurse) -> int:
        cap = _working_capacity(n, inp.roster_config)
        if cfg_max_night is not None and cfg_max_night >= 0:
            cap = min(cap, cfg_max_night)
        # per-nurse 야간 상한(n_exact/n_max)도 동시에 적용 — 전역 상한만 보면 야간 공급을
        # 과대계산해 shortage 를 놓친다. 상한을 조이는 방향이라 false positive 없음.
        if n.night_cap is not None and n.night_cap >= 0:
            cap = min(cap, n.night_cap)
        return cap

    cap = sum(_night_cap_for_nurse(n) for n in n_capable)
    monthly_need = sum(_need(inp.roster_config, "N", d) for d in range(inp.num_days))
    if cap >= monthly_need:
        return []

    # 일별 N 수요 분포 — peak day 식별
    daily_N_need = [
        {"day": d + 1, "demand": _need(inp.roster_config, "N", d)}
        for d in range(inp.num_days)
    ]
    avg_n_daily = monthly_need / inp.num_days if inp.num_days else 0.0
    peak_days = sorted(
        [r for r in daily_N_need if r["demand"] > avg_n_daily],
        key=lambda x: x["demand"], reverse=True,
    )

    # N 가능 nurse 별 working capacity — 누가 가장 가용 적은지
    n_capable_caps = [
        {"nurse_id": n.nurse_id, "capacity_days": _working_capacity(n, inp.roster_config)}
        for n in n_capable
    ]
    n_capable_caps.sort(key=lambda x: x["capacity_days"])

    return [
        _issue(
            "MONTHLY_NIGHT_CAPACITY_SHORTAGE",
            {
                # ontology template keys
                "n_required": monthly_need,
                "n_capacity": cap,
                "shortage": monthly_need - cap,
                # legacy keys
                "night_allowed_count": len(n_capable),
                "night_capacity": cap,
                "monthly_N_need": monthly_need,
                # 풍부 디테일
                "avg_daily_N_demand": round(avg_n_daily, 2),
                "peak_n_days": peak_days,
                "night_capable_nurses": n_capable_caps,
                "demand_uniform": len(peak_days) == 0,
            },
        )
    ]


def check_daily_night_shortage(inp: PrecheckInput) -> List[Dict]:
    """일별 N 수요 > 그 날 N 가능 active 인원.

    monthly_night 와 다른 차원 — 월 합은 충분하지만 특정 day 에 N 가능자가
    OFF/휴가/carryover 회복 등으로 부족할 수 있다.
    """
    S = _apply_shifts(bool(inp.roster_config.get("use_mid", False)))
    issues: List[Dict] = []
    n_capable_ids = {n.nurse_id for n in inp.nurses if "N" in _allowed_set(n, S)}
    if not n_capable_ids:
        return []

    for d in range(inp.num_days):
        nd = _need(inp.roster_config, "N", d)
        if nd <= 0:
            continue
        # 그 날 N 가능하면서 active 한 nurse
        active_n_capable = [
            n.nurse_id for n in inp.nurses
            if n.nurse_id in n_capable_ids and _active(n, d)
        ]
        avail = len(active_n_capable)
        if nd <= avail:
            continue

        # 비활성 사유 — 같은 N 가능자 중 fixed_off/leave/join 으로 빠진 사람
        blocked = []
        for n in inp.nurses:
            if n.nurse_id not in n_capable_ids:
                continue
            if _active(n, d):
                continue
            reason = []
            if d in n.fixed_off_days:
                reason.append("fixed_off")
            if d < n.join_day:
                reason.append("not_joined")
            if d > n.leave_day:
                reason.append("after_leave")
            blocked.append({"nurse_id": n.nurse_id, "reasons": reason or ["unknown"]})

        issues.append(
            _issue(
                "N_CAPACITY_SHORTAGE",
                {
                    # ontology template keys
                    "day": d + 1,
                    "n_required": nd,
                    "n_capacity": avail,
                    "shortage": nd - avail,
                    # 풍부 디테일
                    "active_night_capable_nurses": active_n_capable,
                    "blocked_night_capable_nurses": blocked,
                    "total_night_capable_pool": len(n_capable_ids),
                },
            )
        )
    return issues


def check_preceptee_sync_mismatch(inp: PrecheckInput) -> List[Dict]:
    """preceptor-preceptee pair 가 동시 근무 불가 → 페어링 실패.

    감지 사유:
      - shift_intersection_empty: 양쪽 allowed_shifts intersection ∅
      - team_mismatch: team_id 불일치
      - window_empty: sync_window 가 양쪽 active span 와 ∅
    """
    S = _apply_shifts(bool(inp.roster_config.get("use_mid", False)))
    by_id: Dict[str, PrecheckNurse] = {n.nurse_id: n for n in inp.nurses}
    # 상호배제(배반) 맵 — preceptor-preceptee 페어가 동시에 mutex 로 걸리면 '함께근무 + 배반'
    # 직접 모순(데이터-리딩성 상태 오염 포함). shift/team 이 호환이어도 이건 표현돼야 한다.
    _mutex_map = inp.roster_config.get("mutual_exclusion_by_nurse_id") or {}

    def _pair_has_mutex(a_id: str, b_id: str) -> bool:
        for k in (a_id, b_id):
            info = _mutex_map.get(str(k))
            if isinstance(info, dict) and info.get("days") \
                    and str(info.get("partner_id")) in (str(a_id), str(b_id)):
                return True
        return False

    issues: List[Dict] = []
    for n in inp.nurses:
        if not n.preceptor_id:
            continue
        ptor = by_id.get(str(n.preceptor_id))
        if ptor is None:
            continue  # mentor 가 PrecheckInput 에 없으면 다른 영역에서 처리

        ptor_shifts = _allowed_set(ptor, S)
        ptee_shifts = _allowed_set(n, S)
        shift_intersection = ptor_shifts & ptee_shifts

        team_match = (ptor.team_id is not None
                      and ptor.team_id == n.team_id
                      and ptor.team_id not in (None, "", 0))

        ws_start = n.sync_window_start if n.sync_window_start is not None else max(n.join_day, ptor.join_day)
        ws_end = n.sync_window_end if n.sync_window_end is not None else min(n.leave_day, ptor.leave_day)
        window_days = max(0, ws_end - ws_start + 1)
        # active span ∩ window — 두 사람 모두 활동하는 구간 안에 window 가 있어야 의미
        effective_start = max(ws_start, n.join_day, ptor.join_day)
        effective_end = min(ws_end, n.leave_day, ptor.leave_day)
        effective_days = max(0, effective_end - effective_start + 1)

        reasons: List[str] = []
        if not shift_intersection:
            reasons.append("shift_intersection_empty")
        if not team_match:
            reasons.append("team_mismatch")
        if effective_days <= 0:
            reasons.append("window_empty")
        # 함께근무(preceptee) 인데 동시에 상호배제(배반) → 직접 모순. shift/team 호환 여부와 무관.
        if _pair_has_mutex(ptor.nurse_id, n.nurse_id):
            reasons.append("mutual_exclusion_conflict")

        if not reasons:
            continue

        issues.append(
            _issue(
                "PRECEPTEE_SYNC_MISMATCH",
                {
                    # ontology template keys
                    "preceptor_id": ptor.nurse_id,
                    "preceptee_id": n.nurse_id,
                    "start_day": ws_start + 1,
                    "end_day": ws_end + 1,
                    # 풍부 디테일
                    "window_days": window_days,
                    "mismatch_reasons": reasons,
                    "preceptor_shifts": sorted(ptor_shifts),
                    "preceptee_shifts": sorted(ptee_shifts),
                    "shift_intersection": sorted(shift_intersection),
                    "preceptor_team": ptor.team_id,
                    "preceptee_team": n.team_id,
                },
            )
        )
    return issues


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

_CONFIG_ERROR_CODES = {
    "MID_REQUIRED_MISSING",
    "MID_DISABLED_BUT_USED",
    "ALLOWED_SHIFTS_ISOLATES_NURSE",
    "FIXED_ASSIGN_EXCEEDS_NEED",
    "FIXED_ASSIGN_VIOLATES_ALLOWED",
    "FIXED_OFF_EXCEEDS_SPAN",
    "GRADE_MIN_EXCEEDS_MAX",
}


def _dedup_issues(issues: List[Dict]) -> List[Dict]:
    """Fix 2: ANTIPAIR × MAX_SUM_BELOW_NEED 중복 억제.

    같은 (shift, day) 에 GRADE_ANTIPAIR_FORCES_SHORTAGE 가 이미 있으면
    GRADE_MAX_SUM_BELOW_NEED 는 suppress. 전자가 더 구체적이고 같은 root cause.
    """
    antipair_keys = {
        (i["evidence"].get("shift"), i["evidence"].get("day"))
        for i in issues
        if i["reason_code"] == "GRADE_ANTIPAIR_FORCES_SHORTAGE"
    }
    result: List[Dict] = []
    for i in issues:
        if (
            i["reason_code"] == "GRADE_MAX_SUM_BELOW_NEED"
            and (i["evidence"].get("shift"), i["evidence"].get("day")) in antipair_keys
        ):
            continue
        result.append(i)
    return result


def run_precheck(
    inp: PrecheckInput,
    *,
    stop_on_config_error: bool = False,
) -> Dict[str, Any]:
    """전체 precheck 실행.

    Args:
        inp: PrecheckInput
        stop_on_config_error: True 시 설정성 오류(D, E-2, F-1, F-2)가 발견되면
            이후 일자별 검사를 스킵한다.

    Returns:
        {"status": "OK" | "HAS_ISSUES", "issues": [...]}
    """
    issues: List[Dict] = []

    # Phase 1 — 설정성 오류
    config_phase = [
        check_mid_required_missing,
        check_mid_disabled_but_used,
        check_allowed_shifts_isolates_nurse,
        check_fixed_off_exceeds_span,  # Fix 4
        check_grade_min_exceeds_max,
        check_team_size_insufficient,
        check_grade_min_sum_exceeds_need,
        check_fixed_assign_exceeds_need,
        check_fixed_assign_violates_allowed,
    ]
    for fn in config_phase:
        issues.extend(fn(inp))

    config_error_hit = any(i["reason_code"] in _CONFIG_ERROR_CODES for i in issues)
    if stop_on_config_error and config_error_hit:
        return {"status": "HAS_ISSUES", "issues": _dedup_issues(issues)}

    # Phase 2 — 데이터성 (일자별)
    day_phase = [
        check_global_day_capacity_shortage,
        check_global_shift_allowed_shortage,
        check_capacity_total_shortage,
        check_team_min_exceeds_global_need,
        check_team_active_members_insufficient,
        check_team_shift_allowed_shortage,
        check_grade_max_sum_below_need,
        check_grade_min_available_shortage,
        check_grade_antipair_forces_shortage,
        check_fixed_assign_breaks_team_min,
        check_monthly_night_capacity,  # Fix 1 (renamed from check_common_pool_night_capacity)
        check_daily_night_shortage,
        check_preceptee_sync_mismatch,
    ]
    for fn in day_phase:
        issues.extend(fn(inp))

    issues = _dedup_issues(issues)  # Fix 2
    status = "OK" if not issues else "HAS_ISSUES"
    return {"status": status, "issues": issues}
