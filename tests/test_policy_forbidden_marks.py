"""EI-11 11c — '정책상 금지' 표지(`.omc/aide_docs/v2/engine_input/ei11/EI11_SPEC_v2.md` §2.5 · §4.1).

싱크(`finalize_infeasibility`)는 카드 칸만 거른다. 카드가 아닌 출구 칸은 생산자 쪽에서 표지한다(H1).
  - 처방 항목: 원인을 덮는 금지 처방이 `policy_forbidden` 에 `kind="treatment"` 로 남는다(`blocked_by_hard_lock` 에 센다).
  - problem_list: 금지 처방 `mapped_treatments` 에 `policy_forbidden`·`rule_nos` 표지, 그 원인의 `cause_solution_ko` 재작성.
    원인 목록·`rendered_ko`(진단 문구)는 그대로다.
  - 상한형 처방(월 나이트 상한 +1) rationale·동의 문구(apply_hint)에 '(최대 15회 — 하드락 #6)'(처방 목록·서술·problem_list·graph · 11r 단위·동의 문구).
  - fix_plan: 막힌 family 의 T2 축(전월 경계 전이 #2)은 `protected_axes` 로, 상한형 축 문구에는 상한.
  - 원인 문구(D30): coupled_sequence 는 조건 없이 '하드락이라 완화할 수 없어요' 문구 — 두 출구(precheck 브리지·사후).
  - 고장 주입: 표지 판정 실패 → 하드락 가족 처방 전부 표지·`policy_filter_error` 1 · fix_plan 판정 실패 → 하드락 family 축 보호.
EI-11 11d — constraint_impact '적용 불가' 표지(control 은 표지만 — 모드 없음).
  - 모듈 목록 `ModuleSummary.hard_lock` · 미리보기 조정 기록(`applicable: False`·rule_nos — 기본값이면 칸 없음) · conflict_probe 단계.
  - 고장 주입 3건: 조정 기록 · 모듈 목록 · conflict_probe 모두 예외 없이 막는 쪽으로 표시한다.
"""

from __future__ import annotations

import ast
import copy
import dataclasses
import logging
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from services import roster_create_service as rcs
from services.constraint_impact import control
from services.constraint_impact.conflict_probe import RankedCandidate, build_probe_plan
from services.ontology_graph import lagrangian
from services.policy import card_filter as cf
from services.policy import hard_lock as hl
from services.precheck import fix_plan as fp
from services.precheck.payload import build_blocking_payload, build_unrecoverable_payload
from services.precheck.presolve_block import PresolveBlockContext, raise_presolve_block
from services.semantics.axis_registry import get_default_axis_registry
from services.semantics.ontology import friendly_config_key_label, get_default_ontology

REPO_ROOT = Path(__file__).resolve().parents[1]
_CAP15 = " (최대 15회 — 하드락 #6)"
_CAP5 = " (최대 5일 — 하드락 #1)"
_HARD_LOCK_ONLY_SOLUTION = "하드락 규칙이라 자동 조정이 없습니다 — 인원·고정 근무를 직접 조정해 주세요."
_D30 = ("근무 규칙(야간 회복·연속·전이)이 서로 겹쳐 배치가 안 돼요. 이 규칙들은 하드락이라 완화할 수 없어요 — "
        "간호사 인원을 늘리거나 고정 근무(원티드·휴가)를 조정해 주세요. 필요 인원을 줄이는 것은 환자안전을 직접 확인한 뒤에만 "
        "해 주세요.")
_D30_CONSECUTIVE = _D30 + " 연속근무 상한은 최대 5일까지만 올릴 수 있어요."
_FIX_PLAN_ERROR_WHY = "하드락 판정을 지금 할 수 없어 자동 완화 안내에서 뺐습니다."

#: E4 원인 6개(§2.2 ⑧) — 자동 처방에 하드락 처방이 든 원인.
_E4_CAUSES = (
    "cause:capacity:daily_night_shortage", "cause:carryover:prev_month_n_tail_blocks_start",
    "cause:consecutive:work_limit_blocks_coverage", "cause:recovery:three_n_two_off_blocks_demand",
    "cause:recovery:two_n_two_off_blocks_demand", "cause:transition:nod_pattern_forces_infeasibility",
)
#: 금지 처방이 막는 설정 키(control 매핑 그대로 — 엄격 표본 diff 의 BLOCK 키).
_FORBIDDEN_KEYS = {
    "treatment:disable:night_recovery": {"two_offs_after_two_nig", "two_offs_after_three_nig"},
    "treatment:disable:transition_ban": {"ban_n_to_d", "ban_e_to_d", "ban_n_to_e"},
    "treatment:threshold:consecutive_work": {"max_conseq_work"},
}

_NIGHT_ROWS = [{"cap_by_axis": {"config": 15, "working": 25}, "capacity_days": 25, "capped_by": "config",
                "capped_by_all": ["config"], "is_night_dedicated": False, "name": f"N0{i}", "night_cap": 15,
                "nurse_id": f"N0{i}", "personal_night_cap": None} for i in (1, 2)]
#: EI5(presolve 골든 — RN 2명 vs 요구 D2/E2/N2) 모양의 precheck 이슈 3종.
_EI5_ISSUES = [
    {"reason_code": "GLOBAL_DAY_CAPACITY_SHORTAGE", "severity": "hard",
     "evidence": {"available_nurses": 2, "day": 0, "required_total": 6}},
    {"reason_code": "CAPACITY_TOTAL_SHORTAGE", "severity": "hard",
     "evidence": {"avg_daily_demand": 6.0, "capacity": 50, "capacity_total": 50, "conseq_cap_binding": None,
                  "demand_by_shift": {"D": 60, "E": 60, "N": 60}, "num_days": 30, "nurse_count": 2, "required": 180,
                  "required_total": 180, "shortage": 130}},
    {"reason_code": "MONTHLY_NIGHT_CAPACITY_SHORTAGE", "severity": "hard",
     "evidence": {"avg_daily_N_demand": 2.0, "monthly_N_need": 60, "n_capacity": 30, "n_required": 60,
                  "night_allowed_count": 2, "night_capable_nurses": _NIGHT_ROWS, "night_capacity": 30, "shortage": 30}},
]


