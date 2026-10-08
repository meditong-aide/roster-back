"""병동이동 × 발행본 가드 (services/issued_month_guard · assignment_service) — dev-7.

배경: 발행본은 간호사에게 이미 배포된 확정 근무표다. 발행본 조회는 살아 있는 병동이동을 출발·도착
병동 양쪽 표에 덧씌우므로, 원장(nurse_assignments)만 바꾸면 배포된 그 달 표가 바뀐다.
가드가 **서비스단**에 있어야 에이전트·프론트 REST 두 경로가 같이 막힌다.

결정
- (2026-08-26) 발행본만 막고 **과거월 자체는 통과**시킨다(소급 정정 허용).
- (Q6, 2026-10-08) 대상 달을 판정할 수 없으면 막고 안내한다(fail-closed).
- (Q48, 2026-10-08) 보내는(source)·받는(target) 병동 둘 다 본다.
연월은 meta_json 이 정본이다(year/month 컬럼은 nullable). 모든 호출은 실제 로그인 사용자(UserSchema)로 한다.
"""
from datetime import date

import pytest
from fastapi import HTTPException
from sqlalchemy import text

from agents_v2.errors import ErrorType, classify
from agents_v2.middleware import execute_skill
from agents_v2.schemas.session_context import SessionContext
from db.models import Group, IssuedRosterSnapshot, NurseAssignment, Schedule
from schemas.roster_schema import NurseAssignmentCreate, NurseAssignmentUpdate
from services import assignment_service, issued_month_guard, schedule_versioning
from services.issued_month_guard import (
    CODE_BLOCKED, CODE_UNAVAILABLE, CODE_UNKNOWN, TransferState, transfer_guard_scope,
)
from tests.agent_qa.harness import make_auth_user

_AUTO = object()


def _hn():
    return SessionContext(office_id="OFF001", group_id="GRP001", year=2026, month=8,
                          nurse_id="N001", nurse_name="김민지", user_role="HN",
                          auth_user=make_auth_user())


@pytest.fixture
def target_ward(db, seed_data):
    db.add(Group(group_id="GRP002", office_id="OFF001", group_name="중환자실2"))
    db.add(Group(group_id="GRP003", office_id="OFF001", group_name="외과3"))
    db.flush()


def _issue(db, group_id, year, month, *, active=True, sid=None, meta=_AUTO, cols=_AUTO):
    """그 달 발행본 스냅샷 1건. 연월 정본은 meta_json — cols 로 nullable 컬럼을 따로 줄 수 있다."""
    sid = sid or f"ISS{group_id[-3:]}{year % 100:02d}{month:02d}"
    db.add(Schedule(schedule_id=sid, office_id="OFF001", group_id=group_id,
                    year=year, month=month, version=1, status="issued", dropped=False))
    db.flush()
    y, m = (year, month) if cols is _AUTO else cols
    db.add(IssuedRosterSnapshot(office_id="OFF001", group_id=group_id, schedule_id=sid, version=1,
                                is_active_issued=active, year=y, month=m,
                                meta_json={"year": year, "month": month} if meta is _AUTO else meta))
    db.flush()


def _transfer(nurse="박지은", start="2026-09-01", preview=False):
    return {"operation": "create", "kind": "병동이동", "nurse_name": nurse,
            "target_ward": "중환자실2", "start_date": start, "preview_only": preview}


def _create_req(start=date(2026, 9, 1), target="GRP002", nurse="N002"):
    return NurseAssignmentCreate(nurse_id=nurse, source_group_id="GRP001", office_id="OFF001",
                                 target_group_id=target, start_date=start, reason="병동이동")


def _create(db, **kw):
    return assignment_service.create_assignment(_create_req(**kw), db, current_user=make_auth_user(), notify=False)


def _row(db, *, start, end=None, nurse="N002", target="GRP002", status="active"):
    """이미 있는 병동이동 행(직접 넣는다 — 발행 달을 덮는 행도 만들 수 있게)."""
    r = NurseAssignment(nurse_id=nurse, source_group_id="GRP001", target_group_id=target, office_id="OFF001",
                        start_date=start, expected_end_date=end, reason="병동이동", kind="transfer", status=status)
    db.add(r)
    db.flush()
    return r


def _update(db, row, **change):
    return assignment_service.update_assignment(row.id, NurseAssignmentUpdate(**change), db,
                                                current_user=make_auth_user())


