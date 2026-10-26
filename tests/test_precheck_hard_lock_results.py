"""EI-10c 결과 테스트 — precheck 가 엔진과 같은 하드락 값을 보게 되어 판정이 바뀌는 사례.

배경(결정 D8 · 설계서 §2.6 C8'): 생성 서비스는 하드락 5키(`allow_three_consecutive_nights`·`two_offs_after_three_nig`·
`two_offs_after_two_nig`·`ban_single_night`·`ban_shift_transitions`)를 precheck 앞까지 들고 오지만
`runtime_bridge.build_precheck_input` 이 `roster_config` 를 새로 지으며 버렸다. 그래서 precheck 는
연속 N 상한을 늘 2 로, 회복·1N 규칙을 늘 끈 것으로, 전이 금지를 늘 켠 것으로 봤다.
10c 는 EI-8 `effective_hard_lock`(엔진 `create_config_from_db` 해석 그대로)을 판정 직전에 싣는다.

각 테스트의 '엔진 판정'은 fallback_lex 의 하드 제약 의미(`test_precheck_dp_engine_semantics.py` 의
독립 brute-force 오라클과 같은 규칙)로 따진 값이다. ✗ = 10c 전 코드에서 빨강(결함 재현),
(가드) = 전/후 모두 초록이지만 변이·퇴행을 잡는 불변 확인이다.
"""

from __future__ import annotations

from services.precheck import run_runtime_precheck
from services.precheck.payload import has_blocking_issues
from services.precheck.team_grade_precheck import (
    PrecheckInput,
    PrecheckNurse,
    check_monthly_night_capacity,
    max_nights_under_recovery,
    precheck_sequence_params,
    run_precheck,
)

SEQ = "PER_NURSE_SEQUENCE_INFEASIBLE"
NIGHT = "MONTHLY_NIGHT_CAPACITY_SHORTAGE"
CAP = "CAPACITY_TOTAL_SHORTAGE"


def _cfg(**extra):
    """엔진 기본에 가까운 설정(3연속 N 허용 · 3N→2OFF 켬 · 2N→2OFF 끔 · 1N 허용)."""
    base = {
        "daily_shift_requirements": {"D": 1, "E": 1, "N": 1}, "max_conseq_work": 5,
        "max_nig_per_month": 15, "ban_shift_transitions": True, "allow_three_consecutive_nights": True,
        "two_offs_after_three_nig": True, "two_offs_after_two_nig": False, "ban_single_night": False,
        "use_mid": False, "off_days": 8, "shift_priority": 0.7,
        # EI-14r3 — 이 파일은 연속근무·주휴·퇴사 용량을 따로 잰다. OFF 하드 하한(min_off — staffing_first=False 병동)은 별개 제약이라 끈다
        # (그 하한은 test_precheck_min_off_floor.py · test_precheck_vs_engine_stage1.py::test_s7e* 가 본다).
        "staffing_first": True,
    }
    base.update(extra)
    return base


def _nurses(n=10, **extra):
    return [{"nurse_id": f"n{i}", "grade": 1, "allowed_shifts": [], **extra} for i in range(n)]


def _night_only_ward():
    """N 전담 4명 — 2명은 한 달 내내, 2명은 11일 입사(20일 구간)."""
    full = _nurses(2, allowed_shifts=["N"])
    joined = [{"nurse_id": f"p{i}", "grade": 1, "allowed_shifts": ["N"], "joining_date": "2026-11-11"}
              for i in range(2)]
    return full + joined


def _night_demand(per_day, *, zero_last_days=0):
    """30일 N 수요(일 per_day명) — 마지막 `zero_last_days` 일은 0."""
    by_day = [{"D": 0, "E": 0, "N": per_day if d < 30 - zero_last_days else 0} for d in range(30)]
    return {"daily_shift_requirements": {"D": 0, "E": 0, "N": per_day},
            "daily_shift_requirements_by_day": by_day}


def _cells(nurse_id, by_day):
    """bridge 형식 고정 칸(`day` 는 1-based)."""
    return [{"nurse_id": nurse_id, "day": d, "shift": s} for d, s in by_day.items()]


def _run(nurses, cfg, cells=None, month=11):
    result = run_runtime_precheck(
        nurses_dict=nurses, config_dict=cfg, grade_config=None, fixed_cells=cells,
        year=2026, month=month, stop_on_config_error=False)
    return result, {issue["reason_code"] for issue in result["issues"]}


