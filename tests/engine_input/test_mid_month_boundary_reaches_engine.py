"""월중 경계(파견·복귀) 하드락 제약이 엔진 입력에 닿는다 — EI-11 11r(리뷰 HIGH · 운영 main 에도 있던 결함).

precheck 입력(L0 `config_dict`)에 실린 월중 경계 금지칸·강제 OFF·OFF 창은 엔진 입력(L1 `config_data`)에도 모두 있어야
한다(L0 ⊆ L1). 예전엔 `_run_cp_sat_basic` 이 `initial_constraints` 를 월경계·허용 시프트·금지 원티드로만 다시 지어 덮어
월중 경계 몫이 사라졌고, 전월 근무표가 있으면 `off_window_constraints` 를 월경계 것으로 통째로 바꿨다 — 파견 온 간호사가
상대 병동에서 N,N 을 선 바로 다음 날 D 를 받는 배정이 하드락(#1·#2·#4·#5) 판정 없이 나갈 수 있었다.

S6(파견 in/out · 병동이동)과, 전월 근무표(S7 마감본 꼬리)를 더하고 X01 의 상대 병동 꼬리를 OOOONN 으로 바꾼 변형 두 개를
돈다. 판정은 '포함'이다 — L1 에는 허용 시프트·월경계 몫이 더 있다.
"""

from __future__ import annotations

import dataclasses
from datetime import datetime
from functools import partial

from .harness import run_with_output
from .scenario_defs import SCENARIOS, _s6_other_schedule, _s7_prev_schedules, s6_ward
from .ward import OFFICE_ID, OTHER_GROUP_ID, seed_ward, stamp


def _other_schedule_with_tail(tail: str):
    """상대 병동 G2 11월 근무표 — X01 의 14~19일을 `tail`(6글자)로 둔다. N07(파견 out)은 S6 그대로."""
    def seed(db):
        from db.models import Schedule, ScheduleEntry

        db.add(Schedule(schedule_id="g2nov0000001", office_id=OFFICE_ID, group_id=OTHER_GROUP_ID,
                        year=2026, month=11, version=1, config_id=None, created_by="acc_HN01",
                        status="draft", dropped=False, name="G2 11월", **stamp()))
        cells = [("N07", d, "O" if d == 12 else "D") for d in range(10, 15)]
        cells += [("X01", 14 + i, code) for i, code in enumerate(tail)]
        for i, (nid, day, code) in enumerate(cells):
            db.add(ScheduleEntry(entry_id=f"g2e{i:05d}", schedule_id="g2nov0000001", nurse_id=nid,
                                 work_date=datetime(2026, 11, day), shift_id=code))
    return seed


def _s6_with_prev_month():
    """S6 + 전월(10월) 마감본 꼬리 + X01 상대 병동 꼬리 OOOONN + 2N·3N 뒤 2OFF 켬 — 월경계 OFF 창이 생기는 달."""
    ward = s6_ward()
    ward.extras = [_other_schedule_with_tail("OOOONN") if e is _s6_other_schedule else e for e in ward.extras]
    ward.extras.append(_s7_prev_schedules)
    ward.config.update(two_offs_after_two_nig=True, two_offs_after_three_nig=True)
    return dataclasses.replace(SCENARIOS["S6"], name="S6+전월", seed=partial(seed_ward, ward))


def _layers(engine, scenario):
    rec = run_with_output(engine, scenario).result
    assert rec.layers.get("L0") and rec.layers.get("L1"), (rec.outcome, rec.error)
    return rec.layers["L0"][0]["config_dict"], rec.layers["L1"][0]["config_data"]


def _cells(ic) -> tuple[set, set]:
    """initial_constraints → ({(간호사, 일)} 강제 OFF, {(간호사, 일, 코드)} 금지)."""
    ic = ic or {}
    forced = {(str(n), int(d)) for n, days in (ic.get("forced_off") or {}).items() for d in days or ()}
    forbidden = {(str(n), int(d), str(c)) for n, day_map in (ic.get("forbidden") or {}).items()
                 for d, codes in (day_map or {}).items() for c in codes or ()}
    return forced, forbidden


