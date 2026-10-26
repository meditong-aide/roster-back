"""L0(precheck 입력) 하드 요소 ⊆ L1(엔진 입력) 계약 + 월중 경계 칸 계약 — EI-11 11q(점검 `ei11/q11/AUDIT.md` ② · §3.4 · 리뷰 반영).

왜 있는가
  11r 리뷰가 찾은 결함(운영 main 에도 있던 것): 월중 경계(파견·병동이동) 하드락 칸(합류일 강제 OFF·전이 금지칸·OFF 창)이
  precheck 입력에는 있는데 엔진 입력에는 없었다(`_run_cp_sat_basic` 이 다시 지어 덮었다). 같은 종류의 틈이 다시 생기면 잡는다.

규칙(맞대는 칸) — L0 의 하드 요소가 L1 에 그대로 있어야 한다
  R0 설정 키 — L0 의 키는 모두 L1 에 있다(L1 에만 있는 키는 E2 표의 칸뿐)
  R1 하드락 설정 키(레지스트리 HardLock 정본·별칭 + 경계 스위치) — 값 같음(없음도 같음)
  R2 강제 OFF (간호사, 날) · R3 금지칸 (간호사, 날, 코드) · R4 OFF 창 (간호사, 창) · R5 고정 칸 (nurse_id, 날, 메인코드) — L0 ⊆ L1
  R6 통과 칸 — 같음 · R7 간호사 명단과 하드 관련 칸(등급·프리셉터 포함) — 같음 · R8 등급 설정 — 같음
의도된 제외는 `EXCLUSIONS` 표 한 곳에 근거·칸과 함께 두고, 판정 함수가 그 표의 칸을 읽는다(새 제외는 이 표에만 더한다 —
  `test_exclusion_*` 가 표의 제외마다 '허용하는 변화는 통과 · 허용 밖 변화는 위반'을 묶는다).
사다리·탐색 시도(리뷰 HIGH): 첫 시도 뒤 재시도·탐색도 엔진을 다시 부른다 — 시도마다 R2~R4(경계 칸)를 맞댄다(골든 사다리 기록 ·
  경계가 있는 사다리 시나리오 `boundary_cases.ladder_boundary_scenarios`).

월중 경계 칸 계약(점검 §3.4 보완)
  L0 ⊆ L1 은 생성 경로가 아예 안 만드는 경계 칸(G1 N 꼬리 이어짐 · G3 출발 경계 · G7 월초 근처 경계)을 못 본다. 그래서
  '경계만의 위반 이어짐'(`boundary_cases.boundary_only` — 바깥 근무와 이어 붙일 때만 생기는 하드락 위반, 독립 판정기)을 모두
  만들고 L1 의 경계 칸(강제 OFF·금지칸·OFF 창·개인 월 N 상한)이 하나도 남김없이 막는지 본다. 엔진이 그 칸을 모델에서 버리는지(G2)는
  `test_boundary_hard_lock_engine.py`.
"""

from __future__ import annotations

import dataclasses
import json

import pytest
from fastapi import HTTPException

from .boundary_cases import (
    CFG,
    DEPARTURES,
    EARLY,
    MONTH_START_IN,
    NDAYS,
    NIGHT_CAP_G2,
    OCT_DAYS,
    PREV_OCT,
    BoundaryCase,
    BoundaryGap,
    attempt_configs,
    boundary_only,
    d,
    early_in,
    early_ret,
    excluded,
    first_seen,
    hard_violations,
    l1_cells,
    ladder_boundary_scenarios,
    minimal,
    n07_out,
    night_cap_case,
    run_to_l1,
    tail_cells,
    transfer_out,
    x01_in,
)
from .golden import GOLDEN_DIR
from .golden_gate import GOLDEN, require_golden
from .harness import run_with_output
from .ladder import drain_live_graph_dirs


@dataclasses.dataclass(frozen=True)
class Exclusion:
    """의도된 제외 하나 — 근거와 그 제외가 다루는 칸(판정 함수가 이 칸을 읽는다)."""

    reason: str
    fields: tuple[str, ...]


DEMAND = ("daily_shift_requirements_by_day", "daily_shift_requirements_max_by_day")
OFF_LIMITS = ("o_min", "o_max", "o_exact")

