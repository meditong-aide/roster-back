"""EI-5 배관 헬퍼(`services.precheck.presolve_block`) 단위 테스트.

`tests/engine_input/test_presolve_block_characterization.py` 는 실제 두 블록
(precheck 차단·나이트 과공급 차단)이 이 헬퍼로 바뀐 뒤에도 그대로 동작하는지를
본다(통합). 이 파일은 그 헬퍼 자체의 선택지 조합(`record_first`·`on_record_fail`·
`card_merge`)과 탐지기 실패 내성을 DB·엔진 없이 가볍게 본다(단위).

실제 DB 세션 대신 `_FakeDb`(delete/commit/rollback 호출만 기록)를 쓰고,
`services.precheck.build_blocking_payload` 는 monkeypatch 로 고정된 base payload
를 돌려주게 해 헬퍼의 병합 로직만 분리해서 검증한다.
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException
from services.precheck.presolve_block import (
    PRESOLVE_DETECTORS,
    Detector,
    PresolveBlockContext,
    PresolveFinding,
    PresolveView,
    raise_presolve_block,
    run_presolve_detectors,
)


def _identity_build(raw, _view):
    """테스트용 `Detector.build` — `detect` 가 이미 완성된 `PresolveFinding`(또는 `None`)을
    돌려주는 단순 탐지기를 흉내 낼 때, 조립 단계를 그냥 통과시킨다."""
    return raw


class _FakeDb:
    """`db.delete`/`db.commit`/`db.rollback` 호출만 기록하는 가짜 세션."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def delete(self, _obj) -> None:
        self.calls.append("delete")

    def commit(self) -> None:
        self.calls.append("commit")

    def rollback(self) -> None:
        self.calls.append("rollback")


class _RecordFailure(RuntimeError):
    """기록 함수를 통째로 바꿔 끼운 실패(characterization 테스트와 같은 방식)."""


def _make_bctx(**overrides) -> tuple[PresolveBlockContext, list[str], dict]:
    """기록 호출 순서(`order`)와 ctx 플래그를 같이 돌려준다."""
    order: list[str] = []
    ctx: dict = {}

    def build_snapshot(*_a, **_kw):
        order.append("snapshot")
        return "snap"

    def record_status(*_a, **_kw):
        order.append("mark")

    db = _FakeDb()
    base = dict(
        db=db, config=object(), year=2026, month=11, job_id="job-1",
        persistent_fix=None, intervened=False, nurses_dict=[], config_dict={},
        schedule=object(), ctx=ctx,
        record_status=record_status, build_snapshot=build_snapshot,
        log=lambda msg: order.append(f"log:{msg}"),
    )
    base.update(overrides)
    return PresolveBlockContext(**base), order, ctx


def _base_payload_patch(monkeypatch, order: list[str] | None = None):
    """`services.precheck.build_blocking_payload` 를 고정 payload 로 바꾼다."""
    import services.precheck as precheck

    def fake_build(_precheck_result):
        if order is not None:
            order.append("payload")
        return {"infeasibility": {
            "resolution_options": [{"id": "base"}],
            "summary_message_ko": "base msg",
            "fix_suggestions_ko": ["base fix"],
        }}

    monkeypatch.setattr(precheck, "build_blocking_payload", fake_build)


# ── record_first × on_record_fail — 기록 순서 ────────────────────────────────


def test_record_first_true_runs_mark_before_payload(monkeypatch):
    bctx, order, ctx = _make_bctx()
    _base_payload_patch(monkeypatch, order)
    finding = PresolveFinding(reason_code="X", severity="hard", evidence={})

    with pytest.raises(HTTPException) as exc_info:
        raise_presolve_block(
            bctx, [finding], record_first=True, on_record_fail="proceed", card_merge="prepend")

    assert order == ["snapshot", "mark", "payload"], "기록(스냅샷→mark) 뒤 payload 순서여야 한다"
    assert ctx.get("recorded") is True
    assert exc_info.value.status_code == 500
    assert bctx.db.calls == ["delete", "commit"]


