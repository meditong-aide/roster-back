"""schedule_edit 규칙(rules.py) ↔ 생성 엔진(fallback_lex) 자동 대조.

rules.py 는 엔진 하드 규칙을 옮겨 적은 것이라, 엔진이 바뀌었는데 rules.py 를 안 고치면 판정이
어긋난다. 그래서 작은 병동을 **실제 생성 엔진**(DB 없이 `generate_roster_cp_sat`)으로 만들고, 그
결과를 `rules.measure` 로 잰다. 엔진이 하드로 지키는 규칙 종류의 위반이 하나라도 나오면 둘 중
한쪽 정의가 바뀐 것이다 — rules.py 를 엔진에 다시 맞춘다.

입력은 서비스 조립(`roster_create_service._generate_roster_service_impl` RCS:5862~5925 ·
`_run_cp_sat_basic` RCS:3467)이 엔진에 넘기는 키만 최소로 맞췄다. 키 이름은 이 브랜치의
`create_config_from_db`(CSB:446~) 가 읽는 이름이다.

★ 엔진은 CP-SAT 워커 8개(FL:4085 등)라 seed 를 고정해도 해가 매번 같지는 않다. 이 시험은
  특정 해가 아니라 '어떤 해든 하드 위반 0' 을 보므로 그래도 된다.
★ 느려서(약 23초·CPU 144초) 기본 실행에서 뺀다 — 엔진이나 rules.py 를 고쳤을 때 `pytest -m engine_parity`.
"""

from __future__ import annotations

import calendar
import copy
from datetime import date

import pytest

from services.cp_sat.shift_normalizer import build_shift_normalizer, normalize_shift_code
from services.cp_sat_basic import generate_roster_cp_sat
from services.schedule_edit.context import EDIT_MAINS, EditContext
from services.schedule_edit.rules import build_rules, measure

pytestmark = pytest.mark.engine_parity

YEAR, MONTH = 2026, 11
DAYS = calendar.monthrange(YEAR, MONTH)[1]          # 30 · 11/1 이 일요일
OFFICE, GROUP = "O1", "G1"
OFF_DAYS = 9
#: CSB:5515 `time_limit_seconds` — 총 상한이 아니라 단계별 배분 기준이다(FL:586). 10명·30일이면 충분하다.
TIME_LIMIT = 8

WEEKEND_OFF = "N01"     # 주말휴무자 — 주말 OFF·평일 근무(FL:1495)
DE_ONLY = "N02"         # 허용 근무형 D·E — N(·M) 금지(FL:2634 허용 근무형 마스크)
N_CAP = "N03"           # 개인 월 한도 N ≤ 4(monthly_limit_constraints · FL:3635)
#: 기피 근무(initial_constraints.forbidden) — 엔진은 하드(FL:1648). {nurse_id: {0-based day: [codes]}}
AVOID = {"N04": {9: ["N"]}, "N05": {14: ["D", "E"]}}