#: 의도된 제외(근거) — 점검 §3.2 표. 모두 'precheck 가 더 엄격하거나 precheck 가 아직 안 보는 쪽'이다.
EXCLUSIONS: dict[str, Exclusion] = {
    "E1": Exclusion("배정 차단일 위 고정 칸 — precheck 번역에서 뺀다(엔진은 활동일 밖 고정 칸을 건너뛴다 · 남기면 오차단). "
                    "그래서 L1 에만 있는 고정 칸은 그 간호사의 차단일 위여야 한다", ("fixed_cells",)),
    "E2": Exclusion("L1 전용 칸 — 월경계 금지칸·강제 OFF·OFF 창·prev_month_*·shift_definitions·team_handoff_policy_by_team·"
                    "fixed_cells(precheck 는 전월 꼬리를 아직 안 본다 — D8 · EI-13a / 엔진 조립용 칸)",
                    ("initial_constraints", "off_window_constraints", "prev_month_last_main", "prev_month_n_tail",
                     "prev_month_n_offs_after", "prev_month_last_n_k", "prev_month_off_tail", "shift_definitions",
                     "team_handoff_policy_by_team", "fixed_cells")),
    "E3": Exclusion("수요 — 고정근무 차감이 precheck 뒤라 L1 ≤ L0(precheck 가 더 엄격). L1 은 차감 뒤 수요(나이트 사전 검사 L0p 가 "
                    "받은 값)와 같아야 한다 — 차감 밖으로 수요가 줄면 위반", DEMAND),
    "E4": Exclusion("근무 우선(off_first)의 개인 OFF 한도 — 엔진 입력에서 None(2026-10-12 사용자 결정)", OFF_LIMITS),
    "E5": Exclusion("등급 설정의 allow_soft_fallback 한 칸 — 처방 soft:grade_max 가 엔진 쪽만 켠다(L0 False → L1 True 만). "
                    "등급 설정의 다른 칸과 간호사 등급은 R7·R8 로 같아야 한다", ("allow_soft_fallback",)),
}
PASS_THROUGH = ("blocked_by_nurse_id", "weekly_off_map", "coverage_exclude_nurse_days", "preceptee_period_by_nurse_id",
                "mutual_exclusion_by_nurse_id", "other_group_offs")
NURSE_HARD = ("allowed_shifts", "fixed_shift", "d_min", "d_max", "d_exact", "e_min", "e_max", "e_exact", "n_min", "n_max",
              "n_exact", "is_weekend_off", "joining_date", "resignation_date", "team_id", "grade", "preceptor_id")
BOUNDARY_SWITCHES = ("cross_month_hard_rules_enable", "cross_month_lookback_days", "ban_n_to_d", "ban_e_to_d", "ban_n_to_e")
#: 골든 16개(D29 — 골든은 로컬 전용이라 이름은 코드에 둔다. 파일 수가 달라지면 `test_golden_names_are_the_golden_files` 가 잡는다).
GOLDEN_NAMES = ("S1", "S1-D", "S1-L", "S1-L3", "S1-LB", "S1-LT", "S1-P", "S2", "S3", "S4", "S5", "S6", "S7", "S8", "S9", "S9-F")
#: L1 이 없는 골든 — 나이트 사전 차단(엔진 입력이 없어 맞댈 것이 없다).
PRE_ENGINE_BLOCK = {"S9": "night_block", "S9-F": "night_block"}


def _hard_keys() -> tuple[str, ...]:
    from services.engine_contract.settings import SETTINGS, HardLock

    keys = [k for spec in SETTINGS if isinstance(spec.hard_lock, HardLock) for k in (spec.key, *spec.aliases)]
    return tuple(dict.fromkeys(keys + list(BOUNDARY_SWITCHES)))


def _ic(cfg: dict) -> tuple[set, set]:
    ic = cfg.get("initial_constraints") if isinstance(cfg.get("initial_constraints"), dict) else {}
    fo = {(str(n), int(x)) for n, days in (ic.get("forced_off") or {}).items() for x in days or ()}
    fb = {(str(n), int(day), str(c)) for n, dm in (ic.get("forbidden") or {}).items()
          for day, codes in (dm or {}).items() for c in codes or ()}
    return fo, fb


def _ow(cfg: dict) -> set:
    ow = cfg.get("off_window_constraints") if isinstance(cfg.get("off_window_constraints"), dict) else {}
    return {(str(n), tuple(int(x) for x in w)) for n, wins in ow.items() for w in wins or () if isinstance(w, (list, tuple))}


def _fixed_l1(l1: dict) -> set:
    ids = [str(n.get("nurse_id")) for n in l1["nurses_data"]]
    c2m = {str(c).upper(): r.get("main_code") for r in l1.get("shift_manage_data") or [] for c in r.get("codes") or []}
    out = set()
    for c in l1["config_data"].get("fixed_cells") or []:
        raw = str(c.get("shift") or "").strip().upper()
        off = raw in ("O", "OFF", "주") or str(c.get("shift_type") or "").strip() in ("휴가", "공가", "휴무")
        out.add((ids[c["nurse_index"]], int(c["day_index"]), "O" if off else c2m.get(raw, raw)))
    return out


