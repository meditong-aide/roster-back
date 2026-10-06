"""근무표 두 상태의 칸 비교 — ③ 버전 간 비교와 ② 재마감 변경 계산이 같은 함수를 쓴다.

★ 비교 단위는 (간호사, 날짜) 칸의 **근무코드**다. 코드가 같으면 `shifts.id` 가 달라도(같은 코드의
  중복 행) 같다고 본다 — 사람이 보는 것은 코드다.
★ 간호사가 한쪽에만 있으면 칸 변경이 아니라 **명단 변경**으로 따로 센다. 행이 통째로 생기거나
  빠진 것을 칸 수십 개 변경으로 세면 "바뀐 칸" 이 부풀어 실제 수정이 묻힌다(설계 9-28).
★ D→E→D 처럼 결국 같아진 칸은 변경이 아니다 — 두 상태만 비교하므로 자동으로 그렇게 된다.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import date
from typing import Optional

from sqlalchemy.orm import Session

from db.models import Nurse, Schedule, Shift
from services.schedule_history_service import CellMap, snapshot_entries


@dataclass
class CellDiff:
    """두 칸 지도의 차이. `changed` 는 양쪽 명단에 다 있는 간호사의 칸만 담는다."""

    #: (nurse_id, 날짜, 이전 코드, 바뀐 코드) — 코드 None 은 빈 칸
    changed: list[tuple[str, date, Optional[str], Optional[str]]] = field(default_factory=list)
    added_nurses: list[str] = field(default_factory=list)     # 뒤쪽에만 있는 간호사
    removed_nurses: list[str] = field(default_factory=list)   # 앞쪽에만 있는 간호사

    @property
    def affected_nurses(self) -> list[str]:
        """칸이 하나라도 바뀐 간호사(명단 변경 제외)."""
        return sorted({n for n, _, _, _ in self.changed})


def diff_cell_maps(before: CellMap, after: CellMap) -> CellDiff:
    """`before` → `after` 칸 차이. 명단에 한쪽만 있는 간호사는 명단 변경으로 분리한다."""
    b_nurses = {n for n, _ in before}
    a_nurses = {n for n, _ in after}
    common = b_nurses & a_nurses
    changed = []
    for cell in set(before) | set(after):
        nurse_id, day = cell
        if nurse_id not in common:
            continue
        b_code = (before.get(cell) or (None, None))[0]
        a_code = (after.get(cell) or (None, None))[0]
        if b_code != a_code:
            changed.append((nurse_id, day, b_code, a_code))
    changed.sort(key=lambda c: (c[0], c[1]))
    return CellDiff(
        changed=changed,
        added_nurses=sorted(a_nurses - b_nurses),
        removed_nurses=sorted(b_nurses - a_nurses),
    )


def diff_rows(before: CellMap, b_colors: dict, after: CellMap, a_colors: dict) -> tuple[list[dict], CellDiff]:
    """두 칸 지도의 차이를 화면 행으로 — 바뀐 칸(kind='cell') + 명단에 생기거나 빠진 간호사의 칸
    (kind='added'|'removed'). 색 사전은 칸 `(nurse_id, 날짜)` → 색.

    ② 재마감 변경 알림과 버전 변경 조회(`schedule_changes`)가 **같은 행 모양**을 쓴다 — 화면 컴포넌트 하나로
    그리게(10-06 사용자 결정).
    """
    diff = diff_cell_maps(before, after)
    rows = [_diff_row(n, d, "cell", b, a, b_colors, a_colors) for n, d, b, a in diff.changed]
    for kind, ids, src in (("added", diff.added_nurses, after), ("removed", diff.removed_nurses, before)):
        wanted = set(ids)
        for (nurse_id, day), (code, _) in sorted(src.items(), key=lambda kv: kv[0]):
            if nurse_id in wanted:
                b, a = (None, code) if kind == "added" else (code, None)
                rows.append(_diff_row(nurse_id, day, kind, b, a, b_colors, a_colors))
    return rows, diff


def _diff_row(nurse_id, day, kind, b, a, b_colors, a_colors) -> dict:
    return {
        "nurse_id": nurse_id, "work_date": day, "kind": kind,
        "before_code": b, "before_color": b_colors.get((nurse_id, day)) if b else None,
        "after_code": a, "after_color": a_colors.get((nurse_id, day)) if a else None,
    }


def diff_cells_payload(rows: list[dict], names: dict) -> list[dict]:
    """`diff_rows` 행 → 응답 칸 `{nurse_id, nurse_name, date, kind, before, after}` (간호사·날짜순)."""
    return [{
        "nurse_id": r["nurse_id"], "nurse_name": names.get(str(r["nurse_id"])),
        "date": r["work_date"].isoformat(), "kind": r["kind"],
        "before": {"shift_id": r["before_code"], "color": r["before_color"]} if r["before_code"] else None,
        "after": {"shift_id": r["after_code"], "color": r["after_color"]} if r["after_code"] else None,
    } for r in sorted(rows, key=lambda r: (str(r["nurse_id"]), r["work_date"]))]


def _group_shift_colors(db: Session, group_id: str) -> dict[str, Optional[str]]:
    """근무코드 → 색. 같은 코드 중복 행은 목록 조회와 같은 (sequence, id) 첫 행을 쓴다."""
    out: dict[str, Optional[str]] = {}
    rows = (
        db.query(Shift.shift_id, Shift.color)
        .filter(Shift.group_id == group_id)
        .order_by(Shift.sequence.asc(), Shift.id.asc())
        .all()
    )
    for shift_id, color in rows:
        out.setdefault(shift_id, color)
    return out


def _nurse_names(db: Session, nurse_ids: set[str]) -> dict[str, str]:
    if not nurse_ids:
        return {}
    rows = db.query(Nurse.nurse_id, Nurse.name).filter(Nurse.nurse_id.in_(list(nurse_ids))).all()
    return {str(n): name for n, name in rows}


def _codes_by_nurse(cells: CellMap) -> dict[str, Counter]:
    out: dict[str, Counter] = {}
    for (nurse_id, _), (code, _) in cells.items():
        if code:
            out.setdefault(nurse_id, Counter())[code] += 1
    return out


def _schedule_brief(s: Schedule) -> dict:
    return {"schedule_id": s.schedule_id, "version": s.version, "name": s.name, "status": s.status}


def _visible_cells(db: Session, cells: CellMap) -> CellMap:
    """간호사 행이 있는 사람의 칸만. 간호사 행이 지워진 사람의 칸은 버전에 남아 있어도 근무표 화면·마감
    스냅샷 어디에도 안 보인다 — 비교에 넣으면 아무도 못 보는 칸이 '추가'로 잡힌다(dev 실측 30칸 · 2026-10-06).
    `/roster/compare` 와 버전 변경 조회가 같이 쓴다(둘 숫자가 같아야 한다)."""
    ids = {n for n, _ in cells}
    alive = {str(n) for (n,) in db.query(Nurse.nurse_id).filter(Nurse.nurse_id.in_(list(ids))).all()} if ids else set()
    return {k: v for k, v in cells.items() if k[0] in alive}


def shown_codes(db: Session, group_id: str, cells: CellMap) -> CellMap:
    """칸 코드를 **화면에 보이는 코드**로 — 저장된 코드가 이 병동에 **없는 옛 이름**이면 id 가 가리키는 행의 지금 코드.
    근무코드는 같은 id 에서 이름만 바뀐다(실측 N→N1·O→OFF). 칸에는 옛 이름이 남지만 근무표 화면(프론트)과 마감
    스냅샷(`roster_service` 가 entry.id 로 복원)은 지금 코드로 보여 준다 — 저장값 그대로 비교하면 손대지 않은 칸이
    전부 바뀐 칸으로 잡힌다(Codex 3회차 MEDIUM).
    ★ 마감 스냅샷 칸에도 쓴다(`schedule_ids` 에 id 가 있다) — 마감 뒤 이름만 바꾼 근무는 **실제 근무가 바뀐 게 아니라**
      변경 조회·재마감 알림에 잡히면 안 된다(10-06 사용자 결정: 이름 변경은 알릴 이유가 없고 실제 근무 변경만).
    ★ 저장 코드가 이 병동에 **있는** 코드면 id 가 다른 행을 가리켜도 코드를 따른다(프론트 해석과 같다). id 가
      어긋난 칸이 실제로 있다 — dev 9월 중환자실 189칸, id 우선으로 읽으면 마감본 대비 0→189 거짓 변경."""
    rows = db.query(Shift.id, Shift.shift_id).filter(Shift.group_id == group_id).all()
    codes = {c for _, c in rows}
    by_id = {i: c for i, c in rows if i is not None}
    return {cell: ((by_id.get(int_id, code) if code and code != "-" and code not in codes else code), int_id)
            for cell, (code, int_id) in cells.items()}


def schedule_cells(db: Session, s: Schedule) -> CellMap:
    """버전의 지금 칸을 화면에 보이는 그대로(간호사 행 있는 사람만 · 코드는 id 기준).
    `/roster/compare`·버전 변경 조회·새 버전 저장 출처 요약이 같이 쓴다(셋 숫자가 같아야 한다)."""
    return shown_codes(db, s.group_id, _visible_cells(db, snapshot_entries(db, s.schedule_id)))


def compare_schedules(db: Session, src: Schedule, dst: Schedule) -> dict:
    """두 근무표(같은 병동·같은 달)의 **현재 칸**을 비교한 응답.

    응답: `{from, to, group_id, year, month, summary:{changed_cells, affected_nurses,
    added_nurses, removed_nurses}, cells:[{nurse_id, nurse_name, date, from, to}],
    by_nurse:[{nurse_id, nurse_name, changed, from_counts, to_counts}],
    added_nurses:[{nurse_id, nurse_name, cells}], removed_nurses:[…]}`.
    `from`/`to` 칸 값은 `{shift_id, color}` 이고 빈 칸이면 null.
    """
    before = schedule_cells(db, src)
    after = schedule_cells(db, dst)
    diff = diff_cell_maps(before, after)
    colors = _group_shift_colors(db, dst.group_id)
    names = _nurse_names(db, set(diff.affected_nurses) | set(diff.added_nurses) | set(diff.removed_nurses))

    def _cell(code: Optional[str]) -> Optional[dict]:
        return {"shift_id": code, "color": colors.get(code)} if code else None

    b_counts, a_counts = _codes_by_nurse(before), _codes_by_nurse(after)
    per_nurse = Counter(n for n, _, _, _ in diff.changed)

    def _members(ids: list[str], cells: CellMap) -> list[dict]:
        sizes = Counter(n for n, _ in cells)
        return [{"nurse_id": n, "nurse_name": names.get(n), "cells": sizes.get(n, 0)} for n in ids]

    return {
        "from": _schedule_brief(src),
        "to": _schedule_brief(dst),
        "group_id": dst.group_id,
        "year": dst.year,
        "month": dst.month,
        "summary": {
            "changed_cells": len(diff.changed),
            "affected_nurses": len(diff.affected_nurses),
            "added_nurses": len(diff.added_nurses),
            "removed_nurses": len(diff.removed_nurses),
        },
        "cells": [
            {"nurse_id": n, "nurse_name": names.get(n), "date": d.isoformat(),
             "from": _cell(b), "to": _cell(a)}
            for n, d, b, a in diff.changed
        ],
        "by_nurse": [
            {"nurse_id": n, "nurse_name": names.get(n), "changed": per_nurse[n],
             "from_counts": dict(b_counts.get(n, {})), "to_counts": dict(a_counts.get(n, {}))}
            for n in diff.affected_nurses
        ],
        "added_nurses": _members(diff.added_nurses, after),
        "removed_nurses": _members(diff.removed_nurses, before),
    }


def schedule_changes(db: Session, schedule: Schedule) -> dict:
    """이 버전이 **기준 대비** 무엇이 바뀌었는지(성남 ③ — 사용자 결정 10-06).

    기준 고르기 — "가장 최근에 마감된 상태, 마감된 적이 없으면 원본":
      1) 이 버전에 마감 기록이 있으면 그 **최근 마감 스냅샷**(철회돼 비활성이어도). 마감 → 철회 → 수정한
         경우 원본이 아니라 **그 마감본 대비 새로 바뀐 칸**을 보여 준다.
      2) 없으면 [새 버전으로 저장]의 **출처 버전**(지금 칸 — `/roster/compare` 와 같다).
      3) 둘 다 없으면 기준 없음(`base` null · 칸 없음). 출처 버전이 지워졌으면 `base.kind='source_missing'`.
    ★ 마감 → 철회 → 같은 버전 수정에서는 마감 당시 칸이 스냅샷에만 남는다(버전 칸은 덮어써진다).
      그래서 `/roster/compare`(두 버전의 지금 칸)로는 재마감 전에 '마감본 대비 바뀐 칸'을 볼 수 없었다.
    응답 칸 모양은 ② 변경 알림(`/roster/change-notices`)과 같다(`diff_rows`·`diff_cells_payload`).
    색: 마감 스냅샷 쪽은 그 마감 시점 색, 버전 쪽은 지금 병동 색.
    """
    from db.models import IssuedRosterSnapshot, ScheduleLineage
    from services.roster_change_notice_service import snapshot_cells

    code_colors = _group_shift_colors(db, schedule.group_id)

    def _cell_colors(cells: CellMap) -> dict:
        return {cell: code_colors.get(code) for cell, (code, _) in cells.items()}

    after = schedule_cells(db, schedule)
    before: CellMap = {}
    b_colors: dict = {}
    base: Optional[dict] = None
    snap = (
        db.query(IssuedRosterSnapshot)
        .filter(IssuedRosterSnapshot.schedule_id == schedule.schedule_id)
        .order_by(IssuedRosterSnapshot.snapshot_id.desc())
        .first()
    )
    if snap is not None:
        before, b_colors = snapshot_cells(snap)
        before = shown_codes(db, schedule.group_id, before)
        base = {"kind": "issued", "snapshot_id": snap.snapshot_id, "schedule_id": schedule.schedule_id,
                "version": snap.version, "issued_at": snap.created_at.isoformat() if snap.created_at else None,
                "is_active_issued": bool(snap.is_active_issued)}
    else:
        lineage = db.get(ScheduleLineage, schedule.schedule_id)
        src = db.get(Schedule, lineage.source_schedule_id) if lineage is not None else None
        if src is not None and not src.dropped and (src.group_id, src.year, src.month) == (
                schedule.group_id, schedule.year, schedule.month):
            before = schedule_cells(db, src)
            b_colors = _cell_colors(before)
            base = dict(_schedule_brief(src), kind="source")
        elif lineage is not None:
            base = {"kind": "source_missing", "schedule_id": lineage.source_schedule_id,
                    "version": lineage.source_version}
    rows, diff = (diff_rows(before, b_colors, after, _cell_colors(after))
                  if base is not None and base["kind"] in ("issued", "source") else ([], CellDiff()))
    names = _nurse_names(db, {str(r["nurse_id"]) for r in rows})
    return {
        "schedule": _schedule_brief(schedule),
        "group_id": schedule.group_id,
        "year": schedule.year,
        "month": schedule.month,
        "base": base,
        "summary": {
            "changed_cells": len(diff.changed),
            "affected_nurses": len(diff.affected_nurses),
            "added_nurses": len(diff.added_nurses),
            "removed_nurses": len(diff.removed_nurses),
            "total_cells": len(rows),
        },
        "cells": diff_cells_payload(rows, names),
    }
