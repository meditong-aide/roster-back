from __future__ import annotations

from typing import List, Dict, Tuple
from calendar import monthrange
from sqlalchemy.orm import Session
from sqlalchemy import select, and_, update
from datetime import datetime

from db.models import DailyShift, ShiftManage, Nurse, Wanted
from services.shift_manage_defaults import ensure_default_shift_manage


def _get_month_days(year: int, month: int) -> int:
    """월의 말일(일수)을 반환합니다.
    - 인자: year(int), month(int)
    - 반환: int (해당 월의 일수)
    - 예시: 2024년 2월 → 29
    """
    return monthrange(year, month)[1]


def _query_rn_slots(db: Session, office_id: str, group_id: str) -> List[ShiftManage]:
    """shift_manage 에서 RN 클래스의 슬롯(1:D,2:E,3:N,5:M) 행을 읽어옵니다."""
    return (
        db.query(ShiftManage)
        .filter(
            ShiftManage.office_id == office_id,
            ShiftManage.group_id == group_id,
            ShiftManage.nurse_class == 'RN',
            ShiftManage.shift_slot.in_([1, 2, 3, 5]),
        )
        .order_by(ShiftManage.id.asc())  # 중복행 결정성: 슬롯별 최대 id(최근) 행 채택
        .all()
    )


def _read_template_from_shift_manage(db: Session, office_id: str, group_id: str) -> Tuple[int, int, int, int]:
    """shift_manage에서 RN 클래스의 슬롯(1:D,2:E,3:N,5:M)별 manpower를 읽어옵니다.
    - 반환: (day, evening, night, mid)
    - 예시: (3,2,2,0)
    - 템플릿이 아예 없으면(시프트 관리 화면 미개봉 병동) 기본 슬롯을 seeding 후 재조회한다.
      (부분 등록으로 slot 수가 3 미만인 손상 상태는 seeding no-op 이므로 여전히 에러 → 수동 점검 필요)
    """
    slots = _query_rn_slots(db, office_id, group_id)
    if not slots or len(slots) < 3:
        # 템플릿 미존재 → 기본값(D3/E3/N2/M0) seeding 후 재조회. shifts 라우터 lazy 생성과 동일 기준.
        ensure_default_shift_manage(db, office_id, group_id, "RN")
        slots = _query_rn_slots(db, office_id, group_id)
    if not slots or len(slots) < 3:
        raise ValueError("shift_manage 템플릿(RN)이 없습니다.")
    slot_to_value: Dict[int, int] = {}
    for s in slots:
        slot_no = int(getattr(s, "shift_slot", 0) or 0)
        slot_to_value[slot_no] = int(getattr(s, "manpower", 0) or 0)
    return (
        slot_to_value.get(1, 0),
        slot_to_value.get(2, 0),
        slot_to_value.get(3, 0),
        slot_to_value.get(5, 0),
    )


