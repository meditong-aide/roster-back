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
    if raw is None or len(raw) == 0:
        return set(S)
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
    return max(0, span - _required_off_days(nurse, cfg))


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
    if "M" not in dsr or int(dsr.get("M", 0) or 0) <= 0:
        return [_issue("MID_REQUIRED_MISSING", {"daily_shift_requirements": dict(dsr)})]
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
            issues.append(
                _issue(
                    "TEAM_SIZE_INSUFFICIENT",
                    {"team_id": tid, "team_size": size, "team_min_sum": need_sum},
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
            avail = 0
            for n in inp.nurses:
                if not _active(n, d):
                    continue
                if s in _allowed_set(n, S):
                    avail += 1
            if nd > avail:
                issues.append(
                    _issue(
                        "GLOBAL_SHIFT_ALLOWED_SHORTAGE",
                        {"shift": s, "day": d, "required": nd, "allowed_nurses": avail},
                    )
                )
    return issues


def check_capacity_total_shortage(inp: PrecheckInput) -> List[Dict]:
    S = _apply_shifts(bool(inp.roster_config.get("use_mid", False)))
    total_need = sum(_need(inp.roster_config, s, d) for d in range(inp.num_days) for s in S)
    total_cap = sum(_working_capacity(n, inp.roster_config) for n in inp.nurses)
    if total_need > total_cap:
        return [
            _issue(
                "CAPACITY_TOTAL_SHORTAGE",
                {
                    "required_total": total_need,
                    "capacity_total": total_cap,
                    "nurse_count": len(inp.nurses),
                    "num_days": inp.num_days,
                },
            )
        ]
    return []


def check_team_min_exceeds_global_need(inp: PrecheckInput) -> List[Dict]:
    S = _apply_shifts(bool(inp.roster_config.get("use_mid", False)))
    issues: List[Dict] = []
    for s in S:
        tmin_sum = sum(int((tm or {}).get(s, 0) or 0) for tm in (inp.team_coverage or {}).values())
        if tmin_sum <= 0:
            continue
        for d in range(inp.num_days):
            nd = _need(inp.roster_config, s, d)
            if tmin_sum > nd:
                issues.append(
                    _issue(
                        "TEAM_MIN_EXCEEDS_GLOBAL_NEED",
                        {"shift": s, "day": d, "teams_min_sum": tmin_sum, "global_need": nd},
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
                issues.append(
                    _issue(
                        "TEAM_ACTIVE_MEMBERS_INSUFFICIENT",
                        {
                            "team_id": tid,
                            "day": d,
                            "active_count": active_cnt,
                            "required_min_sum": need_sum,
                        },
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
                issues.append(
                    _issue(
                        "FIXED_ASSIGN_BREAKS_TEAM_MIN",
                        {
                            "team_id": tid,
                            "day": d,
                            "remaining_members": remaining,
                            "required_min_sum": need_sum,
                        },
                    )
                )
    return issues


def check_team_grade_intersect_shortage(inp: PrecheckInput) -> List[Dict]:
    """Fix 3: 팀 × Grade 교차 infeasibility.

    팀 t 의 shift s 요구(team_min[t,s]) 를 채울 때, 팀원 중 s 허용자를 grade 제약으로
    cap 한 유효 배정 가능수가 요구 미달이면 infeasible.

    공식:
        for each (t, s, d) with team_min[t,s] > 0:
            by_grade[g] = |{ n ∈ team_t : active(n,d) ∧ s∈allowed(n) ∧ grade(n)==g }|
            effective = Σ_g min(by_grade[g], max_by_shift[s][g])   # grade 무지정은 cap 없음
            if effective < team_min[t,s]: infeasible
    """
    S = _apply_shifts(bool(inp.roster_config.get("use_mid", False)))
    gc_max = (inp.grade_constraints or {}).get("max_by_shift") or {}
    if not gc_max:
        return []
    members_by_team: Dict[Any, List[PrecheckNurse]] = {}
    for n in inp.nurses:
        if n.team_id in (None, "", 0):
            continue
        members_by_team.setdefault(n.team_id, []).append(n)
    issues: List[Dict] = []
    for tid, tmin in (inp.team_coverage or {}).items():
        if not tmin:
            continue
        mems = members_by_team.get(tid, [])
        for s in S:
            req = int((tmin or {}).get(s, 0) or 0)
            if req <= 0:
                continue
            per_grade_max_raw = gc_max.get(s) or {}
            if not per_grade_max_raw:
                continue  # grade 제약 없으면 B-4 가 커버
            per_grade_max = {}
            for g_raw, v in per_grade_max_raw.items():
                if v is None:
                    continue
                try:
                    per_grade_max[int(g_raw)] = int(v)
                except (TypeError, ValueError):
                    continue
            if not per_grade_max:
                continue
            for d in range(inp.num_days):
                by_grade: Dict[Any, int] = {}
                for n in mems:
                    if not _active(n, d):
                        continue
                    if s not in _allowed_set(n, S):
                        continue
                    g = n.grade
                    by_grade[g] = by_grade.get(g, 0) + 1
                effective = 0
                for g, cnt in by_grade.items():
                    if g is None:
                        effective += cnt
                        continue
                    try:
                        gi = int(g)
                    except (TypeError, ValueError):
                        effective += cnt
                        continue
                    cap = per_grade_max.get(gi)
                    effective += cnt if cap is None else min(cnt, cap)
                if effective < req:
                    issues.append(
                        _issue(
                            "TEAM_GRADE_INTERSECT_SHORTAGE",
                            {
                                "team_id": tid,
                                "shift": s,
                                "day": d,
                                "effective_capacity": effective,
                                "required": req,
                                "by_grade": {str(k): v for k, v in by_grade.items()},
                            },
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
    """
    S = _apply_shifts(bool(inp.roster_config.get("use_mid", False)))
    n_capable = [n for n in inp.nurses if "N" in _allowed_set(n, S)]
    cap = sum(_working_capacity(n, inp.roster_config) for n in n_capable)
    monthly_need = sum(_need(inp.roster_config, "N", d) for d in range(inp.num_days))
    if cap < monthly_need:
        return [
            _issue(
                "MONTHLY_NIGHT_CAPACITY_SHORTAGE",
                {
                    "night_allowed_count": len(n_capable),
                    "night_capacity": cap,
                    "monthly_N_need": monthly_need,
                },
            )
        ]
    return []


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
        check_team_grade_intersect_shortage,  # Fix 3
        check_fixed_assign_breaks_team_min,
        check_monthly_night_capacity,  # Fix 1 (renamed from check_common_pool_night_capacity)
    ]
    for fn in day_phase:
        issues.extend(fn(inp))

    issues = _dedup_issues(issues)  # Fix 2
    status = "OK" if not issues else "HAS_ISSUES"
    return {"status": status, "issues": issues}
