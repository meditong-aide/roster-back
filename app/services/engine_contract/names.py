"""설정 칸 이름 — 바깥 이름(물리 컬럼·API JSON·저장 JSON) ↔ 파이썬 이름 (NM · D58).

roster_config 14칸은 이름이 동작과 어긋나거나(예 `off_first` 는 True 가 '근무 인원 우선') 이름만으로 뜻이
정해지지 않아, 파이썬 안에서는 뜻이 드러나는 이름을 쓴다. 바깥 계약은 옛 이름 그대로다 — 프론트·DB·저장된
JSON 은 바뀌지 않는다. 값의 뜻(True/False·단위)도 그대로다.

- 요청에서 들어온 설정 dict 는 `to_internal`, 응답·저장으로 나가는 dict 는 `to_external`(얕게)·`to_external_deep`.
- 값은 그대로 옮긴다(None 포함) — None 과 키 부재는 엔진에서 뜻이 다르다(예 `two_offs_per_week` None=꺼짐 · 부재=켬).
- 저장 JSON 을 읽을 때는 설정 dict 자리만 `to_internal` 한다. 깊은 역변환은 없다 — 옛 이름은 위반 유형·규칙 id 같은
  바깥 식별자로도 쓰여(예 위반 type `three_seq_nig`) 문자열 값을 통째로 바꾸면 그것까지 바뀐다.
- 시험은 `ROSTER_NM_STRICT=1`(tests/conftest.py) — 옛 이름이 안쪽에 남으면 `OldConfigNameError` 로 드러난다.
  운영은 새 이름으로 이어 동작을 유지하고 이름·자리별로 한 번 경고한다.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Mapping
from types import MappingProxyType

logger = logging.getLogger(__name__)

#: 바깥 이름 → 파이썬 이름. 레지스트리(`settings.SETTINGS` 의 `column`)는 이 표와 같아야 한다(시험이 강제).
EXTERNAL_TO_INTERNAL: Mapping[str, str] = MappingProxyType({
    "off_first": "staffing_first",                        # True=근무 인원 우선(남는 칸 OFF · 월 OFF 수 무시)
    "banned_day_after_eve": "ban_shift_transitions",      # True=E→D·N→D·N→E 전이 금지(하드락 #2) · 끄면 D→N 사후 검사까지 함께
    "nod_noe": "avoid_nod_noe_eod",                       # True=N-O-D·N-O-E·E-O-D 패턴 벌점
    "min_exp_per_shift": "senior_min_years",              # 책임 간호사로 칠 경력 연차(년)
    "req_exp_nurses": "senior_nurses_per_shift",          # 근무마다 둘 책임 간호사 수(명)
    "preceptee_shift_count": "count_preceptee_in_staffing",  # True=프리셉티를 근무 인원에 셈
    "three_seq_nig": "allow_three_consecutive_nights",    # True=나이트 최대 3연속 · False=2연속
    "not_one_night": "ban_single_night",                  # True=1N 금지(하드락 #7)
    "sequential_offs": "avoid_isolated_off_and_work",     # True=고립 OFF·고립 근무 벌점
    "two_offs_per_week": "prefer_two_offs_per_7days",     # True=1일부터 7일 블록마다 OFF 2개 미만 벌점
    "preceptee_on": "preceptee_follows_preceptor",        # True=프리셉티가 프리셉터와 같은 근무(하드)
    "off_swap_enabled": "convert_excess_off_to_leave",    # True=OFF 기준 초과분 연차 전환 후처리
    "fixed_wanted_use_yn": "apply_confirmed_wanted",      # True=확정 원티드를 고정 칸으로 반영
    "max_conseq_off": "soft_max_conseq_off",              # 연속 OFF 상한(벌점 — 넘을 수 있음)
})
#: 파이썬 이름 → 바깥 이름.
INTERNAL_TO_EXTERNAL: Mapping[str, str] = MappingProxyType({v: k for k, v in EXTERNAL_TO_INTERNAL.items()})

STRICT_ENV = "ROSTER_NM_STRICT"


class OldConfigNameError(RuntimeError):
    """바깥(옛) 이름이 파이썬 안쪽에서 쓰였다 — strict(시험)에서만 던진다.

    RuntimeError 라 `getattr(obj, 옛 이름, 기본값)` 도 기본값으로 삼키지 못하고 드러난다."""


def strict() -> bool:
    return os.environ.get(STRICT_ENV) == "1"


_warned: set[str] = set()


def report_old_name(old: str, where: str) -> None:
    """안쪽에서 옛 이름을 만났다 — strict 면 예외, 아니면 (이름·자리)별 경고 1회."""
    msg = f"옛 설정 이름 '{old}' 이 파이썬 안쪽에서 쓰였다({where}) — '{EXTERNAL_TO_INTERNAL[old]}' 로 바꿔야 한다"
    if strict():
        raise OldConfigNameError(msg)
    key = f"{old}@{where}"
    if key not in _warned:
        _warned.add(key)
        logger.warning(msg)


def internal_key(name: str) -> str:
    """바깥 이름이면 파이썬 이름, 아니면 그대로."""
    return EXTERNAL_TO_INTERNAL.get(name, name)


def external_key(name: str) -> str:
    """파이썬 이름이면 바깥 이름, 아니면 그대로 — 응답·사용자 메시지에 키 이름을 적을 때 쓴다."""
    return INTERNAL_TO_EXTERNAL.get(name, name)


def _rename_keys(config: Mapping, table: Mapping[str, str], *, conflict_raises: bool) -> dict:
    """키만 바꾼 사본(순서·값 그대로). 같은 칸이 두 이름으로 함께 있으면 값이 같을 때 하나로 접는다.

    값이 다를 때 — 들어오는 쪽(`conflict_raises=True`)은 어느 값이 맞는지 모르므로 `ValueError`. 나가는 쪽(응답·저장)은
    이미 저장·생성이 끝난 뒤라 실패시키지 않고, 파이썬 이름 쪽 값(안쪽의 현재 값)을 남기고 경고만 한다."""
    out: dict = {}
    from_table: set = set()  # out 의 키 중 표로 바꿔 넣은 것(= 원래 이름이 달랐던 것)
    for key, value in config.items():
        renamed = table.get(key, key)
        if renamed in out:
            if out[renamed] != value:
                external = INTERNAL_TO_EXTERNAL.get(renamed, renamed)
                internal = EXTERNAL_TO_INTERNAL.get(renamed, renamed)
                msg = (f"같은 설정이 두 이름으로 다른 값과 함께 왔다 — '{external}'(바깥 이름)·'{internal}'(파이썬 이름): "
                       f"{out[renamed]!r} ≠ {value!r}")
                if conflict_raises:
                    raise ValueError(msg)
                logger.warning("%s — 파이썬 이름 쪽 값을 남긴다", msg)
                keep_new = (key in INTERNAL_TO_EXTERNAL) and renamed not in from_table
                if keep_new:
                    out[renamed] = value
                    from_table.add(renamed)
            elif renamed != key:
                from_table.add(renamed)
            continue
        out[renamed] = value
        if renamed != key:
            from_table.add(renamed)
    return out


def to_internal(config):
    """바깥 이름 키를 파이썬 이름으로 바꾼 얕은 사본(순서·값 그대로 · None 포함).

    같은 칸이 두 이름으로 함께 오면 값이 같을 때 하나로 접고, 다르면 `ValueError`(두 이름을 다 적는다).
    바꿀 키가 없거나 Mapping 이 아니면 받은 객체를 그대로 돌려준다(호출부의 identity·truthiness 판정 유지)."""
    if not isinstance(config, Mapping) or not any(k in EXTERNAL_TO_INTERNAL for k in config):
        return config
    return _rename_keys(config, EXTERNAL_TO_INTERNAL, conflict_raises=True)


def to_external(config):
    """파이썬 이름 키를 바깥 이름으로 바꾼 얕은 사본(`to_internal` 의 역). 두 이름 값이 다르면 파이썬 이름 쪽을 남긴다."""
    if not isinstance(config, Mapping) or not any(k in INTERNAL_TO_EXTERNAL for k in config):
        return config
    return _rename_keys(config, INTERNAL_TO_EXTERNAL, conflict_raises=False)


def to_external_deep(obj):
    """응답·저장으로 나가는 값 전체 — dict 키와 **정확히 일치하는 문자열 값**의 파이썬 이름을 바깥 이름으로.

    새 이름 14개는 이 변경 전 코드·프론트 어디에도 없던 이름이라(NM 설계 §1) 위치와 상관없이 바꿔도 안전하다.
    문장 안 부분 문자열은 건드리지 않는다. dict·list·tuple 만 따라 내려가고 tuple 은 list 로 돌려준다(JSON 과 같음).
    나가는 쪽이라 실패시키지 않는다 — 같은 dict 에 두 이름이 다른 값으로 있으면 파이썬 이름 쪽 값을 남기고 경고한다."""
    if isinstance(obj, Mapping):
        return _rename_keys({k: to_external_deep(v) for k, v in obj.items()}, INTERNAL_TO_EXTERNAL,
                            conflict_raises=False)
    if isinstance(obj, (list, tuple)):
        return [to_external_deep(v) for v in obj]
    if isinstance(obj, str):
        return INTERNAL_TO_EXTERNAL.get(obj, obj)
    return obj


def to_internal_config_slots(obj):
    """저장 JSON(바깥 이름)을 파이썬 이름으로 보일 때 — 설정 dict 자리(`apply`)의 키와 설정 이름 자리(`config_key`)의 값만
    되돌린 깊은 사본. 통째 역변환은 하지 않는다(위반 유형·규칙 id 도 옛 이름을 쓴다). 에이전트 도구가 작업 결과를 보일 때 쓴다."""
    if isinstance(obj, Mapping):
        out: dict = {}
        for key, value in obj.items():
            if key == "apply" and isinstance(value, Mapping):
                try:
                    out[key] = to_internal(value)
                except ValueError:  # 저장본에 두 이름이 다른 값으로 섞였으면 보이는 그대로 둔다(보기용)
                    out[key] = value
            elif key == "config_key" and isinstance(value, str):
                out[key] = internal_key(value)
            else:
                out[key] = to_internal_config_slots(value)
        return out
    if isinstance(obj, (list, tuple)):
        return [to_internal_config_slots(v) for v in obj]
    return obj


def check_engine_config_keys(config, *, where: str):
    """엔진 입구 검사 — 엔진 입력 dict 에 바깥 이름 키가 남았는지 본다.

    strict: 하나라도 있으면 `OldConfigNameError`. 운영: `to_internal` 로 옮겨 돌려주고(두 이름이 다른 값이면
    `ValueError`) 이름별로 한 번 경고한다. 바깥 이름이 없으면 받은 객체를 그대로 돌려준다."""
    if not isinstance(config, Mapping):
        return config
    old = [k for k in config if k in EXTERNAL_TO_INTERNAL]
    if not old:
        return config
    for name in old:
        report_old_name(name, where)
    return to_internal(config)


class OldNameDescriptor:
    """옛 이름 호환 심 — 인스턴스에서 옛 이름으로 읽고 쓰면 새 이름 속성으로 잇는다(strict 면 예외).

    ORM 은 `synonym(새 이름, descriptor=OldNameDescriptor(옛 이름))` 으로 달아 클래스 수준 질의
    (`RosterConfig.off_first == …`)도 새 컬럼으로 이어지게 한다. 엔진 dataclass 는 클래스 속성으로 단다."""

    def __init__(self, old: str):
        self.old = old
        self.new = EXTERNAL_TO_INTERNAL[old]

    def __get__(self, obj, owner=None):
        if obj is None:
            return self
        report_old_name(self.old, f"{type(obj).__name__}.{self.old} 읽기")
        return getattr(obj, self.new)

    def __set__(self, obj, value) -> None:
        report_old_name(self.old, f"{type(obj).__name__}.{self.old} 쓰기")
        setattr(obj, self.new, value)


__all__ = [
    "EXTERNAL_TO_INTERNAL",
    "INTERNAL_TO_EXTERNAL",
    "STRICT_ENV",
    "OldConfigNameError",
    "OldNameDescriptor",
    "check_engine_config_keys",
    "external_key",
    "internal_key",
    "report_old_name",
    "strict",
    "to_external",
    "to_external_deep",
    "to_internal",
    "to_internal_config_slots",
]
