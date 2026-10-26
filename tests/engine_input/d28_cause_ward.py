"""D28 재확인 — '휴가 겹침 + 회복 OFF' 로 일자별 인원 부족이 되는 작은 병동(합성, 실명·PII 없음).

실병동 102598edcd0e 의 모양: 간호사 6명 · D/E/N/M 각 1명(하루 4명) · 나이트 30칸이 확정 원티드로 매일 1명 고정.
  - 나이트는 2일 블록(1·2일 N01, 3·4일 N02 … 순환)으로 고정 → 매일 '나이트 1명 + 직전 블록의 회복 OFF 1명'.
  - 8일·18일은 거기에 고정 휴가('연') 2명이 겹친다 → 가능 6−1−2=3 < 필요 4.
"""

from __future__ import annotations

from datetime import date, time
from functools import partial

from .harness import Scenario
from .scenario_defs import BASE_DDL, _request, _rows
from .scenarios import make_head_nurse
from .ward import (
    GROUP_ID,
    base_ward,
    config_row,
    seed_ward,
    shift_manage_row,
    shift_row,
    stamp,
)

NURSES = tuple(f"N{i:02d}" for i in range(1, 7))
#: 휴가가 겹치는 날 → 휴가자(그 날 나이트·회복 OFF 인 사람이 아닌 둘).
VACATIONS = {8: ("N01", "N02"), 18: ("N04", "N05")}


def night_nurse(day: int) -> str:
    """day 일의 고정 나이트 근무자 — 2일 블록 순환."""
    return NURSES[((day - 1) // 2) % 6]


def _fixed_rows() -> list[dict]:
    base = {"group_id": GROUP_ID, "year": 2026, "month": 11, **stamp()}
    rows = []
    for day in range(1, 31):
        rows.append({**base, "nurse_id": night_nurse(day), "shift_date": date(2026, 11, day),
                     "shift_id": "N", "shifts_table_id": 3, "is_applied": True,
                     "source_type": "original", "original_shift_id": "N", "created_by": "HN01"})
    for day, people in VACATIONS.items():
        for nid in people:
            rows.append({**base, "nurse_id": nid, "shift_date": date(2026, 11, day),
                         "shift_id": "연", "shifts_table_id": 8, "is_applied": True,
                         "source_type": "original", "original_shift_id": "연", "created_by": "HN01"})
    return rows


def ward():
    w = base_ward(6)
    w.shifts = [
        shift_row("D", 1, "데이", "D", "근무", "D", 1, time(7, 0), time(15, 0)),
        shift_row("E", 2, "이브닝", "E", "근무", "E", 2, time(15, 0), time(23, 0)),
        shift_row("N", 3, "나이트", "N", "근무", "N", 3, time(23, 0), time(7, 0)),
        shift_row("OFF", 4, "오프", None, "오프", "O", 4, show=False),
        shift_row("D2", 5, "미드", "M", "근무", "M", 5, time(11, 0), time(19, 0)),
        shift_row("연", 8, "연차", None, "휴가", None, 8),
    ]
    w.shift_manage = [shift_manage_row(1, "D", ["D"], 1), shift_manage_row(2, "E", ["E"], 1),
                      shift_manage_row(3, "N", ["N"], 1), shift_manage_row(5, "M", ["D2"], 1)]
    w.config = config_row(day_req=1, eve_req=1, nig_req=1, use_mid=True, apply_confirmed_wanted=True,
                          two_offs_after_two_nig=True, two_offs_after_three_nig=True,
                          ban_single_night=True)
    w.raw = {"n2n_min_gap": 0, "fixed_holiday_off_yn": 0}
    w.extras.append(_rows("FixedWantedEntry", _fixed_rows()))
    return w


def scenario(name: str = "D28-cause") -> Scenario:
    w = ward()
    return Scenario(name=name, seed=partial(seed_ward, w), make_request=_request(2026, 11),
                    make_user=make_head_nurse, ddl=tuple(BASE_DDL), stop_at="L2", patches=(allow_bridge_solve,),
                    description="D28 — 휴가 2 + 회복 OFF 1 이 8·18일에 겹침(6명 · 필요 4)")


#: 원인 브리지(λ 미니 CP-SAT)가 실제로 푼 횟수 — 시험이 읽는다.
BRIDGE_SOLVES: list[int] = []


def allow_bridge_solve(mp) -> None:
    """원인 브리지(lagrangian 안)의 CpSolver 풀이만 통과시킨다 — 그 밖의 풀이는 하네스대로 막는다.

    하네스 `Capture.install` 이 `CpSolver.solve` 를 막는 대역으로 갈아 끼우므로(바운드 메서드라 솔버 객체를 못 받는다),
    install 이 끝난 뒤 같은 자리를 '호출 스택에 lagrangian 이 있으면 원본, 아니면 막기' 함수로 다시 갈아 끼운다.
    """
    import inspect

    from ortools.sat.python import cp_model

    from .capture import Capture, SolveBlocked

    real_solve = cp_model.CpSolver.solve
    BRIDGE_SOLVES.clear()
    orig_install = Capture.install

    def install(self, mp2, engine):
        orig_install(self, mp2, engine)

        def guarded(solver, *args, **kwargs):
            if any("ontology_graph/lagrangian" in f.filename for f in inspect.stack()[1:8]):
                BRIDGE_SOLVES.append(1)
                return real_solve(solver, *args, **kwargs)
            self.solve_calls += 1
            raise SolveBlocked("하네스는 CpSolver.solve 를 돌리지 않는다(브리지 밖)")

        mp2.setattr(cp_model.CpSolver, "solve", guarded)

    mp.setattr(Capture, "install", install)