def test_record_first_false_runs_payload_before_mark(monkeypatch):
    bctx, order, ctx = _make_bctx()
    _base_payload_patch(monkeypatch, order)
    finding = PresolveFinding(reason_code="MONTHLY_NIGHT_FORCED_OVERSUPPLY", severity="error",
                               evidence={}, cards=[{"id": "night"}], message_ko="night msg",
                               exclusive_cards=True)

    with pytest.raises(HTTPException):
        raise_presolve_block(
            bctx, [finding], record_first=False, on_record_fail="block", card_merge="exclusive")

    assert order.index("payload") < order.index("mark"), "나이트 차단은 payload → 기록 순서여야 한다"
    assert ctx.get("recorded") is True


def test_record_first_true_on_record_fail_proceed_propagates(monkeypatch):
    """precheck 의 '기록 실패 → 솔버 진행' — 예외가 그대로 올라가 HTTPException 이 안 난다."""
    bctx, order, ctx = _make_bctx()

    def raising_record(*_a, **_kw):
        raise _RecordFailure("기록 실패 주입")

    bctx.record_status = raising_record
    _base_payload_patch(monkeypatch, order)
    finding = PresolveFinding(reason_code="X")

    with pytest.raises(_RecordFailure):
        raise_presolve_block(
            bctx, [finding], record_first=True, on_record_fail="proceed", card_merge="prepend")

    assert "payload" not in order, "기록이 실패로 끝나면 payload 단계까지 가지 않아야 한다"
    assert bctx.db.calls == [], "초안 삭제도 실행되지 않아야 한다"
    assert ctx.get("recorded") is None


def test_record_first_false_on_record_fail_block_still_raises(monkeypatch):
    """나이트의 '기록 실패해도 차단' — 기록 예외를 삼키고 그대로 HTTP 500 을 낸다."""
    bctx, order, ctx = _make_bctx()

    def raising_record(*_a, **_kw):
        raise _RecordFailure("기록 실패 주입")

    bctx.record_status = raising_record
    _base_payload_patch(monkeypatch, order)
    finding = PresolveFinding(reason_code="X", cards=[{"id": "c"}], message_ko="m",
                               exclusive_cards=True)

    with pytest.raises(HTTPException) as exc_info:
        raise_presolve_block(
            bctx, [finding], record_first=False, on_record_fail="block", card_merge="exclusive")

    assert exc_info.value.status_code == 500
    assert bctx.db.calls == ["delete", "commit"], "기록 실패와 무관하게 초안 삭제까지 실행돼야 한다"
    assert ctx.get("recorded") is None, "기록 자체가 실패했으니 recorded 플래그는 안 선다"
    assert any(o.startswith("log:") for o in order), "기록 실패는 로그로 남아야 한다"


# ── card_merge — 카드·메시지 병합 ─────────────────────────────────────────────


def test_card_merge_prepend_keeps_base_options_after_overlay(monkeypatch):
    bctx, order, _ctx = _make_bctx()
    _base_payload_patch(monkeypatch)
    finding = PresolveFinding(reason_code=None, cards=[{"id": "overlay"}], message_ko="overlay msg")

    with pytest.raises(HTTPException) as exc_info:
        raise_presolve_block(
            bctx, [finding], base_issues=[{"reason_code": "BASE", "severity": "hard", "evidence": {}}],
            record_first=True, on_record_fail="proceed", card_merge="prepend")

    inf = exc_info.value.detail["infeasibility"]
    assert inf["resolution_options"] == [{"id": "overlay"}, {"id": "base"}]
    assert inf["summary_message_ko"] == "overlay msg"
    assert inf["fix_suggestions_ko"] == ["overlay msg", "base fix"]


def test_card_merge_exclusive_overwrites_base_options(monkeypatch):
    bctx, _order, _ctx = _make_bctx()
    _base_payload_patch(monkeypatch)
    finding = PresolveFinding(reason_code="X", cards=[{"id": "only"}], message_ko="only msg")

    with pytest.raises(HTTPException) as exc_info:
        raise_presolve_block(
            bctx, [finding], record_first=True, on_record_fail="proceed", card_merge="exclusive")

    inf = exc_info.value.detail["infeasibility"]
    assert inf["resolution_options"] == [{"id": "only"}]
    assert inf["fix_suggestions_ko"] == ["only msg"], "배타 모드는 기존 fix_suggestions 를 섞지 않는다"