def _key_violations(c0: dict, c1: dict) -> list[str]:
    """R0 — L0 키는 모두 L1 에 있고, L1 에만 있는 키는 E2 칸뿐이다."""
    allowed = set(EXCLUSIONS["E2"].fields)
    return ([f"R0 L1 에 없는 키 {k}" for k in sorted(set(c0) - set(c1))]
            + [f"R0/E2 L1 에만 있는 키 {k}" for k in sorted(set(c1) - set(c0) - allowed)])


def _fixed_violations(l0: dict, l1: dict) -> list[str]:
    """R5 — L0 고정 칸 ⊆ L1. E1 — L1 에만 있는 고정 칸은 그 간호사의 차단일 위다."""
    f0 = {(str(c["nurse_id"]), int(c["day_index"]), str(c["shift"])) for c in l0.get("fixed_cells") or []}
    f1 = _fixed_l1(l1)
    blocked = {str(n): {int(x) for x in days or ()}
               for n, days in (l1["config_data"].get("blocked_by_nurse_id") or {}).items()}
    return ([f"R5 fixed {x}" for x in sorted(f0 - f1)]
            + [f"E1 차단일 밖 L1 전용 고정 칸 {x}" for x in sorted(f1 - f0) if x[1] not in blocked.get(x[0], set())])


def _demand_violations(c0: dict, c1: dict, l0p: dict | None) -> list[str]:
    """E3 — 수요는 L1 ≤ L0 이고, L1 은 고정근무 차감 뒤 수요(L0p)와 같다(L0p 가 없으면 차감이 없었으니 L0 와 같다)."""
    post = (l0p or {}).get("config") or c0
    bad = []
    for key in EXCLUSIONS["E3"].fields:
        v0, v1 = c0.get(key), c1.get(key)
        if post.get(key) != v1:
            bad.append(f"E3 {key}: 차감 뒤 수요와 다르다")
        if not (isinstance(v0, list) and isinstance(v1, list) and len(v0) == len(v1)):
            continue
        bad += [f"E3 {key}[{i}].{k}: {d0.get(k)} → {v}" for i, (d0, d1) in enumerate(zip(v0, v1, strict=True))
                for k, v in (d1 or {}).items() if k in (d0 or {}) and v > d0[k]]
    return bad


def _nurse_violations(l0: dict, l1: dict) -> list[str]:
    """R7 — 명단·하드 관련 칸 같음. E4 — 근무 우선이면 L1 의 개인 OFF 한도는 None(아니면 같음)."""
    n0 = {str(n["nurse_id"]): n for n in l0["nurses_dict"]}
    n1 = {str(n["nurse_id"]): n for n in l1["nurses_data"]}
    if set(n0) != set(n1):
        return [f"R7 명단 {sorted(set(n0) ^ set(n1))}"]
    off_first = bool(l1["config_data"].get("off_first"))
    bad = [f"R7 {nid}.{f}: {n0[nid].get(f)!r} → {n1[nid].get(f)!r}" for nid in sorted(n0) for f in NURSE_HARD
           if n0[nid].get(f) != n1[nid].get(f)]
    bad += [f"R7/E4 {nid}.{f}: {n0[nid].get(f)!r} → {n1[nid].get(f)!r}" for nid in sorted(n0)
            for f in EXCLUSIONS["E4"].fields if n1[nid].get(f) != (None if off_first else n0[nid].get(f))]
    return bad


def _grade_violations(l0: dict, l1: dict) -> list[str]:
    """R8 — 등급 설정 같음. E5 — allow_soft_fallback 만 L0 거짓 → L1 참으로 바뀔 수 있다."""
    g0, g1 = l0.get("grade_config") or {}, l1.get("grade_config") or {}
    soft = EXCLUSIONS["E5"].fields
    bad = [f"R8 grade_config.{k}: {g0.get(k)!r} → {g1.get(k)!r}" for k in sorted(set(g0) | set(g1))
           if k not in soft and g0.get(k) != g1.get(k)]
    bad += [f"R8/E5 grade_config.{k}: {g0.get(k)!r} → {g1.get(k)!r}" for k in soft
            if g0.get(k) != g1.get(k) and not (not g0.get(k) and g1.get(k) is True)]
    return bad


def contract_violations(l0: dict, l1: dict, l0p: dict | None = None) -> list[str]:
    """R0~R8(제외 E1~E5 반영) 위반 목록 — 빈 목록이면 계약을 지킨다."""
    c0, c1 = l0["config_dict"], l1["config_data"]
    bad = _key_violations(c0, c1)
    bad += [f"R1 {k}: {c0.get(k, '<없음>')!r} → {c1.get(k, '<없음>')!r}" for k in _hard_keys()
            if c0.get(k, "<없음>") != c1.get(k, "<없음>")]
    bad += _cell_violations(c0, c1)
    bad += _fixed_violations(l0, l1)
    bad += [f"R6 {k} 다름" for k in PASS_THROUGH
            if json.dumps(c0.get(k), sort_keys=True, default=str) != json.dumps(c1.get(k), sort_keys=True, default=str)]
    return bad + _nurse_violations(l0, l1) + _demand_violations(c0, c1, l0p) + _grade_violations(l0, l1)


