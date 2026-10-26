"""엔진 제약 이름표(RULES) → precheck 판정 — EI-4 역방향 감사(설계서 §2.6 A2 · §5.3).

이 표는 rule id 마다 근무표 생성·저장 이전(precheck)에 이 제약 위반을 미리 잡아 차단·경고
하는 검사기가 있는지를 **현행 그대로** 기록한다. value 는 '함수(또는 모듈)명:REASON_CODE
[시점·효과]' 를 ' | ' 로 이은 문자열이다. 여러 함수·여러 REASON_CODE 가 한 세그먼트에 쓰이면
그 안에서 '·' 로 잇는다. 시점·효과:
  [생성·차단]  생성 요청 때 HTTP 500 을 내고 솔버를 생략한다
  [생성·경고]  severity=warning — 생성을 막지 않는다
  [저장·차단]  설정 저장 때 422/400 으로 막는다

연결이 없으면 `없음: <이유>`(어휘는 `NONE_REASONS`)를 적는다 — 검사 코드가 있어도 키·고정
칸 같은 입력이 생성 경로(precheck/runtime_bridge.py)에 안 닿으면 '없음: 미구현' 으로 적었다
(결함 ①·②, EI-10 몫 — ①은 10c, ②는 10d 에서 닿게 했고 해당 행을 갱신했다). 상세 분류 근거는
`.omc/aide_docs/v2/engine_input/ei4/inv_precheck.json`(저장소 밖, EI-4 조사 산출물).

이 모듈은 데이터만 담는다 — DB·엔진 구현을 import 하지 않는다. 배선(C8' — precheck 와
엔진이 하드락 키에 같은 값)은 EI-10 몫이다.

새 하드 제약을 넣을 때(설계서 §2.7): 이 표에도 1행 추가 — precheck 가 아직 안 보면
`없음: 미구현`. 빠뜨리면 A2(`tests/engine_contract/test_rule_consumers.py`)가 막는다.
"""

from __future__ import annotations

#: 이 표에서 '없음: <이유>' 로 쓸 수 있는 어휘 전부(이 목록 밖 문자열은 A2 가 막는다).
NONE_REASONS: frozenset[str] = frozenset({
    '없음: primary 전용',  # SKIP_PRIMARY 기본값 때문에 lex 경로에서는 걸리지 않는다
    '없음: stage2 이후·자동 완화',  # stage2 부터만 걸리고 기본은 위반칸 최소화나 사다리 해제다
    '없음: stage3 한정·기본 soft',  # 하드여도 lex stage3 에서만 걸리고, 실패하면 stage2 해로 돌아간다(handoff_* 는 기본 soft)
    '없음: 구조 제약',  # 설정 키 없는 정의식이라 혼자서는 해를 없애지 않는다
    '없음: 단계 동결',  # lex_freeze_*
    '없음: 미구현',  # 풀이 전에 해를 없앨 수 있는 하드인데 생성 경로 precheck 가 보지 않는다(검사 자체 미등록, 또는 키·고정 칸 같은 입력이 생성 경로에 안 닿는 경우 포함)
    '없음: 보조식',  # strength=AUX
    '없음: 엔진 내부',  # LNS 가 스스로 만드는 고정(lns_freeze)이다
    '없음: 운영자 전용',  # 운영자 전용 설정이라서 검사를 안 거는 배제 규칙이 없다(0행)
    '없음: 의도적 제외',  # precheck 주석이 일부러 안 본다고 명시한다(off_days 를 soft 로 취급)
    '없음: 자동 완화',  # HARD_AUTO_RELAX — 실패하면 사다리가 자동으로 풀고 다시 시도한다
    '없음: 하드락',  # precheck 쪽에 하드락 8종이라서 검사를 안 거는 배제 규칙이 없다(0행)
})