def _to_response(
    office_id: str,
    group_id: str,
    year: int,
    month: int,
    rows: List[DailyShift],
) -> Dict:
    """DailyShift row 리스트를 응답 포맷으로 변환합니다.
    - day=0 row 가 있으면 그 값을 month_summary 권위(월 일괄값)로 노출.
    - day=0 row 가 없으면 day=1 row 미러로 fallback (기존 동작).
    - date 배열은 day=1..말일 순서로만 구성 (day=0 분리).
    """
    rows_sorted = sorted(rows, key=lambda r: int(getattr(r, "day", 0) or 0))
    bulk_row = next((r for r in rows_sorted if int(getattr(r, "day", 0) or 0) == 0), None)
    daily_rows = [r for r in rows_sorted if int(getattr(r, "day", 0) or 0) > 0]
    d_list = [r.d_count for r in daily_rows]
    e_list = [r.e_count for r in daily_rows]
    n_list = [r.n_count for r in daily_rows]
    m_list = [int(getattr(r, "m_count", 0) or 0) for r in daily_rows]
    d_max_list = [int(getattr(r, "d_count_max", 0) or 0) for r in daily_rows]
    e_max_list = [int(getattr(r, "e_count_max", 0) or 0) for r in daily_rows]
    n_max_list = [int(getattr(r, "n_count_max", 0) or 0) for r in daily_rows]
    m_max_list = [int(getattr(r, "m_count_max", 0) or 0) for r in daily_rows]
    if bulk_row is not None:
        month_summary = {
            "D_count": int(bulk_row.d_count or 0),
            "E_count": int(bulk_row.e_count or 0),
            "N_count": int(bulk_row.n_count or 0),
            "M_count": int(getattr(bulk_row, "m_count", 0) or 0),
            "D_count_max": int(getattr(bulk_row, "d_count_max", 0) or 0),
            "E_count_max": int(getattr(bulk_row, "e_count_max", 0) or 0),
            "N_count_max": int(getattr(bulk_row, "n_count_max", 0) or 0),
            "M_count_max": int(getattr(bulk_row, "m_count_max", 0) or 0),
            "max_enabled": bool(getattr(bulk_row, "max_enabled", False)),
        }
    else:
        month_summary = {
            "D_count": d_list[0] if d_list else 0,
            "E_count": e_list[0] if e_list else 0,
            "N_count": n_list[0] if n_list else 0,
            "M_count": m_list[0] if m_list else 0,
            "D_count_max": d_max_list[0] if d_max_list else 0,
            "E_count_max": e_max_list[0] if e_max_list else 0,
            "N_count_max": n_max_list[0] if n_max_list else 0,
            "M_count_max": m_max_list[0] if m_max_list else 0,
            "max_enabled": bool(getattr(daily_rows[0], "max_enabled", False)) if daily_rows else False,
        }
    daily_max_enabled = bool(getattr(daily_rows[0], "max_enabled", False)) if daily_rows else False
    return {
        "office_id": office_id,
        "group_id": group_id,
        "year": year,
        "month": month,
        "month_summary": month_summary,
        "date": {
            "D_count": d_list,
            "E_count": e_list,
            "N_count": n_list,
            "M_count": m_list,
            "D_count_max": d_max_list,
            "E_count_max": e_max_list,
            "N_count_max": n_max_list,
            "M_count_max": m_max_list,
        },
        "max_enabled": daily_max_enabled,
    }


def get_or_init_month(db: Session, office_id: str, group_id: str, year: int, month: int) -> Dict:
    """월 단위 데이터를 조회하거나 없으면 shift_manage 템플릿으로 초기화합니다.
    - 예시: 2025/07 조회 시 데이터가 없으면 (D,E,N) = (3,2,2)로 31일 생성
    """
    rows = (
        db.query(DailyShift)
        .filter(
            DailyShift.office_id == office_id,
            DailyShift.group_id == group_id,
            DailyShift.year == year,
            DailyShift.month == month,
        )
        .all()
    )
    if not rows:
        print("[daily_shift] 월 데이터 없음 → shift_manage 기반으로 초기화")
        days = _get_month_days(year, month)
        d, e, n, mid_cnt = _read_template_from_shift_manage(db, office_id, group_id)
        for day_idx in range(1, days + 1):
            db.add(
                DailyShift(
                    office_id=office_id,
                    group_id=group_id,
                    year=year,
                    month=month,
                    day=day_idx,
                    d_count=d,
                    e_count=e,
                    n_count=n,
                    m_count=mid_cnt,
                )
            )
        db.commit()
        rows = (
            db.query(DailyShift)
            .filter(
                DailyShift.office_id == office_id,
                DailyShift.group_id == group_id,
                DailyShift.year == year,
                DailyShift.month == month,
            )
            .all()
        )
    return _to_response(office_id, group_id, year, month, rows)


