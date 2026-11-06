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
from dataclasses import dataclass
from typing import Any, Callable

from sqlalchemy.orm import Session

SkillFunc = Callable[[Session, dict], Any]

#: AIDE 본 에이전트(프로필 id). 태그 없는 스킬은 전부 여기 속한다.
AIDE = "aide"
MODES = ("read", "preview", "write")
COMMITS = ("rerun", "button")


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
    # auto_safe: autonomy_mode="auto" 에서 이 mutation 을 승인 없이 즉시 실행해도 되는가.
    # HITL 3-tier 의 auto/notify 티어. 기본 False(=항상 승인). '어느 변경이 안전한가'는
    # 위험한 결정이라 스킬별 명시 opt-in 으로만 켠다.
    auto_safe: bool = False
    # ── 파생 에이전트 칸(S2 · 11-06) — 한 매니페스트에 AIDE 와 파생 에이전트(근무표 수정 도우미 등) 스킬이 함께 산다 ──
    # agents: 이 스킬을 쓸 수 있는 에이전트(프로필 id). AIDE 도구 목록·라우터는 "aide" 가 든 스킬만 받는다.
    agents: tuple[str, ...] = (AIDE,)
    # mode: read · preview · write. 비우면 mutation 에서 정한다(write/read) — 계획 노드의 읽기/쓰기를
    # LLM 이 적은 값이 아니라 이것으로 정한다(H2).
    mode: str = ""
    # sources: 호출이 올 수 있는 곳(llm · button · example …). 비우면 제한 없음 — 파생 에이전트의 적용처럼
    # 버튼으로만 와야 하는 스킬이 좁힌다(⓪ 범위 단계가 소비 · S3).
    sources: tuple[str, ...] = ()
    # llm_visible: LLM 도구 목록에 보이나 — 버튼 전용 스킬은 False.
    llm_visible: bool = True
    # grounder: 스킬 자체 이름 확정 — grounder(turn, params) → (확정한 값 또는 None, 끝낼 결과 dict 또는 None).
    #   결과 dict 가 있으면(되물음 등) 그대로 돌려주고 끝낸다. 있으면 AIDE 공통 문맥 주입·grounding 을 건너뛴다(S4).
    grounder: Callable[..., Any] | None = None
    # context_first: 문맥 주입 전에 먼저 정할 것(수정 도우미의 고칠 첫날) — context_first(turn, params) → 끝낼 결과 dict 또는 None. S4.
    context_first: Callable[..., Any] | None = None
    # commit: 승인 뒤 커밋 방식 — rerun(미리보기 뒤 다시 실행 · AIDE) · button(버튼으로만, 다시 풀지 않음 · H3·S9).
    commit: str = "rerun"

    @property
    def effective_mode(self) -> str:
        return self.mode or ("write" if self.mutation else "read")


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
    auto_safe: bool = False,
    agents: tuple[str, ...] | list[str] = (AIDE,),
    mode: str = "",
    sources: tuple[str, ...] | list[str] = (),
    llm_visible: bool = True,
    grounder: Callable[..., Any] | None = None,
    context_first: Callable[..., Any] | None = None,
    commit: str = "rerun",
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
    if not agents:
        raise ValueError(f"skill {name!r}: agents 가 비었다 — 어느 에이전트도 쓸 수 없는 스킬")
    if mode and mode not in MODES:
        raise ValueError(f"skill {name!r}: mode={mode!r} 는 {MODES} 중 하나여야 한다")
    if commit not in COMMITS:
        raise ValueError(f"skill {name!r}: commit={commit!r} 는 {COMMITS} 중 하나여야 한다")

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
            auto_safe=auto_safe,
            agents=tuple(agents),
            mode=mode,
            sources=tuple(sources),
            llm_visible=llm_visible,
            grounder=grounder,
            context_first=context_first,
            commit=commit,
        )
        SKILL_SPECS[name] = spec
        # 기존 dispatch 재사용 — run_skill 이 그대로 찾는다.
        from agents_v2.skills.registry import SKILL_REGISTRY

        SKILL_REGISTRY[name] = func
        return func

    return decorator


# ── 파생(derive) — 소비 지점들이 부른다 ──────────────────────────


def manifest_specs(agent: str = AIDE) -> list[SkillSpec]:
    """그 에이전트가 쓸 수 있는 스펙 — 등록 순서대로."""
    return [spec for spec in SKILL_SPECS.values() if agent in spec.agents]


def manifest_tools(agent: str = AIDE) -> list[dict]:
    """그 에이전트의 LLM 에 보일 스키마 목록 (aide 면 descriptions.SKILL_TOOLS 로 병합).

    파생 에이전트 스킬은 AIDE 목록에 들어가지 않고, 버튼 전용(llm_visible=False)은 어느 LLM 에도 안 보인다."""
    return [spec.schema for spec in manifest_specs(agent) if spec.llm_visible]


def manifest_category_tools(agent: str = AIDE) -> dict[str, list[str]]:
    """카테고리 → 그 에이전트 스킬 이름 목록 (aide 면 router.CATEGORY_TOOLS 로 병합)."""
    out: dict[str, list[str]] = {}
    for spec in manifest_specs(agent):
        if not spec.llm_visible:
            continue
        for cat in spec.categories:
            out.setdefault(cat, []).append(spec.name)
    return out


def manifest_mutation_skills() -> set[str]:
    """쓰기 스킬 이름 집합 (middleware._MUTATION_SKILLS 로 병합)."""
    return {s.name for s in SKILL_SPECS.values() if s.mutation}


def manifest_hn_only_skills() -> set[str]:
    """HN/ADM 전용 스킬 이름 집합 (middleware 권한 게이트가 소비)."""
    return {s.name for s in SKILL_SPECS.values() if s.hn_only}


def find_spec(name: str) -> SkillSpec | None:
    """스킬 이름 → 스펙. `run_skill` 처럼 원래 이름·하이픈·밑줄 순서로 찾는다(어느 쪽으로 등록했든)."""
    for candidate in (name, name.replace("_", "-"), name.replace("-", "_")):
        spec = SKILL_SPECS.get(candidate)
        if spec is not None:
            return spec
    return None


def skill_mode(name: str) -> str | None:
    """스킬 이름(하이픈·밑줄 무관) → read·preview·write. 매니페스트에 없으면 None(옛 @register 스킬)."""
    spec = find_spec(name)
    return spec.effective_mode if spec is not None else None


def manifest_router_hints(agent: str = AIDE) -> str:
    """신규 스킬의 trigger_hint 를 라우터 분류 프롬프트에 붙일 텍스트로 파생.

    category→tool 배선(manifest_category_tools)만으로는 라우터가 "이 발화가 그 category
    인지"를 모른다. trigger_hint 를 분류 프롬프트에 주입해 신규 스킬의 어휘를 라우터가
    학습하게 한다(vocabulary propagation). trigger_hint 가 없으면 빈 문자열.
    파생 에이전트 스킬의 힌트는 AIDE 라우터에 들어가지 않는다.
    """
    lines = [
        f"- {s.trigger_hint} → {', '.join(s.categories)}"
        for s in manifest_specs(agent)
        if s.trigger_hint and s.categories and s.llm_visible
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
    "agents_v2.skills.lookup_guide",
    "agents_v2.skills.manage_daily_shift",
    "agents_v2.skills.manage_leave_targets",
    "agents_v2.skills.manage_banned_wanted",
    # 파생 에이전트(근무표 수정 도우미) 스킬 — 등록은 그쪽 모듈이 이 파일을 import 해서 한다(층 분리 · S4)
    "schedule_edit_agent.aide_skills",
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
