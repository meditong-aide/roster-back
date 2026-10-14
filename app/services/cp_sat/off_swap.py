"""초과 OFF → 연차 코드 변환 후처리.

`roster_config.off_swap_enabled=True` 일 때 동작.
월 OFF 수 baseline(`off_days`) 초과분을 `shifts.off_swap_target=True` 로 등록된
코드로 변환한다. 보호 4종(회복 OFF / fixed_wanted / '주' / N전담)은 후보에서 제외.
주2OFF(`two_offs_per_week`)를 켠 병동은 주별 휴일 하한을 깨야 하는 몫은 바꾸지 않고
O 로 남긴다(`_pick_keeping_week_off`) — 그 병동은 초과분이 전부 바뀌지 않을 수 있다.

호출 위치: `roster_create_service._persist_entries` 직전.
"""

from __future__ import annotations

import logging
from datetime import date
from typing import Iterable

from dateutil.relativedelta import relativedelta
from sqlalchemy import and_, or_
from sqlalchemy.orm import Session

from db.models import FixedWantedEntry, Nurse, Schedule, ScheduleEntry, Shift
from services.cp_sat.allowed_shift_types import is_n_only_profile

logger = logging.getLogger(__name__)


def postprocess_off_swap(
    db: Session,
    schedule,
    generated: dict,
    latest_config,
    req,
) -> dict:
    """초과 OFF 를 연차 코드로 변환.

    Args:
        db: SQLAlchemy 세션
        schedule: 저장 대상 Schedule (group_id/year/month/office_id 사용)
        generated: {nurse_id: [shift_code_per_day, ...]} — 1일=index 0
        latest_config: RosterConfig 인스턴스 (off_swap_enabled / off_days / use_mid)
        req: RosterRequest (year, month)

    Returns:
        변환된 generated dict (in-place mutation 후 반환).
    """
    print(
        f"[OffSwap][ENTER] schedule={schedule.schedule_id} "
        f"off_swap_enabled={getattr(latest_config, 'off_swap_enabled', None)!r}"
    )
    if not bool(getattr(latest_config, "off_swap_enabled", False)):
        print("[OffSwap][SKIP] off_swap_enabled=False — early return")
        return generated

    target = _resolve_target_shift(db, schedule.group_id)
    print(
        f"[OffSwap] target_shift={target.shift_id if target else None} "
        f"target_id={target.id if target else None} target_type={getattr(target, 'type', None) if target else None}"
    )
    if target is None:
        print("[OffSwap][SKIP] target shift 없음 (off_swap_target=True 인 shifts row 미존재)")
        return generated
    # 안전망: target shift 의 type 이 '근무' 면 OFF→근무 변환이 일별 coverage oversupply 를
    # 유발하므로 변환 자체를 SKIP. 정상적으로는 _assert_off_swap_target_valid 로 저장
    # 단계에서 차단되지만, 기존 dirty 데이터 보호용.
    if str(getattr(target, "type", "") or "").strip() == "근무":
        logger.warning(
            f"[OffSwap][SKIP] target shift={target.shift_id} type='근무' — OFF→근무 치환 시 "
            f"oversupply 위험으로 후처리 스킵. 운영자가 다른 휴가성 shift 로 변경 필요."
        )
        print(
            f"[OffSwap][SKIP] target shift={target.shift_id} type='근무' — oversupply 위험"
        )
        return generated

    baseline = int(getattr(latest_config, "off_days", 0) or 0)
    print(f"[OffSwap] baseline off_days={baseline}")
    if baseline <= 0:
        print(f"[OffSwap][SKIP] baseline={baseline} <= 0")
        return generated

    n_codes, o_only_codes, weekly_off_codes = _resolve_shift_code_sets(
        db, schedule.group_id
    )
    print(
        f"[OffSwap] n_codes={n_codes} o_only_codes={o_only_codes} "
        f"weekly_off_codes={weekly_off_codes}"
    )
    if not o_only_codes:
        print("[OffSwap][SKIP] OFF 코드 없음 (default_shift='O' 인 shift 미존재)")
        return generated
    # 솔버의 baseline 비교는 'O'+'주' 합산 기준 (cap_semantics=nonvac_total_off).
    # '주' 셀은 변환 안 하지만 excess 계산엔 반드시 포함시켜야 한다.
    all_off_codes = o_only_codes | weekly_off_codes

    fixed_set = _load_fixed_wanted_set(db, schedule.group_id, req.year, req.month)
    nurses = _load_nurses(db, schedule.group_id)
    # 타병동 전입(inbound) 간호사는 nurses.group_id 가 source 병동이라 위 group 조회에
    #   안 잡혀 nu=None 으로 연차 변환에서 통째로 스킵되던 버그. generated(=이 근무표의
    #   실제 배정 대상)에 있는데 group 조회에 누락된 nurse_id 를 group 무관하게 보충 로드.
    _missing_nids = [str(_nid) for _nid in generated.keys() if str(_nid) not in nurses]
    if _missing_nids:
        for _n in db.query(Nurse).filter(Nurse.nurse_id.in_(_missing_nids)).all():
            nurses[str(_n.nurse_id)] = _n
        print(f"[OffSwap] inbound 전입 간호사 보충 로드: {_missing_nids}")
    use_mid = bool(getattr(latest_config, "use_mid", False))
    # ★ 주별 O 하한을 고려한 선택은 병동이 **주2OFF 를 켠 경우에만** 쓴다. 안 켠 병동은
    #   지킬 하한이 없으므로 기존 순서(월말부터) 초과분을 전부 바꾼다. 켠 병동은 하한을
    #   깨야 하는 몫을 바꾸지 않고 O 로 남긴다(`_pick_keeping_week_off`).
    #   DB 컬럼명은 `two_offs_per_week` 다(엔진 dataclass 의 `enforce_two_offs_per_week`
    #   와 이름이 다르다). NULL(미설정)은 False 로 떨어진다.
    two_offs_on = bool(getattr(latest_config, "two_offs_per_week", False))

    converted_total = 0
    skipped_n_only = 0
    kept_for_floor = 0  # 주별 O 하한 때문에 연차로 못 바꾸고 O 로 남긴 수
    print(f"[OffSwap] generated nurses={len(generated)}")

    for nurse_id, shifts_seq in generated.items():
        nu = nurses.get(str(nurse_id))
        if nu is None:
            continue
        if is_n_only_profile(getattr(nu, "allowed_shifts", None), use_mid=use_mid):
            skipped_n_only += 1
            continue

        prev_tail = _load_prev_month_tail(
            db, str(nurse_id), schedule.year, schedule.month, n_days=3
        )
        # 회복 OFF 검사는 'O'+'주' 모두 OFF 로 인정해야 함.
        # (예: N N N 주 O → 주/O 둘 다 회복 OFF, 보호 대상)
        recovery_offs = _identify_recovery_offs(
            shifts_seq, n_codes, all_off_codes, prev_month_tail=prev_tail
        )

        # baseline 비교용 — 'O' + '주' 합산 (솔버와 동일 시맨틱)
        total_off_count = sum(
            1 for c in shifts_seq if str(c).strip().upper() in all_off_codes
        )
        excess = total_off_count - baseline
        if excess <= 0:
            continue

        # 변환 후보 — 'O' 만 (주 보호) + fixed_wanted/회복 OFF 제외
        eligible: list[int] = []
        for d_idx, code in enumerate(shifts_seq):
            day_num = d_idx + 1
            code_str = str(code).strip().upper()
            if code_str not in o_only_codes:
                continue
            if (str(nurse_id), day_num) in fixed_set:
                continue
            if d_idx in recovery_offs:
                continue
            eligible.append(d_idx)

        to_convert_count = min(excess, len(eligible))
        if to_convert_count <= 0:
            continue

        # ★★ 주2OFF 를 켠 병동은 주별 O 하한을 깨지 않는 칸만 바꾼다 — 모자란 몫은 O 로
        #   남는다(2026-10-14 사용자 결정 · 근거는 `_pick_keeping_week_off`).
        #   기존은 `sorted(reverse=True)` 로 **월말부터** 훑어 마지막 주 O 를 통째로
        #   연차로 바꿨다(실측 중환자실2 2026-07: 전환 33셀이 O<2 주를 10개 만들었다).
        pool = sorted(eligible, reverse=True)      # 기존 선택 순서(월말부터)
        picked: list[int] = []
        if two_offs_on:
            picked, kept = _pick_keeping_week_off(
                pool, shifts_seq, all_off_codes, to_convert_count
            )
            kept_for_floor += kept
        else:
            picked = pool[:to_convert_count]

        for d_idx in picked:
            shifts_seq[d_idx] = target.shift_id
            converted_total += 1

    print(
        f"[OffSwap][DONE] schedule={schedule.schedule_id} converted={converted_total} "
        f"baseline={baseline} target_shift={target.shift_id} "
        f"skipped_n_only={skipped_n_only} "
        f"kept_off_for_week_floor={kept_for_floor} (two_offs_per_week={two_offs_on})"
    )
    return generated


