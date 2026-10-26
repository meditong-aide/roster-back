"""엔진 제약 이름표(RULES) → 온톨로지 family — EI-4 역방향 감사(설계서 §2.6 A2 · §5.3).

이 표는 `rules.py` 의 rule id 마다 ontology.yaml `constraints:` family 로 귀속되는지를
**현행 그대로** 기록한다. family 귀속 판정은 ontology 쪽 세 대응표(`mus_bridge._family_of`·
`axis_registry.matches_patterns`·`ontology_audit._FAMILY_TO_MUS_TOKENS`) 중 하나라도 이
규칙을 그 family 로 보내면 그 family 를 적는다. 세 표가 갈리면 다수결과 T1 보호(하드락
보수성)를 따른다 — 상세 분류 근거는 `.omc/aide_docs/v2/engine_input/ei4/inv_ontology.json`
(저장소 밖, EI-4 조사 산출물).

연결이 없으면 `없음: <이유>`(어휘는 `NONE_REASONS`)를 적는다 — '없음' 은 '이 규칙이 약하다'
는 뜻이 아니라 '진단이 이 규칙을 family 로 묶어 설명하지 못한다'는 뜻이다. 이 목록이 곧
사용자 불만(U4 — "왜 안 풀리는지 설명이 없다")의 실체다.

이 모듈은 데이터만 담는다 — 엔진·DB·ortools·ontology 구현을 import 하지 않는다(A1 과 같은
원칙). 배선(진단 코드가 실제로 이 표를 읽게 만드는 일)은 EI-11·EI-14 몫이고 이 표는 아직
아무 곳에서도 안 읽힌다(그래서 '현행 기록'이다).

★EI-4 역방향 감사 반박 검토(2026-10-14) 반영: "현행 그대로"의 세 대응표(`mus_bridge._family_of`
·`axis_registry.matches_patterns`·`ontology_audit._FAMILY_TO_MUS_TOKENS`) 중 `ontology_audit`
표는 테스트 I7 전용 **선언표**라서, 엔진 MUS 생산자가 실제로 0건인 규칙도 family 가 채워질 수
있다(weekend_off_only·team_min·recovery_2n2off·grade_min 이 그랬다 — 전부 아래에서 '없음:
미구현' 으로 고쳤다). 세 표가 갈려 다수결(2:1)로 선언표 쪽이 이기면, 실제로 실행되는 유일한
family 라우터(`fix_plan.py` 가 쓰는 `axis_registry.axes_for_reasons`)가 내는 family 와 이
표의 값이 어긋날 수 있다(carryover_boundary 가 그렇다 — 아래 해당 행 주석 참고). family 값이
있는 나머지 행도 "선언에 근거"와 "실행 경로로 확인"은 다른 확신 수준이라는 점을 유의할 것 —
개별 행 주석에 실행 경로 확인 여부를 적어 뒀다.

새 하드 제약을 넣을 때(설계서 §2.7): ① 엔진에 `hard_add(m, "새_id", expr)` ② `rules.py` 에
RuleSpec 1줄 ③ 이 표를 포함한 소비자 4표(온톨로지·원인·카드·precheck)에 1행씩 — family 가
아직 없으면 `없음: 미구현` 을 적는다. 빠뜨리면 `tests/engine_contract/test_rule_consumers.py`
(A2)가 그 id 를 이름으로 짚는다.

★이 표의 "세 대응표" 정의 범위 밖에도 설명 채널이 둘 있다(`ban_night_before_fixed_off`·
`fixed_assignment:*`·`monthly_limit_*` 에 해당) — ① primary 가 실패 시에만 내는
`_emit_rec.emit(family=...)` 기록을 `roster_create_service.build_conflict_probe_report` 가
family 순위로 묶어 낸다(primary 전용). ② `routers/ontology.py` 의 `CONFLICT_CATALOG` 가
`cpsat_mus:fixed_assignment:*`·`cpsat_mus:monthly_limit_n` 같은 MUS pattern 을 사람이 읽는
설명으로 매핑한다(대시보드, MUS 켜짐 시). 이 표의 값이 '없음: 미구현' 이어도 이 두 채널로는
설명이 나갈 수 있다는 뜻이다 — 둘 다 이 표가 재는 "세 대응표 귀속"과는 다른 경로라 값에는
반영하지 않았다.
"""

from __future__ import annotations