def _cell_violations(c0: dict, c1: dict) -> list[str]:
    """R2~R4 — L0 의 강제 OFF·금지칸·OFF 창 ⊆ 그 config(첫 시도 L1 · 사다리·탐색 시도마다)."""
    (fo0, fb0), (fo1, fb1) = _ic(c0), _ic(c1)
    return ([f"R2 forced_off {x}" for x in sorted(fo0 - fo1)] + [f"R3 forbidden {x}" for x in sorted(fb0 - fb1)]
            + [f"R4 off_window {x}" for x in sorted(_ow(c0) - _ow(c1))])


# ── ① 골든 16(기록 대조 · 정적) ──────────────────────────────────────────


def test_golden_names_are_the_golden_files():
    require_golden(GOLDEN)
    assert {p.stem for p in GOLDEN_DIR.glob("*.json")} == set(GOLDEN_NAMES)


@pytest.mark.parametrize("name", GOLDEN_NAMES)
def test_golden_l0_hard_elements_reach_l1(name):
    require_golden(GOLDEN)
    rec = json.loads((GOLDEN_DIR / f"{name}.json").read_text(encoding="utf-8"))
    layers = rec["layers"]
    if name in PRE_ENGINE_BLOCK:
        assert not layers.get("L1") and rec["path"] == PRE_ENGINE_BLOCK[name], (rec["path"], len(layers.get("L1") or []))
        return
    assert layers.get("L0") and layers.get("L1"), name
    assert contract_violations(layers["L0"][0], layers["L1"][0], (layers.get("L0p") or [None])[0]) == []
    # 사다리 골든 — 재시도·탐색 시도마다 L0 경계 칸이 그 시도의 엔진 입력에 있다(리뷰 HIGH).
    c0 = layers["L0"][0]["config_dict"]
    assert [f"{site}: {v}" for site, cfg in attempt_configs(layers) for v in _cell_violations(c0, cfg)] == []


# ── ② 경계 변형(프로덕션 경로를 L2 직전까지 실제로 돈다 · solve 0) ─────────────────


def _in(tail: str, cfg: str, **over) -> BoundaryCase:
    return BoundaryCase(name=f"in-{tail}-{cfg}", focus="X01", boundaries=(("after", 19),), assignments=[x01_in(20, 25)],
                        g2_nov={"X01": tail_cells(tail, 19)}, config=CFG[cfg], **over)


def _ret(tail: str, cfg: str, **over) -> BoundaryCase:
    return BoundaryCase(name=f"ret-{tail}-{cfg}", focus="N07", boundaries=(("after", 14),),
                        assignments=[n07_out(d(11, 10), d(11, 14))], g2_nov={"N07": tail_cells(tail, 14)},
                        config=CFG[cfg], g2_nurses=(), **over)


def _transfer_in(tail: str, cfg: str) -> BoundaryCase:
    return BoundaryCase(name=f"tin-{tail}-{cfg}", focus="X01", boundaries=(("after", 19),),
                        assignments=[x01_in(20, 0, reason="병동이동")], g2_nov={"X01": tail_cells(tail, 19)},
                        config=CFG[cfg])


LIVE = [
    _in("OOOONN", "Y"),
    _in("OOOONN", "Y", g1_oct=PREV_OCT),
    _in("ODDDDD", "X", g1_oct=PREV_OCT),
    _in("OOOOON", "X"),
    _ret("OOONN", "Y", g1_oct=PREV_OCT),
    _ret("OOOON", "X"),
    _transfer_in("OOOONN", "Y"),
    _in("OOOONN", "Y", fixed_wanted=[("X01", 20, "D")]),
    BoundaryCase(name="in+ret-Z-prev", focus="X01", boundaries=(("after", 19),),
                 assignments=[x01_in(20, 25), n07_out(d(11, 10), d(11, 14))],
                 g2_nov={"X01": tail_cells("OOOOON", 19), "N07": tail_cells("ONNNO", 14)}, g1_oct=PREV_OCT,
                 config=CFG["Z"]),
    early_in(2, "OOOOON", "N", "Y"),
    early_ret(3, "OOOONN", "NO", "X", prev=True),
    night_cap_case(),
]


@pytest.mark.parametrize("case", LIVE, ids=[c.name + ("-prev" if c.g1_oct and not c.name.endswith("-prev") else "")
                                            + ("-fixedD" if c.fixed_wanted else "") for c in LIVE])
