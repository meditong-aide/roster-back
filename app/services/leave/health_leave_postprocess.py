"""보건휴가 후처리 — 대상자의 OFF(고정근무자는 근무일) 하나를 휴가코드로 바꾼다.

★★ 왜 후처리인가
  날짜를 미리 찍어 고정셀로 주입하면 보건휴가가 근무일 사이에 단독으로 박힌다.
  실무는 반대다 — 엑셀 11개·보건 셀 238건 실측:
  ```
  OFF 에 인접        206건  86.6%
  근무일 사이에 단독     16건   6.7%
  ```
  배치를 솔버에 맡기고, 솔버가 놓은 OFF 중 하나를 여기서 바꾼다.

★★ OFF 총량이 유지되는 구조 (2026-08-04 실측으로 확정)
  대상자는 생성 시 `extra_min_off=1` 로 하한이 11이 되고, **상한도 하한으로 고정**된다
  (fallback_lex). 상한을 같이 고정하지 않으면 `_extra_off_fb`(사람마다 0~2)가 얹혀
  11~13 으로 벌어지고 후처리 후 9~12 로 분산된다 — 실제로 그렇게 무너졌다.
  바뀐 칸은 `vacation_types` 라 countable_off 에서 빠지므로 결과는 OFF 10 + 보건 1 이다.

★ 커버리지 검사가 없다 — OFF 자리의 코드만 바꾸므로 그날 근무 인원이 변하지 않는다.
  (수면OFF 후처리는 근무일을 치환하므로 daily_shift 검사가 필요하다. 성격이 다르다.)

설계: docs/leave_auto_assignment_design.md §6 Step2
"""
from __future__ import annotations

import calendar
import logging
import zlib
from datetime import date
from typing import Optional

from sqlalchemy.orm import Session

from db.models import RosterConfig
from services.leave.health_leave_planner import resolve_health_leave_shift

logger = logging.getLogger(__name__)

# 근무가 아닌 '쉬는' 코드. 주휴(주)는 요일 고정이라 건드리지 않는다.
_OFF_CODES = {"O", "OFF"}


def _pick_off_day(codes: list[str], days_in_month: int, year: int, month: int,
                  nurse_id: str, allow_weekend: bool,
                  fixed_wanted: Optional[set[tuple[str, int]]] = None) -> Optional[int]:
    """치환할 OFF 하루(1-based). 없으면 None.

    ★ 결정론적 분산 — `crc32(nurse_id)`. `hash()` 는 PYTHONHASHSEED 에 따라 실행마다
      달라져 같은 입력이 다른 근무표를 내므로 쓰지 않는다.
    ★ **확정 원티드로 굳힌 OFF 는 후보에서 뺀다.** 간호사가 날짜를 지정해 받은 휴일인데
      보건휴가로 덮으면 그 지정이 사라진다(총 휴일 수는 같아도 의미가 다르다).
      off_swap 이 같은 이유로 fixed_wanted 를 보호하며, 판정도 그쪽 헬퍼를 재사용한다.
      실측(2026-08-31 인천의료원 중환자실 2026-05): 4명이 신청한 OFF 를 이렇게 잃었다.
    """
    fw = fixed_wanted or set()
    nid = str(nurse_id)
    cand = [
        i + 1 for i in range(min(days_in_month, len(codes)))
        if str(codes[i] or "").strip() in _OFF_CODES
        and (allow_weekend or date(year, month, i + 1).weekday() < 5)
        and (nid, i + 1) not in fw
    ]
    if not cand:
        return None
    return cand[zlib.crc32(nid.encode()) % len(cand)]


def _pick_fixed_work_day(codes: list[str], days_in_month: int,
                         nurse_id: str) -> Optional[int]:
    """고정근무자용 — 치환할 **근무일**(1-based). 없으면 None.

    ★ 고정근무자는 솔버를 우회해 셀이 채워지므로 OFF 하한 경로를 타지 않는다.
      OFF 를 바꾸면 쉬는 날이 줄 뿐이라 실무처럼 근무일을 대체한다
      (중환자실 이주희: M 16일 중 하루가 보건 — 앞 8·9일이 OFF 라 연휴가 된다).
    ★ OFF 에 인접한 근무일을 우선한다.
    """
    n = min(days_in_month, len(codes))
    off_set = {i for i in range(n) if str(codes[i] or "").strip() in _OFF_CODES}
    work = [i for i in range(n)
            if str(codes[i] or "").strip() and i not in off_set]
    if not work:
        return None
    adj = [i for i in work if (i - 1) in off_set or (i + 1) in off_set]
    pool = adj or work
    return pool[zlib.crc32(str(nurse_id).encode()) % len(pool)] + 1


