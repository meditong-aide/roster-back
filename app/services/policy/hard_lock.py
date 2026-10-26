"""하드락 SSOT 판정 — EI-7(dev-4·dev-4e 흡수).

CLAUDE.md 하드락 표 #1~#8 을 레지스트리(`engine_contract.settings.SETTINGS`)에서 읽어
판정 4종(`config_change_violations`·`override_violations`·`person_attr_violations`·
`sequence_violations`)을 낸다. 숫자(상한 5·15·3)·키 이름·완화 방향은 전부 레지스트리가
정본이다 — 이 모듈은 그 표를 해석하는 로직만 두고 숫자를 다시 적지 않는다(R3-06).
해결 카드 `monthly_limit_release` 판정은 `release_violations` 하나다 — 요청 가드 ①과 카드 필터
(`policy.card_filter`)가 같이 쓴다(EI-11 §2.0.2).

D17(2026-10-15 결정) — 하드락 판정 범위는 **① 8종만 강제**한다. 인접(ADJACENT)·영향
(AFFECTS) 키, 월경계 하드락 스위치(boundary_switch)가 완화 방향이어도 `config_change_
violations` 는 차단하지 않고 `kind='relax_mark'` 로만 표시한다. 다만 월경계 스위치
(`cross_month_hard_rules_enable`·`cross_month_lookback_days`)를 끄는 방향은
`override_violations` 에서만 예외적으로 차단한다 — 끄면 #1~#5·#7 의 월경계분이 통째로
풀려(레지스트리 `HardLock.rule_nos`) 완화 방향과 같은 효과이기 때문이다(2026-10-15
추가 결정, '요청으로 끄기 금지').

★ 서명은 DESIGN_V2.md §4.3 의 에이전트 쪽 `(before_eff, after_eff)`/`(base_eff, request)`
와 다르다 — 이 브랜치의 실제 호출부(요청 가드 `roster_create_service
.assert_night_caps_within_hard_lock` · 카드 필터 `policy.card_filter` 의 카드·처방·카탈로그·control 기록 판정)는
'현재 effective config' 를 들고 있지 않고
요청 델타(override/변경분)만 받는다. 그래서 여기서는 **절대값 판정**(그 키가 바뀐
값이 상한을 넘는가)만 한다 — CLAUDE.md 표의 '완화로 보는 변경'이 전부 절대 기준
("5 초과"·"15 초과"·"3 초과")이라 절대값 판정으로 충분하다. bool 계열(#2·#4·#5·#7·#8)
만 '끄는 값(falsy)'을 절대 판정한다(이전 상태 비교 없이 — 규칙이 늘 활성 전제이므로
다르지 않다). 에이전트 쪽이 before/after 비교가 필요하면 이 함수를 감싸면 된다
(STATUS.md 에 기록).
"""

from __future__ import annotations

from dataclasses import dataclass

from services.engine_contract import nurse_attrs as _nurse_attrs_module
from services.engine_contract.names import external_key
from services.engine_contract.rules import RULES
from services.engine_contract.settings import ADJACENT, AFFECTS, RUNTIME_DATA_NOT_SETTINGS, SETTINGS, HardLock
from services.engine_contract.settings import lookup as _lookup_setting

BLOCK = "block"
RELAX_MARK = "relax_mark"

#: int() 변환 실패를 '값 없음(None)'과 구분하기 위한 표지.
_COERCE_FAIL = object()

#: CLAUDE.md 하드락 표 #1~#8 라벨(메시지 표시용). {cap} 자리는 레지스트리 cap 으로 채운다
#: (#1·#3·#6 만 cap 이 있다 — 숫자를 여기 다시 적으면 레지스트리 cap 이 바뀔 때 라벨만
#: 뒤처진다, Codex 리뷰 LOW).
_RULE_LABEL_KO: dict[int, str] = {
    1: "하드락 #1(연속근무 {cap}일 이하)",
    2: "하드락 #2(ND·ED·NE 전이 금지)",
    3: "하드락 #3(나이트 연속 {cap}일 이하)",
    4: "하드락 #4(나이트 3연속 후 OFF 2회)",
    5: "하드락 #5(나이트 2연속 후 OFF 2회)",
    6: "하드락 #6(월 나이트 {cap}회 이하)",
    7: "하드락 #7(1N 단독 배정 금지)",
    8: "하드락 #8(MID 근무코드 사용)",
}

