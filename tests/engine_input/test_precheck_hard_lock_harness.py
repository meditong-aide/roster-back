"""EI-10c 하네스 — precheck 의 엔진 하드락 조회(create_config_from_db 1회)는 'L1 밖 L2 호출'로 세지 않고
`precheck_hard_lock_calls` 로 따로 센다(조용히 빠지거나 두 번 불리는 회귀를 잡는다).

배경: `effective_hard_lock`(EI-8)이 `CPSATBasicEngine.create_config_from_db` 를 부르고, 캡처 층은 L1 밖
L2 호출을 `l2_calls_outside_l1` 로 센다. 설계서 §5.3 EI-8 이 'precheck 소비자는 이 함수만 쓴다'고 정한
정당한 호출이라 안전장치 기록(`safety.l2_calls_outside_l1`, 골든 16개 모두 0)은 그대로 둬야 한다.
골든 기록(`golden.build_record`)에는 새 카운터를 넣지 않는다.
"""

from __future__ import annotations

import pytest

from .capture import Capture
from .harness import run_with_output
from .scenario_defs import SCENARIOS
from .scenario_ladder import LADDER_SCENARIOS


@pytest.mark.parametrize("name", ["S1", "S1-L", "S3", "S8", "S9"])
def test_precheck_hard_lock_called_once_and_not_counted_outside_l1(name, engine, engine_input_tables):
    """시나리오 전체 실행 — 하드락 조회 정확히 2회(precheck 1 + EI-14b2a1 뷰의 정본 키 번역 1 · 사다리 불가 경로는 사후 번역 +1) · L1 밖 L2 호출 0
    (S3 은 엔진도 못 읽는 설정이라 폴백).

    S1-L 은 자동 완화 사다리(시도 25회 이상)다 — precheck 가 재시도 루프 안으로 옮겨져 시도마다 불리면(엔진 print 7줄이 곱해진다)
    생성 1회당 2회가 깨진다(리뷰 지적).
    """
    result = run_with_output(engine, {**SCENARIOS, **LADDER_SCENARIOS}[name]).result
    # EI-14b2b1 — 사다리가 끝내 못 푸는 경로(S1-L)는 사후 소비자 입력(`to_roster_ir_post`)의 정본 키 번역이 한 번 더 부른다(실패 뒤 1회 — 시도마다 아님).
    assert result.precheck_hard_lock_calls == (3 if name in LADDER_SCENARIOS else 2)
    assert result.l2_calls_outside_l1 == 0


def test_l2_call_outside_l1_and_outside_precheck_lookup_is_still_counted():
    """예외는 precheck 하드락 조회 안쪽뿐이다 — 그 밖의 L1 밖 L2 호출은 여전히 센다."""
    capture = Capture()
    spy = capture._spy_l2(lambda _self: {"ok": True}, "settings_eff", stop=False)
    spy(object())
    assert capture.l2_calls_outside_l1 == 1
    capture._in_precheck_hl = True
    spy(object())
    assert capture.l2_calls_outside_l1 == 1


def test_hard_lock_lookup_spy_counts_and_clears_flag_even_on_error():
    """조회가 예외로 끝나도 깃발이 내려간다(안 내려가면 뒤 L2 호출이 계속 가려진다)."""
    capture = Capture()

    def failing(_config):
        assert capture._in_precheck_hl is True
        raise TypeError("엔진도 못 읽는 설정")

    spy = capture._spy_precheck_hard_lock(failing)
    with pytest.raises(TypeError):
        spy({})
    assert capture.precheck_hard_lock_calls == 1
    assert capture._in_precheck_hl is False


def test_install_restores_hard_lock_lookup_after_run(engine, engine_input_tables):
    """실행이 끝나면 감싼 `engine_hard_lock` 이 원본으로 돌아와 있다."""
    import services.precheck.hard_lock_view as hard_lock_view

    run_with_output(engine, SCENARIOS["S1"])
    assert hard_lock_view.engine_hard_lock.__qualname__ == "engine_hard_lock"