def _status_of(fn):
    try:
        fn()
    except HTTPException as e:
        return e.status_code
    return 200


# ── 차단 ────────────────────────────────────────────────


def test_blocked_when_effective_month_is_issued(db, seed_data, target_ward):
    _issue(db, "GRP001", 2026, 9)
    res = execute_skill(db, "manage_assignment", _transfer(start="2026-09-01"), _hn())
    assert "error" in res.data
    assert "발행된 근무표" in res.data["error"]
    assert db.query(NurseAssignment).count() == 0


def test_blocked_when_later_month_is_issued(db, seed_data, target_ward):
    """발효월 이후 달이 발행돼 있어도 막는다 — 그 표도 옛 소속 기준이다."""
    _issue(db, "GRP001", 2026, 11)
    res = execute_skill(db, "manage_assignment", _transfer(start="2026-09-01"), _hn())
    assert "error" in res.data and "2026년 11월" in res.data["error"]


def test_error_lists_blocking_months(db, seed_data, target_ward):
    _issue(db, "GRP001", 2026, 9, sid="SCHISS0009")
    _issue(db, "GRP001", 2026, 10, sid="SCHISS0010")
    res = execute_skill(db, "manage_assignment", _transfer(start="2026-09-01"), _hn())
    assert "2026년 9월" in res.data["error"] and "2026년 10월" in res.data["error"]


def test_target_ward_issue_blocks(db, seed_data, target_ward):
    """★ Q48 — 받는 병동 발행본도 막는다(받는 병동 표에도 이동자가 실린다). 문구는 병동 이름으로."""
    _issue(db, "GRP002", 2026, 9)
    with pytest.raises(HTTPException) as ei:
        _create(db)
    assert ei.value.status_code == 409 and ei.value.detail["code"] == CODE_BLOCKED
    assert "중환자실2 2026년 9월" in ei.value.detail["message"]
    assert ei.value.detail["months"] == [{"group_id": "GRP002", "group_name": "중환자실2",
                                          "year": 2026, "month": 9}]


# ── 통과 ────────────────────────────────────────────────


def test_allowed_when_issued_month_is_before_effective(db, seed_data, target_ward):
    """발효월 이전 발행본은 무관 — 그 달은 옛 소속이 맞다."""
    _issue(db, "GRP001", 2026, 7)
    res = execute_skill(db, "manage_assignment", _transfer(start="2026-09-01"), _hn())
    assert res.data.get("ok") is True, res.data


def test_allowed_when_issue_deactivated(db, seed_data, target_ward):
    """발행 취소(is_active_issued=False)면 다시 이동 가능해야 한다."""
    _issue(db, "GRP001", 2026, 9, active=False)
    res = execute_skill(db, "manage_assignment", _transfer(start="2026-09-01"), _hn())
    assert res.data.get("ok") is True, res.data


def test_past_month_still_allowed(db, seed_data, target_ward):
    """★ 결정: 과거월 자체는 막지 않는다(소급 정정 허용)."""
    res = execute_skill(db, "manage_assignment", _transfer(start="2026-03-01"), _hn())
    assert res.data.get("ok") is True, res.data
    row = db.query(NurseAssignment).first()
    assert row.start_date == date(2026, 3, 1)


def test_draft_schedule_does_not_block(db, seed_data, target_ward):
    """초안(발행 안 된 Schedule)만 있으면 막지 않는다 — 재생성하면 되니까."""
    db.add(Schedule(schedule_id="SCHDRAFT01", office_id="OFF001", group_id="GRP001",
                    year=2026, month=9, version=1, status="draft", dropped=False))
    db.flush()
    res = execute_skill(db, "manage_assignment", _transfer(start="2026-09-01"), _hn())
    assert res.data.get("ok") is True, res.data


def test_unrelated_ward_issue_does_not_block(db, seed_data, target_ward):
    """출발·도착 어느 쪽도 아닌 병동의 발행본은 무관하다."""
    _issue(db, "GRP003", 2026, 9)
    res = execute_skill(db, "manage_assignment", _transfer(start="2026-09-01"), _hn())
    assert res.data.get("ok") is True, res.data


