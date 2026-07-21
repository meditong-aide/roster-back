"""probe magnitude-search — 고정 델타 대신 최소침습 feasible 값 이분탐색.

기존 RELAX_CATALOG 는 고정 델타(+8 등)라, 진짜 필요한 값이 그보다 크면(부족)
재solve 해도 infeasible → 해결책을 놓쳤다. magnitude-search 는 단조 노브를
이분탐색해 최소로 풀리는 값을 찾는다. 합성 resolve_fn(단조 임계값)으로 로직만
격리 검증 — 실제 솔버 없이 이분탐색 정확성/유한성/방향/예산을 본다.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))

from services.cp_sat.undiagnosed_probe import (  # noqa: E402
    probe_relaxations,
    to_resolution_options,
    _search_boundary,
)

_NULL = lambda *_a, **_k: None  # noqa: E731


def _cat_up():
    return [{"id": "raise_max_night_cap", "family": "night_cap", "label_ko": "월 야간 상한 완화",
             "apply": lambda c: {"max_nig_per_month": int(c.get("max_nig_per_month") or 0) + 8},
             "search": {"key": "max_nig_per_month", "dir": "up", "hi": 31}}]


def test_search_up_finds_minimal_beyond_fixed_delta():
    # feasible iff max_nig >= 12. 고정 +8=11 이면 놓치는데, search 는 12 를 찾아야.
    calls = []

    def rf(cfg):
        v = int(cfg.get("max_nig_per_month", 0))
        calls.append(v)
        return (v >= 12, {})

    res = probe_relaxations({"max_nig_per_month": 3}, rf, catalog=_cat_up(),
                            try_combo=False, verify=False, logger=_NULL)
    assert res["found"]
    r = res["resolutions"][0]
    assert r["delta"] == {"max_nig_per_month": 12}      # 최소 feasible
    assert r["info"]["searched"] is True
    assert len(calls) <= 8                              # 이분 → 유한(로그 범위)


def test_search_down_finds_least_disruptive():
    # feasible iff off_days <= 5 (OFF 적을수록 여유). 최소 침습 = 최대 feasible = 5.
    def rf(cfg):
        return (int(cfg.get("off_days", 99)) <= 5, {})

    cat = [{"id": "lower_off_days", "family": "off_budget", "label_ko": "월 OFF 완화",
            "apply": lambda c: {"off_days": max(0, int(c.get("off_days") or 0) - 3)},
            "search": {"key": "off_days", "dir": "down", "lo": 0}}]
    res = probe_relaxations({"off_days": 10}, rf, catalog=cat,
                            try_combo=False, verify=False, logger=_NULL)
    assert res["resolutions"][0]["delta"] == {"off_days": 5}


def test_search_unfixable_no_resolution():
    # 상한(31)서도 infeasible → 이 노브 단독 불가 → resolution 없음.
    def rf(cfg):
        return (False, {})

    res = probe_relaxations({"max_nig_per_month": 3}, rf, catalog=_cat_up(),
                            try_combo=False, verify=False, logger=_NULL)
    assert not res["found"]
    assert res["resolutions"] == []


def test_search_budget_caps_solves():
    # budget=3 이면 재solve 3회 초과 금지(수렴 못해도 유효 경계 반환).
    calls = []

    def rf(cfg):
        v = int(cfg.get("max_nig_per_month", 0))
        calls.append(v)
        return (v >= 12, {})

    res = probe_relaxations({"max_nig_per_month": 3}, rf, catalog=_cat_up(),
                            try_combo=False, verify=False, search_budget=3, logger=_NULL)
    assert len(calls) <= 3
    # 예산 소진해도 반환값은 feasible(유효)해야
    if res["resolutions"]:
        assert res["resolutions"][0]["delta"]["max_nig_per_month"] >= 12


def test_searched_value_surfaces_verified_in_option():
    def rf(cfg):
        return (int(cfg.get("max_nig_per_month", 0)) >= 12, {})

    res = probe_relaxations({"max_nig_per_month": 3}, rf, catalog=_cat_up(),
                            try_combo=False, verify=False, logger=_NULL)
    opts = to_resolution_options(res, {"max_nig_per_month": 3})
    o = opts[0]
    assert o["verified"] is True                        # probe 재solve 검증됨
    assert o["apply"] == {"max_nig_per_month": 12}
    assert o["changes"][0]["from"] == 3 and o["changes"][0]["to"] == 12


def test_search_boundary_up_direct():
    # 헬퍼 직접: threshold 12, 예산 넉넉 → 12, solves 유한.
    def rf(cfg):
        return (int(cfg.get("max_nig_per_month", 0)) >= 12, {})

    val, solves = _search_boundary({"max_nig_per_month": 3},
                                   {"key": "max_nig_per_month", "dir": "up", "hi": 31},
                                   rf, budget=8, logger=_NULL)
    assert val == 12 and solves <= 8