#: 월경계 하드락 스위치(`cross_month_lookback_days`)가 '꺼진 것'으로 보는 최소 일수.
#: 레지스트리 `current_default`(정본)보다 짧으면 전월 꼬리가 잘려 #1·#3·#4·#5·#7 의
#: 월경계분이 부분적으로 풀린다(Codex 리뷰 HIGH — lookback 1~5·True(=1) 실측 재현).
#: 레지스트리 조회가 안 되면 6 으로 방어(R3-06 — 그래도 숫자를 여기 새로 적는 게 아니라
#: 레지스트리가 없을 때만 쓰는 폴백).
def _lookback_min_days() -> int:
    spec = _lookup_setting("cross_month_lookback_days")
    try:
        return int(spec.current_default) if spec is not None else 6
    except (TypeError, ValueError):
        return 6


#: REST `config_override`·설정 변경 어느 경로로도 **값과 무관하게** 차단하는 엔진 내부 키는 두 갈래다.
#:  ① 이 튜플(`boundary_switch_off` — 월경계 스위치와 같은 판정): `_inbound_source_map`. 서버가 인바운드 배정에서
#:    스스로 계산해 주입하는 캐리오버다. 인바운드 배정이 없는 달에는 주입이 안 덮어써 그대로 남고, 월경계 빌더가
#:    `dict` 가 아닌 값을 받으면 깨지며 그 예외를 호출부가 삼켜 월경계 하드락 #1·#2·#3·#4·#5·#7 이
#:    `cross_month_hard_rules_enable=False` 와 똑같이 통째로 풀린다(Codex 리뷰 HIGH — 실측 재현).
#:    `_return_source_map`(1일 복귀 출처 — EI-11 11w-1)도 같은 월경계 빌더 꼬리 보충 키라 같은 판정이다.
#:  ② 그 밖의 `engine_contract.settings.RUNTIME_DATA_NOT_SETTINGS` 전부(`runtime_key_forbidden` — EI-11 11r ·
#:    `_runtime_data_keys`): 금지칸·강제 OFF·OFF 창·전월 꼬리·배정 차단일·고정 칸처럼 서버가 계산해 엔진에 넣는 값이다.
#:    요청이 `config_dict.update(config_override)` 로 그 값을 덮거나 월중 경계 병합을 깨뜨려 하드락 제약을 지웠다(리뷰
#:    재현 — `initial_constraints` 로 월중 경계 OFF 창 소실 → 엔진 배정 N→E·9일 연속근무). 카드 생산자의 `apply` 는
#:    레지스트리 키만 쓰고(11r 재확인) `hold_generate` 는 `config_override` 칸이 없는 죽은 경로라(지시서 P28) 정당한
#:    생산자가 없다. 레지스트리 목록을 늘리면 판정이 같이 따라간다(두 벌 금지).
_FORBIDDEN_INTERNAL_KEYS: tuple[str, ...] = ("_inbound_source_map", "_return_source_map")


def _runtime_data_keys() -> tuple[str, ...]:
    """②의 키 — `RUNTIME_DATA_NOT_SETTINGS` 중 ①(`_FORBIDDEN_INTERNAL_KEYS`) 밖의 것."""
    return tuple(key for key, _part in RUNTIME_DATA_NOT_SETTINGS if key not in _FORBIDDEN_INTERNAL_KEYS)


#: 요청(`config_override`)으로 보낼 수 없는 별칭의 전용 문구(D13 ③ · EI-11 11b). 어느 별칭이 금지인지는
#: 레지스트리 표지(`SettingSpec.derived_inputs[별칭]['runtime'] == 'forbidden'`)가 정한다 — 이 표는 문구만 든다.
#: 표에 없는 별칭은 `"{별칭} 는 요청으로 보낼 수 없는 키입니다."` 를 쓴다.
_RUNTIME_FORBIDDEN_MESSAGE_KO: dict[str, str] = {
    "max_consecutive_nights": "max_consecutive_nights 는 요청으로 보낼 수 없는 키입니다"
                              "(나이트 연속 상한은 설정의 나이트 3연속으로 정합니다).",
}


