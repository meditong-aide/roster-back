"""퇴사자 대응 부분 재생성 오케스트레이션 서비스."""

from __future__ import annotations

import calendar
import uuid
from datetime import date, datetime, timedelta
from typing import Any

from fastapi import HTTPException
from sqlalchemy import or_
from sqlalchemy.orm import Session

from db.models import (
    Nurse,
    NursePrecepteePeriod,
    RosterConfig,
    Schedule,
    ScheduleEntry,
    Shift,
)
from schemas.roster_schema import RosterRequest
from services.group_access import assert_caller_can_access_group, caller_is_head_nurse
from services.preceptee_period import open_preceptee_period
from services.roster_create_service import generate_roster_service


def _shift_meta(db: Session, office_id: str, group_id: str) -> tuple[dict[str, dict], dict[str, dict]]:
    rows = (
        db.query(Shift)
        .filter(Shift.office_id == office_id, Shift.group_id == group_id)
        .all()
    )
    by_code: dict[str, dict] = {}
    by_pk: dict[str, dict] = {}
    for shift in rows:
        meta = {
            "shift_id": shift.shift_id,
            "name": shift.name,
            "type": shift.type,
            "shift_gb": shift.shift_gb,
            "default_shift": shift.default_shift,
        }
        by_code[str(shift.shift_id)] = meta
        by_code[str(shift.shift_id).upper()] = meta
        if shift.id is not None:
            by_pk[str(shift.id)] = meta
    return by_code, by_pk


def _shift_name(shift_id: str, shift_meta: dict[str, dict]) -> str:
    if shift_id in (None, "-"):
        return "-"
    meta = shift_meta.get(str(shift_id)) or shift_meta.get(str(shift_id).upper())
    return str((meta or {}).get("name") or shift_id)


def _is_off(shift_id: str, shift_meta: dict[str, dict]) -> bool:
    if shift_id in (None, "-"):
        return False
    code = str(shift_id).strip().upper()
    if code in {"O", "OFF", "주"}:
        return True
    meta = shift_meta.get(str(shift_id)) or shift_meta.get(code) or {}
    return str(meta.get("type") or "").strip() in {"휴무", "휴가", "공가"}


def _is_work(shift_id: str, shift_meta: dict[str, dict]) -> bool:
    if shift_id in (None, "-"):
        return False
    meta = shift_meta.get(str(shift_id)) or shift_meta.get(str(shift_id).upper()) or {}
    if str(meta.get("type") or "").strip() == "근무":
        return True
    return str(shift_id).strip().upper() in {"D", "E", "M", "N"}


def _change_kind(
    *,
    nurse_id: str,
    resigned_nurse_id: str,
    before: str,
    after: str,
    shift_meta: dict[str, dict],
) -> str:
    if nurse_id == resigned_nurse_id:
        return "resigned"
    before_off = _is_off(before, shift_meta)
    after_off = _is_off(after, shift_meta)
    if before_off and _is_work(after, shift_meta):
        return "off_to_work"
    if _is_work(before, shift_meta) and after_off:
        return "work_to_off"
    return "shift_change"


def _entries_grid(entries: list[ScheduleEntry], days_in_month: int) -> dict[str, list[str]]:
    grid: dict[str, list[str]] = {}
    for entry in entries:
        nurse_id = str(entry.nurse_id)
        grid.setdefault(nurse_id, ["-"] * days_in_month)
        day_idx = entry.work_date.day - 1
        if 0 <= day_idx < days_in_month:
            grid[nurse_id][day_idx] = str(entry.shift_id)
    return grid


def _main_code(shift_id: Any, shift_meta: dict[str, dict]) -> str:
    """shift_id를 커버리지 집계용 main 코드(D/E/N/M/O)로 정규화한다."""
    code = str(shift_id or "").strip().upper()
    if code in {"", "-", "O", "OFF", "주"}:
        return "O"
    meta = shift_meta.get(str(shift_id)) or shift_meta.get(code) or {}
    if str(meta.get("type") or "").strip() in {"휴무", "휴가", "공가"}:
        return "O"
    gb = str(meta.get("shift_gb") or "").strip().upper()
    if gb in {"D", "데이"}:
        return "D"
    if gb in {"E", "이브닝"}:
        return "E"
    if gb in {"N", "나이트"}:
        return "N"
    if gb in {"M", "미드"}:
        return "M"
    if code in {"D", "E", "N", "M"}:
        return code
    return "O"


