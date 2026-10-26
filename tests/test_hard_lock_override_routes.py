"""EI-11 11b — 라우터 단 하드락 요청 400(`.omc/aide_docs/v2/engine_input/ei11/EI11_SPEC_v2.md` §2.4 · §4.1).

라우터는 0줄이다. `/roster_create/async`(그룹 해석 뒤·materialize 앞)·`/roster_create/generate`(첫 문장)·
`/roster_create/apply-resolution`(컬럼 delta 임시 commit 앞)이 모두 `assert_night_caps_within_hard_lock` 하나를 부른다 —
11b 가 그 함수를 넓혀 세 경로가 같이 막힌다. 라우터 함수를 직접 부른다(`tests/test_roster_config_generate_materialize.py`
패턴 — SQS 는 호출 수를 세는 가짜). 솔버·SQS·DB 접속 0(sqlite 픽스처).

sqlite 에는 INFORMATION_SCHEMA 가 없어 materialize 뒤 `inherit_n2n_min_gap` 이 rollback 하며 시드를 지운다(기준선 실패
`test_async_materializes_config_payload` 와 같은 원인). 그래서 통과 경우는 `config` 없이 보내고, materialize 를 지나야 하는
테스트만 그 함수를 no-op 으로 바꾼다.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
import routers.roster_create as rc
import services.roster_create_service as rcs
from db.models import RosterConfig, RosterJob
from fastapi import HTTPException
from schemas.roster_schema import RosterRequest
from starlette.requests import Request

_RELAX_PREFIX = "하드락 규칙은 요청으로 완화할 수 없습니다: "


def _cfg_payload(**overrides) -> dict:
    """모달 config payload — 시드 프리셋(config_id=1)과 달라 materialize 하면 새 행으로 포크된다."""
    base = dict(
        min_exp_per_shift=1, req_exp_nurses=1, two_offs_per_week=True, max_nig_per_month=8,
        three_seq_nig=True, two_offs_after_three_nig=True, two_offs_after_two_nig=True,
        banned_day_after_eve=True, max_conseq_work=5, off_days=9, shift_priority=0.8,
        sequential_offs=True, nod_noe=False, not_one_night=True, preceptee_on=True,
    )
    base.update(overrides)
    return base


@pytest.fixture
def user():
    return SimpleNamespace(nurse_id="N001", account_id="acc_N001", group_id="GRP001",
                           office_id="OFF001", is_master_admin=True)


@pytest.fixture
def sqs_calls(monkeypatch) -> list[dict]:
    calls: list[dict] = []

    async def fake_sqs(body):
        calls.append(body)
        return {"MessageId": f"msg-{len(calls)}"}

    monkeypatch.setattr(rc, "_send_sqs_job", fake_sqs)
    return calls


@pytest.fixture
def no_inherit(monkeypatch):
    """materialize 를 지나야 하는 테스트만 — sqlite 에서 `inherit_n2n_min_gap` 의 rollback 이 시드를 지우지 않게."""
    monkeypatch.setattr(rcs, "inherit_n2n_min_gap", lambda *_a, **_k: None)


@pytest.fixture
def no_generation(monkeypatch) -> list[int]:
    """동기 생성 호출을 센다 — 가드가 앞에서 막으면 0 이다."""
    called: list[int] = []

    def _spy(*_args, **_kwargs):
        called.append(1)
        raise AssertionError("가드 뒤 생성이 불렸다")

    monkeypatch.setattr(rc, "generate_roster_service", _spy)
    return called


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _async(req: RosterRequest, user, db):
    request = Request({"type": "http", "headers": [], "client": ("127.0.0.1", 0)})
    return _run(rc.roster_create_async(req, request, current_user=user, _db=db, wait_for_result=False))


def _counts(db) -> tuple[int, int]:
    return db.query(RosterConfig).count(), db.query(RosterJob).count()


# ── /roster_create/async ─────────────────────────────────────────────────────────


def test_async_rejects_hard_lock_override_before_queue(db, seed_data, user, sqs_calls):
    """`config_override` 의 #2 끄기 → 400 · SQS 0 · job 행 0."""
    before = _counts(db)
    with pytest.raises(HTTPException) as exc_info:
        _async(RosterRequest(year=2026, month=11, group_id="GRP001", config_override={"banned_day_after_eve": False}),
               user, db)
    assert exc_info.value.status_code == 400
    assert exc_info.value.detail == f"{_RELAX_PREFIX}하드락 #2(ND·ED·NE 전이 금지) — banned_day_after_eve=False"
    assert sqs_calls == [] and _counts(db) == before


