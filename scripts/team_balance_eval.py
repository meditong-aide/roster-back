"""
팀 균등/동일 교대(옵션 A) 기능을 간단히 평가하는 스크립트입니다.

목적:
- gauge(0~10) 변화에 따라 팀 동일화 정도가 실제로 얼마나 변하는지 빠르게 확인합니다.
- 팀 대표 교대 분포가 요구량 비율에 맞춰지는지(대략) 확인합니다.

주의:
- 이 스크립트는 DB 없이 최소 데이터로 CP-SAT을 실행합니다.
- 결과는 최적 해가 아닐 수 있으며, time_limit_seconds에 영향받습니다.
"""

from __future__ import annotations

import os
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from typing import Dict, List, Tuple

import numpy as np

# 스크립트를 직접 실행할 때도 앱의 "절대 임포트(db.*, services.*)"가 동작하도록 경로를 추가한다.
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP_ROOT = os.path.join(REPO_ROOT, "app")
for p in (REPO_ROOT, APP_ROOT):
    if p not in sys.path:
        sys.path.insert(0, p)

from services.cp_sat_basic import generate_roster_cp_sat


@dataclass(frozen=True)
class EvalResult:
    """팀 균등 평가 결과를 담는 DTO."""

    gauge: int
    team_same_shift_rate: float
    team_shift_counts: Dict[str, Dict[str, int]]


def _build_minimal_inputs(
    year: int,
    month: int,
    num_days: int,
    team_map: Dict[str, str],
    daily_req: Dict[str, int],
    gauge: int,
) -> Tuple[List[dict], List[dict], dict, List[dict]]:
    """DB 없이 cp_sat_basic 엔진을 호출할 최소 입력을 구성한다."""
    nurses = []
    for i, (nurse_id, team_id) in enumerate(team_map.items()):
        nurses.append(
            {
                "nurse_id": nurse_id,
                "name": f"N{i}",
                "experience": 5,
                "is_head_nurse": False,
                "is_night_nurse": 0,
                "personal_off_adjustment": 0,
                "joining_date": None,
                "resignation_date": None,
                "team_id": team_id,
                "sequence": i,
            }
        )

    # 선호도는 비워서(중립) 팀 항의 영향을 보기 쉽게 한다.
    prefs: List[dict] = []

    config = {
        "daily_shift_requirements": daily_req,
        # 요구 우선순위는 너무 높지 않게(팀 soft가 먹을 수 있게)
        "shift_priority": 0.7,
        # 팀 옵션 on
        "team_balance_enable": True,
        "team_balance_gauge": gauge,
        "team_balance_mode": "balanced",
        # 테스트 기간 단축을 위해 day-by-day 요구치로 강제
        "daily_shift_requirements_by_day": [daily_req for _ in range(num_days)],
        # 폴백 관련 기본값
        "off_days": 8,
        "preceptor_gauge": 0,
    }

    # grouped(ShiftManage) 구조는 코드->main_code 매핑 용도로 쓰이므로 최소 형태만 제공
    grouped = [
        {"main_code": "D", "codes": ["D"]},
        {"main_code": "E", "codes": ["E"]},
        {"main_code": "N", "codes": ["N"]},
        {"main_code": "O", "codes": ["O"]},
    ]
    return nurses, prefs, config, grouped


def _count_team_shifts(roster: Dict[str, List[str]], team_map: Dict[str, str]) -> Dict[str, Dict[str, int]]:
    """팀별 D/E/N 배정량을 집계한다."""
    out: Dict[str, Dict[str, int]] = defaultdict(lambda: {"D": 0, "E": 0, "N": 0, "O": 0})
    for nurse_id, days in roster.items():
        t = team_map.get(nurse_id, "NONE")
        for s in days:
            if s in out[t]:
                out[t][s] += 1
    return dict(out)


def _team_same_shift_rate(roster: Dict[str, List[str]], team_map: Dict[str, str]) -> float:
    """같은 팀 내에서 같은 날 동일 교대(D/E/N)가 얼마나 나오는지 비율로 계산한다."""
    team_members: Dict[str, List[str]] = defaultdict(list)
    for nid, tid in team_map.items():
        if tid:
            team_members[tid].append(nid)

    if not team_members:
        return 0.0

    # day별로 팀 내 pair가 동일(D/E/N)인 비율
    num = 0
    den = 0
    num_days = len(next(iter(roster.values())))
    for tid, members in team_members.items():
        if len(members) < 2:
            continue
        for d in range(num_days):
            shifts = [roster[m][d] for m in members]
            # OFF 제외하고 비교
            work = [s for s in shifts if s in ("D", "E", "N")]
            for i in range(len(work)):
                for j in range(i + 1, len(work)):
                    den += 1
                    if work[i] == work[j]:
                        num += 1
    return (num / den) if den > 0 else 0.0


def run_eval() -> None:
    """게이지별로 팀 동일화/분포를 출력한다."""
    year, month = 2027, 3
    num_days = 7
    daily_req = {"D": 2, "E": 2, "N": 1}

    # 6명, 2팀(각 3명)
    team_map = {
        "N1": "T1",
        "N2": "T1",
        "N3": "T1",
        "N4": "T2",
        "N5": "T2",
        "N6": "T2",
    }

    for g in (0, 3, 5, 7, 10):
        nurses, prefs, config, grouped = _build_minimal_inputs(
            year=year,
            month=month,
            num_days=num_days,
            team_map=team_map,
            daily_req=daily_req,
            gauge=g,
        )
        # gauge 0은 enable False로 바뀌어야 하므로 여기서 직접 반영
        if g == 0:
            config["team_balance_enable"] = False
        # 가중치 예상치(참고용): cap=240, p=1.7
        cap = 240
        power = 1.7
        w_est = int(round(cap * ((g / 10.0) ** power))) if g > 0 else 0
        print(f"\n--- gauge={g}, expected_weight≈{w_est} ---")

        out = generate_roster_cp_sat(
            nurses,
            prefs,
            config,
            year,
            month,
            grouped,
            time_limit_seconds=8,
            randomize=False,
            seed=123,
        )
        roster = out["roster"] if isinstance(out, dict) and "roster" in out else out
        counts = _count_team_shifts(roster, team_map)
        same_rate = _team_same_shift_rate(roster, team_map)
        print(f"\n[gauge={g}] team_same_shift_rate={same_rate:.3f}")
        for tid, c in counts.items():
            print(f"  - team {tid}: D={c['D']}, E={c['E']}, N={c['N']}, O={c['O']}")


if __name__ == "__main__":
    run_eval()