def test_finding_exclusive_cards_overrides_call_level_prepend(monkeypatch):
    """finding 자신이 `exclusive_cards=True` 면 호출측 card_merge="prepend" 를 덮는다."""
    bctx, _order, _ctx = _make_bctx()
    _base_payload_patch(monkeypatch)
    finding = PresolveFinding(reason_code="X", cards=[{"id": "only"}], exclusive_cards=True)

    with pytest.raises(HTTPException) as exc_info:
        raise_presolve_block(
            bctx, [finding], record_first=True, on_record_fail="proceed", card_merge="prepend")

    inf = exc_info.value.detail["infeasibility"]
    assert inf["resolution_options"] == [{"id": "only"}]


def test_no_overlay_findings_leaves_base_payload_untouched(monkeypatch):
    """findings 에 카드·메시지가 없으면(빈 리스트) build_blocking_payload 결과 그대로."""
    bctx, _order, _ctx = _make_bctx()
    _base_payload_patch(monkeypatch)

    with pytest.raises(HTTPException) as exc_info:
        raise_presolve_block(
            bctx, [], base_issues=[],
            record_first=True, on_record_fail="proceed", card_merge="prepend")

    inf = exc_info.value.detail["infeasibility"]
    assert inf["resolution_options"] == [{"id": "base"}]
    assert inf["summary_message_ko"] == "base msg"


def test_base_issues_passed_through_verbatim(monkeypatch):
    """`base_issues` 가 있으면 findings 를 거치지 않고 그대로 build_blocking_payload 로 간다."""
    import services.precheck as precheck

    seen: dict = {}

    def fake_build(precheck_result):
        seen["issues"] = precheck_result["issues"]
        return {"infeasibility": {"resolution_options": []}}

    monkeypatch.setattr(precheck, "build_blocking_payload", fake_build)
    bctx, _order, _ctx = _make_bctx()
    original_issues = [{"reason_code": "FOO", "severity": "hard", "evidence": {}, "ontology": {"x": 1}}]

    with pytest.raises(HTTPException):
        raise_presolve_block(
            bctx, [], base_issues=original_issues,
            record_first=True, on_record_fail="proceed", card_merge="prepend")

    assert seen["issues"] == original_issues
    assert seen["issues"][0] is original_issues[0], (
        "원본 issue dict 가 그대로(재구성 없이) 전달돼야 한다(온톨로지 태그 등 손실 방지)")


def test_findings_without_base_issues_build_issue_list_from_reason_code():
    """`base_issues` 가 없으면 reason_code 가 있는 finding 만 issue 로 간다."""
    from services.precheck.presolve_block import _collect_issues

    overlay_only = PresolveFinding(reason_code=None, cards=[{"id": "c"}])
    real_issue = PresolveFinding(reason_code="X", severity="hard", evidence={"a": 1})

    issues = _collect_issues([overlay_only, real_issue], base_issues=None)

    assert issues == [{"reason_code": "X", "severity": "hard", "evidence": {"a": 1}}]


# ── 탐지기 실패 시 진행 ──────────────────────────────────────────────────────


def test_run_presolve_detectors_skips_failing_detector_and_keeps_others(monkeypatch):
    import services.precheck.presolve_block as presolve_block

    ok_finding = PresolveFinding(reason_code="OK")

    def failing(_view):
        raise RuntimeError("탐지기 실패 주입")

    def ok(_view):
        return ok_finding

    def none_result(_view):
        return None

    monkeypatch.setattr(
        presolve_block, "PRESOLVE_DETECTORS",
        (Detector("failing", failing, _identity_build),
         Detector("ok", ok, _identity_build),
         Detector("none", none_result, _identity_build)),
    )
    view = PresolveView(nurses=[], config_dict={}, num_days=30,
                         code2main={}, id_to_main={}, fixed_cells=[],
                         year=2026, month=11)

    findings = run_presolve_detectors(view)

    assert findings == [ok_finding], "실패한 탐지기는 건너뛰고 나머지는 계속 돈다"


