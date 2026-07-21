"""Schedule tools — read/modify roster data."""

from __future__ import annotations

from sqlalchemy.orm import Session
from sqlalchemy import desc

from db.models import Schedule, ScheduleEntry, Shift, IssuedRoster


def resolve_target_schedule(
    db: Session, group_id: str, year: int, month: int
) -> dict | None:
    """Find the best schedule for a group/month.

    Priority: published (issued) > latest version.
    Returns schedule metadata dict or None.
    """
    # Check for published (issued) schedule
    issued = (
        db.query(IssuedRoster)
        .filter(
            IssuedRoster.group_id == group_id,
            IssuedRoster.is_active == True,
        )
        .order_by(desc(IssuedRoster.issued_at))
        .first()
    )
    if issued:
        sched = db.query(Schedule).filter(Schedule.schedule_id == issued.schedule_id).first()
        if sched and sched.year == year and sched.month == month:
            return _schedule_meta(sched, is_published=True)

    # Fall back to latest version for this month
    sched = (
        db.query(Schedule)
        .filter(
            Schedule.group_id == group_id,
            Schedule.year == year,
            Schedule.month == month,
            Schedule.dropped == False,
        )
        .order_by(desc(Schedule.version))
        .first()
    )
    if sched:
        return _schedule_meta(sched, is_published=False)
    return None


def get_schedule_entries(
    db: Session,
    schedule_id: str,
    *,
    nurse_ids: list[str] | None = None,
    date_str: str | None = None,
    date_range_start: str | None = None,
    date_range_end: str | None = None,
) -> list[dict]:
    """Read schedule entries with optional filters."""
    q = db.query(ScheduleEntry).filter(ScheduleEntry.schedule_id == schedule_id)
    if nurse_ids:
        q = q.filter(ScheduleEntry.nurse_id.in_(nurse_ids))
    if date_str:
        # Parse to date object for cross-DB compat (SQLite stores datetime, MSSQL stores date)
        from datetime import date as _date, datetime as _dt
        try:
            d = _dt.strptime(date_str, "%Y-%m-%d").date() if isinstance(date_str, str) else date_str
            # Match both date and datetime columns
            q = q.filter(
                ScheduleEntry.work_date >= _dt.combine(d, _dt.min.time()),
                ScheduleEntry.work_date < _dt.combine(d, _dt.min.time()) + __import__('datetime').timedelta(days=1),
            )
        except (ValueError, TypeError):
            q = q.filter(ScheduleEntry.work_date == date_str)
    if date_range_start:
        from datetime import datetime as _dt
        try:
            d = _dt.strptime(date_range_start, "%Y-%m-%d") if isinstance(date_range_start, str) else date_range_start
            q = q.filter(ScheduleEntry.work_date >= d)
        except (ValueError, TypeError):
            q = q.filter(ScheduleEntry.work_date >= date_range_start)
    if date_range_end:
        from datetime import datetime as _dt, timedelta
        try:
            d = _dt.strptime(date_range_end, "%Y-%m-%d") if isinstance(date_range_end, str) else date_range_end
            q = q.filter(ScheduleEntry.work_date < d + timedelta(days=1))
        except (ValueError, TypeError):
            q = q.filter(ScheduleEntry.work_date <= date_range_end)
    rows = q.order_by(ScheduleEntry.work_date, ScheduleEntry.nurse_id).all()
    return [
        {
            "entry_id": r.entry_id,
            "schedule_id": r.schedule_id,
            "nurse_id": r.nurse_id,
            "work_date": str(r.work_date.date()) if hasattr(r.work_date, "date") else str(r.work_date),
            "shift_id": r.shift_id,
        }
        for r in rows
    ]


def get_schedule_versions(
    db: Session, group_id: str, year: int, month: int
) -> list[dict]:
    """List all schedule versions for a group/month."""
    rows = (
        db.query(Schedule)
        .filter(
            Schedule.group_id == group_id,
            Schedule.year == year,
            Schedule.month == month,
            Schedule.dropped == False,
        )
        .order_by(desc(Schedule.version))
        .all()
    )
    return [_schedule_meta(r) for r in rows]


