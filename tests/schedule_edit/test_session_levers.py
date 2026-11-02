"""대화 이어가기(넘긴 묶음·시작 해)·재검증·레버 여러 개·규칙 열쇠 유일성.

기대값은 손으로 센 값이다.
"""

from __future__ import annotations

from collections import Counter
from datetime import date

import pytest

from services.schedule_edit.levers import find_levers, lever_allow
from services.schedule_edit.requests import SetCell
from services.schedule_edit.rules import build_rules
from services.schedule_edit.session import EditSession, allow_lever, allow_lever_id, keep, reject
from services.schedule_edit.solver import AVOIDED_ONLY, propose_edits, verify_candidate
from tests.schedule_edit.test_solver import BEFORE_MONTH, _two_nurse_ctx, make_ctx


def _session(ctx) -> EditSession:
    return EditSession(schedule_id="s1", ctx=ctx, rules=build_rules(ctx), screen=None, stamp="x")


def _propose(session, requests):
    from services.schedule_edit.session import propose
    return propose(session, requests, today=BEFORE_MONTH)


def test_rejected_people_are_not_offered_again():
    # B·C 가 똑같아 'A·B' 안과 'A·C' 안이 둘 다 4칸. A·B 를 넘기면 A·C 만 남는다.
    ctx = make_ctx({"A": "DODO", "B": "ODOD", "C": "ODOD"}, cov_min={"D": 1, "E": 0, "N": 0},
                   cov_max={"D": 2}, off_days=2, config={"not_one_night": False})
    s = _session(ctx)
    first = _propose(s, [SetCell("A", 0, "O")]).candidates[0]
    reject(s, first)
    again = _propose(s, [SetCell("A", 0, "O")])
    assert all(set(c.affected) != set(first.affected) for c in again.candidates)


def test_kept_candidate_becomes_hint_without_changing_rules():
    ctx = _two_nurse_ctx()
    s = _session(ctx)
    first = _propose(s, [SetCell("A", 0, "O")]).candidates[0]
    keep(s, first)
    assert s.hint["A"][0] == "O"
    again = _propose(s, [SetCell("A", 0, "O")])
    assert again.candidates[0].changed_count == first.changed_count   # 힌트는 시작점일 뿐 답을 바꾸지 않는다


def test_verify_candidate_catches_a_tampered_change():
    ctx = _two_nurse_ctx()
    s = _session(ctx)
    cand = _propose(s, [SetCell("A", 0, "O")]).candidates[0]
    assert verify_candidate(ctx, s.rules, cand, s.requests, today=BEFORE_MONTH) == []
    cand.changes = [c for c in cand.changes if c.nurse_id != "B"]    # B 를 안 바꾸면 1일 D 미달 등
    assert verify_candidate(ctx, s.rules, cand, s.requests, today=BEFORE_MONTH) != []


def test_verify_candidate_rechecks_dates_and_request():
    ctx = _two_nurse_ctx()
    s = _session(ctx)
    cand = _propose(s, [SetCell("A", 0, "O")]).candidates[0]
    # 후보를 찾은 뒤 자정을 넘겨 1일이 지난 날이 됐다
    assert "1일은 이미 지난 날짜입니다" in verify_candidate(ctx, s.rules, cand, s.requests, today=date(2026, 11, 2))
    cand.changes = [c for c in cand.changes if (c.nurse_id, c.day) != ("A", 0)]
    assert any(p.startswith("요청 미충족") for p in verify_candidate(ctx, s.rules, cand, s.requests,
                                                                    today=BEFORE_MONTH))


def test_levers_are_distinct_and_cheapest_first():
    # A 의 OFF 는 주휴 2개뿐 → 1일 OFF 는 OFF 상한을 넘겨야만. 레버는 서로 다른 넘김 묶음이어야 한다.
    ctx = make_ctx({"A": "DDOO", "B": "DDOO"}, off_days=2, config={"not_one_night": False})
    for nid in ("A", "B"):
        ctx.frozen[(nid, 2)] = ctx.frozen[(nid, 3)] = "주휴·특수 휴무"
    levers = find_levers(ctx, [SetCell("A", 0, "O")], None, max_levers=3)
    assert levers and len({tuple(sorted(lv.allow)) for lv in levers}) == len(levers)
    assert len(levers[0].allow) <= min(len(lv.allow) for lv in levers)


def test_allow_lever_accumulates():
    s = _session(_two_nurse_ctx())

    class L:
        allow = {("OFF수", "A", None, None, "상한"): 1}
    allow_lever(s, L)
    allow_lever(s, L)
    assert s.allow == {("OFF수", "A", None, None, "상한"): 2}


