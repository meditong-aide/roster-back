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


def compare_schedules(db: Session, src: Schedule, dst: Schedule) -> dict:
    """두 근무표(같은 병동·같은 달)의 **현재 칸**을 비교한 응답.

    응답: `{from, to, group_id, year, month, summary:{changed_cells, affected_nurses,
    added_nurses, removed_nurses}, cells:[{nurse_id, nurse_name, date, from, to}],
    by_nurse:[{nurse_id, nurse_name, changed, from_counts, to_counts}],
    added_nurses:[{nurse_id, nurse_name, cells}], removed_nurses:[…]}`.
    `from`/`to` 칸 값은 `{shift_id, color}` 이고 빈 칸이면 null.
    """
    before = snapshot_entries(db, src.schedule_id)
    after = snapshot_entries(db, dst.schedule_id)
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
