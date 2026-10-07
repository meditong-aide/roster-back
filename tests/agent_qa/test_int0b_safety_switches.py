"""int-0b — dev 병합 직후 안전 스위치(설계서 §5.4).

① 마감본 칸 쓰기 거절(dev 정본 409 와 같은 규칙, 성남 ③ 10-02) — N80·W62
② 사용량 기록 실패가 호출자 세션을 죽이지 않는다(SAVEPOINT) — M6·N82, 병합 프로브 P5
③ 사용량 화면 기본 미마운트 — M8·N57
④ 테스트 인스턴스용 주기 작업 끄기 스위치 — M15
⑤ 픽스처에 dev 새 테이블(알림 대기열 등) — W72. 알림 적재가 테스트에서도 실제로 돈다.
(발행 쓰기 끄기는 test_publish_schedule.py)
"""
from __future__ import annotations

import asyncio
from datetime import datetime

import pytest

import agents_v2.usage as usage
from agents_v2.errors import ErrorType, classify
from agents_v2.middleware import execute_skill
from agents_v2.schemas.session_context import SessionContext
from agents_v2.skills.bulk_mutation import _mutate_schedule_entries
from agents_v2.tools import schedule_tools
from db.models import AgentLlmUsage, Group, PushOutbox, Schedule, ScheduleEntry
from tests.agent_qa.harness import make_auth_user

_SID = "SCH202604V1"  # 시드 근무표(4월, status=completed)


def _issue_seed(db) -> None:
    db.query(Schedule).filter_by(schedule_id=_SID).one().status = "issued"
    db.flush()


def _cell(db, nurse_id="N001", day=1) -> str:
    return (db.query(ScheduleEntry)
            .filter(ScheduleEntry.schedule_id == _SID, ScheduleEntry.nurse_id == nurse_id,
                    ScheduleEntry.work_date == datetime(2026, 4, day))
            .one().shift_id)


def _single_edit_params(**extra) -> dict:
    return {"group_id": "GRP001", "year": 2026, "month": 4, "schedule_id": _SID,
            "nurse_ids": ["N001"], "date": "2026-04-01", "new_shift_code": "N_GRP001", **extra}


# ── ① 마감본 칸 쓰기 ─────────────────────────────────────────


@pytest.mark.parametrize("preview_only", [True, False])
def test_issued_schedule_cell_edit_is_rejected(db, seed_data, preview_only):
    _issue_seed(db)
    before = _cell(db)
    res = _mutate_schedule_entries(db, _single_edit_params(), {}, preview_only)
    assert res["error"] == schedule_tools.ISSUED_OVERWRITE_MSG
    assert classify(res) is ErrorType.GENERIC_ERROR  # 미리보기 OK → 승인 실패가 아니다
    assert _cell(db) == before


def test_issued_schedule_bulk_edit_is_rejected(db, seed_data):
    _issue_seed(db)
    before = [_cell(db, "N001", d) for d in range(1, 4)]
    params = {"group_id": "GRP001", "year": 2026, "month": 4, "schedule_id": _SID,
              "nurse_ids": ["N001"], "new_shift_code": "OFF_GRP001"}
    res = _mutate_schedule_entries(db, params, {}, False)
    assert res["error"] == schedule_tools.ISSUED_OVERWRITE_MSG
    assert [_cell(db, "N001", d) for d in range(1, 4)] == before


def test_issued_check_holds_at_the_cell_writer(db, seed_data):
    """칸 쓰기 함수 자체도 막는다 — 다른 경로가 미리 확인을 건너뛰어도 마감본은 못 쓴다."""
    _issue_seed(db)
    entry_id = (db.query(ScheduleEntry.entry_id)
                .filter_by(schedule_id=_SID, nurse_id="N001").first()[0])
    res = schedule_tools.update_schedule_entry(db, entry_id, "N_GRP001", "GRP001")
    assert res["error"] == schedule_tools.ISSUED_OVERWRITE_MSG