def test_rule_keys_are_unique():
    # 레버·허용량이 열쇠로 위치를 찾는다 — 같은 열쇠가 둘이면 엉뚱한 부등식이 풀린다(검토 L6)
    ctx = make_ctx({"A": "NNOODDEEOO", "B": "DDEENNOOOD"}, cov_min={"D": 1, "E": 1, "N": 1},
                   cov_max={"D": 2}, off_days=3, config={"two_offs_after_three_nig": False})
    ctx.mutex = {"A": {"partner_id": "B", "days": {1}}}
    rules = build_rules(ctx)
    dup = [k for k, n in Counter(i.key for i in rules.ineqs + rules.soft).items() if n > 1]
    assert dup == []


def test_over_coverage_on_days_without_max_is_a_quality_note():
    # 최대가 없는 날 D 필요 1. A 를 2일에 D 로 넣으면(요청) 2일 D 가 2명 — 규칙 위반은 아니고 품질 문장으로 나온다.
    from services.schedule_edit.solver import quality_notes
    ctx = make_ctx({"A": "DOD", "B": "DDD"}, cov_min={"D": 1, "E": 0, "N": 0}, cov_max={"E": 2, "N": 2},
                   config={"not_one_night": False})
    grid = {"A": list("DDD"), "B": list("DDD")}
    assert "2일 필요 인원보다 많이 배정 D" in quality_notes(ctx, build_rules(ctx), grid)


def test_result_serializes_to_json_with_lever_ids():
    import json

    from services.schedule_edit.serialize import allow_from_json, result_to_dict
    ctx = make_ctx({"A": "DDOO"}, off_days=2, config={"not_one_night": False})
    ctx.frozen[("A", 2)] = ctx.frozen[("A", 3)] = "주휴·특수 휴무"
    s = _session(ctx)
    res = _propose(s, [SetCell("A", 0, "O")])
    out = json.loads(json.dumps(result_to_dict(ctx, res), ensure_ascii=False))
    assert out["status"] == "infeasible" and out["levers"]
    lever = out["levers"][0]
    assert len(lever["lever_id"]) == 12
    assert allow_from_json(lever["allow"]) == res.levers[0].allow   # 화면이 돌려보낸 레버로 그대로 다시 탐색


def test_rejected_groups_reset_when_request_changes():
    ctx = make_ctx({"A": "DODO", "B": "ODOD", "C": "ODOD"}, cov_min={"D": 1, "E": 0, "N": 0},
                   cov_max={"D": 2}, off_days=2, config={"not_one_night": False})
    s = _session(ctx)
    reject(s, _propose(s, [SetCell("A", 0, "O")]).candidates[0])
    _propose(s, [SetCell("A", 0, "O")])
    assert s.avoid_people                      # 같은 요청 — 넘긴 묶음 유지
    _propose(s, [SetCell("A", 0, "O"), SetCell("A", 0, "O")])
    assert s.avoid_people                      # 순서·중복만 다른 같은 요청도 유지
    _propose(s, [SetCell("A", 2, "O")])
    assert s.avoid_people == []                # 다른 요청 — 비운다(검토 M3)


def test_only_rejected_group_left_is_said_plainly():
    # A 1일 OFF 는 A·B 를 둘 다 바꿔야만 된다. 그 묶음을 넘기면 남는 방법이 없다 — '이유 못 좁힘' 이 아니다.
    s = _session(_two_nurse_ctx())
    reject(s, _propose(s, [SetCell("A", 0, "O")]).candidates[0])
    again = _propose(s, [SetCell("A", 0, "O")])
    assert again.status == "infeasible" and again.reasons == [AVOIDED_ONLY]


def test_hard_lock_allow_is_ignored_everywhere():
    # 1일 N 고정 뒤 2일 D(N→D 하드락). 밖에서 '전환금지' 허용량을 넣어도 봐주지 않는다(검토 M2).
    ctx = make_ctx({"A": "NOOOO"}, config={"not_one_night": False})
    ctx.frozen[("A", 0)] = "확정 원티드"
    key = ("전환금지", "A", 1, "ND")
    assert lever_allow({key: 1}) == {}
    assert propose_edits(ctx, [SetCell("A", 1, "D")], allow={key: 1}, today=BEFORE_MONTH).status == "infeasible"
    s = _session(ctx)

    class L:
        allow = {key: 1}
    allow_lever(s, L)
    assert s.allow == {}


def test_lever_is_chosen_by_id_from_last_result():
    from services.schedule_edit.serialize import lever_id
    ctx = make_ctx({"A": "DDOO"}, off_days=2, config={"not_one_night": False})
    ctx.frozen[("A", 2)] = ctx.frozen[("A", 3)] = "주휴·특수 휴무"
    s = _session(ctx)
    res = _propose(s, [SetCell("A", 0, "O")])
    chosen = allow_lever_id(s, lever_id(res.levers[0].allow))
    assert s.allow == chosen.allow
    with pytest.raises(KeyError):
        allow_lever_id(s, "000000000000")


