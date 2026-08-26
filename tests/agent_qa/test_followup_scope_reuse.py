"""후속 턴 라우터 스코프 재사용 (D1).

배경(실측): 캡처된 703턴 중 108턴(15%)이 "아무거나 / 그래 / 1" 같은 후속 발화였고,
라우터가 단독 분류에 실패해 fallback → tool 29개 전부를 프롬프트에 실었다. 라우터
LLM 호출은 호출대로 쓰면서 스코핑 이득은 0인 구간.

수정: 직전 턴이 사용자에게 되물었으면(awaited_reply) 이번 발화는 그 답이므로 직전
스코프를 그대로 재사용하고 라우터를 아예 부르지 않는다.

검증 축:
  ① 되물은 다음 턴은 라우터 호출 0회 + 직전 스코프 유지
  ② 안 되물은 다음 턴은 평소대로 라우팅(안전 방향)
  ③ ReAct 경로가 clarification 을 결과에 싣는가(그 전엔 DAG 만 실어 비대칭)
  ④ 예약키가 스킬 파라미터로 새지 않는가
"""
from agents_v2.conversation import (
    _LAST_ROUTE_VM_KEY,
    _PENDING_APPROVAL_VM_KEY,
    _split_reserved,
)
from agents_v2.errors import ErrorType, classify


# ── ③ ReAct clarification 전파 ─────────────────────────


def test_react_result_carries_clarification():
    """스킬이 needs_clarification 을 내면 턴 결과에도 실려야 다음 턴이 알 수 있다."""
    from agents_v2.agent_v3 import AgentResult

    r = AgentResult(answer="어느 병동인가요?", needs_clarification=True)
    assert r.to_dict()["needs_clarification"] is True

    r2 = AgentResult(answer="조회 결과입니다")
    assert "needs_clarification" not in r2.to_dict()


def test_clarification_outcome_classification():
    """CLARIFICATION 판별이 재사용 트리거의 근거 — 분류가 흔들리면 안 된다."""
    assert classify({"needs_clarification": True, "question": "어느 병동?"}) is ErrorType.CLARIFICATION
    assert classify({"rows": []}) is not ErrorType.CLARIFICATION


# ── ④ 예약키 격리 ───────────────────────────────────────


def test_reserved_keys_split_out_of_variable_memory():
    """last_route 는 vm_json 에 얹혀 살지만 호출자 vm 으로 새면 스킬 파라미터를 오염시킨다."""
    vm = {
        "nurse_ids": ["N001"],
        _PENDING_APPROVAL_VM_KEY: {"type": "plan"},
        _LAST_ROUTE_VM_KEY: {"categories": ["read"], "tool_names": ["query_schedule"]},
    }
    clean, pending, last_route = _split_reserved(vm)

    assert clean == {"nurse_ids": ["N001"]}
    assert pending == {"type": "plan"}
    assert last_route["categories"] == ["read"]


def test_split_reserved_empty():
    assert _split_reserved({}) == ({}, None, None)


# ── ①② 재사용 트리거 ───────────────────────────────────


def _ctx_with(last_route, awaited):
    from agents_v2.schemas.session_context import SessionContext

    ctx = SessionContext(office_id="OFF001", group_id="GRP001", year=2026, month=8,
                         nurse_id="N001", nurse_name="김민지", user_role="HN")
    ctx.last_route = last_route
    ctx.awaited_reply = awaited
    return ctx


def test_reuse_condition_requires_both_signals():
    """재사용은 (직전 스코프 있음) AND (직전 턴이 되물었음) 둘 다일 때만."""
    scope = {"categories": ["settings_people"], "tool_names": ["update_person_attr"]}

    assert _should_reuse(_ctx_with(scope, True)) is True
    assert _should_reuse(_ctx_with(scope, False)) is False, "안 되물었으면 정상 라우팅"
    assert _should_reuse(_ctx_with(None, True)) is False, "직전 스코프 없으면 정상 라우팅"
    assert _should_reuse(_ctx_with({"tool_names": []}, True)) is False, "빈 스코프는 재사용 불가"


def _should_reuse(ctx) -> bool:
    """agent_v3 의 재사용 게이트와 동일 조건(문서화 + 회귀 고정)."""
    return bool(
        not ctx.pending_approval
        and getattr(ctx, "awaited_reply", False)
        and (getattr(ctx, "last_route", None) or {}).get("tool_names")
    )


def test_pending_approval_still_wins():
    """승인 대기 턴은 원래도 라우터를 건너뛴다 — 재사용 경로가 이를 가로채면 안 된다."""
    scope = {"categories": ["read"], "tool_names": ["query_schedule"]}
    ctx = _ctx_with(scope, True)
    ctx.pending_approval = {"type": "plan"}
    assert _should_reuse(ctx) is False


# ── 스토어 왕복 ─────────────────────────────────────────


def test_last_route_survives_variable_memory_save(db, seed_data, monkeypatch):
    """save_variable_memory 가 예약키를 지우면 재사용이 조용히 죽는다(회귀 방지)."""
    from agents_v2.conversation import conversation_store as store

    conv = store.create(db, user_id="N001", group_id="GRP001")
    scope = {"categories": ["settings_rules"], "tool_names": ["update_constraint"],
             "awaited_reply": True}
    store.set_last_route(db, conv.id, scope, user_id="N001", group_id="GRP001")

    # 그 뒤 평범한 vm 저장이 일어나도 last_route 는 살아남아야 한다.
    store.save_variable_memory(db, conv.id, {"nurse_ids": ["N002"]},
                               user_id="N001", group_id="GRP001")

    got = store.get(db, conv.id, user_id="N001", group_id="GRP001")
    assert got is not None
    assert got.last_route == scope
    assert got.variable_memory == {"nurse_ids": ["N002"]}
    assert _LAST_ROUTE_VM_KEY not in got.variable_memory


