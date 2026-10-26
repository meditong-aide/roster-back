"""골든 캡처의 canonical 직렬화와 digest — 설계서 §7.1 '결정론' · §2.4/§7.3 digest 제외 목록.

canonical 규칙(§7.1)
  - dict 키 정렬(JSON 직렬화 때 sort_keys) · 키는 문자열로(정수 → 숫자 문자열, 튜플 등 → JSON 문자열)
  - set → 정렬한 list · tuple → list · date/datetime/time → ISO · Decimal → str
  - `_sa_*` 키 제거(ORM `__dict__` 의 InstanceState)
  - 간호사 순서는 그대로 둔다 — 캡처 지점이 이미 엔진 정렬(engine_index 순)로 넘긴다
  - ORM 객체: 목록 원소·최상위면 전체 기록, dict 값(관계 속성)이면 '참조'(클래스 + identity)만
  - dataclass(엔진 Nurse·NurseRosterConfig) → `__type__` + `vars()`
  - 모르는 타입은 CanonicalError — repr 은 메모리 주소가 섞여 결정론을 깨므로 쓰지 않는다

★EI-9a(2026-10-15, O-6): 위 규칙의 순수 핵심(`_scalar`·`_mapping`·`_value`·`canonical`·
  `canonical_json`·`sha256_digest` + 보조 `_json_text`·`_key_text`·`_orm_identity`·`_orm`·
  `_is_record_object`·`_object`·`CanonicalError`·`_MAX_DEPTH`·`_SCALARS`)은
  `app/services/engine_input/snapshot.py` 로 **원문 그대로** 옮겼다(EI9_SPEC.md §2.2·§6 O-6).
  이 모듈은 이제 그 함수들을 재수출만 한다 — 두 벌을 두지 않는다. 증명은 골든 파일이
  바이트 그대로 통과하는 것(이 아래 '층 전용' 로직만 이 파일에 남았다).

digest(§2.4 '빼는 것' 중 캡처 층에 있는 것) — 이 파일의 몫(층 L0~L3 전용, EngineInput 자체의
digest 제외는 `snapshot.digest_view(ei)` 가 한다)
  - config dict(L0 `config_dict` · L1 `config_data`)의 `settings_raw` 메타 칸 6개
    (사다리 모드 시도 기록 `attempts[*].delta.config_data` 의 같은 칸도)
  - 층 L3(후처리 입력 — §2.4 `post`) 통째로
  - 시도 기록 `attempts[*].env` 의 런타임 토글·진단 env(§3.1 — 기록만). 엔진 계약·실험 env 는 넣는다
  - 시도 기록 `attempts[*].queries_before`(쿼리 위치 — 엔진 입력이 아니다. 골든 기록에는 남는다)
  - `load_report`·`ctx` 는 아직 캡처 층에 없다(EI-9 몫)
"""

from __future__ import annotations

import copy

from services.engine_contract.env import ENV_BY_NAME as _env_by_name

# ★EI-6(2026-10-15): 이 손 목록은 이제 레지스트리 `source=META` 에서 파생한다(§2.4 각주 —
#   "목록은 EI-1 에서 손 목록으로 시작하고, EI-6 부터 레지스트리 source=META 에서 파생한다").
#   값은 바뀌지 않는다(동작 불변) — `tests/engine_contract/test_registry.py` 가
#   `canonical.SETTINGS_META_KEYS == settings.META_KEYS()` 를 단언해 어긋나면 바로 드러난다.
from services.engine_contract.settings import META_KEYS as _registry_meta_keys

# ★EI-9a(O-6): 순수 핵심 재수출 — 정의는 `services.engine_input.snapshot` 에 있다(원문 그대로 옮김).
from services.engine_input.snapshot import (  # noqa: F401 (재수출)
    CanonicalError,
    _is_record_object,
    _json_text,
    _key_text,
    _mapping,
    _object,
    _orm,
    _orm_identity,
    _scalar,
    _value,
    canonical,
    canonical_json,
    sha256_digest,
)

from .engine_env import is_runtime_toggle

#: §2.4 — settings_raw 메타 칸. 엔진·precheck·온톨로지가 읽지 않고 런 스스로 바꾸는 칸도 있다.
SETTINGS_META_KEYS = frozenset(_registry_meta_keys())