#: 이 표에서 '없음: <이유>' 로 쓸 수 있는 어휘 전부(이 목록 밖 문자열은 A2 가 막는다).
NONE_REASONS: frozenset[str] = frozenset({
    '없음: 구조식',  # 설정 키 없는 모델 정의식(exactly_one_shift) — 단독으로는 infeasible 원인 family 대상이 아니다
    '없음: 단계 동결',  # lex_freeze_* — 앞 단계 최적값을 묶는 식이라 자기 family 를 두지 않는다
    '없음: 미구현',  # mus_bridge._family_of · axis_registry.matches_patterns · ontology_audit._FAMILY_TO_MUS_TOKENS 세 대응표 어디도 이 규칙을 family 로 귀속시키지 않는다(실행 경로가 끊긴 경우 포함)
    '없음: 보조식',  # strength=AUX — 슬랙·표지·포락 변수 정의일 뿐 해를 좁히지 않아 위반·충돌 원인(family)이 될 수 없다
    '없음: 운영자 전용',  # 운영자 스위치(숨은 설정)라는 이유만으로 family 를 안 다는 행은 없다(0행) — enforce_4o 도 스위치지만 일부러 뺐다는 근거가 없어 '없음: 미구현' 으로 적었다
    '없음: 탐색 고정',  # lns_freeze — LNS 가 이웃 밖 칸을 이미 feasible 한 현재 해로 묶는 장치라 원인이 되지 않는다
    '없음: 하드락',  # 하드락 8종(CLAUDE.md 표 1~8)이라는 이유만으로 family 를 안 다는 행은 없다(0행) — 온톨로지는 하드락 규칙도 T1·T2 family 로 다룬다
})