def update_monthly(
    db: Session,
    office_id: str,
    group_id: str,
    year: int,
    month: int,
    day_cnt: int,
    eve_cnt: int,
    nig_cnt: int,
    mid_cnt: int = 0,
    day_max: int = 0,
    eve_max: int = 0,
    nig_max: int = 0,
    mid_max: int = 0,
    max_enabled: bool = False,
    apply_globally: bool = True,
) -> Dict:
    """월 전체 일괄 업데이트를 수행합니다.
    - shift_manage 템플릿을 갱신 후 daily_shift의 해당 월 모든 날짜를 동일 값으로 저장합니다.
    - max_enabled=False 이면 사용자 입력 *_max 값 무시하고 0 으로 reset (상한 미사용 모드).
    - max_enabled=True 이면 *_max 사용 — *_max>0 시 *_count <= *_max 보장 (max < min 시 max=min 으로 clamp).
    - 예시: day=4, evening=3, night=2이면 7월 1..말일을 (4,3,2)로 설정
    """
    days = _get_month_days(year, month)
    # max_enabled=False → *_max 값 강제 0; True → max < min clamp.
    if not max_enabled:
        day_max = eve_max = nig_max = mid_max = 0
    else:
        day_max = int(day_max) if int(day_max) <= 0 or int(day_max) >= int(day_cnt) else int(day_cnt)
        eve_max = int(eve_max) if int(eve_max) <= 0 or int(eve_max) >= int(eve_cnt) else int(eve_cnt)
        nig_max = int(nig_max) if int(nig_max) <= 0 or int(nig_max) >= int(nig_cnt) else int(nig_cnt)
        mid_max = int(mid_max) if int(mid_max) <= 0 or int(mid_max) >= int(mid_cnt) else int(mid_cnt)

    if apply_globally:
        # RN, slot 1~3 업데이트
        for slot, value in [(1, day_cnt), (2, eve_cnt), (3, nig_cnt), (5, mid_cnt)]:
            db.query(ShiftManage).filter(
                ShiftManage.office_id == office_id,
                ShiftManage.group_id == group_id,
                ShiftManage.nurse_class == 'RN',
                ShiftManage.shift_slot == slot,
            ).update({ShiftManage.manpower: int(value)})

    # 해당 월 데이터가 없다면 우선 생성
    existing = (
        db.query(DailyShift)
        .filter(
            DailyShift.office_id == office_id,
            DailyShift.group_id == group_id,
            DailyShift.year == year,
            DailyShift.month == month,
        )
        .all()
    )
    if not existing:
        for d_idx in range(1, days + 1):
            db.add(
                DailyShift(
                    office_id=office_id,
                    group_id=group_id,
                    year=year,
                    month=month,
                    day=d_idx,
                    d_count=day_cnt,
                    e_count=eve_cnt,
                    n_count=nig_cnt,
                    m_count=mid_cnt,
                    d_count_max=day_max,
                    e_count_max=eve_max,
                    n_count_max=nig_max,
                    m_count_max=mid_max,
                    max_enabled=max_enabled,
                )
            )
    else:
        # 벌크 업데이트
        for r in existing:
            r.d_count = int(day_cnt)
            r.e_count = int(eve_cnt)
            r.n_count = int(nig_cnt)
            r.m_count = int(mid_cnt)
            r.d_count_max = day_max
            r.e_count_max = eve_max
            r.n_count_max = nig_max
            r.m_count_max = mid_max
            r.max_enabled = max_enabled

    db.commit()
    return {"updated": True, "days_affected": days}


def _normalize_max_list(max_list: List[int], min_list: List[int], days: int) -> List[int]:
    """*_max 리스트를 days 길이로 맞추고 max < min 케이스는 min 으로 clamp 한다.

    빈 리스트면 [0]*days. 길이가 days 와 다르면 ValueError.
    각 day 의 max 가 0이면 상한 미설정으로 그대로 0 유지, 0 보다 크고 min 보다 작으면 min 으로 보정.
    """
    if not max_list:
        return [0] * days
    if len(max_list) != days:
        raise ValueError("*_max 리스트 길이가 월의 일수와 다릅니다.")
    out: List[int] = []
    for i in range(days):
        mx = int(max_list[i] or 0)
        mn = int(min_list[i] or 0)
        if mx <= 0:
            out.append(0)
        elif mx < mn:
            out.append(mn)
        else:
            out.append(mx)
    return out