def test_run_presolve_detectors_logs_failing_detector_id_and_returns_empty(monkeypatch):
    """탐지기 하나가 예외를 내면(등록표에 그것 하나뿐) — 로그에 그 id 와 예외 타입 이름이
    남고 findings 는 빈 목록이다(차단할 Finding 이 없으니 호출부는 `raise_presolve_block`
    을 부르지 않는다)."""
    import services.precheck.presolve_block as presolve_block

    def failing(_view):
        raise RuntimeError("탐지기 실패 주입")

    monkeypatch.setattr(
        presolve_block, "PRESOLVE_DETECTORS",
        (Detector("night_forced_oversupply", failing, _identity_build),))
    view = PresolveView(nurses=[], config_dict={}, num_days=30,
                         code2main={}, id_to_main={}, fixed_cells=[], year=2026, month=11)
    logged: list[str] = []

    findings = run_presolve_detectors(view, log=logged.append)

    assert findings == [], "탐지기 예외는 로그만 남기고 진행 — 차단 Finding 이 없다"
    assert len(logged) == 1
    assert "night_forced_oversupply" in logged[0], "로그에 실패한 탐지기 id 가 남아야 한다"
    assert "RuntimeError" in logged[0], "로그에 예외 타입 이름이 남아야 한다(finding 2)"


def test_run_presolve_detectors_log_tag_prefixes_detection_failure(monkeypatch):
    """호출부가 `log_tag` 를 넘기면 탐지 실패 로그 접두어가 그걸로 바뀐다 — 나이트
    호출부가 `"[NightOversupply]"` 를 넘겨 전환 전 grep 태그를 보존한다(finding 2)."""
    import services.precheck.presolve_block as presolve_block

    def failing(_view):
        raise KeyError("config")

    monkeypatch.setattr(
        presolve_block, "PRESOLVE_DETECTORS",
        (Detector("night_forced_oversupply", failing, _identity_build),))
    view = PresolveView(nurses=[], config_dict={}, num_days=30,
                         code2main={}, id_to_main={}, fixed_cells=[], year=2026, month=11)
    logged: list[str] = []

    findings = run_presolve_detectors(view, log=logged.append, log_tag="[NightOversupply]")

    assert findings == []
    assert logged[0].startswith("[NightOversupply]"), "나이트 호출부는 grep 태그를 보존해야 한다"
    assert "KeyError" in logged[0], "예외 타입 이름이 로그에 남아야 한다"


def test_run_presolve_detectors_all_none_returns_empty_findings():
    """등록된 탐지기가 전부 `None` 을 돌려주면(모순 없음) findings 는 빈 목록 — 호출부는
    통과시킨다(`raise_presolve_block` 을 부르지 않는다). 예외를 삼켜서 빈 목록이 된 것과
    구분하기 위해 로그도 비어 있어야 한다(finding 5)."""
    assert any(d.id == "night_forced_oversupply" for d in PRESOLVE_DETECTORS), (
        "등록표에 나이트 과공급 탐지기가 있어야 한다")
    view = PresolveView(nurses=[], config_dict={}, num_days=30,
                         code2main={}, id_to_main={}, fixed_cells=[], year=2026, month=11)
    logged: list[str] = []

    findings = run_presolve_detectors(view, log=logged.append)

    assert findings == [], "지금 등록된 나이트 탐지기는 모순이 없으면 None 을 돌려준다"
    assert logged == [], "모순이 없어 빈 목록인 것이지 예외를 삼켜서가 아니어야 한다"


def test_build_night_forced_oversupply_finding_assembles_expected_shape():
    """`_build_night_forced_oversupply_finding` 을 합성 데이터로 직접 불러 Finding 모양을
    본다 — 지금까지 이 조립을 직접 보는 단위 테스트가 0건이었다(finding 5)."""
    from services.precheck.presolve_block import _build_night_forced_oversupply_finding

    nfo = {
        "forced_n_sum": 36, "n_slots": 30, "excess": 6,
        "targets": [{"nurse_id": "n1", "name": "김간호", "current": 5, "field": "n_min"}],
    }
    view = PresolveView(nurses=[], config_dict={}, num_days=30,
                         code2main={}, id_to_main={}, fixed_cells=[], year=2026, month=11)

    finding = _build_night_forced_oversupply_finding(nfo, view)

    assert finding.reason_code == "MONTHLY_NIGHT_FORCED_OVERSUPPLY"
    assert finding.exclusive_cards is True, "다른 카드와 섞이면 안 되는 배타 카드여야 한다"
    assert finding.cards is not None and len(finding.cards) == 1
    assert finding.evidence["forced_n_sum"] == 36
    assert finding.evidence["n_slots"] == 30
    assert finding.evidence["year"] == 2026 and finding.evidence["month"] == 11
    assert finding.message_ko