def _violation(cause_id: str, alias: str) -> dict:
    return {"reason_code": alias, "node_id": cause_id, "details": {}, "human_message_ko": f"matrix synthesis: {cause_id}"}


def _ei5() -> dict:
    return build_blocking_payload({"issues": copy.deepcopy(_EI5_ISSUES)})["infeasibility"]


def _without_marks(monkeypatch, build):
    """표지 후처리를 끈 같은 빌더 결과(대조용) — `mark_treatment_views` 를 아무것도 안 하는 함수로 바꾼다."""
    with monkeypatch.context() as m:
        m.setattr(cf, "mark_treatment_views", lambda *_a, **_k: [])
        return build()


def _problem(inf: dict, cause_id: str) -> dict:
    (item,) = [p for p in inf["resolution_narrative"]["problem_list"] if p["cause_id"] == cause_id]
    return item


def _expected_message(tid: str, rule_nos) -> str:
    t = get_default_ontology().get_treatment(tid)
    label = friendly_config_key_label(t.config_key) or t.label  # 11r — 누른 카드 제목과 같은 출처
    rules = "·".join(hl.rule_label(n) for n in rule_nos)
    return f"'{label}' 처방은 {rules} 규칙을 완화해 정책상 내보내지 않습니다(원인 진단은 그대로 남깁니다)."


# ── 처방 항목(`policy_forbidden` kind=treatment) ─────────────────────────────────


@pytest.mark.parametrize("builder", ["blocking", "unrecoverable"])
@pytest.mark.parametrize("cause_id", _E4_CAUSES)
def test_e4_cause_records_its_forbidden_treatments(cause_id, builder):
    """E4 원인 6개 × 별칭 × 두 빌더 — `policy_forbidden` 에 그 원인을 덮는 금지 처방(`kind="treatment"`)·덮는 원인·규칙 번호·
    막는 키·문구가 남고 `blocked_by_hard_lock` == 그 길이."""
    onto = get_default_ontology()
    forbidden = cf.forbidden_treatments()
    covering = [t.treatment_id for t in onto.treatments_for_cause(cause_id) if t.treatment_id in forbidden]
    assert covering, "표본 무의미 — 그 원인을 덮는 금지 처방이 있어야 한다"
    for alias in onto.get_cause(cause_id).aliases:
        if builder == "blocking":
            inf = build_blocking_payload({"issues": [{"reason_code": alias, "severity": "hard", "evidence": {}}]})
        else:
            inf = build_unrecoverable_payload(violated_constraints=[_violation(cause_id, alias)])
        inf = inf["infeasibility"]
        items = [p for p in inf["policy_forbidden"] if p["kind"] == "treatment"]
        assert [(p["id"], p["causes"], p["rule_nos"]) for p in items] == [
            (t, [cause_id], list(forbidden[t])) for t in covering], alias
        for p in items:
            assert set(p["keys"]) == _FORBIDDEN_KEYS[p["id"]] and len(p["keys"]) == len(set(p["keys"]))
            assert p["message_ko"] == _expected_message(p["id"], p["rule_nos"])
            assert "filter_error" not in p
        assert inf["blocked_by_hard_lock"] == len(inf["policy_forbidden"]) and inf["policy_filter_error"] == 0


def test_policy_forbidden_for_causes_resolves_aliases_and_merges_covering_causes():
    """원인 별칭은 정규화하고, 같은 금지 처방이 여러 원인을 덮으면 항목 하나에 원인을 정렬해 모은다. 모르는 원인은 건너뛴다."""
    items = cf.policy_forbidden_for_causes(["RECOVERY_3N2OFF_BLOCKS", "RECOVERY_2N2OFF_BLOCKS", "RECOVERY_2N2OFF_BLOCKS",
                                            "NO_SUCH_CAUSE", None, "UNDIAGNOSED"])
    assert [(p["kind"], p["id"], p["causes"]) for p in items] == [(
        "treatment", "treatment:disable:night_recovery",
        ["cause:recovery:three_n_two_off_blocks_demand", "cause:recovery:two_n_two_off_blocks_demand"])]
    assert cf.policy_forbidden_for_causes([]) == [] and cf.policy_forbidden_for_causes(None) == []


def test_ei5_policy_forbidden_carries_night_recovery_record():
    """§2.5 골든 — EI5 는 night_recovery 처방 항목 1건(덮는 원인 monthly_night_shortage) · `blocked_by_hard_lock` 1."""
    inf = _ei5()
    assert inf["policy_forbidden"] == [{
        "kind": "treatment", "id": "treatment:disable:night_recovery", "rule_nos": [4, 5],
        "keys": inf["policy_forbidden"][0]["keys"], "causes": ["cause:capacity:monthly_night_shortage"],
        "message_ko": _expected_message("treatment:disable:night_recovery", [4, 5])}]
    assert (inf["blocked_by_hard_lock"], inf["policy_filter_error"]) == (1, 0)


# ── problem_list 표지 · cause_solution_ko 재작성 · 상한 문구 ─────────────────────────


