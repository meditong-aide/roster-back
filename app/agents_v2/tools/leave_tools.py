"""휴가 자동부여(보건휴가·수면OFF) 결과 요약 — 확정된 근무표에서 되읽는다.

dev 가 생성 응답에 `roster_data["leave_summary"]` 를 싣기 시작했지만(5bd13ed), 에이전트
경로는 **비동기(SQS)** 라 그 dict 를 볼 수 없다 — `roster_jobs` 에는 결과 payload 컬럼이
없고 job 은 status/result_roster_id 만 남긴다. 그래서 스키마를 바꾸는 대신 **생성된 표를
되읽어** 같은 질문("몇 명에게 줬나")에 답한다.

★ 되읽기가 오히려 더 정직하다 — postprocess stats 는 off_swap 등 이후 단계 **이전**의
  수치인 반면, 여기 수치는 사용자가 실제로 보는 최종 표의 셀 수다.
★ 되읽기로 알 수 없는 것이 하나 있다: 수면OFF 의 **다음 달 이월(carried_out)**.
  자리를 못 찾아 넘어간 건수는 표에 흔적이 없다. 그건 로그(그리고 생성 동기 응답)에만
  있으므로 여기서 추정하지 않는다(없는 수치를 지어내지 않는다).
"""

from __future__ import annotations

from datetime import date

from sqlalchemy.orm import Session

from db.models import Nurse, Schedule, ScheduleEntry


def resolve_leave_codes(db: Session, group_id: str) -> dict[str, dict]:
    """그룹의 휴가 자동부여 타깃 코드. {kind: {"code","name"}}. 미설정 kind 는 키 없음."""
    from services.leave.health_leave_planner import resolve_health_leave_shift
    from services.leave.night_cycle_service import resolve_sleep_off_shift

    out: dict[str, dict] = {}
    for kind, resolver in (
        ("health_leave", resolve_health_leave_shift),
        ("sleep_off", resolve_sleep_off_shift),
    ):
        try:
            shift = resolver(db, group_id)
        except Exception:  # noqa: BLE001 — 코드 미설정은 기능 미사용이지 오류가 아니다
            shift = None
        if shift is not None:
            out[kind] = {"code": shift.shift_id, "name": getattr(shift, "name", None)}
    return out


def _latest_schedule(db: Session, group_id: str, year: int, month: int) -> Schedule | None:
    return (
        db.query(Schedule)
        .filter(
            Schedule.group_id == group_id,
            Schedule.year == year,
            Schedule.month == month,
            Schedule.dropped == False,  # noqa: E712
        )
        .order_by(Schedule.version.desc())
        .first()
    )


def summarize_leave_grants(
    db: Session,
    group_id: str,
    year: int,
    month: int,
    *,
    schedule_id: str | None = None,
) -> dict | None:
    """그 달 표에 실제로 들어간 보건휴가·수면OFF 를 사람·건수로 요약.

    Returns:
        {"year","month","health_leave": {...}, "sleep_off": {...}, "message": str}
        타깃 코드가 하나도 설정돼 있지 않으면(기능 미사용) None.
    """
    codes = resolve_leave_codes(db, group_id)
    if not codes:
        return None

    sched = None
    if schedule_id:
        sched = db.query(Schedule).filter(Schedule.schedule_id == schedule_id).first()
    if sched is None:
        sched = _latest_schedule(db, group_id, year, month)
    if sched is None:
        return {
            "year": year,
            "month": month,
            "found": False,
            "message": f"{year}년 {month}월 근무표가 아직 없어 휴가 부여 결과를 볼 수 없어요.",
        }

    code_to_kind = {v["code"]: k for k, v in codes.items()}
    rows = (
        db.query(ScheduleEntry.nurse_id, ScheduleEntry.shift_id, ScheduleEntry.work_date, Nurse.name)
        .outerjoin(Nurse, Nurse.nurse_id == ScheduleEntry.nurse_id)
        .filter(
            ScheduleEntry.schedule_id == sched.schedule_id,
            ScheduleEntry.shift_id.in_(list(code_to_kind)),
        )
        .all()
    )

    buckets: dict[str, dict[str, list[int]]] = {k: {} for k in codes}
    for nurse_id, shift_id, work_date, name in rows:
        kind = code_to_kind.get(shift_id)
        if kind is None:
            continue
        day = work_date.day if isinstance(work_date, date) else None
        buckets[kind].setdefault(name or str(nurse_id), []).append(day)

    out: dict = {"year": year, "month": month, "found": True}
    for kind, meta in codes.items():
        per_nurse = buckets[kind]
        out[kind] = {
            "code": meta["code"],
            "name": meta["name"],
            "granted_count": sum(len(v) for v in per_nurse.values()),
            "nurse_count": len(per_nurse),
            "nurses": [
                {"nurse": n, "days": sorted(d for d in days if d)}
                for n, days in sorted(per_nurse.items())
            ],
        }
    out["message"] = _compose(out, codes)
    # 되읽기로는 알 수 없는 축을 명시한다 — LLM 이 "이월 없음" 으로 단정하지 않도록.
    out["note"] = (
        "확정된 근무표에서 센 값입니다. 수면OFF 가 자리를 못 찾아 다음 달로 이월된 건수는 "
        "표에 남지 않아 여기 포함되지 않습니다."
    )
    return out


def _compose(summary: dict, codes: dict[str, dict]) -> str:
    label = {"health_leave": "보건휴가", "sleep_off": "수면OFF"}
    parts = []
    for kind in codes:
        s = summary[kind]
        if s["granted_count"]:
            parts.append(f"{label[kind]} {s['nurse_count']}명 {s['granted_count']}건")
        else:
            parts.append(f"{label[kind]} 없음")
    return (
        f"{summary['year']}년 {summary['month']}월 자동 부여 결과 — " + ", ".join(parts) + "."
    )