def update_schedule_entry(
    db: Session,
    entry_id: str,
    new_shift_id: str,
    group_id: str,
) -> dict:
    """Update a single schedule entry's shift code.

    group_id-scoped: ScheduleEntry → Schedule join 으로 group_id 격리 검증
    (cross-group mutation 차단, defense-in-depth).

    Returns the updated entry and its previous value.
    """
    entry = (
        db.query(ScheduleEntry)
        .join(Schedule, Schedule.schedule_id == ScheduleEntry.schedule_id)
        .filter(
            ScheduleEntry.entry_id == entry_id,
            Schedule.group_id == group_id,
        )
        .first()
    )
    if not entry:
        return {"error": f"Entry {entry_id} not found in group {group_id}"}
    old_shift = entry.shift_id
    entry.shift_id = new_shift_id
    db.commit()
    db.refresh(entry)
    result = {
        "entry_id": entry.entry_id,
        "nurse_id": entry.nurse_id,
        "work_date": str(entry.work_date.date()) if hasattr(entry.work_date, "date") else str(entry.work_date),
        "old_shift_id": old_shift,
        "new_shift_id": entry.shift_id,
    }
    # ③ 편집 후 하드락 사후검증(경량·advisory·agent 경로 전용): 위반 시 hard_lock_warnings 로
    #   노출 → LLM 이 자연어로 알림. enforcement 아님(생성 솔버가 SSOT). 정상 HN 웹편집은
    #   프론트 useRosterValidation 이 실시간 커버하므로 여기선 agent 셀편집만 보강.
    warnings = _post_edit_hard_lock_warnings(db, entry.schedule_id, entry.nurse_id, group_id)
    if warnings:
        result["hard_lock_warnings"] = warnings
    return result


def find_schedule_entry(
    db: Session,
    schedule_id: str,
    nurse_id: str,
    date_str: str,
    group_id: str,
) -> dict | None:
    """Find a specific entry by schedule + nurse + date.

    group_id-scoped: Schedule join 으로 RBAC 격리.
    """
    q = (
        db.query(ScheduleEntry)
        .join(Schedule, Schedule.schedule_id == ScheduleEntry.schedule_id)
        .filter(
            ScheduleEntry.schedule_id == schedule_id,
            ScheduleEntry.nurse_id == nurse_id,
            Schedule.group_id == group_id,
        )
    )
    # Cross-DB date comparison: SQLite stores datetime, MSSQL stores date strings
    from datetime import datetime as _dt, timedelta
    if isinstance(date_str, str):
        try:
            d = _dt.strptime(date_str, "%Y-%m-%d").date()
            q = q.filter(
                ScheduleEntry.work_date >= _dt.combine(d, _dt.min.time()),
                ScheduleEntry.work_date < _dt.combine(d, _dt.min.time()) + timedelta(days=1),
            )
        except (ValueError, TypeError):
            q = q.filter(ScheduleEntry.work_date == date_str)
    else:
        q = q.filter(ScheduleEntry.work_date == date_str)
    entry = q.first()
    if not entry:
        return None
    return {
        "entry_id": entry.entry_id,
        "schedule_id": entry.schedule_id,
        "nurse_id": entry.nurse_id,
        "work_date": str(entry.work_date.date()) if hasattr(entry.work_date, "date") else str(entry.work_date),
        "shift_id": entry.shift_id,
    }


# ── private ──────────────────────────────────────────────


def _post_edit_hard_lock_warnings(db, schedule_id, nurse_id, group_id) -> list[str]:
    """셀 편집 후 영향 간호사 근무열의 하드락 위반을 경고 문자열로 반환(경량·advisory).

    ★enforcement 아님(생성 솔버가 SSOT). agent 편집 결과를 LLM 에 노출하기 위한 사후검증.
    config 비활성(토글 off)인 제약은 검사하지 않는다(하드락 정책의 활성 조건과 동일).
    """
    from agents_v2.tools import constraint_tools, shift_tools
    config = constraint_tools.get_roster_config(db, group_id) or {}
    cats = _shift_category_map(shift_tools.read_shift_definitions(db, group_id))
    seq = _load_nurse_sequence(db, schedule_id, nurse_id)
    if not seq:
        return []
    return (
        _streak_warnings(seq, cats, config)
        + _adjacency_warnings(seq, cats, config)
        + _monthly_night_warning(seq, cats, config)
    )


