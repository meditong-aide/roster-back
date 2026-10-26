"""EI-10f 특성화 테스트 — 골든 시나리오에는 월중 입퇴사자가 없어 10f 가 어느 골든도 바꾸지 않는다.

10f 는 precheck 의 근무 가능 구간(`PrecheckNurse.join_day`·`leave_day`)을 엔진과 같은 날 계산으로 읽는다(퇴사일 −1 ·
그 달과 겹치지 않는 입퇴사는 근무 없음). 골든 16개의 간호사는 전부 입사일이 대상 월 한참 전이고 퇴사일이 없다 — 구간이 월
전체 `(0, 말일−1)` 그대로라 판정·입력·출력이 하나도 안 바뀐다. `StageDiff "10f"` 가 허용 0 인 근거가 이 테스트다.

골든 L0 의 `nurses_dict`·`config_dict`·`fixed_cells`(precheck 에 들어가는 값 그대로)로 `build_precheck_input` 을 다시 돌려
간호사마다 구간을 대조한다. 골든 파일은 읽기만 한다. 골든에 월중 입퇴사자가 생기면 이 테스트가 먼저 알려 준다(그때는 그
골든이 10f 허용 차이에 들어야 하는지 정한다).

D29(2026-10-20) — 골든은 커밋하지 않는 로컬 전용이다. 매개변수 목록은 수집 때 골든 폴더를 훑지 않고 **코드가 정의한
시나리오 이름**(`SCENARIOS` ∪ `LADDER_SCENARIOS`)에서 뽑는다 — 골든이 없는 체크아웃에서는 시나리오마다 D29 사유로
건너뛴다(`read_golden` 의 문지기). 코드 이름 = L0 가 있는 골든 이름인지는 개수 테스트가 본다.
"""
from __future__ import annotations

import calendar

import pytest
from services.precheck.runtime_bridge import build_precheck_input

from .golden import GOLDEN_DIR, read_golden
from .golden_gate import GOLDEN, require_golden
from .scenario_defs import SCENARIOS
from .scenario_ladder import LADDER_SCENARIOS

#: 코드가 정의한 골든 시나리오 이름(정렬) — 수집 시점에 골든 파일을 읽지 않는다(D29).
GOLDEN_NAMES = sorted({*SCENARIOS, *LADDER_SCENARIOS})


def test_there_are_golden_scenarios_with_a_precheck_input():
    """코드가 정의한 시나리오 이름 = L0 가 있는 골든 이름이고 16개다 — 골든 하나가 빠지거나 남아도, 아래 대조가 빈 목록으로
    조용히 통과해도 드러난다."""
    require_golden(GOLDEN)
    with_l0 = sorted(path.stem for path in GOLDEN_DIR.glob("*.json")
                     if (read_golden(path.stem) or {}).get("layers", {}).get("L0"))
    assert with_l0 == GOLDEN_NAMES, (with_l0, GOLDEN_NAMES)
    assert len(GOLDEN_NAMES) == 16, GOLDEN_NAMES


@pytest.mark.parametrize("name", GOLDEN_NAMES)
def test_golden_nurses_keep_the_full_month_span(name):
    """골든의 모든 간호사는 입사일이 대상 월 이전이고 퇴사일이 없어 구간이 월 전체다 — 10f 가 바꾸는 입력이 없다."""
    call = read_golden(name)["layers"]["L0"][0]
    year, month = call["year"], call["month"]
    month_start = f"{year:04d}-{month:02d}-01"
    for nurse in call["nurses_dict"]:
        assert not nurse.get("resignation_date"), (name, nurse["nurse_id"])
        assert str(nurse.get("joining_date") or "")[:10] < month_start, (name, nurse["nurse_id"])
    inp = build_precheck_input(
        nurses_dict=call["nurses_dict"], config_dict=call["config_dict"], grade_config=call["grade_config"],
        fixed_cells=call["fixed_cells"], year=year, month=month)
    last_day = calendar.monthrange(year, month)[1] - 1
    assert {(n.nurse_id, n.join_day, n.leave_day) for n in inp.nurses} == {
        (nurse["nurse_id"], 0, last_day) for nurse in call["nurses_dict"]}