def test_async_rejects_before_materializing_the_modal_config(db, seed_data, user, sqs_calls, no_inherit):
    """같은 요청에 모달 `config` 를 함께 실어도 400 이고 새 설정 행 0 — 가드가 materialize 앞이다.
    대조: 하드락 밖 카드(`off_days`)면 같은 `config` 가 새 행으로 굳는다(위 '행 0' 이 빈 단언이 아니다)."""
    before = _counts(db)
    with pytest.raises(HTTPException) as exc_info:
        _async(RosterRequest(year=2026, month=11, group_id="GRP001", config=_cfg_payload(),
                             config_override={"banned_day_after_eve": False}), user, db)
    assert exc_info.value.status_code == 400
    assert sqs_calls == [] and _counts(db) == before
    out = _async(RosterRequest(year=2026, month=11, group_id="GRP001", config=_cfg_payload(),
                               config_override={"off_days": 6}), user, db)
    assert out["materialized_config"]["config_name"].startswith("새로운 설정")
    assert _counts(db) == (before[0] + 1, before[1] + 1) and len(sqs_calls) == 1


@pytest.mark.parametrize("override", [
    {"initial_constraints": [1]},
    {"off_window_constraints": {"N07": "x"}},
    {"initial_constraints": {"forced_off": "x"}},
    {"fixed_cells": [{"nurse_index": 0, "day_index": 1, "shift": "D"}]},
], ids=["ic_list", "ow_str", "ic_forced_off_str", "fixed_cells"])
def test_async_rejects_engine_internal_keys_before_queue(db, seed_data, user, sqs_calls, override):
    """리뷰 HIGH(EI-11 11r) — 서버가 계산해 엔진에 넣는 내부 값(`RUNTIME_DATA_NOT_SETTINGS`)을 요청으로 보내면 400 · SQS 0 ·
    job 행 0. 예전엔 가드를 지나 큐에 실렸고(`job.params.config_override` 그대로) 월중 경계 OFF 창을 지웠다."""
    before = _counts(db)
    with pytest.raises(HTTPException) as exc_info:
        _async(RosterRequest(year=2026, month=11, group_id="GRP001", config_override=override), user, db)
    assert exc_info.value.status_code == 400
    assert exc_info.value.detail.startswith(f"{next(iter(override))} 는 서버가 스스로 계산해 엔진에 넣는 내부 값이라 ")
    assert sqs_calls == [] and _counts(db) == before


def test_generate_endpoint_rejects_engine_internal_key(db, seed_data, user, no_generation):
    """동기 생성도 같은 가드 — 생성 서비스까지 가지 않는다(예전엔 가드를 지나 서비스가 불렸다)."""
    with pytest.raises(HTTPException) as exc_info:
        rc.generate_roster_endpoint(RosterRequest(year=2026, month=11, config_override={"initial_constraints": [1]}),
                                    current_user=user, db=db)
    assert exc_info.value.status_code == 400
    assert no_generation == []


_PASSING_CARDS = {
    "soften_team_min": {"config_override": {"team_min_soft_fallback": True}},
    "lower_off_days": {"config_override": {"off_days": 6}},
    "raise_max_night_cap_15": {"config_override": {"max_nig_per_month": 15}},
    "fix_all_personal": {"monthly_limit_release": [{"nurse_id": "N002", "field": "n_max", "value": 10}]},  # 비 N전담
}


@pytest.mark.parametrize(("card", "fields"), list(_PASSING_CARDS.items()), ids=list(_PASSING_CARDS))
def test_async_passes_cards_within_hard_lock(db, seed_data, user, sqs_calls, card, fields):
    """하드락 밖·상한 안 카드는 그대로 재생성된다(dev-5 완료 기준 '해결 카드 재생성 통과') — SQS 1회 · job 행 1."""
    before = _counts(db)
    out = _async(RosterRequest(year=2026, month=11, group_id="GRP001", **fields), user, db)
    assert out["sqs_message_id"] == "msg-1" and len(sqs_calls) == 1, card
    assert _counts(db) == (before[0], before[1] + 1), card
    for key, value in fields.items():
        assert out["job"]["params"][key] == value, card