def test_r10c1_fixed_nnn_not_blocked_when_three_seq_nig():
    """✗ 3연속 N 허용 병동의 고정 NNN — 엔진은 NNN 뒤 OO 로 풀린다(전에는 L=2 로 막았다)."""
    result, codes = _run(_nurses(), _cfg(), _cells("n0", {4: "N", 5: "N", 6: "N"}))
    assert SEQ not in codes and not has_blocking_issues(result)


def test_r10c2_two_offs_after_two_with_three_nights_allows_nnn():
    """✗ 2N→2OFF + 3연속 N 허용 — 엔진은 블록이 끝날 때 OO 라 NNN OO 가 된다(옛 DP 는 2개에서 끊었다)."""
    cfg = _cfg(two_offs_after_two_nig=True)
    result, codes = _run(_nurses(), cfg, _cells("n0", {4: "N", 5: "N", 6: "N"}))
    assert SEQ not in codes and not has_blocking_issues(result)


def test_r10c3_forced_single_night_then_fixed_day_blocks():
    """✗ 1N 금지가 강제한 N(5일) 다음 고정 D(6일)는 엔진 불가능 — 전에는 1N 키를 못 받아 놓쳤다."""
    cells = _cells("n0", {3: "O", 4: "N", 6: "D"})
    result, codes = _run(_nurses(), _cfg(ban_single_night=True), cells)
    assert SEQ in codes and has_blocking_issues(result)


def test_r10c3b_same_but_bans_off_passes():
    """(가드) `ban_shift_transitions=False` 면 엔진은 전이 금지 4종을 다 끈다(마스킹) — 통과."""
    cells = _cells("n0", {3: "O", 4: "N", 6: "D"})
    result, codes = _run(_nurses(), _cfg(ban_single_night=True, ban_shift_transitions=False), cells)
    assert SEQ not in codes


def test_r10c4_recovery_cap_catches_night_shortage():
    """✗ 2N→2OFF(연속 N 2) — 11일 입사(20일 구간) N 전담은 최대 11회, 전원 합 15+15+11+11=52 < 수요 60.

    전에는 회복 키를 못 받아 월 상한 15 로만 세어 60 으로 보고 놓쳤다.
    """
    cfg = _cfg(two_offs_after_two_nig=True, allow_three_consecutive_nights=False, **_night_demand(2))
    result, codes = _run(_night_only_ward(), cfg)
    assert NIGHT in codes and has_blocking_issues(result)


def test_r10c4b_two_offs_after_two_with_three_nights_capacity_not_overstated():
    """(가드) 2N→2OFF + 3연속 N(L=3) — 엔진은 NNN OO 라 20일 구간 최대 13회(합 56).

    수요 54 는 L 을 무시한 값(11 → 합 52)보다 크고 엔진 상한(56)보다 작다 — 오차단 가드.
    """
    cfg = _cfg(two_offs_after_two_nig=True, allow_three_consecutive_nights=True, **_night_demand(2, zero_last_days=3))
    _result, codes = _run(_night_only_ward(), cfg)
    assert NIGHT not in codes


def test_r10c4c_three_offs_after_three_caps_night_supply():
    """✗ 3N→2OFF(엔진 기본 켬 — 키가 없으면 True) — 20일 구간 N 전담은 최대 14회(NNO 반복), 합 15+15+14+14=58 < 수요 60.

    전에는 `two_offs_after_three_nig` 를 못 받아 이 설정의 회복 상한이 늘 꺼져 있었다.
    """
    cfg = _cfg(**_night_demand(2))
    _result, codes = _run(_night_only_ward(), cfg)
    assert NIGHT in codes


def test_r10c5_env_consec_cap_reaches_precheck(monkeypatch):
    """✗ AIDE_MAX_CONSEC_WORK=4 — 엔진 연속근무 상한 min(5,4)=4 가 precheck 용량에도 걸린다.

    30일·상한 4 → 1인 24일(30-30//5) × 10명 = 240 < 수요 241(8명/일 × 30 + 첫날 1).
    """
    monkeypatch.setenv("AIDE_MAX_CONSEC_WORK", "4")
    by_day = [{"D": 3, "E": 3, "N": 2} for _ in range(30)]
    by_day[0] = {"D": 3, "E": 3, "N": 3}
    cfg = _cfg(daily_shift_requirements={"D": 3, "E": 3, "N": 2}, daily_shift_requirements_by_day=by_day)
    _result, codes = _run(_nurses(10), cfg)
    assert CAP in codes


