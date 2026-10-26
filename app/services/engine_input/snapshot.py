"""EngineInput canonical 직렬화와 digest — EI-9a(`EI9_SPEC.md` §2.2, 설계서 §2.4·§7.3).

canonical 규칙(아래 `_value` 이하)은 `tests/engine_input/canonical.py` 가 먼저 갖고 있던
순수 핵심을 **원문 그대로** 옮긴 것이다(O-6, EI9_SPEC.md §6). 그 쪽은 이제 이 모듈을
재수출하고, 골든 캡처 전용(층 L0~L3 `digest_view`·`_attempts_view`·env 손 목록 폴백)만
남긴다 — 두 벌을 두지 않는다. 증명은 골든 파일이 바이트 그대로 통과하는 것이다.

이 모듈이 더하는 것은 `EngineInput` 전용 digest 제외 규칙이다(§2.2·§2.4):
  - 뺀다: `post`(후처리 입력 기록) · `load_report` 전체 · `ctx.job_id`·`ctx.runtime` ·
    `settings_raw` 의 메타 칸(레지스트리 `source=META`, `settings.META_KEYS()`에서 파생) ·
    `env_effective` 의 런타임 토글·진단(레지스트리 `env.digest_names()` 의 여집합).
  - 넣는다: 나머지 전부 — `settings_raw` 비메타 칸·`settings_eff`·`codebook`·`demand`
    (`pre`·`post` 둘 다 — 차감 뒤 수요는 엔진이 실제로 보는 값이다)·`roster`·`cells`·
    `carryover`·`grade`·`team`·`preferences`·`solver_params`.
제외 목록은 **호출 시점에** 레지스트리에서 파생한다(손 목록을 두지 않는다 — import 때
env 를 읽지 않는다, BR-4·[PY-04]).
"""
from __future__ import annotations

import dataclasses
import enum
import hashlib
import json
import math
import numbers
from collections.abc import Mapping
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from types import SimpleNamespace
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .model import EngineInput

_MAX_DEPTH = 64
_SCALARS = (str, bool, type(None))


class CanonicalError(TypeError):
    """canonical 로 바꿀 수 없는 값 — 경로를 메시지에 담는다."""


# ── 스칼라 ──────────────────────────────────────────────────────────────


def _scalar(value):
    """스칼라면 (True, 변환값), 아니면 (False, None)."""
    if isinstance(value, _SCALARS):
        return True, value
    if isinstance(value, enum.Enum):
        return True, canonical(value.value)
    if isinstance(value, numbers.Integral):
        return True, int(value)
    if isinstance(value, Decimal):
        return True, str(value)
    if isinstance(value, numbers.Real):
        number = float(value)
        return True, number if math.isfinite(number) else str(number)
    if isinstance(value, (datetime, date, time)):
        return True, value.isoformat()
    if isinstance(value, timedelta):
        return True, f"timedelta:{value.total_seconds()}"
    if isinstance(value, (bytes, bytearray)):
        return True, bytes(value).hex()
    return False, None


# ── 컨테이너 ────────────────────────────────────────────────────────────


def _json_text(value) -> str:
    """canonical 값의 정렬된 JSON 문자열(집합 정렬·키 변환 기준)."""
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def _key_text(key, path: str) -> str:
    """dict 키를 문자열로 — 문자열은 그대로, 정수는 숫자 문자열, 그 밖은 JSON 문자열."""
    if isinstance(key, str):
        return key
    if isinstance(key, numbers.Integral) and not isinstance(key, bool):
        return str(int(key))
    return _json_text(canonical(key, _path=f"{path}<key>"))


def _mapping(value: Mapping, depth: int, path: str) -> dict:
    """dict → 문자열 키 dict(`_sa_*` 제거). ORM 값(관계 속성)은 참조로만 남긴다."""
    out: dict = {}
    for key, item in value.items():
        if isinstance(key, str) and key.startswith("_sa_"):
            continue
        text = _key_text(key, path)
        if text in out:
            raise CanonicalError(f"{path}: 키 {key!r} 가 다른 키와 같은 문자열 {text!r} 이 된다")
        out[text] = _value(item, depth + 1, f"{path}.{text}", as_ref=True)
    return out


def _orm_identity(obj) -> list | None:
    """ORM 객체의 identity(기본키 값) — 아직 저장 전이면 None."""
    from sqlalchemy import inspect as sa_inspect

    identity = sa_inspect(obj).identity
    return None if identity is None else canonical(list(identity))


def _orm(obj, depth: int, path: str, *, as_ref: bool):
    """ORM 객체 → 참조 또는 전체 기록(`__dict__` 에 실린 칸만 — 지연 로딩을 일으키지 않는다)."""
    name = type(obj).__name__
    if as_ref:
        return {"__orm_ref__": name, "identity": _orm_identity(obj)}
    record = _mapping(vars(obj), depth, path)
    record["__orm__"] = name
    return record