def test_draft_schedule_cell_edit_still_works(db, seed_data):
    res = _mutate_schedule_entries(db, _single_edit_params(), {}, False)
    assert res.get("new_shift_id") == "N_GRP001", res
    assert _cell(db) == "N_GRP001"


def test_bulk_edit_is_all_or_nothing(db, seed_data, monkeypatch):
    """중간 칸에서 실패하면 앞 칸까지 되감는다 — 일부만 바뀐 근무표를 남기지 않는다."""
    original = schedule_tools.update_schedule_entry
    calls = {"n": 0}

    def _second_fails(db_, entry_id, new_shift_id, group_id):
        calls["n"] += 1
        if calls["n"] == 2:
            return {"error": schedule_tools.ISSUED_OVERWRITE_MSG, "issued": True}
        return original(db_, entry_id, new_shift_id, group_id)

    monkeypatch.setattr(schedule_tools, "update_schedule_entry", _second_fails)
    before = [_cell(db, "N001", d) for d in range(1, 31)]
    params = {"group_id": "GRP001", "year": 2026, "month": 4, "schedule_id": _SID,
              "nurse_ids": ["N001"], "new_shift_code": "OFF_GRP001"}
    res = _mutate_schedule_entries(db, params, {}, False)

    assert classify(res) is ErrorType.GENERIC_ERROR
    assert res["rolled_back"] is True and res["applied_count"] == 0
    assert calls["n"] == 2, "첫 오류에서 멈춰야 한다"
    db.expire_all()
    assert [_cell(db, "N001", d) for d in range(1, 31)] == before, "앞 칸도 되감겨야 한다"


def test_bulk_edit_commits_once(db, seed_data, monkeypatch):
    """칸마다 커밋하지 않는다 — 한 번만 커밋해 근무표 행 잠금이 끝까지 유지된다."""
    commits = {"n": 0}
    real_commit = db.commit

    def _count_commit():
        commits["n"] += 1
        return real_commit()

    monkeypatch.setattr(db, "commit", _count_commit)
    params = {"group_id": "GRP001", "year": 2026, "month": 4, "schedule_id": _SID,
              "nurse_ids": ["N001"], "new_shift_code": "OFF_GRP001"}
    res = _mutate_schedule_entries(db, params, {}, False)
    assert res["affected_count"] == 30
    assert commits["n"] == 1


def test_cell_writer_locks_the_schedule_row(db, seed_data, monkeypatch):
    """칸 쓰기는 근무표 행 UPDLOCK 을 잡고 판정한다(W62①). 미리보기 판정은 잠그지 않는다."""
    from sqlalchemy.orm import Query

    hints: list = []
    real_with_hint = Query.with_hint

    def _spy(self, selectable, text, dialect_name="*"):
        hints.append((text, dialect_name))
        return real_with_hint(self, selectable, text, dialect_name)

    monkeypatch.setattr(Query, "with_hint", _spy)
    _mutate_schedule_entries(db, _single_edit_params(), {}, True)
    assert hints == [], "미리보기는 잠그지 않는다"
    _mutate_schedule_entries(db, _single_edit_params(), {}, False)
    assert ("WITH (UPDLOCK, ROWLOCK)", "mssql") in hints


def test_issued_check_rereads_status_after_lock(db, seed_data):
    """세션이 쥔 옛 상태가 아니라 잠근 뒤 DB 상태로 판정한다 — 그사이 끝난 마감을 놓치지 않는다."""
    stale = db.query(Schedule).filter_by(schedule_id=_SID).one()
    assert stale.status == "completed"
    table = Schedule.__table__
    db.execute(table.update().where(table.c.schedule_id == _SID).values(status="issued"))
    assert stale.status == "completed"  # 세션 객체는 아직 모른다
    res = schedule_tools.issued_overwrite_error(db, _SID, lock=True)
    assert res and res["error"] == schedule_tools.ISSUED_OVERWRITE_MSG


