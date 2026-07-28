"""온톨로지 소프트정렬: probe_relaxations priority_families (regression).

presolve(max-flow) 가 지목한 병목 완화군을 먼저 검증하고, 거기서 풀리면 나머지는 생략(soft).
못 풀면 나머지도 폴백 → 완결성 유지. priority 없으면 기존 순서 그대로.
"""
from __future__ import annotations

import pytest

from services.cp_sat.undiagnosed_probe import probe_relaxations


# search 없는 고정델타 카탈로그(결정론적 — 실제 솔버 불필요)
CAT = [
    {"id": "a_night", "family": "night_cap", "label_ko": "야간상한",
     "apply": lambda c: {"x_night": 1}},
    {"id": "b_off", "family": "off_budget", "label_ko": "OFF완화",
     "apply": lambda c: {"x_off": 1}},
    {"id": "c_team", "family": "team", "label_ko": "팀완화",
     "apply": lambda c: {"x_team": 1}},
]


def _resolve_only(*keys):
    """지정 delta 키가 있으면 feasible=True 인 fake resolve_fn."""
    def _fn(cfg):
        return (any(cfg.get(k) for k in keys)), {}
    return _fn


def test_priority_family_first_and_early_stop():
    # off_budget 만 풀림. 우선군=off_budget → b_off 먼저 검증→풀림→나머지(a,c) 생략.
    res = probe_relaxations(
        {}, _resolve_only("x_off"),
        catalog=CAT, verify=False, try_combo=False,
        priority_families=["off_budget"],
    )
    probed_ids = [p["id"] for p in res["all_probed"]]
    assert probed_ids == ["b_off"]  # 우선군만 검증, a_night·c_team 생략
    assert any(r["id"] == "b_off" and r["feasible"] for r in res["all_probed"])
    assert res["found"] is True


def test_fallback_when_priority_fails():
    # team 만 풀림. 우선군=off_budget(안 풀림) → 나머지도 폴백 검증 → c_team 발견.
    res = probe_relaxations(
        {}, _resolve_only("x_team"),
        catalog=CAT, verify=False, try_combo=False,
        priority_families=["off_budget"],
    )
    probed_ids = {p["id"] for p in res["all_probed"]}
    assert probed_ids == {"a_night", "b_off", "c_team"}  # 완결성: 전부 검증됨
    assert res["found"] is True
    assert any(r["id"] == "c_team" and r["feasible"] for r in res["all_probed"])


def test_stop_after_halts_once_enough_found():
    # 전부 풀리는 상황: stop_after=1 이면 첫 feasible 후 즉시 종료(나머지 미검증).
    res = probe_relaxations(
        {}, lambda cfg: (True, {}),
        catalog=CAT, verify=False, try_combo=False,
        stop_after=1,
    )
    assert len(res["all_probed"]) == 1  # 첫 항목만 검증하고 종료
    assert res["found"] is True


def test_stop_after_with_priority_targets_bottleneck_first():
    # 우선군(off_budget) 먼저 + stop_after=1 → b_off 만 검증하고 종료(a_night·c_team 안 봄).
    res = probe_relaxations(
        {}, lambda cfg: (True, {}),
        catalog=CAT, verify=False, try_combo=False,
        priority_families=["off_budget"], stop_after=1,
    )
    assert [p["id"] for p in res["all_probed"]] == ["b_off"]


def test_no_priority_probes_all_in_order():
    # priority 없음 → 기존 동작(전체 순차).
    res = probe_relaxations(
        {}, _resolve_only("x_team"),
        catalog=CAT, verify=False, try_combo=False,
    )
    assert [p["id"] for p in res["all_probed"]] == ["a_night", "b_off", "c_team"]


def test_combo_first_hit_stops_early():
    # 단일 전부 실패, (a_night, b_off) 쌍만 feasible → 전수 대신 첫 hit 에서 종료.
    calls = []
    def _resolve(cfg):
        calls.append(1)
        return (bool(cfg.get("x_night") and cfg.get("x_off")), {})  # 둘 다여야 풀림(콤보)
    res = probe_relaxations(
        {}, _resolve,
        catalog=CAT, verify=False, try_combo=True,
        priority_families=["night_cap", "off_budget"],  # a_night·b_off 앞으로
    )
    assert res["combo"] is not None
    assert {m["id"] for m in res["combo"]["members"]} == {"a_night", "b_off"}
    # 단일 3회 + 콤보 첫 hit 1회 = 4 (전수면 6). (b_off,c_team)·(a_night,c_team) 미검증.
    assert len(calls) == 4


def test_hard_filter_probes_only_pressure_then_combo():
    # 압박군=[night_cap, off_budget]. 단일 다 실패, (a_night,b_off) 콤보만 풀림.
    # hard_filter: 압박군 단일 2개만 확인 → 그 콤보 → 성공. c_team(비압박) 단일 미검증.
    calls = []
    def _resolve(cfg):
        calls.append(1)
        return (bool(cfg.get("x_night") and cfg.get("x_off")), {})
    res = probe_relaxations(
        {}, _resolve, catalog=CAT, verify=False, try_combo=True,
        priority_families=["night_cap", "off_budget"], hard_filter=True,
    )
    assert res["combo"] is not None
    assert {m["id"] for m in res["combo"]["members"]} == {"a_night", "b_off"}
    # 압박군 단일 2(a_night,b_off) + 압박군 콤보 1 = 3. c_team 단일은 미검증.
    probed_single_ids = [p["id"] for p in res["all_probed"]]
    assert "c_team" not in probed_single_ids
    assert len(calls) == 3


def test_hard_filter_falls_back_to_full_when_pressure_wrong():
    # 압박군 지목이 틀림(night만) — 실제 정답은 c_team 단일. hard_filter 라도 폴백으로 찾음.
    def _resolve(cfg):
        return (bool(cfg.get("x_team")), {})  # c_team 단일이 정답
    res = probe_relaxations(
        {}, _resolve, catalog=CAT, verify=False, try_combo=True,
        priority_families=["night_cap"], hard_filter=True,
    )
    # 완결성: 압박군(단일+콤보) 실패해도 폴백으로 c_team 발견
    assert res["found"] is True
    assert any(r["id"] == "c_team" and r["feasible"] for r in res["all_probed"])


def test_priority_reorders_probe_sequence():
    # 아무것도 안 풀림 → 전체 검증되지만 순서는 우선군(off_budget) 먼저.
    res = probe_relaxations(
        {}, _resolve_only("nope"),
        catalog=CAT, verify=False, try_combo=False,
        priority_families=["off_budget", "team"],
    )
    order = [p["id"] for p in res["all_probed"]]
    assert order[0] == "b_off" and order[1] == "c_team"  # 우선군 먼저
    assert order[2] == "a_night"  # 나머지