def test_causes_and_rendered_ko_stay_and_only_marked_items_change(monkeypatch):
    """원인 목록·problem_list `rendered_ko` 는 표지 전과 같다. 금지 처방에만 `policy_forbidden`·`rule_nos` 가 붙고,
    금지도 상한형도 아닌 처방·원인은 그대로다."""
    inf, base = _ei5(), _without_marks(monkeypatch, _ei5)
    assert inf["causes"] == base["causes"]
    pl, base_pl = inf["resolution_narrative"]["problem_list"], base["resolution_narrative"]["problem_list"]
    assert [(p["cause_id"], p["rendered_ko"]) for p in pl] == [(p["cause_id"], p["rendered_ko"]) for p in base_pl]
    assert pl[:2] == base_pl[:2], "금지·상한형 처방이 없는 원인은 그대로다"
    night_recovery, monthly_cap = pl[2]["mapped_treatments"]
    assert night_recovery == {**base_pl[2]["mapped_treatments"][0], "policy_forbidden": True, "rule_nos": [4, 5]}
    assert monthly_cap == {**base_pl[2]["mapped_treatments"][1], "rationale_ko": "개인당 월 최대 야간 횟수를 1 증가" + _CAP15 + "."}


def test_cause_solution_ko_is_rebuilt_from_allowed_treatments():
    """§2.5 골든 문구 그대로 — 금지 아닌 처방 문장만으로 다시 짓는다(상한 문구 포함)."""
    assert _problem(_ei5(), "cause:capacity:monthly_night_shortage")["cause_solution_ko"] == (
        "월간 N 수요 60회가 N 상한 용량 30회를 초과합니다. → 해결책: 개인당 월 최대 야간 횟수를 1 증가 (최대 15회 — 하드락 #6).")


@pytest.mark.parametrize("cause_id", ["cause:recovery:two_n_two_off_blocks_demand",
                                      "cause:consecutive:work_limit_blocks_coverage"])
def test_hard_lock_only_cause_gets_the_fixed_solution_sentence(cause_id, monkeypatch):
    """처방이 하드락 처방뿐인 원인 — 고정 문구('하드락 규칙이라 자동 조정이 없습니다 …')로 다시 짓는다(진단 문구는 그대로).
    금지 처방 항목은 표지 두 칸만 늘고 rationale 은 그대로다(상한형 family 의 금지 처방 — 연속근무 — 에도 상한 문구 없음)."""
    alias = get_default_ontology().get_cause(cause_id).aliases[0]

    def build():
        return build_unrecoverable_payload(violated_constraints=[_violation(cause_id, alias)])["infeasibility"]

    inf, base = build(), _without_marks(monkeypatch, build)
    item, base_item = _problem(inf, cause_id), _problem(base, cause_id)
    forbidden = cf.forbidden_treatments()
    assert item["mapped_treatments"] == [{**m, "policy_forbidden": True, "rule_nos": list(forbidden[m["treatment_id"]])}
                                         for m in base_item["mapped_treatments"]]
    assert item["rendered_ko"] == base_item["rendered_ko"]
    assert item["cause_solution_ko"] == f"{item['rendered_ko']} → 해결책: {_HARD_LOCK_ONLY_SOLUTION}"


def test_cap_suffix_reaches_every_treatment_view():
    """상한형 처방 rationale 의 상한 문구 — 처방 목록 · 서술 action_levers · problem_list · graph 처방 노드."""
    inf = _ei5()
    tid = "treatment:threshold:monthly_night_cap"
    want = "개인당 월 최대 야간 횟수를 1 증가" + _CAP15 + "."
    views = [t["rationale_ko"] for b in inf["treatment_recommendations"] for t in b.get("treatments") or []
             if t.get("treatment_id") == tid]
    views += [a["rationale_ko"] for a in inf["resolution_narrative"]["action_levers"] if a["treatment_id"] == tid]
    views += [m["rationale_ko"] for p in inf["resolution_narrative"]["problem_list"] for m in p["mapped_treatments"]
              if m["treatment_id"] == tid]
    views += [n["rationale_ko"] for n in inf["graph"]["nodes"] if n.get("id") == tid]
    assert len(views) == 5 and set(views) == {want}
    # 11r(리뷰 LOW) — 같은 처방의 동의 문구(apply_hint)에도 같은 상한 문구가 한 번 들어간다.
    hints = [t["apply_hint"]["human_message_ko"] for b in inf["treatment_recommendations"]
             for t in b.get("treatments") or [] if t.get("treatment_id") == tid and t.get("apply_hint")]
    assert len(hints) == 2
    assert set(hints) == {"'월간 N cap 상향' (월 야간 한도(개인) → 상향) 적용 후 재시도합니다" + _CAP15 + ". 동의하시겠습니까?"}


@pytest.mark.parametrize(("label", "particle"), [
    ("월간 N 한도", "를"), ("전월 경계 전이 금지", "를"), ("일자별 D 필요 인원", "을"), ("연속근무 상한", "을"),
    ("N", "을"), ("", "을"),
])
def test_fix_plan_object_particle_follows_the_last_syllable(label, particle):
    """11r(리뷰 LOW) — 받침 없는 축 라벨은 '를'('월간 N 한도을' 오류), 받침 있음·한글 아님은 '을'(예전 그대로)."""
    assert fp._object_particle(label) == particle


