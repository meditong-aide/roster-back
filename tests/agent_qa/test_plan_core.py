"""DAG plan 결정적 코어 — 위상정렬 + $tN 참조 치환 (LLM 무관)."""
import pytest
from agents_v2.planning.plan import Plan, PlanTask, resolve_refs, resolve_args


def _p(*tasks):
    return Plan(tasks=list(tasks))


def test_topo_levels_linear():
    p = _p(PlanTask("t2", "s", deps=["t1"]), PlanTask("t1", "s"))
    levels = p.topo_levels()
    assert [[t.id for t in lv] for lv in levels] == [["t1"], ["t2"]]


def test_topo_levels_parallel():
    # t1,t2 독립 → 같은 레벨, t3 는 둘 다 의존
    p = _p(PlanTask("t1", "s"), PlanTask("t2", "s"),
           PlanTask("t3", "s", deps=["t1", "t2"]))
    levels = [sorted(t.id for t in lv) for lv in p.topo_levels()]
    assert levels == [["t1", "t2"], ["t3"]]


def test_validate_cycle():
    p = _p(PlanTask("t1", "s", deps=["t2"]), PlanTask("t2", "s", deps=["t1"]))
    with pytest.raises(ValueError):
        p.validate()


def test_validate_missing_dep():
    p = _p(PlanTask("t1", "s", deps=["tX"]))
    with pytest.raises(ValueError):
        p.validate()


def test_resolve_ref_simple():
    outputs = {"t1": {"candidates": [{"name": "김민지"}, {"name": "박지은"}]}}
    assert resolve_refs("$t1.candidates[0].name", outputs) == "김민지"
    assert resolve_refs("$t1.candidates[1].name", outputs) == "박지은"


def test_resolve_ref_whole_and_missing():
    outputs = {"t1": {"x": 3}}
    assert resolve_refs("$t1", outputs) == {"x": 3}
    assert resolve_refs("$t9.foo", outputs) is None          # 미존재 task
    assert resolve_refs("$t1.nope", outputs) is None          # 미존재 경로
    assert resolve_refs("그냥문자열", outputs) == "그냥문자열"  # 참조 아님


def test_resolve_args_nested():
    outputs = {"t1": {"candidates": [{"name": "김민지"}]}}
    args = {"nurse": "$t1.candidates[0].name", "date": "2026-05-03",
            "meta": {"from": "$t1.candidates[0].name"}}
    out = resolve_args(args, outputs)
    assert out == {"nurse": "김민지", "date": "2026-05-03", "meta": {"from": "김민지"}}