def test_dispatch_not_blocked(db, seed_data, target_ward):
    """파견은 이 가드 대상이 아니다(임시 근무, 소속 유지 — 결정 유지)."""
    _issue(db, "GRP001", 2026, 9)
    res = execute_skill(db, "manage_assignment",
                        {"operation": "create", "kind": "파견", "nurse_name": "박지은",
                         "target_ward": "중환자실2", "start_date": "2026-09-01",
                         "end_date": "2026-09-30", "preview_only": False}, _hn())
    assert res.data.get("ok") is True, res.data


def test_guard_lives_in_service_not_only_skill(db, seed_data, target_ward):
    """스킬을 우회해 서비스를 직접 불러도 막혀야 한다(프론트 REST 경로 등가)."""
    _issue(db, "GRP001", 2026, 9)
    with pytest.raises(HTTPException) as ei:
        _create(db)
    assert ei.value.status_code == 409
    assert "발행된 근무표" in str(ei.value.detail)


# ── 연월 정본 = meta_json ───────────────────────────────


def test_meta_json_wins_over_columns(db, seed_data, target_ward):
    """컬럼은 9월이어도 meta 가 7월이면 7월 발행본이다 → 9월 이동은 통과."""
    _issue(db, "GRP001", 2026, 9, meta={"year": 2026, "month": 7})
    assert _create(db).nurse_id == "N002"


def test_null_columns_meta_issued_blocks(db, seed_data, target_ward):
    """컬럼이 NULL 이어도 meta 가 9월이면 막는다(예전 가드는 isnot(None) 으로 놓쳤다)."""
    _issue(db, "GRP001", 2026, 9, cols=(None, None))
    with pytest.raises(HTTPException) as ei:
        _create(db)
    assert ei.value.status_code == 409 and ei.value.detail["code"] == CODE_BLOCKED


def test_meta_json_string_parsed(db, seed_data, target_ward):
    _issue(db, "GRP001", 2026, 9, meta='{"year": 2026, "month": 9}')
    with pytest.raises(HTTPException) as ei:
        _create(db)
    assert ei.value.detail["code"] == CODE_BLOCKED


# ── fail-closed (Q6) ────────────────────────────────────


@pytest.mark.parametrize("meta", [None, {"year": "2026", "month": 9}, "{깨짐",
                                  {"year": 2026, "month": 13}, {"year": True, "month": 9}, {"month": 9}])
def test_unreadable_meta_blocks(db, seed_data, target_ward, meta):
    """연월을 읽을 수 없는 활성 발행본이 있으면 어느 달인지 모르니 막는다(기간과 무관)."""
    _issue(db, "GRP002", 2025, 1, meta=meta)
    with pytest.raises(HTTPException) as ei:
        _create(db, start=date(2026, 9, 1))
    assert ei.value.status_code == 409 and ei.value.detail["code"] == CODE_UNKNOWN
    assert "중환자실2" in ei.value.detail["message"] and "확인" in ei.value.detail["message"]
    assert db.query(NurseAssignment).count() == 0


def test_check_failure_is_503_and_session_clean(db, seed_data, target_ward, monkeypatch):
    """조회가 DB 오류로 실패하면 503(삼키지 않음) — savepoint 만 되감아 호출자의 미커밋 작업과 세션은 산다."""
    monkeypatch.setattr(issued_month_guard, "_read_active_snapshots",
                        lambda _db, _gids: _db.execute(text("SELECT x FROM no_such_table")).all())
    db.add(Group(group_id="GRPP", office_id="OFF001", group_name="앞선작업"))
    db.flush()
    with pytest.raises(HTTPException) as ei:
        _create(db)
    assert ei.value.status_code == 503 and ei.value.detail["code"] == CODE_UNAVAILABLE
    db.add(Group(group_id="GRPX", office_id="OFF001", group_name="후속작업"))
    db.flush()
    assert db.query(Group).filter(Group.group_id.in_(["GRPP", "GRPX"])).count() == 2
    assert db.query(NurseAssignment).count() == 0


# ── 잠금 뒤 판정 ─────────────────────────────────────────


def _record(monkeypatch):
    events = []
    real_read = issued_month_guard._read_active_snapshots
    real_row = assignment_service._lock_assignment_row
    monkeypatch.setattr(schedule_versioning, "lock_month_publish",
                        lambda _db, g, y, m: events.append(("lock", g, y, m)))
    monkeypatch.setattr(issued_month_guard, "_read_active_snapshots",
                        lambda _db, gids: events.append(("read",)) or real_read(_db, gids))
    monkeypatch.setattr(assignment_service, "_lock_assignment_row",
                        lambda _db, aid: events.append(("row",)) or real_row(_db, aid))
    return events


