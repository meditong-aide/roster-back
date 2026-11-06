"""에이전트 프로필(허용 목록) — AIDE 와 파생 에이전트가 같은 도구·실행 틀을 쓰되, 쓸 수 있는 범위를 프로필로 나눈다(S3 · 11-06).

- `aide` = 지금 AIDE 그대로. 태그 없는 옛 `@register` 스킬과 `agents` 에 "aide" 가 든 매니페스트 스킬 전부.
- `schedule-edit` = 근무표 수정 도우미. 매니페스트에서 `agents` 에 "schedule-edit" 가 든 스킬만. 근무표 수정 밖의 AIDE 도구
  (조회·일괄 변경·생성·화면 이동 등)는 이 프로필에서 열지 않는다 — 나중에 AIDE 와 합칠 때 프로필을 넓혀서 연다.

`middleware.execute_skill` 의 ⓪ 범위 단계(`middleware._check_scope`)가 소비한다: 모르는 에이전트 거절 · 프로필 밖 스킬 거절 ·
쓰기 스킬은 `write_sources` 에서 온 호출만(수정 도우미 적용은 버튼으로만).
나머지 칸(LLM 호출 수·라우터·계획기·기억·L2·미리보기 요약·승인 방식·읽기 병렬)은 **아직 읽는 곳이 없는 선언**이다 — 각 칸 옆에
그 칸을 읽을 단계(S6·S8·S9)를 적었다. 그 단계가 들어오기 전에는 값을 바꿔도 동작이 바뀌지 않는다.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

AIDE = "aide"
SCHEDULE_EDIT = "schedule-edit"


@dataclass(frozen=True)
class AgentProfile:
    id: str
    title: str
    #: 쓰기 스킬을 받는 호출 출처(SessionContext.source) — 비우면 제한 없음(⓪ 범위 단계 · S3)
    write_sources: tuple[str, ...] = ()
    #: LLM 도구 선택 강제 — auto · required(말 턴은 반드시 도구 하나)
    tool_choice: str = "auto"
    #: 한 턴의 LLM 호출 상한 — None 이면 제한 없음(S8 불변식 시험이 센다)
    max_llm_calls: Optional[int] = None
    #: AIDE 라우터(카테고리 분류 LLM)를 쓰나(S6 H4)
    router: bool = True
    #: 계획 — llm(AIDE 계획기) · llm_when_mixed(다른 에이전트 도구가 섞일 때만) · code(코드 계획만)(S8)
    planner: str = "llm"
    #: 기억 추출·L2 검증·미리보기 요약 LLM(S8)
    memory: bool = True
    l2: bool = True
    preview_summary: bool = True
    #: 승인 — text(글자 승인 뒤 다시 실행 · AIDE) · button(버튼으로만, 다시 풀지 않음 · S9)
    approval: str = "text"
    #: 읽기 노드 병렬 실행 — 수정 도우미는 턴·DB 세션을 스레드끼리 나눠 쓰면 안 되어 끔(S8)
    parallel_reads: bool = True


PROFILES: dict[str, AgentProfile] = {
    AIDE: AgentProfile(AIDE, "AIDE"),
    SCHEDULE_EDIT: AgentProfile(
        SCHEDULE_EDIT, "근무표 수정 도우미",
        write_sources=("button",), tool_choice="required", max_llm_calls=1,
        router=False, planner="code", memory=False, l2=False, preview_summary=False,
        approval="button", parallel_reads=False,
    ),
}


def profile_of(agent_id: Optional[str]) -> Optional[AgentProfile]:
    """에이전트 id → 프로필. 모르는 id 면 None(⓪ 범위 단계가 거절한다)."""
    return PROFILES.get(agent_id or "")


def in_scope(agent_id: str, skill_name: str) -> bool:
    """그 에이전트가 이 스킬을 쓸 수 있나.

    aide: 파생 에이전트 전용 스킬만 빼고 전부(모르는 이름은 여기서 막지 않고 실행 단계가 '모르는 스킬'로 답한다 — 지금 동작 그대로).
    파생 에이전트: 매니페스트에서 자기 id 가 든 스킬만.
    """
    from agents_v2.skills.manifest import find_spec, load_manifest_skills

    load_manifest_skills()
    spec = find_spec(skill_name)
    if agent_id == AIDE:
        return spec is None or AIDE in spec.agents
    return spec is not None and agent_id in spec.agents


def profile_tools(agent_id: Optional[str]) -> list[dict]:
    """그 에이전트의 LLM 에 보일 도구 스키마 목록(H4). aide 는 지금 descriptions.SKILL_TOOLS 그대로,
    파생 에이전트는 매니페스트에서 자기 id 가 든 LLM 노출 스킬만. 모르는 에이전트는 빈 목록(전체 도구로 새지 않는다)."""
    profile = profile_of(agent_id)
    if profile is None:
        return []
    if profile.id == AIDE:
        from agents_v2.skills.descriptions import SKILL_TOOLS

        return SKILL_TOOLS
    from agents_v2.skills.manifest import load_manifest_skills, manifest_tools

    load_manifest_skills()
    return manifest_tools(profile.id)


def profile_tool_names(agent_id: Optional[str]) -> list[str]:
    """profile_tools 의 이름 목록(정의 순서 보존) — 라우터 분류 실패 폴백이 쓴다."""
    return [t["name"] for t in profile_tools(agent_id)]
