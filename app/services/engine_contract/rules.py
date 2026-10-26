"""엔진 제약 이름표 표(RULES) — 설계서 §2.3 · EI-3.

엔진(lex `fallback_lex` · primary `cp_sat_basic._build_full_model`/`_solve_neighbourhood` ·
공용 `constraints/*` · `lookahead_constraints`)이 거는 제약은 전부 이름표 래퍼
(`services.cp_sat.rule_tag` 의 hard_add·aux_add·hard_call·aux_call)로 이 표의 id 를 단다.
이 표는 온톨로지·카드·precheck 가 "어느 설정이 어느 제약을 켜는가"를 찾는 기준이다.

필드
  id                  이름표. 기존 MUS meta `pattern` 이름이 있으면 그 이름을 그대로 쓴다.
  strength            HARD(해를 제한) · HARD_AUTO_RELAX(자동 사다리가 단계적으로 끔 — ladder_stage) ·
                      AUX(보조 변수 정의·목적식용 — 해를 좁히지 않음).
  module              이 id 를 쓰는 경로: lex · primary · common(둘 다).
  governing_keys      제약을 켜고 끄고 값을 정하는 키. roster_config 컬럼은 이름만, 다른 테이블은
                      '테이블.컬럼', DB 밖 키는 'runtime.'·'env.'·'const.' 접두(D9).
  hard_lock_rule_nos  직접 구현하는 하드락 번호(CLAUDE.md 표 1~8). carryover_boundary 처럼 둘일 수 있다.
  hard_lock_indirect  다른 경로(고정 셀·금지 셀)를 거쳐 간접으로 구현하는 하드락 번호.
  freezes             lex 동결(lex_freeze_*)이 묶는 AUX tag — 동결 단계에서는 사실상 하드가 된다.
  dead_keys           코드가 읽지만 값이 늘 같아 효과가 없는 키(EI-16 결함 큐 후보).

분류 근거·지점별 메모는 `.omc/aide_docs/v2/engine_input/ei3/RULES_FINAL.md`·`sites_final.json`
(EI-3 작업 산출물, 저장소 밖). 이 모듈은 엔진·DB·ortools 를 import 하지 않는다(순수 데이터).
"""

from __future__ import annotations

from dataclasses import dataclass

HARD = "HARD"
HARD_AUTO_RELAX = "HARD_AUTO_RELAX"
AUX = "AUX"


@dataclass(frozen=True)
class RuleSpec:
    """제약 이름표 한 줄."""

    id: str
    strength: str
    module: str
    governing_keys: tuple[str, ...]
    hard_lock_rule_nos: tuple[int, ...] = ()
    hard_lock_indirect: tuple[int, ...] = ()
    ladder_stage: int | None = None
    freezes: tuple[str, ...] = ()
    dead_keys: tuple[str, ...] = ()
    desc_ko: str = ""


