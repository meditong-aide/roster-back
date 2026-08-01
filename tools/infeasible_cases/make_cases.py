"""다양한 infeasible 케이스 JSON 생성 — 오프라인 진단·연구용 코퍼스.

라이브 캡처(case_export.dump_case, AIDE_DUMP_CASE)와 **동일 스키마**로, 각 계열을
합성해 cases/ 에 저장한다. 파라미터를 바꿔 변형 실험 가능.

실행:  .venv/bin/python tools/infeasible_cases/make_cases.py
재현:  .venv/bin/python tools/infeasible_cases/replay.py
"""

from __future__ import annotations

import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "..", "app"))

from services.ontology_graph.case_export import build_case  # noqa: E402

OUT = os.path.join(HERE, "cases")

# 실병동 기본 규칙(부산 CCR 계열): 3N2OFF + not_one_night.
_BASE_CONFIG = {
    "daily_shift_requirements": {"D": 2, "E": 1, "N": 1},
    "off_days": 9, "max_nig_per_month": 15,
    "two_offs_after_three_nig": True, "two_offs_after_two_nig": False,
    "not_one_night": True, "use_mid": False,
}


def _cfg(initial_forbidden=None, initial_forced_off=None, **over):
    c = dict(_BASE_CONFIG)
    c.update(over)
    c["initial_constraints"] = {
        "forbidden": initial_forbidden or {}, "forced_off": initial_forced_off or {}}
    return c


def _nurse(nid, name, allowed=None, **attr):
    n = {"nurse_id": nid, "name": name, "grade": 1, "team_id": "A"}
    if allowed is not None:
        n["allowed_shifts"] = allowed
    n.update(attr)
    return n


def _pool(k):
    """일반 간호사 k명(제약 없음)."""
    return [_nurse(f"g{i}", f"일반{i}") for i in range(k)]


CASES: list[tuple[str, dict, dict]] = []


def add(name, nurses, config):
    CASES.append((name, nurses, config))


# ① banned 4연속(NNNN) — N전담 10~13 O금지 → 4연속 N > 최대3(3N2OFF)
add("banned_4consecutive",
    [_nurse("n1", "장세현", allowed=["N"])] + _pool(5),
    _cfg(initial_forbidden={"n1": {10: ["O"], 11: ["O"], 12: ["O"], 13: ["O"]}}))

# ② banned 회복막힘 — NNN(10~12) 후 하루 건너 14 O금지 → 회복 2OFF(13·14) 막힘
add("banned_recovery_blocked",
    [_nurse("n1", "장세현", allowed=["N"])] + _pool(5),
    _cfg(initial_forbidden={"n1": {10: ["O"], 11: ["O"], 12: ["O"], 14: ["O"]}}))

# ③ banned 고립1N — 11 O금지(강제N) + 10·12 강제OFF → 고립 1N → not_one_night 위반
add("banned_isolated_single_night",
    [_nurse("n1", "장세현", allowed=["N"])] + _pool(5),
    _cfg(initial_forbidden={"n1": {11: ["O"]}}, initial_forced_off={"n1": [10, 12]}))

# ④ 개인 월야간 초과 — n_min 13 > 월상한 7 (산술 총량)
add("personal_night_over_cap",
    [_nurse("n1", "김수선", n_min=13)] + _pool(5),
    _cfg(max_nig_per_month=7))

# ⑤ 주말휴무 병목 — 4명 중 3명 주말휴무 → 주말 커버리지 못 채움
add("weekend_off_bottleneck",
    [_nurse("n1", "A", is_weekend_off=True), _nurse("n2", "B", is_weekend_off=True),
     _nurse("n3", "C", is_weekend_off=True), _nurse("n4", "D")],
    _cfg(off_days=2))

# ⑥ 커버리지 부족 — 하루 수요 D2E2N1=5 > 4명
add("coverage_shortage",
    _pool(4),
    _cfg(daily_shift_requirements={"D": 2, "E": 2, "N": 1}, off_days=1))

# ⑦ feasible 대조군 — 인원 충분·제약 없음(진단=None 기대)
add("feasible_control",
    _pool(8),
    _cfg())


def main():
    os.makedirs(OUT, exist_ok=True)
    for name, nurses, config in CASES:
        case = build_case(year=2026, month=8, group_id="SYNTH", num_days=31,
                          nurses=nurses, config=config)
        case["meta"] = {"group_id": "SYNTH", "source": "synthetic", "name": name}
        path = os.path.join(OUT, f"synth-{name}.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(case, f, ensure_ascii=False, indent=2)
        print(f"  ✎ {os.path.basename(path)}  (nurses={len(case['nurses'])})")
    print(f"\n{len(CASES)}개 합성 케이스 → {OUT}")


if __name__ == "__main__":
    main()
