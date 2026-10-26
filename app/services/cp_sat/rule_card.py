"""엔진 제약 이름표(RULES) → 해결책 카드 레버 id — EI-4 역방향 감사(설계서 §2.6 A2 · §5.3).

이 표는 rule id 마다 사용자에게 뜨는 "해결책 카드"가 실제로 그 제약을 겨냥하는 레버를
갖는지를 **현행 그대로** 기록한다. value 는 레버 id 를 ' · ' 로 이은 문자열이다. 접두:
  probe:<id>             RELAX_CATALOG(`undiagnosed_probe.py`) 탐색 카드(자동)
  lever:<config_key>      precheck 산술 레버 카드(`_LEVER_BY_EVIDENCE_KEY`, 자동)
  personal_night_cap:*    개인 월 야간 상한 카드(release 로 DB 저장)
  treatment:<id>          ontology.yaml `treatments:` 처방 번들(id 그대로가 yaml 키)
  cause:<tail>            mcs_trace 원인 카드 — 고정 3+종(banned_release·allowed_add·
                          fix_all_personal 등, `mcs_trace.py` 리터럴) 또는 family 꼬리
                          (`mcs_trace._FAMILY_KO` 키 — lagrangian 진단이 내는 top_family)
  fix_plan:<action_id>    수동 안내(`fix_plan.py` action_id)
  fix_plan:axis:<axis_id> 수동 안내(axis_registry 의 axis_id, T2 축만)
  fix_reason:<CODE>       fix_location 이 원인에 붙이는 수정 위치(REASON_CODE)

효과 기준으로 센다 — 카드가 바꾸는 키·데이터가 이 규칙의 입력에 실제로 닿을 때만 그 규칙의
레버로 쳤다(엔진이 안 읽는 별칭 레버·생산자가 0인 매핑·적용 때 예외로 무시되는 처방은 값에서
빼고 `없음: 별칭 레버(엔진 미독)` 등으로 적었다). 상세 분류 근거는
`.omc/aide_docs/v2/engine_input/ei4/inv_card.json`(저장소 밖, EI-4 조사 산출물).

이 모듈은 데이터만 담는다 — ortools·엔진·DB 를 import 하지 않는다. 하드락 8종을 완화하는 레버는
EI-11 카드 필터(`policy.card_filter` — 카드가 나가는 싱크에서 거른다)·탐색 카탈로그(`RELAX_CATALOG`)가
지운다. 그래서 하드락 규칙 행에는 상한 안 레버만 남고, 남는 레버가 없는 행은 `없음: 하드락` 이다.
A2(`test_rule_consumers`)가 하드락 행의 토큰이 금지 처방·BLOCK 원인 가족·막힌 축·BLOCK 탐색 항목과
겹치지 않는지 본다 — 남은 공백은 `KNOWN_HARDLOCK_CARD_GAPS` 에 적는다.

새 하드 제약을 넣을 때(설계서 §2.7): 이 표에도 1행 추가 — 카드가 아직 없으면 `없음: 미구현`.
빠뜨리면 A2(`tests/engine_contract/test_rule_consumers.py`)가 막는다.
"""

from __future__ import annotations