def _windows(ow) -> set:
    return {(str(n), tuple(w)) for n, wins in (ow or {}).items() for w in wins or ()}


def _assert_mid_month_reaches_engine(l0: dict, l1: dict) -> None:
    forced0, forbidden0 = _cells(l0.get("initial_constraints"))
    forced1, forbidden1 = _cells(l1.get("initial_constraints"))
    assert forced0 and forbidden0, "표본이 헛돈다 — precheck 입력에 월중 경계 강제 OFF·금지칸이 없다"
    assert forced0 <= forced1, sorted(forced0 - forced1)
    assert forbidden0 <= forbidden1, sorted(forbidden0 - forbidden1)
    windows0 = _windows(l0.get("off_window_constraints"))
    assert windows0, "표본이 헛돈다 — precheck 입력에 월중 경계 OFF 창이 없다"
    assert windows0 <= _windows(l1.get("off_window_constraints")), sorted(windows0 - _windows(
        l1.get("off_window_constraints")))


def test_merge_off_window_constraints_unions_per_nurse():
    """합치기 규칙 — 간호사별 합집합(같은 창은 한 번 · 순서 base → extra), 모양이 틀린 쪽·창은 건너뛴다(예외 없음)."""
    from services.roster_create_service import _merge_off_window_constraints as merge

    assert merge({"N07": [[0, 3]], "N01": [[0, 1]]}, {"N07": [[14, 17], [0, 3]], "X01": [[19, 22]]}) == {
        "N07": [[0, 3], [14, 17]], "N01": [[0, 1]], "X01": [[19, 22]]}
    assert merge(None, {"X01": [(19, 19)]}) == {"X01": [[19, 19]]}
    assert merge({"N07": "x", "X01": [[19, 19], "bad", 3]}, [1]) == {"X01": [[19, 19]]}
    assert merge(None, None) == {}


def test_snapshot_probe_merges_only_the_mid_month_part():
    """탐색(UNDIAG·A1·MUS)이 엔진 스냅숏을 기준으로 다시 부를 때 — 스냅숏의 합쳐진 옛 월경계 칸(그 시도 config 로 다시 지어야 할
    칸)을 되살리지 않고, 따로 실은 월중 경계 몫만 합친다. 키는 꺼내 엔진 입력에 안 실린다."""
    from services.roster_create_service import _MID_MONTH_PARTS_KEY, _boundary_cells_for_engine, _pop_mid_month_parts

    stale = {"forced_off": {"N01": [0, 1], "X01": [19]}, "forbidden": {}}             # 연속근무 상한 4 로 지은 월경계 + 월중
    snapshot_cfg = {"initial_constraints": stale, "off_window_constraints": {"N01": [[0, 3]], "X01": [[19, 22]]},
                    _MID_MONTH_PARTS_KEY: {"initial_constraints": {"forced_off": {"X01": [19]}},
                                           "off_window_constraints": {"X01": [[19, 22]]}},
                    "max_conseq_work": 5}                                               # 탐색이 올린 값
    mid_ic, mid_ow = _pop_mid_month_parts(snapshot_cfg)
    assert _MID_MONTH_PARTS_KEY not in snapshot_cfg
    fresh_cross = {"forced_off": {"N01": [0]}, "forbidden": {}, "off_window_constraints": {"N01": [[0, 2]]}}  # 상한 5 로 다시 지음
    ic, ow = _boundary_cells_for_engine(fresh_cross, {"forbidden": {"X01": {3: ["N"]}}}, {}, mid_ic, mid_ow)
    assert ic["forced_off"] == {"N01": [0], "X01": [19]}                             # 옛 N01 1일 칸은 되살리지 않는다
    assert ic["forbidden"] == {"X01": {3: ["N"]}}
    assert ow == {"N01": [[0, 2]], "X01": [[19, 22]]}                               # 옛 N01 [0, 3] 창도 되살리지 않는다


