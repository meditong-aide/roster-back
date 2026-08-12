# app/routers/nurse_period.py
"""간호사 시점 속성(period) 쓰기 엔드포인트.

- POST /nurse-period/backfill : 현 nurses 캐시값을 period 테이블 첫 구간(open span)으로
  시드한다. 일회성 데이터 이행이지만 멱등(upsert_period)이라 재호출 안전.

설계: docs/NURSE_ATTRIBUTE_PERIOD_DESIGN.md (P2).
"""
from __future__ import annotations

from datetime import date, timedelta
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from db.client2 import get_db
from db.models import (
    Nurse,
    NurseGradePeriod,
    NurseAllowedShiftPeriod,
    NurseWeekendOffPeriod,
)
from routers.auth import get_current_user_from_cookie
from schemas.auth_schema import User as UserSchema
from services.group_access import assert_caller_can_access_group
from services.nurse_period_resolver import upsert_period, resolve_asof, fetch_periods
from services.cp_sat.allowed_shift_types import normalize_allowed_shift_codes

router = APIRouter(prefix="/nurse-period", tags=["nurse-period"])


def _allowed_value(n: Nurse):
    # allowed_shifts(JSON) → 허용 근무형 집합으로 정규화(현 솔버와 동일 의미)
    return sorted(normalize_allowed_shift_codes(getattr(n, "allowed_shifts", None)))


# 속성별 스펙: model · 값컬럼 · ward귀속 · nurses캐시컬럼(투영) · nurses→value 변환(backfill용)
#   · carry_attrs: 같은 satellite 의 형제 value 컬럼(새 구간 열 때 carry-forward)
# allowed_shifts 와 fixed_shift 는 한 테이블(nurse_allowed_shift_period)의 두 컬럼 = 결합 satellite.
def _weekend_off_backfill_value(n) -> int:
    """backfill 시 주말휴무 값 — 현재 period(as-of today)를 읽어 멱등 보존. 컬럼 미조회."""
    from sqlalchemy.orm import object_session
    from services.nurse_period_resolver import is_weekend_off_asof
    _db = object_session(n)
    if _db is None:
        return 0
    return 1 if is_weekend_off_asof(_db, n.nurse_id) else 0


_ATTR_SPECS: dict[str, dict] = {
    "allowed_shifts": dict(model=NurseAllowedShiftPeriod, value_attr="allowed_shifts",
                           group_bound=False, cache_attr="allowed_shifts",
                           value_fn=_allowed_value, carry_attrs=["fixed_shift"]),
    "fixed_shift": dict(model=NurseAllowedShiftPeriod, value_attr="fixed_shift",
                        group_bound=False, cache_attr="fixed_shift",
                        value_fn=lambda n: getattr(n, "fixed_shift", None),
                        carry_attrs=["allowed_shifts"]),
    # 주말휴무: SSOT=period, nurses.is_weekend_off 컬럼 언매핑됨.
    #   cache_attr 없음(투영 안 함). backfill value_fn 은 현재 period 값을 읽어 멱등(재백필 시
    #   기존 값 보존, 컬럼 미조회). _weekend_off_backfill_value 참조.
    "weekend_off": dict(model=NurseWeekendOffPeriod, value_attr="weekend_off",
                        group_bound=False,
                        value_fn=lambda n: _weekend_off_backfill_value(n)),
    "grade": dict(model=NurseGradePeriod, value_attr="grade",
                  group_bound=True, cache_attr="grade",
                  value_fn=lambda n: getattr(n, "grade", None)),
}


class BackfillRequest(BaseModel):
    group_id: Optional[str] = None
    valid_from: Optional[date] = None          # 첫 구간 시작일(기본=오늘)
    attributes: Optional[list[str]] = None     # 기본=전체 4종


class BackfillResult(BaseModel):
    group_id: str
    valid_from: date
    nurse_count: int
    rows: dict[str, int]                        # 속성 → backfill 후 해당 간호사들의 period row 수


