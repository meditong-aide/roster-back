"""엔진 제약 이름표(RULES) → 원인 추론 cause id — EI-4 역방향 감사(설계서 §2.6 A2 · §5.3).

이 표는 rule id 마다, 그 제약의 MUS 코어가 `cause_inferer.py` 에 **단독** pattern 으로
들어왔을 때 실제로 어떤 `cause_id`(`services.precheck.cause_inferer._PATTERN_TO_CAUSE` 의
값)가 붙는지를 **현행 그대로** 기록한다. 표에 매핑 키가 있어도 그 pattern 을 실제로 내보내는
MUS 생산자가 하나도 없으면(죽은 매핑) `없음: 미구현` 으로 적는다 — 표에 키가 있다는 사실과
런타임에 실제로 그 원인이 붙는다는 사실은 다르다. 상세 분류 근거는
`.omc/aide_docs/v2/engine_input/ei4/inv_cause.json`(저장소 밖, EI-4 조사 산출물).

연결이 없으면 `없음: <이유>`(어휘는 `NONE_REASONS`)를 적는다.

이 모듈은 데이터만 담는다 — 엔진·DB·LLM·cause_inferer 구현을 import 하지 않는다. 배선은
EI-5·EI-14c 몫이다.

★EI-4 역방향 감사 반박 검토(2026-10-14) 반영: 값이 있는 행도 실제 발화에는 **세 가지 전제**가
있다 — ① `AIDE_ENABLE_MUS_REGISTRY=1`(기본 OFF, fallback_lex.py:1035-1044·cp_sat_basic.py:
2543-2552) 이어야 MUS 자체가 켜진다. ② 같은 간호사 코어에 패턴이 하나뿐이어야 한다 — 두
패턴이 섞이면 `hard_assumption.py:357-361` 이 `cpsat_mus:mixed` 로 바꾸는데 `_PATTERN_TO_CAUSE`
에 그 키가 없어 원인이 통째로 사라진다(규칙×고정 셀 충돌이 전형이라 실무에서 드물지 않다).
③ lex 경로냐 primary 경로냐 — 둘 다 MUS 를 켠 모델이라도 어느 쪽이 그 제약을 **래핑해서**
거는지는 제약마다 다르다(행 주석에 'primary 전용' 표기가 있으면 lex 기본 경로에서는 원인이
붙지 않는다). 또한 이 표는 `cause_inferer`(MUS 기반) 채널만 본다 — `lagrangian.
explain_infeasibility_from_config`(기본 ON, MUS 와 무관하게 상시 작동)가 내는 원인은 범위
밖이다(해당 행에 표시).

새 하드 제약을 넣을 때(설계서 §2.7): 이 표에도 1행 추가 — 원인 추론이 아직 못 짚으면
`없음: 미구현`. 빠뜨리면 A2(`tests/engine_contract/test_rule_consumers.py`)가 막는다.
"""

from __future__ import annotations

#: 이 표에서 '없음: <이유>' 로 쓸 수 있는 어휘 전부(이 목록 밖 문자열은 A2 가 막는다).
NONE_REASONS: frozenset[str] = frozenset({
    '없음: 구조 제약',  # exactly_one_shift 같은 설정 키 없는 정의식 — 단독으로 불가능을 만들 수 없고 충돌 상대가 원인으로 잡혀야 한다
    '없음: 내부 탐색',  # lns_freeze — 이웃 밖 칸을 이미 가능한 현재 해로 묶는 장치라 원래 문제의 불가능 원인이 될 수 없다
    '없음: 단계 동결',  # lex_freeze_*(무회귀 락 포함) — 원인은 동결 대상을 만든 원래 제약 쪽에서 찾을 몫이다
    '없음: 미구현',  # HARD/HARD_AUTO_RELAX 인데 MUS 미등록·_PATTERN_TO_CAUSE 매핑 누락·죽은 매핑(표에 키는 있어도 그 pattern 생산자가 0) 중 하나로 원인 추론 사슬이 끊겼다
    '없음: 보조식',  # strength=AUX — 보조 변수·슬랙 정의라 infeasible 원인이 될 수 없다
    '없음: 운영자 전용',  # 운영자 override(enforce_4o 등) 라서 원인을 안 붙이는 배제 규칙이 없다(0행) — 설계 §5.3 예시대로 '없음: 미구현' 으로 적었다
    '없음: 하드락',  # 원인 추론 쪽에는 하드락 8종이라서 원인을 안 붙이는 배제 규칙이 없다(0행)
})