@dataclass(frozen=True)
class Violation:
    """하드락 판정 결과 한 건.

    `rule_nos` 는 이 위반이 영향을 주는 CLAUDE.md 규칙 번호 전부(월경계 스위치처럼 하나가
    여러 규칙을 한꺼번에 푸는 경우) — 안 주면 `(rule_no,)` 하나로 채운다(Codex 리뷰 LOW,
    '대표 번호 1개만 보여 집계가 누락됨')."""

    rule_no: int
    key: str
    value: object
    reason_code: str
    message_ko: str
    kind: str = BLOCK  # 'block' | 'relax_mark'
    rule_nos: tuple[int, ...] = ()

    def __post_init__(self) -> None:
        if not self.rule_nos:
            object.__setattr__(self, "rule_nos", (self.rule_no,))


def _rule_label(rule_no: int, cap: object = None) -> str:
    """규칙 라벨. `cap` 을 안 주면 레지스트리에서 찾는다 — 안 그러면 `sequence_violations`
    처럼 상한 없이 부르는 곳에서 '연속근무 None일 이하' 가 나온다."""
    tmpl = _RULE_LABEL_KO.get(rule_no, f"하드락 #{rule_no}")
    if "{cap}" not in tmpl:
        return tmpl
    return tmpl.format(cap=cap if cap is not None else _rule_cap(rule_no))


def rule_label(rule_no: int, cap: object = None) -> str:
    """`_rule_label` 의 공개 이름 — 카드 필터·가드 문구가 같은 라벨을 쓴다(EI-11 §2.0.2)."""
    return _rule_label(rule_no, cap)


def rule_cap_unit(rule_no: int) -> str:
    """상한의 단위 — 규칙 라벨에서 `{cap}` 바로 뒤 낱말('일'·'회'). 상한이 없는 규칙은 ''.

    카드 필터의 상한 문구(`mode_suffix`)가 '(최대 15회 — 하드락 #6)'처럼 단위를 붙인다(EI-11 11r — 단위를 라벨과 두 벌로
    적지 않는다)."""
    _head, sep, rest = _RULE_LABEL_KO.get(rule_no, "").partition("{cap}")
    return rest.split(" ", 1)[0] if sep else ""


def _coerce_int(raw: object) -> int | None | object:
    """엔진 강제 변환(`int()`)과 맞춘다. 실패하면 `_COERCE_FAIL`, 값 없으면 None.

    `OverflowError`(`int(math.inf)` 등)도 '숫자가 아님'으로 본다 — FastAPI 가 JSON
    `Infinity`/`NaN` 토큰을 그대로 받아들여 들어올 수 있다(Codex 리뷰 MEDIUM, 이전에는
    가드가 처리 못 해 500 이 났다)."""
    if raw is None:
        return None
    try:
        return int(raw)
    except (TypeError, ValueError, OverflowError):
        return _COERCE_FAIL


def _present_values(spec, changes: dict) -> list[tuple[str, object]]:
    """정본 키·별칭·바깥 이름(`spec.column`) 중 `changes` 에 들어 있는 것을 **전부** (키, 값)으로 돌려준다.

    하나만 골라 보면 다른 이름으로 상한을 넘겨도 통과한다 — 예: `max_conseq_work=None` 과 별칭
    `max_consecutive_work_days=8` 을 함께 보내면 정본만 보고 끝난다. 요청 판정은 원래 이름을 본 뒤 생성 진입이
    정본으로 바꾼다(EI-12 `engine_contract.aliases`). Codex 게이트 2026-10-15.
    바깥(옛) 이름 14칸(NM D58 — 예 `three_seq_nig`)도 찾는다 — 정본 키가 파이썬 이름으로 바뀐 뒤에도 옛 이름으로 온 요청이
    하드락을 풀지 못하게. 옛·새 이름이 함께 오면 둘 다 판정한다.
    D13(EI-11 11b) 뒤 `max_consecutive_nights` 는 경계가 읽지 않고(정본 `allow_three_consecutive_nights` 만), 요청 판정
    (`override_violations`)은 이 함수에 넘기기 전에 값과 무관하게 막는다(`runtime_key_forbidden`)."""
    names = (spec.key, *spec.aliases, *((spec.column,) if spec.column else ()))
    return [(k, changes[k]) for k in dict.fromkeys(names) if k in changes]