def _schedules(db, *keys):
    for g, y, m in keys:
        db.add(Schedule(schedule_id=f"S{g[-3:]}{y % 100:02d}{m:02d}", office_id="OFF001", group_id=g,
                        year=y, month=m, version=1, status="draft", dropped=False))
    db.flush()


def test_publish_lock_taken_before_read(db, seed_data, target_ward, monkeypatch):
    """마감·철회와 같은 달 잠금을 (병동, 연, 월) 정렬 순서로 다 잡은 **뒤에** 발행본을 읽는다."""
    _schedules(db, ("GRP002", 2026, 9), ("GRP001", 2026, 10), ("GRP001", 2026, 9),
               ("GRP001", 2026, 8), ("GRP003", 2026, 9))
    events = _record(monkeypatch)
    _create(db, start=date(2026, 9, 1))
    assert events == [("lock", "GRP001", 2026, 9), ("lock", "GRP001", 2026, 10),
                      ("lock", "GRP002", 2026, 9), ("read",)]


def test_update_locks_row_then_months(db, seed_data, target_ward, monkeypatch):
    _schedules(db, ("GRP001", 2026, 11))
    row = _row(db, start=date(2026, 9, 1), end=date(2026, 10, 31))
    events = _record(monkeypatch)
    _update(db, row, expected_end_date=date(2026, 11, 30))
    assert events == [("row",), ("lock", "GRP001", 2026, 11), ("read",)]


# ── update_assignment ────────────────────────────────────


def test_update_end_into_issued_month_blocked(db, seed_data, target_ward):
    _issue(db, "GRP001", 2026, 12)
    row = _row(db, start=date(2026, 9, 1), end=date(2026, 10, 31))
    assert _status_of(lambda: _update(db, row, expected_end_date=date(2026, 12, 31))) == 409
    assert db.get(NurseAssignment, row.id).expected_end_date == date(2026, 10, 31)


def test_update_end_shrink_out_of_issued_blocked(db, seed_data, target_ward):
    """발행 달에서 빠지는 것도 그 달 표를 바꾼다 → 막는다."""
    _issue(db, "GRP002", 2026, 12)
    row = _row(db, start=date(2026, 9, 1), end=date(2026, 12, 31))
    assert _status_of(lambda: _update(db, row, expected_end_date=date(2026, 11, 30))) == 409


def test_update_outside_issued_allowed(db, seed_data, target_ward):
    """이미 발행 달(9월)을 덮는 행이라도 바뀌는 날(11월)이 발행 달이 아니면 통과 — 대칭차만 본다."""
    _issue(db, "GRP001", 2026, 9)
    row = _row(db, start=date(2026, 9, 1), end=date(2026, 10, 31))
    _update(db, row, expected_end_date=date(2026, 11, 30))
    assert db.get(NurseAssignment, row.id).expected_end_date == date(2026, 11, 30)


def test_update_note_only_not_judged(db, seed_data, target_ward):
    _issue(db, "GRP001", 2026, 9)
    row = _row(db, start=date(2026, 9, 1))
    _update(db, row, note="메모만")
    assert db.get(NurseAssignment, row.id).note == "메모만"


def test_update_target_change_checks_new_target(db, seed_data, target_ward):
    _issue(db, "GRP003", 2026, 10)
    row = _row(db, start=date(2026, 9, 1))
    assert _status_of(lambda: _update(db, row, target_group_id="GRP003")) == 409
    assert db.get(NurseAssignment, row.id).target_group_id == "GRP002"


def test_update_revive_cancelled_blocked(db, seed_data, target_ward):
    _issue(db, "GRP001", 2026, 9)
    row = _row(db, start=date(2026, 9, 1), status="cancelled")
    assert _status_of(lambda: _update(db, row, status="active")) == 409