def test_live_boundary_l0_hard_elements_reach_l1(case, engine, engine_input_tables):
    l0, l1, rec = run_to_l1(engine, case)
    assert l0 and l1, (rec.outcome, rec.error)
    fo0, fb0 = _ic(l0["config_dict"])
    focus = {x for x in fo0 | fb0 | _ow(l0["config_dict"]) if x[0] == case.focus}
    assert focus, "표본이 헛돈다 — L0 에 초점 간호사의 월중 경계 칸이 없다"
    assert contract_violations(l0, l1, (rec.layers.get("L0p") or [None])[0]) == []


# ── ③ 월중 경계 칸 계약 — L1 의 경계 칸이 '경계만의 위반 이어짐'을 모두 막는다 ─────────────


def _active(l1: dict, nid: str) -> set[int]:
    blocked = {int(x) for x in ((l1["config_data"].get("blocked_by_nurse_id") or {}).get(nid) or ())}
    return set(range(NDAYS)) - blocked


def _assert_cells_block(case: BoundaryCase, engine, rules: tuple[str, ...] | None = None) -> None:
    """L1 경계 칸이 경계만의 위반 이어짐(짧은 것 — 긴 것은 짧은 것이 막히면 같이 막힌다)을 모두 막는다. 못 막으면 `BoundaryGap`.
    `rules` 를 주면 그 규칙(예 '#1')이 걸린 이어짐만 본다 — 나머지는 엔진 몫이라 엔진 시험의 탐침이 본다."""
    _l0, l1, rec = run_to_l1(engine, case)
    assert l1, (rec.outcome, rec.error)
    cfg = l1["config_data"]
    cells = l1_cells(l1, case.focus)
    conts = minimal(boundary_only(case, cfg, active=_active(l1, case.focus)))
    if rules is not None:
        conts = [c for c in conts if any(v.split()[0] in rules for v in hard_violations(case.outside(), c, cfg))]
    assert conts, "표본이 헛돈다 — 경계만의 위반 이어짐이 없다(꼬리·설정을 바꿀 것)"
    open_ = [f"{''.join(c.values())}@{min(c)} {hard_violations(case.outside(), c, cfg)}" for c in conts
             if not excluded(c, cells)]
    if open_:
        raise BoundaryGap(f"{case.name}: L1 경계 칸 {cells} 이 막지 못한 위반 이어짐 {open_}")


MID_MONTH = [
    *[_in(t, c) for t, c in (("OOOONN", "X"), ("OOOONN", "Y"), ("OOOONN", "Z"), ("OOOOON", "X"), ("OOOOON", "Y"),
                             ("OOOOON", "Z"), ("OOONNN", "X"), ("OOONNN", "Y"), ("ODDDDD", "X"), ("ODDDDD", "Y"),
                             ("OOOODE", "X"), ("OOOODE", "Y"), ("OOODDN", "X"), ("DDDDNN", "Y"), ("OOONNO", "Y"),
                             ("OODDDD", "X"), ("OONNNO", "X"))],
    *[_ret(t, c) for t, c in (("OOONN", "X"), ("OOONN", "Y"), ("OOOON", "X"), ("OOOON", "Z"), ("ONNNO", "Y"),
                              ("ODDDD", "Y"), ("OOODE", "Y"), ("ODDNN", "X"))],
    _transfer_in("OOOONN", "X"), _transfer_in("OOOOON", "Z"),
    *EARLY,
    night_cap_case(),
]


@pytest.mark.parametrize("case", MID_MONTH, ids=[c.name for c in MID_MONTH])
def test_mid_month_boundary_cells_block_every_boundary_only_violation(case, engine, engine_input_tables):
    _assert_cells_block(case, engine)


#: 출발(앞쪽) 경계 — 상대 병동 근무표가 먼저 있으면 이 병동의 '떠나기 직전' 칸을 상대 첫 칸과 맞춘다(점검 G3 · EI-11 11w-2 · D34).
#: 11q 의 strict xfail 재료 2건 + 출발 재료(`boundary_cases.DEPARTURES`) 가운데 #1·#2·#3 이어짐이 있는 것(상대 첫 칸 O 는 칸 0 이 맞다).
DEPARTURE = [
    BoundaryCase(name="depart-N07-G2first", focus="N07", boundaries=(("before", 9),),
                 assignments=[n07_out(d(11, 10), d(11, 14))],
                 g2_nov={"N07": {10: "D", 11: "D", 12: "O", 13: "D", 14: "D"}}, config=CFG["Y"], g2_nurses=()),
    BoundaryCase(name="transfer-out-N08-G2first", focus="N08", boundaries=(("before", 15),),
                 assignments=[transfer_out("N08", d(11, 16))],
                 g2_nov={"N08": {16: "D", 17: "D", 18: "E"}}, config=CFG["Y"], g2_nurses=()),
    *[c for c in DEPARTURES if not c.name.startswith("dep-ODDOO")],
]


