"""Regression: precheck night-capacity ceiling was blind to the runtime config key.

버그: check_monthly_night_capacity (team_grade_precheck, Fix 2 α) 는 야간 상한을
`max_night_shifts_per_month` 키로 읽었지만, 엔진(create_config_from_db)과 실 config 는
`max_nig_per_month` 를 쓴다. runtime_bridge 가 두 표기를 매핑하지 않아 상한 로직이
죽어있었고 → max_nig 가 병목인 infeasible 을 precheck 가 통째로 놓쳤다
(constructed case: max_nig=2 + N=8 → 솔버 INFEASIBLE 인데 precheck OK).

수정: runtime_bridge.build_precheck_input 이 max_nig_per_month / max_conseq_work 별칭에서
값을 해석해 forward. 야간 상한은 엔진과 동일 semantics 로 정규화(<=0/None → 15 floor)해
false positive 를 방지한다.
"""

from __future__ import annotations

from services.precheck import run_runtime_precheck
from services.precheck.runtime_bridge import build_precheck_input

YEAR, MONTH = 2026, 8


def _nurses(n: int):
    # 전원 N 가능(allowed_shifts=[] → 제한 없음), grade 1
    return [{"nurse_id": f"n{i}", "grade": 1, "allowed_shifts": []} for i in range(n)]


def _cfg(max_nig, N=8):
    return {
        "daily_shift_requirements": {"D": 1, "E": 1, "N": N},
        "global_monthly_off_days": 2,
        "standard_personal_off_days": 8,
        "max_nig_per_month": max_nig,
    }


def _build(cfg, n=5):
    return build_precheck_input(
        nurses_dict=_nurses(n), config_dict=cfg, grade_config=None,
        fixed_cells=None, year=YEAR, month=MONTH,
    )


def _codes(cfg, n=20):
    res = run_runtime_precheck(
        nurses_dict=_nurses(n), config_dict=cfg, grade_config=None,
        fixed_cells=None, year=YEAR, month=MONTH, stop_on_config_error=False,
    )
    return {i.get("reason_code") for i in res.get("issues", [])}


# ── mapping (unit) ───────────────────────────────────────────────────────────
# 2026-07-21 키 정규화: precheck config dict 키를 DB/엔진 canonical(max_nig_per_month /
# max_conseq_work / off_days)로 통일. output/read 모두 DB 키. 구 dataclass 키는 fallback.
def test_max_nig_per_month_forwarded_to_precheck_key():
    assert _build(_cfg(2)).roster_config["max_nig_per_month"] == 2


def test_max_nig_zero_floored_to_15_like_engine():
    # 엔진 create_config_from_db: max_nig<=0 → 15. precheck 도 동일해야 false positive 없음.
    assert _build(_cfg(0)).roster_config["max_nig_per_month"] == 15


def test_db_canonical_key_takes_precedence():
    # DB 키(max_nig_per_month)가 canonical → 구 키(max_night_shifts_per_month) 있어도 DB 키 우선.
    cfg = _cfg(2)
    cfg["max_night_shifts_per_month"] = 7
    assert _build(cfg).roster_config["max_nig_per_month"] == 2


def test_legacy_key_still_read_as_fallback():
    # DB 키가 없고 구 키만 있으면 fallback 으로 읽어 output(canonical)에 실린다.
    cfg = {"daily_shift_requirements": {"D": 1, "E": 1, "N": 8},
           "max_night_shifts_per_month": 5}
    assert _build(cfg).roster_config["max_nig_per_month"] == 5


def test_max_conseq_work_forwarded_canonical():
    cfg = _cfg(15)
    cfg["max_conseq_work"] = 4
    assert _build(cfg).roster_config["max_conseq_work"] == 4


# ── end-to-end (the bug it fixes) ────────────────────────────────────────────
def test_low_max_nig_triggers_night_capacity_shortage():
    # 20명 × min(wc,2)=2 → cap 40 < N need 8*31=248 → shortage 발화
    assert "MONTHLY_NIGHT_CAPACITY_SHORTAGE" in _codes(_cfg(2))


def test_high_max_nig_no_false_positive():
    # 20명 × min(wc,15)=15 → cap 300 > 248 → shortage 없음
    assert "MONTHLY_NIGHT_CAPACITY_SHORTAGE" not in _codes(_cfg(15))


def test_max_nig_zero_no_false_positive():
    # floor→15 → shortage 없음 (엔진이 실제로 15를 쓰므로)
    assert "MONTHLY_NIGHT_CAPACITY_SHORTAGE" not in _codes(_cfg(0))