# ─────────────────────────── 내부 헬퍼 ───────────────────────────

def _pick_keeping_week_off(
    pool: list[int], shifts_seq: list, off_codes: set[str],
    need: int, keep: int = 2,
) -> tuple[list[int], int]:
    """`pool` 에서 최대 `need` 개를 고르되, 주별 휴일(`off_codes`) 하한을 **깨지 않는** 칸만 고른다.

    ★★ 하한을 깨야만 채울 수 있는 몫은 **바꾸지 않고 O 로 남긴다**(2026-10-14 사용자 결정).
      종전엔 개수를 반드시 채우려고 여유가 적은 주부터 몰아서 깼다. 그래서 주2OFF 를 켠
      병동에서도 전환이 하한을 깼다(성남 61-RN 2026-11 재현: 전환 전 O<2 주 2 → 후 8).
      연차는 근무일 대신 쓰는 것이라, O 가 2개뿐인 주의 O 를 바꾸면 쉬는 날에 연차를
      쓰는 셈이다. 대가로 남긴 사람은 O 가 baseline 을 넘는다(같은 재현 9칸·6명).
    ★ 하한은 온전한 주 `keep`, 월말 자투리는 일수 비례(`round(keep*일수/7)` → 2~3일 1 ·
      1일 0). 자투리(최대 3일)는 다음 달로 이어지는 주의 일부라 솔버도 안 본다
      (`fallback_lex` `weeks = D // 7`). 2 를 지키게 하면 자투리 이틀이 통째로 묶여
      남기는 칸만 는다(같은 재현 11 → 9).
    순서는 `pool` 그대로(월말부터) 가져간다. `pool` 은 호출부가 O 만 넣는다.

    Returns:
        (고른 일 인덱스 목록, 하한 때문에 못 바꾸고 남긴 수)
    """
    remain = _week_off_budget(shifts_seq, off_codes, keep=0)   # 주별 휴일 원본 개수
    full_weeks = len(shifts_seq) // 7
    tail_floor = round(keep * (len(shifts_seq) - full_weeks * 7) / 7)
    picked: list[int] = []
    for d_idx in pool:
        if len(picked) >= need:
            break
        w = d_idx // 7
        floor = keep if w < full_weeks else tail_floor
        if remain.get(w, 0) > floor:
            remain[w] -= 1
            picked.append(d_idx)
    return picked, need - len(picked)