#: rule id(RULES ∪ RULE_PREFIXES 접두 id) → RULE_CAUSE.
RULE_CAUSE: dict[str, str] = {
    'forbidden_shift': 'cause:eligibility:nurse_isolated',  # app/services/cp_sat/fallback_lex.py:1375 · app/services/cp_sat_basic.py:2911 외
    'enforce_4o': '없음: 미구현',  # app/services/cp_sat/fallback_lex.py:1432 · app/services/cp_sat_basic.py:2975 외  # MUS 등록 대기 — 14c-M(엔진 쓰기 필요)
    'weekend_off_only': '없음: 미구현',  # app/services/cp_sat/fallback_lex.py:1526 · app/services/cp_sat_basic.py:3644 외 — ★cause_inferer(MUS) 범위 밖. 기본 ON 인 lagrangian.explain_infeasibility_from_config 는 이와 무관하게 주말휴무자 과다를 (coverage_shortage, weekend_off, cause:fix_all_personal) 로 짚는다(실측) — RULE_CARD 의 cause:weekend_off 카드가 이 채널에서 나온다
    'initial_forbidden': 'cause:fixed:violates_eligibility',  # app/services/cp_sat/fallback_lex.py:1593 · app/services/cp_sat_basic.py:3160 외
    'exactly_one_shift': '없음: 구조 제약',  # app/services/cp_sat/fallback_lex.py:1621 · app/services/cp_sat_basic.py:3189 외
    'preceptee_follow': '없음: 미구현',  # app/services/cp_sat/fallback_lex.py:1664 · app/services/cp_sat_basic.py:3227 외
    'zero_demand_block': '없음: 미구현',  # app/services/cp_sat/fallback_lex.py:1761 · app/services/cp_sat_basic.py:3320 외
    'coverage_min': 'cause:capacity:daily_total_shortage',  # app/services/cp_sat_basic.py:3354(primary) · app/services/precheck/cause_inferer.py:40 외 — ★MUS 는 primary 전용. lex 의 M 최소 하드(fallback_lex.py 약 1775행 `hard_add(m,"coverage_min",assigned_m_bucket>=m_need)`)는 MUS 래핑이 없다. primary 는 SKIP_PRIMARY 기본값 '1' 때문에 기본 생성 경로에서 건너뛴다(cp_sat_basic.py:1511) → 기본 경로(lex)에서는 MUS 를 켜도 원인 0
    'coverage_short': '없음: 보조식',  # app/services/cp_sat/fallback_lex.py:1780 · app/services/cp_sat_basic.py:3336 외
    'coverage_max': '없음: 미구현',  # app/services/cp_sat/fallback_lex.py:1784 · app/services/cp_sat_basic.py:3327 외
    'coverage_over': '없음: 보조식',  # app/services/cp_sat/fallback_lex.py:1788 · app/services/cp_sat_basic.py:3340 외
    'off_first_coverage_cap': '없음: 미구현',  # app/services/cp_sat/fallback_lex.py:1834 · app/services/cp_sat_basic.py:3383 외
    'max_cov_off_equalize': '없음: 보조식',  # app/services/cp_sat/fallback_lex.py:1878 · app/services/cp_sat_basic.py:3432 외
    'isolated_off_slack': '없음: 보조식',  # app/services/cp_sat/fallback_lex.py:1980 · app/services/cp_sat_basic.py:3044 외
    'transition_ban': 'cause:transition:nod_pattern_forces_infeasibility',  # app/services/cp_sat/fallback_lex.py:2003 · app/services/cp_sat_basic.py:3760 외 — ★N→D·E→D·N→E 6곳은 MUS 짝이 있으나, W(메인코드 없는 고정근무)로 접힌 E→W·N→W 4곳(fallback_lex.py:2106,2109 외)은 MUS 래핑이 없다 — 그 조합이 만드는 불가능에는 원인이 안 붙는다
    'mid_transition': '없음: 미구현',  # app/services/cp_sat/fallback_lex.py:2081 · app/services/cp_sat_basic.py:3852 외
    'mid_to_day_ban': '없음: 미구현',  # app/services/cp_sat/fallback_lex.py:2090 · app/services/precheck/cause_inferer.py:33-61 외
    'not_one_night': 'cause:carryover:fixed_n_isolated_by_off_neighbors',  # app/services/cp_sat_basic.py:4005(primary) · app/services/precheck/cause_inferer.py:35 외 — ★primary 전용 MUS. lex(fallback_lex.py 약 2155행)는 MUS 짝이 없어 기본 경로에서는 MUS 를 켜도 원인 0(SKIP_PRIMARY 기본값 '1' — cp_sat_basic.py:1511). 원인 이름도 '월경계 고정 N 이 OFF 이웃에 고립'이라 일반 1N 금지(월중 전체)보다 좁다 — GAPS 의 '✓' 표기에 두 사정 모두 괄호로 적을 것
    'ban_night_before_fixed_off': '없음: 미구현',  # app/services/cp_sat/fallback_lex.py:2227 · app/services/cp_sat_basic.py:4115 외
    'ban_night_before_fixed_off_slack': '없음: 보조식',  # app/services/cp_sat/fallback_lex.py:2222 · app/services/precheck/cause_inferer.py:33-90
    'lex_freeze_ban_night': '없음: 단계 동결',  # app/services/cp_sat/fallback_lex.py:2245 · app/services/precheck/cause_inferer.py:33-90
    'off_window_requirement': 'cause:fixed:off_exceeds_span',  # app/services/cp_sat/fallback_lex.py:2291 · app/services/cp_sat_basic.py:3694 외 — ★의미가 거꾸로다. 이 제약은 '월경계 보정 창 안 OFF≥1'(하한)인데 재사용된 원인명은 'OFF 가 기간을 넘침'(초과) — off_cap(상한) 쪽 원인을 그대로 쓴 것이다. reason_code FIXED_OFF_EXCEEDS_SPAN 은 ontology_attach.py 에서 OffCap 에 귀속된다
    'max_consecutive_work': 'cause:consecutive:work_limit_blocks_coverage',  # app/services/cp_sat/fallback_lex.py:2331 · app/services/cp_sat_basic.py:3730 외
    'same_shift_hard_k': '없음: 미구현',  # app/services/cp_sat/fallback_lex.py:2398 · app/services/precheck/cause_inferer.py:33-61 외 · MUS 등록 대기(14c-M · 엔진 쓰기 예외 필요)
    'isolated_work': '없음: 미구현',  # app/services/cp_sat/fallback_lex.py:2457 · app/services/precheck/cause_inferer.py:33-61 외 · MUS 등록 대기(14c-M · 엔진 쓰기 예외 필요)
    'consecutive_night_cap': 'cause:capacity:monthly_night_shortage',  # app/services/cp_sat/fallback_lex.py:2488 · app/services/cp_sat_basic.py:4164 외 — ★max_night 와 원인 id 가 같아(dedup) 둘을 구분할 수 없다 — 연속 N 상한 위반과 월간 N 총수 상한 위반이 한 원인으로 뭉친다
    'cnight_excess': '없음: 보조식',  # app/services/cp_sat/fallback_lex.py:2511 · app/services/precheck/cause_inferer.py:33-90
    'max_night': 'cause:capacity:monthly_night_shortage',  # app/services/cp_sat/fallback_lex.py:2546 · app/services/cp_sat_basic.py:4216 외
    'allowed_shift_mask': 'cause:eligibility:nurse_isolated',  # app/services/cp_sat/fallback_lex.py:2578 · app/services/cp_sat_basic.py:3876 외
    'week_off_missing': '없음: 보조식',  # app/services/cp_sat/fallback_lex.py:2690 · app/services/precheck/cause_inferer.py:33-90
    'night_block_end': '없음: 보조식',  # app/services/cp_sat/fallback_lex.py:2704 · app/services/cp_sat_basic.py:4530 외
    'carryover_boundary': 'cause:carryover:prev_month_n_tail_blocks_start',  # app/services/cp_sat/fallback_lex.py:2709 · app/services/cp_sat_basic.py:4535 외
    'recovery_3n2off': 'cause:recovery:three_n_two_off_blocks_demand',  # app/services/cp_sat_basic.py:4513(primary) · app/services/precheck/cause_inferer.py:38 외 — ★primary 전용 MUS. lex(fallback_lex.py 약 2827행)는 MUS 짝이 없어 기본 경로에서는 MUS 를 켜도 원인 0(SKIP_PRIMARY 기본값 '1' — cp_sat_basic.py:1511)
    'recovery_2n2off': '없음: 미구현',  # app/services/cp_sat/fallback_lex.py:2922 · app/services/cp_sat_basic.py:4772 외
    'n2n_min_gap': '없음: 미구현',  # app/services/cp_sat/fallback_lex.py:2990 · app/services/precheck/cause_inferer.py:33-61 외 · MUS 등록 대기(14c-M · 엔진 쓰기 예외 필요)
    'n2n_min_gap_slack': '없음: 보조식',  # app/services/cp_sat/fallback_lex.py:2988 · app/services/precheck/cause_inferer.py:33-90
    'lex_freeze_n2n_gap': '없음: 단계 동결',  # app/services/cp_sat/fallback_lex.py:3043 · app/services/precheck/cause_inferer.py:33-90
    'pattern_nod': '없음: 보조식',  # app/services/cp_sat/fallback_lex.py:3055 · app/services/precheck/cause_inferer.py:33-90
    'pattern_noe': '없음: 보조식',  # app/services/cp_sat/fallback_lex.py:3064 · app/services/precheck/cause_inferer.py:33-90
    'pattern_eod': '없음: 보조식',  # app/services/cp_sat/fallback_lex.py:3073 · app/services/precheck/cause_inferer.py:33-90
    'off_quota': '없음: 보조식',  # app/services/cp_sat/fallback_lex.py:3220 · app/services/precheck/cause_inferer.py:33-90
    'min_off': '없음: 미구현',  # app/services/cp_sat/fallback_lex.py:3335 · app/services/cp_sat_basic.py:4357 외 — ★cause_inferer(MUS) 범위 밖. 기본 ON 인 lagrangian.explain_infeasibility_from_config 는 이와 무관하게 월 최소 OFF 과다를 (policy_overconstraint, off_budget, cause:off_budget) 로 짚는다(실측) — RULE_CARD 의 cause:off_budget 카드가 이 채널에서 나온다
    'min_off_lower_slack': '없음: 보조식',  # app/services/cp_sat/fallback_lex.py:3341 · app/services/precheck/cause_inferer.py:33-90
    'min_off_missing': '없음: 보조식',  # app/services/cp_sat/fallback_lex.py:3347 · app/services/precheck/cause_inferer.py:33-90
    'off_cap_slack': '없음: 보조식',  # app/services/cp_sat/fallback_lex.py:3432 · app/services/precheck/cause_inferer.py:33-90
    'off_cap': 'cause:fixed:off_exceeds_span',  # app/services/cp_sat_basic.py:4394(primary) 외 — ★lex 의 MUS 분기(fallback_lex.py 약 3436행)는 죽은 코드다. off_cap_bounded_slack_enable 이 꺼져 있어도 `_slack_max` 가 기본값 1 로 채워져(0 이 아니다) `if _slack_max > 0:` 분기가 항상 참이 된다 — 그 아래 MUS 래핑 else 분기(OffCapNode)는 bounded_slack_max 를 명시적으로 0 으로 설정하지 않는 한 절대 안 탄다
    'lex_freeze_coverage': '없음: 단계 동결',  # app/services/cp_sat/fallback_lex.py:3606 · app/services/precheck/cause_inferer.py:33-90
    'lex_freeze_safety': '없음: 단계 동결',  # app/services/cp_sat/fallback_lex.py:3691 · app/services/precheck/cause_inferer.py:33-90
    'daily_coverage_equalize': '없음: 보조식',  # app/services/cp_sat/fallback_lex.py:3716 · app/services/cp_sat_basic.py:3468 외
    'lex_off_range': '없음: 보조식',  # app/services/cp_sat/fallback_lex.py:4713 · app/services/precheck/cause_inferer.py:33-90
    'lex_freeze_pass': '없음: 단계 동결',  # app/services/cp_sat/fallback_lex.py:4715 · app/services/precheck/cause_inferer.py:33-90
    'lex_grade_short': '없음: 보조식',  # app/services/cp_sat/fallback_lex.py:4730 · app/services/precheck/cause_inferer.py:33-90
    'lex_n_range': '없음: 보조식',  # app/services/cp_sat/fallback_lex.py:4771 · app/services/precheck/cause_inferer.py:33-90
    'lex_d_range': '없음: 보조식',  # app/services/cp_sat/fallback_lex.py:4816 · app/services/precheck/cause_inferer.py:33-90
    'lex_n2n_pair': '없음: 보조식',  # app/services/cp_sat/fallback_lex.py:4852 · app/services/precheck/cause_inferer.py:33-90
    'lex_de_balance': '없음: 보조식',  # app/services/cp_sat/fallback_lex.py:4963 · app/services/precheck/cause_inferer.py:33-90
    'lex_freeze_grade': '없음: 단계 동결',  # app/services/cp_sat/fallback_lex.py:5369 · app/services/precheck/cause_inferer.py:33-90
    'lex_freeze_stage3': '없음: 단계 동결',  # app/services/cp_sat/fallback_lex.py:5577 · app/services/precheck/cause_inferer.py:33-90
    'lns_freeze': '없음: 내부 탐색',  # app/services/cp_sat_basic.py:4908 · app/services/precheck/cause_inferer.py:33-90
    'preceptor_pair_bonus': '없음: 보조식',  # app/services/cp_sat_basic.py:4952 · app/services/precheck/cause_inferer.py:33-90
    'grade_monthly_short': '없음: 보조식',  # app/services/constraints/grade_constraints.py:199 · app/services/precheck/cause_inferer.py:33-90
    'grade_min': '없음: 미구현',  # ★(2026-10-14 반박 검토) MUS 지점(grade_constraints.py 약 657행, registry 있고 allow_soft_fallback=False 분기)은 `_GRADE_CASCADE_ENABLED=True`(:537) 때문에 그 전에 obj_terms 가 return 되어 지금 도달하지 않는다. 다른 하드 지점(:580 대) 은 MUS 짝이 없다. 실제 등급 하한은 soft grade_cascade_short 뿐이라 MUS 로 잡힐 일이 사실상 0 — recovery_2n2off 와 같은 죽은-매핑 기준으로 '없음: 미구현' 고침(이전 값 'cause:grade:min_sum_over_need' 은 이 표 자신의 죽은 매핑 판정 기준과 모순이었다)
    'grade_cascade_short': '없음: 보조식',  # app/services/constraints/grade_constraints.py:593 · app/services/precheck/cause_inferer.py:33-90
    'grade_min_soft_slack': '없음: 보조식',  # app/services/constraints/grade_constraints.py:635 · app/services/precheck/cause_inferer.py:33-90
    'grade_max_soft_slack': '없음: 보조식',  # app/services/constraints/grade_constraints.py:704 · app/services/precheck/cause_inferer.py:33-90
    'grade_max': 'cause:grade:max_sum_below_need',  # app/services/constraints/grade_constraints.py:724 · app/services/precheck/cause_inferer.py:43 외
    'team_min_present': '없음: 보조식',  # app/services/constraints/team_constraints.py:135 · app/services/precheck/cause_inferer.py:33-90
    'team_min_soft_slack': '없음: 보조식',  # app/services/constraints/team_constraints.py:147 · app/services/precheck/cause_inferer.py:33-90
    'team_min': '없음: 미구현',  # app/services/constraints/team_constraints.py:153 · app/services/precheck/cause_inferer.py:52 외
    'handoff_or_link': '없음: 보조식',  # app/services/constraints/team_grade_handoff_constraints.py:97 · app/services/precheck/cause_inferer.py:33-90
    'handoff_same_soft_slack': '없음: 보조식',  # app/services/constraints/team_grade_handoff_constraints.py:197 · app/services/precheck/cause_inferer.py:33-90
    'handoff_same': 'cause:grade:max_sum_below_need',  # app/services/constraints/team_grade_handoff_constraints.py:225 · app/services/precheck/cause_inferer.py:46 외 — ★stage3 한정(기본 soft, allow_soft=False 일 때만 하드)이고 fallback_objectives.py 의 stage3 목적 빌더 안에서만 걸린다. stage3 INFEASIBLE 은 생성 실패가 아니라 stage2 해로 그대로 확정되는 설계상 흔한 경로(실측 11곳 중 6곳)라 이 원인이 사용자에게 갈 길이 없다. grade_max·handoff_adj 와 같은 원인 id 라 dedup 으로도 구분이 사라진다
    'handoff_adj_soft_slack': '없음: 보조식',  # app/services/constraints/team_grade_handoff_constraints.py:262 · app/services/precheck/cause_inferer.py:33-90
    'handoff_adj': 'cause:grade:max_sum_below_need',  # app/services/constraints/team_grade_handoff_constraints.py:292 · app/services/precheck/cause_inferer.py:45 외 — ★handoff_same 과 같은 사정(stage3 한정·기본 soft·dedup) — 주석 참고
    'lookahead_off_cap': '없음: 미구현',  # app/services/cp_sat/lookahead_constraints.py:72 · app/services/precheck/cause_inferer.py:33-61 외
    'lookahead_off_spread_max': '없음: 보조식',  # app/services/cp_sat/lookahead_constraints.py:121 · app/services/precheck/cause_inferer.py:33-90
    'monthly_cap_slack': '없음: 보조식',  # app/services/constraints/monthly_limit_constraints.py:173 · app/services/precheck/cause_inferer.py:33-90
    'lex_freeze_monthly_cap': '없음: 단계 동결',  # app/services/cp_sat/fallback_lex.py:4182 · app/services/precheck/cause_inferer.py:33-90
    'fixed_assignment:*': '없음: 미구현',  # app/services/cp_sat/fallback_lex.py:1319 · app/services/cp_sat_basic.py:2855 외
    'monthly_limit_*': '없음: 미구현',  # app/services/constraints/monthly_limit_constraints.py:165 · app/services/precheck/cause_inferer.py:33-61 외 — ★cause_inferer(MUS) 범위 밖(예외: N전담 간호사 n_exact/n_max 충돌은 n_only_vs_caps 탐지기가 기본 경로에서도 cause:eligibility:role_only_oversupply 를 낸다). 기본 ON 인 lagrangian.explain_infeasibility_from_config 는 이와 무관하게 n_exact>월 N 상한 같은 개인 한도 충돌을 (personal_infeasible, monthly_limit) 로 짚는다(실측) — RULE_CARD 의 cause:monthly_limit 카드가 이 채널에서 나온다. ★전제 주의: registry(AIDE_ENABLE_MUS_REGISTRY)가 붙으면 monthly_limit_constraints.py 가 upper_slack 설정을 무시하고 상한도 하드로 건다(:140-167) — registry 없을 때는 upper_slack 이 있으면 soft(aux_add monthly_cap_slack)다(:171-176). 진단하려고 MUS 를 켜면 평소엔 없던 불가능이 새로 생길 수 있다
}
