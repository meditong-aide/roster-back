"""사다리 모드·성공 모드 시나리오 — S1-L · S1-LT · S1-LB · S1-L3 (설계서 §7.1 EI-1b).

기존 골든 12종(`scenario_defs.SCENARIOS`)과 따로 둔다 — 그쪽 테스트·골든은 그대로다.
모든 사람·병동은 합성(N01·F01·HN01 · 병원 O1 · 병동 G1). 대상 달 2026-11(일요일 시작 · 주말 9일).

★ EI-14b2a4 재설계 — presolve 가 일자별 수요(by_day)를 보게 되면서(뷰의 `_ir_by_day`) 옛 설계 "주말 일자별 요구 E 를 1 로 내려 precheck 만
  통과시키고 presolve 는 기본 요구(E3)로 부족을 본다"가 성립하지 않게 됐다. 이제 부족은 **Hall 결합**으로 만든다: 비주말휴무 9명 중
  E·N 가능자를 정확히 3명(N04~N06)으로 두고 나머지 6명(N07~N12)은 D 전담, 주말도 기본 요구(D4/E3/N2)와 같게 둔다. 시프트별 자격
  인원(E 3 ≥ 3 · N 3 ≥ 2)과 총 가용(9 = 9)은 precheck 를 통과하고, presolve max-flow 는 E+N 수요 5 > 가능자 3 → 주말 부족 2 를 낸다.
  월 야간 상한은 12(N 가능자가 6명이라 8 이면 월 야간 용량 48 < 60 으로 precheck 차단)·`off_first=True`(OFF 하한 min_off 가 precheck
  용량에 안 들어가게 — 원인 진단의 정책 과제약 산술은 off_first 와 무관하게 off_days 를 쓴다). 기준본(`golden_base/dev4ec454e/S1-L*`)은
  순수 dev(`redev_4ec454e/pure_harness`)에서 새 시나리오로 다시 떴다.
S1-L — 사다리 모드. `config_override` 없이 부르는 탐색 3곳(:7680·:7707·:7895)까지 닿게 하는 데이터.
  설계서 §7.1 의 조건(주말휴무 2명 이상 + `weekend_off_load` · `weekend_release_needed ≥ 2` ·
  `n_max > max_nig_per_month` 인 비 N전담 1명)에 더해, 코드에서 확인한 조건 셋을 맞췄다.
  ① 원인 진단(`explain_infeasibility_from_config` :7527)이 λ 단계(lagrangian.py:300 — 미니 CP-SAT
    solve)까지 가면 하네스의 solve 차단이 서비스를 끝낸다. 그래서 그 앞(:290)에서 끝나는
    '정책 과제약'(policy_overconstraint)이 되게 한다: 12명 × (30일 − off_days 8) = 264 < 요구 9 × 30 = 270.
    precheck 용량은 off_days 를 빼지 않으므로(team_grade_precheck.py:155 '소프트') 막히지 않는다.
  ② '개인 즉시모순'·'커버리지 부족'으로 분류되면 탐색을 건너뛴다(:7602). 그래서
    · 야간 '하한'(n_exact/n_min)은 두지 않는다 — 상한 n_max 만(:663 은 하한만 본다)
    · 주말 머릿수는 모자라지 않게 한다(:729 — 주말 가용 12−3=9 = 요구 9)
    · N 가능 인원을 7명 넘게 둬 결합 야간 DP(joint_coverage :170)를 판정 유보로 둔다
  ③ 그런데 presolve(max-flow)는 주말 부족 ≥ 2 를 내야 한다(:188). 머릿수 대신 **근무 자격**으로 만든다:
    주말휴무가 아닌 9명 중 E 가능은 N04 한 명뿐(N05~N12 는 D·N 만). 주말 E 요구 3 중 2 가 빈다.
    precheck 는 일자별 요구(`daily_shift`)를 보므로 주말 E 를 1 로 내려 둔다(사전 차단 방지).
    presolve·원인 진단은 기본 요구(shift_manage D4/E3/N2)만 본다(presolve_diagnosis.py:43).
  그 밖: 월 야간 상한 8(15 면 상한 완화 탐색이 건너뛴다 — undiagnosed_probe.py:32) · raw n2n 4
  (4차 완화 :7219 조건 — stage2 실패 플래그는 기록기 정책이라 운영에선 이 조합이 안 생긴다, ladder.py)
  · `MUS_DIAG_ENABLE=1`(:7336 진단 재solve — 런타임 토글 기록 확인용).
  · `AIDE_PROBE_BUDGET_SEC=0`(완화 탐색 벽시계 상한 끔 — undiagnosed_probe.py:612-637, 0 이하 = 무제한).
    기본 90초를 두면 느린 기계에서 `timed_out` 이 서서 :7659 `_pn_allowed` 가 거짓이 되고 탐색 3곳이
    사라진다. 런타임 토글이라 digest 에서 빠지고 attempts[*].env 기록에만 보인다.
S1-LT — S1-L + treatment_ids 1건(`treatment:soft:grade_max`, 자동 적용) + D 고정근무자 F01.
  treatment_ids 가 있으면 `config_dict` 가 :6361 에서 사본으로 다시 묶여 탐색 3곳이 다른 값을 받는다.
  F01 은 고정근무 차감(:6729)을 일으킨다(고정근무는 주말휴무 대상만 허용 :5375 — 기간표를 둔다).
S1-LB — S1-L + `ban_night_before_fixed_wanted_off=True`(2026-10-13 신설, dev `55dacd4` 사다리
  4차 "전날 N 금지 최소화"가 실제로 발화하는 경우를 닿게 하는 유일한 조합 — `ban_night_before_
  fixed_off` 는 ORM 매핑이 없어 `config_dict`에 실리지 않는다. RULES_DELTA.md §6 반박 참고).
  S1-L 과 달리 사다리 2번째 지점(`ban_night_minimize_retry`, `_ladder_retry` 헬퍼 본문)에
  두 번(하드 복구·누적 해제) 닿은 뒤 나머지 사다리를 그대로 이어간다 — S1-L 전용 단언
  (`test_ladder.py::LADDER_NAMES`)은 건드리지 않고 이 시나리오만의 실행 순서를 별도로 둔다.
S1-L3 — 성공 모드. 기록기가 첫 시도에 결정론 근무표를 돌려줘 후처리(L3)까지 간다.
  S1 병동 + 보건휴가(대상 코드 '보' · 여성 6명) · 수면OFF(코드 '수' · 주기 3) · 초과 OFF 연차 변환
  (코드 '연' · off_days 8 < 패턴 OFF 12). 콜·휴직 가림은 데이터 없이 입구만 지난다.
"""