def postprocess_health_leave(db: Session, schedule, generated: dict,
                             target_ids: set[str],
                             latest_config: Optional[RosterConfig] = None,
                             fixed_ids: Optional[set[str]] = None,
                             stats: Optional[dict] = None) -> dict:
    """`generated` 를 in-place 수정 후 반환. 대상자가 없으면 아무것도 하지 않는다.

    Args:
        fixed_ids: 고정근무자 id. 이들은 OFF 가 아니라 근무일을 치환한다.
    """
    if not target_ids:
        return generated
    gid = str(schedule.group_id)
    cfg = latest_config
    if cfg is None or str(getattr(cfg, "group_id", "")) != gid:
        cfg = (db.query(RosterConfig).filter(RosterConfig.group_id == gid)
                 .order_by(RosterConfig.created_at.desc()).first())
    if not bool(getattr(cfg, "health_leave_enabled", False)):
        return generated
    target = resolve_health_leave_shift(db, gid)
    if target is None:
        return generated

    allow_weekend = bool(getattr(cfg, "health_leave_weekend", False))
    year, month = int(schedule.year), int(schedule.month)
    dim = calendar.monthrange(year, month)[1]
    fixed = {str(x) for x in (fixed_ids or set())}
    # 확정 원티드 OFF 보호 — off_swap 과 같은 헬퍼를 쓴다(판정이 갈리면 안 된다).
    # ★ `fixed_wanted_use_yn` 은 **보지 않는다.** 이 보호는 솔버 제약의 연장이 아니라
    #   "사용자가 날짜를 지정해 받은 휴일을 다른 코드로 덮지 않는다" 는 것이고,
    #   그 사실은 하드 고정 여부와 무관하다. off_swap 도 같은 이유로 이 플래그를 안 본다.
    #   과잉 보호도 아니다 — 후보는 애초에 생성 결과가 OFF 인 날뿐이라,
    #   선호도로만 반영돼 OFF 가 아니게 된 날은 여기 오지 않는다.
    try:
        from services.cp_sat.off_swap import _load_fixed_wanted_set
        fixed_wanted = _load_fixed_wanted_set(db, gid, year, month)
        # ★ 그 함수는 **모든** 확정 원티드 날짜를 준다. 여기서 보호할 것은 "OFF 를 신청한 날"
        #   뿐이다 — N 을 신청했는데 결과가 OFF 인 날까지 빼면 배치할 자리가 부질없이 준다.
        #   대표코드 정규화는 솔버와 같게(OFF·주 → O). 공용 함수는 off_swap 이 쓰므로
        #   그쪽 동작이 바뀌지 않도록 **여기서만** 걸러낸다.
        #   ★★ 요청 코드 판정에는 **`주` 도 포함**한다. `_OFF_CODES`(후보 판정)와 집합이
        #      다른 이유 — 후보는 **생성 결과** 코드를 보고, 보호는 **요청** 코드를 본다.
        #      솔버가 `OFF`·`주` 를 `O` 로 정규화하므로(`_normalize_fixed_to_main`)
        #      주휴로 신청한 셀도 결과는 `O` 가 되어 후보에 오른다. 여기서 `주` 를 빼면
        #      그 신청이 보건휴가로 덮인다.
        from db.models import FixedWantedEntry, Shift
        _OFF_REQ_CODES = _OFF_CODES | {"주"}
        _off_req = {
            str(s.shift_id) for s in
            db.query(Shift).filter(Shift.group_id == gid).all()
            if str(s.default_shift or "").strip().upper() in _OFF_REQ_CODES
        }
        _req_code = {
            (str(e.nurse_id), e.shift_date.day): str(e.shift_id)
            for e in db.query(FixedWantedEntry).filter(
                FixedWantedEntry.group_id == gid,
                FixedWantedEntry.year == year,
                FixedWantedEntry.month == month,
                FixedWantedEntry.is_applied == True,  # noqa: E712
            ).all() if e.shift_date
        }
        fixed_wanted = {k for k in fixed_wanted if _req_code.get(k) in _off_req}
    except Exception as _fw_exc:      # 보호 실패가 배치 자체를 막지는 않게
        print(f"[HealthLeave][WARN] 확정 원티드 로드 실패 — 보호 없이 진행: {_fw_exc}")
        fixed_wanted = set()

    placed = placed_fixed = skipped = 0
    for nid in target_ids:
        codes = generated.get(str(nid))
        if not codes:
            continue
        if target.shift_id in codes:        # 원티드 등으로 이미 있으면 건드리지 않는다
            continue
        if str(nid) in fixed:
            day = _pick_fixed_work_day(codes, dim, str(nid))
            if day is not None:
                placed_fixed += 1
        else:
            day = _pick_off_day(codes, dim, year, month, str(nid), allow_weekend,
                                fixed_wanted)
        if day is None:
            skipped += 1
            continue
        codes[day - 1] = target.shift_id
        placed += 1

    if placed or skipped:
        print(f"[HealthLeave] {gid} {year}-{month:02d} 배치 {placed}건"
              f"(고정근무 {placed_fixed}) · 스킵 {skipped}건 "
              f"(code={target.shift_id} 주말={allow_weekend})")
    if skipped:
        logger.warning(
            "[HealthLeave] group=%s %d-%02d 대상 %d명 중 %d명은 바꿀 칸이 없다 "
            "— extra_min_off 가 반영되지 않았거나 주말 제한으로 후보가 비었다.",
            gid, year, month, len(target_ids), skipped,
        )
    # 화면이 생성 결과를 검토할 수 있게 수치를 넘긴다. 로그로만 남기면 운영자가
    # 몇 명에게 줬는지, 못 준 사람이 있는지 알 방법이 없다.
    if stats is not None:
        stats.update({
            "code": target.shift_id,
            "target_count": len(target_ids),
            "placed": placed,
            "placed_fixed": placed_fixed,
            "skipped": skipped,
            "weekend_allowed": allow_weekend,
        })
    return generated