#: 이 표에서 '없음: <이유>' 로 쓸 수 있는 어휘 전부(이 목록 밖 문자열은 A2 가 막는다).
NONE_REASONS: frozenset[str] = frozenset({
    '없음: primary 전용',  # 기본 lex 경로와 카드 탐색 재계산에서는 생기지 않는 제약이라 카드가 뜨지 않는다
    '없음: 구조 제약',  # 설정 키 없는 정의식(exactly_one_shift)
    '없음: 내부 탐색',  # primary LNS 의 이웃 고정 장치(lns_freeze)
    '없음: 단계 동결',  # lex_freeze_*
    '없음: 자동 완화',  # HARD_AUTO_RELAX — 사다리가 먼저 풀고, 카드 대신 성공 응답의 완화 안내(원인→조정, `precheck/relaxation_notice.py`)로 알린다 — EI-14c
    '없음: 미구현',  # 지정 카드 모듈(undiagnosed_probe·mcs_trace)과 처방(ontology treatments) 어디에도 이 규칙을 풀거나 바꾸는 레버가 없다
    '없음: 별칭 레버(엔진 미독)',  # 카드가 레버를 내지만 엔진이 그 키를 이 규칙에서 읽지 않아 실효가 없다(설계 BR-3 — EI-12 별칭 정규화로 해소돼 지금 이 사유를 쓰는 행은 0)
    '없음: 보조식',  # strength=AUX
    '없음: 운영자 전용',  # 숨은 설정(#39·#19) 손잡이도 Q67 결정으로 카드의 1회성 옵션이 허용돼, '운영자 전용이라 카드가 없다'는 사유가 성립하지 않는다(0행)
    '없음: 하드락',  # 하드락 8종을 완화하는 레버는 EI-11 카드 필터·카탈로그가 지운다 — 남는 레버가 없는 하드락 행(#2·#3·#4·#5·#7 직접 행). #1·#6 행은 상한 안 레버가 남아 레버 id 를 그대로 적는다 — #6 의 수동 안내 `fix_plan:axis:monthly_n_cap`('+1')은 11c 부터 상한 문구('(최대 15회 — 하드락 #6)')가 붙는다
})

