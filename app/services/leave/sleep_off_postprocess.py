"""수면OFF 자동 부여 후처리.

N 연번이 `sleep_off_cycle`(실측 15)에 도달한 간호사에게, **그 N 블록이 끝난 뒤
다음 N 블록이 시작되기 전** 구간의 근무일 하나를 수면OFF 코드로 치환한다.

★ 왜 사전 주입이 아니라 후처리인가
  보건휴가는 "월 1개·아무 평일"이라 생성 전에 날짜를 찍을 수 있다. 수면OFF 는
  **N 블록이 어디 생기는지가 솔버 결과에 달려 있어** 생성 전에 알 수 없다.

★ 왜 OFF 가 아니라 근무일을 치환하는가 (2026-08 실측)
```
수면 받음 57명   OFF 11.00 · 수면 1.00 · 쉼 12.00 · 근무 16.93
수면 없음 171명  OFF 11.24 · 수면 0.00 · 쉼 11.24 · 근무 17.33
```
  OFF 는 거의 같고 쉼이 늘었다 → 실무에서도 근무일을 대체한다.
  OFF 를 치환하면 countable_off 가 줄어 off_days 미달이 된다.

★ 근무일을 빼면 그날 커버리지가 1 줄어든다. `daily_shift` 의 필요 인원을 보고
  **여유가 있는 날만** 고른다. 못 고르면 그 사람만 스킵한다(전체 실패 금지).

설계: docs/leave_auto_assignment_design.md §6 Step5
"""
from __future__ import annotations

import logging
from typing import Optional

from sqlalchemy.orm import Session

from db.models import DailyShift, RosterConfig
from services.leave.night_cycle_service import (
    DEFAULT_CYCLE, fetch_anchor, has_anchor, prev_month_fallback, resolve_cycle,
    resolve_sleep_off_shift, _prev_ym,
)

logger = logging.getLogger(__name__)

_WORK_MAIN = {"D", "E", "N", "M"}
# 블록 끝 N 을 수면OFF 로 바꾸려면 최소 이 길이여야 한다. 3 미만이면 하드락 #7(1N 금지)
# 위반이 되므로 부여를 포기하고 이월한다. _pick_day ② 참조.
_MIN_BLOCK_FOR_TAIL_CUT = 3
# 근무코드 → daily_shift 의 (필요, 최대) 컬럼
_REQ_COLS = {"D": ("d_count", "d_count_max"), "E": ("e_count", "e_count_max"),
             "N": ("n_count", "n_count_max"), "M": ("m_count", None)}


def _load_requirements(db: Session, schedule) -> dict[int, dict[str, int]]:
    """{일(1-based): {'D': 필요, 'E': .., 'N': .., 'M': ..}}. 행이 없으면 빈 dict."""
    rows = (
        db.query(DailyShift)
        .filter(DailyShift.group_id == schedule.group_id,
                DailyShift.year == int(schedule.year),
                DailyShift.month == int(schedule.month))
        .all()
    )
    out: dict[int, dict[str, int]] = {}
    for r in rows:
        out[int(r.day)] = {
            "D": int(getattr(r, "d_count", 0) or 0),
            "E": int(getattr(r, "e_count", 0) or 0),
            "N": int(getattr(r, "n_count", 0) or 0),
            "M": int(getattr(r, "m_count", 0) or 0),
        }
    return out


def _n_blocks(codes: list[str]) -> list[tuple[int, int]]:
    """연속 N 블록의 (시작일, 종료일) 목록 — 1-based."""
    blocks, start = [], None
    for i, c in enumerate(codes):
        if str(c or "").strip() == "N":
            if start is None:
                start = i + 1
        elif start is not None:
            blocks.append((start, i))
            start = None
    if start is not None:
        blocks.append((start, len(codes)))
    return blocks


def trigger_blocks(codes: list[str], *, prev_seq: int, cycle: int) -> list[tuple[int, int]]:
    """연번이 cycle 에 도달한 N 이 속한 블록들을 돌려준다.

    Args:
        codes: 1일=index 0 인 근무코드 배열
        prev_seq: 전월 말 연번
        cycle: 트리거 주기

    Returns:
        [(블록시작일, 블록종료일), ...] — 중복 없음
    """
    seq = int(prev_seq or 0)
    hit_days = []
    for i, c in enumerate(codes):
        if str(c or "").strip() != "N":
            continue
        seq = 1 if seq >= cycle else seq + 1
        if seq >= cycle:
            hit_days.append(i + 1)
    if not hit_days:
        return []
    blocks = _n_blocks(codes)
    out = []
    for d in hit_days:
        for b in blocks:
            if b[0] <= d <= b[1] and b not in out:
                out.append(b)
                break
    return out