#: 엔진이 하드로 거는 규칙 종류 — 엔진 해에서 위반 0 이어야 한다.
ENGINE_HARD = {
    "연속근무",        # FL:2388 K+1 창마다 OFF ≥ 1
    "전환금지",        # FL:2070 ND·ED·NE / FL:2150 M 앞은 D·O·M / FL:2159 M→D
    "연속나이트",      # FL:2582
    "3N후OFF",         # FL:2763
    "2N후OFF",         # FL:2899
    "월나이트",        # FL:2612
    "1N",              # FL:2224
    "같은근무연속",    # FL:2467 same_shift_hard_k
    "고립근무",        # FL:2533 isolated_work_hard
    "금지근무",        # FL:1648 기피 근무 + FL:2634 허용 근무형
    "주말휴무",        # FL:1495
    "인원최대",        # FL:1897 최대 설정 / FL:1863 M 은 최대 없으면 최소가 상한
    "개인한도",        # FL:3635 stage2·3 하드(stage1 초과칸도 0 아니면 버린다)
    "OFF수:하한",      # FL:3414 off_first=False 면 OFF ≥ off_days 하드
    # N 간격은 stage2 부터 걸고 위반칸 합을 최소화해 고정한다(FL:3028 · AIDE_N2N_SLACK).
    # 0 이 못 되면 엔진이 위반을 남기지만, 넉넉한 병동에서는 0 이어야 한다.
    "N간격",
    "휴무전날N",       # FL:2244 — 이 시험엔 휴가·확정 원티드 칸이 없어 식이 안 생긴다
    "팀최소",          # team_constraints — 이 시험엔 팀이 없어 식이 안 생긴다
}
#: 대조에서 빼는 종류 — 엔진이 벌점으로만 다루거나 화면·수정 전용이다(키: 빼는 이유).
EXCLUDED = {
    "인원최소": "엔진은 부족을 슬랙으로 두고 최소화할 뿐이다(FL:1890 `assigned + short >= need`)",
    "등급최소": "엔진은 벌점(grade lex 패스)이다. 이 시험은 등급 설정(grade_config)도 넣지 않는다",
    "OFF수:상한": "엔진은 상한 +1 을 벌점 슬랙으로 허용한다(FL:3503 cap_slack). rules 는 하한=상한",
    "2N후OFF(화면)": "화면(useRosterValidation) 전용 — 엔진보다 엄격하다(NNN 의 첫 NN 도 본다)",
    "상호배제": "엔진은 벌점이다(fallback_objectives) — 이 시험엔 짝도 없다",
    "원티드": "수정 전용 규칙(지금 반영된 원티드 유지) — 엔진 생성에는 대응 규칙이 없다",
}

SCENARIOS = {
    # 3N 뒤 OFF 끔 + 2N 뒤 OFF 켬 · 일별 최대 설정(N 은 정확히 2명)
    "2N후OFF만": dict(
        use_mid=False, two_offs_after_three_nig=False, two_offs_after_two_nig=True, n2n_min_gap=0,
        need={"D": 2, "E": 2, "N": 2}, cap={"D": 3, "E": 3, "N": 2},
    ),
    # MID 사용 · 하드락 전부 켬 · N 간격 하한 4 · 최대 미설정(M 은 최소가 곧 상한)
    "MID+N간격": dict(
        use_mid=True, two_offs_after_three_nig=True, two_offs_after_two_nig=True, n2n_min_gap=4,
        need={"D": 2, "E": 2, "N": 1, "M": 1}, cap={"D": 0, "E": 0, "N": 0, "M": 0},
    ),
}


def _shift_definitions(use_mid: bool) -> list:
    rows = [{"shift_id": c, "default_shift": c, "shift_gb": c, "type": "근무"} for c in ("D", "E", "N")]
    if use_mid:
        rows.append({"shift_id": "M", "default_shift": "M", "shift_gb": "M", "type": "근무"})
    rows.append({"shift_id": "OFF", "default_shift": "O", "shift_gb": None, "type": "휴무"})
    return rows


def _engine_config(sc: dict) -> dict:
    """엔진 config_data — 하드락(CLAUDE.md 1~8)은 켜고, 요구인원·OFF 는 넉넉하게."""
    need, cap = sc["need"], sc["cap"]
    two_n = sc["two_offs_after_three_nig"] or sc["two_offs_after_two_nig"]
    return {
        "max_conseq_work": 5, "three_seq_nig": True, "not_one_night": True, "banned_day_after_eve": True,
        "two_offs_after_three_nig": sc["two_offs_after_three_nig"],
        "two_offs_after_two_nig": sc["two_offs_after_two_nig"],
        "max_nig_per_month": 15, "use_mid": sc["use_mid"], "off_days": OFF_DAYS, "off_first": False,
        # 설정 컬럼 아닌 엔진 하드 — 엔진 기본값(CSB:621·623)을 명시해 ctx 와 같은 값을 쓴다
        "same_shift_hard_k": 3, "isolated_work_hard": True,
        "n2n_min_gap": sc["n2n_min_gap"],                     # RCS:5878
        "weekend_off_only_enable": True,
        "max_extra_off_days": 6 if two_n else 1,              # RCS:5919 None 보정과 같다
        "extra_off_penalty_weight": 80,
        "daily_shift_requirements": dict(need),               # RCS:5871 (ShiftManage 기본)
        "daily_shift_requirements_by_day": [dict(need) for _ in range(DAYS)],
        "daily_shift_requirements_max_by_day": [dict(cap) for _ in range(DAYS)],   # 0 = 최대 없음
        "shift_definitions": _shift_definitions(sc["use_mid"]),                     # RCS:3562
        "initial_constraints": {"forbidden": copy.deepcopy(AVOID), "forced_off": {}},  # RCS:3662
        "grade_strategy": "COMBINED",
    }


