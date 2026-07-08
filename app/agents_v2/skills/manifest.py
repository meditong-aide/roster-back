"""Skill manifest — 스킬의 모든 '선언'을 한 곳에서.

동기: 스킬 하나를 추가하려면 지금까지 최대 5곳(descriptions 스키마 · registry
@register · router CATEGORY_TOOLS · middleware 권한 · grounding)을 손으로 맞춰야 했다.
백엔드 기능이 늘 때마다 이 세금을 반복해서 냈다.

해법: `@skill(...)` 데코레이터 하나로 스킬의 모든 성질(스키마·카테고리·mutation·hn_only·
grounds)을 선언하면, 흩어진 소비 지점들이 이 매니페스트에서 **파생(derive)**한다.

- 핸들러는 기존 SKILL_REGISTRY 에 그대로 등록(run_skill 무변경).
- 스키마는 manifest_tools() → descriptions.SKILL_TOOLS 에 병합.
- 카테고리는 manifest_category_tools() → router.CATEGORY_TOOLS 에 병합.
- 권한은 manifest_mutation_skills()/manifest_hn_only_skills() → middleware 가 소비.

기존 17개 스킬은 @register 그대로 둔다(무위험). 신규 스킬만 @skill 로 등록하면
위 5곳이 자동 구성된다. client_actions 의 dump→프론트 브릿지와 같은 '단일 소스 →
파생' 패턴을 내부에도 적용한 것.

ADR: docs/adr/0003-agent-orchestration-contracts.md (계약화 흐름)
"""

from __future__ import annotations

import importlib
from dataclasses import dataclass, field
from typing import Any, Callable

from sqlalchemy.orm import Session

SkillFunc = Callable[[Session, dict], Any]


@dataclass(frozen=True)
class SkillSpec:
    """한 스킬의 단일 소스 선언."""

    name: str
    schema: dict                        # LLM function-calling 스키마 (descriptions 로)
    handler: SkillFunc                  # run_skill 이 부를 구현
    categories: tuple[str, ...] = ()    # router 카테고리 배선
    mutation: bool = False              # 쓰기 스킬 여부(권한 게이트)
    hn_only: bool = False               # HN/ADM 전용 여부(권한 게이트)
    grounds: tuple[str, ...] = ()        # 필요한 grounding 필드(문서/검증용)
    # trigger_hint: 이 스킬의 category 를 발화에서 알아채도록 라우터 분류 프롬프트에
    # 주입할 자연어 트리거. category→tool 배선만 자동화하면 라우터가 "이 발화가 그
    # category 인지"를 모른다(신규 스킬 어휘 미학습) → tool 이 조용히 미검색된다.
    # 이 필드가 그 vocabulary 갭을 메운다.
    trigger_hint: str = ""
    # postcondition: 실행 성공 조건(5요소의 '검증' 게이트). 스킬이 error 없이 반환해도
    # 이 predicate 가 False 면 VERIFICATION_FAILED 로 승격(silent 부분실패 차단).
    # None 이면 검증 스킵. middleware.execute_skill 가 소비.
    postcondition: Callable[[Any], bool] | None = None


# name → SkillSpec. @skill 이 채운다.
SKILL_SPECS: dict[str, SkillSpec] = {}


def skill(
    name: str,
    schema: dict,
    *,
    categories: tuple[str, ...] | list[str] = (),
    mutation: bool = False,
    hn_only: bool = False,
    grounds: tuple[str, ...] | list[str] = (),
    trigger_hint: str = "",
    postcondition: Callable[[Any], bool] | None = None,
) -> Callable[[SkillFunc], SkillFunc]:
    """스킬 단일 소스 등록 데코레이터.

    핸들러를 기존 SKILL_REGISTRY 에 등록하고(run_skill 재사용), SkillSpec 을
    매니페스트에 기록해 descriptions/router/middleware 가 파생하도록 한다.

    schema["name"] 은 반드시 name 과 일치해야 한다(정합 불변식).
    """
    if schema.get("name") != name:
        raise ValueError(
            f"skill schema name mismatch: decorator name={name!r} != schema name={schema.get('name')!r}"
        )

    def decorator(func: SkillFunc) -> SkillFunc:
        spec = SkillSpec(
            name=name,
            schema=schema,
            handler=func,
            categories=tuple(categories),
            mutation=mutation,
            hn_only=hn_only,
            grounds=tuple(grounds),
            trigger_hint=trigger_hint,
            postcondition=postcondition,
        )
        SKILL_SPECS[name] = spec
        # 기존 dispatch 재사용 — run_skill 이 그대로 찾는다.
        from agents_v2.skills.registry import SKILL_REGISTRY

        SKILL_REGISTRY[name] = func
        return func

    return decorator


# ── 파생(derive) — 소비 지점들이 부른다 ──────────────────────────


def manifest_tools() -> list[dict]:
    """매니페스트 스킬들의 LLM 스키마 목록 (descriptions.SKILL_TOOLS 로 병합)."""
    return [spec.schema for spec in SKILL_SPECS.values()]


def manifest_category_tools() -> dict[str, list[str]]:
    """카테고리 → 매니페스트 스킬 이름 목록 (router.CATEGORY_TOOLS 로 병합)."""
    out: dict[str, list[str]] = {}
    for spec in SKILL_SPECS.values():
        for cat in spec.categories:
            out.setdefault(cat, []).append(spec.name)
    return out


def manifest_mutation_skills() -> set[str]:
    """쓰기 스킬 이름 집합 (middleware._MUTATION_SKILLS 로 병합)."""
    return {s.name for s in SKILL_SPECS.values() if s.mutation}


def manifest_hn_only_skills() -> set[str]:
    """HN/ADM 전용 스킬 이름 집합 (middleware 권한 게이트가 소비)."""
    return {s.name for s in SKILL_SPECS.values() if s.hn_only}


def manifest_router_hints() -> str:
    """신규 스킬의 trigger_hint 를 라우터 분류 프롬프트에 붙일 텍스트로 파생.

    category→tool 배선(manifest_category_tools)만으로는 라우터가 "이 발화가 그 category
    인지"를 모른다. trigger_hint 를 분류 프롬프트에 주입해 신규 스킬의 어휘를 라우터가
    학습하게 한다(vocabulary propagation). trigger_hint 가 없으면 빈 문자열.
    """
    lines = [
        f"- {s.trigger_hint} → {', '.join(s.categories)}"
        for s in SKILL_SPECS.values()
        if s.trigger_hint and s.categories
    ]
    if not lines:
        return ""
    return (
        "\n\n[신규 스킬 트리거 — 아래 표현은 지정된 카테고리로 분류하라]\n"
        + "\n".join(lines)
    )


# ── 매니페스트 스킬 모듈 로딩 ────────────────────────────────────
# @skill 로 등록하는 신규 스킬 모듈 경로. 여기 추가하면 5곳이 자동 구성된다.
_MANIFEST_MODULES: list[str] = [
    "agents_v2.skills.manage_assignment",
]

_loaded = False


def load_manifest_skills() -> None:
    """매니페스트 기반 스킬 모듈을 import 해 @skill 등록을 트리거(idempotent)."""
    global _loaded
    if _loaded:
        return
    _loaded = True
    for mod in _MANIFEST_MODULES:
        importlib.import_module(mod)
