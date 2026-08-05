"""N 연번 앵커(`nurse_night_cycle`) 계산·갱신.

`schedule_entries.shift_id` 에는 `'N'` 만 저장되고 N1~N15 연번은 없다.
그래서 "15 에 도달했는가"를 근무표만으로는 알 수 없다 — 전월 앵커가 있어야
`앵커 + 당월 N` 으로 연번이 복원된다.

이 모듈은 근무표 **확정(발행) 시점**에 그 달 스냅샷 1행을 upsert 해서
다음 달 앵커가 끊기지 않게 한다. 백필은 tools/leave_analysis/backfill_night_cycle.py.

설계: docs/leave_auto_assignment_design.md §5.2 · §6 Step4
"""
from __future__ import annotations

import logging
from typing import Optional

from sqlalchemy.orm import Session

from db.models import NurseNightCycle, Schedule, ScheduleEntry, RosterConfig, Shift

logger = logging.getLogger(__name__)

DEFAULT_CYCLE = 15


def resolve_sleep_off_shift(db: Session, group_id: str) -> Optional[Shift]:
    """그룹의 `sleep_off_target=True` 인 근무코드. 다수면 sequence ASC 첫 건 + warning."""
    rows = (
        db.query(Shift)
        .filter(Shift.group_id == group_id, Shift.sleep_off_target == True)  # noqa: E712
        .order_by(Shift.sequence.asc())
        .all()
    )
    if not rows:
        return None
    if len(rows) > 1:
        logger.warning(
            "[NightCycle] group=%s 타깃 코드 %d건 — sequence 첫 건(%s) 채택",
            group_id, len(rows), rows[0].shift_id,
        )
    return rows[0]


def resolve_cycle(db: Session, group_id: str) -> int:
    """그룹의 수면OFF 트리거 주기. 미설정이면 DEFAULT_CYCLE(15)."""
    cfg = (
        db.query(RosterConfig)
        .filter(RosterConfig.group_id == group_id)
        .order_by(RosterConfig.created_at.desc())
        .first()
    )
    v = getattr(cfg, "sleep_off_cycle", None)
    try:
        return int(v) if v else DEFAULT_CYCLE
    except (TypeError, ValueError):
        return DEFAULT_CYCLE


def _prev_ym(year: int, month: int) -> tuple[int, int]:
    return (year - 1, 12) if month == 1 else (year, month - 1)


def fetch_anchor(db: Session, nurse_id: str, group_id: str,
                 year: int, month: int) -> tuple[int, int, int]:
    """(seq_at_end, pending_sleep, sleep_off_seq) — 해당 월 스냅샷. 없으면 (0, 0, 0)."""
    row = (
        db.query(NurseNightCycle)
        .filter(NurseNightCycle.nurse_id == str(nurse_id),
                NurseNightCycle.group_id == str(group_id),
                NurseNightCycle.year == int(year),
                NurseNightCycle.month == int(month))
        .first()
    )
    if row is None:
        return 0, 0, 0
    return (int(row.seq_at_end or 0), int(row.pending_sleep or 0),
            int(row.sleep_off_seq or 0))


def advance_cycle(codes_by_day: dict[int, str], *, prev_seq: int, prev_pending: int,
                  sleep_code: Optional[str], cycle: int = DEFAULT_CYCLE,
                  prev_sleep_seq: int = 0) -> tuple[int, int, int, int]:
    """한 달치 근무코드를 훑어 (seq_at_end, pending_sleep, sleep_off_count, sleep_off_seq) 전진.

    Args:
        codes_by_day: {일(1-based): 근무코드}
        prev_seq: 전월 말 연번 (없으면 0)
        prev_pending: 전월 말 미부여 이월 수
        sleep_code: 그 그룹의 수면OFF 코드. None 이면 소진 계산을 하지 않는다.
        cycle: 트리거 주기 (실측 15)
        prev_sleep_seq: 전월 말 누적 수령 회차

    Returns:
        (seq_at_end, pending_sleep, sleep_off_count, sleep_off_seq)

    Notes:
        N 은 1..cycle 을 순환한다(실측: N15 다음이 N1).
        cycle 에 도달할 때마다 pending 이 1 늘고, 수면OFF 가 나올 때마다 1 줄어든다.
    """
    seq, pending = int(prev_seq or 0), int(prev_pending or 0)
    sleep_count = 0
    for day in sorted(codes_by_day):
        code = str(codes_by_day[day] or "").strip()
        if code == "N":
            seq = 1 if seq >= cycle else seq + 1
            if seq >= cycle:
                pending += 1
        elif sleep_code and code == sleep_code:
            pending = max(0, pending - 1)
            sleep_count += 1
    return seq, pending, sleep_count, int(prev_sleep_seq or 0) + sleep_count