def test_cancel_and_update_cancelled_agree(db, seed_data, target_ward):
    """발행된 달에 걸린 병동이동은 취소 API 도, 수정 status=cancelled 도 막는다(사용자 결정 10-08 — 우회 금지)."""
    _issue(db, "GRP001", 2026, 9)
    a = _row(db, start=date(2026, 9, 1))
    b = _row(db, start=date(2026, 9, 1), nurse="N003")
    got = [_status_of(lambda: assignment_service.cancel_assignment(a.id, db, current_user=make_auth_user())),
           _status_of(lambda: _update(db, b, status="cancelled"))]
    assert got == [409, 409]
    assert db.get(NurseAssignment, a.id).status == "active" and db.get(NurseAssignment, b.id).status == "active"


@pytest.mark.parametrize("old, new, spans", [
    ((date(2026, 12, 1), None), (date(2026, 11, 1), None), [(date(2026, 11, 1), date(2026, 11, 30))]),
    ((date(2026, 11, 1), None), (date(2026, 12, 1), None), [(date(2026, 11, 1), date(2026, 11, 30))]),
    ((date(2026, 9, 1), date(2026, 10, 31)), (date(2026, 9, 1), None), [(date(2026, 11, 1), None)]),
    ((date(2026, 9, 1), date(2026, 9, 30)), (date(2026, 12, 1), date(2026, 12, 31)),
     [(date(2026, 9, 1), date(2026, 9, 30)), (date(2026, 12, 1), date(2026, 12, 31))]),
    ((date(2026, 9, 1), None), (date(2026, 9, 1), None), []),
])
def test_scope_is_symmetric_difference(old, new, spans):
    """시작일을 당기든 미루든 바뀐 날만 본다(시작일 수정은 지금 체인 검증 400 에 먼저 걸려 범위로 확인)."""
    def st(span):
        return TransferState("병동이동", "active", "GRP001", "GRP002", *span)
    scope = transfer_guard_scope(st(old), st(new))
    assert list(scope.spans) == spans and bool(scope) is bool(spans)


def test_scope_resent_reason_is_empty():
    """프론트는 병동이동 수정에 reason 을 늘 다시 보낸다 — 값이 같으면 판정하지 않는다."""
    row = NurseAssignment(nurse_id="N002", source_group_id="GRP001", target_group_id="GRP002",
                          start_date=date(2026, 9, 1), reason="병동이동", status="active")
    req = NurseAssignmentUpdate(reason="병동이동", note="x", target_team_id=2)
    assert not assignment_service.update_guard_scope(row, req)


# ── 미리보기 = 적용 (완료 기준: 미리보기 OK → 적용 409 사례 0) ──


@pytest.mark.parametrize("issue", [
    dict(group_id="GRP001", year=2026, month=9),                 # 출발 병동 발행
    dict(group_id="GRP002", year=2026, month=9),                 # 도착 병동 발행
    dict(group_id="GRP001", year=2026, month=7),                 # 발효월 이전
    dict(group_id="GRP001", year=2026, month=9, active=False),   # 철회됨
    dict(group_id="GRP002", year=2026, month=9, meta=None),      # 판정 불가
    dict(group_id="GRP003", year=2026, month=9),                 # 무관한 병동
])
def test_preview_matches_apply(db, seed_data, target_ward, issue):
    _issue(db, **issue)
    guard = assignment_service.preview_assignment_impact(
        db, nurse_id="N002", reason="병동이동", start_date=date(2026, 9, 1), target_group_id="GRP002",
    )["issued_guard"]
    applied = _status_of(lambda: _create(db))
    assert guard["blocked"] is (applied == 409)
    assert applied in (200, 409)


@pytest.mark.parametrize("issued_month, new_end", [(12, date(2026, 12, 31)), (9, date(2026, 11, 30))])
def test_preview_matches_apply_for_update(db, seed_data, target_ward, issued_month, new_end):
    """exclude_id 로 수정 미리보기 — 수정과 같은 의미(바뀐 날만)로 판정한다."""
    _issue(db, "GRP001", 2026, issued_month)
    row = _row(db, start=date(2026, 9, 1), end=date(2026, 10, 31))
    guard = assignment_service.preview_assignment_impact(
        db, nurse_id="N002", reason="병동이동", start_date=date(2026, 9, 1), target_group_id="GRP002",
        expected_end_date=new_end, exclude_id=row.id,
    )["issued_guard"]
    applied = _status_of(lambda: _update(db, row, expected_end_date=new_end))
    assert guard["blocked"] is (applied == 409)


