"""assignment 알림 정책 검증 — 파견·병동이동만 **당사자에게** 보낸다(사용자 결정 2026-10-07).

7-15(d10862a)에 배정 알림을 전부 껐다가, 근무 병동이 바뀌는 배정은 당사자 간호사에게만 다시 보내기로 했다.
  - 파견·병동이동: 생성(S06) · 수정(S06/S07) · 취소(S07) · 병동이동 완료(S08) → 수신자는 당사자 1명
  - 휴직·프리셉티 등 그 밖의 사유, 관리자 알림 → 보내지 않는다
  - 알림은 업무 저장과 같은 커밋으로 발송 대기열에 넣는다 — 직접 발송(`set_app_push`)은 하지 않는다

대기열 등록(`utils._send_or_enqueue`)을 가로채 무엇이 누구에게 쌓였는지 본다. 직접 발송이 없다는 것은
비운영에서 `set_app_push` 가 찍는 `[push]` 줄이 없는 것으로 확인한다.
"""
from datetime import date, timedelta
from types import SimpleNamespace

import pytest

from db.models import Group
from schemas.roster_schema import NurseAssignmentCreate, NurseAssignmentUpdate
from services.assignment_service import (
    cancel_assignment,
    create_assignment,
    flush_pending_transfers,
    update_assignment,
)


# 배정 조작은 로그인 사용자를 요구한다(None=미인증 → 401). 권한 판정이 아니라 동작을 보는
# 테스트라 마스터 관리자로 통과시킨다. 관리자도 자기 병원 병동만 다루므로(10-06) 픽스처 병원을 준다.
_ADMIN = SimpleNamespace(is_master_admin=True, office_id="OFF001")


def _add_second_group(db):
    """OFF001 소속 target 병동(GRP002) 추가 — 병동이동/파견 목적지."""
    if not db.query(Group).filter(Group.group_id == "GRP002").first():
        db.add(Group(group_id="GRP002", office_id="OFF001", group_name="10병동"))
        db.flush()


def _push_lines(captured) -> list[str]:
    return [ln for ln in captured.out.splitlines() if "[push]" in ln]


@pytest.fixture
def queued(monkeypatch):
    """발송 대기열에 넣으려던 알림 목록 — `utils._send_or_enqueue` 를 가로챈다."""
    import utils.utils as uu

    rows: list[dict] = []

    def _record(db, **kw):
        rows.append(kw)
        return {"result": "queued"}

    monkeypatch.setattr(uu, "_send_or_enqueue", _record)
    return rows


def _create(db, nurse_id, reason, target, *, start=None, notify=True):
    req = NurseAssignmentCreate(
        nurse_id=nurse_id, source_group_id="GRP001", target_group_id=target,
        office_id="OFF001", start_date=start or date.today() + timedelta(days=1),
        expected_end_date=None if reason == "병동이동" else date.today() + timedelta(days=30),
        reason=reason,
    )
    return create_assignment(req, db, current_user=_ADMIN, notify=notify)


@pytest.mark.parametrize("nurse_id,reason", [("N002", "파견"), ("N003", "병동이동")])
def test_create_inbound_notifies_only_the_nurse(seed_data, db, capsys, queued, nurse_id, reason):
    """파견·병동이동 생성(S06): 당사자 1명에게만, 대기열로."""
    _add_second_group(db)
    capsys.readouterr()

    _create(db, nurse_id, reason, "GRP002")

    assert [(q["push_sub_code"], q["recipients"]) for q in queued] == [("S06", [nurse_id])]
    assert queued[0]["message"].startswith(f"{reason} 배정: 10병동 (")
    assert not queued[0].get("link_code")  # 연결 코드 없음 — 알림 목록에서 근무표 알림과 묶이지 않게
    assert _push_lines(capsys.readouterr()) == []


@pytest.mark.parametrize("nurse_id,reason", [("N004", "휴직"), ("N005", "프리셉티")])
def test_create_other_reasons_no_push(seed_data, db, capsys, queued, nurse_id, reason):
    """휴직·프리셉티 생성: 알림 없음."""
    capsys.readouterr()
    _create(db, nurse_id, reason, None)

    assert queued == []
    assert _push_lines(capsys.readouterr()) == []


def test_create_notify_false_no_push(seed_data, db, queued):
    """notify=False 면 파견이어도 알림 없음."""
    _add_second_group(db)
    _create(db, "N002", "파견", "GRP002", notify=False)
    assert queued == []