def has_anchor(db: Session, group_id: str, year: int, month: int) -> bool:
    """그 달 앵커가 한 행이라도 있으면 True(= 그 달은 마감 스냅샷이 남아 있다).

    ★ 개인별 `fetch_anchor() == (0,0,0)` 으로 "앵커 없음" 을 판정하면 안 된다.
      전월에 나이트를 한 번도 안 선 사람은 **정상적으로** (0,0,0) 이라 구분이 안 되고,
      그 사람만 폴백을 타 다른 근무표 기준 값이 섞인다. 판정은 그룹 단위로 한다.
    """
    return (
        db.query(NurseNightCycle.nurse_id)
        .filter(NurseNightCycle.group_id == str(group_id),
                NurseNightCycle.year == int(year),
                NurseNightCycle.month == int(month))
        .first()
        is not None
    )


def prev_month_fallback(db: Session, group_id: str, year: int,
                        month: int) -> dict[str, tuple[int, int, int]]:
    """전월 앵커가 없을 때 쓸 대체값 — 전월 최신 근무표에서 즉석 계산한 (seq, pending, sleep_seq).

    ★★ 왜 필요한가
      앵커는 마감(issued)분만 저장한다. 전월을 아직 마감하지 않은 채 다음 달을 만들거나
      마감하면 `fetch_anchor` 가 (0,0,0) 을 돌려주고, **그 달 나이트를 아예 안 센 것처럼**
      연번이 리셋된다. 수면OFF 가 늦게 나오거나 영영 안 나온다.

    ★ 저장하지는 않는다 — draft 는 폐기될 수 있어 앵커로 굳히면 DB 가 오염된다.
      읽기 전용 폴백이라 부작용이 없고, 전월을 마감하면 정식 앵커가 이 값을 대체한다.
    ★ **생성(sleep_off_postprocess)과 마감(compute_snapshot)이 같은 함수를 쓴다.**
      한쪽만 폴백하면 같은 달인데 생성값과 앵커값이 어긋난다.
    ★ 재귀하지 않는다 — 한 단계(전월)만 본다. 전전월까지 미마감이면 거기서 멈춘다.
    """
    # ★ issued 를 draft 보다 우선한다. 확정본이 있는데 그보다 최신 draft 가 있다고
    #   draft 를 집으면, 폐기될 수 있는 값이 다음 달 연번의 기준이 된다.
    #   같은 status 안에서는 created_at 최신.
    sch = (
        db.query(Schedule)
        .filter(Schedule.group_id == str(group_id),
                Schedule.year == int(year), Schedule.month == int(month),
                Schedule.dropped == False)  # noqa: E712
        .order_by((Schedule.status == "issued").desc(), Schedule.created_at.desc())
        .first()
    )
    if sch is None:
        return {}
    try:
        out = {str(r["nurse_id"]): (int(r["seq_at_end"] or 0),
                                    int(r["pending_sleep"] or 0),
                                    int(r["sleep_off_seq"] or 0))
               for r in _compute_snapshot_raw(db, sch, use_fallback=False)}
    except Exception as exc:
        logger.warning("[NightCycle] 전월 폴백 계산 실패(무시) group=%s %d-%02d: %s",
                       group_id, year, month, exc)
        return {}
    if out:
        logger.info("[NightCycle] 전월(%d-%02d) 미마감 — 최신 근무표 %s 로 연번 폴백 %d명",
                    year, month, sch.schedule_id, len(out))
    return out


def compute_snapshot(db: Session, schedule: Schedule) -> list[dict]:
    """스케줄 1건의 간호사별 (nurse_id, seq_at_end, pending_sleep) 목록을 계산한다."""
    return _compute_snapshot_raw(db, schedule, use_fallback=True)