# 메시지 안 키 표기는 `external_key(key)` — 사용자에게 보이는 문구가 파이썬 이름 정리(NM)로 바뀌지 않게 바깥(옛) 이름으로 적는다.
# `Violation.key` 는 들어온 키 그대로 둔다.
def _int_cap_violation(lock: HardLock, key: str, raw: object) -> Violation | None:
    value = _coerce_int(raw)
    if value is None:
        return None  # 값 없음 — 엔진 쪽 null_behavior 는 레지스트리 문서로, 여기서 완화로 보지 않는다
    if value is _COERCE_FAIL:
        return Violation(lock.rule_no, key, raw, "non_numeric",
                          f"{_rule_label(lock.rule_no, lock.cap)}: {external_key(key)}={raw!r} 는 숫자가 아니어서 판정할 수 없습니다.")
    if value > lock.cap:
        return Violation(lock.rule_no, key, raw, "exceeds_cap",
                          f"{_rule_label(lock.rule_no, lock.cap)}: {external_key(key)}={raw!r} 는 상한({lock.cap})을 넘습니다.")
    return None


def _bool_on_violation(lock: HardLock, key: str, raw: object) -> Violation | None:
    if raw:  # Python truthy — 'false' 문자열은 truthy 라 완화로 보지 않는다(레지스트리 기록과 동일)
        return None
    return Violation(lock.rule_no, key, raw, "turned_off",
                      f"{_rule_label(lock.rule_no)}: {external_key(key)} 를 끄는 값({raw!r})은 하드락 완화입니다.")


#: 월경계 빌더를 깨뜨려 월경계분을 통째로 푸는 입력(엔진 내부 키 `_inbound_source_map` · 설정 변경의 비숫자
#: `max_consecutive_nights` — `_derived_cap_violation` 주석)이 영향을 주는 번호 — 월경계 스위치와 같다.
#: 숫자는 레지스트리 월경계 스위치의 `HardLock.rule_nos` 에서 읽는다(두 벌 금지).
_BOUNDARY_RULE_NOS: tuple[int, ...] = tuple(_lookup_setting("cross_month_hard_rules_enable").hard_lock.rule_nos)


def _derived_cap_violation(lock: HardLock, key: str, raw: object, *, boundary_kind: str) -> Violation | None:
    if key != "max_consecutive_nights":
        return None  # 정본 bool(allow_three_consecutive_nights) 은 3 if x else 2 라 cap(3) 을 못 넘는다
    value = _coerce_int(raw)
    if value is _COERCE_FAIL:
        # 옛 근거 — 비숫자 값은 월경계 빌더의 `L = int(config_dict.get('max_consecutive_nights') or ...)` 를
        # 깨뜨리고 그 예외를 호출부가 삼켜 월경계 하드락이 `cross_month_hard_rules_enable=False` 와 똑같이
        # 통째로 풀렸다(Codex 리뷰 HIGH — 'x'·[1]·'3.5' 전부 재현). 그래서 월경계 스위치와 같은 reason_code 로 묶었다.
        # D13 ①(EI-11 11b) 뒤 월경계·월중 경계는 이 별칭을 읽지 않고, 요청(`override_violations`)은 여기 오기 전에
        # 값과 무관하게 막는다(`runtime_key_forbidden`). 이 분기는 설정 변경(`config_change_violations` — relax_mark)
        # 에만 남는다. 분류는 그대로 둔다 — 설정 저장은 `RosterConfigCreate` 가 이 키를 버려 효과가 없다.
        # 문구는 지금 사실로 적는다(EI-11 11r — 경계 계산은 더 이상 이 별칭을 읽지 않는다).
        return Violation(lock.rule_no, key, raw, "boundary_switch_off",
                          f"{key}={raw!r} 는 숫자가 아닙니다(경계 계산은 이 키를 읽지 않습니다 — "
                          f"나이트 연속 상한은 설정의 나이트 3연속으로 정합니다).",
                          kind=boundary_kind, rule_nos=_BOUNDARY_RULE_NOS)
    return _int_cap_violation(lock, key, raw)