def test_run_cp_sat_basic_pops_the_part_first_and_snapshots_it_for_probes():
    """원문 단언 — `_run_cp_sat_basic` 은 config 사본을 만든 바로 다음 줄에서 월중 경계 몫을 꺼내고(엔진 입력에 키가 안 실린다),
    엔진 스냅숏(`_effective_config_snapshot`)에 그 몫을 `_MID_MONTH_PARTS_KEY` 로 싣는다. 스냅숏은 엔진이 끝까지 돌아야 생겨
    골든 하네스(L2 에서 멈춤)로는 닿지 않는다 — 이 단언이 빠지면 탐색이 합쳐진 옛 월경계 칸을 다시 합친다."""
    import ast
    from pathlib import Path

    import services.roster_create_service as rcs

    tree = ast.parse(Path(rcs.__file__).read_text(encoding="utf-8"))
    (func,) = [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "_run_cp_sat_basic"]
    bodies = [[ast.unparse(s) for s in n.body] for n in ast.walk(func) if isinstance(getattr(n, "body", None), list)]
    (body,) = [b for b in bodies if any(s.startswith("config_dict = config_override.copy() if") for s in b)]
    copy_at = next(i for i, s in enumerate(body) if s.startswith("config_dict = config_override.copy() if"))
    assert body[copy_at + 1] == "_mid_ic, _mid_ow = _pop_mid_month_parts(config_dict)"
    snap = [ast.unparse(n) for n in ast.walk(func) if isinstance(n, ast.Assign)
            and ast.unparse(n).startswith("_eff_snap[_MID_MONTH_PARTS_KEY] = ")]
    assert snap == ["_eff_snap[_MID_MONTH_PARTS_KEY] = {'initial_constraints': deepcopy(_mid_ic), "
                    "'off_window_constraints': deepcopy(_mid_ow)}"]


def test_service_config_uses_the_standard_keys_as_the_mid_month_part():
    """생성 서비스 config(첫 시도·사다리)에는 따로 실은 몫이 없다 — 표준 키(서비스가 precheck 앞에서 월중 경계만 심는 칸)가 그 몫이다."""
    from services.roster_create_service import _pop_mid_month_parts

    ic, ow = {"forced_off": {"X01": [19]}}, {"X01": [[19, 19]]}
    cfg = {"initial_constraints": ic, "off_window_constraints": ow}
    assert _pop_mid_month_parts(cfg) == (ic, ow) and cfg == {"initial_constraints": ic, "off_window_constraints": ow}
    assert _pop_mid_month_parts({}) == (None, None)


def test_s6_mid_month_boundary_cells_reach_engine(engine, engine_input_tables):
    """S6 — X01(파견 in · 상대 병동 꼬리 D D D D N N)의 합류일 강제 OFF·D/E 금지와 OFF 창이 엔진 입력에 있다."""
    l0, l1 = _layers(engine, SCENARIOS["S6"])
    _assert_mid_month_reaches_engine(l0, l1)
    forced1, forbidden1 = _cells(l1["initial_constraints"])
    assert ("X01", 19) in forced1
    assert {("X01", 19, "D"), ("X01", 19, "E")} <= forbidden1


def test_prev_month_window_does_not_replace_mid_month_windows(engine, engine_input_tables):
    """전월 근무표가 있는 달 — 월경계 OFF 창과 월중 경계 OFF 창이 간호사별로 합쳐진다(한쪽이 다른 쪽을 덮지 않는다)."""
    l0, l1 = _layers(engine, _s6_with_prev_month())
    _assert_mid_month_reaches_engine(l0, l1)
    windows1 = _windows(l1["off_window_constraints"])
    assert ("X01", (19, 22)) in windows1  # 월중 경계(G2 꼬리 N N → 2N 뒤 OFF 2회 · 연속근무 창)
    assert ("N07", (14, 17)) in windows1  # 월중 경계(파견 out 복귀)
    assert ("N01", (0, 3)) in windows1  # 월경계(전월 꼬리) — 합쳐도 그대로 남는다
    assert len({n for n, _w in windows1}) > len({n for n, _w in _windows(l0["off_window_constraints"])})
