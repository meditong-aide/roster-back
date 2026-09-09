"""근무표 셀 수동 수정 이력 — 스냅샷 비교로 바뀐 칸만 골라 `schedule_entry_log` 에 남긴다.

★ 왜 diff 인가
  `/roster/save` 는 프론트가 **근무표 전체**를 보내고 서버가 전량 교체하는 구조다. 즉 서버는
  "무엇이 바뀌었는지" 를 모른다. 그래서 교체 **직전에** 현재 상태를 찍어 두고, 교체 후 값과
  비교해 바뀐 칸만 골라낸다.

★ 대상은 사람이 화면에서 고친 것뿐이다. 생성·재생성은 남기지 않는다 — 한 번에 수백 행이
  바뀌어 로그가 금세 커지고, 사용자가 보려는 "내가 고친 이력" 과 성격이 다르다.
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Any, Optional

from sqlalchemy import and_, func, or_
from sqlalchemy.orm import Session

from db.models import ScheduleEntry, ScheduleEntryLog, Shift

#: (nurse_id, work_date) → (shift_id, shifts.id)
CellMap = dict[tuple[str, date], tuple[Optional[str], Optional[int]]]


def _as_date(v: Any) -> Optional[date]:
    """`work_date` 는 모델상 DATETIME 이라 datetime 으로 올 수 있다. 키를 date 로 통일한다."""
    if v is None:
        return None
    return v.date() if isinstance(v, datetime) else v


def snapshot_entries(db: Session, schedule_id: str) -> CellMap:
    """현재 저장된 셀 상태. **교체 직전에** 부를 것."""
    rows = (
        db.query(ScheduleEntry.nurse_id, ScheduleEntry.work_date,
                 ScheduleEntry.shift_id, ScheduleEntry.id)
        .filter(ScheduleEntry.schedule_id == schedule_id)
        .all()
    )
    out: CellMap = {}
    for nurse_id, work_date, shift_id, int_id in rows:
        d = _as_date(work_date)
        if d is None or nurse_id is None:
            continue
        out[(str(nurse_id), d)] = (shift_id, int_id)
    return out


def _shift_colors(db: Session, group_id: str) -> dict[str, Optional[str]]:
    """shift_id → color. 색은 그 시점 값을 로그에 박아 둔다 — 병동이 나중에 색을 바꿔도
    과거 이력이 현재 색으로 그려지지 않게 한다.

    ★ `before_color` 는 **이 변경 직전**의 색이다 = 편집자가 화면에서 본 색.
      *직전 저장 시점* 의 색이 아니다. 그래서 여기서 현재 `shifts` 를 읽는 것이 맞다.
      병동이 색을 바꾼 뒤 셀을 고치면 새 색이 before 로 남는데, 그 편집자가 실제로
      본 것이 새 색이므로 정확한 기록이다. 조회 시점에 재구성하지 않고 로그 행에
      값으로 박기 때문에, 이후 색이 또 바뀌어도 이 행은 변하지 않는다.
    """
    return {
        s.shift_id: s.color
        for s in db.query(Shift.shift_id, Shift.color).filter(Shift.group_id == group_id).all()
    }


def _next_seq_map(db: Session, schedule_id: str,
                  cells: list[tuple[str, date]]) -> dict[tuple[str, date], int]:
    """바뀐 칸들의 다음 `seq`. 한 번의 쿼리로 기존 최대값을 모아 온다.

    ★ 칸마다 따로 MAX 를 치면 수정 한 번에 N 번 왕복한다. 보통 1~5 칸이지만
      하루치를 한꺼번에 고치면 수십 칸이 되므로 묶어서 읽는다.
    ★ nurse_id·work_date 를 각각 IN 으로 거는 것은 카테시안이라 **대상이 아닌 칸도 읽힌다.**
      다만 GROUP BY 결과를 `prev.get(cell)` 로 정확한 칸만 꺼내므로 값은 옳다(여분 행을
      읽을 뿐). is_latest 를 꺾는 쪽은 사정이 다르다 — 거기서 카테시안을 쓰면 무관한 칸의
      플래그까지 내려가므로 반드시 쌍으로 묶어야 한다.
    """
    if not cells:
        return {}
    nurse_ids = {n for n, _ in cells}
    dates = {d for _, d in cells}
    rows = (
        db.query(ScheduleEntryLog.nurse_id, ScheduleEntryLog.work_date,
                 func.max(ScheduleEntryLog.seq))
        .filter(
            ScheduleEntryLog.schedule_id == schedule_id,
            ScheduleEntryLog.nurse_id.in_(list(nurse_ids)),
            ScheduleEntryLog.work_date.in_(list(dates)),
        )
        .group_by(ScheduleEntryLog.nurse_id, ScheduleEntryLog.work_date)
        .all()
    )
    prev = {(str(n), _as_date(d)): int(m or 0) for n, d, m in rows}
    return {c: prev.get(c, 0) + 1 for c in cells}


def list_schedule_history(
    db: Session,
    *,
    schedule_id: str,
    limit: int = 500,
    latest_only: bool = False,
) -> dict:
    """근무표 하나의 수정 이력 — 최신순.

    Args:
        latest_only: True 면 칸마다 **마지막 변경 한 건씩**만. 같은 칸을 여러 번 고친 경우
            중간 단계를 빼고 "지금까지 손댄 칸들의 최종 모습"을 본다.

    ★ 현재값은 이 테이블이 아니라 `schedule_entries` 에서 가져온다. 로그에는 **바뀐 칸만**
      있으므로 마지막 `after` 를 현재값으로 쓰면 한 번도 안 바뀐 칸을 놓치고, 이력이 남은
      칸도 이후 재생성으로 값이 달라졌다면 어긋난다. 현재 상태의 정본은 언제나 근무표 쪽이다.
    """
    _lim = max(1, min(int(limit or 500), 2000))
    _q = db.query(ScheduleEntryLog).filter(ScheduleEntryLog.schedule_id == schedule_id)
    if latest_only:
        # 필터 인덱스(IX_sel_latest, WHERE is_latest=1)를 그대로 탄다.
        # MSSQL bit — `.is_(True)` 는 구문 오류다(위 주석 참조).
        _q = _q.filter(ScheduleEntryLog.is_latest == True)  # noqa: E712
    # ★ limit+1 을 읽어 "더 있는지" 를 판별한다. 정확히 limit 건일 때 무조건 truncated 로
    #   보고하면 딱 맞아떨어진 경우까지 "잘렸다" 고 알려 프론트가 헛되이 더 부른다.
    rows = (
        _q.order_by(ScheduleEntryLog.changed_at.desc(), ScheduleEntryLog.log_id.desc())
        .limit(_lim + 1)
        .all()
    )
    _truncated = len(rows) > _lim
    rows = rows[:_lim]
    # ★ 잘렸을 때만 전체 건수를 따로 센다. 안 잘렸으면 반환 수가 곧 전체다 —
    #   매번 COUNT 를 치면 흔한 경우에 쓸데없는 왕복이 붙는다.
    _total = len(rows)
    if _truncated:
        _cq = db.query(func.count(ScheduleEntryLog.log_id)).filter(
            ScheduleEntryLog.schedule_id == schedule_id)
        if latest_only:
            _cq = _cq.filter(ScheduleEntryLog.is_latest == True)  # noqa: E712
        _total = int(_cq.scalar() or 0)
    # 이름은 화면 표시용 — 이력에 등장한 간호사만 모아 한 번에 읽는다.
    from db.models import Nurse
    nurse_ids = {r.nurse_id for r in rows}
    names: dict[str, str] = {}
    if nurse_ids:
        names = {
            str(n.nurse_id): n.name
            for n in db.query(Nurse.nurse_id, Nurse.name)
            .filter(Nurse.nurse_id.in_(list(nurse_ids))).all()
        }
    # 현재값 — **이력에 등장한 칸만** 근무표에서 읽는다.
    # ★ 전체 스냅샷을 뜨면 limit=1 짜리 요청도 근무표 전부(수백 행)를 끌어온다.
    cur: CellMap = {}
    if rows:
        _cells = {(str(r.nurse_id), _as_date(r.work_date)) for r in rows}
        _ent = (
            db.query(ScheduleEntry.nurse_id, ScheduleEntry.work_date,
                     ScheduleEntry.shift_id, ScheduleEntry.id)
            .filter(
                ScheduleEntry.schedule_id == schedule_id,
                ScheduleEntry.nurse_id.in_(list({n for n, _ in _cells})),
                ScheduleEntry.work_date.in_(list({d for _, d in _cells})),
            )
            .all()
        )
        # nurse/date 를 각각 IN 으로 걸어 여분 행이 딸려오지만, 아래에서 정확한 칸만 꺼내
        # 값은 옳다(대상 칸이 보통 수십 개라 전체를 읽는 것보다 훨씬 적다).
        for _n, _d, _sh, _id in _ent:
            _k = (str(_n), _as_date(_d))
            if _k in _cells:
                cur[_k] = (_sh, _id)

    items = []
    for r in rows:
        cell = (str(r.nurse_id), _as_date(r.work_date))
        cur_shift, cur_id = cur.get(cell, (None, None))
        items.append({
            "log_id": r.log_id,
            "seq": r.seq,
            "nurse_id": r.nurse_id,
            "nurse_name": names.get(str(r.nurse_id)),
            "date": r.work_date.isoformat() if r.work_date else None,
            "action": r.action,
            "before": {"shift_id": r.before_shift_id, "id": r.before_id,
                       "color": r.before_color} if r.before_shift_id else None,
            "after": {"shift_id": r.after_shift_id, "id": r.after_id,
                      "color": r.after_color} if r.after_shift_id else None,
            # ★ 로그의 after 가 아니라 근무표의 현재값. 재생성 등으로 달라졌을 수 있다.
            "current": {"shift_id": cur_shift, "id": cur_id} if cur_shift else None,
            "changed_by": r.changed_by,
            "changed_at": r.changed_at.isoformat() if r.changed_at else None,
            "is_latest": bool(r.is_latest),
        })
    return {
        "schedule_id": schedule_id,
        "latest_only": bool(latest_only),
        "count": len(items),      # 이번 응답에 담긴 건수
        "total": _total,          # 조건에 맞는 전체 건수
        "truncated": _truncated,
        "items": items,
    }


def log_manual_changes(
    db: Session,
    *,
    schedule_id: str,
    group_id: str,
    before: CellMap,
    after: CellMap,
    changed_by: Optional[str],
) -> int:
    """`before` → `after` 로 바뀐 칸을 로그에 적재하고 건수를 돌려준다.

    ★ commit 은 하지 않는다. 호출부(저장 트랜잭션)와 **같은 커밋에 묶여야** 근무표와
      이력이 어긋나지 않는다.
    """
    changed: list[tuple[str, date]] = []
    for cell in set(before) | set(after):
        if before.get(cell) != after.get(cell):
            changed.append(cell)
    if not changed:
        return 0

    colors = _shift_colors(db, group_id)
    seqs = _next_seq_map(db, schedule_id, changed)
    now = datetime.now()

    # ★ 같은 칸의 직전 최신 기록을 먼저 꺾는다. 칸당 is_latest 는 항상 하나여야 한다.
    #   ★★ nurse_id.in_(...) 와 work_date.in_(...) 를 **따로** 걸면 안 된다 — 카테시안이라
    #      (n1,d1)·(n2,d2) 만 바뀌어도 (n1,d2)·(n2,d1) 까지 꺾인다. 칸을 쌍으로 묶어야 한다.
    #      MSSQL 은 (a,b) IN ((..),(..)) 을 지원하지 않으므로 OR 로 편다.
    db.query(ScheduleEntryLog).filter(
        ScheduleEntryLog.schedule_id == schedule_id,
        # ★ MSSQL 은 `IS TRUE` 를 모른다 — SQLAlchemy 의 `.is_(True)` 는 `IS 1` 로 나가
        #   구문 오류가 된다(sqlite 로는 통과해서 더 늦게 드러난다). bit 는 `== True` 로 비교한다.
        ScheduleEntryLog.is_latest == True,  # noqa: E712
        or_(*[
            and_(ScheduleEntryLog.nurse_id == n, ScheduleEntryLog.work_date == d)
            for n, d in changed
        ]),
    ).update({"is_latest": False}, synchronize_session=False)

    for cell in changed:
        nurse_id, work_date = cell
        b_shift, b_id = before.get(cell, (None, None))
        a_shift, a_id = after.get(cell, (None, None))
        if b_shift is None and a_shift is not None:
            action = "insert"
        elif b_shift is not None and a_shift is None:
            action = "delete"
        else:
            action = "update"
        db.add(ScheduleEntryLog(
            schedule_id=schedule_id,
            nurse_id=nurse_id,
            work_date=work_date,
            seq=seqs.get(cell, 1),
            before_id=b_id,
            before_shift_id=b_shift,
            before_color=colors.get(b_shift) if b_shift else None,
            after_id=a_id,
            after_shift_id=a_shift,
            after_color=colors.get(a_shift) if a_shift else None,
            group_id=group_id,
            action=action,
            source="manual",
            changed_by=str(changed_by) if changed_by else None,
            changed_at=now,
        ))
    return len(changed)
