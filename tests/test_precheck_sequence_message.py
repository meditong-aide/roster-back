"""EI-11 11z — 개인 시퀀스 차단(`PER_NURSE_SEQUENCE_INFEASIBLE`) 문구 등록(EI-10d 후속 U7 · ei11/EI11_SPEC_v2.md §2.8).

개인 시퀀스 DP(`team_grade_precheck.check_per_nurse_sequence`)가 내는 차단 코드에 문구가 없어 `humanize` 가 코드 원문을
`human_message_ko` 로 썼다(폴백). 그래서 모달 '생성이 막힌 조건' 상자에 코드가 떴다.
기대 문안은 지시서 확정 문안을 글자 그대로 옮겼다 — 문구 표에서 읽지 않으므로 표가 바뀌면 여기서 잡힌다.
"""

from __future__ import annotations

from services.precheck.messaging import _MESSAGES, humanize
from services.precheck.payload import _BLOCKING_CODES, build_blocking_payload

CODE = "PER_NURSE_SEQUENCE_INFEASIBLE"
# 판정 함수가 싣는 evidence 모양 그대로(team_grade_precheck `check_per_nurse_sequence`) — humanize 꼬리표 키가 없다.
EVIDENCE = {"nurse_id": "N1", "active_days": 30, "fixed_cells": 3}
MSG = "어떤 간호사의 미리 정한 근무·휴무(원티드·휴가 등)가 근무 순서 규칙(야간 다음 날 주간 금지, 야간 뒤 휴식, 연속 근무 상한 등)과 맞물려, 그 간호사의 이달 근무 순서를 만들 수 없습니다."
FIX = [
    "고정 근무 편집 > 해당 간호사에서 근무 순서 규칙과 부딪히는 확정 근무·휴무를 옮기거나 줄여 주세요(예: 야간 바로 다음 날의 확정 주간 근무).",
    "간호사 관리 > 해당 간호사 > 설 수 있는 근무 종류가 확정 근무와 맞는지 확인해 주세요.",
]
# 문구 원칙(messaging.py — '실패/불가능/에러/위반' 금지) + 하드락 규칙을 완화하라는 안내 금지.
FORBIDDEN_WORDS = ("실패", "불가능", "에러", "위반", "완화", "해제", "꺼 주세요")


def _issue() -> dict:
    return {"reason_code": CODE, "evidence": dict(EVIDENCE)}


def test_sequence_issue_humanized_with_exact_text():
    """문안 글자 일치 — 자리표시자·꼬리표 없이 msg 그대로이고, 조정 안내 2개가 순서대로다."""
    out = humanize(_issue())
    assert out["human_message_ko"] == MSG
    assert out["fix_suggestions_ko"] == FIX


def test_every_blocking_code_has_message():
    """완전성 — 차단 코드 집합(payload `_BLOCKING_CODES`)의 모든 코드에 문구가 있다(코드 원문 폴백 0)."""
    assert CODE in _BLOCKING_CODES
    assert sorted(code for code in _BLOCKING_CODES if code not in _MESSAGES) == []


def test_sequence_message_wording_rules():
    """문구 규칙 — msg·fix 어디에도 금지 단어가 없다."""
    entry = _MESSAGES[CODE]
    for text in (entry["msg"], *entry["fix"]):
        assert [word for word in FORBIDDEN_WORDS if word in text] == [], text


def test_blocking_payload_shows_message_instead_of_raw_code():
    """다리 — 차단 payload 의 요약(모달 '생성이 막힌 조건')·확인된 항목·조정 제안이 코드 원문이 아니라 문안이다."""
    inf = build_blocking_payload({"issues": [_issue()]})["infeasibility"]
    assert inf["summary_message_ko"] == MSG
    assert [issue["human_message_ko"] for issue in inf["preflight_issues"]] == [MSG]
    assert inf["fix_suggestions_ko"] == FIX
