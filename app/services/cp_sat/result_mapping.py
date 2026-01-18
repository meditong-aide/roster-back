"""엔진 결과를 DB 저장/응답 형식으로 변환하는 모듈."""

from __future__ import annotations

from typing import Optional

import numpy as np

from db.nurse_config import Nurse
from services.roster_system import RosterSystem


def convert_result_to_db_format(
    roster_system: RosterSystem,
    nurses: list[Nurse],
    *,
    canonical_to_shift_id: dict[str, str] | None = None,
    fixed_original_shift_map: dict[tuple[int, int], str] | None = None,
) -> dict[str, list[str]]:
    """RosterSystem 결과를 DB 형식(간호사 DB ID → shift_id 리스트)으로 변환한다.

    Args:
        roster_system: 계산이 완료된 RosterSystem
        nurses: 간호사 객체 리스트
        canonical_to_shift_id: 메인 코드(D/E/N/O/W/주) → 실제 shift_id 매핑
        fixed_original_shift_map: 고정 셀의 원본 shift_id 매핑

    Returns:
        Dict[str, List[str]]: nurse.db_id를 키로 하는 근무표
    """
    result: dict[str, list[str]] = {}
    canonical_map = {k.upper(): v for k, v in (canonical_to_shift_id or {}).items() if v}
    if not canonical_map:
        canonical_map = {"D": "D", "E": "E", "N": "N", "O": "O", "주": "주"}
    fixed_original_shift_map = fixed_original_shift_map or {}
    shift_map = {i: s for i, s in enumerate(roster_system.config.shift_types)}
    fixed = getattr(roster_system, "fixed_cells", None)
    fixed_lookup: dict[tuple[int, int], str] = {}
    if fixed:
        for cell in fixed:
            fixed_lookup[(cell["nurse_index"], cell["day_index"])] = cell["shift"]

    for n_idx, nurse in enumerate(nurses):
        nurse_schedule: list[str] = []
        for day_idx in range(roster_system.num_days):
            # 고정된 셀은 원래 값으로 반환
            if (n_idx, day_idx) in fixed_lookup:
                original = fixed_original_shift_map.get((n_idx, day_idx))
                if original:
                    nurse_schedule.append(original)
                    continue
                fixed_shift = fixed_lookup[(n_idx, day_idx)]
                nurse_schedule.append(
                    canonical_map.get(str(fixed_shift).upper(), str(fixed_shift))
                )
                continue
            shift_vector = roster_system.roster[n_idx, day_idx]
            shift_idx = np.where(shift_vector == 1)[0]
            if len(shift_idx) > 0:
                shift_id = shift_map[int(shift_idx[0])]
                mapped = canonical_map.get(str(shift_id).upper(), str(shift_id))
                nurse_schedule.append(mapped)
            else:
                nurse_schedule.append("-")
        result[nurse.db_id] = nurse_schedule
    return result