def test_drop_summary_echo_only_when_no_cards_remain():
    """11r(리뷰 MEDIUM) — 카드가 0장일 때만 '조정 제안'에서 요약과 같은 줄을 뺀다. 카드가 남으면 그대로다."""
    from services.precheck.presolve_block import _drop_summary_echo_without_cards as drop

    no_cards = {"resolution_options": [], "summary_message_ko": _D30, "fix_suggestions_ko": [_D30, "다른 안내"]}
    drop(no_cards)
    assert no_cards["fix_suggestions_ko"] == ["다른 안내"]
    with_cards = {"resolution_options": [{"option_id": "x"}], "summary_message_ko": _D30, "fix_suggestions_ko": [_D30]}
    drop(with_cards)
    assert with_cards["fix_suggestions_ko"] == [_D30]
    other = {"summary_message_ko": "요약", "fix_suggestions_ko": ["다른 안내"]}
    drop(other)
    assert other["fix_suggestions_ko"] == ["다른 안내"]


def test_mark_treatment_views_is_idempotent():
    """두 번 불러도 한 번과 같다 — 상한 문구는 이미 있으면 그대로, 재작성은 같은 결과다."""
    inf = _ei5()
    once = copy.deepcopy((inf["treatment_recommendations"], inf["resolution_narrative"]))
    cause_ids = [c["reason_code"] for c in inf["causes"]]
    items = cf.mark_treatment_views(inf["treatment_recommendations"], inf["resolution_narrative"], cause_ids)
    assert (inf["treatment_recommendations"], inf["resolution_narrative"]) == once
    assert [p["id"] for p in items] == ["treatment:disable:night_recovery"]


@pytest.mark.parametrize(("text", "want"), [
    ("1 증가.", "1 증가 (최대 15회 — 하드락 #6)."),
    ("1 증가.\n적용 대상: 김간호", "1 증가 (최대 15회 — 하드락 #6).\n적용 대상: 김간호"),
    ("마침표 없음", "마침표 없음 (최대 15회 — 하드락 #6)"),
    ("1 증가 (최대 15회 — 하드락 #6).", "1 증가 (최대 15회 — 하드락 #6)."),
])
def test_cap_suffix_goes_before_the_first_period(text, want):
    assert cf._with_cap_suffix(text, _CAP15) == want


@pytest.mark.parametrize(("entries", "want"), [
    ([{"mode": "capped", "cap": 5, "key": "k", "rule_nos": [1]}, {"mode": "blocked", "rule_nos": [2]},
      {"mode": "blocked", "rule_nos": [3, 2], "error": True}], {"mode": "blocked", "rule_nos": [2, 3], "error": True}),
    ([{"mode": "marked", "rule_nos": []}, {"mode": "capped", "cap": 15, "key": "k", "rule_nos": [6]}, {"mode": "ok"}],
     {"mode": "capped", "cap": 15, "key": "k", "rule_nos": [6]}),
    ([{"mode": "ok", "rule_nos": []}, {"mode": "marked", "rule_nos": [], "marks": []}],
     {"mode": "marked", "rule_nos": [], "marks": []}),
    ([{"mode": "ok", "rule_nos": []}], {"mode": "ok", "rule_nos": []}),
], ids=["blocked_wins", "capped_over_marked", "marked_over_ok", "ok"])
def test_fold_modes_order_blocked_capped_marked_ok(entries, want):
    """family 접기 순서 — blocked(rule_nos 합집합·error 유지) → capped(그 항목) → marked → ok(지금 레지스트리에는 섞인 family 가
    없어 합성 판정으로 본다)."""
    assert cf._fold_modes(entries) == want


def test_family_modes_fold_actions_per_family():
    """family 단위 판정 — blocked(#2 · #4·#5 · #3) · capped(#1 상한 5 · #6 상한 15) · marked · ok."""
    modes = cf.family_modes()
    assert modes["BoundaryTransitionBan"] == {"mode": "blocked", "rule_nos": [2]}
    assert modes["NightRecovery"] == {"mode": "blocked", "rule_nos": [4, 5]}
    assert modes["ConsecutiveNightLimit"] == {"mode": "blocked", "rule_nos": [3]}
    assert modes["ConsecutiveWorkLimit"] == {"mode": "capped", "key": "max_conseq_work", "cap": 5, "rule_nos": [1]}
    assert modes["MonthlyNightCap"] == {"mode": "capped", "key": "max_nig_per_month", "cap": 15, "rule_nos": [6]}
    assert modes["BanNightBeforeFixedOff"]["mode"] == "marked"
    assert {f for f, e in modes.items() if e["mode"] == "ok"} == {
        "CoverageMin", "GradeMax", "GradeMin", "TeamMin", "WeekendOffOnly"}
    assert cf.capped_suffix("MonthlyNightCap", "set_threshold") == _CAP15
    assert cf.capped_suffix("ConsecutiveWorkLimit", "set_threshold") == _CAP5
    assert cf.capped_suffix("NightRecovery", "disable_module") == "" == cf.capped_suffix("MonthlyNightCap", None)
    assert cf.mode_suffix(None) == "" == cf.mode_suffix({"mode": "capped", "rule_nos": [6]})


# ── fix_plan — 막힌 축 보호 · 상한 문구 · 남은 축 기준 truncated ────────────────────


def _fix_plan_for(axis_ids: list[str]) -> dict:
    """검증기 실패 셀의 `blocking_axes` 로 축을 직접 거는 fix_plan(이유 코드 없음 — 단계 판정 unknown)."""
    cells = [{"blocking_axes": list(axis_ids)}]
    return fp.build_fix_plan(structural_diagnosis={}, preflight_issues=[], conflict_cores=[], pool_snapshot={},
                             violated_constraints=[{"reason_code": "X", "details": {"validator_evidence": {
                                 "top_failed_cells": cells}}}])


_MANY_AXES = ["carryover_transition", "monthly_n_cap", "consecutive_work_limit", "carryover_consecutive_work",
              "day_capacity", "evening_capacity", "night_capacity", "team_min", "grade_min"]


