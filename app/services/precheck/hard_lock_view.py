"""precheck 가 엔진과 같은 하드락 값을 보게 하는 연결(EI-10c · 결정 D8 · 설계서 §2.6 C8').

값의 출처는 EI-8 계약 `engine_contract.effective.effective_hard_lock` 하나다 — 엔진
(`CPSATBasicEngine.create_config_from_db`)이 이 설정에서 실제로 쓰는 값(키 부재 기본값 ·
`banned_day_after_eve` 마스킹 · `AIDE_MAX_CONSEC_WORK` 상한 · 월 나이트 15 바닥)을 그대로 받는다.
precheck 안에 그 해석 규칙을 다시 적지 않는다(두 벌 금지).

★ `effective_hard_lock` 은 `create_config_from_db` 를 한 번 부른다. 그 함수의 print 7줄
  (`config_data …` · `max_nig_per_month(raw)` · `[OffPriorityMode]` · `[OffPolicy][Config]` ·
  `=== DEBUG …` 3줄)이 precheck 1회마다 더 찍힌다 — 엔진 파일은 고치지 않는다(허용 차이).
  조건부로 두 줄이 더 붙는다(리뷰 실측): `max_nig_per_month ≤ 0`(운영에 실재하는 값 — 1병동 config=0)이면
  `[CP-SAT-Basic] [WARN] max_nig_per_month=… → 15 로 보정 …` 1줄, env `AIDE_MAX_CONSEC_WORK` 가 있으면
  `[MaxConsecWork] hard=…` 1줄 — 합쳐 7~9줄. WARN 줄은 엔진 실행 때와 별개로 precheck 마다 한 번 더 나온다
  (로그 경보 필터에 걸릴 수 있다). EI-16 큐: 해석과 출력을 가른 print 없는 해석 함수로 바꾸면 이 줄들이 사라진다.
★ 엔진도 같은 예외(`TypeError`·`ValueError`·`OverflowError`)로 그 설정을 못 읽으면(`shift_priority=None` 등) 값을
  조용히 싣지 않는다 — 10c 이전과 같은 값으로 판정하고, 그 설정은 뒤에서 엔진이 같은 예외로 실패한다.
★ 그 밖의 예외(엔진 계약 드리프트 — `NotInConfig`·`UnknownSettingKey` 등)도 값을 싣지 않고 10c 이전 값으로 판정하되
  **한 줄 로그**를 남긴다 — precheck 전체를 건너뛰게 두지 않으면서(생성은 막지 않는다) 10c 가 꺼진 것을 알 수 있게 한다.
  드리프트는 C8' 가 CI 에서 먼저 잡는다.
"""

from __future__ import annotations

from collections.abc import Mapping, MutableMapping
from typing import Any

#: `effective_hard_lock` 키 → precheck `roster_config` 키(검사기가 읽는 이름).
#: `three_seq_nig` 의 엔진 값은 bool 이 아니라 연속 N 상한 정수(3/2)라 이름을 바꿔 싣는다.
ENGINE_TO_PRECHECK_KEYS: dict[str, str] = {
    "max_conseq_work": "max_conseq_work",
    "banned_day_after_eve": "banned_day_after_eve",
    "three_seq_nig": "max_consecutive_nights",
    "two_offs_after_three_nig": "two_offs_after_three_nig",
    "two_offs_after_two_nig": "two_offs_after_two_nig",
    "max_nig_per_month": "max_nig_per_month",
    "not_one_night": "not_one_night",
    "use_mid": "use_mid",  # 방어용 — 엔진 해석이 bridge 와 같은 bool(config.get('use_mid', False)) 라 값은 늘 같다
    "ban_e_to_d": "ban_e_to_d",
    "ban_n_to_d": "ban_n_to_d",
    "ban_n_to_e": "ban_n_to_e",
}