def _is_digest_excluded_env(name: str) -> bool:
    """env 가 digest 에서 빠지는 '런타임 토글·진단'인가 — EI-6(2026-10-15) 반박 검토(MEDIUM)
    반영: 이제 ENV 레지스트리(`services.engine_contract.env`)의 `digest` 칸에서 파생한다
    (`not spec.digest`). `engine_env.is_runtime_toggle()`(손 목록, cp_sat·cp_sat_basic.py·
    roster_create_service.py 좁은 스캔 기준)과 실측으로 13종이 갈렸는데, 그중 좁은 스캔
    범위 안 5종(AIDE_CAUSE_GATE·AIDE_LEX_DIAG_S3·CONFLICT_CORE_MEMBER_BUDGET·
    ROSTER_GEN_BUDGET_SEC·ROSTER_LADDER_MIN_LEFT_SEC)은 레지스트리가 RUNTIME_TOGGLE(digest=
    False)로 보는데 손 목록은 토글로 안 쳐서 digest 에 섞여 들어갔다. 레지스트리에 없는
    이름(이론상 `engine_env_names()` ⊆ `ENGINE_PATH_NAMES` 계약 테스트가 막아 주지만, 방어적으로)은
    손 목록으로 폴백한다. 기존 골든 4종(기록된 실제 env)은 두 판정이 일치해 이 전환으로 바뀌는
    골든 digest 는 없다(ei6/inv_env.json '차이의_골든_영향')."""
    spec = _env_by_name.get(name)
    if spec is not None:
        return not spec.digest
    return is_runtime_toggle(name)
#: 캡처 층에서 config dict 가 놓이는 자리(층 → 호출 기록 안의 인자 이름).
CONFIG_ARG_BY_LAYER = {"L0": "config_dict", "L1": "config_data"}
#: digest 에서 통째로 빼는 층 — L3 는 후처리 입력(§2.4 `post`)이다.
DIGEST_EXCLUDED_LAYERS = frozenset({"L3"})


def _drop_meta(config) -> None:
    """config dict 에서 settings_raw 메타 칸을 지운다(제자리)."""
    if isinstance(config, dict):
        for key in SETTINGS_META_KEYS:
            config.pop(key, None)


#: 시도 기록에서 digest 에 넣지 않는 칸 — 엔진 입력이 아니다. `queries_before` 는 그 시도 직전까지의
#: 쿼리 수(쿼리 위치)라 재조회가 줄면 입력이 같아도 바뀐다. 쿼리 수·순서는 `queries` 기록이 단언한다.
ATTEMPT_DIGEST_EXCLUDED = ("queries_before",)


def _attempts_view(attempts: list) -> None:
    """시도 기록에서 쿼리 위치 칸·런타임 토글 env·config 차이의 메타 칸을 지운다(제자리)."""
    for attempt in attempts:
        for key in ATTEMPT_DIGEST_EXCLUDED:
            attempt.pop(key, None)
        env = attempt.get("env")
        if isinstance(env, dict):
            for name in [n for n in env if _is_digest_excluded_env(n)]:
                env.pop(name)
        delta = (attempt.get("delta") or {}).get("config_data")
        if isinstance(delta, dict):
            _drop_meta(delta.get("changed"))
            if isinstance(delta.get("removed"), list):
                delta["removed"] = [k for k in delta["removed"] if k not in SETTINGS_META_KEYS]


def digest_view(layers: dict) -> dict:
    """digest 에서 뺄 칸(§2.4)을 지운 사본. 캡처 층 원본은 그대로 둔다."""
    view = copy.deepcopy({k: v for k, v in layers.items() if k not in DIGEST_EXCLUDED_LAYERS})
    for layer, arg in CONFIG_ARG_BY_LAYER.items():
        for call in view.get(layer) or []:
            _drop_meta(call.get(arg))
    _attempts_view(view.get("attempts") or [])
    return view


def layers_digest(layers: dict) -> str:
    """캡처 층 digest — 제외 칸을 뺀 뒤 sha256."""
    return sha256_digest(digest_view(layers))
