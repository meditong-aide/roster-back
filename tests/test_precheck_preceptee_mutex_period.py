"""Regression: precheck preceptee-sync 가 (A) period SSOT 경로와 (B) 상호배제 모순을 놓치던 갭.

발견(diagnose_matrix, 8 wards): 동일한 '함께근무(preceptee) + 배반(mutex)' 논리 모순이
`preceptor_id` 경로로는 8/8 표현되지만 authoritative `period` 경로로는 2/8 밖에 안 나왔다
(데이터-리딩 버그로 8월에 preceptee 가 되살아나며 mutex 공존하는 시나리오가 거의 미진단).

수정:
  A) runtime_bridge.build_precheck_input 이 preceptee_period_by_nurse_id(authoritative)를
     preceptor_id 로 overlay → period 경로 preceptee 가 sync 체크에 보이게.
  B) check_preceptee_sync_mismatch 가 mutual_exclusion_by_nurse_id 를 대조 →
     shift/team 이 호환이어도 preceptor-preceptee 페어의 mutex 공존을 mutual_exclusion_conflict
     로 표현.
"""

from __future__ import annotations

from services.precheck import run_runtime_precheck

DAYS = set(range(31))
BASE_CFG = {
    "daily_shift_requirements": {"D": 1, "E": 1, "N": 1},
    "global_monthly_off_days": 2,
    "standard_personal_off_days": 8,
}


def _nurses(preceptor_on_b=False, b_team=1):
    b = {"nurse_id": "B", "grade": 1, "team_id": b_team, "allowed_shifts": []}
    if preceptor_on_b:
        b["preceptor_id"] = "A"
    return [{"nurse_id": "A", "grade": 1, "team_id": 1, "allowed_shifts": []}, b]


def _mutex(cfg):
    c = dict(cfg)
    c["mutual_exclusion_by_nurse_id"] = {
        "A": {"partner_id": "B", "days": set(DAYS)},
        "B": {"partner_id": "A", "days": set(DAYS)},
    }
    return c


def _period(cfg):
    c = dict(cfg)
    c["preceptee_period_by_nurse_id"] = {"B": {"preceptor_id": "A", "days": set(DAYS)}}
    c["preceptee_period_authoritative"] = True
    return c


def _codes(nurses, cfg):
    r = run_runtime_precheck(nurses_dict=nurses, config_dict=cfg, grade_config=None,
                             fixed_cells=None, year=2026, month=8, stop_on_config_error=False)
    return {i.get("reason_code") for i in r.get("issues", [])}


# ── no false positives ───────────────────────────────────────────────────────
def test_compatible_preceptor_pair_no_false_positive():
    # 같은 team/shift, mutex 없음 → 정상 preceptee 페어. 절대 flag 하면 안 됨.
    assert "PRECEPTEE_SYNC_MISMATCH" not in _codes(_nurses(preceptor_on_b=True), dict(BASE_CFG))


def test_no_preceptee_relation_clean():
    assert "PRECEPTEE_SYNC_MISMATCH" not in _codes(_nurses(preceptor_on_b=False), dict(BASE_CFG))


# ── Fix B: mutex conflict (shift/team 호환이어도 표현) ────────────────────────
def test_preceptor_id_plus_mutex_flagged():
    # 호환 페어인데 mutex → 유일한 사유는 mutual_exclusion_conflict.
    assert "PRECEPTEE_SYNC_MISMATCH" in _codes(_nurses(preceptor_on_b=True), _mutex(BASE_CFG))


# ── Fix A: period SSOT 경로 가시성 ───────────────────────────────────────────
def test_period_preceptee_visible_on_team_mismatch():
    # B 에 preceptor_id 필드 없음 — 관계는 period map 으로만. team mismatch → flag 되어야.
    assert "PRECEPTEE_SYNC_MISMATCH" in _codes(_nurses(preceptor_on_b=False, b_team=2), _period(BASE_CFG))


def test_period_preceptee_plus_mutex_flagged():
    # 데이터-리딩 버그 시나리오: period 로 preceptee 되살아남 + mutex 공존 (같은 team/shift).
    # 이전엔 미진단(2/8). 이제 표현돼야.
    assert "PRECEPTEE_SYNC_MISMATCH" in _codes(_nurses(preceptor_on_b=False), _period(_mutex(BASE_CFG)))


def test_period_preceptee_compatible_no_mutex_clean():
    # period preceptee 지만 team/shift 호환 + mutex 없음 → 정상 → flag 안 함(period overlay 오발화 방지).
    assert "PRECEPTEE_SYNC_MISMATCH" not in _codes(_nurses(preceptor_on_b=False), _period(BASE_CFG))
