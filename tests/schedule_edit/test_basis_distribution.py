"""판정 기준 사본(생성 당시 값)과 가로축 분배 지표.

기대값은 규칙을 손으로 따라가 센 값이다.
"""

from __future__ import annotations

import json

from services.schedule_edit import basis
from services.schedule_edit.distribution import metrics
from services.schedule_edit.requests import AvoidCell, SetCell
from tests.schedule_edit.test_solver import make_ctx, propose


def test_basis_payload_restores_every_input():
    ctx = make_ctx({"A": "DDOON", "B": "OODDN"}, cov_min={"D": 1}, cov_max={"D": 2}, off_days=2)
    ctx.frozen[("A", 1)] = "확정 원티드"
    ctx.frozen[("B", 0)] = "휴가·공가"                 # 칸에서 다시 계산되는 이유 — 사본에 넣지 않는다
    ctx.forbidden[("A", 4)] = {"N"}
    ctx.wishes[("B", 2)] = {"D"}
    ctx.mutex = {"A": {"partner_id": "B", "days": {3}}}
    ctx.team_of, ctx.team_min = {"A": 1, "B": 2}, {1: {"D": 1}}
    ctx.tail = {"A": ["N", "N", "O", "O", "D", "D"]}
    ctx.n2n_gap, ctx.same_shift_k, ctx.isolated_work_hard = 7, 3, True
    payload = json.loads(json.dumps(basis.to_payload(ctx)))   # DB 의 JSON 칸을 거친 것과 같게

    restored = make_ctx({"A": "DDOON", "B": "OODDN"})
    restored.config = payload["config"]
    basis._apply_cells_rules(restored, payload)
    basis._apply_people(restored, payload)
    basis._apply_sequence(restored, payload)
    basis._apply_wishes(restored, payload)

    assert restored.frozen == {("A", 1): "확정 원티드"}
    assert restored.forbidden == {("A", 4): {"N"}}
    assert restored.wishes == {("B", 2): {"D"}}
    assert restored.mutex == {"A": {"partner_id": "B", "days": {3}}}
    assert restored.team_min == {1: {"D": 1}} and restored.team_of == {"A": 1, "B": 2}
    assert restored.cov_min == ctx.cov_min and restored.cov_max == ctx.cov_max
    assert restored.off_bounds == {"A": (2, 2), "B": (2, 2)}
    assert restored.tail == ctx.tail
    assert (restored.n2n_gap, restored.same_shift_k, restored.isolated_work_hard) == (7, 3, True)
    assert payload["daily_state"][0] == {"D": 1, "E": 0, "N": 0, "M": 0}


def test_save_as_inherits_the_source_basis(db, monkeypatch):
    from datetime import datetime

    from db.models import ScheduleEditBasis
    monkeypatch.setattr(basis, "_table_ready", False)
    ScheduleEditBasis.__table__.create(bind=db.get_bind(), checkfirst=True)
    db.add(ScheduleEditBasis(schedule_id="src000000001", basis_version=1, payload={"cov_min": [{"D": 3}]},
                             created_at=datetime.now()))
    db.flush()
    basis.inherit_edit_basis(db, "new000000001", "src000000001")
    row = db.get(ScheduleEditBasis, "new000000001")
    assert row is not None and row.payload == {"cov_min": [{"D": 3}]}
    assert row.source_schedule_id == "src000000001"
    basis.inherit_edit_basis(db, "new000000002", "nobasis00001")   # 원본에 사본이 없으면 아무것도 안 만든다
    assert db.get(ScheduleEditBasis, "new000000002") is None


def test_nurse_added_after_snapshot_is_frozen():
    payload = basis.to_payload(make_ctx({"A": "DDOO"}))
    later = make_ctx({"A": "DDOO", "NEW": "DDOO"})      # 사본 뒤 명단에 들어온 사람
    basis._freeze_unknown(later, payload)
    assert all(later.frozen.get(("NEW", d)) == "판정 기준 사본에 없는 간호사" for d in range(4))
    assert not any(n == "A" for n, _d in later.frozen)


