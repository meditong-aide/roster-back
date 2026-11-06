"""int-0a — 에이전트가 정본 서비스에 '로그인 사용자'를 넘기는가.

배경: dev `1548b08` 이 배정 서비스의 ``current_user=None`` 을 401 로 막았다(로그인 없이
파견·휴직을 조작하던 구멍). 에이전트 스킬은 None 을 넘겨, dev 를 병합하자마자 파견·병동이동
등록·취소가 전부 'Not authenticated' 로 멈췄다(미리보기는 통과 → 승인만 실패).

고친 방식: ``SessionContext.auth_user``(토큰에서 온 User) → 미들웨어가 스킬을 실행하는
동안만 contextvar 로 묶는다. args·감사 기록·승인 대기(Redis·MSSQL)에는 남지 않는다.
dev 의 401 은 그대로 둔다(되돌리면 구멍이 다시 열린다).
"""
from __future__ import annotations

import json
from datetime import date
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException

from agents_v2.agent_v3 import SchedulingAgent
from agents_v2.auth_context import bind_auth_user, current_auth_user
from agents_v2.chat_router import _build_session_ctx
from agents_v2.middleware import execute_skill
from agents_v2.schemas.session_context import SessionContext
from db.models import AgentSkillInvocation, Group, NurseAssignment
from schemas.auth_schema import User as UserSchema
from tests.agent_qa.harness import make_auth_user

_UNSET = object()


@pytest.fixture
def target_ward(db, seed_data):
    db.add(Group(group_id="GRP002", office_id="OFF001", group_name="중환자실2"))
    db.flush()


def _ctx(nurse_id="N001", name="김민지", role="HN", auth_user=_UNSET) -> SessionContext:
    user = make_auth_user(nurse_id, name, role=role) if auth_user is _UNSET else auth_user
    return SessionContext(office_id="OFF001", group_id="GRP001", year=2026, month=8,
                          nurse_id=nurse_id, nurse_name=name, user_role=role, auth_user=user)


def _dispatch(op="create", preview=False) -> dict:
    params = {"operation": op, "kind": "파견", "nurse_name": "박지은", "preview_only": preview}
    if op == "create":
        params.update(target_ward="중환자실2", start_date="2026-08-03", end_date="2026-08-20")
    return params


def _active(db) -> int:
    return db.query(NurseAssignment).filter(NurseAssignment.status == "active").count()


# ── 병합으로 깨졌던 것: 로그인 수간호사의 등록·취소 ─────────────────


def test_create_and_cancel_with_logged_in_head_nurse(db, seed_data, target_ward):
    res = execute_skill(db, "manage_assignment", _dispatch(), _ctx())
    assert res.data.get("ok") is True, res.data
    assert _active(db) == 1

    res = execute_skill(db, "manage_assignment", _dispatch("cancel"), _ctx())
    assert res.data.get("ok") is True, res.data
    assert _active(db) == 0
    assert db.query(NurseAssignment).one().status == "cancelled"


# ── 막혀야 하는 것 ────────────────────────────────────────────


def test_without_auth_user_fails_closed(db, seed_data, target_ward):
    """사용자 객체가 없으면 정본이 401 — 스킬은 오류를 돌려주고 아무것도 만들지 않는다."""
    res = execute_skill(db, "manage_assignment", _dispatch(), _ctx(auth_user=None))
    assert res.data.get("error") == "Not authenticated"
    assert _active(db) == 0


def test_service_still_rejects_missing_user(db, seed_data, target_ward):
    """dev 의 보안 수정(None → 401)을 되돌리지 않았는지."""
    from schemas.roster_schema import NurseAssignmentCreate
    from services import assignment_service

    req = NurseAssignmentCreate(
        nurse_id="N002", source_group_id="GRP001", office_id="OFF001",
        target_group_id="GRP002", start_date=date(2026, 8, 3),
        expected_end_date=date(2026, 8, 20), reason="파견",
    )
    with pytest.raises(HTTPException) as ei:
        assignment_service.create_assignment(req, db, current_user=None, notify=False)
    assert ei.value.status_code == 401


def test_same_ward_nurse_blocked_by_skill_gate(db, seed_data, target_ward):
    """정본 서비스는 같은 병동 일반 간호사도 통과시키므로 스킬의 hn_only 가 막아야 한다."""
    ctx = _ctx("N003", "이수정", role="NURSE")
    res = execute_skill(db, "manage_assignment", _dispatch(), ctx)
    assert res.data.get("permission_denied") is True
    assert _active(db) == 0


# ── 사용자 객체가 실행 밖으로 새지 않는가 ─────────────────────────


def test_auth_user_stays_inside_execution(db, seed_data, target_ward):
    ctx = _ctx()
    res = execute_skill(db, "manage_assignment", _dispatch(), ctx)
    assert res.data.get("ok") is True, res.data

    assert current_auth_user() is None, "실행이 끝나면 묶음이 풀려야 한다"
    assert "auth_user" not in res.grounded_params
    assert not any(isinstance(v, UserSchema) for v in res.grounded_params.values())
    audit = " ".join(r.args_json or "" for r in db.query(AgentSkillInvocation).all())
    assert "acc_N001" not in audit
    assert "acc_N001" not in repr(ctx)