@pytest.mark.parametrize("case", DEPARTURE, ids=[c.name for c in DEPARTURE])
def test_departure_boundary_cells_block_every_boundary_only_violation(case, engine, engine_input_tables):
    """출발 경계 칸 계약(11w-2) — #1·#2·#3 은 L1 칸이 막는다. 이 병동 끝 N 블록의 회복(#4·#5)은 엔진이 막는다(출발 뒤 날은 차단일이라
    엔진 회복 식에서 'OFF 아님') — 엔진 시험의 탐침이 본다."""
    _assert_cells_block(case, engine, rules=("#1", "#2", "#3"))


# ── ④ 월초(1일) 합류 — 상대 병동 전월 꼬리 보충(`_inbound_source_map`)이 엔진 입력에 닿는다(리뷰 MEDIUM) ─────────────


def _trailing(seq: str, members: str) -> int:
    return len(seq) - len(seq.rstrip(members))


@pytest.mark.parametrize("case", MONTH_START_IN, ids=[c.name for c in MONTH_START_IN])
def test_month_start_join_carries_other_ward_prev_tail(case, engine, engine_input_tables):
    """1일 합류 간호사는 이 병동 전월 근무표에 없다 — 월경계가 상대 병동 전월 꼬리로 보충해야 한다(보충을 버리면 엔진의 월경계
    N 상한·회복·전이가 그 간호사에게 안 걸린다). 엔진 입력의 월경계 이월 값이 상대 병동 꼬리에서 나온 값과 같은지 본다."""
    _l0, l1, rec = run_to_l1(engine, case)
    assert l1, (rec.outcome, rec.error)
    cfg = l1["config_data"]
    tail = "".join(case.g2_oct["X01"].get(day, "-") for day in range(OCT_DAYS - 5, OCT_DAYS + 1))
    assert (cfg.get("prev_month_last_main") or {}).get("X01") == tail[-1]
    assert (cfg.get("prev_month_n_tail") or {}).get("X01", 0) == _trailing(tail, "N")
    cells = l1_cells(l1, "X01")
    work = _trailing(tail, "DEN")
    if work:
        assert any(a == 0 for a, _b in cells.windows) or 0 in cells.forced, (work, cells)
    if tail[-1] == "N":
        assert {"D", "E"} <= cells.forbidden.get(0, set()), cells


# ── ⑤ 하드락 #6 — 같은 달 상대 병동 나이트를 이 병동 몫에서 뺀다(리뷰 MEDIUM) ─────────────────────


def test_monthly_night_cap_counts_other_ward_nights(engine, engine_input_tables):
    """X01 은 상대 병동에서 11월 나이트 12개를 섰다 → 이 병동 몫 월 N 상한은 15 − 12 = 3(L0·L1 같은 값 — precheck 도 본다)."""
    case = night_cap_case()
    l0, l1, rec = run_to_l1(engine, case)
    assert l0 and l1, (rec.outcome, rec.error)
    room = int(l1["config_data"].get("max_nig_per_month") or 15) - NIGHT_CAP_G2.count("N")
    n0 = {str(n["nurse_id"]): n for n in l0["nurses_dict"]}
    n1 = {str(n["nurse_id"]): n for n in l1["nurses_data"]}
    assert (n0["X01"].get("n_max"), n1["X01"].get("n_max")) == (room, room)
    assert all(n1[nid].get("n_max") is None for nid in n1 if nid != "X01"), "다른 간호사의 상한은 그대로다"


# ── ⑥ 사다리·탐색 시도마다 — L0 월중 경계 칸 ⊆ 그 시도의 엔진 입력(리뷰 HIGH) ──────────────────────

LADDER_BOUNDARY = ladder_boundary_scenarios()