def _upsert_day_zero(
    db: Session,
    office_id: str,
    group_id: str,
    year: int,
    month: int,
    bulk: Dict[str, int],
) -> None:
    """월 일괄값을 day=0 row 로 upsert.
    - bulk: {D_count, E_count, N_count, M_count, D_count_max, E_count_max, N_count_max, M_count_max, max_enabled}
    - max_enabled=False 시 *_count_max 는 0 으로 reset.
    - max_enabled=True 시 *_count_max < *_count 면 *_count 로 clamp.
    """
    bulk_max_enabled = bool(bulk.get("max_enabled", False))
    d_min = int(bulk.get("D_count", 0))
    e_min = int(bulk.get("E_count", 0))
    n_min = int(bulk.get("N_count", 0))
    m_min = int(bulk.get("M_count", 0))
    if not bulk_max_enabled:
        d_max = e_max = n_max = m_max = 0
    else:
        def _clamp(raw, mn: int) -> int:
            mx = int(raw or 0)
            if mx <= 0:
                return 0
            return mx if mx >= mn else mn
        d_max = _clamp(bulk.get("D_count_max", 0), d_min)
        e_max = _clamp(bulk.get("E_count_max", 0), e_min)
        n_max = _clamp(bulk.get("N_count_max", 0), n_min)
        m_max = _clamp(bulk.get("M_count_max", 0), m_min)
    row = (
        db.query(DailyShift)
        .filter(
            DailyShift.office_id == office_id,
            DailyShift.group_id == group_id,
            DailyShift.year == year,
            DailyShift.month == month,
            DailyShift.day == 0,
        )
        .first()
    )
    if row:
        row.d_count = d_min
        row.e_count = e_min
        row.n_count = n_min
        row.m_count = m_min
        row.d_count_max = d_max
        row.e_count_max = e_max
        row.n_count_max = n_max
        row.m_count_max = m_max
        row.max_enabled = bulk_max_enabled
    else:
        db.add(DailyShift(
            office_id=office_id,
            group_id=group_id,
            year=year,
            month=month,
            day=0,
            d_count=d_min,
            e_count=e_min,
            n_count=n_min,
            m_count=m_min,
            d_count_max=d_max,
            e_count_max=e_max,
            n_count_max=n_max,
            m_count_max=m_max,
            max_enabled=bulk_max_enabled,
        ))


def _sync_shift_manage_template(
    db: Session,
    office_id: str,
    group_id: str,
    day_cnt: int,
    eve_cnt: int,
    nig_cnt: int,
    mid_cnt: int,
) -> None:
    """shift_manage 의 RN 슬롯(1:D, 2:E, 3:N, 5:M)을 주어진 값으로 동기화.
    - 인자: 4개 슬롯에 적용할 manpower 스칼라값
    - 반환 없음 (commit 은 호출자 책임)
    """
    for slot, value in [(1, day_cnt), (2, eve_cnt), (3, nig_cnt), (5, mid_cnt)]:
        db.query(ShiftManage).filter(
            ShiftManage.office_id == office_id,
            ShiftManage.group_id == group_id,
            ShiftManage.nurse_class == 'RN',
            ShiftManage.shift_slot == slot,
        ).update({ShiftManage.manpower: int(value)})


def update_daily(
    db: Session,
    office_id: str,
    group_id: str,
    year: int,
    month: int,
    d_list: List[int],
    e_list: List[int],
    n_list: List[int],
    m_list: List[int],
    d_max_list: List[int] | None = None,
    e_max_list: List[int] | None = None,
    n_max_list: List[int] | None = None,
    m_max_list: List[int] | None = None,
    max_enabled: bool = False,
) -> Dict:
    """일자별 배열을 그대로 저장합니다.
    - 리스트 길이는 해당 월의 일수와 같아야 합니다.
    - max_enabled=False 이면 *_max 입력 무시하고 0 으로 reset (상한 미사용).
    - max_enabled=True 이면 *_max 리스트 사용; 빈 리스트면 0 채움; max < min 은 min 으로 clamp.
    - 예시: D=[1,3,3], E=[2,2,3], N=[2,2,2], D_max=[3,3,3], max_enabled=True
    """
    days = _get_month_days(year, month)
    if not (len(d_list) == len(e_list) == len(n_list) == len(m_list) == days):
        raise ValueError("리스트 길이가 월의 일수와 다릅니다.")
    if not max_enabled:
        d_max = e_max = n_max = m_max = [0] * days
    else:
        d_max = _normalize_max_list(d_max_list or [], d_list, days)
        e_max = _normalize_max_list(e_max_list or [], e_list, days)
        n_max = _normalize_max_list(n_max_list or [], n_list, days)
        m_max = _normalize_max_list(m_max_list or [], m_list, days)

    # 기존 행 조회
    rows = (
        db.query(DailyShift)
        .filter(
            DailyShift.office_id == office_id,
            DailyShift.group_id == group_id,
            DailyShift.year == year,
            DailyShift.month == month,
        )
        .all()
    )
    rows_map: Dict[int, DailyShift] = {}
    for r in rows:
        rows_map[int(getattr(r, "day", 0) or 0)] = r

    # upsert
    for idx in range(days):
        day_no = idx + 1
        if day_no in rows_map:
            r = rows_map[day_no]
            r.d_count = int(d_list[idx])
            r.e_count = int(e_list[idx])
            r.n_count = int(n_list[idx])
            r.m_count = int(m_list[idx])
            r.d_count_max = d_max[idx]
            r.e_count_max = e_max[idx]
            r.n_count_max = n_max[idx]
            r.m_count_max = m_max[idx]
            r.max_enabled = max_enabled
        else:
            db.add(
                DailyShift(
                    office_id=office_id,
                    group_id=group_id,
                    year=year,
                    month=month,
                    day=day_no,
                    d_count=int(d_list[idx]),
                    e_count=int(e_list[idx]),
                    n_count=int(n_list[idx]),
                    m_count=int(m_list[idx]),
                    d_count_max=d_max[idx],
                    e_count_max=e_max[idx],
                    n_count_max=n_max[idx],
                    m_count_max=m_max[idx],
                    max_enabled=max_enabled,
                )
            )
    db.commit()
    return {"updated": True, "days_affected": days}


