"""설정 실효값 해석 — `effective`·`effective_hard_lock`(설계서 §5.3 EI-8).

`create_config_from_db`(`services.cp_sat_basic.CPSATBasicEngine`)가 만든 `NurseRosterConfig`
에서 정본 키·별칭으로 cfg 단계 실효값을 읽는다. 대응은 레지스트리 `(key, *aliases)` 순서로
`vars(cfg)`(인스턴스 속성)를 찾는 규칙 하나다 — 레지스트리에 cfg 필드용 새 칸을 두지 않는다
(두 벌 금지, `.omc/aide_docs/v2/engine_input/ei8/EI8_SPEC.md` §2.2·§2.5).

'실효값'은 **cfg 단계**까지다. 엔진이 cfg 를 받은 뒤 다시 섞는 하류 env 3종·`off_days`
재해석(`cp_sat/off_policy.resolve_effective_off_days`)·사다리 시도별 변형은 반영하지 않는다
(BR-4, 같은 문서 §2.3). 이 모듈은 레지스트리만 모듈 수준에서 import 한다(순수 데이터 원칙
유지) — 엔진 import 는 `effective_hard_lock` 안의 지연 import 한 곳뿐이다.

이 모듈은 **월 안(`create_config_from_db`) 해석만** 낸다. 월경계·월중 경계 빌더
(`roster_create_service.build_cross_month_constraints`·`build_mid_month_boundary_constraints`)
는 같은 하드락 키라도 **키 부재를 다른 기본값으로** 읽어
cfg 단계와 다른 값을 낼 수 있다(연속근무 K 의 별칭 폴백은 EI-12 에서 없앴고, 나이트 연속 L 은 D13 ① 뒤 정본 `three_seq_nig` 만 읽는다 —
`tests/engine_contract/test_effective.py` `test_boundary_builder_ignores_alias_and_diverges_from_effective_only_on_default`).
별칭 완화·원시값 판정은 EI-7 `policy/hard_lock.py` 가 맡는다 — 이 모듈과 섞지 않는다.
"""

from __future__ import annotations

from collections.abc import Mapping

from .settings import SETTINGS, SETTINGS_BY_KEY, HardLock

_MISSING = object()

#: 별칭 → 정본 키. 모듈 import 시 1회 만든다(정본 키가 별칭보다 항상 우선 — `canonical_key`).
_ALIAS_TO_KEY: dict[str, str] = {alias: spec.key for spec in SETTINGS for alias in spec.aliases}

#: 하드락 11키(cfg 를 거치는 것) — 월경계 스위치 2키(`kind='boundary_switch'`)는 뺀다.
HARD_LOCK_CFG_KEYS: tuple[str, ...] = tuple(
    s.key for s in SETTINGS if isinstance(s.hard_lock, HardLock) and s.hard_lock.kind != "boundary_switch"
)
#: 하드락이지만 cfg 를 거치지 않는 2키(월경계 스위치 — 생성 서비스가 config_dict 에서 직접 읽는다).
HARD_LOCK_NON_CFG_KEYS: tuple[str, ...] = tuple(
    s.key for s in SETTINGS if isinstance(s.hard_lock, HardLock) and s.hard_lock.kind == "boundary_switch"
)

#: `effective_hard_lock` 이 요구인원 자리표시로 쓰는 값. 하드락 11키 계산은 요구인원을
#: 읽지 않는다(cp_sat_basic.py:456-681 판독 + 골든 L1 14건 실측) — 자리표시 없이 부르면
#: 빈 config·요구인원 0 행이 ValueError 로 막혀 '빈 config 의 엔진 실효값'을 낼 수 없다.
_PLACEHOLDER_DAILY_SHIFT_REQUIREMENTS: dict[str, int] = {"D": 1, "E": 1, "N": 1}


class EffectiveLookupError(KeyError):
    """`effective` 조회 실패 공통 베이스(`UnknownSettingKey`·`NotInConfig` 의 부모)."""


class UnknownSettingKey(EffectiveLookupError):
    """정본 키·별칭 어디에도 없는 키 — `rules.py` 철자·`CODE_CONSTS`·`RUNTIME_DATA_NOT_SETTINGS`
    키는 받지 않는다."""

    def __str__(self) -> str:
        return f"{self.args[0]!r}: 정본 키·별칭만 받는다(레지스트리 SETTINGS 에 없음)"