_RULE_SPECS: tuple[RuleSpec, ...] = (
    RuleSpec(
        id='forbidden_shift',
        strength='HARD',
        module='common',
        governing_keys=(
            'shifts.default_shift',
            'shifts.shift_gb',
            'shifts.type',
            'runtime.fixed_cells',
        ),
        desc_ko='고정 셀이 아닌 칸에 W(메인코드 없는 근무) 배정 금지',
    ),
    RuleSpec(
        id='enforce_4o',
        strength='HARD',
        module='common',
        governing_keys=(
            'runtime.enforce_4o_hard',
            'runtime.prev_month_off_tail_by_idx',
            'env.ROSTER_DISABLE_4O_HARD',
        ),
        desc_ko='OFF 4연속 금지(4O)와 월경계 보정. 기본 꺼짐(config_override 로만 켜짐)',
    ),
    RuleSpec(
        id='weekend_off_only',
        strength='HARD',
        module='common',
        governing_keys=(
            'nurse_weekendoff_period.weekend_off',
            'runtime.weekend_off_only_enable',
        ),
        desc_ko='주말휴무자는 주말 OFF 강제 · 평일 OFF 금지',
    ),
    RuleSpec(
        id='initial_forbidden',
        strength='HARD',
        module='common',
        governing_keys=(
            'ban_shift_transitions',
            'max_conseq_work',
            'ban_single_night',
            'allow_three_consecutive_nights',
            'use_mid',
            'banned_wanted_entries.banned_shift_ids',
            'banned_wanted_entries.is_applied',
            'nurse_allowed_shift_period.allowed_shifts',
            'nurses.allowed_shifts',
            'nurses.weekly_off_enabled',
            'nurses.weekly_off_weekday',
            'weekly_off_settings.activate',
            'runtime.ban_e_to_d',
            'runtime.ban_n_to_d',
            'runtime.ban_n_to_e',
            'runtime.initial_constraints',
            'runtime.max_consecutive_work_days',
        ),
        hard_lock_indirect=(2, 3),
        desc_ko='금지 셀 합집합(허용근무 기간표·금지 원티드·월경계/월중 경계의 전이 금지·연속 N 상한·1N 꼬리+day0 주휴) 배정 금지. 하드락 #2·#3 의 경계분을 간접 구현한다(hard_lock_indirect)',
    ),
    RuleSpec(
        id='exactly_one_shift',
        strength='HARD',
        module='common',
        governing_keys=(),
        desc_ko='간호사·날짜마다 근무 정확히 1개(AddExactlyOne). 설정 키 없는 구조 제약',
    ),
    RuleSpec(
        id='preceptee_follow',
        strength='HARD',
        module='common',
        governing_keys=(
            'preceptee_follows_preceptor',
            'nurse_preceptee_period.preceptor_id',
            'nurses.preceptor_id',
        ),
        desc_ko='프리셉티 근무 = 프리셉터 근무(기간 안). lex 는 프리셉터가 특수코드인 날 프리셉티 OFF 도 건다',
    ),
    RuleSpec(
        id='zero_demand_block',
        strength='HARD',
        module='common',
        governing_keys=(
            'day_req',
            'eve_req',
            'nig_req',
            'use_mid',
            'daily_shift.d_count',
            'daily_shift.d_count_max',
            'daily_shift.e_count',
            'daily_shift.e_count_max',
            'daily_shift.m_count',
            'daily_shift.max_enabled',
            'daily_shift.n_count',
            'daily_shift.n_count_max',
            'shift_manage.manpower',
        ),
        desc_ko=(
            '요구 인원이 0 인 날·근무 배정 금지. M 은 최대 설정과 무관하게 항상 0(최소·최대 모두 0 — '
            'fallback_lex.py:1760·cp_sat_basic.py:3320). D/E/N 은 최소 0 인 날에 max_enabled 이고 '
            '그날 최대가 설정돼 있으면 0~최대(coverage_max 와 같은 식 — need_max), 그 밖(max_enabled=False '
            '포함 — roster_create_service.py `_row_to_day_counts_max` 가 max_enabled=False 면 '
            '*_count_max 값과 무관하게 need_max 를 0 으로 돌려준다)에는 0(fallback_lex.py:1804·'
            'cp_sat_basic.py:3346 — "최소 0 은 금지, 최대가 1 이상이면 선택")'
        ),
    ),
    RuleSpec(
        id='coverage_min',
        strength='HARD',
        module='common',
        governing_keys=(
            'day_req',
            'eve_req',
            'nig_req',
            'count_preceptee_in_staffing',
            'use_mid',
            'daily_shift.d_count',
            'daily_shift.e_count',
            'daily_shift.m_count',
            'daily_shift.m_count_max',
            'daily_shift.max_enabled',
            'daily_shift.n_count',
            'shift_manage.manpower',
        ),
        desc_ko='일별 근무 최소 인원 하드. primary 는 D/E/N/M, lex 는 M 만(max 설정 시) — lex 의 D/E/N 은 부족 슬랙(coverage_short)',
    ),
    RuleSpec(
        id='coverage_short',
        strength='AUX',
        module='common',
        governing_keys=(
            'day_req',
            'eve_req',
            'nig_req',
            'use_mid',
            'daily_shift.d_count',
            'daily_shift.e_count',
            'daily_shift.m_count',
            'daily_shift.max_enabled',
            'daily_shift.n_count',
            'shift_manage.manpower',
        ),
        desc_ko='커버리지 부족 슬랙 정의(assigned + short ≥ need)',
    ),
    RuleSpec(
        id='coverage_max',
        strength='HARD',
        module='common',
        governing_keys=(
            'count_preceptee_in_staffing',
            'use_mid',
            'daily_shift.d_count_max',
            'daily_shift.e_count_max',
            'daily_shift.m_count',
            'daily_shift.m_count_max',
            'daily_shift.max_enabled',
            'daily_shift.n_count_max',
            'shift_manage.manpower',
        ),
        desc_ko='일별 근무 상한(max 설정 시). M 은 max 가 없으면 min 을 상한으로 쓴다',
    ),
    RuleSpec(
        id='coverage_over',
        strength='AUX',
        module='common',
        governing_keys=(
            'day_req',
            'eve_req',
            'nig_req',
            'use_mid',
            'daily_shift.d_count',
            'daily_shift.d_count_max',
            'daily_shift.e_count',
            'daily_shift.e_count_max',
            'daily_shift.m_count',
            'daily_shift.m_count_max',
            'daily_shift.max_enabled',
            'daily_shift.n_count',
            'daily_shift.n_count_max',
            'shift_manage.manpower',
        ),
        desc_ko='커버리지 초과 슬랙 정의',
    ),
    RuleSpec(
        id='off_first_coverage_cap',
        strength='HARD',
        module='common',
        governing_keys=(
            'day_req',
            'eve_req',
            'nig_req',
            'staffing_first',
            'daily_shift.d_count',
            'daily_shift.e_count',
            'daily_shift.m_count',
            'daily_shift.n_count',
            'shift_manage.manpower',
        ),
        desc_ko='staffing_first 이고 max 미설정이면 배정 ≤ 요구(남는 칸은 OFF 로 회수)',
    ),
    RuleSpec(
        id='max_cov_off_equalize',
        strength='AUX',
        module='common',
        governing_keys=(
            'staffing_first',
            'daily_shift.d_count_max',
            'daily_shift.e_count_max',
            'daily_shift.max_enabled',
            'daily_shift.n_count_max',
        ),
        desc_ko='max 커버리지·staffing_first 병동의 간호사 간 OFF 균등화 보조 변수(AddMax/MinEquality 포함)',
    ),
    RuleSpec(
        id='isolated_off_slack',
        strength='AUX',
        module='common',
        governing_keys=(
            'avoid_isolated_off_and_work',
            'runtime.enforce_clustered_offs',
            'runtime.isolated_off_slack_penalty',
        ),
        desc_ko='고립 OFF 슬랙 정의(lex 는 avoid_isolated_off_and_work, primary 는 enforce_clustered_offs 로 켬)',
    ),
    RuleSpec(
        id='transition_ban',
        strength='HARD',
        module='common',
        governing_keys=(
            'ban_shift_transitions',
            'runtime.ban_e_to_d',
            'runtime.ban_n_to_d',
            'runtime.ban_n_to_e',
        ),
        hard_lock_rule_nos=(2,),
        desc_ko='N→D·E→D·N→E 전이 금지와 W 로 접힌 주간근무 E→W·N→W 금지. 양 끝 고정 시 면제',
    ),
    RuleSpec(
        id='mid_transition',
        strength='HARD',
        module='common',
        governing_keys=(
            'use_mid',
        ),
        desc_ko=(
            'M 의 전날은 D·OFF 만(E→M·N→M 금지) — 원칙. ★경로 차이(2026-10-12): primary'
            '(cp_sat_basic.py:3852)는 이 식 그대로(M→M 도 금지). lex(fallback_lex.py:2078)는 '
            '병원 확정본에 연속 M(MM)이 실재해 전날이 M 인 경우도 허용하도록 바뀌었다(D·OFF·M '
            '세 가지) — 같은 id, 경로별 식이 다르다(primary 는 고치지 않았다)'
        ),
    ),
    RuleSpec(
        id='mid_to_day_ban',
        strength='HARD',
        module='lex',
        governing_keys=(
            'use_mid',
        ),
        desc_ko='역방향 M→D 금지(2026-09-16 추가). 고정 셀로 M→D 가 명시된 자리는 면제',
    ),
    RuleSpec(
        id='not_one_night',  # nm:external
        strength='HARD',
        module='common',
        governing_keys=(
            'ban_single_night',
            'nurse_monthly_limits.n_exact',
            'nurse_monthly_limits.n_max',
            'runtime.prev_month_n_offs_after_by_idx',
            'runtime.prev_month_n_tail_by_idx',
        ),
        hard_lock_rule_nos=(7,),
        desc_ko='단독 N(1N) 금지. n_max==1 인 간호사는 면제',
    ),
    RuleSpec(
        id='ban_night_before_fixed_off',
        strength='HARD_AUTO_RELAX',
        module='common',
        governing_keys=(
            'ban_night_before_fixed_wanted_off',
            'runtime.ban_night_before_fixed_off',
            'runtime.ban_night_minimize',
            'shifts.type',
            'runtime.fixed_cells',
            'fixed_wanted_entries.shift_id',
        ),
        ladder_stage=4,
        desc_ko=(
            '휴가·공가(또는 확정 원티드 O) 전날 N 금지. 자동 사다리 4단계(team_min→same_shift→'
            'isolated_work 뒤, n2n_min_gap 앞 — roster_create_service.py `_ladder_retry` '
            '"4차 완화" 블록, 2026-10-12 dev 추가)에서 하드 그대로가 아니라 `ban_night_minimize`를 '
            '켜 위반칸 최소화 모드로 전환한다(ban_night_before_fixed_off_slack 참조) — 지킬 수 '
            '있으면 위반 0 = 하드와 같다. 단순히 규칙을 끄는 게 아니라 "위반칸 최소화 후 그 값으로 '
            '동결"이라 ladder_stage 의미(다음 완화 단계에서 0 으로 내려 해제)와는 메커니즘이 다르지만, '
            '사다리 호출 순서상 4번째 자리라 ladder_stage=4 로 표시한다. '
            '★ type 축 키(`ban_night_before_fixed_off`)는 RosterConfig ORM 매핑이 없어 config_dict '
            '(=latest_config.__dict__) 에는 절대 안 실린다 — 그런데 엔진이 그 값을 읽는 자리 '
            '(cp_sat_basic.py:469) 는 `config_data.get("ban_night_before_fixed_off", True)` 로 '
            '**기본값 True** 다. 그래서 type 축(휴가·공가 전날 N 금지)은 config_dict 에 키가 없어도 '
            '항상 켜져 있고(하드), wanted 축(`ban_night_before_fixed_wanted_off`, 기본 False)이 꺼진 '
            '병동에서는 그 True 가 "아무도 안 채우는 죽은 키의 기본값"이 아니라 사다리 **밖에서 늘 '
            '작동하는 하드**다. 사다리 4단계의 발화 조건 `_ban_on`(roster_create_service.py:7168-7169) '
            '은 config_dict 만 보므로 `ban_night_before_fixed_wanted_off=True` 인 병동에서만 발화하고, '
            '그때는 type·wanted 두 축 모두 최소화 모드로 바뀐다. 즉 wanted 축이 꺼진 병동(실측상 기본 '
            '다수)에서 type 축은 **사다리 밖 순수 HARD**(자동 완화되지 않는다) — strength=HARD_AUTO_RELAX·'
            'ladder_stage=4 라는 이 메타데이터는 wanted 축이 켜진 병동에만 그대로 적용된다(재현: '
            'ei3c_scratch/review_classification/repro_ban_default.py, 2026-10-13). governing_keys 의 '
            '`runtime.ban_night_before_fixed_off` 는 이 엔진 기본값 True 를 가리킨다 — config_dict 로는 '
            '절대 안 채워지지만 엔진 자체 기본값으로 늘 켜져 있다는 뜻이다. module=common 이지만 '
            'primary(cp_sat_basic.py:4115) 는 `cfg.ban_night_minimize` 를 전혀 읽지 않는다(:623 에서 '
            'cfg 로 실어 보내기만 하고, 소비하는 곳은 fallback_lex.py:2191 뿐) — primary 경로에는 최소화 '
            '모드가 없고, 게다가 primary 는 기본 skip 이다(SKIP_PRIMARY 기본값 "1", cp_sat_basic.py:1511 '
            '— SKIP_PRIMARY=0 일 때만 이 primary 경로가 실제로 의미를 가진다)'
        ),
    ),
    RuleSpec(
        id='ban_night_before_fixed_off_slack',
        strength='AUX',
        module='lex',
        governing_keys=(
            'ban_night_before_fixed_wanted_off',
            'runtime.ban_night_before_fixed_off',
            'runtime.ban_night_minimize',
        ),
        desc_ko=(
            '전날 N 금지 위반칸 슬랙 정의(ban_night_minimize 모드 — fallback_lex.py:2219). '
            'ban_night_minimize 는 설정 컬럼이 아니라 자동 완화 사다리 4단계(roster_create_service '
            '_ladder_retry)가 config_dict 에 끼워 넣는 runtime 값. 슬랙 bool 은 하드가 아니라 이 '
            '단계의 stage1 서브솔브(sum(_ban_slacks_type)·sum(_ban_slacks_wanted) 최소화)에서 '
            '목적식으로 줄어든다 — hard_lock_indirect 없음(사다리가 켠 뒤에도 해를 좁히는 쪽은 '
            '목적식이고, OnlyEnforceIf 자체는 그 최소화를 가능하게 하는 보조 변수 정의일 뿐이다)'
        ),
    ),
    RuleSpec(
        id='lex_freeze_ban_night',
        strength='HARD',
        module='lex',
        governing_keys=(
            'ban_night_before_fixed_wanted_off',
            'runtime.ban_night_before_fixed_off',
            'runtime.ban_night_minimize',
        ),
        freezes=('ban_night_before_fixed_off_slack',),
        desc_ko=(
            'stage1 서브솔브(전날N금지최소화)에서 구한 위반칸 최소값을 축별(휴가·공가축·신청휴무축) '
            '상한으로 stage2·3 빌드에 동결(fallback_lex.py:2242·2244 — ban_slack_le 인자로 전달)'
        ),
    ),
    RuleSpec(
        id='off_window_requirement',
        strength='HARD',
        module='common',
        governing_keys=(
            'max_conseq_work',
            'runtime.off_window_constraints',
        ),
        hard_lock_rule_nos=(1,),
        desc_ko='월경계 연속근무 보정 — 전월 꼬리를 이어 본 월초 창에 OFF≥1',
    ),
    RuleSpec(
        id='max_consecutive_work',
        strength='HARD',
        module='common',
        governing_keys=(
            'max_conseq_work',
            'env.AIDE_MAX_CONSEC_WORK',
        ),
        hard_lock_rule_nos=(1,),
        desc_ko='K+1 일 창마다 OFF≥1(연속근무 상한 K)',
    ),
    RuleSpec(
        id='same_shift_hard_k',
        strength='HARD_AUTO_RELAX',
        module='lex',
        governing_keys=(
            'use_mid',
            'nurse_allowed_shift_period.allowed_shifts',
            'nurses.allowed_shifts',
            'runtime.same_shift_hard_k',
            'env.AIDE_SAME_SHIFT_HARD_K',
        ),
        ladder_stage=2,
        desc_ko='같은 근무 연속 K 상한. 자동 사다리 2단계에서 0 으로 내려 해제',
    ),
    RuleSpec(
        id='isolated_work',
        strength='HARD_AUTO_RELAX',
        module='lex',
        governing_keys=(
            'runtime.isolated_work_hard',
        ),
        ladder_stage=3,
        desc_ko='고립 근무(O-근무-O, 가운데 고정 근무 포함) 금지. 자동 사다리 3단계에서 해제',
    ),
    RuleSpec(
        id='consecutive_night_cap',
        strength='HARD',
        module='common',
        governing_keys=(
            'allow_three_consecutive_nights',
            'runtime.prev_month_n_offs_after_by_idx',
            'runtime.prev_month_n_tail_by_idx',
        ),
        hard_lock_rule_nos=(3,),
        desc_ko='연속 N 상한(allow_three_consecutive_nights 면 3, 아니면 2)과 월경계 보정',
    ),
    RuleSpec(
        id='cnight_excess',
        strength='AUX',
        module='lex',
        governing_keys=(
            'allow_three_consecutive_nights',
        ),
        desc_ko='연속 N 초과 슬랙',
    ),
    RuleSpec(
        id='max_night',
        strength='HARD',
        module='common',
        governing_keys=(
            'max_nig_per_month',
        ),
        hard_lock_rule_nos=(6,),
        desc_ko='간호사 월 N 횟수 상한(전역 max_nig_per_month)',
    ),
    RuleSpec(
        id='allowed_shift_mask',
        strength='HARD',
        module='common',
        governing_keys=(
            'use_mid',
            'nurse_allowed_shift_period.allowed_shifts',
            'nurses.allowed_shifts',
        ),
        desc_ko='허용 근무형 밖 D/E/N/M 배정 금지',
    ),
    RuleSpec(
        id='week_off_missing',
        strength='AUX',
        module='lex',
        governing_keys=(
            'prefer_two_offs_per_7days',
        ),
        desc_ko='주 2OFF 부족 슬랙',
    ),
    RuleSpec(
        id='night_block_end',
        strength='AUX',
        module='common',
        governing_keys=(
            'two_offs_after_three_nig',
            'two_offs_after_two_nig',
            'runtime.prev_month_n_offs_after_by_idx',
            'runtime.prev_month_n_tail_by_idx',
        ),
        desc_ko='N 블록 끝 표지 Bool 정의(== ¬X)',
    ),
    RuleSpec(
        id='carryover_boundary',
        strength='HARD',
        module='common',
        governing_keys=(
            'two_offs_after_three_nig',
            'two_offs_after_two_nig',
            'runtime.fixed_cells',
            'runtime.prev_month_n_offs_after_by_idx',
            'runtime.prev_month_n_tail_by_idx',
        ),
        hard_lock_rule_nos=(4, 5),
        dead_keys=('runtime._2n2off_pre_injected',),
        desc_ko='전월 N 꼬리(3N·2N) 뒤 월초 회복 OFF 와 월중 3N 가드(회복 칸이 비-OFF 확정 원티드면 3N 금지)',
    ),
    RuleSpec(
        id='recovery_3n2off',
        strength='HARD',
        module='common',
        governing_keys=(
            'two_offs_after_three_nig',
        ),
        hard_lock_rule_nos=(4,),
        desc_ko='월중 3N 블록 끝 → 다음 2칸 OFF',
    ),
    RuleSpec(
        id='recovery_2n2off',
        strength='HARD',
        module='common',
        governing_keys=(
            'two_offs_after_two_nig',
            'runtime.fixed_cells',
        ),
        hard_lock_rule_nos=(5,),
        dead_keys=('runtime._2n2off_pre_injected',),
        desc_ko='월중 2N 블록 끝 → 다음 2칸 OFF, 회복 칸이 비-OFF 확정 원티드면 그 자리 블록 종료 금지',
    ),
    RuleSpec(
        id='n2n_min_gap',
        strength='HARD_AUTO_RELAX',
        module='lex',
        governing_keys=(
            'n2n_min_gap',
            'use_mid',
            'nurse_allowed_shift_period.allowed_shifts',
            'nurses.allowed_shifts',
            'env.AIDE_N2N_MIN_GAP',
            'env.AIDE_N2N_SLACK',
            'env.AIDE_N2N_CROSS_MONTH',
            'runtime.prev_month_last_n_k_by_idx',
        ),
        ladder_stage=5,
        desc_ko=(
            'N 블록 사이 최소 간격(gap>=4 부터만, stage2 이상에서만 — stage1 은 커버리지 '
            '확정 전이라 제외). N전담(N-only) 간호사는 간격 하한을 걸면 즉시 INFEASIBLE 이라 '
            '`is_n_only_profile(allowed, use_mid=cfg.use_mid)` 로 면제한다(use_mid 가 바뀌면 '
            '같은 allowed_shifts 라도 면제 여부가 달라진다 — 실측 2026-10-13: '
            "[`'N'`,`'M'`] use_mid=False→N전담 True, use_mid=True→N전담 False). "
            '`AIDE_N2N_SLACK`(기본 1=on) 이면 이 하드식 대신 '
            'n2n_min_gap_slack 이 위반칸 슬랙으로 받고 stage2 서브솔브가 최소화한 값을 '
            'lex_freeze_n2n_gap 이 동결한다(위반 0 이면 이 하드와 동치) — `AIDE_N2N_SLACK=0` '
            '이면 이 식이 그대로 걸린다. 월경계는 `AIDE_N2N_CROSS_MONTH`(기본 1=on)로 전월 '
            '마지막 N 블록 꼬리(`prev_month_last_n_k_by_idx`)까지 같은 간격을 잰다(0 이면 월 '
            '안만). 실행 순서상 자동 사다리 5단계(team_min→same_shift→isolated_work→'
            'ban_night_before_fixed_off 뒤, validation_error 사다리 밖에서 그보다 늦게 실행)'
            '에서 0 으로 내려 완전히 해제 — ★ 단 env `AIDE_N2N_MIN_GAP` 이 설정돼 있으면 '
            '`effective_n2n_min_gap`(fallback_lex.py:441-446, env 가 설정값을 이긴다) 이 '
            '사다리의 config 0 을 무시하고 env 값을 그대로 써 5단계로도 해제되지 않는다 '
            '(same_shift_hard_k 는 "cfg 0=자동완화 상태 → env 로 되살리지 않는다" 로 이 경우를 '
            '따로 막아 두지만 — fallback_lex.py:2358-2360 — n2n_min_gap 은 아직 그 가드가 없다, '
            'EI-16 결함 큐 후보)'
        ),
    ),
    RuleSpec(
        id='n2n_min_gap_slack',
        strength='AUX',
        module='lex',
        governing_keys=(
            'n2n_min_gap',
            'use_mid',
            'nurse_allowed_shift_period.allowed_shifts',
            'nurses.allowed_shifts',
            'env.AIDE_N2N_MIN_GAP',
            'env.AIDE_N2N_SLACK',
            'env.AIDE_N2N_CROSS_MONTH',
            'runtime.prev_month_last_n_k_by_idx',
        ),
        desc_ko=(
            'N 블록 간 최소 간격 위반칸 슬랙 정의(`_forbid_n` · fallback_lex.py:2985). '
            'AIDE_N2N_SLACK 기본값(1=on)에서 위반칸 방식이 기본이고, 0 이면 종전 하드(n2n_min_gap) '
            '그대로다. 슬랙 bool 은 stage2 서브솔브(n2n위반칸 · sum(_g2) 최소화)에서 목적식으로 줄어든다. '
            'N전담 면제(use_mid 의존)·월경계(AIDE_N2N_CROSS_MONTH·prev_month_last_n_k_by_idx) 는 '
            '하드/슬랙 두 경로가 `_forbid_n` 한 함수를 공유해 n2n_min_gap 과 완전히 같다'
        ),
    ),
    RuleSpec(
        id='lex_freeze_n2n_gap',
        strength='HARD',
        module='lex',
        governing_keys=(
            'n2n_min_gap',
            'use_mid',
            'nurse_allowed_shift_period.allowed_shifts',
            'nurses.allowed_shifts',
            'env.AIDE_N2N_MIN_GAP',
            'env.AIDE_N2N_SLACK',
            'env.AIDE_N2N_CROSS_MONTH',
            'runtime.prev_month_last_n_k_by_idx',
        ),
        freezes=('n2n_min_gap_slack',),
        desc_ko=(
            'stage2 서브솔브(n2n위반칸)에서 구한 N 간격 위반칸 최소값을 상한으로 동결 — '
            'stage2 n2n위반칸 서브솔브 직후·stage2 본 풀이 **전**(fallback_lex.py:4344, '
            '`m2.Minimize(_s2_obj_expr)` 로 본 목적식을 되돌리기 전)과 stage3 빌드(n2n_slack_le '
            '인자 → fallback_lex.py:3043) 두 지점 모두 같은 동결. freezes 대상(n2n_min_gap_slack) '
            '의 governing_keys 를 그대로 물려받는다(use_mid·월경계 포함 — 이 동결이 없으면 그 '
            '슬랙이 자유로워져 간격이 무시된다). ★ 사다리 5단계(n2n_min_gap=0)가 켜지면 이 동결도 '
            '함께 없앤다(발화 조건이 같다) — 기본 운영 경로(AIDE_N2N_SLACK 기본 1)에서 해를 실제로 '
            '묶는 하드는 n2n_min_gap 자신이 아니라 이 동결이다'
        ),
    ),
    RuleSpec(
        id='pattern_nod',
        strength='AUX',
        module='lex',
        governing_keys=(
            'avoid_nod_noe_eod',
        ),
        desc_ko='N-O-D 패턴 표지(avoid_nod_noe_eod)',
    ),
    RuleSpec(
        id='pattern_noe',
        strength='AUX',
        module='lex',
        governing_keys=(
            'avoid_nod_noe_eod',
        ),
        desc_ko='N-O-E 패턴 표지(avoid_nod_noe_eod)',
    ),
    RuleSpec(
        id='pattern_eod',
        strength='AUX',
        module='lex',
        governing_keys=(
            'avoid_nod_noe_eod',
        ),
        desc_ko='E-O-D 패턴 표지(avoid_nod_noe_eod)',
    ),
    RuleSpec(
        id='off_quota',
        strength='AUX',
        module='lex',
        governing_keys=(
            'off_days',
            'staffing_first',
            'nurse_allowed_shift_period.allowed_shifts',
            'nurse_weekendoff_period.weekend_off',
            'nurses.allowed_shifts',
            'runtime.max_extra_off_days',
            'runtime.off_quota_excess_weight',
            'env.OFF_QUOTA_EXCESS_WEIGHT',
        ),
        desc_ko='OFF 할당 초과·부족 슬랙',
    ),
    RuleSpec(
        id='min_off',
        strength='HARD',
        module='common',
        governing_keys=(
            'health_leave_enabled',
            'off_days',
            'staffing_first',
            'nurse_allowed_shift_period.allowed_shifts',
            'nurse_leave_period.health_leave_eligible',
            'nurse_weekendoff_period.weekend_off',
            'nurses.allowed_shifts',
            'runtime.other_group_offs',
        ),
        desc_ko='간호사 월 최소 OFF 하한',
    ),
    RuleSpec(
        id='min_off_lower_slack',
        strength='AUX',
        module='lex',
        governing_keys=(
            'health_leave_enabled',
            'off_days',
            'staffing_first',
            'nurse_allowed_shift_period.allowed_shifts',
            'nurse_leave_period.health_leave_eligible',
            'nurse_weekendoff_period.weekend_off',
            'nurses.allowed_shifts',
        ),
        desc_ko='월 최소 OFF 하한 슬랙 가중치 정의(죽은 분기 — _lower_slack_max=0)',
    ),
    RuleSpec(
        id='min_off_missing',
        strength='AUX',
        module='lex',
        governing_keys=(
            'health_leave_enabled',
            'off_days',
            'staffing_first',
            'nurse_allowed_shift_period.allowed_shifts',
            'nurse_leave_period.health_leave_eligible',
            'nurse_weekendoff_period.weekend_off',
            'nurses.allowed_shifts',
        ),
        desc_ko='월 최소 OFF 미달 슬랙',
    ),
    RuleSpec(
        id='off_cap_slack',
        strength='AUX',
        module='lex',
        governing_keys=(
            'runtime.fallback_off_cap_bounded_slack_enable',
            'runtime.fallback_off_cap_bounded_slack_max',
            'runtime.fallback_off_cap_bounded_slack_weight',
        ),
        desc_ko='OFF 상한 슬랙 가중치 정의',
    ),
    RuleSpec(
        id='off_cap',
        strength='HARD',
        module='common',
        governing_keys=(
            'max_nig_per_month',
            'off_days',
            'staffing_first',
            'two_offs_after_three_nig',
            'two_offs_after_two_nig',
            'daily_shift.max_enabled',
            'nurse_allowed_shift_period.allowed_shifts',
            'nurse_monthly_limits.n_exact',
            'nurse_monthly_limits.n_max',
            'nurse_weekendoff_period.weekend_off',
            'nurses.allowed_shifts',
            'runtime.fallback_off_cap_bounded_slack_enable',
            'runtime.fallback_off_cap_bounded_slack_max',
            'runtime.max_extra_off_days',
            'runtime.other_group_offs',
        ),
        desc_ko='간호사 월 OFF 상한(primary 는 N 전담 공식 포함, lex 는 슬랙 0..1)',
    ),
    RuleSpec(
        id='lex_freeze_coverage',
        strength='HARD',
        module='lex',
        governing_keys=(
            'day_req',
            'eve_req',
            'nig_req',
            'use_mid',
            'daily_shift.d_count',
            'daily_shift.d_count_max',
            'daily_shift.e_count',
            'daily_shift.e_count_max',
            'daily_shift.m_count',
            'daily_shift.m_count_max',
            'daily_shift.max_enabled',
            'daily_shift.n_count',
            'daily_shift.n_count_max',
            'runtime.coverage_eq',
            'runtime.over_le',
            'runtime.ban_night_minimize',
        ),
        freezes=('coverage_short', 'coverage_over'),
        desc_ko=(
            'stage1 커버리지 부족 합(==)·초과 합(≤)을 stage2·3 에 동결. ★`ban_night_minimize` '
            '모드일 때는 stage1 전날N금지최소화 서브솔브(같은 m1)에 들어가기 전, 같은 식으로 '
            'm1 자신에게도 한 번 더 재동결한다(fallback_lex.py:4175·4176 — 그 서브솔브의 목적식이 '
            '커버리지를 다시 늘리는 쪽으로 해를 끌고 가지 못하게 막는다. stage2·3 진입부의 동결과 '
            '완전히 같은 식·같은 목적, m1 자신에게 거는 자리라는 점만 다르다)'
        ),
    ),
    RuleSpec(
        id='lex_freeze_safety',
        strength='HARD',
        module='lex',
        governing_keys=(
            'health_leave_enabled',
            'avoid_nod_noe_eod',
            'off_days',
            'staffing_first',
            'avoid_isolated_off_and_work',
            'allow_three_consecutive_nights',
            'prefer_two_offs_per_7days',
            'nurse_allowed_shift_period.allowed_shifts',
            'nurse_leave_period.health_leave_eligible',
            'nurse_weekendoff_period.weekend_off',
            'nurses.allowed_shifts',
            'runtime.fallback_off_cap_bounded_slack_enable',
            'runtime.fallback_off_cap_bounded_slack_max',
            'runtime.fallback_off_cap_bounded_slack_weight',
            'runtime.isolated_off_slack_penalty',
            'runtime.max_extra_off_days',
            'runtime.off_quota_excess_weight',
            'runtime.stage2_zero_locks',
            'env.AIDE_LEX_S4',
            'env.AIDE_LEX_S4_STAGE2',
            'env.AIDE_LEX_S4_STAGE3',
            'env.AIDE_S4_ITEM_W_MIN',
            'env.OFF_QUOTA_EXCESS_WEIGHT',
        ),
        freezes=('isolated_off_slack', 'cnight_excess', 'week_off_missing', 'pattern_nod', 'pattern_noe', 'pattern_eod', 'off_quota', 'min_off_missing', 'off_cap_slack', 'min_off_lower_slack'),
        desc_ko='safety 항목 값을 lex 진입·stage3 에 동결(stage2 에서 0 이던 변수 0 잠금 포함)',
    ),
    RuleSpec(
        id='daily_coverage_equalize',
        strength='AUX',
        module='common',
        governing_keys=(
            'daily_shift.d_count_max',
            'daily_shift.e_count_max',
            'daily_shift.m_count_max',
            'daily_shift.max_enabled',
            'daily_shift.n_count_max',
        ),
        desc_ko='일자별 커버리지 균등화 보조 변수(AddMax/MinEquality 포함)',
    ),
    RuleSpec(
        id='lex_off_range',
        strength='AUX',
        module='lex',
        governing_keys=(
            'env.LEX_PASS_ORDER',
        ),
        desc_ko='lex off_range 패스의 max·min 포락 정의',
    ),
    RuleSpec(
        id='lex_freeze_pass',
        strength='HARD',
        module='lex',
        governing_keys=(
            'off_days',
            'staffing_first',
            'avoid_isolated_off_and_work',
            'use_mid',
            'daily_shift.d_count',
            'daily_shift.e_count',
            'daily_shift.m_count',
            'daily_shift.n_count',
            'nurse_allowed_shift_period.allowed_shifts',
            'nurse_team_period.team_id',
            'nurse_weekendoff_period.weekend_off',
            'nurses.allowed_shifts',
            'nurses.team_id',
            'roster_grade_config.constraints_json',
            'shift_manage.manpower',
            'teams.min_shift',
            'runtime._pref_cells',
            'runtime.de_balance_enable',
            'runtime.de_balance_tolerance',
            'runtime.isolated_off_slack_penalty',
            'runtime.max_extra_off_days',
            'runtime.n_to_n_interval_target',
            'runtime.off_quota_excess_weight',
            'runtime.prev_month_n_tail_by_idx',
            'runtime.team_min_by_team',
            'runtime.team_min_soft_fallback',
            'env.AIDE_N2N_CROSS_FIX',
            'env.AIDE_S6_PASS8',
            'env.DE_BALANCE_TOL',
            'env.DE_LEX_ENABLE',
            'env.LEX_PASS_ORDER',
            'env.OFF_QUOTA_EXCESS_WEIGHT',
        ),
        freezes=('lex_off_range', 'lex_grade_short', 'lex_n_range', 'lex_d_range', 'lex_n2n_pair', 'lex_de_balance', 'isolated_off_slack', 'team_min_soft_slack', 'off_quota'),
        desc_ko='lex 패스(off_range·grade·n_range·n2n·de·team·pref 등)의 최적값 동결 콜백',
    ),
    RuleSpec(
        id='lex_grade_short',
        strength='AUX',
        module='lex',
        governing_keys=(
            'roster_grade_config.constraints_json',
            'env.LEX_PASS_ORDER',
        ),
        desc_ko='lex grade 패스·stage3 의 등급 미달 슬랙 정의(m2·m3)',
    ),
    RuleSpec(
        id='lex_n_range',
        strength='AUX',
        module='lex',
        governing_keys=(
            'env.LEX_PASS_ORDER',
        ),
        desc_ko='lex n_range 패스의 max·min 포락 정의',
    ),
    RuleSpec(
        id='lex_d_range',
        strength='AUX',
        module='lex',
        governing_keys=(
            'env.LEX_PASS_ORDER',
        ),
        desc_ko='lex d_range 패스의 max·min 포락 정의(기본 순서 밖)',
    ),
    RuleSpec(
        id='lex_n2n_pair',
        strength='AUX',
        module='lex',
        governing_keys=(
            'runtime.n_to_n_interval_target',
            'runtime.prev_month_n_tail_by_idx',
            'env.AIDE_N2N_CROSS_FIX',
            'env.LEX_PASS_ORDER',
        ),
        desc_ko='lex n2n 패스의 N 쌍 변수 정의(월경계 _add_cross 포함)',
    ),
    RuleSpec(
        id='lex_de_balance',
        strength='AUX',
        module='lex',
        governing_keys=(
            'runtime.de_balance_enable',
            'runtime.de_balance_tolerance',
            'env.DE_BALANCE_TOL',
            'env.DE_LEX_ENABLE',
            'env.LEX_PASS_ORDER',
        ),
        desc_ko='lex de 패스의 |D−E| 초과 슬랙 정의',
    ),
    RuleSpec(
        id='lex_freeze_grade',
        strength='HARD',
        module='lex',
        governing_keys=(
            'roster_grade_config.constraints_json',
            'env.LEX_PASS_ORDER',
        ),
        freezes=('lex_grade_short',),
        desc_ko='stage3 등급 미달 합 ≤ grade 패스 값(GRADE_FREEZE_STAGE3 상수)',
    ),
    RuleSpec(
        id='lex_freeze_stage3',
        strength='HARD',
        module='lex',
        governing_keys=(
            'nurse_mutual_exclusion_period.partner_id',
            'roster_grade_config.constraints_json',
            'roster_grade_config.constraints_max_json',
            'teams.handoff_policy',
            'runtime.mutual_exclusion_penalty_weight',
            'env.AIDE_D5_LEX',
            'env.AIDE_D5_LEX_MAXN',
            'env.AIDE_MUTEX_LEX',
            'env.AIDE_MUTEX_LEX_MAXN',
            'env.MUTEX_W',
        ),
        desc_ko='stage3 mutex-lex·d5-lex 무회귀 락',
    ),
    RuleSpec(
        id='lns_freeze',
        strength='HARD',
        module='primary',
        governing_keys=(
            'runtime.lns_neighbourhood',
        ),
        desc_ko='LNS 이웃 밖 칸을 현재 해로 고정(_solve_neighbourhood)',
    ),
    RuleSpec(
        id='preceptor_pair_bonus',
        strength='AUX',
        module='common',
        governing_keys=(
            'runtime.pair_matrix',
            'runtime.preceptor_enable',
            'runtime.preceptor_focus_shifts',
            'runtime.preceptor_min_pair_weight',
            'runtime.preceptor_strength_multiplier',
            'runtime.preceptor_top_days',
        ),
        desc_ko='프리셉터 짝 보너스 z ≤ X 정의(LNS 쪽 2곳은 목적식에 안 쓰이는 죽은 변수)',
    ),
    RuleSpec(
        id='grade_monthly_short',
        strength='AUX',
        module='common',
        governing_keys=(
            'use_mid',
            'daily_shift.d_count',
            'daily_shift.e_count',
            'daily_shift.m_count',
            'daily_shift.n_count',
            'nurse_grade_period.grade',
            'nurses.grade',
            'roster_grade_config.constraints_json',
            'roster_grade_config.use_dynamic_scaling',
            'shift_manage.manpower',
            'runtime.grade_config.min_leader_keep',
            'runtime.grade_config.min_ratio_floor',
            'runtime.grade_monthly_hard',
        ),
        desc_ko='등급 월 합계 부족 슬랙(near-hard 라는 이름이지만 soft)',
    ),
    RuleSpec(
        id='grade_min',
        strength='HARD',
        module='common',
        governing_keys=(
            'use_mid',
            'daily_shift.d_count',
            'daily_shift.e_count',
            'daily_shift.m_count',
            'daily_shift.n_count',
            'nurse_grade_period.grade',
            'nurses.grade',
            'roster_grade_config.allow_soft_fallback',
            'roster_grade_config.constraints_json',
            'roster_grade_config.use_dynamic_scaling',
            'shift_manage.manpower',
            'runtime.grade_config.min_leader_keep',
            'runtime.grade_config.min_ratio_floor',
        ),
        desc_ko='등급별 최소 인원 하드. 두 지점 모두 지금 도달하지 않아 실제 하한은 늘 soft(grade_cascade_short)',
    ),
    RuleSpec(
        id='grade_cascade_short',
        strength='AUX',
        module='common',
        governing_keys=(
            'use_mid',
            'daily_shift.d_count',
            'daily_shift.e_count',
            'daily_shift.m_count',
            'daily_shift.n_count',
            'nurse_grade_period.grade',
            'nurses.grade',
            'roster_grade_config.constraints_json',
            'roster_grade_config.use_dynamic_scaling',
            'shift_manage.manpower',
            'runtime.grade_config.min_leader_keep',
            'runtime.grade_config.min_ratio_floor',
            'const._GRADE_CASCADE_ENABLED',
        ),
        desc_ko='등급 하한 cascade 부족 정의 — 지금 등급 하한을 실제로 구현하는 지점',
    ),
    RuleSpec(
        id='grade_min_soft_slack',
        strength='AUX',
        module='common',
        governing_keys=(
            'use_mid',
            'daily_shift.d_count',
            'daily_shift.e_count',
            'daily_shift.m_count',
            'daily_shift.n_count',
            'nurse_grade_period.grade',
            'nurses.grade',
            'roster_grade_config.allow_soft_fallback',
            'roster_grade_config.constraints_json',
            'roster_grade_config.use_dynamic_scaling',
            'shift_manage.manpower',
            'runtime.grade_config.min_leader_keep',
            'runtime.grade_config.min_ratio_floor',
        ),
        desc_ko='레거시 등급 하한 soft 슬랙(도달 불가)',
    ),
    RuleSpec(
        id='grade_max_soft_slack',
        strength='AUX',
        module='common',
        governing_keys=(
            'use_mid',
            'nurse_grade_period.grade',
            'nurses.grade',
            'roster_grade_config.allow_soft_fallback',
            'roster_grade_config.constraints_max_json',
            'runtime._force_grade_max_soft_fallback',
        ),
        desc_ko='등급 상한 초과 슬랙',
    ),
    RuleSpec(
        id='grade_max',
        strength='HARD',
        module='common',
        governing_keys=(
            'use_mid',
            'nurse_grade_period.grade',
            'nurses.grade',
            'roster_grade_config.allow_soft_fallback',
            'roster_grade_config.constraints_max_json',
            'runtime._force_grade_max_soft_fallback',
        ),
        desc_ko='등급별 최대 인원 하드(allow_soft_fallback·처방으로 soft 전환)',
    ),
    RuleSpec(
        id='team_min_present',
        strength='AUX',
        module='common',
        governing_keys=(
            'use_mid',
            'nurse_team_period.team_id',
            'nurses.team_id',
            'teams.min_shift',
        ),
        desc_ko='팀 출석 지표 present 정의',
    ),
    RuleSpec(
        id='team_min_soft_slack',
        strength='AUX',
        module='common',
        governing_keys=(
            'use_mid',
            'daily_shift.d_count',
            'daily_shift.e_count',
            'daily_shift.m_count',
            'daily_shift.n_count',
            'nurse_team_period.team_id',
            'nurses.team_id',
            'shift_manage.manpower',
            'teams.min_shift',
            'runtime.team_min_soft_fallback',
        ),
        desc_ko='team_min soft 슬랙',
    ),
    RuleSpec(
        id='team_min',
        strength='HARD_AUTO_RELAX',
        module='common',
        governing_keys=(
            'use_mid',
            'daily_shift.d_count',
            'daily_shift.e_count',
            'daily_shift.m_count',
            'daily_shift.n_count',
            'nurse_team_period.team_id',
            'nurses.team_id',
            'shift_manage.manpower',
            'teams.min_shift',
            'runtime.team_min_soft_fallback',
        ),
        ladder_stage=1,
        desc_ko='팀별 근무 최소 인원. 자동 사다리 1단계에서 soft 로 1회 재시도',
    ),
    RuleSpec(
        id='handoff_or_link',
        strength='AUX',
        module='common',
        governing_keys=(
            'nurse_grade_period.grade',
            'nurse_team_period.team_id',
            'nurses.grade',
            'nurses.team_id',
            'teams.handoff_policy',
        ),
        desc_ko='handoff OR 보조 변수 링크(b ≥ X)',
    ),
    RuleSpec(
        id='handoff_same_soft_slack',
        strength='AUX',
        module='common',
        governing_keys=(
            'nurse_grade_period.grade',
            'nurse_team_period.team_id',
            'nurses.grade',
            'nurses.team_id',
            'teams.handoff_policy',
            'runtime.team_handoff_soft_fallback',
        ),
        desc_ko='handoff_same soft 슬랙',
    ),
    RuleSpec(
        id='handoff_same',
        strength='HARD',
        module='common',
        governing_keys=(
            'nurse_grade_period.grade',
            'nurse_team_period.team_id',
            'nurses.grade',
            'nurses.team_id',
            'teams.handoff_policy',
            'runtime.team_handoff_soft_fallback',
        ),
        desc_ko='팀 인계 규칙 — 지정 등급 집합이 같은 시프트에 2명 이상 금지. 기본 soft(하드는 override 로만)',
    ),
    RuleSpec(
        id='handoff_adj_soft_slack',
        strength='AUX',
        module='common',
        governing_keys=(
            'nurse_grade_period.grade',
            'nurse_team_period.team_id',
            'nurses.grade',
            'nurses.team_id',
            'teams.handoff_policy',
            'runtime.team_handoff_soft_fallback',
        ),
        desc_ko='handoff_adj soft 슬랙',
    ),
    RuleSpec(
        id='handoff_adj',
        strength='HARD',
        module='common',
        governing_keys=(
            'nurse_grade_period.grade',
            'nurse_team_period.team_id',
            'nurses.grade',
            'nurses.team_id',
            'teams.handoff_policy',
            'runtime.team_handoff_soft_fallback',
        ),
        desc_ko='팀 인계 규칙 — 지정 등급 사이 인접 인계(D→E·E→N·N→익일 D) 금지. 기본 soft',
    ),
    RuleSpec(
        id='lookahead_off_cap',
        strength='HARD',
        module='primary',
        governing_keys=(
            'shift_manage.manpower',
            'runtime.K_lookahead',
            'runtime.lookahead_weekly_off_cells',
            'runtime.next_month_head_requirements',
        ),
        desc_ko='룩어헤드(다음 달 머리) 일별 OFF 상한(primary 전용)',
    ),
    RuleSpec(
        id='lookahead_off_spread_max',
        strength='AUX',
        module='primary',
        governing_keys=(
            'runtime.K_lookahead',
            'runtime.lookahead_distribution_weight',
        ),
        desc_ko='룩어헤드 OFF 분산 목적의 max 변수 정의',
    ),
    RuleSpec(
        id='monthly_cap_slack',
        strength='AUX',
        module='lex',
        governing_keys=(
            'nurse_monthly_limits.d_max',
            'nurse_monthly_limits.e_max',
            'nurse_monthly_limits.n_max',
            'nurse_monthly_limits.o_max',
            'nurse_monthly_limits.d_exact',
            'nurse_monthly_limits.e_exact',
            'nurse_monthly_limits.n_exact',
            'nurse_monthly_limits.o_exact',
            'env.AIDE_S1_CAP_SLACK',
            'staffing_first',
        ),
        desc_ko=(
            '간호사별 월 D/E/N/O 상한(정확치 아님)의 초과칸 슬랙 정의(monthly_limit_constraints.py:173 '
            '— upper_slack=True 일 때만). 하드 의미는 그대로다(docstring) — 상한을 `합 <= 상한 + 초과칸`'
            '으로 걸고 fallback_lex stage1 이 초과칸을 최우선으로 최소화하며, 0 이 아닌 해는 버린다. '
            'stage1 서브솔브 안에서는 lex_freeze_monthly_cap 이 0 으로 동결한다. 호출은 fallback_lex '
            '전용(cp_sat_basic 은 upper_slack 인자를 안 넘겨 기본 False). ★O 축은 `staffing_first=True` 면 '
            '개인 OFF 한도(o_min/o_max/o_exact) 자체를 호출 전에 비워(roster_create_service.py — '
            '"근무 우선 모드는 OFF 한도를 안 읽는다") 이 슬랙 대상에서 빠진다. ★id 가 '
            '`monthly_limit_` 로 시작하지 않게 지었다 — RULE_PREFIXES 의 동적 접두(간호사별 '
            'd/e/n/o 정확치·min·max HARD)와 이름공간이 겹치면 이 id 의 오타가 `lookup()` 에서 '
            '그 접두로 조용히 떨어져 HARD 로 오판될 수 있었다(2026-10-12 반박 검토 지적)'
        ),
    ),
    RuleSpec(
        id='lex_freeze_monthly_cap',
        strength='HARD',
        module='lex',
        governing_keys=(
            'nurse_monthly_limits.d_max',
            'nurse_monthly_limits.e_max',
            'nurse_monthly_limits.n_max',
            'nurse_monthly_limits.o_max',
            'nurse_monthly_limits.d_exact',
            'nurse_monthly_limits.e_exact',
            'nurse_monthly_limits.n_exact',
            'nurse_monthly_limits.o_exact',
            'env.AIDE_S1_CAP_SLACK',
            'staffing_first',
            'runtime.ban_night_minimize',
        ),
        freezes=('monthly_cap_slack',),
        desc_ko=(
            'stage1 에서 월 상한 초과칸을 0 으로 증명한 뒤(cap 연장 재시도), 그 상태를 전날N금지최소화 '
            '서브솔브(같은 m1 모델)에도 그대로 묶는다(fallback_lex.py:4179) — 다른 목적(위반칸 최소화)이 '
            '초과칸을 다시 늘리는 것을 막는다. ★ 이 유일한 지점은 `if _bs1:`(ban_night_minimize 모드 — '
            '`_ban_n_slacks` 가 있을 때만) 블록 안에 있다 — 같은 블록의 lex_freeze_coverage 와 같은 '
            '발화 조건이라 governing_keys 에 `runtime.ban_night_minimize` 를 같이 적는다. freezes '
            '대상(monthly_cap_slack)의 `*_exact` 키도 그대로 물려받는다(정확치가 있으면 애초에 슬랙 '
            '자체가 안 생겨 이 동결도 안 걸린다)'
        ),
    ),
)