def test_cancel_notifies_only_the_nurse(seed_data, db, capsys, queued):
    """진행 중 파견 취소(S07): 당사자에게만."""
    _add_second_group(db)
    created = _create(db, "N006", "파견", "GRP002")
    queued.clear()
    capsys.readouterr()

    cancel_assignment(created.id, db, current_user=_ADMIN)

    assert [(q["push_sub_code"], q["recipients"]) for q in queued] == [("S07", ["N006"])]
    assert queued[0]["message"].startswith("파견 배정 취소: 10병동 (")
    assert _push_lines(capsys.readouterr()) == []


def test_update_period_notifies_change(seed_data, db, queued):
    """파견 기간 수정(S06 변경): 바뀐 기간을 담아 당사자에게. 메모만 바꾸면 알림 없음."""
    _add_second_group(db)
    created = _create(db, "N002", "파견", "GRP002")
    queued.clear()

    update_assignment(created.id, NurseAssignmentUpdate(note="메모만"), db, current_user=_ADMIN)
    assert queued == []

    new_end = date.today() + timedelta(days=10)
    update_assignment(created.id, NurseAssignmentUpdate(expected_end_date=new_end), db, current_user=_ADMIN)
    assert [(q["push_sub_code"], q["recipients"]) for q in queued] == [("S06", ["N002"])]
    assert queued[0]["message"].startswith("파견 배정 변경: 10병동 · 기간 ")
    assert f"{new_end.month}/{new_end.day}" in queued[0]["message"]


def test_update_to_cancelled_uses_cancel_message(seed_data, db, queued):
    """수정으로 취소 상태가 되면 취소 문구(S07)."""
    _add_second_group(db)
    created = _create(db, "N002", "파견", "GRP002")
    queued.clear()

    update_assignment(created.id, NurseAssignmentUpdate(status="cancelled"), db, current_user=_ADMIN)
    assert [(q["push_sub_code"], q["recipients"]) for q in queued] == [("S07", ["N002"])]


def test_transfer_complete_notifies_only_the_nurse(seed_data, db, capsys, queued):
    """병동이동 완료(S08) flush: 당사자에게만. 발신자는 비어 있지 않다(알림 목록 노출 조건)."""
    _add_second_group(db)
    _create(db, "N005", "병동이동", "GRP002", start=date.today())  # 오늘 발효 → flush 대상
    queued.clear()
    capsys.readouterr()

    count = flush_pending_transfers(db, "GRP002")
    assert count >= 1, "flush 가 병동이동을 완료 처리해야 검증이 유효함"

    assert [(q["push_sub_code"], q["recipients"]) for q in queued] == [("S08", ["N005"])]
    today = date.today()
    assert queued[0]["message"] == f"병동이동 완료: {today.month}/{today.day}부터 10병동 소속입니다"
    assert queued[0]["sender_emp_seq_no"]
    assert _push_lines(capsys.readouterr()) == []


def test_completed_assignment_update_and_cancel_notify(seed_data, db, queued):
    """종료된 파견도 수정(S06 변경)·취소(S07)는 당사자에게 — 발효 뒤 정정이 빠지지 않게."""
    _add_second_group(db)
    created = _create(db, "N002", "파견", "GRP002")
    update_assignment(created.id, NurseAssignmentUpdate(status="completed"), db, current_user=_ADMIN)
    queued.clear()

    new_end = date.today() + timedelta(days=5)
    update_assignment(created.id, NurseAssignmentUpdate(expected_end_date=new_end), db, current_user=_ADMIN)
    assert [(q["push_sub_code"], q["recipients"]) for q in queued] == [("S06", ["N002"])]
    queued.clear()

    cancel_assignment(created.id, db, current_user=_ADMIN)
    assert [(q["push_sub_code"], q["recipients"]) for q in queued] == [("S07", ["N002"])]
    queued.clear()

    cancel_assignment(created.id, db, current_user=_ADMIN)  # 이미 취소된 것을 다시 취소 — 알림 없음
    assert queued == []


