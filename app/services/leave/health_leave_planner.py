"""보건휴가 자동 부여 계획기.

`roster_config.health_leave_enabled=True` 인 그룹에서, 적격 간호사에게 월 1개씩
`shifts.health_leave_target=True` 인 코드를 **생성 전에** 배정할 날짜를 정한다.

★ off_swap 과 무관하다 — 초과 OFF 를 변환하는 후처리가 아니라, 근무일을 대체하는
  사전 주입이다. OFF 쿼터는 소비하지 않는다(cp_sat_basic 의 vacation_types 경로).
  상세: docs/leave_auto_assignment_design.md §4.1

★ 반환 dict 는 `special_fixed_requests` 규약을 따른다. `shift_type` 을 생략하면
  cp_sat_basic 이 일반 OFF 로 처리해 쿼터를 먹으므로 반드시 싣는다(설계 §6 Step2 조건①).
"""
from __future__ import annotations

import logging
from datetime import date
from typing import Any, Optional

from sqlalchemy.orm import Session

from db.models import RosterConfig, Shift

logger = logging.getLogger(__name__)

# 하루에 몰릴 수 있는 최대 인원 = max(_MIN_PER_DAY, 적격자수 // _SPREAD_DIVISOR).
# 한 날짜에 몰리면 그날 커버리지가 무너지므로 분산 상한을 둔다.
_SPREAD_DIVISOR = 10
_MIN_PER_DAY = 1


def resolve_health_leave_shift(db: Session, group_id: str) -> Optional[Shift]:
    """그룹의 `health_leave_target=True` 인 근무코드. 다수면 sequence ASC 첫 건 + warning.

    ★ off_swap 의 `_resolve_target_shift` 와 형태만 같고 대상 컬럼이 다르다.
    """
    rows = (
        db.query(Shift)
        .filter(Shift.group_id == group_id, Shift.health_leave_target == True)  # noqa: E712
        .order_by(Shift.sequence.asc())
        .all()
    )
    if not rows:
        return None
    if len(rows) > 1:
        logger.warning(
            "[HealthLeave] group=%s 타깃 코드 %d건 — sequence 첫 건(%s) 채택. "
            "앱단 검증(_assert_health_leave_target_valid)을 우회한 데이터.",
            group_id, len(rows), rows[0].shift_id,
        )
    return rows[0]


def _latest_config(db: Session, group_id: str) -> Optional[RosterConfig]:
    """생성이 고르는 것과 동일하게 `created_at` 최신 1건 (config_id 최대가 아니다)."""
    return (
        db.query(RosterConfig)
        .filter(RosterConfig.group_id == group_id)
        .order_by(RosterConfig.created_at.desc())
        .first()
    )


def _is_night_only(nurse: Any) -> bool:
    """N 전담 여부.

    ★ 판정 소스는 `allowed_shifts` 다 — 호출 시점에 대상월 as-of period 로 오버레이된
      실효값이며, 파이프라인의 team 미지정 분기도 같은 함수를 쓴다(roster_create_service).
      `is_night_nurse` 컬럼은 as-of-TODAY 캐시라 미래월 생성에서 stale 하다.
    """
    from services.cp_sat.allowed_shift_types import is_n_only_profile
    return bool(is_n_only_profile(getattr(nurse, "allowed_shifts", None)))


def eligible_nurses(nurses: list, active_range_map: dict,
                    leave_flags: Optional[dict] = None) -> list:
    """보건휴가 적격자 (실측 규칙 §3.1 + `nurse_leave_period` 3-state 예외).

    자동판정 규칙 — 활성 · 그 달 근무구간 있음 · 여성 · N전담 아님.
    그 위에 per-nurse 예외가 있으면 그것이 이긴다(`is_health_leave_eligible`).

    ★ 임신은 `nurse_leave_period.pregnant` 로만 판정한다. 예전엔 그 달 확정 원티드의
      코드명이 "산전" 으로 시작하는지 봤는데, **병원마다 코드명이 달라**(VD·출산 등)
      102243 밖에서는 동작하지 않았다. 추정 대신 명시 관리로 바꾼다.
      (엑셀 실측 근거는 유지된다 — 산전 17건 중 보건 수령 0건 / 없음 521건 중 413건 79%.
       근거가 바뀐 게 아니라 판별 소스만 바뀌었다.)

    ★★ 고정근무자도 후보에 넣는다 — 자동판정만 끄고 개인속성으로 켤 수 있게 한다.
      실무 실측(엑셀 11개): 고정근무 69명 중 **34명(49.3%)이 보건휴가를 받는다**
      (예: 중환자실 이주희 — M 16일 전원 고정인데 10일에 보건 수령).
      49.3% 는 자동판정으로 못 맞추는 값이다. 켜면 35명 과잉, 끄면 34명 누락이라
      **기본은 끄고**(auto=False) 줄 사람만 `health_leave_eligible=1` 로 관리한다.
      후보 목록에서 아예 빼면 개인속성을 켜도 순회 대상이 아니라 무시된다 —
      그래서 "빼는" 게 아니라 "auto 를 False 로 두는" 방식이어야 한다.

    Args:
        nurses: 대상 후보. **고정근무자를 포함한 그룹 전체**를 넘긴다.
        active_range_map: `_clip_active_range_for_leaves` 통과본. 값이 None 이면
            그 달 전체 비활성(휴직/퇴사) → 제외. 조건② 자동 충족.
        leave_flags: `fetch_leave_flags()` 결과 {nurse_id: {...}}. 비어 있으면 전원 자동판정.

    Returns:
        적격 간호사 리스트.
    """
    from services.leave.leave_eligibility import is_health_leave_eligible

    flags_map = leave_flags or {}
    out = []
    for n in nurses:
        nid = str(getattr(n, "nurse_id", "") or "")
        if not nid or getattr(n, "active", 1) == 0:
            continue
        # 아래 둘은 예외로도 못 넘긴다 — 그 달에 근무 자체가 없거나 이미 다른 사유로 빠진 셀.
        if active_range_map.get(nid) is None:
            continue
        auto = (
            str(getattr(n, "gender", "") or "").strip() == "여"
            and not _is_night_only(n)
            and not str(getattr(n, "fixed_shift", "") or "").strip()
        )
        if not is_health_leave_eligible(flags_map.get(nid), auto):
            continue
        out.append(n)
    return out