def _week_off_budget(shifts_seq: list, off_codes: set[str],
                     keep: int = 2) -> dict[int, int]:
    """주별로 연차 전환을 허용할 휴일 개수 = `max(0, 그 주 휴일 수 - keep)`.
    `keep=0` 이면 그 주 휴일 원본 개수를 그대로 준다.

    ★ 주 경계는 솔버의 `enforce_two_offs_per_week` 와 **같은 7일 블록**이다
      (`fallback_lex`: `d0, d1 = w*7, min(w*7+7, D)` → `d_idx // 7`).
      달력 주(월~일)로 잡으면 보호하는 대상과 제약이 세는 대상이 어긋난다.
    ★ 다만 솔버는 `weeks = D // 7` 이라 **월말 자투리(최대 3일)를 안 본다.**
      자투리도 블록 하나로 세어 돌려준다 — off_swap 이 `sorted(reverse=True)` 로
      **월말부터** 가져가므로 자투리를 풀어 두면 통째로 연차가 된다. 자투리 하한은
      호출부(`_pick_keeping_week_off`)가 따로 정한다.
    ★ `주`(weekly_off)도 휴일로 **센다**(호출부가 O+주 를 넘긴다). 솔버는 '주' 를 O 로
      바꿔 넣고(`fallback_lex._normalize_fixed_to_main` · 주휴 고정칸 `shift='O'`)
      주2OFF 도 O 로 센다. 종전엔 O 만 셌는데, 하한을 깨는 몫을 남기게 된 뒤로는 이 차이가
      남기는 수를 늘려 '주+O+O' 주를 휴일 2개로 잘못 보았다(2026-10-14 리뷰).
    ★ 확정 원티드로 굳은 O 는 **센다.** 그 주에 실제로 있는 휴일이라 하한을 채운다
      (전환 후보에서 빠지는 것과, 하한 계산에 세는 것은 별개다).
    """
    per: dict[int, int] = {}
    for d_idx, code in enumerate(shifts_seq):
        if str(code).strip().upper() in off_codes:
            per[d_idx // 7] = per.get(d_idx // 7, 0) + 1
    return {w: max(0, c - keep) for w, c in per.items()}

def _resolve_target_shift(db: Session, group_id: str) -> Shift | None:
    """그룹의 off_swap_target=True 인 shift. 다수면 sequence ASC 첫 번째 + warning."""
    rows = (
        db.query(Shift)
          .filter(Shift.group_id == group_id, Shift.off_swap_target == True)
          .order_by(Shift.sequence.asc())
          .all()
    )
    if not rows:
        return None
    if len(rows) > 1:
        logger.warning(
            f"[OffSwap] target shift 다수 ({len(rows)}개) — 프론트에서 단일 강제 필요. "
            f"sequence 첫 번째 사용: shift_id={rows[0].shift_id}"
        )
    return rows[0]


def _resolve_shift_code_sets(
    db: Session, group_id: str
) -> tuple[set[str], set[str], set[str]]:
    """그룹 shifts 에서 N / 'O' 전용 / '주' (weekly_off) 코드 셋 반환.

    - n_codes: default_shift='N' 인 shift_id (회복 OFF 식별용)
    - o_only_codes: default_shift='O' 인 shift_id (변환 후보)
    - weekly_off_codes: default_shift='주' 인 shift_id (보호, baseline 카운트엔 포함)
    """
    rows = db.query(Shift).filter(Shift.group_id == group_id).all()
    n_codes: set[str] = set()
    o_only_codes: set[str] = set()
    weekly_off_codes: set[str] = set()
    for s in rows:
        ds = str(getattr(s, "default_shift", "") or "").strip().upper()
        sid = str(s.shift_id).strip().upper()
        if ds == "N":
            n_codes.add(sid)
        elif ds == "O":
            o_only_codes.add(sid)
        elif ds == "주":
            weekly_off_codes.add(sid)
    return n_codes, o_only_codes, weekly_off_codes


def _load_fixed_wanted_set(
    db: Session, group_id: str, year: int, month: int
) -> set[tuple[str, int]]:
    """(nurse_id, day_num) tuple 셋."""
    rows = db.query(FixedWantedEntry).filter(
        FixedWantedEntry.group_id == group_id,
        FixedWantedEntry.year == year,
        FixedWantedEntry.month == month,
        FixedWantedEntry.is_applied == True,
    ).all()
    return {(str(e.nurse_id), e.shift_date.day) for e in rows if e.shift_date}


def _load_nurses(db: Session, group_id: str) -> dict[str, Nurse]:
    rows = db.query(Nurse).filter(Nurse.group_id == group_id, Nurse.active == 1).all()
    return {str(n.nurse_id): n for n in rows}


def _load_prev_month_tail(
    db: Session, nurse_id: str, year: int, month: int, n_days: int = 3
) -> list[str]:
    """전월 마지막 n_days 의 default_shift 코드. group 무관 (파견자 대응).

    - schedule_entries.id NULL 폴백: id 가 비어있는 legacy row 는 shift_id+group_id 로 조인
    - 같은 (nurse, date) 에 dropped=0 schedule 이 다수 공존하면
      version DESC → updated_at DESC 우선순위로 1건만 채용 (결정적 선택)
    - 누락된 날짜는 빈 문자열로 채워서 N-run 카운트 끊기
    """
    first_of_month = date(year, month, 1)
    end = first_of_month - relativedelta(days=1)
    start = end - relativedelta(days=n_days - 1)

    join_condition = or_(
        and_(ScheduleEntry.id.isnot(None), Shift.id == ScheduleEntry.id),
        and_(
            ScheduleEntry.id.is_(None),
            Shift.shift_id == ScheduleEntry.shift_id,
            Shift.group_id == Schedule.group_id,
        ),
    )

    rows = (
        db.query(ScheduleEntry.work_date, Shift.default_shift)
        .join(Schedule, Schedule.schedule_id == ScheduleEntry.schedule_id)
        .join(Shift, join_condition)
        .filter(
            Schedule.dropped == False,  # noqa: E712
            ScheduleEntry.nurse_id == nurse_id,
            ScheduleEntry.work_date >= start,
            ScheduleEntry.work_date <= end,
        )
        .order_by(
            ScheduleEntry.work_date.asc(),
            Schedule.version.desc(),
            Schedule.updated_at.desc(),
        )
        .all()
    )

    by_day: dict[date, str] = {}
    for work_date, default_shift in rows:
        wd = work_date.date() if hasattr(work_date, "date") else work_date
        # 같은 날짜 중복 row 는 가장 최신 version 만 채용
        if wd in by_day:
            continue
        by_day[wd] = str(default_shift or "").strip().upper()

    return [by_day.get(start + relativedelta(days=i), "") for i in range(n_days)]


def _identify_recovery_offs(
    shifts_seq: list,
    n_codes: set[str],
    off_codes: set[str],
    prev_month_tail: Iterable[str] | None = None,
) -> set[int]:
    """회복 OFF 인덱스 (이번 달 0-based day_index).

    G2/I1: cfg flag 무관 — 1N→1O, 2N→2O, 3N→2O 항상 보호.
    4N+ 는 HARD lock #3 위반이라 발생 불가.
    """
    tail = [str(c).strip().upper() for c in (prev_month_tail or [])]
    full = tail + [str(c).strip().upper() for c in shifts_seq]
    offset = len(tail)

    protected: set[int] = set()
    n_run = 0
    for i, code in enumerate(full):
        if code in n_codes:
            n_run += 1
            continue
        # n-run 종료 — i 가 첫 비-N 위치
        if n_run == 1:
            recover_count = 1
        elif n_run in (2, 3):
            recover_count = 2
        else:
            recover_count = 0

        for k in range(i, i + recover_count):
            if k >= len(full) or full[k] not in off_codes:
                break
            rel = k - offset
            if 0 <= rel < len(shifts_seq):
                protected.add(rel)
        n_run = 0

    return protected