def test_fix_plan_moves_blocked_axis_to_protected_and_caps_the_rest():
    plan = _fix_plan_for(_MANY_AXES)
    assert plan["protected_axes"] == [{
        "axis_id": "carryover_transition", "family": "BoundaryTransitionBan", "tier": "T2", "label_ko": "전월 경계 전이 금지",
        "why_protected_ko": "하드락 #2(ND·ED·NE 전이 금지) — 정책상 완화 대상이 아닙니다.", "hard_lock_rule_nos": [2]}]
    actions = {a["axis_id"]: a["human_message_ko"] for a in plan["axis_actions"]}
    assert "carryover_transition" not in actions
    assert actions["consecutive_work_limit"].endswith(_CAP5) and actions["carryover_consecutive_work"].endswith(_CAP5)
    assert [a["priority"] for a in plan["axis_actions"]] == [1, 2, 3, 4, 5]
    assert plan["axis_actions_truncated"] == len(_MANY_AXES) - 1 - 5, "남은 축(8개) 기준"
    assert plan["tier_summary"]["T2"] == len(_MANY_AXES), "tier_summary 는 그대로"


def test_fix_plan_monthly_n_cap_message_carries_the_cap():
    plan = _fix_plan_for(["monthly_n_cap", "day_capacity"])
    assert [(a["axis_id"], a["human_message_ko"]) for a in plan["axis_actions"]] == [
        ("day_capacity", "일자별 D 필요 인원을 1 줄여보세요"), ("monthly_n_cap", "월간 N 한도를 1 늘려보세요" + _CAP15)]
    assert plan["protected_axes"] == [] and plan["axis_actions_truncated"] == 0


def test_build_axis_action_puts_the_suffix_after_the_scope_hint():
    """상한 문구는 안내 문구 끝 — 대상 예시('(예: …)')가 있으면 그 뒤다."""
    registry = get_default_axis_registry()
    axis = registry.get("night_capacity")
    cells = {"top_failed_cells": [{"day": 3, "shift": "N", "shortage": 1}]}
    with_targets = fp._build_axis_action(axis=axis, registry=registry, reasons=[], validator_evidence=cells,
                                         shortages=[], priority=1, hard_lock_suffix=" (꼬리)")
    bare = fp._build_axis_action(axis=axis, registry=registry, reasons=[], validator_evidence={}, shortages=[], priority=1)
    assert with_targets["human_message_ko"] == "일자별 N 필요 인원을 1 줄여보세요 (예: 3일 N) (꼬리)"
    assert bare["human_message_ko"] == "일자별 N 필요 인원을 1 줄여보세요"


# ── 원인 문구(D30) — 직접 · 배선 · 두 출구 ─────────────────────────────────────────


@pytest.mark.parametrize(("top", "max_consec", "want"), [
    ("night_recovery", None, _D30), ("transition", None, _D30), ("night_recovery", 4, _D30),
    ("consecutive", 4, _D30_CONSECUTIVE), ("consecutive", 0, _D30_CONSECUTIVE),
    ("consecutive", 5, _D30), ("consecutive", 6, _D30), ("consecutive", None, _D30),
], ids=["night_recovery-None", "transition-None", "night_recovery-4", "consecutive-4-tail", "consecutive-0-tail",
        "consecutive-5", "consecutive-6", "consecutive-None"])
def test_coupled_sequence_certificate_text(top, max_consec, want):
    assert lagrangian._coupled_sequence_certificate(top, max_consec) == want


def test_coupled_sequence_certificate_skips_the_tail_when_cap_is_unreadable(monkeypatch):
    from services.engine_contract import settings

    monkeypatch.setattr(settings, "lookup", lambda _key: None)
    assert lagrangian._coupled_sequence_certificate("consecutive", 4) == _D30


def test_lagrangian_source_no_longer_recommends_relaxing_a_rule():
    assert "규칙 하나를 완화하세요" not in (REPO_ROOT / "app/services/ontology_graph/lagrangian.py").read_text(encoding="utf-8")


def _coupled_night_recovery():
    """`tests/test_lagrangian_blame.py` 의 coupled_sequence 입력 — top night_recovery."""
    return lagrangian.explain_infeasibility(2, 6, {"N": 2}, night_cap=6, recovery=True)


def test_explain_infeasibility_uses_the_d30_certificate():
    exp = _coupled_night_recovery()
    assert (exp.classification, exp.top_family) == ("coupled_sequence", "night_recovery")
    assert exp.certificate == lagrangian._coupled_sequence_certificate("night_recovery", None) == _D30


class _FakeDb:
    def delete(self, _obj) -> None:
        pass

    def commit(self) -> None:
        pass

    def rollback(self) -> None:
        pass


def test_precheck_bridge_shows_d30_and_keeps_the_family_card_out(monkeypatch):
    """출구 ① precheck 브리지 — 원인 브리지 finding 이 `summary_message_ko` 를 D30 문구로 덮고, 그 원인 가족 카드
    (`cause:night_recovery`, tradeoff)는 카드 칸에 없고 `policy_forbidden` 에 `cause_card` 로 남는다."""
    exp = _coupled_night_recovery()
    monkeypatch.setattr(lagrangian, "explain_infeasibility_from_config", lambda *_a, **_k: exp)
    finding = rcs._build_precheck_cause_bridge_finding([], {}, 2026, 11)
    assert finding is not None and finding.message_ko == _D30
    bctx = PresolveBlockContext(
        db=_FakeDb(), config=object(), year=2026, month=11, job_id="job-1", persistent_fix=None, intervened=False,
        nurses_dict=[], config_dict={}, schedule=object(), record_status=lambda *_a, **_k: None,
        build_snapshot=lambda *_a, **_k: "snap", log=lambda _msg: None)
    with pytest.raises(HTTPException) as exc_info:
        raise_presolve_block(bctx, [], record_first=True, on_record_fail="block", card_merge="prepend",
                             extra_findings=lambda: [finding])
    inf = exc_info.value.detail["infeasibility"]
    assert inf["summary_message_ko"] == _D30 and inf["resolution_options"] == []
    assert _D30 not in inf["fix_suggestions_ko"], "11r — 카드 0장이면 '조정 제안'에 요약을 다시 싣지 않는다(두 번 뜸 방지)"
    assert "cause:night_recovery" not in [o.get("option_id") for o in inf["resolution_options"]]
    assert ("cause_card", "cause:night_recovery") in [(p["kind"], p["id"]) for p in inf["policy_forbidden"]]