class NotInConfig(EffectiveLookupError):
    """레지스트리에는 있지만 이 cfg 인스턴스에 대응 속성이 없는 키(§2.4 범위 밖 52키 등)."""

    def __init__(self, key: str, source: str) -> None:
        super().__init__(key, source)
        self.key = key
        self.source = source

    def __str__(self) -> str:
        return f"{self.key!r}: cfg(create_config_from_db 결과)에 대응 속성이 없다(source={self.source})"


def canonical_key(key: str) -> str:
    """정본 키 또는 별칭을 받아 정본 키를 돌려준다.

    정본 키가 별칭보다 우선한다. 레지스트리 SETTINGS·별칭 어디에도 없으면 `UnknownSettingKey`.
    """
    if key in SETTINGS_BY_KEY:
        return key
    owner = _ALIAS_TO_KEY.get(key)
    if owner is None:
        raise UnknownSettingKey(key)
    return owner


def _require_nurse_roster_config(cfg: object) -> None:
    """`cfg` 가 `create_config_from_db` 결과(`NurseRosterConfig`)인지 이름·모듈로만 본다.

    무거운 import 없이 타입을 가려낸다(리뷰 반영) — ORM `RosterConfig` 행·`RosterConfigCreate`
    (pydantic)·`SimpleNamespace` 처럼 정본 키 이름의 속성을 우연히 가진 객체를 받으면 원시값이
    '실효값'인 양 조용히 나온다(별칭·하드락 변환을 하나도 거치지 않는다). 그런 입력은 여기서
    막는다 — config dict 는 `effective_hard_lock` 을 쓴다.
    """
    if type(cfg).__name__ == "NurseRosterConfig" and type(cfg).__module__ == "db.roster_config":
        return
    raise TypeError(
        "cfg 는 create_config_from_db 결과(NurseRosterConfig)만 받는다 — "
        "ORM 행·pydantic 모델·dict 는 effective_hard_lock 을 쓴다"
    )


def cfg_field_name(cfg: object, key: str) -> str | None:
    """`(정본 키, *별칭)` 순서로 `vars(cfg)`(인스턴스 속성)에 있는 첫 이름을 돌려준다.

    `hasattr`/`getattr` 기본값을 쓰지 않는다 — dataclass 클래스 기본값·property 까지 잡아서
    '조건부 setattr 미설정'(예: `by_day`)을 가려내지 못하기 때문이다(§2.2 규칙 3).
    대응되는 이름이 없으면 None(EI-10·14 소비자가 필드 이름만 알고 싶을 때 쓴다).

    Raises:
        TypeError: `cfg` 가 `NurseRosterConfig` 가 아니면(`_require_nurse_roster_config`).
    """
    _require_nurse_roster_config(cfg)
    spec = SETTINGS_BY_KEY[canonical_key(key)]
    instance_vars = vars(cfg)
    for name in (spec.key, *spec.aliases):
        if name in instance_vars:
            return name
    return None


def effective(cfg: object, key: str, default: object = _MISSING) -> object:
    """정본 키 또는 별칭으로 cfg 단계 실효값을 돌려준다(§2.2 해석 규칙).

    `cfg` 는 `create_config_from_db` 결과(`NurseRosterConfig`)를 전제한다. 값은 복사·변환 없이
    `getattr(cfg, name)` 그대로 돌려준다 — 별칭으로 물어도 결과는 정본 키와 같다. 반환된
    dict·list 는 cfg·config_data 와 같은 객체일 수 있으니(`create_config_from_db` 가 일부
    입력을 복사 없이 그대로 가리킨다) 읽기 전용으로만 다룬다.

    Raises:
        TypeError: `cfg` 가 Mapping(= config dict)이거나, `NurseRosterConfig` 가 아닌 다른
            객체(ORM 행·pydantic 모델 등)이면(`_require_nurse_roster_config`) — 둘 다
            config dict 는 `effective_hard_lock` 을 쓴다는 뜻이다.
        UnknownSettingKey: 정본 키·별칭 어디에도 없는 키.
        NotInConfig: 레지스트리 키지만 이 cfg 에 대응 속성이 없고, `default` 도 주지 않았을 때.
    """
    if isinstance(cfg, Mapping):
        raise TypeError(
            "effective 는 create_config_from_db 결과(NurseRosterConfig)를 받는다 — "
            "config dict 는 effective_hard_lock 을 쓴다"
        )
    canon = canonical_key(key)
    spec = SETTINGS_BY_KEY[canon]
    name = cfg_field_name(cfg, canon)
    if name is None:
        if default is not _MISSING:
            return default
        raise NotInConfig(spec.key, spec.source)
    return getattr(cfg, name)


