"""DAG 플랜 커밋 원자성 — 중간 실패 시 전체 롤백(부분 반영 없음)."""
from unittest.mock import MagicMock
from agents_v2.agent_v3 import SchedulingAgent
from agents_v2.schemas.session_context import SessionContext
from db.models import DailyShift


def _ctx():
    return SessionContext(office_id="OFF001", group_id="GRP001", year=2026, month=8,
                          nurse_id="N001", nurse_name="김민지", user_role="HN")


def _agent():
    a = SchedulingAgent(MagicMock(), enable_user_memory=False)
    a._dag_planning = True
    return a


def test_atomic_rollback_on_midplan_failure(db, seed_data):
    # t1: month D=9 (성공·flush) → t2: weekend 인데 counts 없음 → clarify(STOP) → 전체 롤백
    ctx = _ctx()
    ctx.pending_approval = {
        "type": "plan", "user_message": "8월 전체 데이9 주말 데이?",
        "plan": {"tasks": [
            {"id": "t1", "skill": "manage_daily_shift", "kind": "mutate",
             "args": {"scope": "month", "d_count": 9}},
            {"id": "t2", "skill": "manage_daily_shift", "kind": "mutate", "deps": ["t1"],
             "args": {"scope": "weekend"}},  # counts 없음 → clarify → STOP
        ]},
    }
    res = _agent().run(db, "응", ctx)
    assert "롤백" in res.answer or "취소" in res.answer, res.answer
    # t1 의 D=9 가 롤백돼 8월 DailyShift 어디에도 없어야(부분 반영 방지)
    rows = db.query(DailyShift).filter_by(group_id="GRP001", year=2026, month=8).all()
    day_rows = [r for r in rows if int(r.day) > 0]
    assert all(int(r.d_count) != 9 for r in day_rows), "부분 반영됨 — 롤백 실패"
    assert ctx.pending_approval is None


def test_atomic_commit_on_success(db, seed_data):
    # t1: month D=6 → t2: weekend D=2 (둘 다 성공) → 원자 커밋 → 평일6 주말2
    ctx = _ctx()
    ctx.pending_approval = {
        "type": "plan", "user_message": "8월 전체 데이6 주말 데이2",
        "plan": {"tasks": [
            {"id": "t1", "skill": "manage_daily_shift", "kind": "mutate",
             "args": {"scope": "month", "d_count": 6}},
            {"id": "t2", "skill": "manage_daily_shift", "kind": "mutate", "deps": ["t1"],
             "args": {"scope": "weekend", "d_count": 2}},
        ]},
    }
    _agent().run(db, "응", ctx)
    rows = {int(r.day): r for r in
            db.query(DailyShift).filter_by(group_id="GRP001", year=2026, month=8).all()
            if int(r.day) > 0}
    assert rows[3].d_count == 6, f"평일 8/3 D={rows[3].d_count}"   # 평일
    assert rows[2].d_count == 2, f"주말 8/2 D={rows[2].d_count}"   # 주말(override)


# ── async(generate) 은 커밋 후에만, 롤백 시 미실행 ──
def _spy_generate(monkeypatch):
    from agents_v2.skills import registry
    calls = {"n": 0}
    monkeypatch.setitem(registry.SKILL_REGISTRY, "generate-schedule",
                        lambda db, params: (calls.update(n=calls["n"] + 1), {"ok": True, "job_id": "J1"})[1])
    return calls


def test_generate_deferred_after_commit(db, seed_data, monkeypatch):
    calls = _spy_generate(monkeypatch)
    ctx = _ctx()
    ctx.pending_approval = {"type": "plan", "user_message": "설정하고 돌려줘", "plan": {"tasks": [
        {"id": "t1", "skill": "manage_daily_shift", "kind": "mutate", "args": {"scope": "month", "d_count": 6}},
        {"id": "t2", "skill": "generate_schedule", "kind": "mutate", "deps": ["t1"]},
    ]}}
    _agent().run(db, "응", ctx)
    assert calls["n"] == 1, "생성이 커밋 후 실행 안 됨"
    rows = {int(r.day): r for r in db.query(DailyShift).filter_by(group_id="GRP001", year=2026, month=8).all() if int(r.day) > 0}
    assert rows[3].d_count == 6  # 설정도 커밋됨


def test_generate_not_run_on_rollback(db, seed_data, monkeypatch):
    calls = _spy_generate(monkeypatch)
    ctx = _ctx()
    ctx.pending_approval = {"type": "plan", "user_message": "x", "plan": {"tasks": [
        {"id": "t1", "skill": "manage_daily_shift", "kind": "mutate", "args": {"scope": "month", "d_count": 9}},
        {"id": "t2", "skill": "manage_daily_shift", "kind": "mutate", "deps": ["t1"], "args": {"scope": "weekend"}},  # counts 없음 STOP
        {"id": "t3", "skill": "generate_schedule", "kind": "mutate", "deps": ["t1", "t2"]},
    ]}}
    res = _agent().run(db, "응", ctx)
    assert calls["n"] == 0, "롤백인데 생성 실행됨"
    assert "롤백" in res.answer or "취소" in res.answer


# ── #5 승인 staleness: 미리보기와 커밋 시점 다르면 재확인(커밋 안 함) ──
def test_staleness_reconfirm_on_mismatch(db, seed_data):
    ctx = _ctx()
    # 오래된(불일치) 지문 → 현재 dry-run 과 다름 → 재확인
    ctx.pending_approval = {"type": "plan", "user_message": "8월 데이7", "preview_fp": "STALE_FP",
        "plan": {"tasks": [{"id": "t1", "skill": "manage_daily_shift", "kind": "mutate",
                            "args": {"scope": "month", "d_count": 7}}]}}
    res = _agent().run(db, "응", ctx)
    assert res.awaiting_approval is True, "재확인 안 함"
    assert ctx.pending_approval and ctx.pending_approval["type"] == "plan"  # 다시 대기
    # 커밋 안 됨 → d=7 반영 없어야
    rows = [r for r in db.query(DailyShift).filter_by(group_id="GRP001", year=2026, month=8).all() if int(r.day) > 0]
    assert all(int(r.d_count) != 7 for r in rows), "재확인인데 커밋됨"