def test_transfer_completion_claimed_once(seed_data, db, queued):
    """다른 처리가 먼저 완료한 병동이동은 다시 완료 처리·S08 하지 않는다."""
    from db.models import NurseAssignment

    _add_second_group(db)
    created = _create(db, "N005", "병동이동", "GRP002", start=date.today())
    queued.clear()
    # 스케줄러가 먼저 완료했다고 치고(같은 행을 이미 읽어 둔 상태), 레이지 체크가 뒤따라 온다.
    row = db.query(NurseAssignment).filter(NurseAssignment.id == created.id).one()
    db.query(NurseAssignment).filter(NurseAssignment.id == created.id).update(
        {NurseAssignment.status: "completed"}, synchronize_session=False)

    from services.assignment_service import _claim_transfer_completion
    assert _claim_transfer_completion(db, row, date.today()) is None
    assert flush_pending_transfers(db, "GRP002") == 0
    assert queued == []


def test_transfer_completion_uses_latest_row(seed_data, db, queued):
    """완료 처리가 목록을 읽은 뒤 시작일이 미뤄지면 완료하지 않고, 도착 병동이 바뀌면 바뀐 병동으로 적용한다."""
    from db.models import NurseAssignment
    from services.assignment_service import _claim_transfer_completion

    _add_second_group(db)
    if not db.query(Group).filter(Group.group_id == "GRP003").first():
        db.add(Group(group_id="GRP003", office_id="OFF001", group_name="11병동"))
        db.flush()
    created = _create(db, "N005", "병동이동", "GRP002", start=date.today())
    queued.clear()
    row = db.query(NurseAssignment).filter(NurseAssignment.id == created.id).one()  # 목록에서 읽어 둔 행

    # 다른 요청이 시작일을 내일로 미뤘다(읽어 둔 행은 옛 값 그대로) → 확보하지 않는다.
    db.query(NurseAssignment).filter(NurseAssignment.id == created.id).update(
        {NurseAssignment.start_date: date.today() + timedelta(days=1)}, synchronize_session=False)
    assert _claim_transfer_completion(db, row, date.today()) is None

    # 다시 오늘로 되돌리고 도착 병동을 11병동으로 바꿨다 → 바뀐 병동으로 완료·알림.
    db.query(NurseAssignment).filter(NurseAssignment.id == created.id).update(
        {NurseAssignment.start_date: date.today(), NurseAssignment.target_group_id: "GRP003"},
        synchronize_session=False)
    fresh = _claim_transfer_completion(db, row, date.today())
    assert fresh is not None and fresh.target_group_id == "GRP003" and fresh.status == "completed"
    assert flush_pending_transfers(db, "GRP003") == 0  # 이미 확보됨 — 되풀이하지 않는다


def _outbox_module(monkeypatch):
    """발송기 모듈 — 테스트의 가짜 `db.client2` 에는 DB 이름이 없어 넣어 준 뒤 불러온다."""
    import importlib
    import sys

    monkeypatch.setattr(sys.modules["db.client2"], "EUN_DB_NAME", "eun_roster_test", raising=False)
    return importlib.import_module("services.push_outbox_service")


def _assignment_cursor(db):
    """발송기 guard 확인용 가짜 커서 — 그룹웨어 연결 대신 테스트 세션의 배정 행을 돌려준다."""
    from db.models import NurseAssignment

    from db.models import Nurse

    class _Cursor:
        row = None

        def execute(self, sql, params):
            assert "UPDLOCK" in sql
            if "nurse_assignment" in sql:
                self.row = db.query(
                    NurseAssignment.status, NurseAssignment.reason, NurseAssignment.target_group_id,
                    NurseAssignment.start_date, NurseAssignment.expected_end_date, NurseAssignment.end_date,
                    NurseAssignment.nurse_id,
                ).filter(NurseAssignment.id == params[0]).first()
            else:
                assert "nurses" in sql
                self.row = db.query(Nurse.group_id).filter(Nurse.nurse_id == params[0]).first()

        def fetchone(self):
            return tuple(self.row) if self.row else None

    return _Cursor()