from __future__ import annotations

import calendar
from datetime import date
from functools import partial

from .engine_env import isolate_engine_env
from .harness import Scenario
from .ladder import redirect_live_graph
from .scenario_defs import BASE_DDL, _request, _rows, s1_ward
from .scenarios import make_head_nurse
from .ward import (
    GROUP_ID,
    OFFICE_ID,
    Ward,
    base_ward,
    nurse_row,
    seed_ward,
    shift_manage_row,
    shift_row,
    stamp,
)

#: 주말휴무 대상(전 근무 가능) · 주말휴무 아닌 E 가능 1명 · 야간 상한 초과 n_max 1명.
LADDER_WEEKEND_OFF = ("N01", "N02", "N03")
#: EI-14b2a4 — 주말 E·N 을 설 수 있는 비주말휴무자 3명(나머지 6명은 D 전담). 주말 E+N 수요 5 > 3 이라 presolve(max-flow)가
#:   Hall 결합 부족 2 를 낸다. 시프트별 자격 인원(E 3 ≥ 3 · N 3 ≥ 2)과 총 가용(9 = 9)은 통과해 precheck 는 안 막는다.
LADDER_E_CAPABLE = ("N04", "N05", "N06")
LADDER_NIGHT_CAP_NURSE = "N05"
#: 월 야간 상한. N 가능 인원이 6명(주말휴무 3 + E·N 가능 3)이라 상한 8 이면 월 야간 용량 48 < 수요 60 으로 precheck 가 막는다 —
#:   12 면 72 ≥ 60. 15 면 상한 완화 탐색이 건너뛴다(undiagnosed_probe.py:32)라 15 미만이어야 한다.
LADDER_MAX_NIG = 12
LADDER_N_MAX = 14
LADDER_TREATMENT = "treatment:soft:grade_max"
#: 기본 요구(shift_manage) · 주말 일자별 요구(daily_shift).
LADDER_BASE_REQ = {"D": 4, "E": 3, "N": 2}
LADDER_WEEKEND_REQ = {"D": 4, "E": 3, "N": 2}   # EI-14b2a4 — presolve 가 일자별 수요를 보므로 주말도 기본과 같게 둔다(예전엔 E 1)