def _shift_manage(sc: dict) -> list:
    """ShiftManage 행(`create_shift_manage_from_db` 모양) — 엔진은 main_code·codes 만 쓴다(FL:742)."""
    return [{"office_id": OFFICE, "group_id": GROUP, "nurse_class": "RN", "shift_slot": i + 1,
             "main_code": code, "codes": [code], "manpower": count}
            for i, (code, count) in enumerate(sc["need"].items())]


def _nurses() -> list:
    """10명 — 엔진 `create_nurses_from_db`(CSB:746) 가 읽는 키만."""
    rows = []
    for i in range(1, 11):
        nid = f"N{i:02d}"
        rows.append({
            "nurse_id": nid, "name": nid, "active": 1, "sequence": i, "experience": 20 - i,
            "grade": 1 if i <= 4 else 2, "allowed_shifts": [], "is_weekend_off": nid == WEEKEND_OFF,
            "team_id": None, "preceptor_id": None, "personal_off_adjustment": 0,
        })
    rows[1]["allowed_shifts"] = ["D", "E"]
    rows[2]["n_max"] = 4
    return rows


def _context(config: dict, nurses: list, mains: dict, raw: dict) -> EditContext:
    """엔진 입력과 같은 값으로 판정 재료를 채운다(test_solver.make_ctx 처럼 직접 · 전월 꼬리 없음)."""
    ctx = EditContext(schedule_id="engine-parity", group_id=GROUP, office_id=OFFICE,
                      year=YEAR, month=MONTH, days=DAYS)
    ctx.config = config
    ctx.use_mid = bool(config["use_mid"])
    ctx.rep_code = {c: c for c in EDIT_MAINS}
    codes = {"D", "E", "N"} | ({"M"} if ctx.use_mid else set())
    for row in nurses:
        nid = row["nurse_id"]
        ctx.nurse_ids.append(nid)
        ctx.names[nid] = row["name"]
        ctx.main[nid] = list(mains[nid])
        ctx.raw[nid] = list(raw[nid])
        ctx.raw_ids[nid] = [None] * DAYS
        ctx.off_bounds[nid] = (OFF_DAYS, OFF_DAYS)    # context._load_off_bounds(off_first=False) 와 같다
        banned = codes - set(row["allowed_shifts"]) if row["allowed_shifts"] else set()
        for d in range(DAYS):
            if banned:
                ctx.forbidden.setdefault((nid, d), set()).update(banned)
        if row.get("n_max") is not None:
            ctx.monthly_limits[nid] = {"n_max": row["n_max"]}
    for nid, by_day in config["initial_constraints"]["forbidden"].items():
        for d, banned_codes in by_day.items():
            ctx.forbidden.setdefault((nid, int(d)), set()).update(banned_codes)
    ctx.weekend_off = {r["nurse_id"] for r in nurses if r["is_weekend_off"]}
    ctx.weekend_days = {d for d in range(DAYS) if date(YEAR, MONTH, d + 1).weekday() >= 5}
    ctx.cov_min = [dict(x) for x in config["daily_shift_requirements_by_day"]]
    ctx.cov_max = [dict(x) for x in config["daily_shift_requirements_max_by_day"]]
    ctx.same_shift_k = int(config["same_shift_hard_k"])
    ctx.isolated_work_hard = bool(config["isolated_work_hard"])
    ctx.n2n_gap = int(config["n2n_min_gap"])
    return ctx


