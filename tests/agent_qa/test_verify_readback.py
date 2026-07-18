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