def test_levers_skip_solutions_the_screen_rejects(monkeypatch):
    from services.schedule_edit import solver as solver_mod
    ctx = make_ctx({"A": "DDOO"}, off_days=2, config={"not_one_night": False})
    ctx.frozen[("A", 2)] = ctx.frozen[("A", 3)] = "주휴·특수 휴무"
    assert find_levers(ctx, [SetCell("A", 0, "O")], None)
    monkeypatch.setattr(solver_mod, "screen_problems", lambda screen, changes, allow: ["화면 미달"])
    assert find_levers(ctx, [SetCell("A", 0, "O")], None, screen=object()) == []


def test_forbid_same_people_ignores_groups_with_someone_outside_the_window():
    from services.schedule_edit.model import EditModel, add_cell_vars, edit_codes, forbid_same_people
    ctx = make_ctx({"A": "DODO", "B": "ODOD"})
    em = EditModel(ctx=ctx, base=ctx.main, free={("A", 1), ("A", 2)}, codes=edit_codes(ctx))
    add_cell_vars(em)
    before = len(em.model.Proto().constraints)
    forbid_same_people(em, {"A", "B"})        # B 는 이 범위에서 못 바뀐다 — A 만 바뀐 해까지 막으면 안 된다
    assert len(em.model.Proto().constraints) == before


class _FakeDB:
    def __init__(self):
        self.rollbacks = 0

    def rollback(self):
        self.rollbacks += 1


def test_apply_locks_then_refuses_stale_or_foreign_candidates(monkeypatch):
    import copy

    from services.schedule_edit import apply as apply_mod
    s = _session(_two_nurse_ctx())
    cand = _propose(s, [SetCell("A", 0, "O")]).candidates[0]
    db, order = _FakeDB(), []
    monkeypatch.setattr(apply_mod, "_lock_source", lambda db, sid: order.append("lock"))
    monkeypatch.setattr(apply_mod, "is_stale", lambda db, session: order.append("stale?") or True)
    with pytest.raises(apply_mod.ApplyError) as err:
        apply_mod.apply_to_new_version(db, s, cand, None, today=BEFORE_MONTH)
    assert err.value.code == "stale" and order == ["lock", "stale?"] and db.rollbacks == 1
    with pytest.raises(apply_mod.ApplyError) as err:
        apply_mod.apply_to_new_version(db, s, copy.copy(cand), None, today=BEFORE_MONTH)
    assert err.value.code == "unknown"

    def lock_timeout(db, sid):
        raise TimeoutError("잠금 대기 시간 초과")
    monkeypatch.setattr(apply_mod, "_lock_source", lock_timeout)
    with pytest.raises(TimeoutError):
        apply_mod.apply_to_new_version(db, s, cand, None, today=BEFORE_MONTH)
    assert db.rollbacks == 2                   # 잠금 실패도 되돌린다


def test_apply_saves_and_records(monkeypatch):
    from services.schedule_edit import apply as apply_mod
    s = _session(_two_nurse_ctx())
    cand = _propose(s, [SetCell("A", 0, "O")]).candidates[0]
    recorded = []
    monkeypatch.setattr(apply_mod, "_lock_source", lambda db, sid: None)
    monkeypatch.setattr(apply_mod, "is_stale", lambda db, session: False)
    monkeypatch.setattr(apply_mod, "_save_as", lambda db, session, candidate, user, name: {
        "new_schedule_id": "s2", "changed_cells": candidate.changed_count})
    monkeypatch.setattr(apply_mod, "record_event", lambda db, sid, event, payload, **kw: recorded.append(
        (sid, event, kw["new_schedule_id"], payload["request_text"])))
    out = apply_mod.apply_to_new_version(_FakeDB(), s, cand, None, request_text="A 1일 OFF", today=BEFORE_MONTH)
    assert out["new_schedule_id"] == "s2"
    assert recorded == [("s1", "apply", "s2", "A 1일 OFF")]


def test_record_event_failure_does_not_raise(monkeypatch):
    from sqlalchemy.exc import SQLAlchemyError

    from services.schedule_edit import journal

    class DB(_FakeDB):
        def begin_nested(self):
            raise SQLAlchemyError("잠금 시간 초과")
    monkeypatch.setattr(journal, "_table_ready", lambda db: True)
    db = DB()
    assert journal.record_event(db, "s1", "apply", {}) is None and db.rollbacks == 1
