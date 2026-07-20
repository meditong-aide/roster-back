"""Regression: precheck capacity 상한을 과대계산해 infeasible 을 놓치던 두 갭.

gap-hunter(8 wards) 발견 (INFEASIBLE 인데 precheck 침묵):
  3b) max_conseq_work 가 실제 근무가능일을 줄이는데 _working_capacity 가 미반영 (3/8).
  3a) per-nurse 야간 상한(n_max/n_exact)을 야간cap 체크가 미반영 — 전역 상한만 봐서
      야간 공급을 과대계산 (3/8).

두 수정 모두 capacity 상한을 '조이는' 방향이라 precision 보존(엔진도 같은 하드 제약을
가지므로 shortage 는 진짜 shortage → false positive 없음).
"""

from __future__ import annotations

from services.precheck import run_runtime_precheck
from services.precheck.team_grade_precheck import _working_capacity, PrecheckNurse

YEAR, MONTH = 2026, 8


def _codes(nurses, cfg):
    r = run_runtime_precheck(nurses_dict=nurses, config_dict=cfg, grade_config=None,
                             fixed_cells=None, year=YEAR, month=MONTH, stop_on_config_error=False)
    return {i.get("reason_code") for i in r.get("issues", [])}


# ── 3b: consecutive-work capacity ceiling (unit) ─────────────────────────────
def test_working_capacity_consec_ceiling():
    nurse = PrecheckNurse(nurse_id="n", join_day=0, leave_day=30)  # span 31, off=0
    assert _working_capacity(nurse, {}) == 31                       # 제한 없음
    assert _working_capacity(nurse, {"max_consecutive_work": 1}) == 16   # 31 - 31//2
    assert _working_capacity(nurse, {"max_consecutive_work": 5}) == 26   # 31 - 31//6
    # off 가 더 조이면 off 가 binding
    n2 = PrecheckNurse(nurse_id="n", join_day=0, leave_day=30)
    assert _working_capacity(n2, {"max_consecutive_work": 10,
                                  "standard_personal_off_days": 20}) == 11   # min(31-20, 31-31//11=29)


# ── 3b: end-to-end ───────────────────────────────────────────────────────────
def _nurses(n, **extra):
    return [{"nurse_id": f"n{i}", "grade": 1, "allowed_shifts": [], **extra} for i in range(n)]


def _cfg(N=8, D=1, E=1, max_nig=15, **extra):
    return {"daily_shift_requirements": {"D": D, "E": E, "N": N},
            "global_monthly_off_days": 2, "standard_personal_off_days": 8,
            "max_nig_per_month": max_nig, **extra}


def test_max_conseq_1_triggers_capacity_shortage():
    # 10 nurses, max_conseq=1 → 각 16일 근무가능, demand D=E=N=6 → 월 need 558 > cap 160 → shortage
    codes = _codes(_nurses(10), _cfg(D=6, E=6, N=6, max_consecutive_work=1))
    assert "CAPACITY_TOTAL_SHORTAGE" in codes


def test_loose_conseq_no_false_positive():
    # 넉넉한 인원 + 느슨한 연속근무 → shortage 없어야
    codes = _codes(_nurses(30), _cfg(D=2, E=2, N=2, max_consecutive_work=6))
    assert "CAPACITY_TOTAL_SHORTAGE" not in codes


# ── 3a: per-nurse night cap ──────────────────────────────────────────────────
def test_per_nurse_n_max_triggers_night_shortage():
    # 20 nurses, n_max=1 each → night cap 20 < 8*31=248 → shortage
    assert "MONTHLY_NIGHT_CAPACITY_SHORTAGE" in _codes(_nurses(20, n_max=1), _cfg(N=8))


def test_no_per_nurse_cap_no_false_positive():
    # per-nurse cap 없음 + 넉넉한 전역 상한 → shortage 없음
    assert "MONTHLY_NIGHT_CAPACITY_SHORTAGE" not in _codes(_nurses(20), _cfg(N=8, max_nig=15))


def test_n_exact_takes_precedence_over_n_max():
    # n_exact=0 이면 n_max 무관하게 야간 0 → shortage (N=1 여도)
    assert "MONTHLY_NIGHT_CAPACITY_SHORTAGE" in _codes(_nurses(20, n_max=15, n_exact=0), _cfg(N=1))