@pytest.mark.parametrize("name", list(LADDER_BOUNDARY))
def test_ladder_and_search_attempts_keep_mid_month_cells(name, engine, engine_input_tables):
    """첫 시도 뒤 자동 완화 사다리·탐색(스냅숏 기준 MUS·UNDIAG · config_override 없는 주말휴무·야간한도 지목)이 엔진을 다시 부를
    때마다 L0 의 월중 경계 칸(강제 OFF·금지칸·OFF 창)이 그 시도 엔진 입력에 있다. 사다리 결과는 저장되므로 한 경로라도 버리면
    저장 근무표에 경계 하드락 위반이 나간다(리뷰 — 첫 시도만 보던 시험은 경로별 버림 변이 3종을 못 잡았다)."""
    spec = LADDER_BOUNDARY[name]
    try:
        rec = run_with_output(engine, spec.scenario).result
    finally:
        drain_live_graph_dirs()   # test_ladder.py 의 live graph 흔적 단언을 지킨다(test_expected_diff.py 와 같은 이유)
    c0 = rec.layers["L0"][0]["config_dict"]
    fo0, fb0 = _ic(c0)
    want = [{x for x in s if x[0] in spec.focus} for s in (fo0, fb0, _ow(c0))]
    assert all(want), f"표본이 헛돈다 — L0 에 경계 칸(강제 OFF·금지칸·OFF 창)이 다 있지 않다: {want}"
    attempts = attempt_configs(rec.layers)
    assert first_seen(site for site, _cfg in attempts) == spec.sites, (rec.outcome, rec.error)
    lost = [f"{site}: {v}" for site, cfg in attempts for v in _cell_violations(c0, cfg)]
    assert lost == [], lost


# ── ⑦ 순서 의존 드러내기(점검 §5.2) — 월중 경계 몫 키는 요청으로 보내면 400 ────────────────────


def test_mid_month_parts_key_in_request_is_400():
    """11r 은 스냅숏 탐색용 몫 키(`_mid_month_boundary_parts`)를 요청 config 보다 우선해 쓴다. 이 키를 요청으로 실으면 엔진 입력의
    월중 경계 칸이 통째로 바뀌므로(점검 실측 — 빈 값이면 X01 강제 OFF·금지·OFF 창 소실) 요청 가드가 400 으로 막아야 한다."""
    from services.engine_contract.settings import RUNTIME_DATA_NOT_SETTINGS
    from services.roster_create_service import _MID_MONTH_PARTS_KEY, assert_night_caps_within_hard_lock

    assert _MID_MONTH_PARTS_KEY in {key for key, _part in RUNTIME_DATA_NOT_SETTINGS}
    with pytest.raises(HTTPException) as exc:
        assert_night_caps_within_hard_lock({_MID_MONTH_PARTS_KEY: {"initial_constraints": {}, "off_window_constraints": {}}},
                                           None)
    assert exc.value.status_code == 400


# ── ⑧ 의도된 제외 표가 판정에 묶여 있다(리뷰 LOW — 표마다 '허용하는 변화는 통과 · 밖은 위반') ─────────────


def _pair(**over) -> tuple[dict, dict, dict]:
    """최소 L0 · L1 · L0p(같은 값) — 제외 단위 시험용."""
    nurse = {"nurse_id": "N01", "grade": 1, "preceptor_id": None, "o_min": 8, "o_max": None, "o_exact": None}
    demand = [{"D": 2, "E": 2, "N": 2}]
    l0 = {"config_dict": {"daily_shift_requirements_by_day": demand, "off_first": False}, "nurses_dict": [dict(nurse)],
          "fixed_cells": [], "grade_config": {"allow_soft_fallback": False, "constraints": {"1": 1}}}
    l1 = {"config_data": {"daily_shift_requirements_by_day": [dict(demand[0])], "off_first": False},
          "nurses_data": [dict(nurse)], "shift_manage_data": [], "grade_config": {"allow_soft_fallback": False,
                                                                                  "constraints": {"1": 1}}}
    l0p = {"config": {"daily_shift_requirements_by_day": [dict(demand[0])]}}
    for path, value in over.items():
        layer, *keys = path.split("__")
        target = {"l0": l0, "l1": l1, "l0p": l0p}[layer]
        for key in keys[:-1]:
            target = target[int(key)] if key.isdigit() else target[key]
        target[keys[-1]] = value
    return l0, l1, l0p


def _codes(l0, l1, l0p) -> set[str]:
    return {v.split()[0] for v in contract_violations(l0, l1, l0p)}


def test_exclusion_table_ids_are_the_judged_ones():
    """표의 제외는 E1~E5 이고 각자 칸이 있다(판정 함수가 그 칸을 읽는다 — 아래 시험이 제외마다 묶는다)."""
    assert sorted(EXCLUSIONS) == ["E1", "E2", "E3", "E4", "E5"]
    assert all(e.fields and e.reason for e in EXCLUSIONS.values())
    assert _codes(*_pair()) == set()


def test_exclusion_e1_l1_only_fixed_cell_must_be_on_blocked_day():
    cell = [{"nurse_index": 0, "day_index": 3, "shift": "D"}]
    assert _codes(*_pair(l1__config_data__fixed_cells=cell, l1__config_data__blocked_by_nurse_id={"N01": [3]},
                         l0__config_dict__blocked_by_nurse_id={"N01": [3]})) == set()
    assert "E1" in _codes(*_pair(l1__config_data__fixed_cells=cell))