def _coverage_shortfall_warnings(
    *,
    db: Session,
    draft_schedule: Schedule,
    draft_grid: dict[str, list[str]],
    cutoff_idx: int,
    days_in_month: int,
    shift_meta: dict[str, dict],
) -> list[str]:
    """draft의 cutoff 이후 일자별 D/E/N(/M) 커버리지가 config 요구치 미달인지 스캔한다.

    §6.1 '조용한 미달 가시화': 퇴사로 빈자리를 못 채워 NOD/NOE가 남거나 팀/등급이
    조용히 미달돼도 커버리지 수준으로 드러나므로, 관리자에게 명시적 경고로 노출한다.
    """
    cfg = (
        db.query(RosterConfig)
        .filter(RosterConfig.config_id == draft_schedule.config_id)
        .first()
    )
    if cfg is None:
        return []
    req: dict[str, int] = {
        "D": int(getattr(cfg, "day_req", 0) or 0),
        "E": int(getattr(cfg, "eve_req", 0) or 0),
        "N": int(getattr(cfg, "nig_req", 0) or 0),
    }
    if bool(getattr(cfg, "use_mid", False)):
        req["M"] = int(getattr(cfg, "mid_req", 0) or 0)
    req = {code: need for code, need in req.items() if need > 0}
    if not req:
        return []

    label = {"D": "데이", "E": "이브닝", "N": "나이트", "M": "미드"}
    warnings: list[str] = []
    for day_idx in range(cutoff_idx, days_in_month):
        counts: dict[str, int] = {}
        for row in draft_grid.values():
            if day_idx >= len(row):
                continue
            main = _main_code(row[day_idx], shift_meta)
            if main in req:
                counts[main] = counts.get(main, 0) + 1
        for code, need in req.items():
            have = counts.get(code, 0)
            if have < need:
                dt = date(draft_schedule.year, draft_schedule.month, day_idx + 1)
                warnings.append(
                    f"{dt.isoformat()} {label.get(code, code)} 커버리지 미달 ({have}/{need})"
                )
    return warnings


def build_resignation_diff_response(
    *,
    db: Session,
    base_schedule: Schedule,
    draft_schedule: Schedule,
    resigned_nurse: Nurse,
    cutoff_date: date,
) -> dict[str, Any]:
    """원본 schedule과 새 draft schedule의 cutoff 이후 변경분을 응답 스키마로 만든다."""
    days_in_month = calendar.monthrange(base_schedule.year, base_schedule.month)[1]
    cutoff_idx = max(0, min(days_in_month, cutoff_date.day - 1))
    base_entries = (
        db.query(ScheduleEntry)
        .filter(ScheduleEntry.schedule_id == base_schedule.schedule_id)
        .all()
    )
    draft_entries = (
        db.query(ScheduleEntry)
        .filter(ScheduleEntry.schedule_id == draft_schedule.schedule_id)
        .all()
    )
    base_grid = _entries_grid(base_entries, days_in_month)
    draft_grid = _entries_grid(draft_entries, days_in_month)
    nurse_ids = sorted(set(base_grid) | set(draft_grid))
    nurses = (
        db.query(Nurse)
        .filter(Nurse.nurse_id.in_(nurse_ids))
        .all()
        if nurse_ids else []
    )
    nurse_meta = {
        str(n.nurse_id): {
            "nurse_id": str(n.nurse_id),
            "name": n.name,
            "grade": getattr(n, "grade", None),
            "team_id": getattr(n, "team_id", None),
        }
        for n in nurses
    }
    shift_meta, _shift_meta_by_pk = _shift_meta(
        db, base_schedule.office_id, base_schedule.group_id
    )

    changed_nurses: list[dict[str, Any]] = []
    cells_changed = 0
    touched_non_resigned = 0
    resigned_id_str = str(resigned_nurse.nurse_id)
    for nurse_id in nurse_ids:
        is_resigned = nurse_id == resigned_id_str
        before_row = base_grid.get(nurse_id, ["-"] * days_in_month)
        after_row = draft_grid.get(nurse_id, ["-"] * days_in_month)
        changes: list[dict[str, Any]] = []
        for day_idx in range(cutoff_idx, days_in_month):
            before = before_row[day_idx]
            after = after_row[day_idx]
            if before == after:
                continue
            # cells_changed는 앵커 ChangeCells(비퇴사자 자유 셀)와 일치시킨다.
            # 퇴사자의 vacated 셀은 churn이 아니라 결원이므로 카운트 제외(표시는 유지).
            if not is_resigned:
                cells_changed += 1
            changes.append(
                {
                    "day": day_idx + 1,
                    "date": date(base_schedule.year, base_schedule.month, day_idx + 1).isoformat(),
                    "from": before,
                    "from_name": _shift_name(before, shift_meta),
                    "to": after,
                    "to_name": _shift_name(after, shift_meta),
                    "kind": _change_kind(
                        nurse_id=nurse_id,
                        resigned_nurse_id=str(resigned_nurse.nurse_id),
                        before=before,
                        after=after,
                        shift_meta=shift_meta,
                    ),
                }
            )
        if changes:
            if not is_resigned:
                touched_non_resigned += 1
            meta = nurse_meta.get(
                nurse_id,
                {"nurse_id": nurse_id, "name": nurse_id, "grade": None, "team_id": None},
            )
            changed_nurses.append(
                {
                    **meta,
                    "num_changes": len(changes),
                    "changes": changes,
                }
            )

    changed_nurses.sort(key=lambda item: (-int(item["num_changes"]), str(item["name"])))
    coverage_warnings = _coverage_shortfall_warnings(
        db=db,
        draft_schedule=draft_schedule,
        draft_grid=draft_grid,
        cutoff_idx=cutoff_idx,
        days_in_month=days_in_month,
        shift_meta=shift_meta,
    )
    return {
        "schedule_id": draft_schedule.schedule_id,
        "base_schedule_id": base_schedule.schedule_id,
        "cutoff_date": cutoff_date.isoformat(),
        "resigned_nurse": {
            "nurse_id": str(resigned_nurse.nurse_id),
            "name": resigned_nurse.name,
        },
        "summary": {
            "nurses_touched": touched_non_resigned,
            "cells_changed": cells_changed,
            "warnings": coverage_warnings,
        },
        "changed_nurses": changed_nurses,
    }