def _compute_snapshot_raw(db: Session, schedule: Schedule,
                          *, use_fallback: bool) -> list[dict]:
    """use_fallback=False 는 폴백 자신이 부르는 경로 — 재귀를 끊는다."""
    gid = str(schedule.group_id)
    cycle = resolve_cycle(db, gid)
    target = resolve_sleep_off_shift(db, gid)
    sleep_code = target.shift_id if target else None
    py, pm = _prev_ym(int(schedule.year), int(schedule.month))
    # 전월 앵커가 **그룹 단위로** 없을 때만 폴백을 만든다. 있으면 계산 자체를 안 해
    # 불필요한 전월 전체 스냅샷 비용도 들지 않는다.
    fallback = ({} if not use_fallback or has_anchor(db, gid, py, pm)
                else prev_month_fallback(db, gid, py, pm))

    rows = (
        db.query(ScheduleEntry.nurse_id, ScheduleEntry.work_date, ScheduleEntry.shift_id)
        .filter(ScheduleEntry.schedule_id == schedule.schedule_id)
        .all()
    )
    by_nurse: dict[str, dict[int, str]] = {}
    for nid, wdate, code in rows:
        if not nid or wdate is None:
            continue
        by_nurse.setdefault(str(nid), {})[int(wdate.day)] = str(code or "")

    out = []
    for nid, codes in by_nurse.items():
        prev_seq, prev_pending, prev_sleep_seq = fetch_anchor(db, nid, gid, py, pm)
        if nid in fallback:
            # fallback 은 전월 앵커가 그룹 단위로 없을 때만 채워진다(= 전월 미마감).
            prev_seq, prev_pending, prev_sleep_seq = fallback[nid]
        seq, pending, s_cnt, s_seq = advance_cycle(
            codes, prev_seq=prev_seq, prev_pending=prev_pending,
            sleep_code=sleep_code, cycle=cycle, prev_sleep_seq=prev_sleep_seq,
        )
        out.append({"nurse_id": nid, "seq_at_end": seq, "pending_sleep": pending,
                    "sleep_off_count": s_cnt, "sleep_off_seq": s_seq})
    return out


def upsert_night_cycle_snapshot(db: Session, schedule: Schedule) -> int:
    """확정된 근무표의 월 스냅샷을 upsert 한다. 반영 행수를 돌려준다.

    ★ 커밋은 호출자 책임 — 발행 트랜잭션에 함께 묶는다.
    ★ 수면OFF 기능이 꺼진 그룹에서도 앵커는 남긴다. 연번은 기능과 무관하게
      이어져야 하고, 나중에 기능을 켰을 때 과거 앵커가 없으면 판정이 불가능하다.
    """
    gid = str(schedule.group_id)
    y, m = int(schedule.year), int(schedule.month)
    snap = compute_snapshot(db, schedule)
    if not snap:
        # ★ 조용히 넘기지 않는다. 마감 근무표에 entry 가 0건인 것은 비정상이며,
        #   호출자가 flush 하지 않은 채 부른 경우가 대표적이다(autoflush=False 세션에서
        #   bulk delete 직후 재삽입분이 아직 DB 에 없는 상태 — 실측된 함정).
        logger.warning(
            "[NightCycle] group=%s %s-%02d 스냅샷 0명 — schedule_entries 가 비어 있다. "
            "bulk delete 후 flush 없이 호출했는지 확인할 것.",
            gid, y, m,
        )
        print(f"[NightCycle][WARN] {gid} {y}-{m:02d} 스냅샷 0명 — entries 없음")
        return 0

    existing = {
        str(r.nurse_id): r
        for r in db.query(NurseNightCycle).filter(
            NurseNightCycle.group_id == gid,
            NurseNightCycle.year == y,
            NurseNightCycle.month == m,
        ).all()
    }
    for s in snap:
        row = existing.get(s["nurse_id"])
        if row is None:
            db.add(NurseNightCycle(
                nurse_id=s["nurse_id"], group_id=gid, year=y, month=m,
                seq_at_end=s["seq_at_end"], pending_sleep=s["pending_sleep"],
                sleep_off_count=s["sleep_off_count"], sleep_off_seq=s["sleep_off_seq"],
            ))
        else:
            row.seq_at_end = s["seq_at_end"]
            row.pending_sleep = s["pending_sleep"]
            row.sleep_off_count = s["sleep_off_count"]
            row.sleep_off_seq = s["sleep_off_seq"]
    # ★★ flush 가 필수다 — rebuild_night_cycle_from 은 여러 달을 순서대로 돌리고,
    #   다음 달 fetch_anchor 는 **DB 쿼리**다. autoflush=False 라 여기서 add 한 신규
    #   앵커가 flush 없이는 DB 에 없어, 다음 달이 (0,0,0) 을 읽고 연번을 0 부터 센다.
    #   기존 행 UPDATE 는 identity map 덕에 우연히 보이지만 INSERT 는 안 보인다.
    db.flush()
    print(f"[NightCycle] {gid} {y}-{m:02d} 스냅샷 {len(snap)}명 upsert")
    return len(snap)


