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
