"""일자별 수요(by_day) — 뷰가 만든 사설 키만 읽는다 (EI-14b2a3).

그래프 엔진(IR·frontier DP·factor graph·joint coverage·explain 산술)은 지금까지 평탄 수요(`daily_shift_requirements`)만
읽었다. 일자별 수요(`daily_shift_requirements_by_day`)를 엔진이 **직접** 읽게 하면 `_probe_base` 를 받는 사후 소비자의 판정까지
바뀌어 2단계(13a 뒤) 일이 1단계로 샌다. 그래서 `engine_input.views.to_roster_ir` 가 `IR_BY_DAY_KEY` 로 정규화한 사본을 써 줄 때만
일자별로 계산하고, 키가 없으면 모든 함수가 평탄 수요(= 지금과 같은 값)를 돌려준다. raw 키는 계속 무시한다.

`IR_BY_DAY_KEY` 는 엔진 config 가 아니라 뷰가 만든 IR 입력 안에서만 산다(요청으로는 받지 않는다).
원소는 `{"D": int, "E": int, "N": int}`(M 은 그래프가 모르므로 뷰가 버린다).
"""
from __future__ import annotations

from typing import Any, Mapping

IR_BY_DAY_KEY = "_ir_by_day"
_SHIFTS = ("D", "E", "N")


def flat_demand(config: Mapping[str, Any]) -> dict[str, int]:
    """평탄 수요 — `frontier_dp._req`·`hypergraph_conditioning._req` 와 같은 규칙(dsr 가 비어 있지 않으면 D/E/N, 아니면 N 만 nig_req)."""
    dsr = config.get("daily_shift_requirements") or {}
    if isinstance(dsr, dict) and dsr:
        return {s: int(dsr.get(s, 0) or 0) for s in _SHIFTS}
    return {"D": 0, "E": 0, "N": int(config.get("nig_req", 0) or 0)}


def has_by_day(config: Mapping[str, Any]) -> bool:
    rows = config.get(IR_BY_DAY_KEY)
    return isinstance(rows, list) and len(rows) > 0


def demand_on_day(config: Mapping[str, Any], day: int) -> dict[str, int]:
    """day(0-based) 의 수요 — 사설 키가 있고 그 날 행이 있으면 그 행, 아니면 평탄."""
    if has_by_day(config):
        rows = config[IR_BY_DAY_KEY]
        if 0 <= day < len(rows) and isinstance(rows[day], Mapping):
            return {s: int(rows[day].get(s, 0) or 0) for s in _SHIFTS}
    return flat_demand(config)


def demand_by_day(config: Mapping[str, Any], num_days: int) -> list[dict[str, int]]:
    """일자별 수요 목록(길이 num_days) — 사설 키가 없으면 평탄 × num_days."""
    return [demand_on_day(config, d) for d in range(num_days)]