def test_preview_matches_apply_for_chained_transfer(db, seed_data, target_ward):
    """진행 중인 이동(9~10월 GRP002)에 이어지는 다음 이동은 출발 병동이 GRP002 다 — 미리보기도 같은 병동을 본다."""
    _row(db, start=date(2026, 9, 1), end=date(2026, 10, 31))
    _issue(db, "GRP002", 2026, 11)
    guard = assignment_service.preview_assignment_impact(
        db, nurse_id="N002", reason="병동이동", start_date=date(2026, 11, 1), target_group_id="GRP003",
    )["issued_guard"]
    req = NurseAssignmentCreate(nurse_id="N002", source_group_id="GRP002", office_id="OFF001",
                                target_group_id="GRP003", start_date=date(2026, 11, 1), reason="병동이동")
    admin = make_auth_user("ADM1", "관리자", role="ADM")   # 출발이 GRP002 라 9병동 수간호사 권한 밖
    applied = _status_of(lambda: assignment_service.create_assignment(req, db, current_user=admin, notify=False))
    assert guard["blocked"] is True and applied == 409


def test_preview_check_failure_is_blocked_not_raised(db, seed_data, target_ward, monkeypatch):
    monkeypatch.setattr(issued_month_guard, "_read_active_snapshots",
                        lambda _db, _gids: _db.execute(text("SELECT x FROM no_such_table")).all())
    impact = assignment_service.preview_assignment_impact(
        db, nurse_id="N002", reason="병동이동", start_date=date(2026, 9, 1), target_group_id="GRP002")
    assert impact["issued_guard"]["blocked"] is True
    assert impact["issued_guard"]["code"] == CODE_UNAVAILABLE
    assert db.query(Group).count() >= 3  # 같은 세션으로 다음 쿼리가 된다


def test_agent_preview_blocked_is_error(db, seed_data, target_ward):
    """에이전트 미리보기가 막히면 승인 카드(PREVIEW) 대신 오류 — 승인 뒤 409 가 생기지 않는다."""
    _issue(db, "GRP002", 2026, 9)
    res = execute_skill(db, "manage_assignment", _transfer(start="2026-09-01", preview=True), _hn())
    assert "preview" not in res.data and classify(res.data) is not ErrorType.PREVIEW
    assert "발행된 근무표" in res.data["error"] and "2026년 9월" in res.data["error"]


def test_agent_create_error_message_is_text(db, seed_data, target_ward):
    """실행 오류는 str(dict) 가 아니라 message 문구로 보인다."""
    _issue(db, "GRP001", 2026, 9)
    res = execute_skill(db, "manage_assignment", _transfer(start="2026-09-01"), _hn())
    assert res.data["error"].startswith("이미 발행된 근무표") and CODE_BLOCKED not in res.data["error"]


def test_agent_cancel_preview_follows_cancel_rule(db, seed_data, target_ward):
    """에이전트 취소 미리보기도 쓰기와 같은 판정 — 막히면 승인 카드 대신 안내."""
    _issue(db, "GRP001", 2026, 9)
    _row(db, start=date(2026, 9, 1))
    res = execute_skill(db, "manage_assignment", {"operation": "cancel", "kind": "병동이동",
                                                  "nurse_name": "박지은", "preview_only": True}, _hn())
    assert "error" in res.data and "발행된 근무표" in res.data["error"], res.data


# ── 잠금 → 행 · 트랜잭션 소실 (dev-7 리뷰) ─────────────────────────────────


def test_month_lock_before_caller_pending_write_flush(db, seed_data, target_ward, monkeypatch):
    """호출자의 미반영 쓰기가 마감 달 잠금보다 먼저 DB 에 나가지 않는다(잠금 → 행).

    마감은 그 달 잠금을 쥔 채 그 병동 간호사·팀을 읽는다(RCSI 꺼짐). 우리 행이 먼저 잠기고 잠금을 기다리면 교착한다.
    """
    from sqlalchemy import event

    _schedules(db, ("GRP001", 2026, 9))
    events = _record(monkeypatch)
    db.get(Group, "GRP003").group_name = "외과3-변경"   # 호출자의 미반영 쓰기

    def _spy(conn, cursor, statement, params, context, executemany):
        if statement.strip().upper().startswith("UPDATE GROUPS"):
            events.append(("UPDATE groups",))

    engine = db.get_bind()
    event.listen(engine, "before_cursor_execute", _spy)
    try:
        issued_month_guard.assert_transfer_not_on_issued(
            db, issued_month_guard.GuardScope.of(["GRP001", "GRP002"], [(date(2026, 9, 1), None)]))
    finally:
        event.remove(engine, "before_cursor_execute", _spy)
    assert events == [("lock", "GRP001", 2026, 9), ("UPDATE groups",), ("read",)]


