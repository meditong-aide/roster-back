"""Router 단위테스트 — 매핑/fallback/multi-label/coverage/파싱.

LLM 호출은 fake 더블로 대체(결정론).
"""

from __future__ import annotations

from agents_v2.llm_client import LLMResponse
from agents_v2.router import (
    ALL_TOOL_NAMES,
    CATEGORY_TOOLS,
    classify,
    resolve_tools,
    route,
)


class _FakeLLM:
    """chat() 가 미리 정한 text/예외를 돌려주는 더블."""

    def __init__(self, text: str | None = None, *, raise_exc: bool = False,
                 as_tool_call: bool = False):
        self._text = text
        self._raise = raise_exc
        self._as_tool_call = as_tool_call

    def chat(self, messages, tools, *, tool_choice="auto"):  # noqa: ANN001
        if self._raise:
            raise RuntimeError("boom")
        if self._as_tool_call:
            return LLMResponse(type="tool_call", tool_calls=[])
        return LLMResponse(type="text", text=self._text or "")


# ── coverage ──

def test_all_tools_covered():
    """모든 tool 이 각각 최소 1개 카테고리에 등장."""
    covered = set()
    for tools in CATEGORY_TOOLS.values():
        covered.update(tools)
    # Wave-1 (2026-06-19): query_generation_job + manage_wanted_deadline → 16.
    # Wave-2 (2026-06-19): manage_teams + manage_wanted_limits → 18.
    # Wave-3 (2026-06-19): resolve_infeasibility → 19.
    # switch_ward client-action (2026-06-24) → 20.
    # invoke client-action (2026-07-07, 엑셀 등 비파괴 UI 명령) → 21.
    # manage_assignment (2026-07-08, 매니페스트 첫 시민, 파견) → 22.
    # lookup_guide (help-doc RAG) → 23.
    # manage_daily_shift (2026-07, 시프트별 필요인원=DailyShift) → 24.
    # publish_schedule (2026-07, 근무표 확정/발행) → 25.
    # manage_mutual_exclusion (2026-07, 두 간호사 상호배제) → 26.
    # log_feedback (2026-07, 처리불가 불만/건의/버그 접수 — triage 정책) → 27.
    assert len(ALL_TOOL_NAMES) == 27
    missing = set(ALL_TOOL_NAMES) - covered
    assert not missing, f"카테고리 미커버 tool: {missing}"
    # 맵의 tool 이름이 전부 실제 tool 이름인지(오타 방지)
    unknown = covered - set(ALL_TOOL_NAMES)
    assert not unknown, f"맵에 없는 tool 이름: {unknown}"


# ── client-action 스코핑 ──

def test_navigation_includes_client_actions():
    tools = resolve_tools(["navigation"])
    assert "navigate" in tools and "prefill" in tools


# ── gray-zone 번들 ──

def test_read_bundles_navigate():
    tools = resolve_tools(["read"])
    assert "query_schedule" in tools
    assert "navigate" in tools  # navigate-vs-read 미세결정을 메인 프롬프트에 위임


# ── multi-label ──

def test_multilabel_union():
    tools = set(resolve_tools(["mutate", "recommend"]))
    assert {"bulk_mutation", "recommend_candidates", "query_schedule"} <= tools


def test_scoped_subset_smaller_than_full():
    """스코핑이 실제로 후보를 줄인다(읽기 카테고리)."""
    assert len(resolve_tools(["read"])) < len(ALL_TOOL_NAMES)


def test_scoped_preserves_skill_tools_order():
    tools = resolve_tools(["settings_people"])
    idx = [ALL_TOOL_NAMES.index(t) for t in tools]
    assert idx == sorted(idx)


# ── fallback ──

def test_empty_categories_returns_full():
    assert resolve_tools([]) == ALL_TOOL_NAMES


def test_unknown_categories_returns_full():
    assert resolve_tools(["nonsense", "???"]) == ALL_TOOL_NAMES


def test_partial_unknown_keeps_known_only():
    tools = resolve_tools(["read", "nonsense"])
    assert tools == resolve_tools(["read"])


# ── classify 파싱 ──

def test_classify_parses_json_array():
    assert classify(_FakeLLM('["read"]'), "5월 근무표 보여줘") == ["read"]