def _numbered_rule_violation(lock: HardLock, key: str, raw: object, *, boundary_kind: str) -> Violation | None:
    if lock.kind == "int_cap":
        return _int_cap_violation(lock, key, raw)
    if lock.kind == "bool_on":
        return _bool_on_violation(lock, key, raw)
    if lock.kind == "derived_cap":
        return _derived_cap_violation(lock, key, raw, boundary_kind=boundary_kind)
    return None


def _boundary_switch_violation(lock: HardLock, key: str, raw: object, kind: str) -> Violation | None:
    """월경계 하드락 스위치가 꺼지는 방향인지 판정(`cross_month_hard_rules_enable`·
    `cross_month_lookback_days` — 레지스트리 `HardLock.rule_nos` 가 영향받는 번호를 든다).

    `cross_month_lookback_days` 는 **상한(≤0)만**이 아니라 레지스트리 `current_default`
    (조회 실패 시 6)보다 짧아도 '꺼진 것'으로 본다 — 전월 꼬리를 그 길이로 잘라서 보는
    빌더가 부분적으로 풀리기 때문이다(Codex 리뷰 HIGH — 1~5·True(=1)·2.9(=2) 전부 실측
    재현, 기존 코드는 ≤0 만 걸러 1~5 가 통과했다)."""
    if key == "cross_month_hard_rules_enable":
        off = not bool(raw)
    else:  # cross_month_lookback_days
        value = _coerce_int(raw)
        off = value is None or value is _COERCE_FAIL or value < _lookback_min_days()
    if not off:
        return None
    nos = "·".join(f"#{n}" for n in lock.rule_nos)
    return Violation(lock.rule_no, key, raw, "boundary_switch_off",
                      f"월경계 하드락 스위치({key}={raw!r})를 끄면 {nos} 의 월경계분이 통째로 풀립니다.",
                      kind=kind, rule_nos=lock.rule_nos)


def _key_violation(lock, key: str, raw: object, *, boundary_kind: str) -> Violation | None:
    """키 하나(정본·별칭·바깥 이름 `spec.column`)의 값을 그 설정의 하드락 분류로 판정한다."""
    if lock in (ADJACENT, AFFECTS):
        return Violation(0, key, raw, "adjacent_changed",
                         f"인접 설정 {external_key(key)} 변경 — 하드락 완화 방향일 수 있어 확인이 필요합니다.",
                         kind=RELAX_MARK)
    if lock.kind == "boundary_switch":
        return _boundary_switch_violation(lock, key, raw, kind=boundary_kind)
    return _numbered_rule_violation(lock, key, raw, boundary_kind=boundary_kind)


def _internal_key_violations(changes: dict) -> list[Violation]:
    """엔진 내부 전용 키(`_FORBIDDEN_INTERNAL_KEYS` ① · `_runtime_data_keys` ②)가 들어오면 값과 무관하게 차단한다.

    `SETTINGS` 레지스트리에 없는(=`RUNTIME_DATA_NOT_SETTINGS`) 키라 `_scan_changes` 의
    SETTINGS 순회로는 안 걸린다 — 별도로 본다. 설정 변경(`config_change_violations`)·
    REST 요청(`override_violations`) 어느 경로에도 이 키의 정당한 생산자가 없으므로
    (카드 생산자의 `apply` 는 밑줄 없는 레지스트리 키만 쓴다) 둘 다 항상 `BLOCK` 이다. ①은 월경계 스위치와 같은
    `boundary_switch_off`, ②는 요청 가드 ③과 같은 `runtime_key_forbidden`(EI-11 11r)으로 낸다."""
    out: list[Violation] = []
    nos = "·".join(f"#{n}" for n in _BOUNDARY_RULE_NOS)
    for key in _FORBIDDEN_INTERNAL_KEYS:
        if key in changes:
            out.append(Violation(
                _BOUNDARY_RULE_NOS[0], key, changes[key], "boundary_switch_off",
                f"{key} 는 엔진이 스스로 계산해 주입하는 내부 키라 설정·요청으로 보낼 수 "
                f"없습니다 — 보내면 {nos} 의 월경계분이 통째로 풀릴 수 있습니다.",
                kind=BLOCK, rule_nos=_BOUNDARY_RULE_NOS,
            ))
    for key in _runtime_data_keys():
        if key in changes:
            out.append(Violation(
                _BOUNDARY_RULE_NOS[0], key, changes[key], "runtime_key_forbidden",
                f"{key} 는 서버가 스스로 계산해 엔진에 넣는 내부 값이라 설정·요청으로 보낼 수 없습니다"
                f"(보내면 경계·고정 칸의 하드락 {nos} 제약이 풀릴 수 있습니다).",
                kind=BLOCK, rule_nos=_BOUNDARY_RULE_NOS,
            ))
    return out