def _rcs_tree() -> ast.Module:
    return ast.parse((REPO_ROOT / "app/services/roster_create_service.py").read_text(encoding="utf-8"))


def test_post_path_suggestion_and_cause_explanation_carry_the_certificate():
    """출구 ② 사후 — 원인이 unknown 이 아니면 `fix_suggestions_ko = [_cause.certificate]`, `cause_explanation.certificate` 도
    `_cause.certificate` 다(원문 배선 — 사후 경로는 생성 서비스를 실패까지 돌려야 닿고 주 골든에 coupled_sequence 가 없다)."""
    ifs = [n for n in ast.walk(_rcs_tree()) if isinstance(n, ast.If)
           and ast.unparse(n.test) == "_cause.classification != 'unknown'"]
    assigns = [(ast.unparse(s.targets[0]), ast.unparse(s.value)) for n in ifs for s in n.body if isinstance(s, ast.Assign)]
    assert ("unrecoverable['infeasibility']['fix_suggestions_ko']", "[_cause.certificate]") in assigns
    explanations = [n.value for n in ast.walk(_rcs_tree()) if isinstance(n, ast.Assign)
                    and ast.unparse(n.targets[0]) == "unrecoverable['infeasibility']['cause_explanation']"]
    assert len(explanations) == 1 and isinstance(explanations[0], ast.Dict)
    fields = {ast.unparse(k): ast.unparse(v) for k, v in zip(explanations[0].keys, explanations[0].values, strict=True)}
    assert fields["'certificate'"] == "_cause.certificate"


# ── hard_case(11a-2 뒤 C-UNCOVER) 유지 ─────────────────────────────────────────────


def test_hard_lock_only_cause_keeps_c_uncover_hard_case():
    cause_id = "cause:recovery:two_n_two_off_blocks_demand"
    alias = get_default_ontology().get_cause(cause_id).aliases[0]
    inf = build_unrecoverable_payload(violated_constraints=[_violation(cause_id, alias)])["infeasibility"]
    assert inf["hard_case"]["is_hard"] is True and "C-UNCOVER" in inf["hard_case"]["criteria_matched"]


# ── 고장 주입(11c) ─────────────────────────────────────────────────────────────────


def _boom(*_a, **_k):
    raise RuntimeError("주입 — 하드락 판정 실패")


def test_fault_marks_judgement_failure_marks_every_hard_lock_family_treatment(monkeypatch, caplog):
    """§2.0.6 ⑤ — 표지 판정이 실패하면 하드락 가족 처방(`HARD_LOCK_FAMILIES_FALLBACK`)을 모두 표지하고(월 나이트 상한 처방 포함)
    `policy_filter_error` 가 1 이다. 처방 목록·서술은 비지 않고, 하드락 가족 밖 처방 원인은 그대로다."""
    base = _ei5()
    monkeypatch.setattr(cf, "forbidden_treatments", _boom)
    inf = _ei5()
    family_ids = cf.hard_lock_family_treatment_ids()
    marked = {m["treatment_id"]: m for p in inf["resolution_narrative"]["problem_list"] for m in p["mapped_treatments"]
              if m.get("policy_forbidden") is True}
    assert set(marked) == {"treatment:disable:night_recovery", "treatment:threshold:monthly_night_cap"} <= family_ids
    assert all(m["rule_nos"] == [] for m in marked.values())
    item = _problem(inf, "cause:capacity:monthly_night_shortage")
    assert item["cause_solution_ko"] == f"{item['rendered_ko']} → 해결책: {_HARD_LOCK_ONLY_SOLUTION}"
    assert _problem(inf, "cause:capacity:daily_total_shortage") == _problem(base, "cause:capacity:daily_total_shortage")
    assert inf["policy_filter_error"] == 1 and inf["blocked_by_hard_lock"] == len(inf["policy_forbidden"])
    assert [(p["kind"], p["id"], p.get("filter_error")) for p in inf["policy_forbidden"]] == [("treatment", None, True)]
    assert inf["treatment_recommendations"] and inf["resolution_narrative"] is not None
    assert any(r.levelno == logging.WARNING and r.name == cf.logger.name for r in caplog.records)


def test_fault_marks_fallback_also_failing_marks_every_treatment(monkeypatch):
    """대체 판정(하드락 가족 처방 집합)도 못 읽으면 원인의 처방을 모두 표지한다 — 완화 권고 문장이 하나도 안 남는다.
    (빌더 전체로 부르면 set cover 의 대체 판정도 같이 터져 서술이 통째로 빈다 — 11a-2 동작. 그래서 표지 함수만 직접 부른다.)"""
    base = _without_marks(monkeypatch, _ei5)
    monkeypatch.setattr(cf, "forbidden_treatments", _boom)
    monkeypatch.setattr(cf, "hard_lock_family_treatment_ids", _boom)
    items = cf.mark_treatment_views(base["treatment_recommendations"], base["resolution_narrative"],
                                    [c["reason_code"] for c in base["causes"]])
    for p in base["resolution_narrative"]["problem_list"]:
        assert all(m.get("policy_forbidden") is True for m in p["mapped_treatments"])
        assert p["cause_solution_ko"] == f"{p['rendered_ko']} → 해결책: {_HARD_LOCK_ONLY_SOLUTION}"
    assert [(p["kind"], p["id"], p.get("filter_error")) for p in items] == [("treatment", None, True)]


