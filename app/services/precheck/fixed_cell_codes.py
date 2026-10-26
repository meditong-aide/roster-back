"""precheck 고정 칸 — 엔진 색인·원코드를 precheck 가 읽는 nurse_id·메인코드로 바꾼다(EI-10d · 결정 D8).

생성 경로의 `combined_fixed_cells` 원소는 엔진 형식(`nurse_index`·`day_index`·원코드 `shift`)인데
`runtime_bridge._collect_fixed_assignments` 는 `nurse_id`·`date`/`day` 만 읽어 **모든 칸을 버렸다**(결함 ②).
여기서 엔진과 같은 규칙으로 바꿔 넘긴다. 해석 규칙은 새로 적지 않고 엔진 함수를 그대로 부른다.

- 간호사: `presolve_block._fixed_cells_by_nurse_id` — 나이트 과공급 탐지기와 **같은 함수**
  (`rcs._build_engine_nurse_index_map` 의 역, 엔진 정렬 그대로).
- 근무코드: 엔진 2단계 정규화를 근무코드 표(shifts) 조회 없이 한다(추가 조회 0).
  1단계 `shift_normalizer.normalize_shift_code`(cp_sat_basic.py:1158)는 표준 코드(D·E·N·M·O·주·W·OFF)를 표 없이
  바로 답하므로 엔진과 같다. 2단계는 `code2main.get(c) or c`, OFF·주→O(cp_sat_basic.py:2643-2650)다.
  · 특수 칸(`fixed_source='special_fixed'`)의 코드는 그 칸이 들고 있는 `shift_type`(근무코드 표의 type)으로
    엔진 `build_shift_normalizer` 를 같은 방식으로 돌려 정한다(휴가·공가→O, 근무→W).
    칸이 default_shift·shift_gb 를 안 들고 있어 type 만으로 정한다 — 표준 코드와 겹치는 드문 설정은 엔진과 갈릴 수 있다.
  · 그 밖의 비표준 코드(원티드 하위코드 D1 등)는 표 없이는 엔진 값을 알 수 없어 **건너뛴다**
    (실측 S4: `code2main` 은 D1→D 인데 엔진 `id_to_main` 은 D1→W). 건너뛰면 precheck 가 느슨해질 뿐 오차단은 없다.
- precheck 가 모델링하지 않는 W 와 MID 꺼진 병동의 M 도 건너뛴다(엔진도 shift_types 밖 코드는 버린다).
- 배정 차단일(`blocked_by_nurse_id` — 파견·휴직·퇴사·병동이동 배정 기간) 위의 칸도 버린다. 엔진 lex 는 활동일(차단일 제외)
  밖 고정 칸을 건너뛴다(fallback_lex.py:1311 `(n, d) not in active_days: continue`). 남기면 precheck 가 엔진이 안 보는 칸을
  하드로 보고 막는다(오차단). 값은 이미 메모리(`config_dict`)에 있다 — 조회 0. 차단일 때문에 가용 인원이 줄어드는 부분은
  10g(EI-13a 뒤) 몫이다.
- 같은 (간호사, 날)에 칸이 겹치면 엔진은 **마지막 칸**이 이긴다(`fixed[(n, d)] = s_idx` 덮어쓰기). 뒤 칸을 번역하지 못하면
  (표 없는 비표준 코드·모델링 밖 코드) 그 날은 '모름'이다 — 앞 칸을 남기지 않는다(느슨한 쪽).
"""

from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence
from typing import Any

from services.cp_sat.shift_normalizer import build_shift_normalizer, normalize_shift_code
from services.precheck.presolve_block import _fixed_cells_by_nurse_id
from services.precheck.team_grade_precheck import _apply_shifts

_SPECIAL_SOURCE = "special_fixed"


def _special_id_to_main(rows: Sequence[Mapping[str, Any]]) -> dict[str, str]:
    """특수 칸의 코드 → 메인코드 — 엔진 `build_shift_normalizer` 를 칸의 `shift_type` 만으로 돌린다."""
    defs = [{"shift_id": row.get("shift"), "type": row.get("shift_type")}
            for row in rows if str(row.get("fixed_source") or "").strip() == _SPECIAL_SOURCE]
    return build_shift_normalizer(defs)[0]


def fixed_cell_main_code(
    row: Mapping[str, Any], code2main: Mapping[str, str], special_id_to_main: dict[str, str],
) -> str | None:
    """칸 하나의 엔진 메인코드 — 근무코드 표 없이 정할 수 없으면 None.

    `special_id_to_main` 은 특수 칸 코드의 1단계 표(`_special_id_to_main`)다. 표준 코드는 표 없이 정해진다.
    """
    stage1 = normalize_shift_code(row.get("shift"), special_id_to_main)
    if stage1 is None:
        return None
    mapped = str(code2main.get(stage1) or stage1).strip().upper()
    # 방어용 — 1단계가 OFF→O 로 접고 `_build_code_to_main_map` 이 주→O 를 늘 넣어 이 접기가 없어도 지금 입력에서는 값이 같다
    # (등가 변이). 엔진 2단계(cp_sat_basic.py:2643-2650)와 같은 모양을 지키려고 둔다.
    return "O" if mapped in ("OFF", "주") else mapped


def _blocked_days(blocked_by_nurse_id: Mapping[str, Any] | None, nurse_id: Any) -> Collection[int]:
    """이 간호사의 배정 차단일(0-based) — 없거나 모양이 틀리면 빈 집합."""
    days = (blocked_by_nurse_id or {}).get(str(nurse_id))
    return days if isinstance(days, (set, frozenset, list, tuple)) else ()


def precheck_fixed_cells(
    nurses: Sequence[Any],
    combined_fixed_cells: Sequence[Mapping[str, Any]] | None,
    shift_manage_data: Sequence[Mapping[str, Any]] | None,
    *,
    use_mid: bool,
    blocked_by_nurse_id: Mapping[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """엔진 형식 고정 칸 → `[{nurse_id, day_index, shift(메인코드)}]` — precheck 가 모델링하는 D·E·N·O(+M) 칸만.

    입력 칸은 건드리지 않는다. 칸이 없으면 근무코드 표 변환도 하지 않는다(순수 함수 둘만 부르며 조회는 0).
    `blocked_by_nurse_id`({nurse_id: 0-based 차단일})에 든 칸은 버린다. 같은 (간호사, 날) 칸은 마지막 것만 남고,
    마지막 칸을 번역하지 못하면 그 날은 칸이 없다(위 모듈 설명). 출력 순서는 각 (간호사, 날)이 처음 나온 순서다.
    """
    if not combined_fixed_cells:
        return []
    from services.roster_create_service import _build_code_to_main_map

    code2main = _build_code_to_main_map(list(shift_manage_data or []))
    rows = _fixed_cells_by_nurse_id(
        list(nurses), list(combined_fixed_cells), extra_keys=("shift_type", "fixed_source"))
    special = _special_id_to_main(rows)
    modeled = {*_apply_shifts(use_mid), "O"}
    final: dict[tuple[Any, int], str | None] = {}
    for row in rows:
        nid, day = row["nurse_id"], row["day_index"]
        if nid is None or day in _blocked_days(blocked_by_nurse_id, nid):
            continue
        main = fixed_cell_main_code(row, code2main, special)
        final[(nid, day)] = main if main in modeled else None
    return [{"nurse_id": nid, "day_index": day, "shift": main}
            for (nid, day), main in final.items() if main is not None]


__all__ = ["fixed_cell_main_code", "precheck_fixed_cells"]