def _d(month: int, day: int) -> date:
    """2026년 날짜 줄임."""
    return date(2026, month, day)


# ── 시나리오 패치(실행 동안만) ──────────────────────────────────────────


#: 완화 탐색 벽시계 상한 끔(0 이하 = 무제한, undiagnosed_probe.py:612-637) — 골든이 시간에 기대지 않게.
PROBE_BUDGET_OFF = {"AIDE_PROBE_BUDGET_SEC": "0"}


def ladder_env(mp) -> None:
    """S1-L·S1-LT — 엔진 env 격리 + MUS 진단 켬 + 탐색 시간 상한 끔 + live graph 쓰기를 임시 폴더로."""
    isolate_engine_env(mp, MUS_DIAG_ENABLE="1", **PROBE_BUDGET_OFF)
    redirect_live_graph(mp)


def skip_post_by_day(mp) -> None:
    """S1-LT — 사후 소비자 입력(`to_roster_ir_post`)의 일자별 수요 겹침(`by_day`)만 끈다(EI-14b2b3 · SPEC D-D14).

    S1-LT 는 D 고정근무자 차감으로 일자별 수요가 평탄이 아니다(평일 D 3 · 주말 4) → 겹침이 `_ir_by_day` 를 만들면 원인 진단이 λ 단계(재풀이)로
    넘어가 이 하네스가 `CpSolver.solve` 를 막는다(`SolveBlocked`, C1 F9 — 14b2a4 가 S1-L 계열을 재설계한 이유). 시나리오를 다시 설계하는 대신
    이 시나리오에서만 그 겹침을 끄고, 겹침 자체는 `test_roster_ir_post_view.py` D-P1~ 단위 시험이 고정한다. 프로덕션 목록은 바뀌지 않는다.
    """
    from services.engine_input import views

    mp.setattr(views, "_OVERLAYS_POST", tuple(item for item in views._OVERLAYS_POST if item[0] != "by_day"))


def post_env(mp) -> None:
    """S1-L3 — 엔진 env 격리 + 탐색 시간 상한 끔 + (실패 경로로 빠져도 저장소에 안 쓰게) live graph 임시 폴더."""
    isolate_engine_env(mp, **PROBE_BUDGET_OFF)
    redirect_live_graph(mp)


# ── 시드 재료 ───────────────────────────────────────────────────────────


def _weekend_off_periods(nurse_ids: tuple[str, ...]):
    """주말휴무 기간표(as-of SSOT — nurse_period_resolver.weekend_off_ids_asof)."""
    def add(db):
        from db.models import NurseWeekendOffPeriod

        for nid in nurse_ids:
            db.add(NurseWeekendOffPeriod(nurse_id=nid, valid_from=_d(1, 1), weekend_off=1,
                                         source="edited", **stamp()))
    return add


def _daily_shift_rows(db) -> None:
    """2026-11 일자별 요구 30행 — 평일 = 기본 요구, 주말 = E 1."""
    from db.models import DailyShift

    for day in range(1, 31):
        weekend = calendar.weekday(2026, 11, day) >= 5
        req = LADDER_WEEKEND_REQ if weekend else LADDER_BASE_REQ
        db.add(DailyShift(office_id=OFFICE_ID, group_id=GROUP_ID, year=2026, month=11, day=day,
                          d_count=req["D"], e_count=req["E"], n_count=req["N"], m_count=0,
                          **stamp()))


