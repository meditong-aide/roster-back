"""L1 read-back 검증기 — '조용한 거짓완료'를 VERIFICATION_FAILED 로 잡는지."""
import pytest
from db.models import Group
from agents_v2.middleware import execute_skill
from agents_v2.errors import classify, ErrorType
from tests.agent_qa.test_manage_assignment import _hn_ctx
from services import assignment_service


@pytest.fixture()
def target_ward(db, seed_data):
    g = Group(group_id="GRP002", office_id="OFF001", group_name="중환자실2")
    db.add(g); db.flush()
    return g


def _apply_params():
    return {"operation": "create", "kind": "병동이동", "nurse_name": "김민지",
            "target_ward": "중환자실2", "start_date": "2026-08-01", "preview_only": False}


def test_readback_passes_on_real_apply(db, seed_data, target_ward):
    # 정상 적용 → 배정이 DB 에 실재 → 검증 통과(ok)
    res = execute_skill(db, "manage_assignment", _apply_params(), _hn_ctx())
    assert res.data.get("verification_failed") is not True
    assert res.data.get("ok") is True


def test_readback_catches_false_complete(db, seed_data, target_ward, monkeypatch):
    # create_assignment 을 no-op 로 → 스킬은 ok 보고하지만 DB 엔 아무것도 안 생김(거짓완료 재현)
    monkeypatch.setattr(assignment_service, "create_assignment",
                        lambda *a, **k: None)
    res = execute_skill(db, "manage_assignment", _apply_params(), _hn_ctx())
    # read-back 이 미반영을 감지 → VERIFICATION_FAILED 로 승격
    assert res.data.get("verification_failed") is True
    assert classify(res.data) is ErrorType.VERIFICATION_FAILED
    assert "반영" in res.data.get("error", "")


# ── update_person_attr read-back (period 필드 스킵, false-fail-safe) ──
import agents_v2.skills.update_person_attr  # noqa: F401  (@readback 등록 트리거)
from agents_v2.verify import run_readback
from db.models import Nurse


def _pa_result(field, frm, to, nid="N001"):
    return {"nurse_id": nid, "applied_mutations": [{"field": field, "from": frm, "to": to}]}


def test_person_attr_readback_catches_unpersisted(db, seed_data):
    n = db.query(Nurse).filter(Nurse.nurse_id == "N001").first()
    n.experience = 5; db.flush()
    # 결과는 9로 바뀌었다 '보고'하나 DB 는 여전히 5 → 미반영
    vr = run_readback(db, "update_person_attr", {"group_id": "GRP001"},
                      _pa_result("experience", 5, 9))
    assert vr.ok is False and "반영" in vr.reason


def test_person_attr_readback_passes_when_applied(db, seed_data):
    n = db.query(Nurse).filter(Nurse.nurse_id == "N001").first()
    n.experience = 9; db.flush()  # 실제 반영
    vr = run_readback(db, "update_person_attr", {"group_id": "GRP001"},
                      _pa_result("experience", 5, 9))
    assert vr.ok is True


def test_person_attr_readback_skips_period_field(db, seed_data):
    # grade 는 period(시점 발효) → read-back 스킵. 미반영처럼 보여도 통과(오탐 방지)
    vr = run_readback(db, "update_person_attr", {"group_id": "GRP001"},
                      _pa_result("grade", 1, 3))
    assert vr.ok is True


# ── bulk_mutation read-back (근무표 셀 변경, 정합성 최우선) ──
import agents_v2.skills.bulk_mutation  # noqa: F401,E402  (@readback 등록 트리거)
from agents_v2.tools import schedule_tools  # noqa: E402


def _sched_params():
    return {"scope": "schedule", "group_id": "GRP001", "schedule_id": "SCH202604V1"}


def _cell(db, nid="N001", d="2026-04-01"):
    return schedule_tools.find_schedule_entry(db, "SCH202604V1", nid, d, "GRP001")


def test_bulk_readback_passes_when_shift_matches(db, seed_data):
    # 실제 셀 값을 그대로 '주장' → DB 와 일치 → 통과
    c = _cell(db)
    result = {"entry_id": c["entry_id"], "nurse_id": "N001",
              "work_date": "2026-04-01", "new_shift_id": c["shift_id"]}
    assert run_readback(db, "bulk_mutation", _sched_params(), result).ok is True


def test_bulk_readback_catches_unpersisted(db, seed_data):
    # 셀은 안 바뀌었는데 다른 시프트로 '바꿨다'고 거짓 주장 → 포착
    c = _cell(db)
    bogus = "N_GRP001" if c["shift_id"] != "N_GRP001" else "D_GRP001"
    result = {"entry_id": c["entry_id"], "nurse_id": "N001",
              "work_date": "2026-04-01", "new_shift_id": bogus}
    vr = run_readback(db, "bulk_mutation", _sched_params(), result)
    assert vr.ok is False and "반영" in vr.reason


def test_bulk_readback_catches_partial_in_batch(db, seed_data):
    # 다중 결과 중 하나만 미반영(거짓 주장) → 전체를 미반영으로 포착
    c1, c2 = _cell(db, "N001", "2026-04-01"), _cell(db, "N002", "2026-04-02")
    bogus = "N_GRP001" if c2["shift_id"] != "N_GRP001" else "D_GRP001"
    result = {"affected_count": 2, "results": [
        {"entry_id": c1["entry_id"], "nurse_id": "N001", "work_date": "2026-04-01",
         "new_shift_id": c1["shift_id"]},                                  # 정상
        {"entry_id": c2["entry_id"], "nurse_id": "N002", "work_date": "2026-04-02",
         "new_shift_id": bogus},                                          # 거짓
    ]}
    assert run_readback(db, "bulk_mutation", _sched_params(), result).ok is False


