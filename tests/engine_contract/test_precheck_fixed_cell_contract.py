"""EI-10d 번역 계약 — precheck 의 고정 칸 번역은 엔진 정규화와 모순되지 않는다(결정 D8 · 설계서 §2.6).

엔진 값은 엔진 두 단계를 그대로 다시 계산한 오라클이다(근무코드 표는 실제 `build_shift_normalizer` 가 지은 것).
  1단계 `normalize_shift_code(원코드, id_to_main)`            (cp_sat_basic.py:1158)
  2단계 `code2main.get(c) or id_to_main.get(c) or c`, OFF·주→O (cp_sat_basic.py:2643-2650)
precheck 는 근무코드 표(shifts) 조회 없이 번역하므로 아는 만큼만 답한다. 계약은 두 줄이다.
  (a) precheck 가 칸을 남기면 그 코드는 엔진 코드와 **같다** — 틀린 코드가 없으니 오차단의 원천도 없다.
  (b) precheck 가 칸을 버리면 이유가 둘 중 하나다 — 엔진 코드가 precheck 가 모델링하지 않는 코드(W, MID 꺼진 M)이거나,
      표 없이는 엔진 값을 알 수 없는 비표준 코드(원티드 하위코드 등)다. 후자가 '느슨한 한계'이고 이 표가 고정한다.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from services.cp_sat.shift_normalizer import build_shift_normalizer, normalize_shift_code
from services.precheck.fixed_cell_codes import precheck_fixed_cells
from services.roster_create_service import _build_code_to_main_map

#: 근무코드 정의(shifts 행) — 표준 D/E/N/OFF/M 에 원티드 하위코드(D1)·특수코드(연·공·특근)를 더한 현실적인 표.
SHIFT_DEFS = [
    {"shift_id": "D", "default_shift": "D", "shift_gb": "D", "type": "근무"},
    {"shift_id": "E", "default_shift": "E", "shift_gb": "E", "type": "근무"},
    {"shift_id": "N", "default_shift": "N", "shift_gb": "N", "type": "근무"},
    {"shift_id": "M", "default_shift": "M", "shift_gb": "M", "type": "근무"},
    {"shift_id": "OFF", "default_shift": "O", "shift_gb": None, "type": "오프"},
    {"shift_id": "D1", "default_shift": None, "shift_gb": None, "type": "근무"},
    {"shift_id": "연", "default_shift": None, "shift_gb": None, "type": "휴가"},
    {"shift_id": "공", "default_shift": None, "shift_gb": None, "type": "공가"},
    {"shift_id": "특근", "default_shift": None, "shift_gb": None, "type": "근무"},
]
#: 근무코드 그룹(ShiftManage) — D1 은 그룹표로는 D 지만 엔진 1단계(`id_to_main`)는 W 로 본다(S4 실측).
SHIFT_MANAGE = [
    {"main_code": "D", "codes": ["D", "D1"]}, {"main_code": "E", "codes": ["E"]},
    {"main_code": "N", "codes": ["N"]}, {"main_code": "M", "codes": ["M"]},
]
#: (원코드, fixed_source, shift_type) — 생성 경로가 실제로 만드는 칸 종류 전부.
CELLS = [
    ("D", "fixed_wanted", "근무"), ("E", "fixed_wanted", "근무"), ("N", "fixed_wanted", "근무"),
    ("M", "fixed_wanted", "근무"), ("O", "fixed_wanted", "근무"), ("OFF", "fixed_wanted", "근무"),
    ("주", "weekly_off", "주휴"), ("O", "weekly_off", "주휴"), ("O", "2n2off_recovery", None),
    ("W", "fixed_wanted", "근무"), ("d", "fixed_wanted", "근무"), (" N ", "fixed_wanted", "근무"),
    ("D1", "fixed_wanted", "근무"),
    ("연", "special_fixed", "휴가"), ("공", "special_fixed", "공가"), ("특근", "special_fixed", "근무"),
]
NURSES = [SimpleNamespace(nurse_id="a", sequence=1, experience=0)]


def _engine_main(raw: str, id_to_main: dict[str, str], code2main: dict[str, str]) -> str:
    """엔진 두 단계를 그대로 다시 계산한 메인코드(1단계가 못 알아봐도 2단계는 원코드로 이어진다)."""
    stage1 = normalize_shift_code(raw, id_to_main)
    code = (stage1 if stage1 is not None else str(raw).strip()).upper()
    mapped = code2main.get(code) or id_to_main.get(code) or code
    return "O" if mapped in ("OFF", "주") else mapped


def _needs_table(raw: str, source: str) -> bool:
    """표 없이는 엔진 값을 알 수 없는 칸 — 특수 칸이 아니면서 표준 코드가 아니다."""
    return source != "special_fixed" and normalize_shift_code(raw, {}) is None


@pytest.mark.parametrize("use_mid", [False, True])
@pytest.mark.parametrize("raw, source, kind", CELLS)
def test_translation_agrees_with_engine_or_drops_for_a_known_reason(raw, source, kind, use_mid):
    id_to_main, _ = build_shift_normalizer(SHIFT_DEFS)
    code2main = _build_code_to_main_map(SHIFT_MANAGE)
    cell = {"nurse_index": 0, "day_index": 3, "shift": raw, "shift_type": kind, "fixed_source": source}
    out = precheck_fixed_cells(NURSES, [cell], SHIFT_MANAGE, use_mid=use_mid)

    engine = _engine_main(raw, id_to_main, code2main)
    modeled = {"D", "E", "N", "O"} | ({"M"} if use_mid else set())
    if out:
        assert out == [{"nurse_id": "a", "day_index": 3, "shift": engine}], (raw, source, engine)
    else:
        assert engine not in modeled or _needs_table(raw, source), (raw, source, engine)


def test_known_loose_limit_is_exactly_the_table_dependent_codes():
    """(b) 의 '느슨한 한계'는 표가 필요한 비표준 코드뿐이다 — 표준 코드·특수 코드는 하나도 새지 않는다.

    D1 은 엔진 1단계가 W 로 읽는 원티드 하위코드라 precheck 가 건너뛴다(엔진 W 도 모델링하지 않으니 어긋남은 없다).
    """
    id_to_main, _ = build_shift_normalizer(SHIFT_DEFS)
    dropped_modeled = [
        (raw, source) for raw, source, kind in CELLS
        if not precheck_fixed_cells(
            NURSES, [{"nurse_index": 0, "day_index": 1, "shift": raw, "shift_type": kind, "fixed_source": source}],
            SHIFT_MANAGE, use_mid=True)
        and _engine_main(raw, id_to_main, _build_code_to_main_map(SHIFT_MANAGE)) in {"D", "E", "N", "O", "M"}
    ]
    assert dropped_modeled == []
    assert _engine_main("D1", id_to_main, _build_code_to_main_map(SHIFT_MANAGE)) == "W"