def _runtime_forbidden_aliases() -> list[tuple[str, HardLock]]:
    """레지스트리가 '요청으로 보낼 수 없다'고 표지한 별칭과 그 하드락(`derived_inputs[별칭]['runtime'] == 'forbidden'`).

    호출마다 레지스트리를 다시 읽는다(캐시 없음 — 표지를 바꾸면 판정이 바로 따라간다). `_scan_changes` 처럼 하드락
    행(`HardLock`)만 본다 — 레지스트리 정합 테스트가 이 표지를 하드락 행에만 허용한다."""
    return [(alias, spec.hard_lock) for spec in SETTINGS if isinstance(spec.hard_lock, HardLock)
            for alias, info in (spec.derived_inputs or {}).items()
            if isinstance(info, dict) and info.get("runtime") == "forbidden"]


def runtime_forbidden_violation(setting_key: str) -> Violation | None:
    """레지스트리 설정 하나(`setting_key` — 정본 키)의 런타임 금지 별칭 위반 한 건(없으면 None). control 이 그 설정을 바꾸려는
    조정을 '적용 불가'로 기록할 때 쓴다(EI-12 · D13) — 별칭 이름·문구·규칙 번호는 레지스트리·문구 표에서 읽는다."""
    for spec in SETTINGS:
        for alias, lock in _runtime_forbidden_aliases():
            if spec.key == setting_key and alias in (spec.derived_inputs or {}):
                message = _RUNTIME_FORBIDDEN_MESSAGE_KO.get(alias, f"{alias} 는 요청으로 보낼 수 없는 키입니다.")
                return Violation(lock.rule_no, alias, None, "runtime_key_forbidden", message, kind=BLOCK)
    return None


def _runtime_key_violations(changes: dict) -> list[Violation]:
    """요청에 든 런타임 금지 별칭(D13 ③)을 **값과 무관하게** BLOCK(`runtime_key_forbidden`)으로 보고 `changes` 에서 꺼낸다.

    꺼낸 키는 `_scan_changes` 에 넘기지 않는다 — 같은 키를 두 번 내지 않고 `_derived_cap_violation` 을 거치지 않게."""
    out: list[Violation] = []
    for alias, lock in _runtime_forbidden_aliases():
        if alias in changes:
            raw = changes.pop(alias)
            message = _RUNTIME_FORBIDDEN_MESSAGE_KO.get(alias, f"{alias} 는 요청으로 보낼 수 없는 키입니다.")
            out.append(Violation(lock.rule_no, alias, raw, "runtime_key_forbidden", message, kind=BLOCK))
    return out


def _scan_changes(changes: dict, *, boundary_kind: str) -> list[Violation]:
    if not changes:
        return []
    out: list[Violation] = [*_internal_key_violations(changes)]
    for spec in SETTINGS:
        if spec.hard_lock is None:
            continue
        seen: set[tuple] = set()
        for key, raw in _present_values(spec, changes):
            v = _key_violation(spec.hard_lock, key, raw, boundary_kind=boundary_kind)
            if v is None:
                continue
            # 같은 칸이 바깥·파이썬 이름으로 같은 값과 함께 오면 문구까지 같은 위반이 둘 생긴다 — 한 건만 남긴다(NM).
            #   값이 다르면 문구가 달라 둘 다 남는다(둘 다 막아야 한다).
            sig = (v.rule_no, v.kind, v.message_ko)
            if sig in seen:
                continue
            seen.add(sig)
            out.append(v)
    return out