#: rule id(RULES ∪ RULE_PREFIXES 접두 id) → RULE_CARD.
RULE_CARD: dict[str, str] = {
    'forbidden_shift': '없음: 미구현',  # app/services/semantics/ontology.yaml:824-1089 · app/services/cp_sat/fallback_lex.py:1387 외
    'enforce_4o': 'probe:disable_enforce_4o',  # app/services/cp_sat/undiagnosed_probe.py `RELAX_CATALOG` 맨 끝 — ★EI-14c-4: 켠 병동(enforce_4o_hard=True)에서만 나오는 검증형 카드. 원인·family 열(MUS 등록)은 엔진 쓰기 대기
    'weekend_off_only': 'treatment:disable:weekend_off_only · cause:fix_all_personal · cause:weekend_off',  # app/services/semantics/ontology.yaml:1057-1066 · app/services/constraint_impact/control.py:353-357 외
    'initial_forbidden': 'cause:banned_release · cause:allowed_add · fix_plan:rebalance_fixed_and_carryover · fix_reason:FIXED_ASSIGN_VIOLATES_ALLOWED',  # app/services/ontology_graph/mcs_trace.py:292-320 · app/services/roster_create_service.py:6529 외 — ★EI-11 11a-1: 하드락 #2·#3 간접 완화 토큰(탐색 전이 3종·연속 N·1N, 처방 transition_ban)을 뺐다 — 카탈로그 삭제·카드 필터
    'exactly_one_shift': '없음: 구조 제약',  # app/services/engine_contract/rules.py:111 · app/services/cp_sat/fallback_lex.py:1621
    'preceptee_follow': 'probe:disable_preceptee_sync · fix_reason:PRECEPTEE_SYNC_MISMATCH',  # app/services/cp_sat/undiagnosed_probe.py:93-94 ·app/services/cp_sat_basic.py:593 외 — ★fix_reason 쪽은 효과가 없다. precheck 의 check_preceptee_sync_mismatch 가 PRECEPTEE_SYNC_MISMATCH 를 내는 경로(team_grade_precheck.py)는 preceptee_follows_preceptor 값을 전혀 보지 않는다(브리지가 그 키를 안 넘김) — 그래서 fix_location.py 가 안내하는 "교육(프리셉티) 함께 근무" 끄기(preceptee_follows_preceptor=False)를 적용해 재생성해도 precheck 가 같은 이유로 또 차단한다(disable 로 실제 해소되는 건 probe:disable_preceptee_sync 뿐, MUS 경로 한정)
    'zero_demand_block': '없음: 미구현',  # app/services/semantics/ontology.yaml:824-1089 · app/services/semantics/ontology.yaml:891-899 외
    'coverage_min': 'fix_plan:adjust_coverage_or_supply · fix_plan:adjust_daily_requirement_stepwise · fix_plan:axis:night_capacity · fix_plan:axis:day_capacity · fix_plan:axis:evening_capacity · fix_plan:axis:mid_capacity · fix_reason:CAPACITY_TOTAL_SHORTAGE · fix_reason:GLOBAL_DAY_CAPACITY_SHORTAGE · fix_reason:N_CAPACITY_SHORTAGE · fix_reason:MID_REQUIRED_MISSING · cause:night_coverage',  # app/services/precheck/fix_plan.py:357-421 · app/services/semantics/axis_registry.py:40-75 외
    'coverage_short': '없음: 보조식',  # app/services/engine_contract/rules.py:177
    'coverage_max': '없음: 미구현',  # app/services/precheck/treatment_enricher.py:168-169 · app/services/semantics/ontology.yaml:824-1089 외
    'coverage_over': '없음: 보조식',  # app/services/engine_contract/rules.py:212
    'off_first_coverage_cap': '없음: 미구현',  # app/services/semantics/ontology.yaml:824-1089 · app/services/engine_contract/rules.py:234
    'max_cov_off_equalize': '없음: 보조식',  # app/services/engine_contract/rules.py:251
    'isolated_off_slack': '없음: 보조식',  # app/services/engine_contract/rules.py:264
    'transition_ban': '없음: 하드락',  # app/services/policy/card_filter.py · app/services/cp_sat_basic.py:464 외 — ★EI-11 11a-1: 탐색 전이 해제 3종 삭제 · 처방 transition_ban·원인 cause:transition(tradeoff) 카드는 카드 필터가 거른다
    'mid_transition': '없음: 미구현',  # app/services/engine_contract/rules.py:288
    'mid_to_day_ban': '없음: 미구현',  # app/services/engine_contract/rules.py:302
    'not_one_night': '없음: 하드락',  # app/services/policy/card_filter.py · app/services/cp_sat_basic.py:468 외 — ★EI-11 11a-1: 탐색 disable_not_one_night 삭제 · 원인 cause:not_one_night(tradeoff) 카드는 카드 필터가 거른다 · nm:external
    'ban_night_before_fixed_off': 'probe:disable_ban_n_before_fixed_off · probe:disable_ban_n_before_fixed_wanted_off · treatment:disable:ban_night_before_fixed_off',  # app/services/cp_sat/undiagnosed_probe.py:82-83 ·app/services/cp_sat_basic.py:469 외
    'ban_night_before_fixed_off_slack': '없음: 보조식',  # app/services/engine_contract/rules.py:366
    'lex_freeze_ban_night': '없음: 단계 동결',  # app/services/engine_contract/rules.py:384
    'off_window_requirement': 'probe:raise_max_consec_work · lever:max_conseq_work',  # app/services/cp_sat/undiagnosed_probe.py:79-81 · app/services/cp_sat/undiagnosed_probe.py:237-287 외 — ★EI-11 11a-1: 상한(하드락 #1) 안 레버만 남는다 — cause:consecutive(tradeoff, 연속근무 40)는 카드 필터가 거른다 — ★ontology.yaml 의 treatment:threshold:consecutive_work 는 EI-12 12s 뒤 config_key=max_conseq_work(엔진이 읽는 키)라 실제 설정에 값을 쓰지만(+1), 하드락 #1 금지 처방이라 값에서는 뺐다(카드는 카드 필터가 거르고, 요청은 진입 관문·적용 직후 재검(12b)이 400)
    'max_consecutive_work': 'probe:raise_max_consec_work · lever:max_conseq_work',  # app/services/cp_sat/undiagnosed_probe.py:79-81 · app/services/cp_sat_basic.py:456 외 — ★EI-11 11a-1: 위 행과 같다(cause:consecutive 제외) — ★treatment:threshold:consecutive_work(하드락 #1 금지 처방) 동일 — 위 행 주석 참고
    'same_shift_hard_k': '없음: 자동 완화',  # app/services/roster_create_service.py `same_shift_hard_to_soft_applied` 알림(성공 자리 + `_carried_hardlock_relaxations`) — ★EI-14c-2: 원인→조정·넘은 곳을 알림에 더했다. 원인·family 열(MUS 등록)은 엔진 쓰기 대기
    'isolated_work': '없음: 자동 완화',  # app/services/roster_create_service.py `isolated_work_hard_to_soft_applied` 알림(성공 자리 + `_carried_hardlock_relaxations`) — ★EI-14c-3: 원인→조정·생긴 곳을 알림에 더했다. 원인·family 열(MUS 등록)은 엔진 쓰기 대기
    'consecutive_night_cap': '없음: 하드락',  # app/services/cp_sat/fallback_lex.py:2472 외 — ★EI-11 11a-1(D13 ②): 별칭 레버 relax_consecutive_nights(탐색)·fix_location 행을 지웠다 — 카드가 max_consecutive_nights 를 내지 않는다
    'cnight_excess': '없음: 보조식',  # app/services/engine_contract/rules.py:457
    'max_night': 'probe:raise_max_night_cap · treatment:threshold:monthly_night_cap · personal_night_cap:* · cause:fix_all_personal · cause:night_cap · fix_plan:axis:monthly_n_cap',  # app/services/cp_sat/undiagnosed_probe.py:26-35 ·app/services/cp_sat_basic.py:478-485 외
    'allowed_shift_mask': 'cause:allowed_add · fix_plan:relax_allowed_shift_or_role_lock · fix_reason:ALLOWED_SHIFTS_ISOLATES_NURSE · fix_reason:GLOBAL_SHIFT_ALLOWED_SHORTAGE · fix_reason:N_ONLY_ROLE_OVERSUPPLY',  # app/services/ontology_graph/mcs_trace.py:301-319 · app/services/ontology_graph/lagrangian.py:640-655 외
    'week_off_missing': '없음: 보조식',  # app/services/engine_contract/rules.py:487
    'night_block_end': '없음: 보조식',  # app/services/engine_contract/rules.py:496
    'carryover_boundary': '없음: 하드락',  # app/services/policy/card_filter.py · app/services/cp_sat_basic.py:466-467 외 — ★EI-11 11a-1: 탐색 2N·3N 회복 해제 삭제 · 처방 night_recovery·원인 cause:night_recovery(tradeoff) 카드는 카드 필터가 거른다 — ★(2026-10-14 반박 검토) `fix_plan:axis:carryover_transition` 토큰을 뺐다. 실제로 이 pattern 은 axis_registry 에서 axis_id=carryover_transition(family=BoundaryTransitionBan, 라벨 "전월 경계 전이 금지")으로 나가 fix_plan 이 "검토 후 조정해보세요" 안내를 내는데, 이 안내는 회복 OFF 입력(two_offs_after_two_nig 등 · 전월 N 꼬리)에 닿지 않는다(트리거·효과 어긋남 — 맞는 축은 carryover_recovery 지만 pattern 'carryover_recovery*' 라 이 id 로는 안 걸리는 코드 결함). MUS 등록 자체도 기본 OFF(AIDE_ENABLE_MUS_REGISTRY)라 기본 경로에서는 발화하지 않는다
    'recovery_3n2off': '없음: 하드락',  # app/services/policy/card_filter.py · app/services/cp_sat_basic.py:466 외 — ★EI-11 11a-1: carryover_boundary 행과 같다
    'recovery_2n2off': '없음: 하드락',  # app/services/policy/card_filter.py · app/services/cp_sat_basic.py:467 외 — ★EI-11 11a-1: carryover_boundary 행과 같다
    'n2n_min_gap': '없음: 자동 완화',  # app/services/roster_create_service.py `n2n_min_gap_released_applied`·`n2n_min_gap_partial_applied` 알림 — ★EI-14c-1: 원인→조정·짧아진 곳을 알림에 더했다. 원인·family 열(MUS 등록)은 엔진 쓰기 대기
    'n2n_min_gap_slack': '없음: 보조식',  # app/services/engine_contract/rules.py:581
    'lex_freeze_n2n_gap': '없음: 단계 동결',  # app/services/engine_contract/rules.py:603
    'pattern_nod': '없음: 보조식',  # app/services/engine_contract/rules.py:629
    'pattern_noe': '없음: 보조식',  # app/services/engine_contract/rules.py:638
    'pattern_eod': '없음: 보조식',  # app/services/engine_contract/rules.py:647
    'off_quota': '없음: 보조식',  # app/services/engine_contract/rules.py:656
    'min_off': 'probe:lower_off_days · cause:off_budget',  # app/services/cp_sat/undiagnosed_probe.py:90-92 ·app/services/cp_sat_basic.py:579 외
    'min_off_lower_slack': '없음: 보조식',  # app/services/engine_contract/rules.py:688
    'min_off_missing': '없음: 보조식',  # app/services/engine_contract/rules.py:703
    'off_cap_slack': '없음: 보조식',  # app/services/engine_contract/rules.py:718
    'off_cap': 'fix_reason:FIXED_OFF_EXCEEDS_SPAN',  # app/services/cp_sat/fix_location.py:56 · app/services/semantics/axis_registry.py:117-125 외
    'lex_freeze_coverage': '없음: 단계 동결',  # app/services/engine_contract/rules.py:752
    'lex_freeze_safety': '없음: 단계 동결',  # app/services/engine_contract/rules.py:783
    'daily_coverage_equalize': '없음: 보조식',  # app/services/engine_contract/rules.py:815
    'lex_off_range': '없음: 보조식',  # app/services/engine_contract/rules.py:828
    'lex_freeze_pass': '없음: 단계 동결',  # app/services/engine_contract/rules.py:837
    'lex_grade_short': '없음: 보조식',  # app/services/engine_contract/rules.py:878
    'lex_n_range': '없음: 보조식',  # app/services/engine_contract/rules.py:888
    'lex_d_range': '없음: 보조식',  # app/services/engine_contract/rules.py:897
    'lex_n2n_pair': '없음: 보조식',  # app/services/engine_contract/rules.py:906
    'lex_de_balance': '없음: 보조식',  # app/services/engine_contract/rules.py:918
    'lex_freeze_grade': '없음: 단계 동결',  # app/services/engine_contract/rules.py:931
    'lex_freeze_stage3': '없음: 단계 동결',  # app/services/engine_contract/rules.py:942
    'lns_freeze': '없음: 내부 탐색',  # app/services/engine_contract/rules.py:960 · app/services/cp_sat_basic.py:4908
    'preceptor_pair_bonus': '없음: 보조식',  # app/services/engine_contract/rules.py:969
    'grade_monthly_short': '없음: 보조식',  # app/services/engine_contract/rules.py:983
    'grade_min': 'fix_plan:axis:grade_min · fix_plan:adjust_coverage_or_supply · fix_reason:GRADE_MIN_EXCEEDS_MAX · fix_reason:GRADE_MIN_SUM_EXCEEDS_NEED',  # app/services/semantics/axis_registry.py:90-98 · app/services/precheck/fix_plan.py:586-597 외
    'grade_cascade_short': '없음: 보조식',  # app/services/engine_contract/rules.py:1025
    'grade_min_soft_slack': '없음: 보조식',  # app/services/engine_contract/rules.py:1046
    'grade_max_soft_slack': '없음: 보조식',  # app/services/engine_contract/rules.py:1067
    'grade_max': 'treatment:soft:grade_max · fix_plan:axis:grade_max · fix_reason:GRADE_MAX_SUM_BELOW_NEED',  # app/services/semantics/ontology.yaml:825-833 · app/services/constraint_impact/control.py:299-302 외
    'team_min_present': '없음: 보조식',  # app/services/engine_contract/rules.py:1095
    'team_min_soft_slack': '없음: 보조식',  # app/services/engine_contract/rules.py:1107
    'team_min': 'probe:soften_team_min · treatment:soft:team_min · treatment:disable:team_min · fix_plan:axis:team_min · fix_plan:adjust_coverage_or_supply · fix_reason:TEAM_SIZE_INSUFFICIENT · fix_reason:TEAM_MIN_EXCEEDS_GLOBAL_NEED',  # app/services/cp_sat/undiagnosed_probe.py:101-102 ·app/services/cp_sat_basic.py:646 외
    'handoff_or_link': '없음: 보조식',  # app/services/engine_contract/rules.py:1144
    'handoff_same_soft_slack': '없음: 보조식',  # app/services/engine_contract/rules.py:1157
    'handoff_same': '없음: 미구현',  # app/services/semantics/ontology.yaml:824-1089 · app/services/constraints/team_grade_handoff_constraints.py:215 외
    'handoff_adj_soft_slack': '없음: 보조식',  # app/services/engine_contract/rules.py:1185
    'handoff_adj': '없음: 미구현',  # app/services/semantics/ontology.yaml:824-1089 · app/services/constraints/team_grade_handoff_constraints.py:281 외
    'lookahead_off_cap': '없음: primary 전용',  # app/services/engine_contract/rules.py:1213 · app/services/cp_sat_basic.py:2559 외
    'lookahead_off_spread_max': '없음: 보조식',  # app/services/engine_contract/rules.py:1225
    'monthly_cap_slack': '없음: 보조식',  # app/services/engine_contract/rules.py:1235
    'lex_freeze_monthly_cap': '없음: 단계 동결',  # app/services/engine_contract/rules.py:1264
    'fixed_assignment:*': 'probe:raise_max_consec_work · probe:disable_preceptee_sync · lever:max_conseq_work · cause:fix_all_personal · fix_plan:rebalance_fixed_and_carryover · fix_reason:FIXED_ASSIGN_EXCEEDS_NEED · fix_reason:FIXED_ASSIGN_VIOLATES_ALLOWED · fix_reason:FIXED_OFF_EXCEEDS_SPAN · fix_reason:PER_NURSE_SEQUENCE_INFEASIBLE',  # app/services/roster_create_service.py:2392-2396 · app/services/roster_create_service.py:2397-2398 외 — ★EI-11 11a-1: 하드락 #3·#4·#5·#7 완화 토큰(탐색 2N·3N·1N·연속 N, 처방 night_recovery, 원인 consecutive·night_recovery)을 뺐다 — 카탈로그 삭제·카드 필터. K 레버(probe·lever)는 상한(하드락 #1) 안으로 잘린다. 아래는 그 전 기록이다 — ★(2026-10-14 반박 검토) `lever:max_conseq_work`(=K)·`probe:relax_consecutive_nights`(=L, config 'max_consecutive_nights') 추가. 월경계 강제 OFF 빌더(roster_create_service.py:2392-2520 대)가 이 둘로 fixed_assignment:off_fixed 를 만든다 — L 을 올리면 `if L and cons_n < L` 분기가 2N/3N 회복 강제 OFF 를 지우고 1N 분기의 day1·2 강제 OFF 도 빠진다(하드락 #5 간접). K 를 올리면 day0 강제 OFF·off_window 보정 구간이 줄어든다(하드락 #1 간접). initial_forbidden 행은 이미 probe:relax_consecutive_nights 를 넣어 왔으므로 기준을 맞췄다. ★출처 예외: 2N2OFF 사전 주입(fixed_source='2n2off_recovery', roster_create_service.py:6223-6260)으로 생긴 고정 OFF 는 치료 적용(config_override, :5673)이 사전 주입보다 먼저 반영될 때만 풀리고, treatment:disable:night_recovery·probe:disable_2n2off 의 사후 재계산(UNDIAGNOSED 탐색, :7534)은 이미 만들어진 combined_fixed_cells 를 그대로 넘겨 이 출처분은 검증하지 못한다
    'monthly_limit_*': 'personal_night_cap:* · cause:fix_all_personal · cause:night_only_over_hard_cap · cause:night_forced_oversupply · cause:monthly_limit · fix_reason:MONTHLY_LIMIT_MIN_EXCEEDS_MAX',  # app/services/cp_sat/undiagnosed_probe.py:352-489 ·app/services/precheck/payload.py:382-383 외
}
