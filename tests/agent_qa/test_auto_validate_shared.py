"""P5 — 커밋 후 자동검증을 세 경로가 공유. DAG plan 커밋도 이제 ReAct 처럼 auto-validate.

이전엔 ReAct 커밋만 근무표 셀 mutation 후 validate_schedule 을 돌렸고 DAG plan 커밋은 안 했다.
_is_schedule_mutation 판정 + _auto_validate 공유 헬퍼로 대칭화.
"""
from unittest.mock import MagicMock

from agents_v2.agent_v3 import SchedulingAgent, _is_schedule_mutation
from agents_v2.schemas.session_context import SessionContext


def _ctx():
    return SessionContext(office_id="OFF001", group_id="GRP001", year=2026, month=8,
                          nurse_id="N001", nurse_name="김민지", user_role="HN")


def _agent():
    return SchedulingAgent(MagicMock(), enable_user_memory=False)


# ── predicate ──
def test_is_schedule_mutation_predicate():
    assert _is_schedule_mutation("bulk_mutation", {"scope": "schedule"}) is True
    assert _is_schedule_mutation("bulk-mutation", {"scope": "published_schedule"}) is True
    assert _is_schedule_mutation("bulk_mutation", {"scope": "wanted"}) is False   # 원티드=검증 대상 아님
    assert _is_schedule_mutation("update_constraint", {"scope": "schedule"}) is False  # 설정변경≠셀수정


def _spy(monkeypatch):
    from agents_v2.skills import registry
    from agents_v2.verify import VerifyResult

    registry._ensure_loaded()  # 로드 후 stub — 안 그러면 _ensure_loaded 가 stub 을 덮어씀
    # bulk_mutation 은 L1 read-back 이 실제 DB 변화를 요구 → stub 이면 실패해 커밋 abort.
    # P5(자동검증)만 검증하려는 것이므로 read-back 을 무해 통과로 중립화.
    monkeypatch.setattr("agents_v2.verify.run_readback",
                        lambda db, s, p, r: VerifyResult(True))
    calls = {"validate": 0}
    monkeypatch.setitem(registry.SKILL_REGISTRY, "bulk-mutation",
                        lambda db, p: {"ok": True, "scope": p.get("scope")})
    monkeypatch.setitem(
        registry.SKILL_REGISTRY, "validate-schedule",
        lambda db, p: (calls.update(validate=calls["validate"] + 1), {"violation_count": 0})[1])
    return calls


def test_dag_commit_auto_validates_schedule_mutation(db, seed_data, monkeypatch):
    # 스케줄 셀 mutation 을 plan 으로 커밋 → 커밋 후 자동검증 1회
    calls = _spy(monkeypatch)
    ctx = _ctx()
    ctx.pending_approval = {"type": "plan", "user_message": "8월 3일 근무 바꿔",
        "plan": {"tasks": [
            {"id": "t1", "skill": "bulk_mutation", "kind": "mutate", "args": {"scope": "schedule"}},
        ]}}
    _agent().run(db, "응", ctx)
    assert calls["validate"] == 1, "DAG 커밋 후 자동검증 안 함"


def test_dag_commit_skips_autovalidate_when_plan_has_validate(db, seed_data, monkeypatch):
    # 플랜이 이미 validate task 를 포함하면 중복 자동검증 안 함(validate 는 plan 실행 중 1회만)
    calls = _spy(monkeypatch)
    ctx = _ctx()
    ctx.pending_approval = {"type": "plan", "user_message": "바꾸고 검증",
        "plan": {"tasks": [
            {"id": "t1", "skill": "bulk_mutation", "kind": "mutate", "args": {"scope": "schedule"}},
            {"id": "t2", "skill": "validate_schedule", "kind": "read", "deps": ["t1"]},
        ]}}
    _agent().run(db, "응", ctx)
    # plan 안의 t2 로 1회, 커밋 후 자동검증은 스킵 → 총 1회 (중복 없음)
    assert calls["validate"] == 1, f"중복 검증(={calls['validate']}) — 자동검증 스킵 실패"