def effective_hard_lock(config_data: Mapping[str, object]) -> dict[str, object]:
    """하드락 cfg 경유 11키(`HARD_LOCK_CFG_KEYS`)의 실효값 dict(§3).

    `config_data` 는 건드리지 않는다(얕은 사본에만 요구인원 자리표시를 넣는다). 월경계
    스위치 2키(`HARD_LOCK_NON_CFG_KEYS`)는 cfg 를 거치지 않아 이 dict 에 없다 — 그 둘은
    생성 서비스가 config_dict 에서 직접 읽으므로(EI-7 `override_violations` 가 원시값으로
    판정) 여기서 다시 해석하면 엔진 지식이 두 벌이 된다.

    입력 별칭 정규화는 생성 진입(`engine_contract.aliases`)이 하고 `create_config_from_db` 는 정본만 읽는다.
    이 함수는 그 거울이라 정규화하지 않는다(EI-12). 예외는 잡지 않고 그대로 올린다 — `create_config_from_db` 가 ORM 컬럼
    기준 하드락 키(예: `max_conseq_work`·`shift_priority`)에서 실패하면 그 설정은 엔진도
    실패한다는 뜻이라, 하드락 입력 키만 걸러 받는 대신(Q4) 실패를 숨기지 않는 쪽을 택했다.
    단 `max_extra_off_days`·`oversupply_equalize_weight`(ORM 컬럼 아님)는 생성 서비스가
    엔진을 부르기 전에 None 을 보정해 채우므로, 그 두 키만 None 이면 이 래퍼는 TypeError 를
    내도 실제 생성 런은 성공한다(리뷰 반영 — 범위를 좁힌다).

    env(`AIDE_MAX_CONSEC_WORK`)는 호출 시점에 읽는다(BR-4) — 굳히지 않는다. 이 env 는
    **호출한 프로세스 기준**이다. 저장할 설정을 판정하는 API 프로세스와 실제로 그 설정을
    돌릴 워커 프로세스의 env 가 다르면(MS-5) 이 함수가 돌려주는 값도 다르다 — 완화 판정에
    쓸 때는 EI-7 원시값 판정과 같이 써야 한다(리뷰 반영).

    `three_seq_nig` 값은 bool 이 아니라 int `3`/`2` 다(`== True` 비교 금지). bool 하드락
    값은 변환 없이 원시값(`None`·`0`·`'false'`·`-1`)이 그대로 나올 수 있다 — 엔진은 이 값을
    `is True` 가 아니라 **truthiness** 로 읽는다(예: 문자열 `'false'` 는 참). 첫 호출은
    `create_config_from_db` 의 모듈 수준 `dotenv`·`db.client2` 로딩 때문에 약 1.6초가 든다
    (그 뒤 호출은 이 비용이 없다). 생성 런에서는 요청의 `not_one_night`·`config_override` 가
    DB config 행 값을 덮어쓰므로, 이 함수가 돌려주는 'config 실효값'이 실제 생성에 쓰인
    값과 다를 수 있다([RVERIFY-08]) — 리뷰 반영, docstring 만 추가(동작 변경 없음).

    `create_config_from_db` 의 print 는 억제하지 않는다 — `contextlib.redirect_stdout` 은
    프로세스 전역이라, 동시에 도는 다른 코드의 로그까지 삼킬 수 있어 쓰지 않는다(호출부가
    필요하면 직접 감싼다).
    """
    from services.cp_sat_basic import CPSATBasicEngine  # 지연 import — 모듈 수준은 순수 데이터로 둔다

    cd = dict(config_data)
    cd["daily_shift_requirements"] = dict(_PLACEHOLDER_DAILY_SHIFT_REQUIREMENTS)
    cfg = CPSATBasicEngine().create_config_from_db(cd)
    return {k: effective(cfg, k) for k in HARD_LOCK_CFG_KEYS}


__all__ = [
    "EffectiveLookupError",
    "UnknownSettingKey",
    "NotInConfig",
    "canonical_key",
    "cfg_field_name",
    "effective",
    "HARD_LOCK_CFG_KEYS",
    "HARD_LOCK_NON_CFG_KEYS",
    "effective_hard_lock",
]
