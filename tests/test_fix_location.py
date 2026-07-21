"""해결책/원인 → "어디서 어떻게 고치는가"(fix) 구조화 검증.

프론트가 원인 설명과 함께 "설정 어디로 가서 무엇을 바꾸면 되는지"를 렌더링하도록
resolution_option/cause 에 fix 객체를 붙인다.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))

from services.cp_sat.fix_location import (  # noqa: E402
    fix_for_option,
    fix_for_reason,
    attach_fix_to_options,
    attach_fix_to_causes,
)


def test_numeric_knob_auto_apply_with_value():
    o = {"apply": {"max_conseq_work": 3},
         "changes": [{"config_key": "max_conseq_work", "from": 2, "to": 3}]}
    f = fix_for_option(o)
    assert f["mode"] == "auto_apply"
    assert f["where"] == "roster_config.max_consec_work"
    assert "연달아 일하는 최대 날수" in f["where_label_ko"]
    assert "3" in f["how_ko"]


def test_toggle_knob_message():
    o = {"apply": {"two_offs_after_two_nig": False},
         "changes": [{"config_key": "two_offs_after_two_nig", "to": False}]}
    f = fix_for_option(o)
    assert f["mode"] == "auto_apply"
    assert "꺼주세요" in f["how_ko"]


def test_sized_option_uses_suggested_value():
    o = {"changes": [{"config_key": "max_nig_per_month", "suggested_value": 8}],
         "apply": {"max_nig_per_month": 8}}
    f = fix_for_option(o)
    assert "8" in f["how_ko"] and f["where"] == "roster_config.night_month_limit"


def test_unmapped_key_returns_none():
    assert fix_for_option({"apply": {"some_unknown_key": 1},
                           "changes": [{"config_key": "some_unknown_key", "to": 1}]}) is None


def test_data_correction_reason_with_deeplink():
    f = fix_for_reason("ALLOWED_SHIFTS_ISOLATES_NURSE", {"nurse_id": "141180"})
    assert f["mode"] == "manual_navigate"
    assert f["where"] == "nurse.allowed_shifts"
    assert f["target"] == {"nurse_id": "141180"}


def test_fixed_cell_reason_deeplinks_day():
    f = fix_for_reason("FIXED_ASSIGN_EXCEEDS_NEED", {"day": 12})
    assert f["where"] == "fixed_shift.edit" and f["target"] == {"day": 12}


def test_attach_options_idempotent():
    opts = [{"apply": {"max_conseq_work": 3},
             "changes": [{"config_key": "max_conseq_work", "to": 3}]}]
    attach_fix_to_options(opts)
    attach_fix_to_options(opts)
    assert "fix" in opts[0] and opts[0]["fix"]["config_key"] == "max_conseq_work"


def test_attach_causes():
    causes = [{"reason_code": "MONTHLY_LIMIT_MIN_EXCEEDS_MAX",
               "details": {"nurse_id": "n5"}}]
    attach_fix_to_causes(causes)
    assert causes[0]["fix"]["where"] == "nurse.monthly_limit"
    assert causes[0]["fix"]["target"] == {"nurse_id": "n5"}
