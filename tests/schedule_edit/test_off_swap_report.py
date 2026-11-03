"""자동 연차전환 후처리(`off_swap.postprocess_off_swap`)의 report — 근무표 수정 도우미가 '왜 안 바뀌었는지'를 답할 재료.

DB 를 읽는 도우미만 가짜로 바꾸고 판정 본체는 그대로 돈다. report 를 넘기지 않는 생성 경로와 바뀌는 칸이 같아야 한다.
"""

from __future__ import annotations

import copy
from types import SimpleNamespace

import pytest

from services.cp_sat import off_swap

SCHEDULE = SimpleNamespace(schedule_id="s1", group_id="g1", year=2026, month=11)
REQ = SimpleNamespace(year=2026, month=11)


def _config(**kw):
    base = dict(off_swap_enabled=True, off_days=4, use_mid=False, two_offs_per_week=False)
    return SimpleNamespace(**{**base, **kw})


@pytest.fixture
def ward(monkeypatch):
    nurses = {"A": SimpleNamespace(allowed_shifts=None), "B": SimpleNamespace(allowed_shifts=None),
              "NN": SimpleNamespace(allowed_shifts="N")}
    state = {"target": SimpleNamespace(shift_id="연", id=1, type="휴가"), "fixed": set()}
    monkeypatch.setattr(off_swap, "_resolve_target_shift", lambda db, g: state["target"])
    monkeypatch.setattr(off_swap, "_resolve_shift_code_sets", lambda db, g: ({"N"}, {"O"}, {"주"}))
    monkeypatch.setattr(off_swap, "_load_fixed_wanted_set", lambda db, g, y, m: state["fixed"])
    monkeypatch.setattr(off_swap, "_load_nurses", lambda db, g: dict(nurses))
    monkeypatch.setattr(off_swap, "_load_prev_month_tail", lambda *a, **kw: ["", "", ""])
    monkeypatch.setattr(off_swap, "is_n_only_profile", lambda allowed, use_mid=False: allowed == "N")
    return state


def _run(generated, config, report=None):
    out = copy.deepcopy(generated)
    off_swap.postprocess_off_swap(None, SCHEDULE, out, config, REQ, report=report)
    return out


# 14일 · A 는 OFF 6(기준 4 보다 2 많음) · B 는 기준 그대로 · NN 은 N 전담
GENERATED = {"A": list("DODODOODDDDODO"), "B": list("DODODDDDODDDDO"), "NN": list("NNOONNOONNOONN")}


def test_report_matches_what_was_converted(ward):
    report = {}
    out = _run(GENERATED, _config(), report)
    assert [d for d, (a, b) in enumerate(zip(GENERATED["A"], out["A"])) if a != b] == [11, 13]   # 월말부터
    assert out["B"] == GENERATED["B"] and out["NN"] == GENERATED["NN"]
    assert report == {"skip": None, "target_shift": "연", "baseline": 4, "two_offs_per_week": False,
                      "converted": 2, "skipped_n_only": 1, "kept_off_for_week_floor": 0, "excess_total": 2,
                      "protected": 0}
    assert _run(GENERATED, _config()) == out                          # report 없이 부르는 생성 경로와 같다


def test_report_says_why_nothing_changed(ward):
    for config, skip in ((_config(off_swap_enabled=False), "disabled"), (_config(off_days=0), "no_baseline")):
        report = {}
        assert _run(GENERATED, config, report) == GENERATED
        assert report["skip"] == skip and report["converted"] == 0
    ward["target"] = None
    report = {}
    _run(GENERATED, _config(), report)
    assert report["skip"] == "no_target"


def test_week_floor_and_protected_cells_are_counted(ward):
    # 주 2회 휴일 하한(기준 3) — A 는 3개 넘지만 둘째 주 O 가 2개뿐이라 첫 주에서 2개만 · B 는 1개 넘지만 두 주 다 O 2개
    report = {}
    out = _run(GENERATED, _config(two_offs_per_week=True, off_days=3), report)
    assert [d for d, (a, b) in enumerate(zip(GENERATED["A"], out["A"])) if a != b] == [5, 6]
    assert (report["excess_total"], report["converted"], report["kept_off_for_week_floor"]) == (4, 2, 2)
    # 확정 원티드로 굳은 O 는 후보에서 빠진다 — 모자란 몫은 protected
    ward["fixed"] = {("A", d + 1) for d, c in enumerate(GENERATED["A"]) if c == "O"}
    report = {}
    assert _run(GENERATED, _config(), report) == GENERATED
    assert report["protected"] == 2 and report["converted"] == 0