def _kind(key: tuple) -> str:
    """위반 위치 → 규칙 종류. OFF수는 하한(엔진 하드)·상한(엔진 벌점)을 나눈다."""
    return f"{key[0]}:{key[-1]}" if key[0] == "OFF수" else key[0]


@pytest.mark.parametrize("name", list(SCENARIOS))
def test_engine_roster_has_no_hard_rule_violations(name):
    sc = SCENARIOS[name]
    config = _engine_config(sc)
    nurses = _nurses()
    sent = copy.deepcopy(config)        # 엔진이 config_data 를 고쳐 쓸 수 있어 넣은 값을 따로 둔다

    out = generate_roster_cp_sat(
        nurses, [], config, YEAR, MONTH, _shift_manage(sc),
        time_limit_seconds=TIME_LIMIT, randomize=False, seed=0,
        # 등급 규칙은 대조에서 빼므로 등급 설정을 넣지 않는다(넣으면 엔진이 /tmp 에 CSV 도 쓴다 · CSB:5396)
        grade_strategy="COMBINED", grade_config=None,
    )

    # ── 엔진이 해를 냈는가 — 못 내면 건너뛰지 않고 실패(입력을 넉넉하게 고쳐야 한다) ──
    stage1_none = getattr(out["roster_system"], "_fallback_stage1_no_solution", None)
    assert stage1_none is None, f"[{name}] 엔진 1단계 해 없음({stage1_none}) — 입력을 더 넉넉하게"
    roster = out["roster"]
    assert set(roster) == {r["nurse_id"] for r in nurses}, f"[{name}] 결과 간호사 불일치: {sorted(roster)}"
    id_to_main, _ = build_shift_normalizer(sent["shift_definitions"])
    valid = {"D", "E", "N", "O"} | ({"M"} if sc["use_mid"] else set())
    mains = {nid: [normalize_shift_code(c, id_to_main) for c in row] for nid, row in roster.items()}
    bad = {nid: row for nid, row in mains.items() if len(row) != DAYS or not set(row) <= valid}
    assert not bad, f"[{name}] 해석 못 한 칸(빈 해?) — 원문 {({n: roster[n] for n in bad})}"
    assert any(c != "O" for row in mains.values() for c in row), f"[{name}] 전원 OFF — 엔진이 배정을 못 했다"

    ctx = _context(sent, nurses, mains, roster)
    rules = build_rules(ctx)

    # ── 시험이 헛돌지 않는지 — 이 입력이 거는 하드 규칙 식이 실제로 만들어졌는가 ──
    built = {_kind(i.key) for i in rules.ineqs}
    expected = {"연속근무", "전환금지", "연속나이트", "2N후OFF", "월나이트", "1N", "같은근무연속",
                "고립근무", "금지근무", "주말휴무", "인원최대", "개인한도", "OFF수:하한"}
    if sc["two_offs_after_three_nig"]:
        expected.add("3N후OFF")
    if sc["n2n_min_gap"] >= 4:
        expected.add("N간격")
    assert expected <= built, f"[{name}] 식이 안 만들어진 규칙: {sorted(expected - built)}"

    violations = measure(ctx, ctx.main, rules)
    unknown = sorted({_kind(k) for k in violations} - ENGINE_HARD - set(EXCLUDED))
    assert not unknown, f"[{name}] 분류 안 된 규칙 종류 {unknown} — ENGINE_HARD·EXCLUDED 중 하나에 넣을 것"

    hard = sorted((k for k in violations if _kind(k) in ENGINE_HARD), key=str)
    rows = "\n".join(f"  {n}: {''.join(mains[n])}" for n in sorted({k[1] for k in hard if k[1]}))
    assert not hard, (
        f"[{name}] 엔진 해가 rules.py 하드 규칙 {len(hard)}건을 어긴다 — rules.py 와 엔진 정의가 어긋났다\n"
        f"  위반 키: {hard[:40]}\n{rows}"
    )