def test_lock_phase_db_error_is_503_and_session_reset(db, seed_data, target_ward, monkeypatch):
    """잠금 단계는 savepoint 밖이라 DB 오류 뒤 트랜잭션 상태를 장담할 수 없다 — 전체 rollback 뒤 503, 세션은 다시 쓸 수 있다."""
    from sqlalchemy.exc import OperationalError

    def _boom(_db, _scope):
        raise OperationalError("SELECT schedules", {}, Exception("연결 끊김"))

    monkeypatch.setattr(issued_month_guard, "_publish_months_to_lock", _boom)
    with pytest.raises(HTTPException) as ei:
        _create(db)
    assert ei.value.status_code == 503 and ei.value.detail["code"] == CODE_UNAVAILABLE
    assert not db.in_transaction()          # 전체 rollback 됐다
    db.query(Group).count()                 # 같은 세션으로 다음 쿼리가 된다


def _tiny_session():
    """서버 쪽 트랜잭션 소실을 흉내 낼 독립 SQLite 세션 — 표시 문(MARK)을 만나면 DBAPI 트랜잭션을 날리고 실패시킨다."""
    from sqlalchemy import Column, Integer, String, create_engine, event
    from sqlalchemy.orm import Session, declarative_base
    from sqlalchemy.pool import StaticPool

    base = declarative_base()

    class Probe(base):
        __tablename__ = "guard_probe"
        id = Column(Integer, primary_key=True)
        name = Column(String)

    engine = create_engine("sqlite://", poolclass=StaticPool)
    base.metadata.create_all(engine)
    Group.__table__.create(engine)

    def _kill_txn(conn, cursor, statement, params, context, executemany):
        if "guard_mark" in statement or "FROM groups" in statement:
            cursor.connection.rollback()          # 서버가 트랜잭션을 날린 상황(교착 희생 흉내)
            return "SELECT x FROM no_such_table", params
        return statement, params

    event.listen(engine, "before_cursor_execute", _kill_txn, retval=True)
    return Session(bind=engine), Probe


@pytest.mark.parametrize("call", ["guarded", "group_names"])
def test_server_side_rollback_resets_session(call):
    """savepoint 되감기가 실패하면(트랜잭션 소실) 전체 rollback — SA 상태가 서버와 맞는다.

    예전엔 is_active(늘 True)로 판정해 rollback 하지 않았다 — 사라진 행이 세션엔 persistent 로 남고, 뒤 commit 은
    앞 작업 없이 뒷부분만 반영됐다(미리보기가 503 을 삼키는 에이전트 턴).
    """
    from sqlalchemy import inspect

    db, probe = _tiny_session()
    first = probe(id=1, name="앞선작업")
    db.add(first)
    db.flush()
    if call == "guarded":
        with pytest.raises(HTTPException) as ei:
            issued_month_guard._guarded(db, lambda: db.execute(text("SELECT 1 AS guard_mark")).all())
        assert ei.value.status_code == 503 and ei.value.detail["code"] == CODE_UNAVAILABLE
    else:
        assert issued_month_guard._group_names(db, ["GRP001"]) == {}
    assert not inspect(first).persistent      # 서버에서 사라진 행을 세션도 버렸다
    assert not db.in_transaction()
    db.add(probe(id=2, name="후속작업"))
    db.commit()                                # 세션을 다시 쓸 수 있다
    assert [r.id for r in db.query(probe).all()] == [2]


def test_cancel_transfer_outside_issued_months_passes(db, seed_data, target_ward):
    """발행된 달에 걸리지 않는 병동이동 취소는 그대로 된다(취소 판정의 정상 경로)."""
    _issue(db, "GRP001", 2026, 8)
    row = _row(db, start=date(2026, 10, 1))
    assert _status_of(lambda: assignment_service.cancel_assignment(row.id, db, current_user=make_auth_user())) == 200
    assert db.get(NurseAssignment, row.id).status == "cancelled"
