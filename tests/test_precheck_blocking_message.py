"""precheck blocking 메시지 품질: raw 코드 대신 사람 안내 + 수치 + 관리자 fix.

산술적으로 불가능한 설정은 솔버 전에 500 으로 막는 게 옳다(안 돌려도 아니까). 단 그 500 이
'무엇을 왜 고쳐야 하는지'를 명확히 안내해야 한다. 기존엔 MONTHLY_NIGHT_CAPACITY_SHORTAGE 가
템플릿 키(MONTHLY_NIGHT_CAPACITY)와 불일치 + evidence(n_required/n_capacity) suffix 미지원
+ 개인속성(월 N 상한) fix 제안 문제가 있었다.
"""

from __future__ import annotations

from services.precheck.messaging import humanize


def test_monthly_night_capacity_shortage_humanized_with_numbers():
    out = humanize({
        "reason_code": "MONTHLY_NIGHT_CAPACITY_SHORTAGE",
        "evidence": {"n_required": 186, "n_capacity": 175, "shortage": 11},
    })
    msg = out["human_message_ko"]
    assert msg != "MONTHLY_NIGHT_CAPACITY_SHORTAGE"       # raw 코드가 아니어야
    assert "야간" in msg
    assert "월요구=186" in msg and "월가능=175" in msg and "부족=11" in msg
    assert out["fix_suggestions_ko"]                      # 비어있지 않아야


def test_night_cap_fix_excludes_personal_attribute():
    # 개인 속성(월 N 상한 상향)은 제안하면 안 됨 — 개인 속성 불가침.
    out = humanize({"reason_code": "MONTHLY_NIGHT_CAPACITY_SHORTAGE", "evidence": {}})
    joined = " ".join(out["fix_suggestions_ko"])
    assert "상한" not in joined
    assert ("추가" in joined) or ("낮추" in joined)      # 관리자 노브만


def test_previously_missing_codes_now_templated():
    for code in ("N_CAPACITY_SHORTAGE", "PRECEPTEE_SYNC_MISMATCH"):
        out = humanize({"reason_code": code, "evidence": {}})
        assert out["human_message_ko"] != code           # raw 코드가 아니어야
        assert out["fix_suggestions_ko"]


def test_unknown_code_still_falls_back_gracefully():
    out = humanize({"reason_code": "SOME_UNKNOWN_CODE", "evidence": {}})
    assert out["human_message_ko"] == "SOME_UNKNOWN_CODE"  # fallback (기존 동작 유지)
    assert out["fix_suggestions_ko"] == []