def test_guard_skips_outdated_assignment_push(seed_data, db, queued, monkeypatch):
    """재시도로 밀린 옛 알림은 발송기가 보내지 않는다 — 생성 뒤 기간이 바뀌거나 취소됐으면 옛 '배정' 알림은 막힌다."""
    pos = _outbox_module(monkeypatch)
    _add_second_group(db)
    created = _create(db, "N002", "파견", "GRP002")
    created_guard = queued[0]["guard"]
    assert created_guard.startswith(f"assignment_state:{created.id}:live:active:dispatch:GRP002:")
    assert pos._guard_holds(_assignment_cursor(db), created_guard) is True  # 그대로면 보낸다

    update_assignment(created.id, NurseAssignmentUpdate(expected_end_date=date.today() + timedelta(days=9)),
                      db, current_user=_ADMIN)
    changed_guard = queued[1]["guard"]
    assert pos._guard_holds(_assignment_cursor(db), created_guard) is False  # 기간이 바뀜 — 옛 생성 알림 막음
    assert pos._guard_holds(_assignment_cursor(db), changed_guard) is True

    cancel_assignment(created.id, db, current_user=_ADMIN)
    cancel_guard = queued[2]["guard"]
    assert pos._guard_holds(_assignment_cursor(db), changed_guard) is False  # 취소됨 — 옛 변경 알림 막음
    assert pos._guard_holds(_assignment_cursor(db), cancel_guard) is True


def test_guard_transfer_completed(seed_data, db, queued, monkeypatch):
    """병동이동 완료 알림은 그 병동으로 완료된 상태일 때만 보낸다."""
    pos = _outbox_module(monkeypatch)
    _add_second_group(db)
    _create(db, "N005", "병동이동", "GRP002", start=date.today())
    queued.clear()
    flush_pending_transfers(db, "GRP002")
    guard = queued[0]["guard"]
    assert ":completed:completed:transfer:GRP002:" in guard
    assert pos._guard_holds(_assignment_cursor(db), guard) is True

    # 완료 뒤 사유를 정정하면(병동·소속 그대로) 옛 완료 알림은 막는다.
    from db.models import NurseAssignment
    db.query(NurseAssignment).filter(NurseAssignment.nurse_id == "N005", NurseAssignment.reason == "병동이동") \
        .update({NurseAssignment.reason: "파견"}, synchronize_session=False)
    assert pos._guard_holds(_assignment_cursor(db), guard) is False
    db.query(NurseAssignment).filter(NurseAssignment.nurse_id == "N005", NurseAssignment.reason == "파견") \
        .update({NurseAssignment.reason: "병동이동"}, synchronize_session=False)
    assert pos._guard_holds(_assignment_cursor(db), guard) is True

    # 재시도를 기다리는 사이 다음 이동이 완료돼 소속이 바뀌면 옛 '10병동 소속' 알림은 막는다.
    from db.models import Nurse
    db.query(Nurse).filter(Nurse.nurse_id == "N005").update({Nurse.group_id: "GRP001"}, synchronize_session=False)
    assert pos._guard_holds(_assignment_cursor(db), guard) is False


@pytest.mark.parametrize("change", [
    {"reason": "휴직"},          # 사유만 바뀜(병동·기간 그대로)
    {"status": "completed"},     # 상태만 바뀜
])
def test_guard_blocks_old_push_after_reason_or_status_change(seed_data, db, queued, monkeypatch, change):
    """사유나 상태만 바뀌어도 옛 '배정' 알림은 막고, 새 변경 알림은 보낸다."""
    pos = _outbox_module(monkeypatch)
    _add_second_group(db)
    created = _create(db, "N002", "파견", "GRP002")
    created_guard = queued[0]["guard"]

    update_assignment(created.id, NurseAssignmentUpdate(**change), db, current_user=_ADMIN)
    assert len(queued) == 2
    assert pos._guard_holds(_assignment_cursor(db), created_guard) is False
    assert pos._guard_holds(_assignment_cursor(db), queued[1]["guard"]) is True


def test_guard_blocks_old_cancel_after_revive_change_recancel(seed_data, db, queued, monkeypatch):
    """취소 알림 대기 중 되살려 기간을 바꾸고 다시 취소하면 옛 취소 알림은 막고 새 취소 알림만 보낸다."""
    pos = _outbox_module(monkeypatch)
    _add_second_group(db)
    created = _create(db, "N002", "파견", "GRP002")
    cancel_assignment(created.id, db, current_user=_ADMIN)
    old_cancel = queued[-1]["guard"]
    update_assignment(created.id, NurseAssignmentUpdate(status="active"), db, current_user=_ADMIN)
    update_assignment(created.id, NurseAssignmentUpdate(expected_end_date=date.today() + timedelta(days=7)),
                      db, current_user=_ADMIN)
    cancel_assignment(created.id, db, current_user=_ADMIN)
    new_cancel = queued[-1]["guard"]
    assert pos._guard_holds(_assignment_cursor(db), old_cancel) is False
    assert pos._guard_holds(_assignment_cursor(db), new_cancel) is True


