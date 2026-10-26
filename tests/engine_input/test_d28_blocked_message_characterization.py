"""D28 재확인 — '일자별 인원 부족' precheck 차단 뒤 사용자에게 나가는 원인 문구·해결 카드의 특성화 시험.

병동: `d28_cause_ward.py`(6명 · D/E/N/M 각 1 · 나이트 확정 원티드 매일 1명 · 8일·18일에 휴가 2명 + 회복 OFF 1명 겹침).
원인 브리지(λ 미니 CP-SAT)는 막지 않고 실제로 돌린다(`allow_bridge_solve` — 브리지 밖 풀이는 그대로 막는다). 실 LLM 호출 0.

- 일반 시험: 지금 동작을 고정한다(차단 · 날짜 · 카드 종류 · 하드락 처방 0 · 브리지가 엉뚱한 카드를 안 낸다).
- 12x 에서 D28-1~3(xfail)을 정상 단언으로 뒤집었고, 해결 카드(버튼 없는 수동 안내) 단언을 더했다.
  근거·수정안은 `.omc/aide_docs/v2/engine_input/ei13_15/d28/RESULT.md`.
"""

from __future__ import annotations

import dataclasses

from . import d28_cause_ward as W
from .harness import run_with_output

_CACHE: dict = {}


def _run(engine) -> dict:
    """시나리오를 한 번만 돌려 응답·브리지 기록을 모은다."""
    if "box" in _CACHE:
        return _CACHE["box"]
    box: dict = {}

    def spy_bridge(mp):
        import services.ontology_graph.lagrangian as lagrangian

        original = lagrangian.explain_infeasibility_from_config

        def spy(*args, **kwargs):
            result = original(*args, **kwargs)
            box["bridge"] = {"classification": result.classification, "top_family": result.top_family,
                             "targets": list(result.targets)}
            return result

        mp.setattr(lagrangian, "explain_infeasibility_from_config", spy)

    scenario = dataclasses.replace(W.scenario(), patches=W.scenario().patches + (spy_bridge,))
    run = run_with_output(engine, scenario)
    box["result"] = run.result
    box["bridge_solves"] = len(W.BRIDGE_SOLVES)
    box["inf"] = (run.result.http_detail or {}).get("infeasibility") or {}
    _CACHE["box"] = box
    return box


def _user_text(inf: dict) -> str:
    """사용자 화면에 닿는 원인 문구 모음 — 요약 · 원인 문장 · 해결 방법 칸 · problem_list 문장."""
    parts = [inf.get("summary_message_ko") or ""]
    parts += [c.get("human_message_ko") or "" for c in inf.get("causes") or []]
    parts += [p.get("rendered_ko") or "" for p in (inf.get("resolution_narrative") or {}).get("problem_list") or []]
    parts += [ch.get("sizing_ko") or "" for o in inf.get("resolution_options") or [] for ch in o.get("changes") or []]
    return "\n".join(parts)


def test_blocks_before_solver_with_both_shortage_days(engine, engine_input_tables):
    box = _run(engine)
    result = box["result"]
    assert (result.outcome, result.path, result.error) == ("http_exception", "precheck_block", "HTTP 500")
    issues = box["inf"]["preflight_issues"]
    assert [(i["reason_code"], i["evidence"]["day"], i["evidence"]["required_total"],
             i["evidence"]["available_nurses"]) for i in issues] == [
        ("GLOBAL_DAY_CAPACITY_SHORTAGE", 7, 4, 3), ("GLOBAL_DAY_CAPACITY_SHORTAGE", 17, 4, 3)]


def test_cause_bridge_really_solves_and_adds_no_card(engine, engine_input_tables):
    box = _run(engine)
    assert box["bridge_solves"] > 0 and box["result"].solve_calls == 0
    assert box["bridge"]["classification"] == "unknown" and box["bridge"]["targets"] == []
    assert [o["option_id"] for o in box["inf"]["resolution_options"]] == ["bundle:treatment:threshold:coverage_min"]