def test_rejection_ends_the_transaction(db, seed_data, monkeypatch):
    """마감본 거절 때 트랜잭션을 끝내 잠금을 바로 푼다(성공 경로와 같은 경계)."""
    _issue_seed(db)
    commits = {"n": 0}
    real_commit = db.commit

    def _count_commit():
        commits["n"] += 1
        return real_commit()

    monkeypatch.setattr(db, "commit", _count_commit)
    entry_id = (db.query(ScheduleEntry.entry_id)
                .filter_by(schedule_id=_SID, nurse_id="N001").first()[0])
    res = schedule_tools.update_schedule_entry(db, entry_id, "N_GRP001", "GRP001")
    assert res["error"] == schedule_tools.ISSUED_OVERWRITE_MSG
    assert commits["n"] == 1


# ── ② 사용량 기록 실패 격리 ────────────────────────────────────


def test_usage_flush_failure_does_not_poison_caller(db, seed_data, monkeypatch):
    """병합 프로브 P5 반전 — 기록 flush 가 실패해도 호출자의 이어지는 저장은 성공한다."""
    monkeypatch.setattr(usage, "_usage_table_present", None)
    monkeypatch.setattr(usage, "compute_cost", lambda *a, **k: object())  # 바인딩 불가 값 → flush 실패
    db.add(Group(group_id="GRPP", office_id="OFF001", group_name="앞선작업"))
    db.flush()  # 호출자가 기록 전에 해 둔 미커밋 작업
    usage.record_llm_usage(db, conversation_id=None, group_id="GRP001", user_id="N002",
                           model="gpt-5.5", purpose="wanted", input_tokens=10, output_tokens=5)

    db.add(Group(group_id="GRPX", office_id="OFF001", group_name="후속작업"))
    db.flush()  # 예전엔 PendingRollbackError
    assert db.query(Group).filter(Group.group_id.in_(["GRPP", "GRPX"])).count() == 2  # 앞선 작업도 산다
    assert db.query(AgentLlmUsage).count() == 0


def test_usage_record_still_writes_normally(db, seed_data, monkeypatch):
    monkeypatch.setattr(usage, "_usage_table_present", None)
    usage.record_llm_usage(db, conversation_id="c1", group_id="GRP001", user_id="N002",
                           model="gpt-5.5", purpose="turn", input_tokens=10, output_tokens=5)
    assert db.query(AgentLlmUsage).count() == 1


# ── ③ 사용량 화면 기본 미마운트 ─────────────────────────────────


def test_usage_routes_not_mounted_by_default(monkeypatch):
    monkeypatch.delenv("AIDE_AGENT_USAGE_VIEW", raising=False)
    import main

    paths = {getattr(r, "path", "") for r in main.app.routes}
    assert not any(p.startswith("/api/agent/usage") for p in paths)


# ── ④ 주기 작업 끄기 스위치 ────────────────────────────────────


def test_scheduler_switch_starts_no_background_tasks(monkeypatch):
    import main

    monkeypatch.setenv("BACKGROUND_SCHEDULERS_DISABLED", "1")

    async def _tasks_inside_lifespan() -> int:
        before = len(asyncio.all_tasks())
        async with main.lifespan(main.app):
            return len(asyncio.all_tasks()) - before

    assert asyncio.run(_tasks_inside_lifespan()) == 0


def test_scheduler_switch_defaults_to_on(monkeypatch):
    import main

    monkeypatch.delenv("BACKGROUND_SCHEDULERS_DISABLED", raising=False)
    assert main._schedulers_disabled() is False


# ── ⑤ 알림 대기열이 테스트에서도 실제로 쌓인다 ───────────────────


def test_assignment_push_is_enqueued_with_token_sender(db, seed_data):
    """int-0a 의 사용자 객체가 알림 발신자로 들어간다(P4 의 발신자 쪽 확인)."""
    db.add(Group(group_id="GRP002", office_id="OFF001", group_name="중환자실2"))
    db.flush()
    ctx = SessionContext(office_id="OFF001", group_id="GRP001", year=2026, month=8,
                         nurse_id="N001", nurse_name="김민지", user_role="HN",
                         auth_user=make_auth_user())
    res = execute_skill(db, "manage_assignment",
                        {"operation": "create", "kind": "파견", "nurse_name": "박지은",
                         "target_ward": "중환자실2", "start_date": "2026-08-03",
                         "end_date": "2026-08-20", "preview_only": False}, ctx)
    assert res.data.get("ok") is True, res.data

    rows = db.query(PushOutbox).all()
    assert len(rows) == 1, "알림이 대기열에 들어가야 한다"
    assert rows[0].sender_emp_seq_no == "N001"
    assert rows[0].sender_member_id == "acc_N001"
    assert rows[0].recipients == "N002"


