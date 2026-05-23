"""US-B1: generate_schedule infeasibility → 한국어 자연어 응답 변환 테스트."""

from __future__ import annotations

import pytest

from agents_v2.tools.narrative_formatter import format_infeasibility_response


# ── fixture helpers ───────────────────────────────────────────────────────────


def _make_payload(
    *,
    cause_codes: list[str] | None = None,
    problem_rendered: list[str] | None = None,
    action_rationale: list[str] | None = None,
    hard_case: bool = False,
) -> dict:
    """infeasibility payload 픽스처 생성 (build_unrecoverable_payload 구조 모사)."""
    causes = [
        {"reason_code": rc, "human_message_ko": f"{rc} 발생"}
        for rc in (cause_codes or [])
    ]
    problem_list = [
        {"cause_id": f"c{i}", "rendered_ko": txt}
        for i, txt in enumerate(problem_rendered or [])
    ]
    action_levers = [
        {"treatment_id": f"t{i}", "rationale_ko": txt}
        for i, txt in enumerate(action_rationale or [])
    ]
    return {
        "infeasibility": {
            "severity": "blocking",
            "summary_message_ko": "근무표를 만들 수 없습니다.",
            "causes": causes,
            "resolution_narrative": {
                "summary_ko": "",
                "problem_list": problem_list,
                "action_levers": action_levers,
                "trade_offs": [],
            },
            "hard_case": {"is_hard_case": hard_case, "reason_ko": "복합 제약" if hard_case else ""},
        }
    }


# ── 시나리오 1: CAPACITY infeasibility ───────────────────────────────────────


def test_capacity_infeasibility_contains_korean_and_no_raw_code():
    payload = _make_payload(cause_codes=["NO_ASSIGNMENT_CAPACITY"])
    result = format_infeasibility_response(payload)

    assert "필요 인원 부족" in result
    assert "NO_ASSIGNMENT_CAPACITY" not in result
    assert "근무표를 만들 수 없었습니다" in result


# ── 시나리오 2: ELIGIBILITY infeasibility ────────────────────────────────────


def test_eligibility_infeasibility_contains_korean_and_no_raw_code():
    payload = _make_payload(cause_codes=["NO_ASSIGNMENT_ELIGIBILITY"])
    result = format_infeasibility_response(payload)

    assert "자격 요건" in result
    assert "NO_ASSIGNMENT_ELIGIBILITY" not in result


# ── 시나리오 3: FIXED infeasibility ──────────────────────────────────────────


def test_fixed_infeasibility_contains_korean_and_no_raw_code():
    payload = _make_payload(cause_codes=["NO_ASSIGNMENT_FIXED"])
    result = format_infeasibility_response(payload)

    assert "확정 원티드" in result
    assert "NO_ASSIGNMENT_FIXED" not in result


# ── 시나리오 4: hard_case=true → 운영자 검토 안내 ────────────────────────────


def test_hard_case_appends_operator_review():
    payload = _make_payload(
        cause_codes=["CAPACITY_TOTAL_SHORTAGE"],
        hard_case=True,
    )
    result = format_infeasibility_response(payload)

    assert "운영자 검토" in result
    # 운영자 안내가 응답의 끝 부분에 위치하는지 확인
    assert result.rstrip().endswith("운영자 검토를 권장합니다.")


def test_no_hard_case_does_not_append_operator_review():
    payload = _make_payload(
        cause_codes=["CAPACITY_TOTAL_SHORTAGE"],
        hard_case=False,
    )
    result = format_infeasibility_response(payload)

    assert "운영자 검토" not in result


# ── 시나리오 5: 정상(feasible) job → narrative 변환 없이 schedule_id 응답 ────


def test_feasible_job_format_returns_no_infeasibility_signal():
    """feasible job 은 infeasibility payload 가 없으므로 함수 자체를 호출하지 않는다.
    빈/None payload 에 대해서는 기본 메시지만 반환됨을 확인."""
    result_none = format_infeasibility_response(None)
    result_empty = format_infeasibility_response({})

    assert result_none == "근무표를 만들 수 없었습니다."
    assert result_empty == "근무표를 만들 수 없었습니다."
    # 'schedule_id' 나 'infeasible' 같은 필드는 이 함수의 관심사가 아님 — str 반환
    assert isinstance(result_none, str)


# ── 추가: narrative problem_list rendered_ko 사용 경로 ───────────────────────


def test_rendered_ko_from_problem_list_used_over_cause_code():
    """problem_list.rendered_ko 가 있을 때 cause_code 매핑 대신 rendered_ko 사용."""
    payload = _make_payload(
        cause_codes=["NO_ASSIGNMENT_CAPACITY"],
        problem_rendered=["야간 담당 간호사가 3명 부족합니다"],
    )
    result = format_infeasibility_response(payload)

    assert "야간 담당 간호사가 3명 부족합니다" in result
    assert "NO_ASSIGNMENT_CAPACITY" not in result


def test_action_levers_included_in_response():
    """action_levers.rationale_ko 가 응답에 포함되는지 확인."""
    payload = _make_payload(
        cause_codes=["CAPACITY_TOTAL_SHORTAGE"],
        action_rationale=["팀 최소 인원 요구를 낮추세요"],
    )
    result = format_infeasibility_response(payload)

    assert "팀 최소 인원 요구를 낮추세요" in result
    assert "권장 조치" in result