#: rule id(RULES ∪ RULE_PREFIXES 접두 id) → RULE_PRECHECK.
RULE_PRECHECK: dict[str, str] = {
    'forbidden_shift': '없음: 미구현',  # app/services/engine_contract/rules.py:50-61 · app/services/precheck/team_grade_precheck.py:112-113 — ★(2026-10-14 반박 검토) '없음: 구조 제약'(이 표 정의: "설정 키 없는 정의식") 에서 고침. forbidden_shift 는 governing_keys 가 4개(shifts.default_shift·shift_gb·type·runtime.fixed_cells)라 '설정 키 없음' 정의에 안 맞는다 — RULE_CARD 는 같은 규칙을 이미 '없음: 미구현'으로 적어 둬 두 표의 판정이 갈렸다
    'enforce_4o': '없음: 미구현',  # app/services/cp_sat_basic.py:625 · app/services/precheck/runtime_bridge.py:333-367  # 증명 가능한 산술이 엔진 내부 의미(is_pure_o·구조 OFF·고정 4개 면제)에 기대 오차단 위험 — 14c SPEC §2
    'weekend_off_only': 'check_global_day_capacity_shortage:GLOBAL_DAY_CAPACITY_SHORTAGE[생성·차단] | check_global_shift_allowed_shortage:GLOBAL_SHIFT_ALLOWED_SHORTAGE[생성·차단] | check_daily_night_shortage:N_CAPACITY_SHORTAGE[생성·차단] | check_capacity_total_shortage:CAPACITY_TOTAL_SHORTAGE[생성·차단] | check_monthly_night_capacity:MONTHLY_NIGHT_CAPACITY_SHORTAGE[생성·차단] | _check_forced_off:MONTHLY_LIMIT_O_MAX_BELOW_FORCED_OFF·MONTHLY_LIMIT_WORK_MIN_EXCEEDS_AVAILABLE_WEEKDAYS[저장·차단]',  # app/services/precheck/runtime_bridge.py:_engine_hard_off_days · app/services/roster_create_service.py:6384-6390 외 — ★(2026-10-19 리뷰 반영) 개인 OFF 바닥의 주말 감산은 `weekend_off_only_enable` 플래그가 켜졌을 때 주말휴무자의 근무 구간 `[입사일, 퇴사일−1]` 안 주말만, 주휴 맵의 날과 **날짜 집합으로 한 번만** 센다(엔진 fallback_lex.py:1496-1526 과 같다). 플래그를 끄고(`treatment:disable:weekend_off_only`) 재생성하면 이 용량 검사기들도 주말휴무 몫을 더는 깎지 않는다 — 10f 리뷰 전에는 플래그와 근무 구간을 안 보고 그 달 주말 전체를 깎았다(2026-10-14 반박 검토가 짚은 어긋남)
    'initial_forbidden': '없음: 미구현',  # app/services/engine_contract/rules.py:83-109 · app/services/precheck/runtime_bridge.py:333-367
    'exactly_one_shift': '없음: 구조 제약',  # app/services/engine_contract/rules.py:110-116 · app/services/precheck/team_grade_precheck.py:425-438
    'preceptee_follow': 'check_preceptee_sync_mismatch:PRECEPTEE_SYNC_MISMATCH[생성·차단]',  # app/services/precheck/team_grade_precheck.py:1093-1173 · app/services/precheck/runtime_bridge.py:235-236 외
    'zero_demand_block': '없음: 미구현',  # app/services/engine_contract/rules.py:128-155 · app/services/precheck/team_grade_precheck.py:384-401 외
    'coverage_min': 'check_global_day_capacity_shortage:GLOBAL_DAY_CAPACITY_SHORTAGE[생성·차단] | check_global_shift_allowed_shortage:GLOBAL_SHIFT_ALLOWED_SHORTAGE[생성·차단] | check_capacity_total_shortage:CAPACITY_TOTAL_SHORTAGE[생성·차단] | check_daily_night_shortage:N_CAPACITY_SHORTAGE[생성·차단] | check_monthly_night_capacity:MONTHLY_NIGHT_CAPACITY_SHORTAGE[생성·차단]',  # app/services/precheck/team_grade_precheck.py:425-438 · app/services/precheck/team_grade_precheck.py:112-125 외 — ★(2026-10-14 반박 검토) 이 목록은 D/E/N 축만 덮는다. M(MID) 커버리지의 생성시점 차단은 team_grade_precheck 가 아니라 별도의 `validate_mid_hard_feasibility`(app/services/cp_sat/mid_feasibility.py, use_mid=True·고정 M 칸+가용 인원 vs 요구 M 비교, roster_create_service.py:3550-3557 에서 호출해 raise Exception → 500)다. 정식 REASON_CODE 없이 f-string 예외라 이 표의 '함수명:REASON_CODE[시점·효과]' 형식에 그대로 못 넣는다(테스트 A2 의 REASON_CODE 존재 검사를 못 통과) — 이 코드는 config_dict['fixed_cells'](nurse_index 형식)·initial_constraints.forced_off/forbidden 을 직접 읽어 생성 경로 고정 칸이 precheck 류 검사에 전혀 안 닿는다는 결론의 M 축 예외다 (★EI-10d 로 D/E/N 칸은 번역돼 precheck 에 닿는다 — 이 설명은 M 코드 커버리지 차단의 별도 경로에 대한 것이다)
    'coverage_short': '없음: 보조식',  # 근거: ei4/inv_*.json 참고(이 행은 부재 확인 — 특정 file:line 없음)
    'coverage_max': '없음: 미구현',  # app/services/engine_contract/rules.py:194-210 · app/services/precheck/runtime_bridge.py:333-367 외
    'coverage_over': '없음: 보조식',  # 근거: ei4/inv_*.json 참고(이 행은 부재 확인 — 특정 file:line 없음)
    'off_first_coverage_cap': 'detect_night_forced_oversupply:MONTHLY_NIGHT_FORCED_OVERSUPPLY[생성·차단]',  # app/services/ontology_graph/lagrangian.py:525-559 · app/services/roster_create_service.py:6684-6769
    'max_cov_off_equalize': '없음: 보조식',  # 근거: ei4/inv_*.json 참고(이 행은 부재 확인 — 특정 file:line 없음)
    'isolated_off_slack': '없음: 보조식',  # 근거: ei4/inv_*.json 참고(이 행은 부재 확인 — 특정 file:line 없음)
    'transition_ban': 'check_per_nurse_sequence:PER_NURSE_SEQUENCE_INFEASIBLE[생성·차단 — 고정 칸이 만드는 N→D·E→D·N→E 충돌만, 두 칸 다 고정이면 엔진이 면제]',  # app/services/precheck/team_grade_precheck.py:1220-1261 · app/services/precheck/fixed_cell_codes.py:53-78 — ★(EI-10d) 생성 경로 고정 칸이 번역돼 개인 DP 에 닿는다(전에는 bridge 가 칸을 전부 버렸다)
    'mid_transition': 'check_per_nurse_sequence:PER_NURSE_SEQUENCE_INFEASIBLE[생성·차단 — MID 병동에서 고정 칸이 만드는 E→M·N→M 충돌만, 엔진처럼 고정 면제 없음]',  # app/services/engine_contract/rules.py:287-300 · app/services/precheck/per_nurse_sequence_feasibility.py:_transition_banned — ★(EI-14r2) 개인 DP 가 M 전이를 따른다(M→M 허용, 월 첫날 직전은 O). 전에는 MID 병동에서 고정 N→고정 M·고정 E→고정 M 이 엔진 stage1 INFEASIBLE 인데 precheck 는 통과했다
    'mid_to_day_ban': '없음: 의도적 제외',  # app/services/engine_contract/rules.py:301-309 · app/services/precheck/per_nurse_sequence_feasibility.py:_transition_banned — ★(EI-14C1r) 개인 DP 로는 관측 불가 — 한쪽만 고정이면 자유 칸 OFF 우회, 양끝 고정이면 엔진이 면제한다. 14r2 가 넣었던 M→D 금지는 어떤 입력도 막지 못하는 no-op 이라 뺐다
    'not_one_night': 'check_per_nurse_sequence:PER_NURSE_SEQUENCE_INFEASIBLE[생성·차단 — 1N 금지가 강제하는 이웃 N 과 고정 칸의 충돌만, 월 N 상한 1·1일 고정 N 은 엔진이 면제]',  # app/services/precheck/team_grade_precheck.py:1201-1261 · app/services/precheck/hard_lock_view.py — ★(EI-10c) 하드락 값이 실리고 (EI-10d) 고정 칸이 닿아 개인 DP 가 걸린다. 칸이 없는 간호사는 'O 로 채우면 늘 가능'이라 판정하지 않는다
    'ban_night_before_fixed_off': '없음: 미구현',  # app/services/engine_contract/rules.py:324-364 · app/services/roster_create_service.py:7160-7173
    'ban_night_before_fixed_off_slack': '없음: 보조식',  # 근거: ei4/inv_*.json 참고(이 행은 부재 확인 — 특정 file:line 없음)
    'lex_freeze_ban_night': '없음: 단계 동결',  # 근거: ei4/inv_*.json 참고(이 행은 부재 확인 — 특정 file:line 없음)
    'off_window_requirement': '없음: 미구현',  # app/services/engine_contract/rules.py:398-408 · app/services/precheck/runtime_bridge.py:333-367 외
    'max_consecutive_work': 'check_capacity_total_shortage:CAPACITY_TOTAL_SHORTAGE[생성·차단] | check_monthly_night_capacity:MONTHLY_NIGHT_CAPACITY_SHORTAGE[생성·차단] | check_per_nurse_sequence:PER_NURSE_SEQUENCE_INFEASIBLE[생성·차단 — 고정 칸이 연속근무 상한을 넘게 만드는 경우만]',  # app/services/precheck/team_grade_precheck.py:170-184 · app/services/precheck/runtime_bridge.py:356-361 외 — ★(EI-10d) 마지막 세그먼트는 고정 칸이 개인 DP 에 닿으면서 생겼다
    'same_shift_hard_k': '없음: 자동 완화',  # app/services/engine_contract/rules.py:420-433 · app/services/roster_create_service.py:7028-7049 외
    'isolated_work': '없음: 자동 완화',  # app/services/engine_contract/rules.py:434-443 · app/services/roster_create_service.py:7099-7111 외
    'consecutive_night_cap': 'check_per_nurse_sequence:PER_NURSE_SEQUENCE_INFEASIBLE[생성·차단 — 고정 칸이 연속 N 상한을 넘게 만드는 경우만]',  # app/services/precheck/team_grade_precheck.py:1192-1261 · app/services/precheck/hard_lock_view.py — ★(EI-10c) 엔진 실효 상한(`max_consecutive_nights`)이 실리고 (EI-10d) 고정 칸이 닿아 개인 DP 가 걸린다
    'cnight_excess': '없음: 보조식',  # 근거: ei4/inv_*.json 참고(이 행은 부재 확인 — 특정 file:line 없음)
    'max_night': 'check_monthly_night_capacity:MONTHLY_NIGHT_CAPACITY_SHORTAGE[생성·차단] | check_allowed_vs_limits:MONTHLY_LIMIT_NIGHT_DEDICATED_N_OVER_MAX_NIGHT[저장·차단 — 허용근무 기간 저장 때만]',  # app/services/precheck/team_grade_precheck.py:888-1030 · app/services/precheck/runtime_bridge.py:350-354 외
    'allowed_shift_mask': 'check_global_shift_allowed_shortage:GLOBAL_SHIFT_ALLOWED_SHORTAGE[생성·차단] | check_daily_night_shortage:N_CAPACITY_SHORTAGE[생성·차단] | check_monthly_night_capacity:MONTHLY_NIGHT_CAPACITY_SHORTAGE[생성·차단] | check_grade_min_available_shortage:GRADE_MIN_AVAILABLE_SHORTAGE[생성·차단] | check_grade_max_sum_below_need:GRADE_MAX_SUM_BELOW_NEED[생성·차단] | check_grade_antipair_forces_shortage:GRADE_ANTIPAIR_FORCES_SHORTAGE[생성·차단] | check_allowed_vs_limits·_check_work_shifts:MONTHLY_LIMIT_NOT_IN_WORK_SHIFTS[저장·차단] | _check_night_dedicated:MONTHLY_LIMIT_NIGHT_DEDICATED_HAS_DAY_OR_EVENING[저장·차단]',  # app/services/precheck/runtime_bridge.py:48-89 · app/services/precheck/team_grade_precheck.py:128-136 외
    'week_off_missing': '없음: 보조식',  # 근거: ei4/inv_*.json 참고(이 행은 부재 확인 — 특정 file:line 없음)
    'night_block_end': '없음: 보조식',  # 근거: ei4/inv_*.json 참고(이 행은 부재 확인 — 특정 file:line 없음)
    'carryover_boundary': '없음: 미구현',  # app/services/engine_contract/rules.py:507-521 · app/services/precheck/runtime_bridge.py:333-367
    'recovery_3n2off': 'check_monthly_night_capacity:MONTHLY_NIGHT_CAPACITY_SHORTAGE[생성·차단] | check_per_nurse_sequence:PER_NURSE_SEQUENCE_INFEASIBLE[생성·차단 — 고정 N 블록 뒤 회복 OFF 와 고정 칸의 충돌만]',  # app/services/precheck/team_grade_precheck.py:918-936 · app/services/precheck/hard_lock_view.py — ★(EI-10c) 엔진 하드락 값이 prepare_precheck_input 으로 실려 회복 용량 축이 살았다. (EI-10d) 고정 칸이 번역돼 닿으면서 개인 DP(PER_NURSE_SEQUENCE_INFEASIBLE)도 걸린다
    'recovery_2n2off': 'check_monthly_night_capacity:MONTHLY_NIGHT_CAPACITY_SHORTAGE[생성·차단] | check_per_nurse_sequence:PER_NURSE_SEQUENCE_INFEASIBLE[생성·차단 — 고정 N 블록 뒤 회복 OFF 와 고정 칸의 충돌만]',  # app/services/precheck/team_grade_precheck.py:918-936 · app/services/precheck/hard_lock_view.py — ★(EI-10c) 엔진 하드락 값이 prepare_precheck_input 으로 실려 회복 용량 축이 살았다. (EI-10d) 고정 칸이 번역돼 닿으면서 개인 DP(PER_NURSE_SEQUENCE_INFEASIBLE)도 걸린다
    'n2n_min_gap': '없음: stage2 이후·자동 완화',  # app/services/engine_contract/rules.py:544-579 · app/services/roster_create_service.py:7223-7248
    'n2n_min_gap_slack': '없음: 보조식',  # 근거: ei4/inv_*.json 참고(이 행은 부재 확인 — 특정 file:line 없음)
    'lex_freeze_n2n_gap': '없음: 단계 동결',  # 근거: ei4/inv_*.json 참고(이 행은 부재 확인 — 특정 file:line 없음)
    'pattern_nod': '없음: 보조식',  # 근거: ei4/inv_*.json 참고(이 행은 부재 확인 — 특정 file:line 없음)
    'pattern_noe': '없음: 보조식',  # 근거: ei4/inv_*.json 참고(이 행은 부재 확인 — 특정 file:line 없음)
    'pattern_eod': '없음: 보조식',  # 근거: ei4/inv_*.json 참고(이 행은 부재 확인 — 특정 file:line 없음)
    'off_quota': '없음: 보조식',  # 근거: ei4/inv_*.json 참고(이 행은 부재 확인 — 특정 file:line 없음)
    'min_off': 'check_capacity_total_shortage:CAPACITY_TOTAL_SHORTAGE[생성·차단 — OFF 하드 하한의 하한값이 근무 용량 바닥에 반영될 때만] | check_monthly_night_capacity:MONTHLY_NIGHT_CAPACITY_SHORTAGE[생성·차단]',  # app/services/precheck/team_grade_precheck.py:_hard_off_floor · app/services/precheck/runtime_bridge.py:_engine_min_off_required · app/services/cp_sat/fallback_lex.py:3253-3345 — ★(EI-14r3) bridge 가 compute_off_bounds 로 간호사별 min_off_required 를 구해 `max(global + p, min_off_required)` 로 용량 바닥에 쓴다(off_first·N 전담·주말휴무·프리셉티 제외, 휴가 구분 안 함 — 오차단 0). 남는 빈칸: 개인 월 근무 하한 합 + off_days > 가용일 같은 개인 단위 충돌은 생성 시점 검사가 없다
    'min_off_lower_slack': '없음: 보조식',  # 근거: ei4/inv_*.json 참고(이 행은 부재 확인 — 특정 file:line 없음)
    'min_off_missing': '없음: 보조식',  # 근거: ei4/inv_*.json 참고(이 행은 부재 확인 — 특정 file:line 없음)
    'off_cap_slack': '없음: 보조식',  # 근거: ei4/inv_*.json 참고(이 행은 부재 확인 — 특정 file:line 없음)
    'off_cap': '없음: 미구현',  # app/services/engine_contract/rules.py:728-750 · app/services/cp_sat/fallback_lex.py:3434 외
    'lex_freeze_coverage': '없음: 단계 동결',  # 근거: ei4/inv_*.json 참고(이 행은 부재 확인 — 특정 file:line 없음)
    'lex_freeze_safety': '없음: 단계 동결',  # 근거: ei4/inv_*.json 참고(이 행은 부재 확인 — 특정 file:line 없음)
    'daily_coverage_equalize': '없음: 보조식',  # 근거: ei4/inv_*.json 참고(이 행은 부재 확인 — 특정 file:line 없음)
    'lex_off_range': '없음: 보조식',  # 근거: ei4/inv_*.json 참고(이 행은 부재 확인 — 특정 file:line 없음)
    'lex_freeze_pass': '없음: 단계 동결',  # 근거: ei4/inv_*.json 참고(이 행은 부재 확인 — 특정 file:line 없음)
    'lex_grade_short': '없음: 보조식',  # 근거: ei4/inv_*.json 참고(이 행은 부재 확인 — 특정 file:line 없음)
    'lex_n_range': '없음: 보조식',  # 근거: ei4/inv_*.json 참고(이 행은 부재 확인 — 특정 file:line 없음)
    'lex_d_range': '없음: 보조식',  # 근거: ei4/inv_*.json 참고(이 행은 부재 확인 — 특정 file:line 없음)
    'lex_n2n_pair': '없음: 보조식',  # 근거: ei4/inv_*.json 참고(이 행은 부재 확인 — 특정 file:line 없음)
    'lex_de_balance': '없음: 보조식',  # 근거: ei4/inv_*.json 참고(이 행은 부재 확인 — 특정 file:line 없음)
    'lex_freeze_grade': '없음: 단계 동결',  # 근거: ei4/inv_*.json 참고(이 행은 부재 확인 — 특정 file:line 없음)
    'lex_freeze_stage3': '없음: 단계 동결',  # 근거: ei4/inv_*.json 참고(이 행은 부재 확인 — 특정 file:line 없음)
    'lns_freeze': '없음: 엔진 내부',  # app/services/engine_contract/rules.py:959-967
    'preceptor_pair_bonus': '없음: 보조식',  # 근거: ei4/inv_*.json 참고(이 행은 부재 확인 — 특정 file:line 없음)
    'grade_monthly_short': '없음: 보조식',  # 근거: ei4/inv_*.json 참고(이 행은 부재 확인 — 특정 file:line 없음)
    'grade_min': 'check_grade_min_exceeds_max:GRADE_MIN_EXCEEDS_MAX[생성·비차단 — _BLOCKING_CODES 밖, 다른 차단 동반 시만 payload 반영] | check_grade_min_sum_exceeds_need:GRADE_MIN_SUM_EXCEEDS_NEED[생성·차단] | check_grade_min_available_shortage:GRADE_MIN_AVAILABLE_SHORTAGE[생성·차단] | validate_grade_change:GRADE_MIN_AVAILABLE_SHORTAGE_AFTER_CHANGE·GRADE_DAILY_SUM_EXCEEDS_AVAILABLE_AFTER_CHANGE[저장·차단]',  # app/services/precheck/team_grade_precheck.py:312-341 · app/services/precheck/runtime_bridge.py:169-196 외 — ★(2026-10-14 반박 검토) GRADE_MIN_EXCEEDS_MAX 는 payload.py `_BLOCKING_CODES`(생성 차단 집합)에 없고 team_grade_precheck.py `_CONFIG_ERROR_CODES`(1239행)에만 있다. 생성 경로는 `run_runtime_precheck(..., stop_on_config_error=False)`(roster_create_service.py:6459)로 호출하므로 이 reason 단독으로는 생성을 막지 못한다(실측: issues=[(GRADE_MIN_EXCEEDS_MAX,'hard')] 인데 has_blocking_issues=False)
    'grade_cascade_short': '없음: 보조식',  # 근거: ei4/inv_*.json 참고(이 행은 부재 확인 — 특정 file:line 없음)
    'grade_min_soft_slack': '없음: 보조식',  # 근거: ei4/inv_*.json 참고(이 행은 부재 확인 — 특정 file:line 없음)
    'grade_max_soft_slack': '없음: 보조식',  # 근거: ei4/inv_*.json 참고(이 행은 부재 확인 — 특정 file:line 없음)
    'grade_max': 'check_grade_min_exceeds_max:GRADE_MIN_EXCEEDS_MAX[생성·차단] | check_grade_max_sum_below_need:GRADE_MAX_SUM_BELOW_NEED[생성·차단] | check_grade_antipair_forces_shortage:GRADE_ANTIPAIR_FORCES_SHORTAGE[생성·차단]',  # app/services/precheck/team_grade_precheck.py:344-381 · app/services/precheck/runtime_bridge.py:169-196 외
    'team_min_present': '없음: 보조식',  # 근거: ei4/inv_*.json 참고(이 행은 부재 확인 — 특정 file:line 없음)
    'team_min_soft_slack': '없음: 보조식',  # 근거: ei4/inv_*.json 참고(이 행은 부재 확인 — 특정 file:line 없음)
    'team_min': 'check_team_size_insufficient:TEAM_SIZE_INSUFFICIENT[생성·경고] | check_team_min_exceeds_global_need:TEAM_MIN_EXCEEDS_GLOBAL_NEED[생성·경고] | check_team_active_members_insufficient:TEAM_ACTIVE_MEMBERS_INSUFFICIENT[생성·경고] | check_team_shift_allowed_shortage:TEAM_SHIFT_ALLOWED_SHORTAGE[생성·경고] | check_fixed_assign_breaks_team_min:FIXED_ASSIGN_BREAKS_TEAM_MIN[생성·경고] | validate_team_min_shift_capacity:TEAM_EMPTY_BUT_MIN_SET·TEAM_ACTIVE_LT_DAY_MIN_SUM·TEAM_MONTHLY_CAPACITY_LT_MIN_TOTAL[저장·차단]',  # app/services/precheck/team_grade_precheck.py:286-309 · app/services/precheck/payload.py:269-277 외
    'handoff_or_link': '없음: 보조식',  # 근거: ei4/inv_*.json 참고(이 행은 부재 확인 — 특정 file:line 없음)
    'handoff_same_soft_slack': '없음: 보조식',  # 근거: ei4/inv_*.json 참고(이 행은 부재 확인 — 특정 file:line 없음)
    'handoff_same': '없음: stage3 한정·기본 soft',  # app/services/engine_contract/rules.py:1170-1183
    'handoff_adj_soft_slack': '없음: 보조식',  # 근거: ei4/inv_*.json 참고(이 행은 부재 확인 — 특정 file:line 없음)
    'handoff_adj': '없음: stage3 한정·기본 soft',  # app/services/engine_contract/rules.py:1198-1211
    'lookahead_off_cap': '없음: primary 전용',  # app/services/engine_contract/rules.py:1212-1223 · app/services/cp_sat_basic.py:1511
    'lookahead_off_spread_max': '없음: 보조식',  # 근거: ei4/inv_*.json 참고(이 행은 부재 확인 — 특정 file:line 없음)
    'monthly_cap_slack': '없음: 보조식',  # 근거: ei4/inv_*.json 참고(이 행은 부재 확인 — 특정 file:line 없음)
    'lex_freeze_monthly_cap': '없음: 단계 동결',  # 근거: ei4/inv_*.json 참고(이 행은 부재 확인 — 특정 file:line 없음)
    'fixed_assignment:*': 'check_fixed_assign_violates_allowed:FIXED_ASSIGN_VIOLATES_ALLOWED[생성·차단] | check_per_nurse_sequence:PER_NURSE_SEQUENCE_INFEASIBLE[생성·차단] | check_fixed_assign_exceeds_need:FIXED_ASSIGN_EXCEEDS_NEED[생성·경고] | check_fixed_off_exceeds_span:FIXED_OFF_EXCEEDS_SPAN[생성·경고]',  # app/services/precheck/fixed_cell_codes.py:53-78 · app/services/precheck/runtime_bridge.py:134-176 · app/services/roster_create_service.py:5981-5989 외 — ★(EI-10d) 생성 경로 고정 칸(주휴·휴가·확정 원티드·회복 OFF)이 번역돼 precheck 에 닿는다. 앞 둘은 엔진도 불가능이라 차단이고, 뒤 둘(고정 초과·고정 OFF 과다)은 엔진이 불가능으로 안 봐서(fallback_lex.py:1732-1738·1278-1280) 경고다. 비표준 코드(D1 등)·프리셉티 칸은 건너뛰고 전월 꼬리(월경계)는 아직 모른다(둘 다 느슨한 쪽 — EI-13a/10g)
    'monthly_limit_*': 'check_monthly_night_capacity:MONTHLY_NIGHT_CAPACITY_SHORTAGE[생성·차단 — n_exact/n_max 상한] | detect_night_forced_oversupply:MONTHLY_NIGHT_FORCED_OVERSUPPLY[생성·차단 — n_exact/n_min 하한] | validate_monthly_limit_row:MONTHLY_LIMIT_VALUE_EXCEEDS_ACTIVE_DAYS·SUM_MAX_BELOW_SUM_MIN·SUM_MAX_BELOW_ACTIVE_DAYS·NIGHT_DEDICATED_HAS_DAY_OR_EVENING·NOT_IN_WORK_SHIFTS·O_MAX_BELOW_FORCED_OFF·WORK_MIN_EXCEEDS_AVAILABLE_WEEKDAYS[저장·차단] | nurse_monthly_limit_service:MONTHLY_LIMIT_GROUP_EXACT_SUM_EXCEEDS·GROUP_MIN_SUM_EXCEEDS·MONTH_EXACT_SUM_EXCEEDS·MONTH_MIN_SUM_EXCEEDS[저장·차단] | check_allowed_vs_limits:MONTHLY_LIMIT_NOT_IN_WORK_SHIFTS·NIGHT_DEDICATED_N_OVER_MAX_NIGHT[저장·차단 — 허용근무 기간 저장] | validate_team_min_shift_capacity:TEAM_MONTHLY_CAPACITY_LT_MIN_TOTAL[저장·차단 — o_exact/o_min]',  # app/services/precheck/runtime_bridge.py:286-295 · app/services/precheck/team_grade_precheck.py:947-958 외
}