def test_cancel_with_simultaneous_change_uses_saved_values(seed_data, db, queued, monkeypatch):
    """수정 한 번에 취소 + 기간 변경을 하면 취소 알림 본문도 저장된(바뀐) 기간을 말하고 guard 와 맞는다."""
    pos = _outbox_module(monkeypatch)
    _add_second_group(db)
    created = _create(db, "N002", "파견", "GRP002")
    new_end = date.today() + timedelta(days=4)
    update_assignment(created.id, NurseAssignmentUpdate(status="cancelled", expected_end_date=new_end),
                      db, current_user=_ADMIN)
    cancel = queued[-1]
    assert cancel["push_sub_code"] == "S07"
    assert cancel["message"].endswith(f"~{new_end.month}/{new_end.day})")
    assert pos._guard_holds(_assignment_cursor(db), cancel["guard"]) is True


def test_guard_distinguishes_every_reason(seed_data, db, queued, monkeypatch):
    """파견→휴직 변경 알림 대기 중 사유를 퇴사로 또 바꾸면 옛 '휴직' 알림은 막는다(사유마다 다른 코드)."""
    pos = _outbox_module(monkeypatch)
    _add_second_group(db)
    created = _create(db, "N002", "파견", "GRP002")
    update_assignment(created.id, NurseAssignmentUpdate(reason="휴직"), db, current_user=_ADMIN)
    leave_guard = queued[-1]["guard"]
    assert ":leave:" in leave_guard
    from db.models import NurseAssignment
    db.query(NurseAssignment).filter(NurseAssignment.id == created.id).update(
        {NurseAssignment.reason: "퇴사"}, synchronize_session=False)
    assert pos._guard_holds(_assignment_cursor(db), leave_guard) is False


def test_guard_length_fits_column_for_long_reason(seed_data, db):
    """표 밖의 긴 사유·긴 병동 id 여도 guard 는 push_outbox.guard(VARCHAR 200) 안에 들어간다."""
    from types import SimpleNamespace as NS

    from services.assignment_service import _assignment_guard, assignment_reason_code

    assert assignment_reason_code("파견") == "dispatch" and assignment_reason_code("휴직") == "leave"
    assert assignment_reason_code("가" * 200) != assignment_reason_code("나" * 200)
    row = NS(id=2_147_483_647, status="completed", reason="가" * 200, target_group_id="g" * 50,
             start_date=date(2026, 10, 7), expected_end_date=date(2026, 12, 31), end_date=date(2026, 12, 31))
    assert len(_assignment_guard(row, "completed")) <= 200


def test_notification_lookup_failure_keeps_business_change(seed_data, db, queued, monkeypatch):
    """알림용 병동 이름 조회가 실패해도 배정 저장·병동이동 완료는 그대로 된다(알림만 빠진다)."""
    import services.assignment_service as asv
    from db.models import Nurse, NurseAssignment

    _add_second_group(db)

    def _boom(*_a, **_k):
        raise RuntimeError("조회 실패")

    monkeypatch.setattr(asv, "_get_group_name", _boom)
    created = _create(db, "N005", "병동이동", "GRP002", start=date.today())
    assert db.query(NurseAssignment).filter(NurseAssignment.id == created.id).one().status == "active"
    assert flush_pending_transfers(db, "GRP002") == 1
    assert db.query(Nurse).filter(Nurse.nurse_id == "N005").one().group_id == "GRP002"
    assert queued == []


def test_recipient_lookup_failure_keeps_update_and_cancel(seed_data, db, queued, monkeypatch):
    """받는 간호사 조회까지 실패해도 수정·취소는 저장된다(알림만 빠진다) — 조회도 알림 격리 안에서 한다."""
    import services.assignment_service as asv
    from db.models import NurseAssignment

    _add_second_group(db)
    created = _create(db, "N002", "파견", "GRP002")
    queued.clear()

    def _boom(*_a, **_k):
        raise RuntimeError("발신자·수신자 조회 실패")

    monkeypatch.setattr(asv, "_push_sender", _boom)
    new_end = date.today() + timedelta(days=6)
    update_assignment(created.id, NurseAssignmentUpdate(expected_end_date=new_end), db, current_user=_ADMIN)
    cancel_assignment(created.id, db, current_user=_ADMIN)
    saved = db.query(NurseAssignment).filter(NurseAssignment.id == created.id).one()
    assert (saved.expected_end_date, saved.status) == (new_end, "cancelled")
    assert queued == []