def replace_month_data(
    db: Session,
    office_id: str,
    group_id: str,
    year: int,
    month: int,
    d_list: List[int],
    e_list: List[int],
    n_list: List[int],
    m_list: List[int],
    d_max_list: List[int] | None = None,
    e_max_list: List[int] | None = None,
    n_max_list: List[int] | None = None,
    m_max_list: List[int] | None = None,
    max_enabled: bool = False,
    apply_globally: bool = False,
    bulk: Dict[str, int] | None = None,
) -> Dict:
    """월 데이터를 일자별 배열로 일괄 교체합니다 (PUT /daily-shift 진입점).
    - 일자별 upsert 는 update_daily 에 위임 (day=1..말일).
    - bulk 가 주어지면 day=0 row 로 월 일괄값 upsert.
    - apply_globally=True 면 day=1 값을 기준으로 shift_manage 템플릿(RN 1/2/3/5) 동기화.
    - 반환: GET /daily-shift 와 동일 형태(저장 직후 재조회).
    """
    update_daily(
        db,
        office_id=office_id,
        group_id=group_id,
        year=year,
        month=month,
        d_list=d_list,
        e_list=e_list,
        n_list=n_list,
        m_list=m_list,
        d_max_list=d_max_list,
        e_max_list=e_max_list,
        n_max_list=n_max_list,
        m_max_list=m_max_list,
        max_enabled=max_enabled,
    )
    if bulk is not None:
        _upsert_day_zero(db, office_id, group_id, year, month, bulk)
        db.commit()
    if apply_globally and d_list:
        _sync_shift_manage_template(
            db,
            office_id,
            group_id,
            int(d_list[0]),
            int(e_list[0]),
            int(n_list[0]),
            int(m_list[0]) if m_list else 0,
        )
        db.commit()
    return get_or_init_month(db, office_id, group_id, year, month)