def test_fault_fix_plan_judgement_failure_protects_hard_lock_family_axes(monkeypatch, caplog):
    """§2.0.6 ⑥ — fix_plan 축 판정이 실패하면 하드락 family(`HARD_LOCK_FAMILIES_FALLBACK`)의 T2 축을 모두 보호로 옮긴다."""
    monkeypatch.setattr(cf, "family_modes", _boom)
    plan = _fix_plan_for(["carryover_transition", "monthly_n_cap", "consecutive_work_limit", "day_capacity"])
    assert [(a["axis_id"], a["human_message_ko"]) for a in plan["axis_actions"]] == [
        ("day_capacity", "일자별 D 필요 인원을 1 줄여보세요")]
    protected = {a["axis_id"]: a for a in plan["protected_axes"]}
    assert set(protected) == {"carryover_transition", "monthly_n_cap", "consecutive_work_limit"}
    assert all(a["why_protected_ko"] == _FIX_PLAN_ERROR_WHY and a["hard_lock_rule_nos"] == [] and a["tier"] == "T2"
               for a in protected.values())
    assert any(r.levelno == logging.WARNING and r.name == fp.logger.name for r in caplog.records)


# ── 11d — constraint_impact '적용 불가' 표지 ────────────────────────────────────────

_RECORD_KEYS = {"family", "action", "scope_filter", "target_value", "reason"}


def _modules() -> dict:
    """모듈 목록 — 스냅샷은 목록이 읽는 칸(config·join·active_days·nurses)만 둔 가벼운 대역."""
    snapshot = SimpleNamespace(config_payload={}, join=[], active_days_by_nurse={}, nurses=[])
    return {m.family: m for m in control.list_constraint_modules(snapshot)}


def test_module_list_carries_hard_lock_verdict_per_action(monkeypatch):
    """`hard_lock` 칸 — BTB·NightRecovery·ConsecutiveNight blocked · ConsecutiveWork cap 5 · MonthlyNightCap cap 15 ·
    BanNight marked · 그 밖은 빈 칸. 라우터가 `asdict` 로 그대로 내보낸다. 판정은 호출당 한 번 짓는다."""
    calls = []
    real = cf.family_action_policy
    monkeypatch.setattr(cf, "family_action_policy", lambda ontology=None: calls.append(1) or real(ontology))
    mods = _modules()
    assert len(calls) == 1
    expected = {
        "BoundaryTransitionBan": {"disable_module": {"mode": "blocked", "rule_nos": [2]}},
        "NightRecovery": {"disable_module": {"mode": "blocked", "rule_nos": [4, 5]}},
        "ConsecutiveNightLimit": {"set_threshold": {"mode": "blocked", "rule_nos": [3]}},
        "ConsecutiveWorkLimit": {"set_threshold": {"mode": "capped", "rule_nos": [1], "cap": 5,
                                                   "key": "max_conseq_work"}},
        "MonthlyNightCap": {"set_threshold": {"mode": "capped", "rule_nos": [6], "cap": 15,
                                              "key": "max_nig_per_month"}},
        "BanNightBeforeFixedOff": {"disable_module": {"mode": "marked", "rule_nos": []}},
    }
    assert {f: m.hard_lock for f, m in mods.items() if m.hard_lock} == expected
    assert dataclasses.asdict(mods["MonthlyNightCap"])["hard_lock"] == expected["MonthlyNightCap"]
    assert mods["TeamMin"].hard_lock == {} and mods["TeamMin"].supported_actions


def _night_cfg() -> dict:
    return {"two_offs_after_two_nig": True, "two_offs_after_three_nig": True, "max_nig_per_month": 10,
            "ban_night_before_fixed_off": True, "ban_night_before_fixed_wanted_off": True,
            "team_min_by_team": {"T1": {"D": 2}}, "team_min_soft_fallback": False}


def test_preview_records_mark_blocking_adjustments_and_still_apply_them():
    """미리보기 기록 — 하드락을 완화하는 조정은 그대로 적용되되 `applicable: False`·`blocked_by_hard_lock`(rule_nos)이 붙고,
    인접 설정 변경은 `hard_lock_relax_marks` 만, 하드락과 무관하거나 상한 안이면 기록 칸이 늘지 않는다(기본값)."""
    out = control.apply_adjustments_to_config(_night_cfg(), [
        {"family": "NightRecovery", "action": "disable_module"},
        {"family": "MonthlyNightCap", "action": "set_threshold", "target_value": 16},
        {"family": "MonthlyNightCap", "action": "set_threshold", "target_value": 15},
        {"family": "BanNightBeforeFixedOff", "action": "disable_module"},
        {"family": "TeamMin", "action": "force_soft_mode"},
    ])
    assert (out["two_offs_after_two_nig"], out["two_offs_after_three_nig"], out["max_nig_per_month"]) == (
        False, False, 15), "표지만 단다 — 적용은 그대로"
    night, over, within, ban, team = out["_applied_constraint_adjustments"]
    assert night["applicable"] is False
    assert {(b["key"], tuple(b["rule_nos"])) for b in night["blocked_by_hard_lock"]} == {
        ("two_offs_after_two_nig", (5,)), ("two_offs_after_three_nig", (4,))}
    assert over["applicable"] is False and [(b["key"], b["rule_nos"]) for b in over["blocked_by_hard_lock"]] == [
        ("max_nig_per_month", [6])]
    assert all(b["message_ko"] for b in [*night["blocked_by_hard_lock"], *over["blocked_by_hard_lock"]])
    assert set(within) == _RECORD_KEYS and set(team) == _RECORD_KEYS
    assert set(ban) == _RECORD_KEYS | {"hard_lock_relax_marks"} and "applicable" not in ban
    assert {m["key"] for m in ban["hard_lock_relax_marks"]} == {"ban_night_before_fixed_off",
                                                                "ban_night_before_fixed_wanted_off"}