#: 레지스트리 하드락·인접·영향 키 중 precheck 가 값으로 쓰지 않는 키와 그 이유(C8' 커버리지).
NOT_CONSUMED: dict[str, str] = {
    "off_days": "없음: 하드락 값으로 싣는 키가 아니다 — 엔진 OFF 하드 하한(min_off, fallback_lex.py:3253-3345)의 하한값은 EI-14r3 부터"
                " runtime_bridge 가 compute_off_bounds 로 따로 구해 PrecheckNurse.min_off_required 에 싣고 team_grade_precheck._hard_off_floor 가"
                " max(global + p, min_off_required) 로 용량 바닥에 쓴다(off_first·N 전담·주말휴무·프리셉티 제외, 휴가는 구분하지 않아 느슨한 쪽)",
    "nod_noe": "없음: 엔진 목적식 벌점 축 — 하드 판정 대상이 아니다",
    "preceptee_on": "없음: 프리셉티 고정 칸·관계 맵을 짓는 생성 서비스가 이미 반영한다(precheck 는 결과만 받는다)",
    "fixed_wanted_use_yn": "없음: 확정 원티드를 고정 칸으로 만들지 정하는 스위치 — 생성 서비스가 칸으로 반영한다",
    "ban_night_before_fixed_wanted_off": "없음: precheck 미구현(느슨한 쪽 — 놓칠 수는 있어도 오차단은 없다)",
    "ban_night_before_fixed_off": "없음: precheck 미구현(느슨한 쪽 — 놓칠 수는 있어도 오차단은 없다)",
    "n2n_min_gap": "없음: 자동 완화 사다리 5단 대상 — precheck 가 하드로 보면 사다리가 풀 수 있는 설정을 오차단한다",
    "same_shift_hard_k": "없음: 자동 완화 사다리 2단 대상 — 하드로 보면 오차단",
    "isolated_work_hard": "없음: 자동 완화 사다리 3단 대상 — 하드로 보면 오차단",
    "ban_night_minimize": "없음: 자동 완화 사다리 4단 스위치(하드를 위반칸 최소화로 바꾸는 방향)",
    "ban_d_to_n": "없음: 엔진 풀이 모델에 D→N 하드 제약이 없다(lex·primary 모두 주석 — 사후 검사·후처리만)",
    "cross_month_hard_rules_enable": "없음: 월경계 스위치 — 전월 꼬리 전달은 EI-10g(EI-13a 뒤)",
    "cross_month_lookback_days": "없음: 월경계 — 전월 꼬리 전달은 EI-10g(EI-13a 뒤)",
}


def engine_hard_lock(config_dict: Mapping[str, Any]) -> dict[str, Any] | None:
    """엔진이 이 설정에서 쓰는 하드락 실효값(EI-8). 못 읽으면 None(10c 이전 값으로 판정한다).

    엔진도 같은 예외로 못 읽는 설정(`TypeError`·`ValueError`·`OverflowError`)은 조용히 None 이다 — 뒤에서 엔진이
    같은 예외로 실패하니 로그를 더하지 않는다. 그 밖의 예외(엔진 계약 드리프트)는 None 이되 한 줄 남긴다.
    """
    from services.engine_contract.effective import effective_hard_lock

    try:
        return effective_hard_lock(config_dict)
    except (TypeError, ValueError, OverflowError):
        return None
    except Exception as exc:  # noqa: BLE001 — 계약 드리프트가 precheck 전체를 건너뛰게 두지 않는다(값만 안 싣는다)
        print(f"[Precheck] 엔진 하드락 조회 실패(무시 — 10c 이전 값으로 판정): {type(exc).__name__}: {exc}")
        return None


def attach_engine_hard_lock(
    roster_config: MutableMapping[str, Any], config_dict: Mapping[str, Any],
) -> bool:
    """precheck `roster_config` 에 엔진 하드락 실효값을 싣는다. 실었으면 True, 못 읽는 설정이면 False."""
    effective = engine_hard_lock(config_dict)
    if effective is None:
        return False
    for engine_key, precheck_key in ENGINE_TO_PRECHECK_KEYS.items():
        roster_config[precheck_key] = effective[engine_key]
    return True


__all__ = ["ENGINE_TO_PRECHECK_KEYS", "NOT_CONSUMED", "attach_engine_hard_lock", "engine_hard_lock"]