def _is_record_object(obj) -> bool:
    """속성 묶음으로 기록할 객체인가 — dataclass 인스턴스·SimpleNamespace."""
    is_dataclass_instance = dataclasses.is_dataclass(obj) and not isinstance(obj, type)
    return is_dataclass_instance or isinstance(obj, SimpleNamespace)


def _object(obj, depth: int, path: str):
    """dataclass·SimpleNamespace → `__type__` + 속성 · numpy 스칼라 → 파이썬 값 · 그 밖은 CanonicalError."""
    if _is_record_object(obj):
        record = _mapping(vars(obj), depth, path)
        record["__type__"] = type(obj).__qualname__
        return record
    if type(obj).__module__ == "numpy" and hasattr(obj, "item"):
        return _value(obj.item(), depth + 1, path)
    raise CanonicalError(f"{path}: canonical 로 바꿀 수 없는 타입 {type(obj).__module__}.{type(obj).__qualname__}")


def _value(value, depth: int, path: str, *, as_ref: bool = False):
    """canonical 본체 — 깊이를 세며 재귀한다."""
    if depth > _MAX_DEPTH:
        raise CanonicalError(f"{path}: 깊이 {_MAX_DEPTH} 초과(순환 참조 의심)")
    ok, scalar = _scalar(value)
    if ok:
        return scalar
    if hasattr(value, "_sa_instance_state"):
        return _orm(value, depth, path, as_ref=as_ref)
    if isinstance(value, Mapping):
        return _mapping(value, depth, path)
    if isinstance(value, (set, frozenset)):
        items = [_value(item, depth + 1, f"{path}[]") for item in value]
        return sorted(items, key=_json_text)
    if isinstance(value, (list, tuple)):
        return [_value(item, depth + 1, f"{path}[{i}]") for i, item in enumerate(value)]
    return _object(value, depth, path)


# ── 공개 함수 ───────────────────────────────────────────────────────────


def canonical(value, *, _path: str = "$"):
    """값을 JSON 으로 바로 쓸 수 있는 canonical 구조로 바꾼다(원본은 건드리지 않는다)."""
    return _value(value, 0, _path)


def canonical_json(value) -> str:
    """canonical 구조의 JSON 문자열(키 정렬·공백 없음)."""
    return _json_text(canonical(value))


def sha256_digest(value) -> str:
    """canonical JSON 의 sha256 hex."""
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


# ── EngineInput 전용 digest 제외(§2.2·§2.4) ──────────────────────────────


def _registry_meta_keys() -> frozenset[str]:
    """`settings_raw` 메타 칸 — 손 목록이 아니라 레지스트리 `source=META` 에서 파생한다."""
    from services.engine_contract.settings import META_KEYS

    return frozenset(META_KEYS())


def _is_digest_excluded_env(name: str) -> bool:
    """env 가 런타임 토글·진단(digest 제외)인가 — `env.digest_names()` 의 여집합.

    `EngineInput.env_effective` 는 `ENGINE_PATH_NAMES`(레지스트리에 등록된 이름)만 담는다는
    전제라 손 목록 폴백을 두지 않는다 — 레지스트리에 없는 이름이 들어오면 포함 쪽으로 둔다
    (digest 에서 조용히 사라지는 쪽보다 안전).
    """
    from services.engine_contract.env import ENV_BY_NAME

    spec = ENV_BY_NAME.get(name)
    return not spec.digest if spec is not None else False


def digest_view(ei: EngineInput) -> dict:
    """digest 에서 뺄 칸(§2.2)을 지운 canonical 사본. `ei` 원본은 바꾸지 않는다.

    `canonical(ei)` 는 매 호출 새 dict/list 트리를 만들므로(원본과 공유 참조 없음) 이 함수가
    반환 전에 제자리로 지워도 `ei` 에는 닿지 않는다.
    """
    view = canonical(ei)
    view.pop("post", None)
    view.pop("load_report", None)
    ctx = view.get("ctx")
    if isinstance(ctx, dict):
        ctx.pop("job_id", None)
        ctx.pop("runtime", None)
    settings_raw = view.get("settings_raw")
    if isinstance(settings_raw, dict):
        for key in _registry_meta_keys():
            settings_raw.pop(key, None)
    env_effective = view.get("env_effective")
    if isinstance(env_effective, dict):
        for name in [n for n in env_effective if _is_digest_excluded_env(n)]:
            env_effective.pop(name)
    return view


def digest(ei: EngineInput) -> str:
    """`EngineInput` 의 digest — 제외 칸을 뺀 뒤 sha256(§2.2)."""
    return sha256_digest(digest_view(ei))