def test_set_last_route_none_clears(db, seed_data):
    from agents_v2.conversation import conversation_store as store

    conv = store.create(db, user_id="N001", group_id="GRP001")
    store.set_last_route(db, conv.id, {"tool_names": ["query_schedule"]},
                         user_id="N001", group_id="GRP001")
    store.set_last_route(db, conv.id, None, user_id="N001", group_id="GRP001")

    got = store.get(db, conv.id, user_id="N001", group_id="GRP001")
    assert got.last_route is None


def test_last_route_and_pending_approval_coexist(db, seed_data):
    """둘은 같은 vm_json 에 얹혀 사는 독립 예약키 — 한쪽 저장이 다른 쪽을 지우면 안 된다."""
    from agents_v2.conversation import conversation_store as store

    conv = store.create(db, user_id="N001", group_id="GRP001")
    store.set_last_route(db, conv.id, {"tool_names": ["query_schedule"]},
                         user_id="N001", group_id="GRP001")
    store.set_pending_approval(db, conv.id, {"type": "plan"},
                               user_id="N001", group_id="GRP001")

    got = store.get(db, conv.id, user_id="N001", group_id="GRP001")
    assert got.last_route == {"tool_names": ["query_schedule"]}
    assert got.pending_approval == {"type": "plan"}


# ── 실제 경로 검증 (게이트 로직 복사가 아니라 agent.run 을 태운다) ──
# 위의 _should_reuse 는 조건을 문서화한 것이라 그것만으로는 증거가 약하다.
# 아래는 SchedulingAgent 를 router_llm 과 함께 돌려 **라우터 호출 횟수**를 직접 센다.


class _CountingRouter:
    """분류를 고정 반환하면서 호출 횟수를 세는 라우터 LLM 스텁."""

    def __init__(self, categories):
        self.calls = 0
        self._payload = str(list(categories)).replace("'", '"')

    def chat(self, messages, tools=None, **kw):
        from agents_v2.llm_client import LLMResponse

        self.calls += 1
        return LLMResponse(type="text", text=self._payload)


class _OneShotMain:
    """메인 LLM: 첫 턴은 텍스트 답변, 이후에도 텍스트. tools 스냅샷을 기록."""

    def __init__(self):
        self.seen_tool_counts = []

    def chat(self, messages, tools=None, *, tool_choice="auto", **kw):
        from agents_v2.llm_client import LLMResponse

        self.seen_tool_counts.append(len(tools or []))
        return LLMResponse(type="text", text="확인했습니다.")


def _agent(main, router):
    from agents_v2.agent_v3 import SchedulingAgent

    return SchedulingAgent(main, enable_user_memory=False, router_llm=router)


def test_followup_turn_skips_router_call(db, seed_data):
    """직전 턴이 되물었으면 라우터를 아예 안 부른다 — 호출 수로 증명."""
    from agents_v2.skills.descriptions import SKILL_TOOLS

    main, router = _OneShotMain(), _CountingRouter(["read"])
    agent = _agent(main, router)
    ctx = _ctx_with(None, False)

    agent.run(db, "8월 근무표에서 김민지 나이트 몇 번이야", ctx)
    assert router.calls == 1
    assert ctx.last_route and ctx.last_route["categories"] == ["read"]
    scoped_n = main.seen_tool_counts[-1]
    assert 0 < scoped_n < len(SKILL_TOOLS), "1턴은 스코핑돼야"

    # 직전 턴이 되물었다고 표시된 상태에서 짧은 후속 발화
    ctx.awaited_reply = True
    agent.run(db, "아무거나", ctx)

    assert router.calls == 1, "후속 턴에서 라우터가 또 불렸다"
    assert main.seen_tool_counts[-1] == scoped_n, "직전 스코프가 유지돼야(전체 tool 아님)"


def test_non_followup_turn_still_routes(db, seed_data):
    """안 되물었으면 평소대로 라우팅한다 — 안전 방향."""
    main, router = _OneShotMain(), _CountingRouter(["read"])
    agent = _agent(main, router)
    ctx = _ctx_with(None, False)

    agent.run(db, "8월 근무표 보여줘", ctx)
    agent.run(db, "9월은?", ctx)  # awaited_reply 안 켬

    assert router.calls == 2


def test_reuse_emits_trace_stage(db, seed_data):
    """재사용은 추적 가능해야 한다(파이프라인 투명성)."""
    main, router = _OneShotMain(), _CountingRouter(["settings_people"])
    agent = _agent(main, router)
    ctx = _ctx_with(None, False)

    agent.run(db, "김민지 등급 알려줘", ctx)
    ctx.awaited_reply = True
    res = agent.run(db, "그래", ctx)

    names = [s.name for s in (res.trace or [])]
    assert "router_reuse" in names
    assert "routing" not in names


def test_fallback_scope_not_remembered(db, seed_data):
    """분류 실패(fallback=전체 tool) 스코프는 기억하지 않는다 — 재사용 값어치 없음."""
    main, router = _OneShotMain(), _CountingRouter([])  # 빈 분류 → fallback
    agent = _agent(main, router)
    ctx = _ctx_with(None, False)

    agent.run(db, "어쩌구", ctx)
    assert ctx.last_route is None
