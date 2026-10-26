"""별칭 → 정본 키 정규화(EI-12 · D9) — 레지스트리 `aliases` 에서 파생한다(이름 표를 두 벌 두지 않는다).

입력(요청 `config_override`)만 바꾼다. 출력(카드·처방·control 기록)은 처음부터 정본 키로 쓴다(C7).
하드락 판정(`policy.hard_lock`)은 별칭·정본 둘 다 보므로 이 정규화보다 **앞**에서 돈다 — 판정이 요청의 원래 이름을 본다.
"""

from __future__ import annotations

from collections.abc import Mapping

from .settings import SETTINGS


def _simple_alias_groups() -> list[tuple[str, tuple[str, ...]]]:
    """(정본 키, 별칭들) — 값 영역이 같은 단순 동의어만. 값 영역이 다른 파생 입력(`derived_inputs` 표지 — 예
    `max_consecutive_nights`)과 다른 표 컬럼 설정(정본 키에 '.' — 예 `roster_grade_config.constraints_json`)은 뺀다."""
    return [(spec.key, tuple(a for a in spec.aliases if a not in (spec.derived_inputs or {})))
            for spec in SETTINGS if "." not in spec.key and spec.aliases]


def normalize_aliases(config: Mapping | None) -> dict | None:
    """`config`(요청 `config_override` 등)의 단순 별칭 키를 정본 키로 바꾼 사본을 돌려준다.

    · 정본 키가 이미 있으면 정본이 이기고 별칭은 버린다(`effective.canonical_key` 와 같은 우선순위).
    · 정본이 없으면 별칭 중 레지스트리 순서가 앞선 것의 값을 정본 키로 옮긴다. 값이 None 이거나 숫자·불리언이 아닌 별칭
      (문자열·리스트·딕트)은 옮기지 않고 버린다(정본에 넣으면 엔진이 TypeError 로 500 — 별칭일 땐 지금까지 무시됐다).
    · 바꿀 것이 없으면 받은 객체를 그대로 돌려준다(None·비 Mapping 포함 — 호출부의 truthiness 판정을 건드리지 않는다)."""
    if not isinstance(config, Mapping):
        return config  # type: ignore[return-value]
    groups = [(canon, [a for a in aliases if a in config]) for canon, aliases in _simple_alias_groups()]
    groups = [(canon, present) for canon, present in groups if present]
    if not groups:
        return config  # type: ignore[return-value]
    out = dict(config)
    for canon, present in groups:
        value = next((out[a] for a in present if isinstance(out[a], (int, float))), None)
        for alias in present:
            del out[alias]
        if canon not in out and value is not None:
            out[canon] = value
    return out


__all__ = ["normalize_aliases"]