def test_classify_strips_code_fence():
    llm = _FakeLLM('```json\n["mutate","recommend"]\n```')
    assert classify(llm, "x") == ["mutate", "recommend"]


def test_classify_filters_unknown_categories():
    assert classify(_FakeLLM('["read","bogus"]'), "x") == ["read"]


def test_classify_handles_surrounding_text():
    assert classify(_FakeLLM('분류 결과: ["analyze"] 입니다'), "x") == ["analyze"]


def test_classify_empty_on_parse_failure():
    assert classify(_FakeLLM("도저히 모르겠음"), "x") == []


def test_classify_empty_on_tool_call_response():
    """DeterministicClient 처럼 tool_call 을 돌려주면 빈 list → fallback 유발."""
    assert classify(_FakeLLM(as_tool_call=True), "x") == []


def test_classify_empty_on_exception():
    assert classify(_FakeLLM(raise_exc=True), "x") == []


# ── route ──

def test_route_fallback_when_empty():
    res = route(_FakeLLM("[]"), "x")
    assert res.fallback_used is True
    assert res.tool_names == ALL_TOOL_NAMES


def test_route_scoped_when_classified():
    res = route(_FakeLLM('["navigation"]'), "팀 어디서 바꿔")
    assert res.fallback_used is False
    assert res.categories == ["navigation"]
    assert set(res.tool_names) == {"navigate", "prefill", "switch_ward", "invoke",
                                   "lookup_guide", "log_feedback"}


def test_route_exception_falls_back():
    res = route(_FakeLLM(raise_exc=True), "x")
    assert res.fallback_used is True
    assert res.tool_names == ALL_TOOL_NAMES


# ── 통합: agent_v3 가 router_llm 주입 시 실제로 scope 하는가 ──

class _CapturingLLM:
    """메인 루프가 받은 tools 를 기록하고 즉시 text 로 종료하는 더블."""

    def __init__(self):
        self.seen_tool_names: list[str] | None = None

    def chat(self, messages, tools, *, tool_choice="auto"):  # noqa: ANN001
        self.seen_tool_names = [t["name"] for t in tools]
        return LLMResponse(type="text", text="ok")


def _ctx():
    from agents_v2.schemas.session_context import SessionContext
    return SessionContext(
        office_id="OFF", group_id="GRP", year=2026, month=5,
        user_role="HN", nurse_id="n1", nurse_name="테스트", conversation_id="c1",
    )


def test_agent_scopes_tools_when_router_injected():
    from agents_v2.agent_v3 import SchedulingAgent

    main = _CapturingLLM()
    agent = SchedulingAgent(
        main, enable_user_memory=False, router_llm=_FakeLLM('["navigation"]'),
    )
    result = agent.run(None, "팀 어디서 바꿔", _ctx())

    # 메인 루프는 scoped tool 만 받았다 (SKILL_TOOLS 정의 순서)
    assert main.seen_tool_names == ["log_feedback", "navigate", "prefill", "switch_ward",
                                    "invoke", "lookup_guide"]
    # routing trace stage 가 있고 categories/ fallback 기록
    routing = [s for s in result.trace if s.name == "routing"]
    assert routing, "routing stage 없음"
    assert routing[0].data["categories"] == ["navigation"]
    assert routing[0].data["fallback_used"] is False


def test_agent_full_tools_when_router_fallback():
    from agents_v2.agent_v3 import SchedulingAgent

    main = _CapturingLLM()
    agent = SchedulingAgent(
        main, enable_user_memory=False, router_llm=_FakeLLM("모르겠음"),
    )
    result = agent.run(None, "아무거나", _ctx())

    assert main.seen_tool_names == ALL_TOOL_NAMES  # fallback = 전체
    routing = [s for s in result.trace if s.name == "routing"]
    assert routing and routing[0].data["fallback_used"] is True


def test_agent_no_routing_stage_when_router_disabled():
    """router_llm 미주입(기본) → 라우팅 스텝/스테이지 없음 + 전체 tool (기존 동작)."""
    from agents_v2.agent_v3 import SchedulingAgent

    main = _CapturingLLM()
    agent = SchedulingAgent(main, enable_user_memory=False)
    result = agent.run(None, "아무거나", _ctx())

    assert main.seen_tool_names == ALL_TOOL_NAMES
    assert not [s for s in result.trace if s.name == "routing"]