def s1l_ward() -> Ward:
    """S1-L — RN 12명(주말휴무 3 · 주말 E·N 가능 비주말휴무 3 · D 전용 6) · 요구 D4/E3/N2 · 월N 상한 12."""
    ward = base_ward(12)
    for row in ward.nurses[6:]:
        row["allowed_shifts"] = ["D"]
    ward.shift_manage = [shift_manage_row(1, "D", ["D"], LADDER_BASE_REQ["D"]),
                         shift_manage_row(2, "E", ["E"], LADDER_BASE_REQ["E"]),
                         shift_manage_row(3, "N", ["N"], LADDER_BASE_REQ["N"])]
    # off_first=True: OFF 하한(min_off)이 precheck 용량 바닥에 들어가지 않게 한다(EI-14r3) — 원인 진단(explain)의 정책 과제약 산술은
    #   off_first 와 무관하게 off_days 를 쓰므로 '용량은 통과 · 산술은 과제약'(아래 docstring ①)이 성립한다.
    ward.config.update(max_nig_per_month=LADDER_MAX_NIG, off_first=True)
    ward.raw = {"n2n_min_gap": 4, "fixed_holiday_off_yn": 0}
    limit = {"nurse_id": LADDER_NIGHT_CAP_NURSE, "group_id": GROUP_ID, "year": 2026, "month": 11,
             "n_max": LADDER_N_MAX, **stamp()}
    ward.extras += [_weekend_off_periods(LADDER_WEEKEND_OFF), _daily_shift_rows,
                    _rows("NurseMonthlyLimit", [limit])]
    return ward


def s1lt_ward() -> Ward:
    """S1-LT — S1-L + D 고정근무자 F01(주말휴무 기간표 포함)."""
    ward = s1l_ward()
    ward.nurses.append(nurse_row("F01", 13, fixed_shift="D"))
    ward.extras.append(_weekend_off_periods(("F01",)))
    return ward


def s1lb_ward() -> Ward:
    """S1-LB — S1-L + `ban_night_before_fixed_wanted_off=True`(사다리 4차 "전날 N 금지 최소화" 발화).

    `ban_night_before_fixed_off` 는 ORM 매핑이 없어 `config_dict`(=`latest_config.__dict__`)에
    실리지 않는다 — 켤 수 있는 자리는 이 컬럼뿐이다(RULES_DELTA.md §6 반박 참고).
    """
    ward = s1l_ward()
    ward.config.update(ban_night_before_fixed_wanted_off=True)
    return ward


def s1l3_ward() -> Ward:
    """S1-L3 — S1 병동 + 보건휴가 · 수면OFF · 초과 OFF 연차 변환."""
    ward = s1_ward()
    for row in ward.nurses[:6]:
        row["gender"] = "여"
    ward.shifts += [
        shift_row("보", 8, "보건", None, "휴가", None, 8, health_leave_target=True),
        shift_row("수", 9, "수면", None, "휴가", None, 9, sleep_off_target=True),
        shift_row("연", 10, "연차", None, "휴가", None, 10, off_swap_target=True),
    ]
    ward.config.update(health_leave_enabled=True, sleep_off_enabled=True, sleep_off_cycle=3,
                       off_swap_enabled=True)
    return ward


# ── 등록 ────────────────────────────────────────────────────────────────


def _scenario(name: str, ward: Ward, description: str, *, stop_at: str, patches, request=None) -> Scenario:
    """병동 재료 → Scenario."""
    return Scenario(
        name=name, seed=partial(seed_ward, ward),
        make_request=request or _request(ward.year, ward.month),
        make_user=make_head_nurse, ddl=tuple(BASE_DDL), patches=tuple(patches),
        stop_at=stop_at, description=description)


def build_ladder_scenarios() -> dict[str, Scenario]:
    """시나리오 이름 → Scenario(실행 순서 = 등록 순서)."""
    items = [
        _scenario("S1-L", s1l_ward(), "사다리 모드 — 자동 완화 4단 + 탐색(MUS·완화 탐색·주말휴무 다인/단일·야간한도 개인)",
                  stop_at="ladder", patches=(ladder_env,)),
        _scenario("S1-LT", s1lt_ward(), "S1-L + treatment_ids 1건 + D 고정근무자 — 탐색 3곳이 latest_config 사본을 받음",
                  stop_at="ladder", patches=(ladder_env, skip_post_by_day),
                  request=_request(2026, 11, treatment_ids=[LADDER_TREATMENT])),
        _scenario("S1-LB", s1lb_ward(), "S1-L + 전날 N 금지 켬 — 사다리 4차(ban_night_minimize_retry) 실제 발화",
                  stop_at="ladder", patches=(ladder_env,)),
        _scenario("S1-L3", s1l3_ward(), "성공 모드 — 결정론 근무표로 후처리 6종(L3) 입력 기록",
                  stop_at="post", patches=(post_env,)),
    ]
    return {s.name: s for s in items}


LADDER_SCENARIOS = build_ladder_scenarios()