def candidate_days(codes: list[str], block: tuple[int, int]) -> list[int]:
    """블록 종료 다음날부터 **다음 N 블록 시작 전**까지의 근무일(1-based).

    실측 규칙: 수면OFF 는 이 구간에 놓인다(준수 104 · 이월 9 · 위반 4 = 96.6%).
    """
    n = len(codes)
    nxt = n + 1
    for i in range(block[1], n):          # block[1] 은 1-based 종료일
        if str(codes[i] or "").strip() == "N":
            nxt = i + 1
            break
    out = []
    for day in range(block[1] + 1, nxt):
        if day > n:
            break
        c = str(codes[day - 1] or "").strip()
        if c in _WORK_MAIN or c == "DE":
            out.append(day)
    return out


def _assigned_count(generated: dict, day: int, main: str) -> int:
    """그날 그 근무의 배정 인원."""
    cnt = 0
    for codes in generated.values():
        if day - 1 < len(codes) and str(codes[day - 1] or "").strip() == main:
            cnt += 1
    return cnt


def postprocess_sleep_off(db: Session, schedule, generated: dict,
                          latest_config: Optional[RosterConfig] = None,
                          stats: Optional[dict] = None) -> dict:
    """수면OFF 부여 후처리. `generated` 를 in-place 수정 후 반환한다.

    설정이 꺼져 있거나 타깃 코드가 없으면 아무것도 하지 않는다.
    """
    gid = str(schedule.group_id)
    cfg = latest_config
    if cfg is None or getattr(cfg, "group_id", None) != gid:
        cfg = (db.query(RosterConfig).filter(RosterConfig.group_id == gid)
                 .order_by(RosterConfig.created_at.desc()).first())
    if not bool(getattr(cfg, "sleep_off_enabled", False)):
        return generated
    target = resolve_sleep_off_shift(db, gid)
    if target is None:
        logger.info("[SleepOff] group=%s enabled 지만 타깃 코드 없음 — 미부여", gid)
        return generated

    cycle = int(getattr(cfg, "sleep_off_cycle", None) or resolve_cycle(db, gid) or DEFAULT_CYCLE)
    py, pm = _prev_ym(int(schedule.year), int(schedule.month))
    req = _load_requirements(db, schedule)
    placed = skipped = 0
    # 화면 요약용 — 전월에서 넘어온 이월분과 이번 달 신규 도달을 구분해 센다.
    carried_in = new_trigger = 0
    carried_nurses: list[str] = []

    # per-nurse 예외(3-state). 테이블이 비어 있으면 {} 라 전원 대상 = 도입 전과 동일.
    from services.leave.leave_eligibility import fetch_leave_flags, is_sleep_off_eligible
    flags = fetch_leave_flags(db, list(generated.keys()),
                              int(schedule.year), int(schedule.month))

    # 전월 미마감 시 연번이 리셋되지 않도록 전월 최신 근무표로 폴백한다.
    #   ★ 마감(compute_snapshot)도 같은 함수를 쓴다 — 한쪽만 폴백하면 같은 달인데
    #     생성값과 앵커값이 어긋난다.
    #   ★ 판정은 **그룹 단위**다. 개인별 (0,0) 으로 보면 전월에 나이트를 안 선 사람이
    #     정상값인데도 폴백을 타 다른 근무표 기준 값이 섞인다.
    fallback = ({} if has_anchor(db, gid, py, pm)
                else prev_month_fallback(db, gid, py, pm))
    # 월 경계도 하드락 대상이다 — 1일 N 을 빼면 전월 말 N 이 단독이 될 수 있다.
    prev_tails = _load_prev_month_tail(db, gid, py, pm)

    for nid, codes in generated.items():
        if not is_sleep_off_eligible(flags.get(str(nid))):
            continue
        prev_seq, prev_pending, _ = fetch_anchor(db, str(nid), gid, py, pm)
        if str(nid) in fallback:
            prev_seq, prev_pending, _ = fallback[str(nid)]
        blocks = trigger_blocks(list(codes), prev_seq=prev_seq, cycle=cycle)
        need = len(blocks) + int(prev_pending or 0)
        if need <= 0:
            continue
        if prev_pending:
            carried_in += int(prev_pending)
            carried_nurses.append(str(nid))
        new_trigger += len(blocks)
        spans = blocks or [(0, 0)]          # 이월만 있으면 월초부터 탐색
        for block in spans[:need]:
            day = _pick_day(generated, codes, block, req, target.shift_id,
                            prev_tails.get(str(nid), ""))
            if day is None:
                skipped += 1
                continue
            codes[day - 1] = target.shift_id
            placed += 1
    if placed or skipped:
        print(f"[SleepOff] {gid} {schedule.year}-{int(schedule.month):02d} "
              f"배치 {placed}건 · 스킵 {skipped}건 (code={target.shift_id} cycle={cycle})")
    # ★ skipped 는 그냥 실패가 아니라 **다음 달로 이월**된다. 화면에 안 보이면
    #   운영자는 다음 달에 갑자기 늘어난 것으로만 보인다.
    if stats is not None:
        stats.update({
            "code": target.shift_id,
            "cycle": cycle,
            "placed": placed,
            "carried_out": skipped,        # 자리를 못 찾아 다음 달로 넘어간 건수
            "carried_in": carried_in,      # 전월에서 넘어와 이번 달에 처리 대상이던 건수
            "carried_in_nurses": carried_nurses,
            "new_trigger": new_trigger,    # 이번 달에 새로 주기에 도달한 건수
        })
    return generated