@router.post("/backfill", response_model=BackfillResult)
async def backfill_nurse_periods(
    payload: BackfillRequest,
    current_user: UserSchema = Depends(get_current_user_from_cookie),
    db: Session = Depends(get_db),
):
    """그룹 소속 active 간호사의 현재 속성값을 period 첫 구간으로 시드(멱등)."""
    group_id = payload.group_id or getattr(current_user, "group_id", None)
    if not group_id:
        raise HTTPException(status_code=400, detail="group_id 가 필요합니다")
    assert_caller_can_access_group(db, current_user, group_id)

    attrs = payload.attributes or list(_ATTR_SPECS.keys())
    unknown = [a for a in attrs if a not in _ATTR_SPECS]
    if unknown:
        raise HTTPException(status_code=400, detail=f"알 수 없는 속성: {unknown}")

    valid_from = payload.valid_from or date.today()
    nurses = (
        db.query(Nurse)
        .filter(Nurse.group_id == group_id, Nurse.active == 1)
        .all()
    )

    for n in nurses:
        for a in attrs:
            spec = _ATTR_SPECS[a]
            upsert_period(
                db, spec["model"], n.nurse_id, valid_from,
                spec["value_attr"], spec["value_fn"](n),
                group_id=group_id if spec["group_bound"] else None,
                source="inherited", carry_attrs=spec.get("carry_attrs"),
            )
    db.commit()

    nurse_ids = [n.nurse_id for n in nurses]
    rows = {
        a: (
            db.query(_ATTR_SPECS[a]["model"])
            .filter(_ATTR_SPECS[a]["model"].nurse_id.in_(nurse_ids))
            .count()
            if nurse_ids else 0
        )
        for a in attrs
    }
    return BackfillResult(group_id=group_id, valid_from=valid_from,
                          nurse_count=len(nurses), rows=rows)


class ChangeRequest(BaseModel):
    attribute: str                             # allowed_shifts | weekend_off | fixed_shift | grade
    nurse_id: str
    valid_from: date                           # 이 날부터 value 적용(close-before-open)
    value: Any = None                          # allowed_shifts=list / weekend_off=0|1 / fixed_shift=str / grade=int
    group_id: Optional[str] = None             # grade(ward귀속)에 필요
    note: Optional[str] = None


class ChangeResult(BaseModel):
    attribute: str
    nurse_id: str
    valid_from: date
    value: Any
    today_value: Any                           # 변경 후 오늘 기준 as-of 값(투영 확인용)


@router.post("/change", response_model=ChangeResult)
async def change_nurse_period(
    payload: ChangeRequest,
    current_user: UserSchema = Depends(get_current_user_from_cookie),
    db: Session = Depends(get_db),
):
    """한 간호사의 한 속성을 valid_from 부터 변경(close-before-open) + 단방향 캐시 투영."""
    spec = _ATTR_SPECS.get(payload.attribute)
    if spec is None:
        raise HTTPException(status_code=400, detail=f"알 수 없는 속성: {payload.attribute}")

    nurse = db.query(Nurse).filter(Nurse.nurse_id == payload.nurse_id).first()
    if nurse is None:
        raise HTTPException(status_code=404, detail=f"간호사 없음: {payload.nurse_id}")

    # 권한: ward귀속(grade)은 명시 group_id, 그 외는 간호사 소속 그룹 기준
    group_id = payload.group_id or getattr(nurse, "group_id", None)
    assert_caller_can_access_group(db, current_user, group_id)

    if payload.attribute == "allowed_shifts" and not isinstance(payload.value, list):
        raise HTTPException(status_code=400, detail="allowed_shifts 는 리스트여야 합니다")

    # cross-attribute 모순 검증(저장 시점 hard gate): 그 달 월한도·고정근무와 충돌이면 422.
    if payload.attribute == "allowed_shifts":
        from services.nurse_period_validator import validate_allowed_shift_period
        issues = validate_allowed_shift_period(
            db, payload.nurse_id, group_id, payload.value, payload.valid_from,
        )
        blocking = [i for i in issues if i.get("severity", "blocking") == "blocking"]
        if blocking:
            raise HTTPException(status_code=422, detail={
                "message": "허용 근무형이 월 한도/고정근무 설정과 모순됩니다.",
                "issues": blocking,
            })

    upsert_period(
        db, spec["model"], payload.nurse_id, payload.valid_from,
        spec["value_attr"], payload.value,
        group_id=group_id if spec["group_bound"] else None,
        nurse=nurse, cache_attr=spec.get("cache_attr"), source="edited",
        carry_attrs=spec.get("carry_attrs"),
    )
    db.commit()

    # 변경 후 오늘 기준 as-of 값(미래발효면 gap/직전 구간, 캐시 투영 검증용)
    rows = (
        db.query(spec["model"]).filter(spec["model"].nurse_id == payload.nurse_id)
        .order_by(spec["model"].valid_from.asc()).all()
    )
    today_value = resolve_asof(rows, date.today(), spec["value_attr"], default=None)

    return ChangeResult(attribute=payload.attribute, nurse_id=payload.nurse_id,
                        valid_from=payload.valid_from, value=payload.value,
                        today_value=today_value)


