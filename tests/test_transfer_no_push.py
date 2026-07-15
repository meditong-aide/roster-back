"""assignment 알림 전면 미발송 검증.

set_app_push 는 비운영 환경에서 push 시도 시 `[push][skipped:non-prod] ...` 를 stdout 에
찍는다. 이 print 를 신호로 삼아:
  - 대조군(set_app_push 직접 호출): 실제로 찍힌다  → 계측이 살아있음을 증명
  - assignment 생성(S06)/취소(S07)/완료(S08): 어떤 사유든 절대 안 찍힌다

즉 "assignment 쪽으로는 알림이 나갈 일이 없다"를 print 유무로 end-to-end 검증한다.
"""
from datetime import date, timedelta

import pytest

from db.models import Group
from schemas.roster_schema import NurseAssignmentCreate
from services.assignment_service import (
    create_assignment,
    cancel_assignment,
    flush_pending_transfers,
)


def _add_second_group(db):
    """OFF001 소속 target 병동(GRP002) 추가 — 병동이동/파견 목적지."""
    if not db.query(Group).filter(Group.group_id == "GRP002").first():
        db.add(Group(group_id="GRP002", office_id="OFF001", group_name="10병동"))
        db.flush()


def _push_lines(captured) -> list[str]:
    return [ln for ln in captured.out.splitlines() if "[push]" in ln]


def test_set_app_push_prints_control(capsys):
    """대조군: set_app_push 직접 호출은 여전히 [push] 를 찍는다 (계측 살아있음 증명)."""
    from utils.utils import set_app_push

    capsys.readouterr()
    set_app_push(
        pushCode="P30", pushSubCode="S06", officeCode="OFF001",
        sendEmpSeqNo="N001", sendMemberId="N001", receiveEmpSeqNo="N002",
        pushMessage="control", orgPushMessage="control", linkUrl="", linkCode="",
    )
    lines = _push_lines(capsys.readouterr())
    assert lines, "set_app_push 는 비운영에서 [push] 를 찍어야 함 (계측 신호)"


# reason, target 필요 여부
_CREATE_CASES = [
    ("N002", "파견", "GRP002"),
    ("N003", "병동이동", "GRP002"),
    ("N004", "휴직", None),
    ("N005", "프리셉티", None),
]


@pytest.mark.parametrize("nurse_id,reason,target", _CREATE_CASES)
def test_assignment_create_no_push(seed_data, db, capsys, nurse_id, reason, target):
    """생성(S06): 어떤 사유든 알림 미발송."""
    _add_second_group(db)
    capsys.readouterr()

    req = NurseAssignmentCreate(
        nurse_id=nurse_id, source_group_id="GRP001", target_group_id=target,
        office_id="OFF001", start_date=date.today() + timedelta(days=1),
        expected_end_date=date.today() + timedelta(days=30), reason=reason,
    )
    create_assignment(req, db, current_user=None)

    lines = _push_lines(capsys.readouterr())
    assert lines == [], f"{reason} 생성 알림이 나가면 안 됨. got={lines}"


def test_assignment_cancel_no_push(seed_data, db, capsys):
    """취소(S07): 알림 미발송."""
    _add_second_group(db)

    req = NurseAssignmentCreate(
        nurse_id="N006", source_group_id="GRP001", target_group_id="GRP002",
        office_id="OFF001", start_date=date.today() + timedelta(days=1),
        expected_end_date=date.today() + timedelta(days=30), reason="파견",
    )
    created = create_assignment(req, db, current_user=None)
    capsys.readouterr()

    cancel_assignment(created.id, db, current_user=None)

    lines = _push_lines(capsys.readouterr())
    assert lines == [], f"취소 알림이 나가면 안 됨. got={lines}"


def test_transfer_complete_no_push(seed_data, db, capsys):
    """병동이동 완료(S08) flush: 알림 미발송."""
    _add_second_group(db)

    req = NurseAssignmentCreate(
        nurse_id="N005", source_group_id="GRP001", target_group_id="GRP002",
        office_id="OFF001", start_date=date.today(),  # 오늘 발효 → flush 대상
        reason="병동이동",
    )
    create_assignment(req, db, current_user=None)
    capsys.readouterr()

    count = flush_pending_transfers(db, "GRP002")
    assert count >= 1, "flush 가 병동이동을 완료 처리해야 검증이 유효함"

    lines = _push_lines(capsys.readouterr())
    assert lines == [], f"병동이동 완료 알림이 나가면 안 됨. got={lines}"