# ── /roster_create/generate · /roster_create/apply-resolution ───────────────────────────────


def test_generate_endpoint_rejects_hard_lock_override(db, seed_data, user, no_generation):
    with pytest.raises(HTTPException) as exc_info:
        rc.generate_roster_endpoint(RosterRequest(year=2026, month=11, config_override={"two_offs_after_three_nig": False}),
                                    current_user=user, db=db)
    assert exc_info.value.status_code == 400
    assert exc_info.value.detail == f"{_RELAX_PREFIX}하드락 #4(나이트 3연속 후 OFF 2회) — two_offs_after_three_nig=False"
    assert no_generation == []


@pytest.mark.parametrize("persist", [False, True])
@pytest.mark.parametrize("apply", [
    {"two_offs_after_two_nig": False},                      # 시드가 이미 False — 같은 값이어도 400(Q1)
    {"two_offs_after_three_nig": False, "off_days": 6},     # 시드 True → 끄기 + 인접 키
    {"max_consecutive_nights": 3},                          # D13 ③ — 비컬럼 키라 config_override 로 갈 값
], ids=["same_value_false_q1", "turn_off_with_adjacent", "d13_alias"])
def test_apply_resolution_rejects_before_temporary_commit(db, seed_data, user, no_generation, monkeypatch, apply, persist):
    """apply-resolution 도 같은 가드다(Q10) — 컬럼 delta 임시 commit 앞에서 400 이라 commit 0 · 설정 값 그대로 · 생성 0."""
    commits: list[int] = []
    real_commit = db.commit
    monkeypatch.setattr(db, "commit", lambda: commits.append(1) or real_commit())
    row = db.query(RosterConfig).filter(RosterConfig.config_id == 1).one()
    snapshot = {k: getattr(row, k) for k in ("two_offs_after_two_nig", "two_offs_after_three_nig", "off_days")}
    with pytest.raises(HTTPException) as exc_info:
        rc.apply_resolution_endpoint(rc.ApplyResolutionRequest(year=2026, month=11, apply=apply, persist=persist),
                                     current_user=user, db=db)
    assert exc_info.value.status_code == 400
    db.refresh(row)
    assert {k: getattr(row, k) for k in snapshot} == snapshot
    assert commits == [] and no_generation == []


# ── 알려진 구멍 특성화(SPEC_REVIEW G3-5 · 지시서 §5.1-5) ──────────────────────────────────────


def test_known_hole_rejected_card_delta_rides_the_next_card_via_modal_config(db, seed_data, user, sqs_calls, no_inherit):
    """★ 알려진 구멍 — 프론트 `lastGenArgsRef` 는 요청 전에 병합본으로 바뀌고 실패해도 되돌리지 않는다
    (front `Roster_create.tsx:529-534`). 그래서 카드 A(하드락)가 400 이어도 같은 모달에서 카드 B(하드락 밖)를 누르면
    `config` = A 병합본 + B 가 나가 A 의 하드락 완화가 새 프리셋 행으로 굳는다. 11b 가드는 그 요청의 `config_override`
    만 본다(모달 `config` 는 int-4a). 프론트 수정·int-4a 때 뒤집는다 — 그때 포크 행의 `two_offs_after_two_nig` 는
    True(기준 프리셋 값) 여야 한다."""
    baseline = _async(RosterRequest(year=2026, month=11, group_id="GRP001", config=_cfg_payload()), user, db)
    base_id = baseline["materialized_config"]["config_id"]
    card_a, card_b = {"two_offs_after_two_nig": False}, {"off_days": 6}
    modal = {**_cfg_payload(), "config_id": base_id, **card_a}     # 요청 전에 갱신된 lastGenArgsRef.config
    with pytest.raises(HTTPException) as exc_info:
        _async(RosterRequest(year=2026, month=11, group_id="GRP001", config=modal, config_override=card_a), user, db)
    assert exc_info.value.status_code == 400
    out = _async(RosterRequest(year=2026, month=11, group_id="GRP001", config={**modal, **card_b},
                               config_override=card_b), user, db)
    forked = db.query(RosterConfig).filter(RosterConfig.config_id == out["materialized_config"]["config_id"]).one()
    assert forked.config_id != base_id and forked.off_days == 6
    assert forked.two_offs_after_two_nig is False, "알려진 구멍 — 400 난 카드 A 의 완화가 프리셋에 굳었다"
    assert len(sqs_calls) == 2