def test_binding_resets_after_exception():
    with pytest.raises(RuntimeError):
        with bind_auth_user("someone"):
            assert current_auth_user() == "someone"
            raise RuntimeError("boom")
    assert current_auth_user() is None


def test_build_session_ctx_carries_cookie_user(db, seed_data):
    user = make_auth_user()
    ctx = _build_session_ctx(db, user, "conv-int0a", 2026, 8)
    assert ctx.auth_user is user
    assert ctx.user_role == "HN"


# ── 승인 턴은 그 턴을 보낸 요청의 사용자로 실행된다 ─────────────────


def _pending_preview(db) -> dict:
    """턴 N: 미리보기 → 승인 대기(agent_v3 가 저장하는 모양과 같게)."""
    args = _dispatch(preview=True)
    pv = execute_skill(db, "manage_assignment", args, _ctx())
    assert pv.data.get("preview") is True, pv.data
    pending = {**pv.data, "skill_name": "manage_assignment", "args": args}
    json.dumps(pending)  # Redis·MSSQL 저장 — 사용자 객체가 섞이면 여기서 TypeError
    return pending


def _approve(db, ctx: SessionContext, pending: dict):
    ctx.pending_approval = pending
    return SchedulingAgent(MagicMock(), enable_user_memory=False).run(db, "응", ctx)


def test_approval_executes_with_requesting_user(db, seed_data, target_ward):
    pending = _pending_preview(db)
    _approve(db, _ctx(), pending)
    assert _active(db) == 1


def test_approval_without_user_does_not_execute(db, seed_data, target_ward):
    """승인 대기에는 사용자가 없다 — 승인 요청에 사용자가 없으면 실행되지 않고, 그렇게 답한다."""
    pending = _pending_preview(db)
    res = _approve(db, _ctx(auth_user=None), pending)
    assert _active(db) == 0
    assert "Not authenticated" in res.answer, res.answer  # '완료' 라고 답하면 거짓 완료
    assert not res.awaiting_approval


# ── 정본에 넘어가는 것은 토큰 사용자 그대로다(간호사 행으로 재구성 금지) ──────


def _spy_current_user(monkeypatch) -> list:
    """정본 create/cancel 이 받은 current_user 를 기록한다(동작은 그대로 위임)."""
    from services import assignment_service

    seen: list = []
    for name in ("create_assignment", "cancel_assignment"):
        original = getattr(assignment_service, name)

        def _wrapped(*args, _original=original, **kwargs):
            seen.append(kwargs.get("current_user"))
            return _original(*args, **kwargs)

        monkeypatch.setattr(assignment_service, name, _wrapped)
    return seen


def test_service_receives_token_user_with_required_fields(db, seed_data, target_ward, monkeypatch):
    """P4 — 권한·알림 발신자가 쓰는 다섯 필드가 채워진 토큰 객체가 그대로 간다."""
    seen = _spy_current_user(monkeypatch)
    ctx = _ctx()
    execute_skill(db, "manage_assignment", _dispatch(), ctx)
    execute_skill(db, "manage_assignment", _dispatch("cancel"), ctx)

    assert len(seen) == 2
    for user in seen:
        assert user is ctx.auth_user
        for f in ("nurse_id", "account_id", "office_id", "group_id", "hn_auth"):
            assert getattr(user, f), f"{f} 가 비어 있다"


def test_admin_without_nurse_row_can_create_and_cancel(db, seed_data, target_ward, monkeypatch):
    """ADM 은 nurses 행이 없다 — 행으로 사용자를 재구성했다면 None 이 되어 401 이 났다."""
    seen = _spy_current_user(monkeypatch)
    adm = make_auth_user("ADM1", "관리자", role="ADM")
    ctx = _ctx("ADM1", "관리자", role="ADM", auth_user=adm)

    res = execute_skill(db, "manage_assignment", _dispatch(), ctx)
    assert res.data.get("ok") is True, res.data
    res = execute_skill(db, "manage_assignment", _dispatch("cancel"), ctx)
    assert res.data.get("ok") is True, res.data

    assert _active(db) == 0
    assert all(u is adm and u.is_master_admin for u in seen)


def test_admin_of_other_hospital_is_rejected(db, seed_data, target_ward):
    """ADM 도 자기 병원 병동만(dev 10-06) — 다른 병원 관리자의 토큰이면 403 으로 막힌다."""
    other = make_auth_user("ADM9", "타병원관리자", role="ADM", office_id="OFF999", group_id="GRP999")
    ctx = _ctx("ADM9", "타병원관리자", role="ADM", auth_user=other)
    res = execute_skill(db, "manage_assignment", _dispatch(), ctx)
    assert "다른 병원" in (res.data.get("error") or ""), res.data
    assert _active(db) == 0