class RollRequest(BaseModel):
    group_id: Optional[str] = None
    as_of: Optional[date] = None               # 기준일(기본=오늘). 미래발효 발효일 처리용
    attributes: Optional[list[str]] = None


class RollResult(BaseModel):
    group_id: str
    as_of: date
    nurse_count: int
    updated: dict[str, int]                    # 속성 → 캐시값이 바뀐 간호사 수


@router.post("/roll", response_model=RollResult)
async def roll_nurse_cache(
    payload: RollRequest,
    current_user: UserSchema = Depends(get_current_user_from_cookie),
    db: Session = Depends(get_db),
):
    """as_of(기본 오늘) 기준 period 값을 nurses 캐시 컬럼에 투영(단방향 동기화).

    미래발효 변경이 발효일에 캐시에 반영되도록 일일 호출(cron)용. 멱등.
    구간이 as_of 를 안 덮으면(gap) 캐시는 건드리지 않는다.
    """
    group_id = payload.group_id or getattr(current_user, "group_id", None)
    if not group_id:
        raise HTTPException(status_code=400, detail="group_id 가 필요합니다")
    assert_caller_can_access_group(db, current_user, group_id)

    attrs = payload.attributes or list(_ATTR_SPECS.keys())
    unknown = [a for a in attrs if a not in _ATTR_SPECS]
    if unknown:
        raise HTTPException(status_code=400, detail=f"알 수 없는 속성: {unknown}")

    as_of = payload.as_of or date.today()
    nurses = (
        db.query(Nurse)
        .filter(Nurse.group_id == group_id, Nurse.active == 1)
        .all()
    )
    nurse_ids = [n.nurse_id for n in nurses]
    nurse_by_id = {n.nurse_id: n for n in nurses}

    updated = {a: 0 for a in attrs}
    for a in attrs:
        spec = _ATTR_SPECS[a]
        by_nurse = fetch_periods(
            db, spec["model"], nurse_ids, as_of, as_of + timedelta(days=1),
            group_id=group_id if spec["group_bound"] else None,
        ) if nurse_ids else {}
        for nid, rows in by_nurse.items():
            _ca = spec.get("cache_attr")
            if _ca is None:
                continue                       # 캐시 없는 속성(주말휴무 등)은 roll 불필요(period fresh 읽음)
            val = resolve_asof(rows, as_of, spec["value_attr"], default=None)
            if val is None:
                continue                       # gap → 캐시 유지
            n = nurse_by_id.get(nid)
            if n is not None and getattr(n, _ca, None) != val:
                setattr(n, _ca, val)
                updated[a] += 1
    db.commit()

    return RollResult(group_id=group_id, as_of=as_of,
                      nurse_count=len(nurses), updated=updated)


