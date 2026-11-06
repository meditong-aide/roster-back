"""S4(11-06) — 같은 스킬을 수정 도우미 미들웨어와 AIDE 실행 경로(`agents_v2.middleware.execute_skill`)로 돌려 같은 결과가 나오는지.

각 경우마다 새 대화·새 턴으로 두 경로를 따로 돌려 Outcome(answer·preview·effects·focus·failed)과 기록 이벤트(이름·payload —
단계 기록 steps 칸은 뺀다)를 견준다. 판정 재료·기록은 `world` fixture 가 가짜로 바꾼 것을 쓴다(실 DB·LLM 없음).
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from agents_v2.errors import ErrorType, classify
from agents_v2.middleware import execute_skill as aide_execute
from agents_v2.schemas.session_context import SessionContext
from agents_v2.skills.manifest import SKILL_SPECS, find_spec, load_manifest_skills
from agents_v2.turn_context import bind_turn, current_turn
from schedule_edit_agent import middleware as edit_mw
from schedule_edit_agent import skills as skills_mod
from schedule_edit_agent.aide_skills import from_aide_result
from schedule_edit_agent.descriptions import BUTTON_TOOLS, SKILL_MODES, SKILL_TOOLS
from schedule_edit_agent.skills import Outcome
from tests.edit_agent.test_agent import A_OFF, N_LESS_2, USER, _agent, _count_ward, _names, _two_nurse_ctx
from tests.schedule_edit.test_solver import BEFORE_MONTH, make_ctx

ROOT = Path(__file__).resolve().parents[2]
SAVED = {"new_schedule_id": "s2", "new_version": 7, "status": "draft", "name": "11월 수정본"}


def _two_minji():
    return _names(make_ctx({"n1": "DODO", "n2": "ODOD"}), {"n1": "김민지", "n2": "이민지"})


PROPOSE = ("propose_edit", {"changes": [A_OFF]}, "llm")

#: id, 근무표(ctx 만드는 함수 또는 None), 먼저 (수정 도우미 경로로) 해 둘 호출들, 비교할 호출
CASES = [
    ("propose_candidates", None, [], PROPOSE),
    ("ambiguous_name", _two_minji, [], ("propose_edit", {"changes": [{**A_OFF, "nurse": "민지"}]}, "llm")),
    ("unknown_name", None, [], ("propose_edit", {"changes": [{**A_OFF, "nurse": "없는사람"}]}, "llm")),
    ("edit_from_day_asked", None, [], ("propose_edit", {"changes": [], "edit_from_day": 3}, "llm")),
    # 고칠 첫날과 기간형 요청을 함께 — 문맥 주입(context_first)이 이름 확정(grounder)보다 먼저여야 같은 답이 나온다
    ("edit_from_day_with_balance", None, [], ("propose_edit", {"changes": [{"kind": "balance", "nurse": "", "shift": "OFF"}],
                                                               "edit_from_day": 2}, "llm")),
    ("edit_from_day_with_count", None, [], ("propose_edit", {"changes": [{"kind": "count", "nurse": "A", "shift": "OFF", "change": 1}],
                                                             "edit_from_day": 2}, "llm")),
    ("lookup", None, [], ("lookup", {"kind": "day_shift", "day": 1, "shift": "D"}, "llm")),
    ("check_rules", None, [], ("check_rules", {"topic": "grade"}, "llm")),
    ("reply_only", None, [], ("reply_only", {"kind": "status"}, "llm")),
    ("example_source", None, [], ("lookup", {"kind": "day_shift", "day": 1, "shift": "D"}, "example")),
    ("button_keep", None, [PROPOSE], ("keep_candidate", {"rank": 1}, "button")),
    ("button_reject", None, [PROPOSE], ("reject_candidate", {"rank": 1}, "button")),
    ("button_choose_transfer", _count_ward, [("propose_edit", {"changes": [N_LESS_2]}, "llm")],
     ("choose_transfer", {"choice_id": "same_team"}, "button")),
    ("button_apply_asks", _count_ward, [PROPOSE], ("apply_edit", {"rank": 1}, "button")),
    ("button_apply_saved", None, [PROPOSE], ("apply_edit", {"rank": 1}, "button")),
    ("llm_picks_a_button", None, [], ("apply_edit", {"rank": 1}, "llm")),
    ("example_picks_write", None, [], ("apply_edit", {"rank": 1}, "example")),
    ("button_picks_a_tool", None, [], ("propose_edit", {"changes": [A_OFF]}, "button")),
    ("aide_skill_name", None, [], ("query_schedule", {"scope": "wanted"}, "llm")),
    ("missing_candidate", None, [PROPOSE], ("keep_candidate", {"rank": 9}, "button")),
    ("extra_args_dropped", None, [], ("lookup", {"kind": "day_shift", "day": 1, "shift": "D", "schedule_id": "x",
                                                  "grounded": {"nurse": "zz"}}, "llm")),
]


def _run(world, monkeypatch, db, case, path, role="HN"):
    _id, ctx, setup, (name, args, source) = case
    monkeypatch.setattr(skills_mod, "apply_to_new_version", lambda *a, **kw: dict(SAVED))
    if _id == "button_apply_saved":      # 적용 전에 짚을 것이 없는 근무표로 — 저장 함수는 가짜
        monkeypatch.setattr(skills_mod, "apply_findings", lambda *a, **kw: [])
    world.ctxs["s1"] = ctx or _two_nurse_ctx       # 경우마다 기본 근무표로 되돌린다 — 앞 경우의 근무표가 남지 않게
    conv = world.store.start(USER.account_id, {"schedule_id": "s1"})
    conv.begin_turn()
    agent, _ = _agent()
    for n, a, s in setup:                      # 앞 상태는 수정 도우미 경로로(두 경로 모두 같은 앞 상태)
        edit_mw.execute_skill(agent._turn(db, USER, conv, BEFORE_MONTH, None), n, dict(a), source=s)
    world.events.clear()
    turn = agent._turn(db, USER, conv, BEFORE_MONTH, None)
    raised = None
    outcome = None
    if path == "edit":
        try:
            outcome = edit_mw.execute_skill(turn, name, dict(args), source=source).data
        except Exception as exc:   # noqa: BLE001
            raised = type(exc).__name__
    else:
        ctx_ = SessionContext(office_id="o", group_id="g", year=2026, month=11, user_role=role, agent_id="schedule-edit",
                              source=source, turn=turn, auth_user=USER)
        res = aide_execute(db, name, dict(args), ctx_)
        if classify(res.data) is ErrorType.GENERIC_ERROR and not res.data.get("failed"):
            raised = "Exception"
        else:
            outcome = from_aide_result(res.data, source)
    return outcome, raised, _events(world)


def _events(world):
    rows = []
    for e in world.events:
        payload = {k: v for k, v in e.payload.items() if k != "steps"}
        rows.append((e.event, e.schedule_id, payload, e.new_schedule_id, e.turn_no))
    return rows


@pytest.mark.parametrize("case", CASES, ids=[c[0] for c in CASES])
def test_both_paths_give_the_same_outcome_and_records(world, monkeypatch, db, case):
    edit = _run(world, monkeypatch, db, case, "edit")
    aide = _run(world, monkeypatch, db, case, "aide")
    assert aide[1] is None and edit[1] is None
    assert aide[0] == edit[0]                      # answer·preview·effects·focus·failed
    assert aide[2] == edit[2]                      # 기록 이벤트
    assert edit[0] is not None


def test_cases_cover_what_they_claim(world, monkeypatch, db):
    by_id = {c[0]: _run(world, monkeypatch, db, c, "edit") for c in CASES}
    assert by_id["propose_candidates"][0].preview["status"] == "ok"
    assert [e[0] for e in by_id["ambiguous_name"][2]] == ["clarify"] and "김민지·이민지 중 누구인가요" in by_id["ambiguous_name"][0].answer
    assert [e[0] for e in by_id["edit_from_day_asked"][2]] == ["lock", "clarify"]
    assert by_id["button_choose_transfer"][0].preview["status"] == "ok"
    assert by_id["button_apply_asks"][0].preview["status"] == "confirm_apply"
    assert by_id["button_apply_saved"][0].effects[0]["type"] == "switch_version"
    assert by_id["missing_candidate"][0].failed and by_id["missing_candidate"][2][-1][0] == "error"
    assert by_id["missing_candidate"][2][-1][2]["stage"] == "button"
    assert by_id["llm_picks_a_button"][0].failed is False and by_id["button_picks_a_tool"][0].failed is True


def test_an_unexpected_exception_is_recorded_by_the_agent_and_returned_as_an_error(world, monkeypatch, db):
    def boom(*a, **kw):
        raise RuntimeError("터졌다")

    monkeypatch.setattr(skills_mod, "lookup_answer", boom)
    case = next(c for c in CASES if c[0] == "lookup")
    edit_out, edit_raised, edit_events = _run(world, monkeypatch, db, case, "edit")
    aide_out, aide_raised, aide_events = _run(world, monkeypatch, db, case, "aide")
    assert edit_raised == "RuntimeError" and aide_raised == "Exception" and edit_out is None and aide_out is None
    assert aide_events == edit_events and aide_events[-1][0] == "error"
    assert aide_events[-1][2] == {"stage": "skill", "error": "RuntimeError: 터졌다"}


def test_the_aide_error_result_is_a_generic_error(world, monkeypatch, db):
    monkeypatch.setattr(skills_mod, "lookup_answer", lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("x")))
    conv = world.store.start(USER.account_id, {"schedule_id": "s1"})
    conv.begin_turn()
    turn = _agent()[0]._turn(db, USER, conv, BEFORE_MONTH, None)
    ctx = SessionContext(office_id="o", group_id="g", year=2026, month=11, agent_id="schedule-edit", source="llm", turn=turn)
    res = aide_execute(db, "lookup", {"kind": "day_shift", "day": 1, "shift": "D"}, ctx)
    assert classify(res.data) is ErrorType.GENERIC_ERROR


@pytest.mark.parametrize("role", ["HN", "ADM", "nurse"])
def test_aide_permission_does_not_block_the_people_the_edit_path_lets_in(world, monkeypatch, db, role):
    # 수정 도우미 경로는 수간호사·그룹관리자·ADM 만 들인다(경로가 먼저 가린다) — 그룹관리자의 user_role 은 HN 이 아닐 수 있어 ① 이 막으면 안 된다
    case = CASES[0]
    out, raised, events = _run(world, monkeypatch, db, case, "aide", role=role)
    assert raised is None and out.preview["status"] == "ok" and events


# ── Outcome 왕복 · classify 대응 ────────────────────────────────────────────


@pytest.mark.parametrize("outcome", [
    Outcome(answer="a"),
    Outcome(answer="b", preview={"status": "ok", "candidates": [{"rank": 1}]}, effects=[{"type": "x", "n": 1}], focus=2),
    Outcome(answer="c", failed=True),
    Outcome(answer="d", preview={"status": "confirm", "question": "?"}, focus=None),
])
def test_outcome_round_trips_without_loss(outcome):
    assert Outcome.from_data(outcome.to_data()) == outcome


@pytest.mark.parametrize("outcome,clarify,expected", [
    (Outcome(answer="x", preview={"status": "ok", "candidates": [{"rank": 1}]}), False, ErrorType.PREVIEW),
    (Outcome(answer="x", preview={"status": "confirm"}), False, ErrorType.CLARIFICATION),
    (Outcome(answer="x", preview={"status": "confirm_apply"}), False, ErrorType.CLARIFICATION),
    (Outcome(answer="x", preview={"status": "choose_plan"}), False, ErrorType.CLARIFICATION),
    (Outcome(answer="x", preview={"status": "choose_transfer"}), False, ErrorType.CLARIFICATION),
    (Outcome(answer="x"), True, ErrorType.CLARIFICATION),
    (Outcome(answer="x", failed=True), False, ErrorType.GENERIC_ERROR),
    (Outcome(answer="x", preview={"status": "infeasible", "levers": []}), False, ErrorType.OK),
    (Outcome(answer="x", preview={"status": "ok", "candidates": []}), False, ErrorType.OK),
    (Outcome(answer="x", preview={"status": "invalid"}), False, ErrorType.OK),
    (Outcome(answer="x", preview={"status": "already"}), False, ErrorType.OK),
    (Outcome(answer="x", preview={"status": "timeout"}), False, ErrorType.OK),
    (Outcome(answer="x", preview={"status": "compare", "cells": []}), False, ErrorType.OK),
    (Outcome(answer="x"), False, ErrorType.OK),
])
def test_classify_understands_the_data(outcome, clarify, expected):
    assert classify(outcome.to_data(clarify=clarify)) is expected


def test_every_preview_status_in_the_code_is_classified():
    # 코드에 나오는 preview.status 값: solver 결과(ok·already·invalid·infeasible·timeout) + 스킬이 직접 만드는 것
    text = "\n".join((ROOT / "app" / "schedule_edit_agent" / "skills.py").read_text(encoding="utf-8").splitlines())
    import re
    direct = set(re.findall(r'"status": "([a-z_]+)"', text)) - {"draft"}
    assert direct <= {"confirm", "confirm_apply", "choose_plan", "choose_transfer", "compare"}
    # 엔진이 만드는 status — EditResult("…") 첫 인자(solver·postprocess)
    engine = set()
    for f in (ROOT / "app" / "services" / "schedule_edit").glob("*.py"):
        engine |= set(re.findall(r'EditResult\(\s*"([a-z_]+)"', f.read_text(encoding="utf-8")))
    assert engine == {"ok", "already", "invalid", "infeasible", "timeout"}
    assert skills_mod.ASKING_STATUSES == {"confirm", "confirm_apply", "choose_plan", "choose_transfer"}


# ── 턴 묶기 · 매니페스트 ─────────────────────────────────────────────────────


def test_current_turn_is_none_outside_a_turn_and_skills_refuse():
    assert current_turn() is None
    spec = find_spec("lookup")
    with pytest.raises(RuntimeError, match="턴 안에서만"):
        spec.handler(None, {})
    marker = object()
    with bind_turn(marker):
        assert current_turn() is marker
    assert current_turn() is None


def test_all_twenty_skills_are_in_the_manifest_with_their_fields():
    load_manifest_skills()
    tools, buttons = [t["name"] for t in SKILL_TOOLS], [b["name"] for b in BUTTON_TOOLS]
    assert len(tools) == 9 and len(buttons) == 11
    for name in tools + buttons:
        spec = SKILL_SPECS[name]
        assert spec.agents == ("schedule-edit",) and spec.mode == SKILL_MODES[name]
        assert spec.llm_visible is (name in tools)
        assert spec.sources == (("llm", "example") if name in tools else ("button",))
        assert not spec.mutation and not spec.hn_only
        assert spec.commit == "button"      # 적용은 버튼으로만 — 후보를 낸 스킬도 글자 승인으로 다시 풀지 않는다
    assert {n for n in tools + buttons if SKILL_SPECS[n].grounder} == set(skills_mod.GROUNDERS)
    assert {n for n in tools + buttons if SKILL_SPECS[n].context_first} == set(skills_mod.CONTEXT_FIRST)


@pytest.mark.parametrize("clash", ["lookup", "check-rules"])
def test_a_name_clash_with_aide_raises_at_registration(monkeypatch, clash):
    # 20개 이름을 두 dict 에서 모두 뺀 뒤 옛 @register 쪽에만 겹치는 이름을 심어, 그 이름에서 터지는지 본다(하이픈·밑줄 구분 없이)
    from agents_v2.skills.registry import SKILL_REGISTRY
    from schedule_edit_agent import aide_skills

    load_manifest_skills()
    for t in SKILL_TOOLS + BUTTON_TOOLS:
        monkeypatch.delitem(SKILL_SPECS, t["name"])
        monkeypatch.delitem(SKILL_REGISTRY, t["name"])
    monkeypatch.setitem(SKILL_REGISTRY, clash, lambda db, params: {})
    with pytest.raises(RuntimeError, match="이미 등록") as err:
        aide_skills.register_all()
    assert clash.replace("-", "_") in str(err.value)


def test_no_name_overlaps_the_old_aide_skills():
    from agents_v2.skills import registry as aide_registry
    aide_registry._ensure_loaded()
    mine = {t["name"] for t in SKILL_TOOLS + BUTTON_TOOLS}
    others = {n for n, s in SKILL_SPECS.items() if "schedule-edit" not in s.agents} | \
        {n for n in aide_registry.SKILL_REGISTRY if n not in SKILL_SPECS}
    assert not mine & {n.replace("-", "_") for n in others}       # 옛 스킬은 하이픈 이름이라 밑줄로 맞춰 견준다


@pytest.mark.parametrize("statement", [
    "import agents_v2.skills.descriptions",
    "import schedule_edit_agent.spec; import agents_v2.middleware; import agents_v2.router",
    "import agents_v2.middleware; import agents_v2.router; import schedule_edit_agent.spec",
    "import main",
])
def test_import_orders_have_no_cycle(statement):
    code = f"import sys; sys.path.insert(0, 'app'); {statement}; " \
           "from agents_v2.skills.manifest import SKILL_SPECS, load_manifest_skills; load_manifest_skills(); " \
           "assert 'apply_edit' in SKILL_SPECS"
    done = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True,
                          env={**__import__("os").environ, "OPENAI_API_KEY": "", "ANTHROPIC_API_KEY": ""})
    assert done.returncode == 0, done.stderr[-1500:]


def test_aide_code_does_not_import_the_edit_agent():
    import re
    hits = []
    for path in (ROOT / "app" / "agents_v2").rglob("*.py"):
        if path.name == "surface.py":     # surface.py = 파생 에이전트를 켜는 배선(S1)
            continue
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if re.match(r"\s*(from|import)\s+schedule_edit_agent", line):
                hits.append(f"{path.name}:{n}")
    assert hits == []


# ── 검토 반영(S4 후속) ──────────────────────────────────────────────────────


def _aide(world, db, name, args, source="llm", agent_id="schedule-edit"):
    conv = world.store.start(USER.account_id, {"schedule_id": "s1"})
    conv.begin_turn()
    turn = _agent()[0]._turn(db, USER, conv, BEFORE_MONTH, None)
    ctx = SessionContext(office_id="o", group_id="g", year=2026, month=11, user_role="HN", agent_id=agent_id,
                         source=source, turn=turn, auth_user=USER)
    return aide_execute(db, name, dict(args), ctx), turn


def test_extra_args_and_caller_grounded_are_dropped(world, db):
    res, _ = _aide(world, db, "lookup", {"kind": "day_shift", "day": 1, "shift": "D", "schedule_id": "x",
                                         "grounded": {"nurse": "zz"}})
    assert "schedule_id" not in res.grounded_params
    assert res.grounded_params["grounded"] != {"nurse": "zz"}
    # 버튼 출처는 서버가 만든 인자라 그대로 둔다
    res, _ = _aide(world, db, "keep_candidate", {"rank": 9, "extra": 1}, source="button")
    assert res.grounded_params.get("extra") == 1


def test_a_body_clarification_is_classified_as_clarification(world, db):
    res, _ = _aide(world, db, "revise_edit", {"add": [A_OFF]})       # 앞 요청이 없어 본문이 되묻는다
    assert classify(res.data) is ErrorType.CLARIFICATION
    res, _ = _aide(world, db, "propose_edit", {"changes": [], "edit_from_day": 3})
    assert classify(res.data) is ErrorType.CLARIFICATION


def test_the_audit_status_follows_the_final_result(world, db, monkeypatch):
    from agents_v2 import middleware as aide_mw
    seen = []
    monkeypatch.setattr(aide_mw, "_write_skill_audit", lambda db_, ctx, name, params, status, err, dt: seen.append((name, status)))
    _aide(world, db, "keep_candidate", {"rank": 9}, source="button")         # SkillError → failed
    _aide(world, db, "revise_edit", {"add": [A_OFF]})                         # 본문 되물음
    _aide(world, db, "propose_edit", {"changes": [A_OFF]})                    # 정상
    assert seen == [("keep_candidate", "ERROR"), ("revise_edit", "CLARIFICATION_NEEDED"), ("propose_edit", "SUCCESS")]


def test_result_and_audit_use_the_defined_skill_name(world, db, monkeypatch):
    from agents_v2 import middleware as aide_mw
    seen = []
    monkeypatch.setattr(aide_mw, "_write_skill_audit", lambda db_, ctx, name, *a: seen.append(name))
    res, _ = _aide(world, db, "propose-edit", {"changes": [A_OFF]})
    assert res.skill_name == "propose_edit" and seen == ["propose_edit"]


def test_an_unexpected_exception_is_logged_and_not_mislabelled(world, db, monkeypatch, caplog):
    def boom(*a, **kw):
        raise KeyError("zz")
    monkeypatch.setattr(skills_mod, "lookup_answer", boom)
    with caplog.at_level("ERROR"):
        res, _ = _aide(world, db, "lookup", {"kind": "day_shift", "day": 1, "shift": "D"})
    assert "Unknown skill" not in res.data["error"] and "zz" in res.data["error"]
    assert any(r.exc_info for r in caplog.records)
    with pytest.raises(RuntimeError, match="zz"):       # 껍데기가 엉뚱한 KeyError 대신 원래 오류를 본다
        from_aide_result(res.data, "llm")


def test_the_trace_is_this_calls_not_a_previous_calls(world, db):
    conv = world.store.start(USER.account_id, {"schedule_id": "s1"})
    conv.begin_turn()
    turn = _agent()[0]._turn(db, USER, conv, BEFORE_MONTH, None)
    edit_mw.execute_skill(turn, "apply_edit", {"rank": 1}, source="llm")        # ① 에서 막혀 기록 없이 끝난다
    world.events.clear()
    ctx = SessionContext(office_id="o", group_id="g", year=2026, month=11, user_role="HN", agent_id="schedule-edit",
                         source="llm", turn=turn, auth_user=USER)
    aide_execute(db, "propose_edit", {"changes": [A_OFF]}, ctx)
    first = next(e for e in world.events if e.event == "propose")
    assert all(step["name"] != "permission" or step["status"] != "block" for step in first.payload.get("steps", []))