def apply_bulk_to_days(
    db: Session,
    office_id: str,
    group_id: str,
    year: int,
    month: int,
    bulk: Dict[str, int] | None = None,
    apply_globally: bool = False,
) -> Dict:
    """day=0 row 값을 day=1..말일 모든 row 에 fan-out (일괄적용).
    - bulk 가 주어지면 day=0 row 를 먼저 그 값으로 upsert 후 fan-out.
    - bulk 생략 시 기존 day=0 row 를 사용. 없으면 ValueError.
    - day=0 row 의 *_count / *_count_max / max_enabled 가 그대로 day=1..말일 에 전파됨.
    - apply_globally=True 면 day=0 값으로 shift_manage 템플릿(RN 1/2/3/5) 동기화.
    """
    if bulk is not None:
        _upsert_day_zero(db, office_id, group_id, year, month, bulk)
        db.commit()
    bulk_row = (
        db.query(DailyShift)
        .filter(
            DailyShift.office_id == office_id,
            DailyShift.group_id == group_id,
            DailyShift.year == year,
            DailyShift.month == month,
            DailyShift.day == 0,
        )
        .first()
    )
    if bulk_row is None:
        raise ValueError("day=0 row 가 없어 일괄적용을 수행할 수 없습니다. month_summary 를 함께 보내거나 먼저 등록하세요.")
    days = _get_month_days(year, month)
    d = int(bulk_row.d_count or 0)
    e = int(bulk_row.e_count or 0)
    n = int(bulk_row.n_count or 0)
    m = int(getattr(bulk_row, "m_count", 0) or 0)
    d_max = int(getattr(bulk_row, "d_count_max", 0) or 0)
    e_max = int(getattr(bulk_row, "e_count_max", 0) or 0)
    n_max = int(getattr(bulk_row, "n_count_max", 0) or 0)
    m_max = int(getattr(bulk_row, "m_count_max", 0) or 0)
    bulk_max_enabled = bool(getattr(bulk_row, "max_enabled", False))
    update_daily(
        db,
        office_id=office_id,
        group_id=group_id,
        year=year,
        month=month,
        d_list=[d] * days,
        e_list=[e] * days,
        n_list=[n] * days,
        m_list=[m] * days,
        d_max_list=[d_max] * days,
        e_max_list=[e_max] * days,
        n_max_list=[n_max] * days,
        m_max_list=[m_max] * days,
        max_enabled=bulk_max_enabled,
    )
    if apply_globally:
        _sync_shift_manage_template(db, office_id, group_id, d, e, n, m)
        db.commit()
    return get_or_init_month(db, office_id, group_id, year, month)


# Daily-Shift 작업
def is_month_closed(db: Session, office_id: str, group_id: str, year: int, month: int) -> bool:
    """Wanted 테이블을 기준으로 월이 마감되었는지 확인합니다.
    - 기준 날짜: 시스템 현재 날짜 (datetime.now())
    - 마감 조건:
      - status가 'closed'면 무조건 마감.
      - status가 'requested'이고 exp_date가 현재 날짜 이전이면 마감.
      - exp_date가 None이거나 현재 날짜 이후면 미마감.
    - 반환: bool (True=마감, False=미마감)
    """
    current_date = datetime.now()  # 시스템 현재 날짜로 동적 설정
    wanted = (
        db.query(Wanted)
        .filter(
            Wanted.group_id == group_id,
            Wanted.year == year,
            Wanted.month == month
        )
        .first()
    )
    if not wanted:
        return False  # Wanted 데이터가 없으면 미마감으로 간주
    if wanted.status == "closed":
        return True
    if wanted.exp_date and wanted.exp_date < current_date:
        return True
    return False


def format_shift_calendar(
    office_id: str,
    group_id: str,
    year: int,
    month: int,
    rows: List[DailyShift],
    db: Session,  # db 파라미터 추가
) -> Dict:
    """DailyShift 데이터를 캘린더 응답 포맷으로 변환합니다.
    - month_summary는 1일차 값을 사용합니다.
    - editable 필드 추가, 마감된 월은 editable=False.
    """
    rows_sorted = sorted(rows, key=lambda r: r.day)
    first_day = rows_sorted[0] if rows_sorted else None
    max_enabled = bool(getattr(first_day, "max_enabled", False)) if first_day else False
    month_summary = {
        "D_count": first_day.d_count if first_day else 0,
        "E_count": first_day.e_count if first_day else 0,
        "N_count": first_day.n_count if first_day else 0,
        "M_count": int(getattr(first_day, "m_count", 0) or 0) if first_day else 0,
        "D_count_max": int(getattr(first_day, "d_count_max", 0) or 0) if first_day else 0,
        "E_count_max": int(getattr(first_day, "e_count_max", 0) or 0) if first_day else 0,
        "N_count_max": int(getattr(first_day, "n_count_max", 0) or 0) if first_day else 0,
        "M_count_max": int(getattr(first_day, "m_count_max", 0) or 0) if first_day else 0,
        "max_enabled": max_enabled,
    }
    daily_shifts = [
        {
            "day": r.day,
            "d_count": r.d_count,
            "e_count": r.e_count,
            "n_count": r.n_count,
            "m_count": int(getattr(r, "m_count", 0) or 0),
            "d_count_max": int(getattr(r, "d_count_max", 0) or 0),
            "e_count_max": int(getattr(r, "e_count_max", 0) or 0),
            "n_count_max": int(getattr(r, "n_count_max", 0) or 0),
            "m_count_max": int(getattr(r, "m_count_max", 0) or 0),
            "max_enabled": bool(getattr(r, "max_enabled", False)),
            "editable": not is_month_closed(db, office_id, group_id, year, month)  # 정확한 파라미터 전달
        }
        for r in rows_sorted
    ]
    return {
        "office_id": office_id,
        "group_id": group_id,
        "year": year,
        "month": month,
        "month_summary": month_summary,
        "daily_shifts": daily_shifts
    }