def test_r10c5b_without_env_same_demand_passes(monkeypatch):
    """(가드) 같은 입력에서 env 가 없으면 상한 5 → 1인 25일 × 10 = 250 ≥ 241 — 용량 부족 없음."""
    monkeypatch.delenv("AIDE_MAX_CONSEC_WORK", raising=False)
    by_day = [{"D": 3, "E": 3, "N": 2} for _ in range(30)]
    by_day[0] = {"D": 3, "E": 3, "N": 3}
    cfg = _cfg(daily_shift_requirements={"D": 3, "E": 3, "N": 2}, daily_shift_requirements_by_day=by_day)
    _result, codes = _run(_nurses(10), cfg)
    assert CAP not in codes


def test_r10c6_engine_unreadable_config_falls_back_without_error(capsys):
    """(가드) 엔진도 못 읽는 설정(`shift_priority=None`) — 하드락 값을 싣지 않고 옛 값(L=2)으로 정상 실행한다.

    값이 실렸다면(L=3) r10c1 처럼 통과였을 고정 NNN 이 옛 값으로는 막히는 것으로 '싣지 않았음'을 확인한다.
    """
    cells = _cells("n0", {4: "N", 5: "N", 6: "N"})
    result, codes = _run(_nurses(), _cfg(shift_priority=None), cells)
    assert result["status"] == "HAS_ISSUES" and SEQ in codes
    assert "실행 실패" not in capsys.readouterr().out


def test_r10c7_single_night_allowed_when_monthly_night_cap_is_one():
    """(가드) 1N 금지여도 월 N 상한 1 간호사는 엔진이 면제한다(collect_single_n_allowed_nurse_indices)."""
    nurses = _nurses()
    nurses[0]["n_max"] = 1
    cells = _cells("n0", {4: "O", 5: "N", 6: "O"})
    _result, codes = _run(nurses, _cfg(ban_single_night=True), cells)
    assert SEQ not in codes


def test_r10c8_day_one_fixed_single_night_is_exempt():
    """(가드) 1일 고정 N 은 엔진 1N 금지 면제(fallback_lex.py:2136) — 2일이 고정 O 여도 통과."""
    _result, codes = _run(_nurses(), _cfg(ban_single_night=True), _cells("n0", {1: "N", 2: "O"}))
    assert SEQ not in codes


def test_r10c8b_single_night_exemption_is_only_for_the_first_day_of_the_month():
    """✗ 리뷰 지적(검증 공백) — 월 1일 고정 N 면제는 **그 달 1일**만이다. 11/10 입사자의 첫날(입사일) 고정 N 은 면제가 아니다.

    엔진 면제(fallback_lex.py:2136)는 절대 일자 d == 0 이다. 입사자의 구간 첫날을 면제로 보면(`join_day == 0` 조건을 지우면)
    1N 금지에서 N(입사일) → O 가 통과해 불가능을 놓친다. 입사 11/10(인덱스 9) · 고정 N(10일) · 고정 O(11일) → 단독 N 이라 엔진 불가능.
    """
    nurses = _nurses()
    nurses[0]["joining_date"] = "2026-11-10"
    result, codes = _run(nurses, _cfg(ban_single_night=True), _cells("n0", {10: "N", 11: "O"}))
    assert SEQ in codes and has_blocking_issues(result)


def test_r10c8c_same_cells_for_a_full_month_nurse_extend_backwards_and_pass():
    """(가드) 같은 칸(10일 고정 N · 11일 고정 O)이라도 한 달 내내 근무하는 간호사는 9일에 N 을 이어 NN 이 되므로 통과한다.

    입사자와 갈리는 이유는 '앞날이 없다'뿐이다 — 1일 고정 N 면제(r10c8)가 아니라 구간 첫날에 앞 N 을 이을 수 없다는 사실이
    r10c8b 의 차단 원인이다. `join_day == 0` 조건을 지우면 r10c8b 가 빨개진다.
    """
    result, codes = _run(_nurses(), _cfg(ban_single_night=True), _cells("n0", {10: "N", 11: "O"}))
    assert SEQ not in codes and not has_blocking_issues(result)


# ── 리뷰 지적(MEDIUM) — 직접 호출부(라우터 POST /groups/{id}/roster/precheck)도 규칙 값을 한 곳에서 읽는다 ─────────