#: id → RuleSpec (정확 일치).
RULES: dict[str, RuleSpec] = {spec.id: spec for spec in _RULE_SPECS}

#: 실행 중에 이름이 만들어지는 규칙 — 접두 → RuleSpec.
#:   fixed_assignment:{source} — 고정 셀(`fallback_lex._fixed_pattern_from_source`)
#:   monthly_limit_{code}      — 개인 월 근무 횟수 상한(`constraints/monthly_limit_constraints`)
RULE_PREFIXES: dict[str, RuleSpec] = {
    'fixed_assignment:': RuleSpec(
            id='fixed_assignment:*',
            strength='HARD',
            module='common',
            governing_keys=(
                'apply_confirmed_wanted',
                'max_conseq_work',
                'ban_single_night',
                'preceptee_follows_preceptor',
                'allow_three_consecutive_nights',
                'two_offs_after_three_nig',
                'two_offs_after_two_nig',
                'fixed_wanted_entries.shift_id',
                'nurse_preceptee_period.preceptor_id',
                'nurse_weekendoff_period.weekend_off',
                'nurses.weekly_off_enabled',
                'nurses.weekly_off_weekday',
                'weekly_off_settings.activate',
                'runtime.fixed_cells',
                'runtime.max_consecutive_work_days',
                'runtime.preceptee_fixed_wanted_map',
            ),
            hard_lock_indirect=(1, 4, 5),
            desc_ko='고정 셀(확정 원티드·휴가/공가·주휴·월경계/월중 경계 강제 OFF·2N2OFF 사전 주입 회복 OFF·수기)을 그 근무로 묶고 같은 칸의 다른 근무를 막는다. 이름은 출처별 동적 fixed_assignment:{source}. 강제 OFF 를 통해 하드락 #1·#4·#5 를 간접 구현한다(hard_lock_indirect)',
    ),
    'monthly_limit_': RuleSpec(
            id='monthly_limit_*',
            strength='HARD',
            module='common',
            governing_keys=(
                'nurse_monthly_limits.d_exact',
                'nurse_monthly_limits.d_max',
                'nurse_monthly_limits.d_min',
                'nurse_monthly_limits.e_exact',
                'nurse_monthly_limits.e_max',
                'nurse_monthly_limits.e_min',
                'nurse_monthly_limits.n_exact',
                'nurse_monthly_limits.n_max',
                'nurse_monthly_limits.n_min',
                'nurse_monthly_limits.o_exact',
                'nurse_monthly_limits.o_max',
                'nurse_monthly_limits.o_min',
                'staffing_first',
            ),
            desc_ko=(
                '간호사별 월 D/E/N/O 하한·상한·정확치. 이름은 동적 monthly_limit_{d|e|n|o}. '
                '★O 축은 `staffing_first=True` 면 호출 전에 o_min/o_max/o_exact 가 전부 None 으로 '
                '비워져(roster_create_service.py `_run_cp_sat_basic` — "근무 우선 모드는 OFF '
                '한도를 안 읽는다") 이 제약 자체가 안 걸린다(2026-10-12 dev c1fd5d5)'
            ),
    ),
}

#: 정본 키 별칭 — ORM 속성 키와 물리 컬럼명이 다른 경우(D9 해석 기록).
KEY_ALIASES: dict[str, str] = {
    'nurses.allowed_shifts': 'nurses.is_night_nurse',  # app/db/models.py:84-86 Column(JSON, name="is_night_nurse", k
}


def lookup(rule_id: str) -> RuleSpec | None:
    """id(또는 등록 접두로 시작하는 동적 이름) → RuleSpec. 없으면 None."""
    spec = RULES.get(rule_id)
    if spec is not None:
        return spec
    return next((s for p, s in RULE_PREFIXES.items() if rule_id.startswith(p)), None)


def is_registered(rule_id: str) -> bool:
    """정확 일치 또는 등록 접두 일치."""
    return lookup(rule_id) is not None