def _load_prev_month_tail(db: Session, group_id: str, year: int,
                          month: int) -> dict[str, str]:
    """전월 **마지막 날** 근무코드 {nurse_id: code}. 없으면 빈 dict.

    하드락은 월 경계를 넘어 적용된다. 이 값이 없으면 1일 N 을 수면OFF 로 바꿔
    전월 말 N 을 단독 1N 으로 만드는 경우를 못 잡는다.
    ★ 마감분 우선 — draft 는 폐기될 수 있어 확정본이 있으면 그것을 본다.
    """
    import calendar as _cal
    from sqlalchemy import case
    from db.models import Schedule, ScheduleEntry

    last_day = _cal.monthrange(int(year), int(month))[1]
    sch = (
        db.query(Schedule)
        .filter(Schedule.group_id == str(group_id),
                Schedule.year == int(year), Schedule.month == int(month),
                Schedule.dropped == False)  # noqa: E712
        .order_by(case((Schedule.status == "issued", 0), else_=1),
                  Schedule.created_at.desc())
        .first()
    )
    if sch is None:
        return {}
    try:
        rows = (
            db.query(ScheduleEntry.nurse_id, ScheduleEntry.work_date,
                     ScheduleEntry.shift_id)
            .filter(ScheduleEntry.schedule_id == sch.schedule_id)
            .all()
        )
    except Exception as exc:
        logger.warning("[SleepOff] 전월 꼬리 조회 실패(무시): %s", exc)
        return {}
    out: dict[str, str] = {}
    for nid, wdate, code in rows:
        if wdate is None or int(wdate.day) != last_day:
            continue
        out[str(nid)] = str(code or "").strip()
    return out


def _count_solo_nights(codes: list[str]) -> int:
    """단독 1N 블록의 개수."""
    n, i, solo = len(codes), 0, 0
    while i < n:
        if str(codes[i] or "").strip() == "N":
            j = i
            while j + 1 < n and str(codes[j + 1] or "").strip() == "N":
                j += 1
            if j - i + 1 == 1:
                solo += 1
            i = j + 1
        else:
            i += 1
    return solo