def test_summary_names_first_shortage_day_in_one_based(engine, engine_input_tables):
    assert "일자=8일" in _run(engine)["inf"]["summary_message_ko"]


def test_cards_and_narrative_release_no_hard_lock(engine, engine_input_tables):
    from services.policy.card_filter import forbidden_treatments

    inf = _run(engine)["inf"]
    ids = {t for o in inf["resolution_options"] for t in o["treatment_ids"]}
    ids |= {m["treatment_id"] for p in inf["resolution_narrative"]["problem_list"] for m in p["mapped_treatments"]}
    assert ids and not (ids & set(forbidden_treatments()))
    assert inf["policy_forbidden"] == [] and inf["blocked_by_hard_lock"] == 0


def test_card_points_at_capacity_cause_not_night_or_personal_policy(engine, engine_input_tables):
    inf = _run(engine)["inf"]
    assert [o["manual_required"] for o in inf["resolution_options"]] == [["treatment:threshold:coverage_min"]]
    assert [p["cause_id"] for p in inf["resolution_narrative"]["problem_list"]] == [
        "cause:capacity:daily_total_shortage"]


def test_fix_suggestion_names_vacation_overlap(engine, engine_input_tables):
    assert any("휴가" in s and "분산" in s for s in _run(engine)["inf"]["fix_suggestions_ko"])


# ── D28-1~3 수정 단언 + 해결 카드 확인(12x) ──────────────────────────────────


def test_problem_list_sentence_names_both_days_in_one_based_with_counts(engine, engine_input_tables):
    """D28-1·3: '7일 … ?명 … ?명' 이던 문장 — 막힌 날 전부(1 기준) · 숫자 채움."""
    sentence = _run(engine)["inf"]["resolution_narrative"]["problem_list"][0]["rendered_ko"]
    assert sentence == "8일·18일 총 근무 수요 4명이 간호사 수 3명을 초과합니다."


def test_cause_message_states_needed_and_available_counts(engine, engine_input_tables):
    """D28-2·3: 원인 문장(= 요약)에 필요/가능 숫자 · 막힌 날 전부(1 기준)."""
    inf = _run(engine)["inf"]
    expected = ("어떤 날에 근무 가능한 간호사 수가 그 날 필요한 총 인원보다 적습니다. "
                "(일자=8일·18일, 필요=4 / 가능=3)")
    assert inf["causes"][0]["human_message_ko"] == expected
    assert inf["summary_message_ko"] == expected
    # 둘째 날 이슈 자신의 문장은 자기 날짜 하나 + 숫자
    assert inf["preflight_issues"][1]["human_message_ko"].endswith("(일자=18일, 필요=4 / 가능=3)")


def test_every_shortage_day_is_named_in_user_text(engine, engine_input_tables):
    text = _user_text(_run(engine)["inf"])
    assert "8일" in text and "18일" in text
    assert "?" not in text and "7일" not in text and "17일" not in text


def test_raw_evidence_day_stays_zero_based(engine, engine_input_tables):
    inf = _run(engine)["inf"]
    assert [i["evidence"]["day"] for i in inf["preflight_issues"]] == [7, 17]
    assert inf["causes"][0]["details"]["day_evidence"] == [
        {"day": 7, "required_total": 4, "available_nurses": 3},
        {"day": 17, "required_total": 4, "available_nurses": 3}]


def test_different_numbers_per_day_are_listed_per_day():
    from services.precheck.messaging import humanize_all
    from services.resolution_narrative import _daily_total_shortage_sentence

    out = humanize_all([
        {"reason_code": "GLOBAL_DAY_CAPACITY_SHORTAGE", "evidence": {"day": 7, "required_total": 4, "available_nurses": 3}},
        {"reason_code": "GLOBAL_DAY_CAPACITY_SHORTAGE", "evidence": {"day": 17, "required_total": 5, "available_nurses": 3}}])
    assert out[0]["human_message_ko"].endswith("(일자=8일(필요=4 / 가능=3)·18일(필요=5 / 가능=3))")
    assert out[1]["human_message_ko"].endswith("(일자=18일, 필요=5 / 가능=3)")
    sentence = _daily_total_shortage_sentence("cause:capacity:daily_total_shortage", {"day_evidence": [
        {"day": 17, "required_total": 5, "available_nurses": 3}, {"day": 7, "required_total": 4, "available_nurses": 3}]})
    assert sentence == "8일(수요 4명/간호사 3명)·18일(수요 5명/간호사 3명) 총 근무 수요가 간호사 수를 초과합니다."


