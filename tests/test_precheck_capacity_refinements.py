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
    nurse = PrecheckNurse(nurse_id="n", join_day=0, leave_day=30)  # span 31
    assert _working_capacity(nurse, {}) == 31                       # 제한 없음
    assert _working_capacity(nurse, {"max_consecutive_work": 1}) == 16   # 31 - 31//2
    assert _working_capacity(nurse, {"max_consecutive_work": 5}) == 26   # 31 - 31//6
    # ★ 개인 월 휴무(off_days/standard)는 엔진 소프트 → hard capacity 를 줄이지 않는다.
    #   (연속근무 상한만 하드 천장. 8d2c2a9 회귀에서 off 를 하드 감산하던 것을 되돌림)
    n2 = PrecheckNurse(nurse_id="n", join_day=0, leave_day=30)
    assert _working_capacity(n2, {"max_consecutive_work": 10,
                                  "standard_personal_off_days": 20}) == 29   # min(31, 31-31//11)
    assert _working_capacity(n2, {"off_days": 20}) == 31                     # off 소프트 → 감산 안 함
    # 전사 고정 휴무(global)는 하드로 취급 → 감산
    assert _working_capacity(n2, {"global_monthly_off_days": 5}) == 26       # 31 - 5


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


# ── 원인 레버(연속근무 상한이 capacity binding) → 자동 완화 옵션 ─────────────────
def _issues(nurses, cfg):
    r = run_runtime_precheck(nurses_dict=nurses, config_dict=cfg, grade_config=None,
                             fixed_cells=None, year=YEAR, month=MONTH, stop_on_config_error=False)
    return r.get("issues", [])


def _cap_issue(issues):
    return next((i for i in issues if i.get("reason_code") == "CAPACITY_TOTAL_SHORTAGE"), None)


# 20명·mcw=1: capped cap=16/명(320) < need 403 ≤ uncapped 21/명(420) → 상한이 binding.
# 10명·mcw=1·D=E=N=6: uncapped(210) 도 need(558) 미달 → 진짜 인원부족(레버 아님).
def test_conseq_cap_binding_evidence_when_lever_is_cause():
    issues = _issues(_nurses(20), _cfg(D=4, E=4, N=5, max_consecutive_work=1))
    cap = _cap_issue(issues)
    assert cap is not None
    b = (cap.get("evidence") or {}).get("conseq_cap_binding")
    assert isinstance(b, dict)
    assert b["config_key"] == "max_conseq_work"
    assert b["current"] == 1
    assert b["suggested_value"] is not None and b["suggested_value"] > 1


def test_no_binding_when_genuinely_short_of_nurses():
    issues = _issues(_nurses(10), _cfg(D=6, E=6, N=6, max_consecutive_work=1))
    cap = _cap_issue(issues)
    assert cap is not None
    assert (cap.get("evidence") or {}).get("conseq_cap_binding") is None


def test_config_lever_option_built_with_auto_fix():
    from services.cp_sat.undiagnosed_probe import config_lever_options_from_issues
    issues = _issues(_nurses(20), _cfg(D=4, E=4, N=5, max_consecutive_work=1))
    opts = config_lever_options_from_issues(issues)
    assert len(opts) == 1
    o = opts[0]
    assert o["option_id"] == "lever:max_conseq_work"
    assert o["apply"]["max_conseq_work"] == o["changes"][0]["suggested_value"]
    assert o["fix"]["mode"] == "auto_apply"
    assert o["verified"] is False


def test_no_lever_option_when_no_binding():
    from services.cp_sat.undiagnosed_probe import config_lever_options_from_issues
    issues = _issues(_nurses(10), _cfg(D=6, E=6, N=6, max_consecutive_work=1))
    assert config_lever_options_from_issues(issues) == []


# ── off_days(소프트)는 capacity 를 못 바꾼다 → CAPACITY_TOTAL 판정에 영향 없음(회귀 가드) ──
def test_off_days_does_not_affect_capacity_shortage():
    # 넉넉한 인원·느슨한 연속근무면 off_days 를 크게 줘도 shortage 안 뜬다(예전엔 뜸=버그).
    codes = _codes(_nurses(30), _cfg(D=2, E=2, N=2, max_consecutive_work=6, off_days=15))
    assert "CAPACITY_TOTAL_SHORTAGE" not in codes


def test_raising_conseq_alone_resolves_off_coupled_case():
    # 예전 'coupled' 로 오판하던 케이스(off=10 & conseq=2)가 실제론 연속근무 단일 레버로 풀린다.
    issues = _issues(_nurses(20), _cfg(D=5, E=4, N=5, max_consecutive_work=2, off_days=10))
    cap = _cap_issue(issues)
    assert cap is not None
    b = (cap.get("evidence") or {}).get("conseq_cap_binding")
    assert isinstance(b, dict)                 # 단일 노브(연속근무)로 풀림
    assert b["current"] == 2 and b["suggested_value"] > 2


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