def rebuild_night_cycle_from(db: Session, group_id: str, year: int, month: int) -> int:
    """(year, month) **부터 이후 모든 마감(issued) 월**의 앵커를 재계산한다.

    ★ 왜 그 달만 갱신하면 안 되는가
      앵커는 전월 값을 이어받는다(`seq_at_end` · `pending_sleep` · `sleep_off_seq`).
      과거 달의 근무표가 바뀌면 그 이후 달이 전부 틀어지므로 **연쇄로** 다시 계산해야 한다.

    ★ 대상은 `status='issued'` 뿐이다 — draft 는 확정이 아니므로 앵커에 반영하지 않는다.
      마감본이 곧 진실이고, 마감본이 바뀌면 이 함수가 다시 불려 정합을 회복한다.

    Returns:
        재계산한 (월 × 인원) 행수 합계.
    """
    rows = (
        db.query(Schedule)
        .filter(Schedule.group_id == str(group_id),
                Schedule.status == "issued",
                # ★ 삭제(soft delete)된 근무표는 제외한다. drop_schedule 은 dropped=True 만
                #   세우고 status 는 'issued' 로 남기므로, 안 거르면 **지운 근무표가 계속
                #   앵커에 반영된다.** 저장소 관행도 조회 시 dropped 를 거른다.
                Schedule.dropped == False)  # noqa: E712
        .all()
    )
    # (year, month) 이상만, 연월 오름차순 — 앞 달 결과가 뒷 달 입력이 되므로 순서가 중요하다.
    #   ★ 같은 달에 issued 가 여러 건이면 **최신 1건만** 쓴다. publish_roster 가 발행 시
    #     같은 달의 기존 issued 를 draft 로 내려 월당 1건을 보장하지만, 과거 데이터나
    #     직접 수정으로 다건이 될 수 있다. 그대로 두면 같은 달을 두 번 전진시켜
    #     seq/pending 이 부풀려진다.
    per_month: dict[tuple[int, int], Schedule] = {}
    for s in rows:
        ym = (int(s.year), int(s.month))
        if ym < (int(year), int(month)):
            continue
        cur = per_month.get(ym)
        if cur is None or (s.created_at or 0) > (cur.created_at or 0):
            per_month[ym] = s
    targets = [per_month[k] for k in sorted(per_month)]

    # ★ upsert 를 **먼저** 한다. 고아 삭제와 upsert 는 서로 다른 달을 다루므로 순서가
    #   결과를 바꾸지 않는데, 삭제를 먼저 하면 뒤쪽 upsert 가 실패했을 때 호출자가
    #   예외를 삼키고 commit 하는 경로에서 **삭제만 영구 반영**된다.
    total = 0
    for sch in targets:
        total += upsert_night_cycle_snapshot(db, sch)

    # ★★ 고아 앵커 제거 — 마감본이 사라진 달의 앵커는 지운다.
    #   마감취소(/unpublish)와 마감본 삭제(DELETE /{schedule_id})는 status 만 바꾸거나
    #   행만 지우므로, 그대로 두면 **근무표가 없는데 앵커만 남는다**. 다음 달 생성이
    #   그 유령 값을 이어받아 실제와 어긋난 연번으로 수면OFF 를 판정하게 된다.
    #   재계산 대상(targets)에 없는 (year, month) 이상의 앵커가 그 대상이다.
    alive = {(int(s.year), int(s.month)) for s in targets}
    orphan_q = db.query(NurseNightCycle).filter(
        NurseNightCycle.group_id == str(group_id))
    removed = 0
    for row in orphan_q.all():
        ym = (int(row.year), int(row.month))
        if ym < (int(year), int(month)) or ym in alive:
            continue
        db.delete(row)
        removed += 1
    if removed:
        db.flush()
        print(f"[NightCycle] group={group_id} 고아 앵커 {removed}행 제거 "
              f"(마감취소·삭제로 근무표가 없어진 달)")

    if targets:
        print(f"[NightCycle] group={group_id} {year}-{int(month):02d} 이후 "
              f"{len(targets)}개월 연쇄 재계산 · {total}행")
    return total