def _direct_night_input(roster_config, *, nights_per_cycle=3, cycle=5, num_days=30):
    """N 전담 1명 + `nights_per_cycle` 일 N · `cycle`-`nights_per_cycle` 일 쉬는 무늬의 N 수요(예: NNN OO → 30일 18)."""
    by_day = [{"D": 0, "E": 0, "N": 1 if d % cycle < nights_per_cycle else 0} for d in range(num_days)]
    cfg = {"use_mid": False, "daily_shift_requirements": {"D": 0, "E": 0, "N": 1},
           "daily_shift_requirements_by_day": by_day, "max_nig_per_month": 31, **roster_config}
    nurse = PrecheckNurse(nurse_id="N01", allowed_shifts=["N"], join_day=0, leave_day=num_days - 1)
    return PrecheckInput(num_days=num_days, nurses=[nurse], teams=[], roster_config=cfg,
                         team_coverage={}, grade_constraints={})


def test_r10c10_direct_call_recovery_cap_uses_the_same_night_run_cap_as_the_personal_dp():
    """✗ 3연속 N 허용(`allow_three_consecutive_nights`) + 2N→2OFF 인 직접 호출 — 월 나이트 회복 용량도 연속 N 상한 L=3 으로 센다(NNN OO → 30일 19).

    전에는 개인 DP 가 L=3(`allow_three_consecutive_nights`) 인데 회복 용량 DP 는 키가 없다고 L=block(2) 으로 세어(16) 같은 호출 안에서 서로 달랐다.
    NNN OO 무늬 수요 18 은 엔진이 풀 수 있다(n_exact=18 OPTIMAL — 독립 리뷰 실측)고 생성 경로도 통과인데 직접 호출만 부족으로 막혔다.
    """
    inp = _direct_night_input({"allow_three_consecutive_nights": True, "two_offs_after_two_nig": True, "two_offs_after_three_nig": True})
    assert precheck_sequence_params(inp.roster_config)["max_consecutive_nights"] == 3
    assert [i["reason_code"] for i in check_monthly_night_capacity(inp)] == []
    assert max_nights_under_recovery(30, 2, max_run=3) == 19


def test_r10c10b_direct_call_without_allow_three_consecutive_nights_keeps_the_two_night_cap():
    """(가드) `allow_three_consecutive_nights` 가 없으면 개인 DP 의 옛 기본(L=2)과 회복 용량 L 이 같다 — 16 < 수요 18 이라 부족을 낸다."""
    inp = _direct_night_input({"two_offs_after_two_nig": True})
    assert precheck_sequence_params(inp.roster_config)["max_consecutive_nights"] == 2
    issues = check_monthly_night_capacity(inp)
    assert [i["reason_code"] for i in issues] == [NIGHT]
    assert issues[0]["evidence"]["night_capable_nurses"][0]["cap_by_axis"]["recovery"] == 16


def test_r10c11_direct_call_masks_all_transition_bans_like_the_engine():
    """✗ 직접 호출부 — `ban_shift_transitions=False` 면 엔진은 전이 금지 4종(E→D·N→D·N→E·D→N)을 모두 끈다(cp_sat_basic.py:673-677).

    1N 금지 + 3일 O · 4일 N · 6일 D 고정 → 5일 N 이 강제돼 N→D 가 되는데 마스킹으로 금지가 꺼져 엔진은 가능하다.
    전에는 E→D 만 껐고 N→D 는 켜 둔 채라 PER_NURSE 로 막았다.
    """
    nurse = PrecheckNurse(nurse_id="N01", join_day=0, leave_day=29, fixed_off_days={3},
                          fixed_shift_assignments={4: "N", 6: "D"})
    roster = {"use_mid": False, "daily_shift_requirements": {"D": 1, "E": 1, "N": 1}, "ban_single_night": True,
              "max_nig_per_month": 15}

    def codes(**extra):
        inp = PrecheckInput(num_days=30, nurses=[nurse], teams=[], roster_config={**roster, **extra},
                            team_coverage={}, grade_constraints={})
        return {i["reason_code"] for i in run_precheck(inp)["issues"]}

    assert SEQ not in codes(ban_shift_transitions=False)
    assert SEQ in codes(ban_shift_transitions=True)  # 대조: 마스킹이 없으면 N→D 금지가 켜져 막힌다
    assert precheck_sequence_params({"ban_shift_transitions": False, "ban_n_to_d": True}) | {} == {
        **precheck_sequence_params({}), "ban_n_to_d": False, "ban_e_to_d": False, "ban_n_to_e": False}