# ──────────────────────────────────────────────────────────────
# 휴가 대상 3-state — 보건휴가 · 수면OFF · 임산부
#   None = 미설정(자동판정에 맡김) / True = 포함 / False = 제외
#   자동판정: 보건휴가는 여성 · N전담 아님 · 고정근무 아님. 수면OFF 는 전원.
#   설계: docs/leave_auto_assignment_design.md §6 Step6
# ──────────────────────────────────────────────────────────────
class LeaveFlagRow(BaseModel):
    nurse_id: str
    name: Optional[str] = None
    health_leave_eligible: Optional[bool] = None
    sleep_off_eligible: Optional[bool] = None
    pregnant: Optional[bool] = None


class LeaveFlagsResult(BaseModel):
    group_id: str
    year: int
    month: int
    rows: list[LeaveFlagRow]


@router.get("/leave-flags", response_model=LeaveFlagsResult)
async def get_leave_flags(
    year: int,
    month: int,
    group_id: Optional[str] = None,
    current_user: UserSchema = Depends(get_current_user_from_cookie),
    db: Session = Depends(get_db),
):
    """그룹 전원의 휴가 대상 3-state 를 대상월 기준으로 반환한다.

    행이 없는 간호사는 세 값이 모두 None(= 전부 자동판정)으로 나간다.
    """
    gid = group_id or getattr(current_user, "group_id", None)
    if not gid:
        raise HTTPException(status_code=400, detail="group_id 가 필요합니다")
    assert_caller_can_access_group(db, current_user, gid)
    nurses = db.query(Nurse).filter(Nurse.group_id == gid, Nurse.active == True).all()  # noqa: E712
    if not nurses:
        return LeaveFlagsResult(group_id=gid, year=year, month=month, rows=[])

    from services.leave.leave_eligibility import fetch_leave_flags

    ids = [str(n.nurse_id) for n in nurses]
    flags = fetch_leave_flags(db, ids, int(year), int(month))
    rows = [
        LeaveFlagRow(
            nurse_id=str(n.nurse_id),
            name=getattr(n, "name", None),
            **{k: (flags.get(str(n.nurse_id)) or {}).get(k)
               for k in ("health_leave_eligible", "sleep_off_eligible", "pregnant")},
        )
        for n in nurses
    ]
    return LeaveFlagsResult(group_id=gid, year=year, month=month, rows=rows)


class LeaveFlagUpdate(BaseModel):
    nurse_id: str
    group_id: Optional[str] = None
    valid_from: Optional[date] = None          # 기본=오늘
    health_leave_eligible: Optional[bool] = None
    sleep_off_eligible: Optional[bool] = None
    pregnant: Optional[bool] = None


@router.post("/leave-flags", response_model=LeaveFlagRow)
async def update_leave_flags(
    payload: LeaveFlagUpdate,
    current_user: UserSchema = Depends(get_current_user_from_cookie),
    db: Session = Depends(get_db),
):
    """한 간호사의 휴가 대상 3-state 를 바꾼다(close-before-open).

    ★ 보내지 않은 값 컬럼은 건드리지 않는다 — 명시적으로 null 을 보내야 '미설정'이
      된다. exclude_unset 으로 둘을 가른다. upsert_leave_period 가 형제 컬럼을
      직전 구간에서 승계하므로 호출부가 carry 를 신경 쓸 필요는 없다.
    """
    nurse = db.query(Nurse).filter(Nurse.nurse_id == str(payload.nurse_id)).first()
    if nurse is None:
        raise HTTPException(status_code=404, detail="간호사를 찾을 수 없습니다.")
    assert_caller_can_access_group(db, current_user, payload.group_id or nurse.group_id)

    sent = payload.model_dump(exclude_unset=True)
    values = {k: sent[k] for k in
              ("health_leave_eligible", "sleep_off_eligible", "pregnant") if k in sent}
    if not values:
        raise HTTPException(status_code=400, detail="변경할 값이 없습니다.")

    from services.leave.leave_eligibility import fetch_leave_flags, upsert_leave_period

    valid_from = payload.valid_from or date.today()
    try:
        upsert_leave_period(db, str(payload.nurse_id), valid_from,
                            source="edited", **values)
        db.commit()
    except HTTPException:
        raise
    except Exception as exc:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"휴가 대상 저장 실패: {exc}") from exc

    cur = fetch_leave_flags(db, [str(payload.nurse_id)],
                            valid_from.year, valid_from.month).get(str(payload.nurse_id)) or {}
    return LeaveFlagRow(nurse_id=str(payload.nurse_id), name=getattr(nurse, "name", None),
                        **{k: cur.get(k) for k in
                           ("health_leave_eligible", "sleep_off_eligible", "pregnant")})