def test_bulk_readback_skips_wanted_scope(db, seed_data):
    # 원티드 스코프는 1차 범위 밖 → 통과(오탐 없음)
    result = {"affected_count": 1, "results": [{"entry_id": "x", "new_shift_id": "D_GRP001"}]}
    vr = run_readback(db, "bulk_mutation",
                      {"scope": "wanted_adjustment", "group_id": "GRP001"}, result)
    assert vr.ok is True


# ── bulk_mutation · 원티드 승인/거부 read-back ──
from db.models import FixedWantedEntry  # noqa: E402


def _wanted_ids(db, applied: bool):
    return [r.id for r in db.query(FixedWantedEntry)
            .filter(FixedWantedEntry.group_id == "GRP001").all()
            if bool(r.is_applied) is applied]


def _wanted_params(target_value):
    return {"scope": "wanted_adjustment", "group_id": "GRP001",
            "mutation": {"target_field": "is_applied", "target_value": target_value}}


def _wanted_result(entry_ids):
    return {"preview": False, "affected_count": len(entry_ids), "entry_ids": entry_ids}


def test_wanted_readback_passes_when_applied(db, seed_data):
    # is_applied=True 인 엔트리에 승인(True) 주장 → 일치 → 통과
    ids = _wanted_ids(db, applied=True)
    vr = run_readback(db, "bulk_mutation", _wanted_params(True), _wanted_result(ids))
    assert vr.ok is True


def test_wanted_readback_catches_unpersisted(db, seed_data):
    # is_applied=False 인 엔트리(N002)를 '승인했다'(True)고 거짓 주장 → 포착
    ids = _wanted_ids(db, applied=False)
    assert ids  # seed 에 False 엔트리(N002 4/15) 존재
    vr = run_readback(db, "bulk_mutation", _wanted_params(True), _wanted_result(ids))
    assert vr.ok is False and "승인" in vr.reason


def test_wanted_readback_reject_matches(db, seed_data):
    # is_applied=False 엔트리에 거부(False) 주장 → 일치 → 통과
    ids = _wanted_ids(db, applied=False)
    vr = run_readback(db, "bulk_mutation", _wanted_params(False), _wanted_result(ids))
    assert vr.ok is True


def test_wanted_readback_skips_unparseable_value(db, seed_data):
    # target_value 해석 불가(애매) → 스킵(오탐 방지)
    ids = _wanted_ids(db, applied=False)
    vr = run_readback(db, "bulk_mutation", _wanted_params("아마도"), _wanted_result(ids))
    assert vr.ok is True


import agents_v2.skills.manage_assignment  # noqa: F401,E402  (@readback 등록 트리거)


# ── cancel류 read-back (부재 확인 방향) ──
from datetime import date as _date  # noqa: E402
from db.models import NurseAssignment, WantedRequest  # noqa: E402


def _mk_assignment(db, status):
    a = NurseAssignment(nurse_id="N001", source_group_id="GRP001", office_id="OFF001",
                        start_date=_date(2026, 8, 1), reason="파견", kind="dispatch", status=status)
    db.add(a); db.flush(); return a


def _cancel_params():
    return {"operation": "cancel", "kind": "파견", "nurse_ids": ["N001"],
            "office_id": "OFF001", "group_id": "GRP001"}


def test_assignment_cancel_catches_still_active(db, seed_data):
    _mk_assignment(db, "active")  # 취소했다 보고했으나 여전히 active
    vr = run_readback(db, "manage_assignment", _cancel_params(),
                      {"ok": True, "message": "파견을 취소했습니다."})
    assert vr.ok is False and "취소" in vr.reason


def test_assignment_cancel_passes_when_gone(db, seed_data):
    _mk_assignment(db, "cancelled")  # 실제 취소됨(active 아님)
    vr = run_readback(db, "manage_assignment", _cancel_params(),
                      {"ok": True, "message": "파견을 취소했습니다."})
    assert vr.ok is True


def _mk_wanted_req(db, rid, submitted):
    w = WantedRequest(nurse_id="N001", request_id=rid, month="2026-04",
                      group_id="GRP001", is_submitted=1 if submitted else 0)
    db.add(w); db.flush(); return w


def _wcancel_params():
    return {"scope": "wanted_submissions", "action": "cancel",
            "group_id": "GRP001", "nurse_ids": ["N001"]}


def test_wanted_cancel_catches_still_submitted(db, seed_data):
    _mk_wanted_req(db, 9001, submitted=True)  # 취소했다 보고했으나 여전히 제출됨
    result = {"nurse_id": "N001", "request_id": 9001, "status": "retracted"}
    vr = run_readback(db, "bulk_mutation", _wcancel_params(), result)
    assert vr.ok is False and "취소" in vr.reason


def test_wanted_cancel_passes_when_retracted(db, seed_data):
    _mk_wanted_req(db, 9002, submitted=False)  # 실제 취소됨
    result = {"nurse_id": "N001", "request_id": 9002, "status": "retracted"}
    vr = run_readback(db, "bulk_mutation", _wcancel_params(), result)
    assert vr.ok is True