def config_change_violations(changes: dict) -> list[Violation]:
    """설정 변경분(해결 카드·설정 저장 등, 정본 키·별칭·바깥 이름 → 새 값)이 하드락 8종을
    완화하는지 판정한다. 8종은 완화 방향이면 `kind='block'`, 인접·월경계 스위치는
    `kind='relax_mark'`(D17 ①) — 차단은 `override_violations` 몫이다."""
    return _scan_changes(changes, boundary_kind=RELAX_MARK)


def override_violations(config_override: dict) -> list[Violation]:
    """REST `config_override`(생성 요청 시점 덮어쓰기) 판정 — 요청 가드(`roster_create_service
    .assert_night_caps_within_hard_lock`)와 카드 필터(`policy.card_filter`)가 같이 쓴다(P-1).

    8종 차단은 `config_change_violations` 와 같다. 여기서만 더하는 것이 둘이다 — ① 월경계 하드락 스위치를 끄는
    방향을 `kind='block'` 으로 올린다(2026-10-15 결정 — '요청으로 끄기 금지'). ② 레지스트리가 요청 금지로 표지한
    별칭(`derived_inputs[별칭]['runtime'] == 'forbidden'` — D13 ③ `max_consecutive_nights`)은 값과 무관하게
    `runtime_key_forbidden` BLOCK 이다 — D13 ① 뒤 월경계·월중 경계는 정본 `allow_three_consecutive_nights` 만 읽고, 이 별칭의
    런타임 유입 통로는 `config_override` 뿐이다. EI-11 11b 부터 가드는 BLOCK 을 모두 400 으로 막는다.
    입력 키는 파이썬 이름이든 바깥(옛) 이름이든(`three_seq_nig`·`banned_day_after_eve`·`not_one_night` 등 — `spec.column`)
    같이 판정한다(`_present_values`) — 어느 경로로 와도 옛 이름으로 하드락을 푸는 구멍이 없다."""
    changes = dict(config_override or {})
    runtime = _runtime_key_violations(changes)
    return [*runtime, *_scan_changes(changes, boundary_kind=BLOCK)]


def person_attr_violations(attr_key: str, value: object, *, nurse_id: str | None = None) -> list[Violation]:
    """간호사 개별 속성 변경(해결 카드 `monthly_limit_release` 등) 한 건이 하드락을
    건드리는지 판정한다. `NurseAttrSpec.hard_lock_*`(§2.2)를 읽는다.

    ★ 범위 축소(시간 제약 — STATUS.md 기록): `hard_lock_kind=='DIRECT'`(현재 `n_max`·
    `n_exact`, #6·#7)의 **#6(월 나이트 상한)** 만 수치 판정을 구현한다. #7 은 'n 상한==1'
    이 완화가 아니라 면제(사용자 명시 의도)라 여기서 판정하지 않는다. 그 밖의 하드락
    연관 속성(`resignation_date`#7·`preceptor_id`#7·`weekend_off`#1·`allowed_shifts`
    #6,8·`fixed_shift` AFFECTS #1~#7)은 계약(이 서명)만 받고 빈 리스트를 돌려준다—
    실제 판정은 EI-16 결함 큐로 미룬다."""
    spec = _nurse_attrs_module.lookup(attr_key)
    if spec is None or spec.hard_lock_kind != "DIRECT" or 6 not in spec.hard_lock_rule_nos:
        return []
    cap = _rule_cap(6)
    coerced = _coerce_int(value)
    if cap is None or coerced in (None, _COERCE_FAIL) or coerced <= cap:
        return []
    who = f"({nurse_id})" if nurse_id else ""
    return [Violation(6, attr_key, value, "exceeds_cap",
                       f"{_rule_label(6, cap)}: {attr_key}={value!r} 는 간호사 1인당 월 {cap}회를 넘습니다{who}.")]


