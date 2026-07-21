"""Langfuse 관측 — 키 게이팅 no-op + 활성 시 trace/generation/span/score 방출.

실제 Langfuse 서버 없이 fake 클라이언트로 검증. 키 없으면 완전 no-op(오버헤드 0) 확인.
"""
import pytest

import agents_v2.observability as obs


@pytest.fixture(autouse=True)
def _reset_obs_state():
    """모듈 전역(_client/_init_done) 이 다른 테스트로 새지 않게 각 테스트 후 복원."""
    yield
    obs._client = None
    obs._init_done = False


# ── fake Langfuse SDK ──
class _FakeSpan:
    def __init__(self, sink, kind, kw): sink.append((kind, kw)); self.ended = False
    def end(self): self.ended = True


class _FakeTrace:
    def __init__(self, sink, kw):
        self.sink = sink; self.kw = kw; self.output = None
    def generation(self, **kw): return _FakeSpan(self.sink, "generation", kw)
    def span(self, **kw): return _FakeSpan(self.sink, "span", kw)
    def score(self, **kw): self.sink.append(("score", kw)); return None
    def update(self, **kw): self.output = kw.get("output")


class _FakeClient:
    def __init__(self): self.sink = []; self.flushed = 0; self.traces = []
    def trace(self, **kw):
        t = _FakeTrace(self.sink, kw); self.traces.append(t); return t
    def flush(self): self.flushed += 1


class _Resp:
    def __init__(self, text, model="gpt-x", it=10, ot=5):
        self.type = "text"; self.text = text; self.tool_calls = []
        self.model = model; self.input_tokens = it; self.output_tokens = ot


class _LLM:
    def chat(self, messages, tools, **kw): return _Resp("답변")


def _reset(client):
    obs._client = client
    obs._init_done = True


# ── 키 없음 → 완전 no-op ──
def test_disabled_is_noop(monkeypatch):
    _reset(None)
    assert obs.enabled() is False
    llm = _LLM()
    wrapped = obs.wrap_client(llm)
    assert wrapped is llm  # 래핑조차 안 함
    with obs.turn(conversation_id="c", user_id="u", group_id="g", user_message="hi") as t:
        assert t is None
        obs.score("outcome", "OK")  # 조용히 무시
        obs.finish_turn("답", [])


# ── 활성 → trace + generation + span + score + flush ──
def test_enabled_emits_full_trace():
    fake = _FakeClient()
    _reset(fake)
    assert obs.enabled() is True
    llm = obs.wrap_client(_LLM())
    assert getattr(llm, "_lf_wrapped", False) is True

    with obs.turn(conversation_id="c1", user_id="n1", group_id="G1", user_message="8월 보여줘"):
        with obs.purpose("router"):
            llm.chat([{"role": "user", "content": "x"}], [])  # generation(name=router)
        llm.chat([{"role": "user", "content": "y"}], [])       # generation(name=llm)
        obs.finish_turn("최종답변",
                        [type("S", (), {"to_dict": lambda self: {"name": "routing"}})()],
                        score_name="outcome", score_value="OK")

    kinds = [k for k, _ in fake.sink]
    assert kinds.count("generation") == 2
    gen_names = [kw.get("name") for k, kw in fake.sink if k == "generation"]
    assert "router" in gen_names and "llm" in gen_names
    assert "span" in kinds and "score" in kinds
    # trace 출력·flush
    assert fake.traces[0].output == "최종답변"
    assert fake.flushed == 1
    # score 값
    sc = [kw for k, kw in fake.sink if k == "score"][0]
    assert sc["name"] == "outcome" and sc["value"] == "OK"


# ── trace 밖 LLM 호출은 generation 안 만듦 ──
def test_llm_call_outside_turn_no_generation():
    fake = _FakeClient()
    _reset(fake)
    llm = obs.wrap_client(_LLM())
    llm.chat([{"role": "user", "content": "x"}], [])  # 턴 밖
    assert fake.sink == []


# ── generation 실패해도 턴 안 깨짐(응답은 반환) ──
def test_generation_failure_never_breaks():
    class _BadTrace(_FakeTrace):
        def generation(self, **kw): raise RuntimeError("langfuse 다운")
    fake = _FakeClient()
    fake.trace = lambda **kw: _BadTrace(fake.sink, kw)  # type: ignore
    _reset(fake)
    llm = obs.wrap_client(_LLM())
    with obs.turn(conversation_id="c", user_id="u", group_id="g", user_message="hi"):
        r = llm.chat([{"role": "user", "content": "x"}], [])
    assert r.text == "답변"  # 예외 삼켜지고 정상 응답