def candidate_days(year: int, month: int, days_in_month: int, allow_weekend: bool) -> list[int]:
    """배치 후보일(1-based). `health_leave_weekend=False` 면 평일만.

    Notes:
        공휴일 제외는 하지 않는다 — 실측 규칙에 근거가 없고, 대체공휴일까지 빼면
        후보일이 과도하게 줄어 분산이 무너진다.
    """
    days = range(1, days_in_month + 1)
    if allow_weekend:
        return list(days)
    return [d for d in days if date(year, month, d).weekday() < 5]


def _assign_days(eligible: list, days: list[int], active_range_map: dict,
                 taken: set[tuple[str, int]], year: int, month: int) -> list[tuple[Any, int]]:
    """적격자에게 후보일을 **월 전체에 균등 분산** 배정한다.

    ★ 시작점을 `i * len(days) / len(eligible)` 로 잡는다. `days[i]` 로 잡으면
      인원수만큼만 앞에서 채워져 **소규모 그룹이 월초에 연속으로 붙는다**
      (실측: 별관1 6명 → 1~6일 연속 · 52-AN 9명 → 1~9일 연속 · 월 후반 전체 공백).

    ★★ 고정근무자는 **평일만** 준다 — 주말은 이미 OFF 라 휴가로 바꿔도 의미가 없다
      (고정근무 스케줄 = 평일 코드 / 주말 OFF). 실무도 근무일을 대체한다
      (중환자실 이주희: M 16일 중 하루가 보건).
    """
    per_day_cap = max(_MIN_PER_DAY, len(eligible) // _SPREAD_DIVISOR)
    used: dict[int, int] = {}
    picked: list[tuple[Any, int]] = []
    n_elig = max(1, len(eligible))
    for i, nurse in enumerate(eligible):
        nid = str(nurse.nurse_id)
        rng = active_range_map.get(nid)
        is_fixed = bool(str(getattr(nurse, "fixed_shift", "") or "").strip())
        base = (i * len(days)) // n_elig
        for offset in range(len(days)):
            day = days[(base + offset) % len(days)]
            if used.get(day, 0) >= per_day_cap:
                continue
            if rng is not None and not (rng[0] <= day - 1 <= rng[1]):
                continue
            if (nid, day) in taken:
                continue
            if is_fixed and date(year, month, day).weekday() >= 5:
                continue
            used[day] = used.get(day, 0) + 1
            picked.append((nurse, day))
            break
        else:
            logger.warning("[HealthLeave] %s(%s) 배치 가능일 없음 — 스킵", nurse.name, nid)
    return picked


def plan_health_leave(
    db: Session,
    *,
    group_id: str,
    year: int,
    month: int,
    days_in_month: int,
    engine_nurses: list,
    active_range_map: dict,
    existing_requests: Optional[list[dict]] = None,
) -> list[dict]:
    """보건휴가 사전 주입 계획. `special_fixed_requests` 에 extend 할 dict 리스트.

    ★★ 왜 사전 주입인가 (2026-08-04 재확인)
      "대상자의 OFF 하한을 +1 하고 후처리로 OFF 하나를 치환"하는 방식을 시도했다.
      배치는 실무와 맞아졌지만(OFF 인접 100% vs 실무 86.6%) **OFF 총량이 무너졌다**:
      ```
      2026-08 (사전주입)  중환자실 26명 OFF 11~11  전원 균일
      2026-09 (후처리)    중환자실 26명 OFF  9~12  12초과 9명 · 9미달 3명
      ```
      `off_first=False` 는 상한=하한이라 OFF 가 고정이어야 하는데, 하한을 올리면
      `base_cap` 도 같이 올라가고 거기에 개인별 `_extra_off_fb` 가 얹혀 분산됐다.
      배치 위치보다 **OFF 균등이 근무표 품질에서 우선**이라 사전 주입으로 되돌린다.

    ★ 고정근무자도 대상이 된다 — `_overlay_fixed_roster_with_special_requests` 가
      고정 스케줄 위에 이 요청을 덮어쓰므로 별도 경로가 필요 없다.
      단 자동판정은 꺼져 있어(`eligible_nurses` 의 auto) 개인속성으로 켜야 한다.

    Returns:
        [{"nurse_id", "day", "shift_id", "shift_type"}, ...]
    """
    cfg = _latest_config(db, group_id)
    if not bool(getattr(cfg, "health_leave_enabled", False)):
        return []
    target = resolve_health_leave_shift(db, group_id)
    if target is None:
        logger.info("[HealthLeave] group=%s enabled 지만 타깃 코드 없음 — 미부여", group_id)
        return []

    # ★ 이미 그 달에 보건휴가가 잡힌 사람은 제외한다 — **원티드 제출이 우선**이다.
    #   실무는 월 1개가 원칙이다(2026-07·08 확정본에서 월 2개 이상 0건).
    already = {
        str(r.get("nurse_id"))
        for r in (existing_requests or [])
        if str(r.get("shift_id") or "").strip() == target.shift_id
    }
    # per-nurse 예외(3-state). 테이블이 비어 있으면 {} 라 전원 자동판정 = 도입 전과 동일.
    from services.leave.leave_eligibility import fetch_leave_flags
    flags = fetch_leave_flags(
        db, [getattr(n, "nurse_id", None) for n in engine_nurses], year, month
    )
    eligible = [n for n in eligible_nurses(engine_nurses, active_range_map, flags)
                if str(getattr(n, "nurse_id", "")) not in already]
    if not eligible:
        return []
    days = candidate_days(year, month, days_in_month,
                          bool(getattr(cfg, "health_leave_weekend", False)))
    if not days:
        return []

    taken = {
        (str(r.get("nurse_id")), int(r.get("day")))
        for r in (existing_requests or [])
        if r.get("nurse_id") and r.get("day")
    }
    picked = _assign_days(eligible, days, active_range_map, taken, year, month)
    n_fixed = sum(1 for n, _ in picked
                  if str(getattr(n, "fixed_shift", "") or "").strip())
    print(
        f"[HealthLeave] group={group_id} {year}-{month:02d} "
        f"적격 {len(eligible)}명(예외설정 {len(flags)}·원티드선점 {len(already)}) "
        f"→ 배정 {len(picked)}건(고정근무 {n_fixed}) "
        f"code={target.shift_id} type={target.type}"
    )
    return [
        {
            "nurse_id": str(n.nurse_id),
            "day": day,
            "shift_id": target.shift_id,
            # ★ 생략 금지 — 비면 vacation_types 판정에서 탈락해 OFF 쿼터를 먹는다.
            "shift_type": target.type,
        }
        for n, day in picked
    ]


def select_health_leave_targets(
    db: Session,
    *,
    group_id: str,
    year: int,
    month: int,
    engine_nurses: list,
    active_range_map: dict,
    existing_requests: Optional[list[dict]] = None,
) -> set[str]:
    """보건휴가를 받을 간호사 id 집합. **날짜는 정하지 않는다.**

    후처리(`postprocess_health_leave`)가 OFF 하나를 휴가코드로 바꾼다.
    대상자는 생성 시 OFF 하한이 1 올라가고(`extra_min_off`) **상한도 하한으로 고정**된다.

    ★ 배치 근거 — 실무 엑셀 11개·보건 셀 238건: OFF 인접 86.6% / 근무일 사이 단독 6.7%.
      날짜를 미리 찍으면 후자가 되므로 배치를 솔버에 맡긴다.
    """
    cfg = _latest_config(db, group_id)
    if not bool(getattr(cfg, "health_leave_enabled", False)):
        return set()
    target = resolve_health_leave_shift(db, group_id)
    if target is None:
        logger.info("[HealthLeave] group=%s enabled 지만 타깃 코드 없음 — 미부여", group_id)
        return set()
    already = {
        str(r.get("nurse_id"))
        for r in (existing_requests or [])
        if str(r.get("shift_id") or "").strip() == target.shift_id
    }
    from services.leave.leave_eligibility import fetch_leave_flags
    flags = fetch_leave_flags(
        db, [getattr(n, "nurse_id", None) for n in engine_nurses], year, month
    )
    out = {
        str(getattr(n, "nurse_id", ""))
        for n in eligible_nurses(engine_nurses, active_range_map, flags)
        if str(getattr(n, "nurse_id", "")) not in already
    }
    print(
        f"[HealthLeave] group={group_id} {year}-{month:02d} "
        f"대상 {len(out)}명(예외설정 {len(flags)}·원티드선점 {len(already)}) "
        f"code={target.shift_id} type={target.type}"
    )
    return out