#: 해결 카드 `monthly_limit_release` 에서 #6(월 나이트 상한)을 보는 칸 — 요청 가드 ①과 같다.
_RELEASE_FIELDS: tuple[str, ...] = ("n_exact", "n_max", "n_min")


def release_violations(monthly_limit_release) -> list[Violation]:
    """해결 카드 `monthly_limit_release`(간호사별 월 나이트 한도 변경 목록) 판정 — 요청 가드 ①과 카드
    필터가 같이 쓴다(EI-11 §2.0.2).

    항목이 dict 가 아니거나 `field` 가 `n_exact`·`n_max`·`n_min` 이 아니거나 `value` 가 None 이면 건너뛴다.
    숫자로 못 읽으면 `non_numeric`, #6 상한(레지스트리)을 넘으면 `exceeds_cap` — 둘 다 BLOCK 이고 입력
    순서를 지킨다. 숫자 변환은 가드의 `int()` 와 같다(' 16 '→16 · True→1 · 16.9→16 · 'abc'·inf·nan 실패).
    레지스트리에 #6 상한이 없으면 판정할 수 없으므로 `LookupError` 를 낸다(호출부가 막는 쪽으로 처리)."""
    cap = _rule_cap(6)
    if cap is None:
        raise LookupError("레지스트리에 하드락 #6(월 나이트) 상한이 없어 판정할 수 없습니다.")
    out: list[Violation] = []
    for rel in monthly_limit_release or []:
        if not isinstance(rel, dict) or rel.get("field") not in _RELEASE_FIELDS or rel.get("value") is None:
            continue
        key, raw = f"{rel.get('nurse_id')}:{rel.get('field')}", rel.get("value")
        value = _coerce_int(raw)
        if value is _COERCE_FAIL:
            out.append(Violation(6, key, raw, "non_numeric",
                                 f"{_rule_label(6, cap)}: {key}={raw!r} 는 숫자가 아니어서 판정할 수 없습니다."))
        elif value > cap:
            out.append(Violation(6, key, raw, "exceeds_cap",
                                 f"{_rule_label(6, cap)}: {key}={raw!r} 는 간호사 1인당 월 {cap}회를 넘습니다."))
    return out


def sequence_violations(rule_id: str, *, disabled: bool) -> list[Violation]:
    """엔진 제약 이름표(`engine_contract.rules.RULES`) 하나가 설정·카드로 꺼지는지
    판정한다. `disabled=True` 로 부르면 그 이름표가 직접 구현하는 하드락 번호
    (`RuleSpec.hard_lock_rule_nos`)마다 차단 1건을 낸다.

    ★ 범위 축소(시간 제약 — STATUS.md 기록): dev-4e 확장 규칙(M→D·E/N→W 전이,
    1N 월경계, 병동 횡단 — `transition_ban`·`not_one_night` 등 RuleSpec)의 실제
    위반 **탐지**(칸 비교·전월 꼬리·병동 횡단 시퀀스)는 하지 않는다. 이 함수는
    '이름표가 꺼지는가'라는 최소 계약만 두고, 호출측(온보딩·에이전트)이 그 탐지
    결과를 `disabled` 로 넘겨줘야 한다(EI-16 결함 큐)."""
    spec = RULES.get(rule_id)
    if spec is None or not disabled:
        return []
    return [
        Violation(rule_no, rule_id, disabled, "rule_disabled",
                  f"{_rule_label(rule_no)}: 제약 이름표 '{rule_id}' 를 끄는 것은 하드락 완화입니다.")
        for rule_no in spec.hard_lock_rule_nos
    ]


def _rule_cap(rule_no: int) -> int | None:
    """레지스트리에서 하드락 번호의 수치 상한을 찾는다(없으면 None — bool 계열)."""
    for spec in SETTINGS:
        lock = spec.hard_lock
        if isinstance(lock, HardLock) and lock.rule_no == rule_no and lock.cap is not None:
            return int(lock.cap)
    return None


def rule_cap(rule_no: int) -> int | None:
    """`_rule_cap` 의 공개 이름 — 카드 필터가 상한을 레지스트리에서 읽는다(EI-11 §2.0.2)."""
    return _rule_cap(rule_no)
