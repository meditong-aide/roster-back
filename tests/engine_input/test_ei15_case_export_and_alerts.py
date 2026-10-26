"""EI-15 15b — case_export 키 목록의 레지스트리 파생 · feasibility_alerts 기본값 expected-diff 시험.

바뀐 것
  - `case_export._CONFIG_KEYS`: 손으로 쓴 23개 → 레지스트리에서 파생(옛 이름은 하나도 안 빠지고, 정본/별칭 짝과 하드락 cfg 키가 더해진다).
    그래서 `allow_three_consecutive_nights`·`ban_e_to_d`·`max_consecutive_work_days` 등이 케이스에 실린다(옛엔 빠져 진단 재현이 설정을 놓쳤다).
  - `feasibility_alerts`: `preceptee_follows_preceptor` 키가 없을 때의 기본이 False → 레지스트리(True, 엔진 기본).
"""

from __future__ import annotations

import ast
from pathlib import Path

from services.cp_sat import feasibility_alerts
from services.engine_contract.defaults import registry_default
from services.engine_contract.effective import HARD_LOCK_CFG_KEYS
from services.ontology_graph import case_export

_OLD_KEYS = (
    "daily_shift_requirements", "day_req", "eve_req", "nig_req",
    "off_days", "global_monthly_off_days", "standard_personal_off_days",
    "max_nig_per_month", "max_night_shifts_per_month",
    "max_consecutive_nights", "max_consec_nights", "max_conseq_work",
    "two_offs_after_two_nig", "two_offs_after_three_nig", "ban_single_night",
    "ban_n_to_d", "ban_n_to_e", "ban_shift_transitions", "ban_night_before_fixed_off",
    "ban_night_before_fixed_wanted_off",
    "use_mid", "staffing_first", "preceptee_follows_preceptor",
)
_ADDED_KEYS = ("allow_three_consecutive_nights", "ban_e_to_d", "max_consecutive_work_days", "max_consecutive_work")


def test_config_keys_keep_every_old_key_and_add_registry_ones():
    keys = set(case_export._CONFIG_KEYS)
    assert set(_OLD_KEYS) <= keys
    assert set(HARD_LOCK_CFG_KEYS) <= keys
    assert set(_ADDED_KEYS) <= keys
    assert len(case_export._CONFIG_KEYS) == len(keys)  # 중복 없음


def test_build_case_now_carries_the_new_hard_lock_keys():
    cfg = {"allow_three_consecutive_nights": 3, "ban_e_to_d": True, "max_conseq_work": 5, "day_req": 3}
    case = case_export.build_case(year=2026, month=11, group_id="g", num_days=30, nurses=[], config=cfg)
    carried = case["config"] if "config" in case else {}
    assert carried.get("allow_three_consecutive_nights") == 3  # 옛 목록엔 없어 빠졌다
    assert carried.get("ban_e_to_d") is True
    assert carried.get("max_conseq_work") == 5
    assert carried.get("day_req") == 3  # 미등록 옛 이름은 그대로


def test_feasibility_alert_preceptee_default_is_registry_not_literal_false():
    assert registry_default("preceptee_follows_preceptor") is True
    tree = ast.parse(Path(feasibility_alerts.__file__).read_text(encoding="utf-8"))
    defaults = []
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "get"
                and node.args and isinstance(node.args[0], ast.Constant) and node.args[0].value == "preceptee_follows_preceptor"):
            defaults.append(node.args[1] if len(node.args) > 1 else None)
    assert defaults and all(isinstance(d, ast.Call) for d in defaults)  # registry_default(...) 호출
