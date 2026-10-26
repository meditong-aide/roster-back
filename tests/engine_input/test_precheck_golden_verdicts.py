"""골든 L0 16개의 precheck 판정 특성화 — 이슈 집합과 (S5·S8) 간호사별 입력 요약을 저장소 안에서 고정한다.

왜 있는가(리뷰 지적 LOW): 골든 16개는 precheck 가 받는 **입력**(L0)과 이후 경로만 기록한다. 그래서 precheck 의 판정(이슈)이
조용히 바뀌어도 경로가 같으면 골든이 못 잡는다 — 10c~10f 의 '골든 판정 변화 0'은 일회성 프로브로만 확인됐다. 이 파일이 그 주장을
테스트로 남긴다: 골든 L0 의 `nurses_dict`·`config_dict`·`fixed_cells`(precheck 에 들어가는 값 그대로)로 `run_runtime_precheck` 를
다시 돌려 이슈 `(reason_code, severity)` 집합을 대조하고, 입력이 바뀌는 S5·S8 은 간호사별 입력 요약(개인 OFF 바닥 · 고정 OFF ·
고정 근무)도 고정한다. 다음 단계(10g·EI-13a)에서 경고나 이슈가 조용히 바뀌면 이 테스트가 먼저 빨개져, 바뀐 시나리오를 허용 차이에
적을지 정하게 한다. 골든 파일은 읽기만 한다.

16개 전부 이슈 0건(`OK`)이다 — 골든 시나리오는 용량이 넉넉한 합성 병동이라 차단·경고 어느 쪽도 나지 않는다.

D29(2026-10-20) — 골든은 커밋하지 않는 로컬 전용이다. 매개변수 목록은 수집 때 골든 폴더를 훑지 않고 **코드가 정의한
시나리오 이름**(`SCENARIOS` ∪ `LADDER_SCENARIOS`)에서 뽑는다 — 골든이 없는 체크아웃에서 빈 매개변수로 조용히 사라지지 않고
시나리오마다 D29 사유로 건너뛴다(`read_golden` 의 문지기). 코드 이름 = L0 가 있는 골든 이름인지는 개수 테스트가 본다.
"""
from __future__ import annotations

import contextlib
import io

import pytest
from services.precheck import run_runtime_precheck
from services.precheck.runtime_bridge import build_precheck_input

from .golden import GOLDEN_DIR, read_golden
from .golden_gate import GOLDEN, require_golden
from .scenario_defs import SCENARIOS
from .scenario_ladder import LADDER_SCENARIOS

#: 코드가 정의한 골든 시나리오 이름(정렬) — 수집 시점에 골든 파일을 읽지 않는다(D29).
GOLDEN_NAMES = sorted({*SCENARIOS, *LADDER_SCENARIOS})

#: 이슈 `(reason_code, severity)` 집합 — 16개 전부 빈 집합(이슈 없음). 어떤 시나리오가 처음 이슈를 내면 여기에 적는다.
EXPECTED_ISSUES: dict[str, set[tuple[str, str]]] = {name: set() for name in GOLDEN_NAMES}

#: 입력이 바뀌는 시나리오의 간호사별 입력 요약 `(개인 OFF 바닥, 고정 OFF 일자, 고정 근무)` — 값이 있는 간호사만.
#: S5(2026-12, 변동 주기 주휴 병동): N01·N02 는 주말휴무자(주말 8일), N03·N04 는 엔진이 박은 주휴(목 5일·월 4일).
#: S8(2026-11): 연차 `연`(휴가)→O · 확정 원티드 OFF→O · 확정 원티드 D/N.
EXPECTED_NURSE_INPUTS: dict[str, dict[str, tuple[int, list[int], dict[int, str]]]] = {
    "S5": {
        "N01": (8, [4, 5, 11, 12, 18, 19, 25, 26], {}),
        "N02": (8, [4, 5, 11, 12, 18, 19, 25, 26], {}),
        "N03": (5, [2, 9, 16, 23, 30], {}),
        "N04": (4, [6, 13, 20, 27], {}),
    },
    "S8": {
        "N01": (0, [9], {2: "D"}),
        "N02": (0, [], {4: "N"}),
        "N04": (0, [11], {}),
    },
}


def _kwargs(name: str) -> dict:
    call = read_golden(name)["layers"]["L0"][0]
    return {
        "nurses_dict": call["nurses_dict"], "config_dict": call["config_dict"], "grade_config": call["grade_config"],
        "fixed_cells": call["fixed_cells"], "year": call["year"], "month": call["month"],
    }


def test_there_are_sixteen_golden_scenarios_with_a_precheck_input():
    """코드가 정의한 시나리오 이름 = L0 가 있는 골든 이름이고 16개다 — 골든 하나가 빠지거나 남아도, 아래 대조가 빈 목록으로
    조용히 통과해도 드러난다."""
    require_golden(GOLDEN)
    with_l0 = sorted(path.stem for path in GOLDEN_DIR.glob("*.json")
                     if (read_golden(path.stem) or {}).get("layers", {}).get("L0"))
    assert with_l0 == GOLDEN_NAMES, (with_l0, GOLDEN_NAMES)
    assert len(GOLDEN_NAMES) == 16, GOLDEN_NAMES
    assert set(EXPECTED_NURSE_INPUTS) <= set(GOLDEN_NAMES)


@pytest.mark.parametrize("name", GOLDEN_NAMES)
def test_golden_precheck_issue_set_is_pinned(name, monkeypatch):
    """골든 L0 로 precheck 를 다시 돌린 이슈 `(reason_code, severity)` 집합이 고정값(없음)과 같다 — 판정 변화는 여기서 드러난다."""
    monkeypatch.delenv("AIDE_MAX_CONSEC_WORK", raising=False)  # 연속근무 상한은 골든 설정 값만 쓴다
    with contextlib.redirect_stdout(io.StringIO()):
        result = run_runtime_precheck(**_kwargs(name), stop_on_config_error=False)
    got = {(issue["reason_code"], issue["severity"]) for issue in result["issues"]}
    assert got == EXPECTED_ISSUES[name], sorted(got)
    assert result["status"] == ("HAS_ISSUES" if got else "OK")


@pytest.mark.parametrize("name", sorted(EXPECTED_NURSE_INPUTS))
def test_golden_nurse_inputs_are_pinned(name):
    """S5·S8 의 간호사별 precheck 입력(개인 OFF 바닥 · 고정 OFF · 고정 근무)이 고정값과 같다 — 값이 있는 간호사만, 나머지는 전부 0·빈 값."""
    inp = build_precheck_input(**_kwargs(name))
    got = {
        nurse.nurse_id: (nurse.personal_off_adjustment, sorted(nurse.fixed_off_days), dict(nurse.fixed_shift_assignments))
        for nurse in inp.nurses
        if nurse.personal_off_adjustment or nurse.fixed_off_days or nurse.fixed_shift_assignments
    }
    assert got == EXPECTED_NURSE_INPUTS[name], got