def _transfer_or_close_preceptee_periods(
    *,
    db: Session,
    resigned_nurse: Nurse,
    cutoff_date: date,
    replacement_preceptor_id: str | None,
) -> list[str]:
    """퇴사자가 프리셉터인 period를 cutoff부터 이관하거나 종료한다."""
    rows = (
        db.query(NursePrecepteePeriod)
        .filter(
            NursePrecepteePeriod.preceptor_id == str(resigned_nurse.nurse_id),
            NursePrecepteePeriod.valid_from < cutoff_date,
            or_(
                NursePrecepteePeriod.valid_to.is_(None),
                NursePrecepteePeriod.valid_to > cutoff_date,
            ),
        )
        .all()
    )
    warnings: list[str] = []
    if not rows:
        return warnings

    replacement = None
    if replacement_preceptor_id:
        replacement = (
            db.query(Nurse)
            .filter(
                Nurse.nurse_id == str(replacement_preceptor_id),
                Nurse.group_id == resigned_nurse.group_id,
                Nurse.active == 1,
            )
            .first()
        )
        if replacement is None:
            raise HTTPException(status_code=400, detail="replacement_preceptor_id가 유효하지 않습니다.")

    for row in rows:
        original_valid_to = row.valid_to
        row.valid_to = max(cutoff_date, row.valid_from)
        row.end_reason = "preceptor_transfer"
        if replacement is not None:
            open_preceptee_period(
                db,
                nurse_id=row.nurse_id,
                preceptor_id=replacement.nurse_id,
                office_id=row.office_id,
                valid_from=cutoff_date,
                valid_to=original_valid_to,
                source_assignment_id=row.source_assignment_id,
                source="resign_resolve",
            )
            warnings.append(f"{row.nurse_id} 프리셉티 프리셉터를 {replacement.name}로 이관했습니다.")
        else:
            warnings.append(f"{row.nurse_id} 프리셉티 프리셉터 follow를 cutoff 이후 해제했습니다.")
    return warnings


def _restore_prefix_from_base(
    *,
    db: Session,
    base_schedule: Schedule,
    draft_schedule: Schedule,
    cutoff_date: date,
) -> int:
    """동결 prefix(cutoff 이전)를 원본 entries로 verbatim 복원한다.

    솔버는 prefix를 main-code(D/E/N/O)로 재저장하므로 원본의 특정 변형(D2 등)이나
    수동 편집을 잃을 수 있다. cutoff 이전은 이미 확정된 과거라 원본과 100% 동일해야
    하므로, draft의 prefix entries를 지우고 원본 entries를 그대로 복사한다.
    (diff는 cutoff 이후만 비교하므로 복원은 diff에 영향 없음.)
    """
    cutoff_dt = datetime.combine(cutoff_date, datetime.min.time())
    db.query(ScheduleEntry).filter(
        ScheduleEntry.schedule_id == draft_schedule.schedule_id,
        ScheduleEntry.work_date < cutoff_dt,
    ).delete(synchronize_session=False)
    base_prefix = (
        db.query(ScheduleEntry)
        .filter(
            ScheduleEntry.schedule_id == base_schedule.schedule_id,
            ScheduleEntry.work_date < cutoff_dt,
        )
        .all()
    )
    for entry in base_prefix:
        db.add(
            ScheduleEntry(
                entry_id=str(uuid.uuid4().hex)[:16],
                schedule_id=draft_schedule.schedule_id,
                nurse_id=entry.nurse_id,
                work_date=entry.work_date,
                shift_id=entry.shift_id,
                id=entry.id,
            )
        )
    db.flush()
    return len(base_prefix)