def manage_month_data(db: Session, office_id: str, group_id: str, year: int, month: int, initialize: bool = False) -> Dict:
    """월 단위 데이터를 조회하거나 초기화합니다.
    - Wanted 테이블을 기준으로 마감된 데이터(현재 날짜 이전 또는 closed)와 미마감 최소값 월 데이터만 포함.
    - initialize=True면 shift_manage 기반으로 초기화.
    - 기준 날짜: 시스템 현재 날짜 (datetime.now())
    - 반환: {years: {year: {month: {...}}}}
    """
    current_date = datetime.now()  # 시스템 현재 날짜로 동적 설정

    # 모든 연도 및 월 데이터 조회
    all_years = (
        db.query(DailyShift.year)
        .filter(
            DailyShift.office_id == office_id,
            DailyShift.group_id == group_id
        )
        .distinct()
        .all()
    )

    result = {
        "office_id": office_id,
        "group_id": group_id,
        "years": {}
    }

    # 미마감 최소값 연월 추적 (초기값은 None으로 설정)
    earliest_unclosed_year = None
    earliest_unclosed_month = None

    for y in [row[0] for row in all_years]:
        result["years"][y] = {}  # 초기화 추가
        all_months = (
            db.query(DailyShift.month)
            .filter(
                DailyShift.office_id == office_id,
                DailyShift.group_id == group_id,
                DailyShift.year == y
            )
            .group_by(DailyShift.month)
            .all()
        )
        for m in [row[0] for row in all_months]:
            rows = (
                db.query(DailyShift)
                .filter(
                    DailyShift.office_id == office_id,
                    DailyShift.group_id == group_id,
                    DailyShift.year == y,
                    DailyShift.month == m
                )
                .all()
            )

            if not rows and initialize and y == year and m == month:
                print(f"[daily_shift] {y}/{m} 데이터 없음 → shift_manage 기반으로 초기화")
                days = _get_month_days(y, m)
                d, e, n, mid_cnt = _read_template_from_shift_manage(db, office_id, group_id)
                for day_idx in range(1, days + 1):
                    db.add(
                        DailyShift(
                            office_id=office_id,
                            group_id=group_id,
                            year=y,
                            month=m,
                            day=day_idx,
                            d_count=d,
                            e_count=e,
                            n_count=n,
                            m_count=mid_cnt,
                        )
                    )
                db.commit()
                rows = (
                    db.query(DailyShift)
                    .filter(
                        DailyShift.office_id == office_id,
                        DailyShift.group_id == group_id,
                        DailyShift.year == y,
                        DailyShift.month == m
                    )
                    .all()
                )

            if rows:
                if is_month_closed(db, office_id, group_id, y, m):
                    result["years"][y][m] = format_shift_calendar(office_id, group_id, y, m, rows, db)  # 마감된 데이터 포함
                else:
                    # 미마감 월 중 최소값 추적
                    if (
                        earliest_unclosed_year is None
                        or earliest_unclosed_month is None
                        or (y < earliest_unclosed_year)
                        or (y == earliest_unclosed_year and m < earliest_unclosed_month)
                    ):
                        earliest_unclosed_year = y
                        earliest_unclosed_month = m
                    if y == earliest_unclosed_year and m == earliest_unclosed_month:
                        result["years"][y][m] = format_shift_calendar(office_id, group_id, y, m, rows, db)  # 최소 미마감 월 포함

    return result
