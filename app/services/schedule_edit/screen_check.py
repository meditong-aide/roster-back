"""후보가 근무표 화면에 '새 빨간 칸'을 만들지 않는지 — 화면 판정(`screen_rules`)으로 한 번 더 본다.

`rules.py` 는 엔진 기준이라 화면과 판정이 다른 곳이 있다(화면 버릇 목록은 screen_rules 머리말).
그래서 후보를 적용한 근무표를 화면 판정에 그대로 넣어, 지금 근무표보다 나빠진 곳이 있으면 버린다.
  · 새 위치의 위반(열쇠가 새로 생김)
  · 같은 위치인데 더 심해짐 — 화면 열쇠에는 수치가 없어 따로 잰다(미달 1→2명, 월 N 16→17회).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Tuple

from sqlalchemy.orm import Session

from services.schedule_edit.screen_rules import (
    CellIds, Cells, ScreenInputs, ScreenViolation, load_schedule_cells, load_screen_inputs,
    screen_violations,
)


@dataclass
class ScreenBaseline:
    inputs: ScreenInputs
    cells: Cells
    cell_ids: CellIds
    before: List[ScreenViolation]
    #: 칸에 쓸 코드 → shifts.id (화면이 숫자 id 로 칸을 해석하므로 같이 바꾼다)
    id_of: Dict[str, Optional[int]]


def load_screen_baseline(db: Session, schedule) -> ScreenBaseline:
    inputs = load_screen_inputs(db, schedule)
    cells, cell_ids = load_schedule_cells(db, schedule.schedule_id)
    id_of: Dict[str, Optional[int]] = {}
    for s in sorted(inputs.shifts, key=lambda s: (s.sequence or 0)):
        id_of.setdefault(s.shift_id, s.id)
    return ScreenBaseline(inputs=inputs, cells=cells, cell_ids=cell_ids,
                          before=screen_violations(cells, cell_ids, inputs), id_of=id_of)


def screen_regressions(base: ScreenBaseline,
                       changes: Iterable[Tuple[str, int, str]]) -> List[ScreenViolation]:
    """(nurse_id, day, 새 코드) 를 적용했을 때 화면에서 나빠지는 위반."""
    cells = {n: list(row) for n, row in base.cells.items()}
    ids = {n: list(row) for n, row in base.cell_ids.items()}
    for nurse_id, day, code in changes:
        if nurse_id not in cells:
            continue
        cells[nurse_id][day] = code
        ids[nurse_id][day] = base.id_of.get(code)
    after = screen_violations(cells, ids, base.inputs)
    before = {v.key: _severity(v) for v in base.before}
    return [v for v in after if v.key not in before or _severity(v) > before[v.key]]


def _severity(v: ScreenViolation) -> float:
    """같은 위치 위반의 심한 정도 — 클수록 나쁘다."""
    if v.type == "manpower_shortage":
        return float((v.required or 0) - (v.actual or 0))
    if v.type == "manpower_excess":
        return float((v.actual or 0) - (v.required_max or 0))
    if v.type == "max_nig_per_month":
        return float(v.actual or 0)
    return 0.0