def partial_resolve_on_resignation(
    *,
    db: Session,
    current_user,
    schedule_id: str,
    resigned_nurse_id: str,
    cutoff_date: date,
    replacement_preceptor_id: str | None = None,
) -> dict[str, Any]:
    """퇴사자 cutoff 이후만 재최적화하고 새 draft와 diff 응답을 반환한다."""
    if not current_user or not caller_is_head_nurse(db, current_user):
        raise HTTPException(status_code=403, detail="Permission denied")
    base_schedule = (
        db.query(Schedule)
        .filter(Schedule.schedule_id == str(schedule_id), Schedule.dropped == False)  # noqa: E712
        .first()
    )
    if base_schedule is None:
        raise HTTPException(status_code=404, detail="schedule을 찾을 수 없습니다.")
    assert_caller_can_access_group(db, current_user, base_schedule.group_id)
    current_user.group_id = base_schedule.group_id

    month_start = date(base_schedule.year, base_schedule.month, 1)
    month_end = date(
        base_schedule.year,
        base_schedule.month,
        calendar.monthrange(base_schedule.year, base_schedule.month)[1],
    )
    if cutoff_date < month_start or cutoff_date > month_end:
        raise HTTPException(status_code=400, detail="cutoff_date가 schedule 월 범위를 벗어났습니다.")

    resigned_nurse = (
        db.query(Nurse)
        .filter(
            Nurse.nurse_id == str(resigned_nurse_id),
            Nurse.group_id == base_schedule.group_id,
        )
        .first()
    )
    if resigned_nurse is None:
        raise HTTPException(status_code=404, detail="퇴사 간호사를 찾을 수 없습니다.")
    # resignation_date는 엔진 active-range 규약상 'inclusive last-active day'로 해석된다.
    # cutoff 당일부터 제외하려면 마지막 근무일 = cutoff 전날을 저장해야, 부분 재생성뿐 아니라
    # 이후 같은 달 일반 재생성에서도 퇴사자가 cutoff 당일 근무하는 off-by-one이 생기지 않는다.
    last_active_day = cutoff_date - timedelta(days=1)
    resigned_nurse.resignation_date = datetime.combine(last_active_day, datetime.min.time())
    db.flush()

    period_warnings = _transfer_or_close_preceptee_periods(
        db=db,
        resigned_nurse=resigned_nurse,
        cutoff_date=cutoff_date,
        replacement_preceptor_id=replacement_preceptor_id,
    )

    req = RosterRequest(
        year=base_schedule.year,
        month=base_schedule.month,
        group_id=base_schedule.group_id,
        config_id=base_schedule.config_id,
    )
    roster_data = generate_roster_service(
        req,
        current_user,
        db,
        partial_resolve_context={
            "base_schedule_id": base_schedule.schedule_id,
            "resigned_nurse_id": str(resigned_nurse_id),
            "cutoff_date": cutoff_date,
            "w_cell": 1,
            "w_nurse": 5,
        },
    )
    draft_schedule_id = str((roster_data or {}).get("schedule_id") or "")
    draft_schedule = (
        db.query(Schedule)
        .filter(Schedule.schedule_id == draft_schedule_id)
        .first()
    )
    if draft_schedule is None:
        raise HTTPException(status_code=500, detail="부분 재생성 draft 저장 결과를 찾을 수 없습니다.")

    # 동결 prefix를 원본과 100% 동일하게 복원(변형/수동편집 보존).
    _restore_prefix_from_base(
        db=db,
        base_schedule=base_schedule,
        draft_schedule=draft_schedule,
        cutoff_date=cutoff_date,
    )

    diff = build_resignation_diff_response(
        db=db,
        base_schedule=base_schedule,
        draft_schedule=draft_schedule,
        resigned_nurse=resigned_nurse,
        cutoff_date=cutoff_date,
    )
    # 커버리지 미달 경고(diff에서 계산) + 프리셉터 이관 + infeasibility 진단을 합친다.
    warnings = list(diff.get("summary", {}).get("warnings") or [])
    warnings.extend(period_warnings)
    try:
        infeasibility = (roster_data or {}).get("infeasibility") or {}
        msg = infeasibility.get("summary_message_ko")
        if msg:
            warnings.append(str(msg))
    except Exception:
        pass
    diff["summary"]["warnings"] = warnings
    diff["roster"] = roster_data
    return diff