def test_demand_card_is_manual_guidance_without_button(engine, engine_input_tables):
    """'일별 시프트 요구 인원 하향' 카드 — 서비스 경로에서 누르면 바뀌는 게 0 이라 버튼 없는 직접 수정 안내로 나간다."""
    from services.treatment_applicator import apply_treatments

    # 서비스가 쓰는 config(daily_shift_requirements 는 dict) 로 적용해 본다 → 아무것도 안 바뀐다(수동 분류).
    cfg = {"daily_shift_requirements": {"D": 1, "E": 1, "N": 1, "M": 1}}
    res = apply_treatments(["treatment:threshold:coverage_min"], cfg)
    assert res.plan.auto_adjustments == [] and res.plan.manual_required == ["treatment:threshold:coverage_min"]
    assert res.patched_config.get("_applied_constraint_adjustments") == []   # 조정 기록 칸만 빈 채 생긴다
    assert res.patched_config["daily_shift_requirements"] == cfg["daily_shift_requirements"]

    (card,) = _run(engine)["inf"]["resolution_options"]
    assert card["treatment_ids"] == [] and card["apply"] == {}
    assert card["manual_required"] == ["treatment:threshold:coverage_min"]
    assert card["title_ko"] == "일별 시프트 요구 인원 (직접 수정)"
    assert card["fix"]["mode"] == "manual_edit" and "버튼" in card["fix"]["how_ko"]
    assert "아래 버튼으로 바로 적용" not in card["fix"]["how_ko"]
    assert "환자안전" in card["fix"]["how_ko"]


def _mixed_bundle(order):
    from services.cp_sat.undiagnosed_probe import treatments_to_resolution_options

    auto = {"treatment_id": "treatment:threshold:max_conseq", "config_key": "max_conseq_work",
            "suggested_value": 6, "trade_off_ko": "연속근무가 늘어납니다."}
    demand = {"treatment_id": "treatment:threshold:coverage_min", "config_key": "daily_shift_requirements",
              "suggested_value": None, "trade_off_ko": "환자 대응 마진 축소"}
    ts = [auto, demand] if order == "auto_first" else [demand, auto]
    (card,) = treatments_to_resolution_options([{"bundle_id": "bundle:x", "treatments": ts}])
    return card


def test_mixed_bundle_does_not_advertise_demand_reduction():
    for order in ("auto_first", "demand_first"):
        card = _mixed_bundle(order)
        assert "일별 시프트 요구 인원" not in card["title_ko"]
        assert [c["config_key"] for c in card["changes"]] == ["max_conseq_work"]
        assert "환자 대응 마진" not in card["trade_off_ko"]
        assert card["manual_required"] == ["treatment:threshold:coverage_min"]
        assert card["apply"] == {"max_conseq_work": 6} and card["treatment_ids"] == []


def test_duplicate_day_issue_and_single_day_sentence():
    from services.precheck.messaging import humanize_all
    from services.resolution_narrative import _daily_total_shortage_sentence

    ev = {"day": 7, "required_total": 4, "available_nurses": 3}
    out = humanize_all([{"reason_code": "GLOBAL_DAY_CAPACITY_SHORTAGE", "evidence": dict(ev)},
                        {"reason_code": "GLOBAL_DAY_CAPACITY_SHORTAGE", "evidence": dict(ev)}])
    assert "8일·8일" not in out[0]["human_message_ko"]
    assert _daily_total_shortage_sentence("cause:capacity:daily_total_shortage", dict(ev)) == \
        "8일 총 근무 수요 4명이 간호사 수 3명을 초과합니다."
