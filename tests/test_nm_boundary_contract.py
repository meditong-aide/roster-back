"""NM — 바깥 계약(API·DB·저장)은 옛 이름 그대로, 파이썬 안쪽과 에이전트는 새 이름(nm/DESIGN_NM.md §2·§3).

- 저장: 요청 모델(옛 필드 이름)로 저장하면 ORM 새 속성에 들어가고, 물리 컬럼은 옛 이름 그대로다.
- 조회: 모달 로드 dict 는 옛 키만 낸다(프론트 변경 없음).
- 하드락 요청 가드: 옛 이름·새 이름 어느 쪽으로 풀려 해도 400 이고 문구는 바깥 이름이다.
- 에이전트 설정 도구: 새 이름으로 읽고 쓰며, 옛 이름 쓰기는 거절하고 새 이름을 알려 준다.
- 라우터 경계: 요청의 설정 dict 는 안쪽(파이썬 이름)으로 바꿔 서비스에 넘기고, 응답·오류 detail 은 바깥(옛 이름)으로 바꾼다.
  큐에 싣는 params 는 요청 그대로(바깥 이름)다.
- 안전망: 시험은 strict — ORM·엔진 dataclass 의 옛 이름 접근은 예외다.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from types import SimpleNamespace

import pytest
import routers.roster_create as rc
from agents_v2.tools import constraint_tools
from db.models import Group, RosterConfig
from db.roster_config import NurseRosterConfig
from fastapi import HTTPException
from routers.roster import _roster_config_to_dict, get_config_by_version, get_config_versions
from schemas.roster_schema import RosterConfigCreate, RosterRequest
from services.engine_contract.names import (
    EXTERNAL_TO_INTERNAL,
    INTERNAL_TO_EXTERNAL,
    OldConfigNameError,
    external_key,
)
from services.roster_service import save_roster_config_service
from sqlalchemy import select, text
from starlette.requests import Request

#: 저장 요청에 실을 바깥 이름 값 — 기본과 다르게 골라 '실제로 옮겨졌다'를 본다.
_EXTERNAL_VALUES = {
    "off_first": True,
    "banned_day_after_eve": True,
    "nod_noe": False,
    "min_exp_per_shift": 4,
    "req_exp_nurses": 2,
    "preceptee_shift_count": False,
    "three_seq_nig": False,
    "not_one_night": True,
    "sequential_offs": False,
    "two_offs_per_week": False,
    "preceptee_on": True,
    "off_swap_enabled": True,
    "fixed_wanted_use_yn": True,
    "max_conseq_off": 4,
}


def _make_config(**overrides) -> RosterConfigCreate:
    base = dict(
        max_nig_per_month=8, two_offs_after_three_nig=True, two_offs_after_two_nig=False, max_conseq_work=5,
        off_days=9, shift_priority=0.8, weekend_shift_ratio=1.0, patient_amount=0, even_nights=True, preceptor_gauge=5,
        **_EXTERNAL_VALUES,
    )
    base.update(overrides)
    return RosterConfigCreate(**base)


@pytest.fixture
def adm_user():
    return SimpleNamespace(nurse_id="N001", account_id="acc_N001", group_id="GRP001", office_id="OFF001",
                           is_master_admin=True)


def test_external_values_cover_all_fourteen():
    assert set(_EXTERNAL_VALUES) == set(EXTERNAL_TO_INTERNAL)


def test_save_writes_external_request_fields_into_renamed_attributes(db, seed_data, adm_user):
    saved = save_roster_config_service(_make_config(config_name="NM"), adm_user, db, override_group_id="GRP001")
    row = db.query(RosterConfig).filter(RosterConfig.config_id == saved["config_id"]).one()
    for old, new in EXTERNAL_TO_INTERNAL.items():
        got = getattr(row, new)
        assert (bool(got) if isinstance(_EXTERNAL_VALUES[old], bool) else got) == _EXTERNAL_VALUES[old], old
    cols = ", ".join(EXTERNAL_TO_INTERNAL)
    raw = db.execute(text(f"SELECT {cols} FROM roster_config WHERE config_id = :i"), {"i": saved["config_id"]}).one()
    for old, value in zip(EXTERNAL_TO_INTERNAL, raw, strict=True):
        expected = _EXTERNAL_VALUES[old]
        assert (bool(value) if isinstance(expected, bool) else value) == expected, f"물리 컬럼 {old}"


def test_orm_sql_keeps_physical_column_names():
    sql = str(select(RosterConfig.staffing_first, RosterConfig.ban_single_night).compile())
    assert "roster_config.off_first" in sql and "roster_config.not_one_night" in sql
    assert "staffing_first" not in sql


def test_modal_load_dict_uses_external_keys_only(db, seed_data):
    row = db.query(RosterConfig).first()
    row.staffing_first, row.ban_single_night, row.allow_three_consecutive_nights = True, True, False
    out = _roster_config_to_dict(row)
    assert set(EXTERNAL_TO_INTERNAL) <= set(out), "모달 로드 dict 는 14칸을 옛 키로 다 낸다"
    assert not set(INTERNAL_TO_EXTERNAL) & set(out), "새 이름이 프론트로 새면 안 된다"
    assert out["off_first"] is True and out["not_one_night"] is True and out["three_seq_nig"] is False


def test_preset_list_summary_keeps_external_key(db, seed_data, adm_user):
    saved = save_roster_config_service(_make_config(config_name="NM"), adm_user, db, override_group_id="GRP001")
    out = get_config_versions(group_id="GRP001", current_user=adm_user, db=db)
    summary = next(p for p in out if p["config_id"] == saved["config_id"])["summary"]
    assert summary["fixed_wanted_use_yn"] is True
    assert not set(INTERNAL_TO_EXTERNAL) & set(summary), "새 이름이 프론트로 새면 안 된다"


def test_first_load_creates_default_preset_and_answers_with_external_keys_only(db, seed_data, adm_user):
    """설정이 없는 병동의 첫 조회 — 기본 설정 행을 새 속성으로 만들고, 응답은 14칸 옛 키만 낸다."""
    db.add(Group(group_id="GRP002", office_id="OFF001", group_name="10병동"))
    db.flush()
    out = get_config_by_version(config_version="latest", group_id="GRP002", current_user=adm_user, db=db)
    assert set(EXTERNAL_TO_INTERNAL) <= set(out)
    assert not set(INTERNAL_TO_EXTERNAL) & set(out)
    row = db.query(RosterConfig).filter(RosterConfig.group_id == "GRP002").one()
    assert row.config_id == out["config_id"] and row.ban_shift_transitions is True


def test_orm_and_engine_config_old_names_raise_under_strict(db, seed_data):
    row = db.query(RosterConfig).first()
    with pytest.raises(OldConfigNameError):
        getattr(row, "off_first", None)
    with pytest.raises(OldConfigNameError):
        getattr(NurseRosterConfig(), "not_one_night", None)


# ── 하드락 요청 가드 ─────────────────────────────────────────────────

@pytest.fixture
def user():
    return SimpleNamespace(nurse_id="N001", account_id="acc_N001", group_id="GRP001", office_id="OFF001",
                           is_master_admin=True)


@pytest.fixture
def sqs_calls(monkeypatch) -> list[dict]:
    calls: list[dict] = []

    async def fake_sqs(body):
        calls.append(body)
        return {"MessageId": f"msg-{len(calls)}"}

    monkeypatch.setattr(rc, "_send_sqs_job", fake_sqs)
    return calls


def _async(req: RosterRequest, user, db, wait_for_result: bool = False):
    request = Request({"type": "http", "headers": [], "client": ("127.0.0.1", 0)})
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(rc.roster_create_async(req, request, current_user=user, _db=db,
                                                              wait_for_result=wait_for_result))
    finally:
        loop.close()


@pytest.mark.parametrize("override", [
    {"banned_day_after_eve": False}, {"ban_shift_transitions": False},
    {"not_one_night": False}, {"ban_single_night": False},
], ids=["#2-external", "#2-internal", "#7-external", "#7-internal"])
def test_hard_lock_guard_blocks_both_names_with_external_wording(db, seed_data, user, sqs_calls, override):
    with pytest.raises(HTTPException) as exc_info:
        _async(RosterRequest(year=2026, month=11, group_id="GRP001", config_override=override), user, db)
    assert exc_info.value.status_code == 400
    detail = exc_info.value.detail
    assert not any(new in detail for new in INTERNAL_TO_EXTERNAL), "사용자 문구는 바깥 이름으로 적는다"
    assert external_key(next(iter(override))) in detail, "어느 이름으로 보냈든 문구에는 바깥 이름이 적힌다"
    assert sqs_calls == []


# ── 에이전트 설정 도구 ───────────────────────────────────────────────

def test_agent_config_tool_reads_internal_names(db, seed_data):
    out = constraint_tools.get_roster_config(db, "GRP001")
    assert out is not None
    assert not set(EXTERNAL_TO_INTERNAL) & set(out), "에이전트에게는 옛 이름을 보이지 않는다"
    assert {"staffing_first", "ban_single_night", "allow_three_consecutive_nights"} <= set(out)
    assert set(INTERNAL_TO_EXTERNAL) <= set(out), "14칸을 새 이름으로 전부 보인다(빠진 칸이 있으면 에이전트가 그 칸을 모른다)"


def test_agent_config_tool_rejects_external_names_with_hint(db, seed_data):
    res = constraint_tools.update_roster_config(db, "GRP001", {"off_first": True}, preview_only=True)
    assert "error" in res and "staffing_first" in res["error"]


def test_agent_config_tool_writes_internal_names(db, seed_data):
    res = constraint_tools.update_roster_config(db, "GRP001", {"staffing_first": True})
    assert "error" not in res, res
    row = db.query(RosterConfig).filter(RosterConfig.group_id == "GRP001").order_by(RosterConfig.config_id.desc()).first()
    assert row.staffing_first is True or row.staffing_first == 1


# ── 라우터 경계 변환 — 요청은 안쪽(파이썬 이름)으로, 응답·오류 detail 은 바깥(옛 이름)으로 ───────────────

#: 서비스 안쪽 모양(파이썬 이름)의 해결 카드 / 라우터가 내보낼 바깥 모양(옛 이름)의 같은 카드.
_CARD_INTERNAL = {"config_key": "ban_single_night", "apply": {"ban_single_night": False, "max_conseq_work": 6}}
_CARD_EXTERNAL = {"config_key": "not_one_night", "apply": {"not_one_night": False, "max_conseq_work": 6}}


@pytest.fixture
def fake_generate(monkeypatch):
    """생성 서비스를 가짜로 바꾼다 — 받은 키워드 인자를 기록하고, 정해 둔 결과(또는 예외)를 낸다."""
    box = SimpleNamespace(calls=[], result={}, error=None)

    def _fake(*_args, **kwargs):
        box.calls.append(kwargs)
        if box.error is not None:
            raise box.error
        return box.result

    monkeypatch.setattr(rc, "generate_roster_service", _fake)
    return box


def test_generate_endpoint_hands_internal_override_to_service_and_answers_in_external_names(
        db, seed_data, user, fake_generate):
    fake_generate.result = {"constraint_impact": {"staffing_first": True},
                            "infeasibility": {"resolution_options": [_CARD_INTERNAL]}}
    out = rc.generate_roster_endpoint(RosterRequest(year=2026, month=11, config_override={"off_first": True}),
                                      current_user=user, db=db)
    assert fake_generate.calls[0]["config_override"] == {"staffing_first": True}, "서비스에는 파이썬 이름으로"
    assert out == {"constraint_impact": {"off_first": True}, "infeasibility": {"resolution_options": [_CARD_EXTERNAL]}}


def test_generate_endpoint_rethrows_structured_detail_in_external_names(db, seed_data, user, fake_generate):
    """프론트가 카드의 apply 를 모달 설정에 병합해 다시 보내므로, 오류 detail 속 카드도 바깥 이름이어야 한다."""
    fake_generate.error = HTTPException(status_code=500, detail={"infeasibility": {"resolution_options": [_CARD_INTERNAL]}})
    with pytest.raises(HTTPException) as exc_info:
        rc.generate_roster_endpoint(RosterRequest(year=2026, month=11), current_user=user, db=db)
    assert exc_info.value.status_code == 500
    assert exc_info.value.detail == {"infeasibility": {"resolution_options": [_CARD_EXTERNAL]}}


def test_generate_endpoint_keeps_plain_string_detail(db, seed_data, user, fake_generate):
    fake_generate.error = HTTPException(status_code=409, detail="staffing_first 가 들어간 문장은 그대로")
    with pytest.raises(HTTPException) as exc_info:
        rc.generate_roster_endpoint(RosterRequest(year=2026, month=11), current_user=user, db=db)
    assert (exc_info.value.status_code, exc_info.value.detail) == (409, "staffing_first 가 들어간 문장은 그대로")


def test_async_sync_mode_answers_in_external_names(db, seed_data, user, fake_generate):
    fake_generate.result = {"constraint_impact": {"ban_single_night": True}}
    out = _async(RosterRequest(year=2026, month=11, group_id="GRP001", config_override={"off_first": True}),
                 user, db, wait_for_result=True)
    assert fake_generate.calls[0]["config_override"] == {"staffing_first": True}
    assert out["mode"] == "sync" and out["result"] == {"constraint_impact": {"not_one_night": True}}


def test_async_queue_params_keep_the_requested_external_names(db, seed_data, user, sqs_calls):
    """큐에 싣는 params·응답 job 은 요청 그대로(바깥 이름) — 라우터가 req 를 바꾸면 안 된다."""
    out = _async(RosterRequest(year=2026, month=11, group_id="GRP001", config_override={"off_first": True}), user, db)
    assert sqs_calls[0]["params"]["config_override"] == {"off_first": True}
    assert out["job"]["params"]["config_override"] == {"off_first": True}


@pytest.mark.parametrize("endpoint", ["async", "generate"])
def test_same_setting_under_both_names_with_different_values_is_a_400(db, seed_data, user, sqs_calls, fake_generate,
                                                                      endpoint):
    req = RosterRequest(year=2026, month=11, group_id="GRP001", config_override={"off_first": True, "staffing_first": False})
    with pytest.raises(HTTPException) as exc_info:
        if endpoint == "async":
            _async(req, user, db)
        else:
            rc.generate_roster_endpoint(req, current_user=user, db=db)
    assert exc_info.value.status_code == 400
    assert not any(new in exc_info.value.detail for new in INTERNAL_TO_EXTERNAL)
    assert sqs_calls == [] and fake_generate.calls == []


def test_same_setting_under_both_names_with_equal_values_collapses(db, seed_data, user, sqs_calls):
    _async(RosterRequest(year=2026, month=11, group_id="GRP001", config_override={"off_first": True, "staffing_first": True}),
           user, db)
    assert len(sqs_calls) == 1


@pytest.mark.parametrize("name", ["banned_day_after_eve", "ban_shift_transitions"], ids=["external", "internal"])
def test_hard_lock_guard_blocks_both_names_on_generate_and_apply_resolution(db, seed_data, user, fake_generate, name):
    with pytest.raises(HTTPException) as gen_exc:
        rc.generate_roster_endpoint(RosterRequest(year=2026, month=11, config_override={name: False}),
                                    current_user=user, db=db)
    with pytest.raises(HTTPException) as apply_exc:
        rc.apply_resolution_endpoint(rc.ApplyResolutionRequest(year=2026, month=11, apply={name: False}),
                                     current_user=user, db=db)
    for exc_info in (gen_exc, apply_exc):
        assert exc_info.value.status_code == 400
        assert "banned_day_after_eve=False" in exc_info.value.detail, "문구의 키는 바깥 이름"
    assert fake_generate.calls == []
    assert db.query(RosterConfig).filter(RosterConfig.config_id == 1).one().ban_shift_transitions is True


@pytest.mark.parametrize("persist", [True, False], ids=["persist", "transient"])
def test_apply_resolution_maps_external_delta_onto_columns_and_answers_in_external_names(
        db, seed_data, user, monkeypatch, persist):
    """카드 apply(바깥 이름)가 ORM 새 속성에 임시/영구 반영되고, 컬럼이 아닌 solver 키만 override 로 간다."""
    row = db.query(RosterConfig).filter(RosterConfig.config_id == 1).one()
    assert (bool(row.staffing_first), row.soft_max_conseq_off) == (False, None)
    seen: dict = {}

    def spy(*_args, **kwargs):
        seen.update(kwargs, during=(bool(row.staffing_first), row.soft_max_conseq_off))
        return {"constraint_impact": {"staffing_first": True}}

    monkeypatch.setattr(rc, "generate_roster_service", spy)
    out = rc.apply_resolution_endpoint(
        rc.ApplyResolutionRequest(year=2026, month=11, option_id="opt-1", persist=persist,
                                  apply={"off_first": True, "max_conseq_off": 4, "team_min_soft_fallback": True}),
        current_user=user, db=db)
    assert seen["during"] == (True, 4), "생성 중에는 컬럼 delta 가 새 속성에 반영돼 있다"
    assert seen["config_override"] == {"team_min_soft_fallback": True}
    assert seen["column_delta_mode"] == ("persist" if persist else "temporary")
    assert out["constraint_impact"] == {"off_first": True}
    assert out["applied_resolution"] == {
        "option_id": "opt-1", "persisted": persist,
        "changes": {"off_first": True, "max_conseq_off": 4, "team_min_soft_fallback": True},
        "transient_keys": ["team_min_soft_fallback"],
    }
    db.refresh(row)
    assert (bool(row.staffing_first), row.soft_max_conseq_off) == ((True, 4) if persist else (False, None))


def test_apply_resolution_rethrows_structured_detail_in_external_names(db, seed_data, user, fake_generate):
    fake_generate.error = HTTPException(status_code=500, detail={"infeasibility": {"resolution_options": [_CARD_INTERNAL]}})
    with pytest.raises(HTTPException) as exc_info:
        rc.apply_resolution_endpoint(rc.ApplyResolutionRequest(year=2026, month=11, apply={"off_first": True}),
                                     current_user=user, db=db)
    assert exc_info.value.detail == {"infeasibility": {"resolution_options": [_CARD_EXTERNAL]}}
    row = db.query(RosterConfig).filter(RosterConfig.config_id == 1).one()
    assert not row.staffing_first, "실패하면 임시로 바꾼 컬럼이 원복된다"


def test_precheck_router_reads_internal_names_and_answers_in_external_names(monkeypatch):
    import routers.roster_precheck as rp

    seen: dict = {}

    def fake_run(inp, *, stop_on_config_error=False):
        seen["roster_config"] = dict(inp.roster_config)
        return {"status": "HAS_ISSUES",
                "issues": [{"reason_code": "X", "severity": "hard", "evidence": {"ban_single_night": True}}]}

    monkeypatch.setattr(rp, "run_precheck", fake_run)
    req = rp.PrecheckRequest(num_days=30, nurses=[],
                             roster_config={"three_seq_nig": True, "not_one_night": True, "max_conseq_work": 5})
    out = rp.precheck_roster("GRP001", req, current_user=SimpleNamespace(), db=None)
    assert seen["roster_config"] == {"allow_three_consecutive_nights": True, "ban_single_night": True, "max_conseq_work": 5}
    assert out["issues"][0]["evidence"] == {"not_one_night": True}


@dataclass
class _Row:
    parent: str
    hard_lock: dict


def test_constraint_impact_listings_answer_in_external_names(monkeypatch):
    import routers.constraint_impact as ci

    row = _Row("Night", {"relax": {"key": "ban_single_night"}})
    monkeypatch.setattr(ci, "_build_listing_snapshot", lambda *_a, **_k: SimpleNamespace())
    monkeypatch.setattr(ci, "resolve_home_group_id", lambda *_a, **_k: "GRP001")
    monkeypatch.setattr(ci, "list_constraint_modules", lambda _snapshot: [row])
    monkeypatch.setattr(ci, "find_constraint_instances", lambda _snapshot, **_k: [row])
    user = SimpleNamespace(office_id="OFF001")
    expected = [{"parent": "Night", "hard_lock": {"relax": {"key": "not_one_night"}}}]
    assert ci.get_modules(year=2026, month=11, current_user=user, db=None)["modules"] == expected
    out = ci.get_instances(year=2026, month=11, family=None, team=None, day=None, shift=None, grade=None,
                           current_user=user, db=None)
    assert out["instances"] == expected


def test_ontology_audit_answers_in_external_names(monkeypatch):
    import routers.ontology as onto
    import services.semantics.ontology_audit as audit

    monkeypatch.setattr(audit, "audit_all", lambda: [])
    monkeypatch.setattr(audit, "audit_summary",
                        lambda _findings: {"pass": False, "findings": [{"invariant": "I5", "config_key": "ban_single_night"}]})
    body = json.loads(onto.ontology_audit_endpoint().body)
    assert body["findings"] == [{"invariant": "I5", "config_key": "not_one_night"}]


# ── 최종 검토 후속 — 에이전트 표면 ────────────────────────────────────

def test_agent_read_soft_max_conseq_off_null_is_effective_three(db, seed_data):
    """NULL 이면 화면(GET /roster/config)·엔진과 같은 실효값 3 — 에이전트가 '설정 안 됨'으로 읽지 않는다."""
    row = (db.query(RosterConfig).filter(RosterConfig.group_id == "GRP001")
           .order_by(RosterConfig.config_id.desc()).first())
    row.soft_max_conseq_off = None
    db.commit()
    assert constraint_tools.get_roster_config(db, "GRP001")["soft_max_conseq_off"] == 3


def test_agent_job_status_shows_config_slots_in_internal_names(monkeypatch):
    """작업 결과 저장본(바깥 이름)을 에이전트에게 보일 때 설정 자리(apply·config_key)만 파이썬 이름 — 위반 유형은 그대로."""
    from agents_v2.tools import generation_tools as gt
    from services.engine_contract.names import to_external_deep

    monkeypatch.setattr(gt, "format_infeasibility", lambda payload: "요약")
    monkeypatch.setattr(gt, "format_infeasibility_response", lambda payload: "응답")
    stored = to_external_deep({
        "resolution_options": [{"apply": {"preceptee_follows_preceptor": False},
                                "changes": [{"config_key": "preceptee_follows_preceptor"}]}],
        "violations": [{"type": "three_seq_nig"}],
    })
    assert stored["resolution_options"][0]["apply"] == {"preceptee_on": False}, "전제: 저장본은 바깥 이름"
    row = SimpleNamespace(job_id="j1", office_id="OFF001", group_id="GRP001", nurse_id="N001", status="FAILED",
                          progress=100, result_roster_id=None, error_message=json.dumps(stored, ensure_ascii=False),
                          created_at=None, updated_at=None)
    d = gt._job_dict(row)
    option = d["debug_payload"]["resolution_options"][0]
    assert option["apply"] == {"preceptee_follows_preceptor": False}
    assert option["changes"][0]["config_key"] == "preceptee_follows_preceptor"
    assert d["debug_payload"]["violations"][0]["type"] == "three_seq_nig", "위반 유형은 바깥 식별자라 그대로"
    assert d["error_message"] == row.error_message, "저장 원문은 그대로"


def test_approval_replay_maps_old_setting_names_and_keeps_new_ones():
    """배포 전에 저장된 승인 대기(옛 이름)를 재실행할 때 설정 이름 자리만 새 이름으로 잇는다."""
    from agents_v2.agent_v3 import _approval_args_with_internal_names

    old = {"group_id": "GRP001", "field": "banned_day_after_eve", "value": True,
           "mutation": {"target_field": "three_seq_nig", "target_value": True},
           "updates": {"not_one_night": True}, "preview_only": False}
    out = _approval_args_with_internal_names(old)
    assert out["field"] == "ban_shift_transitions"
    assert out["mutation"] == {"target_field": "allow_three_consecutive_nights", "target_value": True}
    assert out["updates"] == {"ban_single_night": True}
    assert old["field"] == "banned_day_after_eve", "원본 승인 대기는 그대로 둔다"
    new = {"group_id": "GRP001", "field": "staffing_first", "value": True}
    assert _approval_args_with_internal_names(new) == new