# ──────────────────────────────────────────────────────────────
# 수면OFF 주기 상태 (nurse_night_cycle)
#   ★ 왜 별도 조회가 필요한가 — `schedule_entries.shift_id` 에는 'N' 만 있고
#     N1~N15 연번이 없다. 근무표를 아무리 봐도 "누가 몇 번째 N 인지",
#     "미부여분이 몇 건 이월됐는지" 를 알 수 없다. 그 상태는 이 테이블에만 있다.
#   ★ 특히 `pending_sleep`(이월 대기)은 **이번 달 표 어디에도 안 나온다.**
#     다음 달에 갑자기 나타나므로, 안 보여주면 운영자가 원인을 되짚을 수 없다.
#   설계: docs/leave_auto_assignment_design.md §5.2 · §6 Step4
# ──────────────────────────────────────────────────────────────
class NightCycleRow(BaseModel):
    nurse_id: str
    name: Optional[str] = None
    #: 그 달 마지막 N 의 연번 = 다음 달 시작점. 앵커가 없으면 None.
    seq_at_end: Optional[int] = None
    #: 자리를 못 찾아 다음 달로 넘어간 수면OFF 수. 이게 "대기 건수" 다.
    pending_sleep: Optional[int] = None
    #: 그 달 실제 부여 횟수(보통 0 또는 1).
    sleep_off_count: Optional[int] = None


class NightCycleResult(BaseModel):
    group_id: str
    year: int
    month: int
    #: 수면OFF 주기(연속 N 몇 회에 1건). 그룹 설정에서 해석한 값.
    cycle: Optional[int] = None
    #: pending_sleep 합계 — 화면 상단 요약용.
    pending_total: int = 0
    rows: list[NightCycleRow]


@router.get("/night-cycle", response_model=NightCycleResult)
async def get_night_cycle(
    year: int,
    month: int,
    group_id: Optional[str] = None,
    current_user: UserSchema = Depends(get_current_user_from_cookie),
    db: Session = Depends(get_db),
):
    """그룹 전원의 수면OFF 주기 상태를 대상월 기준으로 반환한다.

    앵커 행이 없는 간호사는 세 값이 모두 None 으로 나간다 — "0" 과 구분해야 한다.
    None 은 "그 달 마감본이 없거나 아직 계산 전", 0 은 "계산했고 대기가 없음" 이다.
    """
    from db.models import NurseNightCycle
    from services.leave.night_cycle_service import resolve_cycle

    gid = group_id or getattr(current_user, "group_id", None)
    if not gid:
        raise HTTPException(status_code=400, detail="group_id 가 필요합니다")
    assert_caller_can_access_group(db, current_user, gid)

    nurses = db.query(Nurse).filter(Nurse.group_id == gid, Nurse.active == True).all()  # noqa: E712
    if not nurses:
        return NightCycleResult(group_id=gid, year=year, month=month, rows=[])

    anchors = {
        str(r.nurse_id): r
        for r in db.query(NurseNightCycle).filter(
            NurseNightCycle.group_id == gid,
            NurseNightCycle.year == int(year),
            NurseNightCycle.month == int(month),
        ).all()
    }
    rows = [
        NightCycleRow(
            nurse_id=str(n.nurse_id),
            name=getattr(n, "name", None),
            seq_at_end=getattr(anchors.get(str(n.nurse_id)), "seq_at_end", None),
            pending_sleep=getattr(anchors.get(str(n.nurse_id)), "pending_sleep", None),
            sleep_off_count=getattr(anchors.get(str(n.nurse_id)), "sleep_off_count", None),
        )
        for n in nurses
    ]
    try:
        cycle = resolve_cycle(db, gid)
    except Exception:
        cycle = None
    return NightCycleResult(
        group_id=gid, year=year, month=month, cycle=cycle,
        pending_total=sum(int(r.pending_sleep or 0) for r in rows),
        rows=rows,
    )