def test_de_tolerance_zero_and_disabled():
    ctx = make_ctx({"A": "DDDE", "B": "EEED"})          # |D-E| = 2 씩
    assert metrics(ctx, ctx.main)["de"] == 0            # 기본 허용폭 2
    ctx.config["de_balance_tolerance"] = 0
    assert metrics(ctx, ctx.main)["de"] == 4            # 0 은 0 그대로(2 로 바뀌면 안 된다)
    ctx.config["de_balance_enable"] = False
    assert metrics(ctx, ctx.main)["de"] == 0


def test_distribution_metrics():
    # OFF: A 2·B 2·C 0 → 격차 2 / N: A 2·B 2·C 0 → 2 / D/E: C 는 D2·E2 라 0
    ctx = make_ctx({"A": "NNOO", "B": "OONN", "C": "DDEE"})
    assert metrics(ctx, ctx.main) == {"off_range": 2, "n_range": 2, "de": 0}


def test_distribution_metrics_skip_night_only_and_weekend_off():
    ctx = make_ctx({"A": "NNOO", "B": "OONN", "C": "DDEE"})
    ctx.n_only = {"A"}
    ctx.weekend_off = {"C"}
    assert metrics(ctx, ctx.main)["n_range"] == 0   # 남는 대상이 B 하나뿐


def test_edit_prefers_choice_that_keeps_distribution():
    # A 4일 D 를 빼야 한다. 바꾸는 칸은 어느 쪽이든 1칸:
    #   OFF → A OFF 1·B 0 → OFF 격차 0→1 / N → 나이트 격차 0→1 / E → 격차 그대로, D/E 차 2(허용폭 안)
    ctx = make_ctx({"A": "DDDD", "B": "DDDD"}, config={"not_one_night": False})
    res = propose(ctx, [AvoidCell("A", 3, frozenset({"D"}))])
    assert res.status == "ok"
    best = res.candidates[0]
    assert [(c.day, c.after_main) for c in best.changes] == [(3, "E")]
    assert best.quality == []


# ── 레버 ────────────────────────────────────────────────────────────────────


def test_infeasible_request_offers_a_lever_and_applying_it_finds_a_candidate():
    # A 의 OFF 2개(3·4일)는 주휴라 못 바꾼다 → 1일 OFF 는 OFF 수 상한을 1 넘겨야만 된다
    ctx = make_ctx({"A": "DDOO"}, off_days=2, config={"not_one_night": False})
    ctx.frozen[("A", 2)] = ctx.frozen[("A", 3)] = "주휴·특수 휴무"
    res = propose(ctx, [SetCell("A", 0, "O")])
    assert res.status == "infeasible" and len(res.levers) == 1
    lever = res.levers[0]
    assert lever.allow == {("OFF수", "A", None, None, "상한"): 1}
    assert lever.lose == ["A 개인 OFF 수 상한을 1 넘김"]
    assert lever.changes == (None, 1)
    again = propose(ctx, [SetCell("A", 0, "O")], allow=lever.allow)
    assert again.status == "ok" and again.candidates[0].changed_count == 1


def test_hard_lock_has_no_lever():
    ctx = make_ctx({"A": "NOOOO"}, config={"not_one_night": False})
    ctx.frozen[("A", 0)] = "확정 원티드"
    res = propose(ctx, [SetCell("A", 1, "D")])
    assert res.status == "infeasible" and res.levers == []


def test_span_is_part_of_the_loss():
    # 1일 OFF 요청. A 는 OFF 를 2일 또는 4일에 돌려받을 수 있다(칸 수 같음) — 기간이 짧은 2일 쪽이어야 한다.
    ctx = make_ctx({"A": "DODO", "B": "ODOD"}, cov_min={"D": 1, "E": 0, "N": 0}, cov_max={"D": 1},
                   off_days=2, config={"not_one_night": False})
    res = propose(ctx, [SetCell("A", 0, "O")])
    best = res.candidates[0]
    assert best.span == (0, 1) and best.loss == 2 * 4 + 1