#: rule id(RULES ∪ RULE_PREFIXES 접두 id) → ENGINE_RULE_FAMILY.
ENGINE_RULE_FAMILY: dict[str, str] = {
    'forbidden_shift': 'AllowedShiftMask',  # app/services/semantics/ontology_audit.py:177 — 선언표 전용(mus_bridge.py:41-45 는 대응 없을 때 원문 pattern 을 돌려주는 줄이라 귀속 근거가 아니다) 외
    'enforce_4o': '없음: 미구현',  # app/services/semantics/ontology_audit.py:162-184 · app/services/ontology_graph/mus_bridge.py:22-45 외  # MUS 등록 대기 — 14c-M(엔진 쓰기 필요)
    'weekend_off_only': '없음: 미구현',  # ★(2026-10-14 반박 검토) 엔진 4지점(fallback_lex.py:1526,1538 · cp_sat_basic.py:3644,3674) 전부 MUS 래핑 없는 hard_add — 선언표(ontology_audit.py:173)만의 귀속이라 '없음: 미구현' 으로 고침(실행 경로 끊김). mus_bridge·axis_registry 어디서도 이 패턴을 안 받는다
    'initial_forbidden': 'FixedWanted',  # app/services/semantics/ontology_audit.py:182 · app/services/ontology_graph/mus_bridge.py:41-45(대응 없으면 원문 pattern 반환 — 귀속 근거 아님) 외
    'exactly_one_shift': '없음: 구조식',  # app/services/cp_sat/fallback_lex.py:1621 · app/services/cp_sat_basic.py:3189
    'preceptee_follow': '없음: 미구현',  # app/services/semantics/ontology_audit.py:162-184 · app/services/ontology_graph/mus_bridge.py:22-45 외
    'zero_demand_block': '없음: 미구현',  # app/services/semantics/ontology_audit.py:162-184 · app/services/ontology_graph/mus_bridge.py:22-45 외
    'coverage_min': 'CoverageMin',  # app/services/semantics/ontology_audit.py:163 · app/services/ontology_graph/mus_bridge.py:41-45(대응 없으면 원문 pattern 반환 — 귀속 근거 아님) 외
    'coverage_short': '없음: 보조식',  # app/services/cp_sat/fallback_lex.py:1818 · app/services/cp_sat/fallback_lex.py:1780
    'coverage_max': '없음: 미구현',  # app/services/semantics/ontology_audit.py:162-184 · app/services/ontology_graph/mus_bridge.py:22-45 외
    'coverage_over': '없음: 보조식',  # app/services/cp_sat/fallback_lex.py:1830 · app/services/cp_sat/fallback_lex.py:1788
    'off_first_coverage_cap': '없음: 미구현',  # app/services/semantics/ontology_audit.py:162-184 · app/services/ontology_graph/mus_bridge.py:22-45 외
    'max_cov_off_equalize': '없음: 보조식',  # app/services/cp_sat/fallback_lex.py:1878 · app/services/cp_sat/fallback_lex.py:1883
    'isolated_off_slack': '없음: 보조식',  # app/services/cp_sat/fallback_lex.py:1980 · app/services/cp_sat/fallback_lex.py:1982 외
    'transition_ban': 'BoundaryTransitionBan',  # app/services/semantics/ontology_audit.py:176 · app/services/ontology_graph/mus_bridge.py:23 외
    'mid_transition': '없음: 미구현',  # app/services/semantics/ontology_audit.py:162-184 · app/services/ontology_graph/mus_bridge.py:22-45 외
    'mid_to_day_ban': '없음: 미구현',  # app/services/semantics/ontology_audit.py:162-184 · app/services/ontology_graph/mus_bridge.py:22-45 외
    'not_one_night': 'NotOneNight',  # app/services/semantics/ontology_audit.py:178 · app/services/ontology_graph/mus_bridge.py:26 외 · nm:external
    'ban_night_before_fixed_off': 'BanNightBeforeFixedOff',  # app/services/semantics/ontology_audit.py:171 · app/services/semantics/ontology.yaml:172 외
    'ban_night_before_fixed_off_slack': '없음: 보조식',  # app/services/cp_sat/fallback_lex.py:2222
    'lex_freeze_ban_night': '없음: 단계 동결',  # app/services/cp_sat/fallback_lex.py:2245 · app/services/cp_sat/fallback_lex.py:2247
    'off_window_requirement': 'OffWindow',  # app/services/semantics/ontology_audit.py:175 · app/services/ontology_graph/mus_bridge.py:41-45(대응 없으면 원문 pattern 반환 — 귀속 근거 아님) 외
    'max_consecutive_work': 'ConsecutiveWorkLimit',  # app/services/semantics/ontology_audit.py:167 · app/services/ontology_graph/mus_bridge.py:27 외
    'same_shift_hard_k': '없음: 미구현',  # app/services/semantics/ontology_audit.py:162-184 · app/services/ontology_graph/mus_bridge.py:22-45 외 · MUS 등록 대기(14c-M · 엔진 쓰기 예외 필요)
    'isolated_work': '없음: 미구현',  # ★(2026-10-14 반박 검토) ontology.yaml 에는 같은 id 'isolated_work' 가 soft_constraints 쪽에 있다(aliases: [ISOLATED_WORK], severity: soft) — 그러나 resolve_alias('isolated_work') 가 그 soft 노드로 풀려도 constraints:(hard family) 에는 대응이 없어 get_constraint 가 None 이라 family payload 가 안 붙는다(실측). 엔진은 이 규칙을 HARD 로 건다(fallback_lex.py:2457,2464) — hard 쪽 family 는 여전히 없다. app/services/semantics/ontology_audit.py:162-184 · app/services/ontology_graph/mus_bridge.py:22-45 외 · MUS 등록 대기(14c-M · 엔진 쓰기 예외 필요)
    'consecutive_night_cap': 'ConsecutiveNightLimit',  # app/services/semantics/ontology_audit.py:168 · app/services/ontology_graph/mus_bridge.py:41-45(대응 없으면 원문 pattern 반환 — 귀속 근거 아님) 외
    'cnight_excess': '없음: 보조식',  # app/services/cp_sat/fallback_lex.py:2511
    'max_night': 'MonthlyNightCap',  # app/services/semantics/ontology_audit.py:170 · app/services/ontology_graph/mus_bridge.py:41-45(대응 없으면 원문 pattern 반환 — 귀속 근거 아님) 외
    'allowed_shift_mask': 'AllowedShiftMask',  # app/services/semantics/ontology_audit.py:177 · app/services/ontology_graph/mus_bridge.py:41-45(대응 없으면 원문 pattern 반환 — 귀속 근거 아님) 외
    'week_off_missing': '없음: 보조식',  # app/services/cp_sat/fallback_lex.py:2690 · app/services/semantics/ontology.yaml:1136
    'night_block_end': '없음: 보조식',  # app/services/cp_sat/fallback_lex.py:2704 · app/services/cp_sat/fallback_lex.py:2841
    'carryover_boundary': 'CarryoverBoundary',  # app/services/semantics/ontology_audit.py:181 · app/services/ontology_graph/mus_bridge.py:24(선언표·mus_bridge 2:1 다수결 + T1 보호로 CarryoverBoundary 유지) 외 — ★(2026-10-14 반박 검토) 그러나 **실제로 실행되는 유일한 family 라우터**(fix_plan.py:504-510 이 쓰는 axis_registry.axes_for_reasons)는 이 pattern 을 axis_id=carryover_transition(family=BoundaryTransitionBan, tier=T2)로 묶는다(axis_registry.py:186-195) — 실측(코어 1개만 넣고 build_fix_plan 호출): axis_actions=[('carryover_transition','BoundaryTransitionBan','T2')], protected_axes=[]. 즉 하드락 #4·#5(회복 OFF)가 사용자에게는 T2 "전월 경계 전이 금지" 완화 후보로 나간다 — CarryoverBoundary(T1 보호) 라벨과 실행 결과가 어긋나는 현행 결함(EI4_GAPS.md 참고, 고치는 일은 EI-14)
    'recovery_3n2off': 'NightRecovery',  # app/services/semantics/ontology_audit.py:169 · app/services/ontology_graph/mus_bridge.py:41-45(대응 없으면 원문 pattern 반환 — 귀속 근거 아님) 외
    'recovery_2n2off': '없음: 미구현',  # ★(2026-10-14 반박 검토) 월중 recovery_2n2off 본식(cp_sat_basic.py 블록 중앙부 hard_add(m,"recovery_2n2off",...))에는 MUS 래핑이 없다. Recovery2N2OFF 리터럴은 "carryover_boundary" 패턴으로 태깅된 경계 하드에만 OnlyEnforceIf 조건으로 붙는다(생산자는 carryover_boundary 몫) — 선언표(ontology_audit.py:169)만의 귀속이라 '없음: 미구현' 으로 고침
    'n2n_min_gap': '없음: 미구현',  # app/services/semantics/ontology_audit.py:162-184 · app/services/ontology_graph/mus_bridge.py:22-45 외 · MUS 등록 대기(14c-M · 엔진 쓰기 예외 필요)
    'n2n_min_gap_slack': '없음: 보조식',  # app/services/cp_sat/fallback_lex.py:2988
    'lex_freeze_n2n_gap': '없음: 단계 동결',  # app/services/cp_sat/fallback_lex.py:4344 · app/services/cp_sat/fallback_lex.py:3043
    'pattern_nod': '없음: 보조식',  # app/services/cp_sat/fallback_lex.py:3055 · app/services/semantics/ontology.yaml:1120
    'pattern_noe': '없음: 보조식',  # app/services/cp_sat/fallback_lex.py:3064 · app/services/semantics/ontology.yaml:1120
    'pattern_eod': '없음: 보조식',  # app/services/cp_sat/fallback_lex.py:3073
    'off_quota': '없음: 보조식',  # app/services/cp_sat/fallback_lex.py:3220 · app/services/cp_sat/fallback_lex.py:3221
    'min_off': '없음: 미구현',  # app/services/semantics/ontology_audit.py:162-184 · app/services/ontology_graph/mus_bridge.py:22-45 외
    'min_off_lower_slack': '없음: 보조식',  # app/services/cp_sat/fallback_lex.py:3341
    'min_off_missing': '없음: 보조식',  # app/services/cp_sat/fallback_lex.py:3347
    'off_cap_slack': '없음: 보조식',  # app/services/cp_sat/fallback_lex.py:3432
    'off_cap': 'OffCap',  # app/services/semantics/ontology_audit.py:174 · app/services/ontology_graph/mus_bridge.py:41-45(대응 없으면 원문 pattern 반환 — 귀속 근거 아님) 외
    'lex_freeze_coverage': '없음: 단계 동결',  # app/services/cp_sat/fallback_lex.py:4178 · app/services/cp_sat/fallback_lex.py:4179 외
    'lex_freeze_safety': '없음: 단계 동결',  # app/services/cp_sat/fallback_lex.py:5334 · app/services/cp_sat/fallback_lex.py:5343 외
    'daily_coverage_equalize': '없음: 보조식',  # app/services/cp_sat/fallback_lex.py:3726 · app/services/cp_sat/fallback_lex.py:3727
    'lex_off_range': '없음: 보조식',  # app/services/cp_sat/fallback_lex.py:4713 · app/services/cp_sat/fallback_lex.py:4714
    'lex_freeze_pass': '없음: 단계 동결',  # app/services/cp_sat/fallback_lex.py:4737 · app/services/cp_sat/fallback_lex.py:4715 외
    'lex_grade_short': '없음: 보조식',  # app/services/cp_sat/fallback_lex.py:5367 · app/services/cp_sat/fallback_lex.py:4730
    'lex_n_range': '없음: 보조식',  # app/services/cp_sat/fallback_lex.py:4771 · app/services/cp_sat/fallback_lex.py:4772
    'lex_d_range': '없음: 보조식',  # app/services/cp_sat/fallback_lex.py:4816 · app/services/cp_sat/fallback_lex.py:4817
    'lex_n2n_pair': '없음: 보조식',  # app/services/cp_sat/fallback_lex.py:4889 · app/services/cp_sat/fallback_lex.py:4893
    'lex_de_balance': '없음: 보조식',  # app/services/cp_sat/fallback_lex.py:4963 · app/services/cp_sat/fallback_lex.py:4964
    'lex_freeze_grade': '없음: 단계 동결',  # app/services/cp_sat/fallback_lex.py:5369
    'lex_freeze_stage3': '없음: 단계 동결',  # app/services/cp_sat/fallback_lex.py:5577 · app/services/cp_sat/fallback_lex.py:5642 외
    'lns_freeze': '없음: 탐색 고정',  # app/services/cp_sat_basic.py:4908 · app/services/cp_sat_basic.py:4910
    'preceptor_pair_bonus': '없음: 보조식',  # app/services/cp_sat_basic.py:5275 · app/services/cp_sat_basic.py:5276
    'grade_monthly_short': '없음: 보조식',  # app/services/constraints/grade_constraints.py:199
    'grade_min': '없음: 미구현',  # ★(2026-10-14 반박 검토) _GRADE_CASCADE_ENABLED=True(grade_constraints.py:537) 라 MUS 래핑 지점(registry 있고 allow_soft_fallback=False 분기)에 지금 도달하지 않는다 — cascade 분기가 그 전에 obj_terms 를 return 한다. 실제 등급 하한은 soft grade_cascade_short 뿐이라 MUS 로 잡힐 일이 없다. recovery_2n2off 와 같은 사유(선언표만의 귀속)로 '없음: 미구현' 으로 고침 — 이전 값 'GradeMin'(app/services/semantics/ontology_audit.py:165 · mus_bridge.py:45 선언표 귀속)은 삭제
    'grade_cascade_short': '없음: 보조식',  # app/services/constraints/grade_constraints.py:593
    'grade_min_soft_slack': '없음: 보조식',  # app/services/constraints/grade_constraints.py:635
    'grade_max_soft_slack': '없음: 보조식',  # app/services/constraints/grade_constraints.py:704
    'grade_max': 'GradeMax',  # app/services/semantics/ontology_audit.py:166 · app/services/ontology_graph/mus_bridge.py:28 외
    'team_min_present': '없음: 보조식',  # app/services/constraints/team_constraints.py:135
    'team_min_soft_slack': '없음: 보조식',  # app/services/constraints/team_constraints.py:147
    'team_min': '없음: 미구현',  # ★(2026-10-14 반박 검토) 엔진 1지점(team_constraints.py:153)뿐이고 MUS 래핑·registry 참조가 전혀 없는 맨 hard_add — 선언표(ontology_audit.py:164)만의 귀속이라 '없음: 미구현' 으로 고침. axis_registry 의 axis_id='team_min' 은 matches_patterns 가 없어(reason_code 전용) MUS 코어 family 라우팅에도 안 걸린다 — 이전 값 'TeamMin' 은 삭제
    'handoff_or_link': '없음: 보조식',  # app/services/constraints/team_grade_handoff_constraints.py:97 · app/services/constraints/team_grade_handoff_constraints.py:100
    'handoff_same_soft_slack': '없음: 보조식',  # app/services/constraints/team_grade_handoff_constraints.py:197
    'handoff_same': '없음: 미구현',  # app/services/semantics/ontology_audit.py:162-184 · app/services/ontology_graph/mus_bridge.py:22-45 외
    'handoff_adj_soft_slack': '없음: 보조식',  # app/services/constraints/team_grade_handoff_constraints.py:262
    'handoff_adj': '없음: 미구현',  # app/services/semantics/ontology_audit.py:162-184 · app/services/ontology_graph/mus_bridge.py:22-45 외
    'lookahead_off_cap': '없음: 미구현',  # app/services/semantics/ontology_audit.py:162-184 · app/services/ontology_graph/mus_bridge.py:22-45 외
    'lookahead_off_spread_max': '없음: 보조식',  # app/services/cp_sat/lookahead_constraints.py:121
    'monthly_cap_slack': '없음: 보조식',  # app/services/constraints/monthly_limit_constraints.py:173
    'lex_freeze_monthly_cap': '없음: 단계 동결',  # app/services/cp_sat/fallback_lex.py:4182
    'fixed_assignment:*': 'FixedWanted',  # app/services/semantics/ontology_audit.py:182 · app/services/ontology_graph/mus_bridge.py:41-45(대응 없으면 원문 pattern 반환 — 귀속 근거 아님) 외
    'monthly_limit_*': '없음: 미구현',  # app/services/semantics/ontology_audit.py:162-184 · app/services/ontology_graph/mus_bridge.py:22-45 외
}