class NightCycleRebuildRequest(BaseModel):
    year: int
    month: int
    group_id: Optional[str] = None


@router.post("/night-cycle/rebuild", response_model=NightCycleResult)
async def rebuild_night_cycle(
    payload: NightCycleRebuildRequest,
    current_user: UserSchema = Depends(get_current_user_from_cookie),
    db: Session = Depends(get_db),
):
    """(year, month) **부터 이후 모든 마감본**의 앵커를 다시 계산한다.

    ★ 앵커는 전월 값을 이어받으므로, 과거 달이 바뀌면 이후가 전부 틀어진다.
      그래서 그 달만이 아니라 **연쇄로** 재계산한다(`rebuild_night_cycle_from`).
    ★ 수동 편집 EP 는 두지 않는다 — `pending_sleep` 은 마감본에서 계산되는 값이라,
      손으로 고치면 다음 재계산에서 덮여 사라진다. 틀렸으면 재계산이 정답이다.

    ★ 조회보다 권한을 높인다. 이 호출은 그룹 전체 앵커를 다시 쓰고 고아 행을
      지운다 — 같은 파일의 `POST /leave-flags`(간호사 1명 플래그)와 파급이 다르다.
    """
    from services.group_access import caller_is_head_nurse
    from services.leave.night_cycle_service import rebuild_night_cycle_from

    gid = payload.group_id or getattr(current_user, "group_id", None)
    if not gid:
        raise HTTPException(status_code=400, detail="group_id 가 필요합니다")
    if not (caller_is_head_nurse(db, current_user)
            or getattr(current_user, "is_master_admin", False)):
        raise HTTPException(status_code=403, detail="Permission denied")
    assert_caller_can_access_group(db, current_user, gid)
    # ★ 범위 검증 — rebuild 는 (year, month) 이상을 튜플 비교로 훑고 고아 앵커를
    #   지운다. 0·13 같은 값이 들어오면 의도치 않은 범위가 재계산·삭제된다.
    if not (1 <= int(payload.month) <= 12) or not (2000 <= int(payload.year) <= 2100):
        raise HTTPException(status_code=400, detail="year/month 범위가 올바르지 않습니다")

    try:
        touched = rebuild_night_cycle_from(db, gid, int(payload.year), int(payload.month))
        # ★ 서비스는 flush 만 한다(여러 달을 순서대로 돌리려고). 커밋은 호출자 책임 —
        #   roster.py 의 기존 호출부도 직후에 db.commit() 한다. 빠뜨리면 응답은
        #   재계산 값을 보여주는데(같은 세션) DB 는 그대로 폐기된다.
        db.commit()
    except HTTPException:
        db.rollback()
        raise
    except Exception as exc:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"수면OFF 앵커 재계산 실패: {exc}") from exc

    print(f"[NightCycle] 앵커 재계산: group={gid} {payload.year}-{payload.month:02d} 이후 "
          f"{touched}행")
    return await get_night_cycle(payload.year, payload.month, gid, current_user, db)
