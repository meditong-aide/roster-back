"""Separator 분해 — 대칭 축소 + 슬라이딩 window 로 대형 결합을 exact 유지.

두 축소가 (a) plain frontier 가 폭발(UNKNOWN)하는 대형을 결론까지 끌고 가고,
(b) window infeasible ⟹ 전체 infeasible 을 sound 하게 인증하며,
(c) feasible 을 infeasible 로 오인하지 않음(no false positive)을 잠근다.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools", "infeasible_cases"))

from exact_oracle import is_feasible  # noqa: E402
from services.ontology_graph.component_decompose import (  # noqa: E402
    decompose_diagnose,
    windowed_certify,
)
from services.ontology_graph.frontier_dp import diagnose_frontier  # noqa: E402


def _pool(k):
    return [{"nurse_id": f"n{i}", "name": f"N{i}", "grade": 1, "team_id": "A"}
            for i in range(k)]


def _cfg(dsr, rules, fb=None, fo=None):
    c = dict(rules, daily_shift_requirements=dsr)
    c["initial_constraints"] = {"forbidden": fb or {}, "forced_off": fo or {}}
    return c


_R = {"two_offs_after_two_nig": True, "not_one_night": True}


def test_symmetry_matches_plain_and_oracle_small():
    """소형에서 대칭 축소 결론 = plain = 독립 oracle (축소가 sound)."""
    nu, cfg = _pool(6), _cfg({"D": 1, "E": 1, "N": 2}, _R)
    plain = diagnose_frontier(nu, cfg, 7, symmetry=False).status
    symm = diagnose_frontier(nu, cfg, 7, symmetry=True).status
    orc = is_feasible(nu, cfg, 7)
    assert plain == symm
    if orc is False:
        assert symm == "INFEASIBLE_CERTIFIED"
    elif orc is True:
        assert symm in ("FEASIBLE_WITNESS", "UNKNOWN")


def test_symmetry_resolves_a_plain_unknown():
    """plain 이 폭 제한으로 UNKNOWN 인 대형을 대칭 축소가 결론까지 끌고 간다."""
    nu, cfg = _pool(8), _cfg({"D": 1, "E": 1, "N": 4}, _R)
    plain = diagnose_frontier(nu, cfg, 8, cap=40, symmetry=False).status
    symm = diagnose_frontier(nu, cfg, 8, cap=40, symmetry=True).status
    assert plain == "UNKNOWN"
    assert symm == "INFEASIBLE_CERTIFIED"                 # 폭 6 → cap 40 안에서 판정


def test_windowed_certify_is_sound_no_false_positive():
    """명백히 feasible 한 인스턴스에 window 분해가 INFEASIBLE 을 주장하지 않는다."""
    nu, cfg = _pool(6), _cfg({"D": 1, "E": 1, "N": 1}, _R)   # 6명>3슬롯, 여유
    assert is_feasible(nu, cfg, 12) is True
    assert windowed_certify(nu, cfg, 12, window=6, stride=3).status != "INFEASIBLE_CERTIFIED"


def test_decompose_certifies_large_with_local_bottleneck():
    """전역 비교환·장기 horizon: plain UNKNOWN, 분해가 병목 window 를 국소 인증."""
    k, days = 9, 18
    nu = _pool(k)
    fo = {f"n{i}": [i % 8] for i in range(k)}             # 서로 다른 초반 강제OFF → 전역 비교환
    fb = {}
    for d in range(12, 18):                               # 12~17 전원 강제근무(회복 붕괴)
        for i in range(k):
            fb.setdefault(f"n{i}", {})[d] = ["O"]
    cfg = _cfg({"D": 2, "E": 2, "N": 2}, _R, fb=fb, fo=fo)
    plain = diagnose_frontier(nu, cfg, days, cap=3000, symmetry=False).status
    dd = decompose_diagnose(nu, cfg, days)
    assert plain == "UNKNOWN"
    assert dd.status == "INFEASIBLE_CERTIFIED"
    assert dd.window is not None and dd.window[0] <= dd.certificate.witness["day"] < dd.window[1]