def _breaks_one_night(codes: list[str], day: int, sleep_code: str,
                      prev_tail: str = "") -> bool:
    """`day` 를 수면OFF 로 치환하면 **단독 1N 이 늘어나는가**.

    ★★ 하드락 #7(1N 금지) 방어. `N N N` 의 가운데를 빼면 `N 수면 N` = 1N 두 개다.
      이월분 처리 경로(block=(0,0))는 후보를 "월 전체 근무일"로 잡는데
      `_WORK_MAIN` 에 N 이 들어 있어 아무 N 이나 뽑을 수 있었다 — 실측으로 확인된
      위반 경로다(호스피스 2026-12, 3회 중 1회 재발).
    ★ 원래부터 1N 이던 것은 우리 탓이 아니므로 **증가분**만 본다.
    ★ 블록 끝 N 치환(②)은 `N N 수면` 이라 증가가 없어 그대로 통과한다.
    ★★ `prev_tail` 은 전월 마지막날 코드다. **월 경계도 하드락 대상**이라 이걸 앞에
      붙여 세지 않으면, 1일 N 을 빼서 전월 말 N 을 단독으로 만드는 경우를 놓친다
      (전월 31일 N + 1일 N = 연속 2N 인데, 1일을 빼면 전월이 1N 이 된다).
    """
    if str(codes[day - 1] or "").strip() != "N":
        return False                      # N 이 아닌 근무일 치환은 N 구조를 안 바꾼다
    head = [str(prev_tail or "").strip()] if prev_tail else []
    before = head + list(codes)
    after = head + list(codes)
    after[len(head) + day - 1] = sleep_code
    return _count_solo_nights(after) > _count_solo_nights(before)


def _scan(generated: dict, codes: list[str], days: list[int],
          req: dict[int, dict[str, int]], sleep_code: str = "",
          prev_tail: str = "") -> Optional[int]:
    """후보일 중 빼도 커버리지가 유지되고 1N 을 만들지 않는 첫 날.

    ★ 근무를 빼면 그날 인원이 1 줄어든다. `daily_shift` 필요 인원을 넘겨야만 뺀다.
      요구 행이 없으면(설정 미비) 안전하게 건너뛴다 — 커버리지를 깨는 것보다 낫다.
    ★ N 을 빼는 경우엔 하드락 #7 을 깨지 않는지도 본다.
    """
    for day in days:
        main = str(codes[day - 1] or "").strip()
        need = (req.get(day) or {}).get(main)
        if not need:
            continue
        if _assigned_count(generated, day, main) - 1 < need:
            continue
        if sleep_code and _breaks_one_night(codes, day, sleep_code, prev_tail):
            continue
        return day
    return None


def _pick_day(generated: dict, codes: list[str], block: tuple[int, int],
              req: dict[int, dict[str, int]], sleep_code: str,
              prev_tail: str = "") -> Optional[int]:
    """치환할 근무일 하나. 없으면 None.

    ① 실측 규칙 우선 — N 블록 종료 후 다음 N 블록 전의 근무일(준수 96.6%).
    ② ①이 비면 **그 블록의 마지막 N** 을 대체한다(`N N N` → `N N 수면`).

    ★★ ② 를 둔 이유 (2026-08-04 실측, issued 64건·트리거 301건)
    ```
    ① 로 후보가 잡힌 트리거      65건 (25.3%)
    구간이 전부 OFF 라 후보 0    192건 (74.7%)  ← 그 구간 코드: O 331 · 근무일 0
    ```
      하드락 #4·#5(나이트 2~3연속 후 OFF 2회)가 강제한 OFF 2개 뒤에 솔버가 곧바로
      다음 N 을 넣어, ①이 찾는 근무일이 **구조적으로 존재하지 않는다**(간격 2일이 61%).
      실무 손작성 근무표는 같은 구간에 근무일이 80.7% 있어 ①이 성립했다.

    ★★ 블록 길이 3 이상에서만 쓴다 — 하드락 #7(1N 금지) 때문이다.
      `N N` 에서 하나를 빼면 `N 수면` = 단독 1N 이 되어 위반이다. 실측상 이 경우가
      103건(53.6%)이라 ②로도 구제되지 않는다(그대로 이월된다).
      다른 하드락은 N 이 줄어드는 방향이라 유지된다(#3 연속상한·#4/#5 이후 OFF).
    """
    days = candidate_days(list(codes), block) if block[1] else [
        i + 1 for i, c in enumerate(codes) if str(c or "").strip() in _WORK_MAIN
    ]
    picked = _scan(generated, codes, days, req, sleep_code, prev_tail)
    if picked is not None:
        return picked
    # ② 블록의 마지막 N — n_count 여유 검사는 _scan 이 동일하게 수행한다.
    if block[1] and (block[1] - block[0] + 1) >= _MIN_BLOCK_FOR_TAIL_CUT:
        return _scan(generated, codes, [block[1]], req, sleep_code, prev_tail)
    return None