def _candidate(family: str) -> RankedCandidate:
    return RankedCandidate(family=family, score=1.0, relaxation_priority=1, scope_explosion="low", emit_count=1)


def test_probe_plan_steps_carry_hard_lock_verdicts():
    """conflict_probe 단계마다 `applicable`(늘) — 하드락 끄기는 `blocked_by_hard_lock`, 인접은 `hard_lock_relax_marks`,
    control 이 지원하지 않는 끄기(엔진이 내는 NotOneNight·AllowedShiftMask 등)는 '적용 불가'(`unsupported_action`)."""
    families = ["BoundaryTransitionBan", "TeamMin", "BanNightBeforeFixedOff", "NotOneNight", "NightRecovery"]
    plan = build_probe_plan(ranked_candidates=[_candidate(f) for f in families])
    actions = {step.family: step.action for step in plan}
    base = {"action": "disable_module"}
    assert actions["BoundaryTransitionBan"] == {
        **base, "family": "BoundaryTransitionBan", "reason": "conflict_probe step 1", "applicable": False,
        "blocked_by_hard_lock": [2]}
    assert actions["TeamMin"] == {**base, "family": "TeamMin", "reason": "conflict_probe step 2", "applicable": True}
    assert actions["BanNightBeforeFixedOff"]["applicable"] is True
    assert [m["key"] for m in actions["BanNightBeforeFixedOff"]["hard_lock_relax_marks"]] == ["ban_night_before_fixed_off"]
    assert (actions["NotOneNight"]["applicable"], actions["NotOneNight"]["unsupported_action"]) == (False, True)
    assert actions["NightRecovery"]["blocked_by_hard_lock"] == [4, 5]


def test_constraint_control_matrix_and_probe_order_are_unchanged():
    """표지는 칸만 더한다 — 지원 매트릭스·probe 순서·단계 수는 그대로다(모드 없음)."""
    plan = build_probe_plan(ranked_candidates=[_candidate(f) for f in "ABCDEFG"], max_steps=3)
    assert [(s.order, s.family, s.action["action"]) for s in plan] == [
        (1, "A", "disable_module"), (2, "B", "disable_module"), (3, "C", "disable_module")]
    assert control.supported_actions_for("ConsecutiveNightLimit") == ["set_threshold"]


# ── 고장 주입(11d) ─────────────────────────────────────────────────────────────────


def test_fault_control_record_judgement_failure_marks_every_record(monkeypatch, caplog):
    """§2.0.6 ⑦ — 조정 기록 판정(`card_filter.delta_violations`)이 실패해도 조정은 예외 없이 적용되고, 기록마다
    `applicable: False`·`hard_lock_check_error: True`."""
    monkeypatch.setattr(cf, "delta_violations", _boom)
    out = control.apply_adjustments_to_config(_night_cfg(), [
        {"family": "NightRecovery", "action": "disable_module"}, {"family": "TeamMin", "action": "force_soft_mode"}])
    assert out["two_offs_after_two_nig"] is False and out["team_min_soft_fallback"] is True
    for record in out["_applied_constraint_adjustments"]:
        assert (record["applicable"], record["hard_lock_check_error"]) == (False, True)
    assert any(r.levelno == logging.WARNING and r.name == control.logger.name for r in caplog.records)


def test_fault_module_list_judgement_failure_blocks_fallback_families(monkeypatch, caplog):
    """§2.5-7 — 모듈 목록 판정(`family_action_policy`)이 실패해도 목록은 나오고, 대체 집합 family 의 지원 action 마다
    `{"mode": "blocked", "rule_nos": [], "error": True}`, 그 밖 family 는 빈 칸."""
    monkeypatch.setattr(cf, "family_action_policy", _boom)
    mods = _modules()
    for family, module in mods.items():
        if family in cf.HARD_LOCK_FAMILIES_FALLBACK:
            assert module.hard_lock == {a: {"mode": "blocked", "rule_nos": [], "error": True}
                                        for a in module.supported_actions}, family
        else:
            assert module.hard_lock == {}, family
    assert {f for f, m in mods.items() if m.hard_lock} == set(cf.HARD_LOCK_FAMILIES_FALLBACK)
    assert any(r.levelno == logging.WARNING and r.name == control.logger.name for r in caplog.records)


def test_fault_probe_plan_judgement_failure_marks_every_step(monkeypatch, caplog):
    """§2.5-9 — conflict_probe 판정이 실패해도 계획은 나오고, 단계마다 `applicable: False`·`hard_lock_check_error: True`."""
    from services.constraint_impact import conflict_probe

    monkeypatch.setattr(cf, "family_action_policy", _boom)
    plan = build_probe_plan(ranked_candidates=[_candidate("TeamMin"), _candidate("BoundaryTransitionBan")])
    assert [(s.action["applicable"], s.action["hard_lock_check_error"]) for s in plan] == [(False, True), (False, True)]
    assert any(r.levelno == logging.WARNING and r.name == conflict_probe.logger.name for r in caplog.records)