def test_raise_presolve_block_merges_cards_and_issues_message_last_wins(monkeypatch):
    """두 탐지기가 각각 Finding 을 낼 때 — issue·카드는 둘 다 쌓이지만, 메시지
    (`summary_message_ko`/`fix_suggestions_ko`)는 마지막 finding 것만 남는다(finding 3 —
    `run_presolve_detectors` docstring 의 "전부 병합" 표현이 틀렸던 부분을 고정한다)."""
    import services.precheck as precheck

    seen: dict = {}

    def fake_build(precheck_result):
        seen["issues"] = precheck_result["issues"]
        return {"infeasibility": {"resolution_options": [{"id": "base"}],
                                   "summary_message_ko": "base msg",
                                   "fix_suggestions_ko": ["base fix"]}}

    monkeypatch.setattr(precheck, "build_blocking_payload", fake_build)
    bctx, _order, _ctx = _make_bctx()
    first = PresolveFinding(reason_code="A", severity="error", evidence={"a": 1},
                             cards=[{"id": "card-a"}], message_ko="msg-a")
    second = PresolveFinding(reason_code="B", severity="error", evidence={"b": 2},
                              cards=[{"id": "card-b"}], message_ko="msg-b")

    with pytest.raises(HTTPException) as exc_info:
        raise_presolve_block(
            bctx, [first, second],
            record_first=True, on_record_fail="proceed", card_merge="prepend")

    assert {i["reason_code"] for i in seen["issues"]} == {"A", "B"}, (
        "두 finding 의 issue 가 둘 다 쌓여야 한다")
    inf = exc_info.value.detail["infeasibility"]
    assert {c["id"] for c in inf["resolution_options"]} == {"card-a", "card-b", "base"}, (
        "두 finding 의 카드가 둘 다 payload 에 들어가야 한다")
    assert inf["summary_message_ko"] == "msg-b", "메시지는 병합되지 않고 마지막 finding 것이 이긴다"


def test_registered_detector_finding_feeds_into_raise_presolve_block_and_blocks(monkeypatch):
    """등록표 왕복 전체(night 블록이 실제로 하는 일) — 탐지기가 Finding 을 내면
    `run_presolve_detectors` 가 그걸 모아 돌려주고, 그 목록을 그대로 `raise_presolve_block`
    에 넘기면 HTTP 500 으로 차단된다(카드 1장 배타 교체까지)."""
    import services.precheck.presolve_block as presolve_block

    night_finding = PresolveFinding(
        reason_code="MONTHLY_NIGHT_FORCED_OVERSUPPLY", severity="error",
        evidence={"forced_n_sum": 36, "n_slots": 30}, cards=[{"id": "night"}],
        message_ko="night msg", exclusive_cards=True,
    )

    def detect(_view):
        return night_finding

    monkeypatch.setattr(
        presolve_block, "PRESOLVE_DETECTORS",
        (Detector("night_forced_oversupply", detect, _identity_build),))
    view = PresolveView(nurses=[], config_dict={}, num_days=30,
                         code2main={}, id_to_main={}, fixed_cells=[], year=2026, month=11)

    findings = run_presolve_detectors(view)
    assert findings == [night_finding], "Finding 을 낸 탐지기는 결과에 그대로 쌓인다"

    bctx, _order, _ctx = _make_bctx()
    _base_payload_patch(monkeypatch)

    with pytest.raises(HTTPException) as exc_info:
        raise_presolve_block(
            bctx, findings, record_first=False, on_record_fail="block", card_merge="exclusive")

    assert exc_info.value.status_code == 500
    inf = exc_info.value.detail["infeasibility"]
    assert inf["resolution_options"] == [{"id": "night"}], "탐지기가 낸 카드 1장으로 차단돼야 한다"