def _load_nurse_sequence(db, schedule_id, nurse_id) -> list[tuple]:
    """영향 간호사의 해당 스케줄 근무열 → 날짜순 (date, shift_id) 리스트."""
    rows = (
        db.query(ScheduleEntry)
        .filter(ScheduleEntry.schedule_id == schedule_id, ScheduleEntry.nurse_id == nurse_id)
        .order_by(ScheduleEntry.work_date)
        .all()
    )
    return [
        (r.work_date.date() if hasattr(r.work_date, "date") else r.work_date, r.shift_id)
        for r in rows
    ]


def _shift_category_map(shifts: list[dict]) -> dict:
    """shift_gb/type → 카테고리 집합(N/D/E/근무)."""
    return {
        "night": {s["shift_id"] for s in shifts if s.get("shift_gb") == "나이트"},
        "day": {s["shift_id"] for s in shifts if s.get("shift_gb") == "데이"},
        "eve": {s["shift_id"] for s in shifts if s.get("shift_gb") == "이브닝"},
        "work": {s["shift_id"] for s in shifts if s.get("is_working")},
    }


def _category_runs(seq: list[tuple], id_set: set) -> list[tuple]:
    """근무열에서 id_set 시프트의 날짜-인접 연속 런 → [(length, start_date, end_date)]."""
    runs, run, prev = [], [], None
    for d, sid in seq:
        if sid in id_set and (not run or (d - prev).days == 1):
            run.append(d)
        elif sid in id_set:
            runs.append((len(run), run[0], run[-1]))
            run = [d]
        elif run:
            runs.append((len(run), run[0], run[-1]))
            run = []
        prev = d
    if run:
        runs.append((len(run), run[0], run[-1]))
    return runs


def _streak_warnings(seq: list[tuple], cats: dict, config: dict) -> list[str]:
    """연속근무 초과·나이트 연속 초과·단일나이트(1N) 검출."""
    out = []
    max_work = config.get("max_conseq_work") or 5
    max_nig = 3 if config.get("three_seq_nig") else 2
    for length, s, e in _category_runs(seq, cats["work"]):
        if length > max_work:
            out.append(f"연속근무 {length}일({s}~{e}) — 최대 {max_work}일 초과")
    for length, s, e in _category_runs(seq, cats["night"]):
        if length > max_nig:
            out.append(f"연속 나이트 {length}일({s}~{e}) — 최대 {max_nig}일 초과")
        elif length == 1 and config.get("not_one_night"):
            out.append(f"단일 나이트(1N) {s} — 나이트는 2회 이상 연속 배정 필요")
    return out


def _adjacency_warnings(seq: list[tuple], cats: dict, config: dict) -> list[str]:
    """인접일 금지 전환(ND/NE=nod_noe · ED=banned_day_after_eve) 검출."""
    def cat_of(sid):
        if sid in cats["night"]:
            return "N"
        if sid in cats["eve"]:
            return "E"
        if sid in cats["day"]:
            return "D"
        return None
    out = []
    for i in range(1, len(seq)):
        (pd, ps), (cd, cs) = seq[i - 1], seq[i]
        if (cd - pd).days != 1:
            continue
        a, b = cat_of(ps), cat_of(cs)
        if config.get("nod_noe") and a == "N" and b in ("D", "E"):
            out.append(f"{pd}→{cd} 금지전환 {a}→{b}(나이트 직후 데이/이브닝)")
        if config.get("banned_day_after_eve") and a == "E" and b == "D":
            out.append(f"{pd}→{cd} 금지전환 E→D(이브닝 직후 데이)")
    return out


def _monthly_night_warning(seq: list[tuple], cats: dict, config: dict) -> list[str]:
    """월 나이트 총량 상한(max_nig_per_month) 초과 검출."""
    cap = config.get("max_nig_per_month")
    if not cap:
        return []
    n_count = sum(1 for _, sid in seq if sid in cats["night"])
    return [f"월 나이트 {n_count}회 — 상한 {cap}회 초과"] if n_count > cap else []


def _schedule_meta(sched: Schedule, is_published: bool = False) -> dict:
    return {
        "schedule_id": sched.schedule_id,
        "group_id": sched.group_id,
        "year": sched.year,
        "month": sched.month,
        "version": sched.version,
        "name": sched.name,
        "status": sched.status,
        "is_published": is_published,
        "config_id": sched.config_id,
        "created_at": str(sched.created_at) if sched.created_at else None,
    }