# ── 승인 경로 ────────────────────────────────────────────────


def _hn_ctx(month=4) -> SessionContext:
    return SessionContext(office_id="OFF001", group_id="GRP001", year=2026, month=month,
                          nurse_id="N001", nurse_name="김민지", user_role="HN",
                          auth_user=make_auth_user())


def _run_approval(db, ctx):
    from unittest.mock import MagicMock

    from agents_v2.agent_v3 import SchedulingAgent

    return SchedulingAgent(MagicMock(), enable_user_memory=False).run(db, "응", ctx)


def test_approval_ends_with_error_when_schedule_got_issued(db, seed_data):
    """미리보기 뒤 화면에서 마감되면 재승인을 묻지 않고 거절로 끝낸다(재미리보기가 오류)."""
    args = {"scope": "schedule", "group_id": "GRP001", "year": 2026, "month": 4,
            "schedule_id": _SID, "nurse_ids": ["N001"], "date": "2026-04-01",
            "new_shift_code": "N", "preview_only": True}
    ctx = _hn_ctx()
    pv = execute_skill(db, "bulk_mutation", dict(args), ctx)
    assert pv.data.get("preview") is True, pv.data
    ctx.pending_approval = {**pv.data, "skill_name": "bulk_mutation",
                            "args": {k: v for k, v in args.items() if k != "preview_only"}}
    before = _cell(db)
    _issue_seed(db)

    res = _run_approval(db, ctx)
    assert not res.awaiting_approval, res.answer
    assert schedule_tools.ISSUED_OVERWRITE_MSG in res.answer
    assert _cell(db) == before


def _old_publish_preview() -> dict:
    """int-0b 이전 스킬이 저장소에 남긴 발행 승인 대기의 모양."""
    return {"preview": True, "operation": "publish_schedule",
            "summary": {"year": 2026, "month": 4, "schedule_id": _SID, "version": 1},
            "skill_name": "publish_schedule", "args": {"year": 2026, "month": 4}}


def test_stale_publish_pending_is_not_reported_as_done(db, seed_data):
    ctx = _hn_ctx()
    ctx.pending_approval = _old_publish_preview()
    res = _run_approval(db, ctx)
    assert "완료" not in res.answer, res.answer
    assert "마감" in res.answer
    from db.models import IssuedRoster, IssuedRosterSnapshot
    assert db.query(IssuedRoster).count() == 0 and db.query(IssuedRosterSnapshot).count() == 0


def test_batch_with_publish_runs_nothing(db, seed_data):
    ctx = _hn_ctx()
    other = {"preview": True, "skill_name": "bulk_mutation",
             "args": {"scope": "schedule", "group_id": "GRP001", "year": 2026, "month": 4,
                      "schedule_id": _SID, "nurse_ids": ["N001"], "date": "2026-04-01",
                      "new_shift_code": "N"}}
    ctx.pending_approval = {"type": "batch", "count": 2, "items": [other, _old_publish_preview()]}
    before = _cell(db)
    res = _run_approval(db, ctx)
    assert "다른 변경도 실행하지 않았습니다" in res.answer, res.answer
    assert _cell(db) == before


def test_mutate_scope_can_navigate():
    """mutate 로 분류된 턴에도 navigate 가 있어야 '화면으로 안내'를 실제로 할 수 있다."""
    from agents_v2.router import resolve_tools

    tools = set(resolve_tools(["mutate"]))
    assert {"navigate", "publish_schedule", "bulk_mutation"} <= tools