def test_exclusion_e2_only_listed_keys_may_exist_only_in_l1():
    assert _codes(*_pair(l1__config_data__prev_month_n_tail={"N01": 2})) == set()
    assert "R0/E2" in _codes(*_pair(l1__config_data__some_new_key=1))
    assert "R0" in _codes(*_pair(l0__config_dict__only_in_l0=1))


def test_exclusion_e3_demand_may_drop_only_by_the_fixed_cut():
    cut = [{"D": 1, "E": 2, "N": 2}]
    assert _codes(*_pair(l1__config_data__daily_shift_requirements_by_day=cut,
                         l0p__config__daily_shift_requirements_by_day=cut)) == set()
    assert "E3" in _codes(*_pair(l1__config_data__daily_shift_requirements_by_day=cut))   # 차감 밖으로 줄었다
    up = [{"D": 3, "E": 2, "N": 2}]
    assert "E3" in _codes(*_pair(l1__config_data__daily_shift_requirements_by_day=up,
                                 l0p__config__daily_shift_requirements_by_day=up))   # L1 > L0


def test_exclusion_e4_off_limits_none_only_when_off_first():
    assert _codes(*_pair(l1__config_data__off_first=True, l0__config_dict__off_first=True,
                         l1__nurses_data__0__o_min=None)) == set()
    assert "R7/E4" in _codes(*_pair(l1__nurses_data__0__o_min=None))


def test_exclusion_e5_only_allow_soft_fallback_may_turn_on():
    assert _codes(*_pair(l1__grade_config__allow_soft_fallback=True)) == set()
    assert "R8" in _codes(*_pair(l1__grade_config__constraints={"1": 0}))
    assert "R8/E5" in _codes(*_pair(l0__grade_config__allow_soft_fallback=True))
    assert "R7" in _codes(*_pair(l1__nurses_data__0__grade=2))
    assert "R7" in _codes(*_pair(l1__nurses_data__0__preceptor_id="N09"))


# ── ⑨ 독립 판정기 자체 점검(손으로 적은 기대값) ────────────────────────────────


_BASE = {"banned_day_after_eve": True, "three_seq_nig": True, "max_conseq_work": 5}
_G2_N12 = {i: "N" for i in range(19) if NIGHT_CAP_G2[i] == "N"}
JUDGE = [
    ({17: "N", 18: "N"}, {19: "N", 20: "N"}, {**_BASE, **CFG["X"]}, {"#3"}),
    ({18: "N"}, {19: "D"}, {**_BASE, **CFG["X"]}, {"#2"}),
    ({18: "E"}, {19: "D"}, {**_BASE, **CFG["X"]}, {"#2"}),
    ({13: "D", 14: "D", 15: "D", 16: "D", 17: "D"}, {18: "D"}, {**_BASE, **CFG["X"]}, {"#1"}),
    ({17: "N", 18: "N"}, {19: "O", 20: "D"}, {**_BASE, **CFG["Y"]}, {"#5"}),
    ({17: "N", 18: "N"}, {19: "O", 20: "D"}, {**_BASE, **CFG["X"]}, set()),
    ({16: "N", 17: "N", 18: "N"}, {19: "O", 20: "D"}, {**_BASE, **CFG["X"]}, {"#4"}),
    ({18: "O"}, {19: "N", 20: "O"}, {**_BASE, "not_one_night": True}, {"#7"}),
    ({18: "N"}, {19: "O", 20: "O"}, {**_BASE, "not_one_night": True}, set()),
    ({0: "N", 1: "N", 2: "N", 3: "N"}, {10: "D"}, {**_BASE, **CFG["X"]}, set()),
    ({17: "N"}, {19: "N", 20: "N"}, {**_BASE, **CFG["Z"]}, set()),
    ({17: "N", 18: "N"}, {19: "N"}, {**_BASE, **CFG["Z"]}, {"#3"}),
    # #6 — 같은 달 바깥 나이트 12 + 이 병동 4 = 16 > 15 · 3 이면 15 · 바깥이 전월이면 세지 않는다.
    (_G2_N12, {21: "N", 22: "N", 25: "N", 26: "N"}, {**_BASE, **CFG["X"]}, {"#6"}),
    (_G2_N12, {21: "N", 22: "N", 25: "N"}, {**_BASE, **CFG["X"]}, set()),
    ({k - OCT_DAYS: v for k, v in _G2_N12.items()}, {21: "N", 22: "N", 25: "N", 26: "N"}, {**_BASE, **CFG["X"]}, set()),
]


@pytest.mark.parametrize("outside,mine,cfg,expected", JUDGE)
def test_independent_judge_reference(outside, mine, cfg, expected):
    assert {v.split()[0] for v in hard_violations(outside, mine, cfg)} == expected
