"""경계(월경계·월중 경계) 하드락 시험 재료 — EI-11 11q(점검 `ei11/q11/AUDIT.md` ②③ · 리뷰 반영).

계약 테스트(`test_l0_l1_hard_contract.py`)와 실제 엔진 경계 시험(`test_boundary_hard_lock_engine.py`)이 같이 쓴다.

- 시나리오: S6 재료(합성 병동 G1 RN 10명 + 상대 병동 G2)에 배정·상대 병동 근무표(같은 달·전월)·전월 근무표·확정 원티드를
  바꿔 끼운다. 프로덕션 경로를 L2 직전까지 돌려 L0(precheck 입력)·L1(엔진 입력)을 받는다(하네스 그대로 — solve 0).
- 위반 판정(`hard_violations`)은 엔진 코드를 쓰지 않는 독립 구현이다(CLAUDE.md 하드락 표 #1~#7). #6 은 '간호사 1인당 월'이라
  같은 달 바깥 근무(상대 병동)의 나이트도 센다.
- '경계만의 위반'(`boundary_only`): 바깥 근무(상대 병동·전월)와 이어 붙였을 때만 생기는 위반 — 이 병동 배정만으로는 위반이
  아니라서 엔진이 스스로 못 막고, 경계 칸(강제 OFF·금지칸·OFF 창)이나 개인 월 N 상한(#6)이 막아야 하는 것이다.
- 엔진 stage1(`run_stage1`): `CPSATBasicEngine.generate_roster` 를 부르고 `fallback_lex._solve_traced` 의 stage1 첫 풀이에서
  멈춘다(`test_precheck_vs_engine_stage1.py` 와 같은 기법). 강제 탐침(`probes`)은 그 모델의 사본에 `x == 1` 을 더해 풀이 가능 여부만
  본다 — '모델이 그 이어짐을 허용하는가'를 해 탐색 운에 기대지 않고 결정적으로 본다. 탐침은 최소 위반 이어짐 **전부**다(리뷰 —
  규칙당 하나만 보면 엔진이 L1 칸 일부(예: E 금지)만 버릴 때 운으로만 잡혔다).
- 사다리·탐색 시나리오(`ladder_boundary_scenarios`): 사다리 기록기(stop_at='ladder')로 첫 시도 뒤의 재시도·탐색까지 지나게 해,
  시도마다 엔진 입력에 L0 월중 경계 칸이 남는지 본다(리뷰 HIGH — 첫 시도만 보던 시험은 경로별 버림을 못 잡았다).
"""

from __future__ import annotations

import contextlib
import copy
import dataclasses
import io
import itertools
from datetime import date, datetime
from functools import partial

from .harness import run_with_output
from .scenario_defs import SCENARIOS, _assignment, _rows
from .scenario_ladder import LADDER_SCENARIOS, ladder_env, s1l_ward, s1lb_ward
from .ward import GROUP_ID, OFFICE_ID, OTHER_GROUP_ID, base_ward, nurse_row, seed_ward, stamp

YEAR, MONTH, NDAYS = 2026, 11, 30
OCT_DAYS = 31
CODES = ("D", "E", "N", "O")
WORK = frozenset({"D", "E", "N", "M", "W"})
BANNED_PAIRS = {("N", "D"): "ban_n_to_d", ("E", "D"): "ban_e_to_d", ("N", "E"): "ban_n_to_e"}

#: 설정 묶음 — X 기본 설정 행(3N 허용 · 3N 뒤 2OFF) · Y(+2N 뒤 2OFF) · Z(나이트 2연속 상한 + 회복 둘 다) · W(Y + 1N 금지 #7).
#: W 는 엔진 시험에만 쓴다 — 엔진은 경계 앞 날(차단일·전월)을 'N 아님'으로 보고 경계일 단독 N 을 이 병동 몫만으로 막는다.
#: 계약(L1 칸) 쪽 '이 병동만으로는 위반 아님' 판정은 경계 앞 날을 모름으로 두어 이 1N 을 경계 칸 몫으로 잘못 셀 수 있다.
CFG = {
    "X": {"two_offs_after_two_nig": False, "two_offs_after_three_nig": True},
    "Y": {"two_offs_after_two_nig": True, "two_offs_after_three_nig": True},
    "Z": {"allow_three_consecutive_nights": False, "two_offs_after_two_nig": True, "two_offs_after_three_nig": True},
    "W": {"two_offs_after_two_nig": True, "two_offs_after_three_nig": True, "ban_single_night": True},
}


class BoundaryGap(AssertionError):
    """경계 하드락 틈이 남았다 — 경계 칸이 위반 이어짐을 못 막거나(계약), stage1 배정에 위반이 있거나 모델이 위반 이어짐을 허용한다(엔진).

    알려진 틈의 `xfail(strict=True, raises=BoundaryGap)` 이 '틈 때문에' 실패할 때만 xfail 로 치게 한다(하네스 고장은 그대로 실패).
    """


def tail_cells(tail: str, last_day: int) -> dict[int, str]:
    """꼬리 문자열(마지막 글자가 `last_day` 일 · 1부터) → {일: 코드} — '-' 는 칸 없음."""
    start = last_day - len(tail) + 1
    return {start + i: c for i, c in enumerate(tail) if c != "-"}


#: 전월(10월) G1 근무표 꼬리(26~31일) — S7 과 같은 모양.
PREV_TAILS = {"N01": "DEOONN", "N02": "EONNNO", "N03": "DDEOON", "N04": "OODDEE", "N05": "NNOODD",
              "N06": "EEOODD", "N07": "DDOOEE", "N08": "ONNOOD", "N09": "DEEOOO", "N10": "OOEEDD"}
PREV_OCT = {nid: tail_cells(t, OCT_DAYS) for nid, t in PREV_TAILS.items()}


def prev_oct_away(nid: str) -> dict:
    """전월 G1 근무표 — `nid` 는 10월 20일부터 상대 병동에 가 있어 G1 칸이 19일(D)까지만 있다(파견 출발 전)."""
    return {**{k: v for k, v in PREV_OCT.items() if k != nid}, nid: {19: "D"}}


def d(month: int, day: int) -> date:
    return date(YEAR, month, day)


def x01_in(start_day: int, end_day: int, reason: str = "파견") -> dict:
    """X01(상대 병동 G2 소속)이 11월 start~end 에 G1 로 온다(D/E/N 가능)."""
    end = d(11, end_day) if end_day else None
    return _assignment("X01", OTHER_GROUP_ID, GROUP_ID, d(11, start_day), end, reason, target_grade=2,
                       target_shift_types=["D", "E", "N"], target_weekly_off_enabled=0)


def n07_out(start: date, end: date, **over) -> dict:
    """N07(G1 소속)이 start~end 에 G2 로 파견 간다(그다음 날 G1 복귀)."""
    return _assignment("N07", GROUP_ID, OTHER_GROUP_ID, start, end, "파견", target_grade=1,
                       target_shift_types=["D", "E", "N"], **over)


def transfer_out(nid: str, start: date) -> dict:
    """G1 간호사가 start 부터 G2 로 병동이동(돌아오지 않는다)."""
    return _assignment(nid, GROUP_ID, OTHER_GROUP_ID, start, None, "병동이동")


def _schedule_seed(sid: str, gid: str, month: int, status: str, cells: dict, prefix: str, version: int = 1):
    """근무표 1벌 — cells = {nurse_id: {일(1부터): 코드}}."""
    def seed(db):
        from db.models import Schedule, ScheduleEntry

        db.add(Schedule(schedule_id=sid, office_id=OFFICE_ID, group_id=gid, year=YEAR, month=month, version=version,
                        config_id=None, created_by="acc_HN01", status=status, dropped=False,
                        name=f"{gid} {month}월", **stamp()))
        rows = [(nid, day, code) for nid, days in cells.items() for day, code in sorted(days.items())]
        for i, (nid, day, code) in enumerate(rows):
            db.add(ScheduleEntry(entry_id=f"{prefix}{i:05d}", schedule_id=sid, nurse_id=nid,
                                 work_date=datetime(YEAR, month, day), shift_id="OFF" if code == "O" else code))
    return seed


def _fixed_wanted_rows(rows) -> list[dict]:
    base = {"group_id": GROUP_ID, "year": YEAR, "month": MONTH, **stamp()}
    return [{**base, "nurse_id": nid, "shift_date": d(MONTH, day), "shift_id": code, "shifts_table_id": None,
             "is_applied": True, "source_type": "original", "original_shift_id": code, "created_by": "HN01"}
            for nid, day, code in rows]


@dataclasses.dataclass
class BoundaryCase:
    """경계 시나리오 하나. `boundaries` = ((방향, 경계 색인), …) — 'after' 는 바깥 근무 뒤 이 병동이 이어 받는 첫날,
    'before' 는 이 병동 마지막 날 다음(상대 병동 첫날) 색인이다. 색인은 대상 달 0부터(전월은 음수).

    상대 병동 G2 근무표 상태: 11월 `g2_nov_status`(기본 작성 중) · 10월 `g2_oct_status`(기본 마감본). `g2_nov_issued` 가 있으면 G2 11월
    마감본(버전 1)을 한 벌 더 두고 `g2_nov` 는 그 뒤 버전 2(마감 뒤 다시 만든 작성 중)가 된다 — 같은 달 근무표 두 벌에서 최신 버전이
    아니라 마감본을 고르는지(EI-11 11w-2). 바깥 근무(`outside`)는 생성 경로가 고를 상대 근무표(마감본 우선) 칸이다."""

    name: str
    focus: str
    boundaries: tuple
    assignments: list = dataclasses.field(default_factory=list)
    g2_nov: dict = dataclasses.field(default_factory=dict)
    g2_oct: dict = dataclasses.field(default_factory=dict)
    g1_oct: dict | None = None
    config: dict = dataclasses.field(default_factory=dict)
    fixed_wanted: list = dataclasses.field(default_factory=list)
    g2_nurses: tuple = ("X01",)
    patches: tuple = ()
    g2_nov_status: str = "draft"
    g2_oct_status: str = "issued"
    g2_nov_issued: dict = dataclasses.field(default_factory=dict)

    def outside(self) -> dict[int, str]:
        """초점 간호사의 바깥 근무(이 생성이 바꿀 수 없는 칸) — 대상 달 색인 → 메인코드."""
        tl: dict[int, str] = {}
        for day, code in (self.g1_oct or {}).get(self.focus, {}).items():
            tl[day - 1 - OCT_DAYS] = main_code(code)
        for day, code in self.g2_oct.get(self.focus, {}).items():
            tl.setdefault(day - 1 - OCT_DAYS, main_code(code))
        for day, code in (self.g2_nov_issued or self.g2_nov).get(self.focus, {}).items():
            tl[day - 1] = main_code(code)
        return tl


def early_in(join_day: int, oct_tail: str, nov_tail: str, cfg: str, *, prev: bool = False) -> BoundaryCase:
    """X01 이 11월 `join_day` 일에 합류(파견 · 25일까지) — 상대 병동 꼬리가 10월 말(`oct_tail`)부터 11월 `join_day`-1 일
    (`nov_tail`)까지 이어진다. `join_day` 2~6 은 월초 근처 월중 경계(리뷰 G7), 1 은 월경계 보충 경로(`_inbound_source_map`)다."""
    nov = tail_cells(nov_tail, join_day - 1) if nov_tail else {}
    return BoundaryCase(name=f"in{join_day}-{oct_tail}|{nov_tail or '-'}-{cfg}{'-prev' if prev else ''}", focus="X01",
                        boundaries=(("after", join_day - 1),), assignments=[x01_in(join_day, 25)],
                        g2_oct={"X01": tail_cells(oct_tail, OCT_DAYS)}, g2_nov={"X01": nov} if nov else {},
                        g1_oct=PREV_OCT if prev else None, config=CFG[cfg])


def early_ret(ret_day: int, oct_tail: str, nov_tail: str, cfg: str, *, prev: bool = False) -> BoundaryCase:
    """N07 이 10월 20일부터 11월 `ret_day`-1 일까지 G2 파견 — `ret_day` 일 복귀(리뷰 G7). 상대 병동 꼬리 = 10월 말 + 11월."""
    return BoundaryCase(name=f"ret{ret_day}-{oct_tail}|{nov_tail}-{cfg}{'-prev' if prev else ''}", focus="N07",
                        boundaries=(("after", ret_day - 1),), assignments=[n07_out(d(10, 20), d(11, ret_day - 1))],
                        g2_oct={"N07": tail_cells(oct_tail, OCT_DAYS)}, g2_nov={"N07": tail_cells(nov_tail, ret_day - 1)},
                        g1_oct=prev_oct_away("N07") if prev else None, config=CFG[cfg], g2_nurses=())


#: 월초 근처 월중 경계(경계일 1~4 — 리뷰 G7): 상대 병동 꼬리가 전월에서 이어진다. X01 합류 · N07 복귀 × N·NN·NNN·DDDD 꼬리 × X·Y
#: (+ 전월 G1 근무표가 있는 달). 계약(L1 칸)·엔진 시험이 같이 쓴다.
EARLY = [
    early_in(2, "OOOOON", "N", "X"), early_in(2, "OOOOON", "N", "Y"), early_in(2, "OOOONN", "N", "X"),
    early_in(3, "OOOOON", "NN", "X"), early_in(3, "OOOODD", "DD", "X"), early_in(4, "OOOOOD", "DDD", "Y"),
    early_in(2, "OOOOON", "N", "Y", prev=True), early_in(3, "OOOODD", "DD", "X", prev=True),
    early_ret(2, "OOOOON", "N", "Y"), early_ret(2, "OOODDD", "D", "X"), early_ret(3, "OOOONN", "NO", "X"),
    early_ret(3, "OOOOON", "NN", "X"), early_ret(2, "OOOOON", "N", "X", prev=True),
    # 경계일 4 — 전월 마지막 하루까지 이어져 연속근무 5 가 된다(같은 달만 보면 4 라 경계일 근무를 허용했다)
    early_in(5, "OOOOOD", "DDDD", "X"), early_ret(5, "OOOOOD", "DDDD", "Y"),
]
#: 월초(1일) 합류 — 이 병동 전월 근무표에 없는 간호사의 꼬리를 월경계가 상대 병동 전월 근무표로 보충하는 경로(`_inbound_source_map`
#: — 리뷰 MEDIUM). 엔진은 월경계 N 상한·회복을 `prev_month_*` 로 걸어 칸만 봐서는 안 되므로 계약은 이월 값을, 엔진 시험은 모델을 본다.
MONTH_START_IN = [early_in(1, t, "", c) for t, c in (("OOOOON", "X"), ("OOOONN", "X"), ("OOONNN", "X"), ("ODDDDD", "X"),
                                                     ("OOOONN", "Y"))]
MONTH_START_IN += [early_in(1, "OOOONN", "", "X", prev=True), early_in(1, "ODDDDD", "", "Y", prev=True)]


def return_day1(tail: str, cfg: str, *, g2_prev: bool = True, own_prev: bool = True, own: str = "") -> BoundaryCase:
    """N07 이 10월 G2 파견을 10월 31일에 마치고 11월 1일 G1 으로 돌아온다(1일 복귀 — EI-11 11w-1 · G4 · D35). 상대 병동 10월
    꼬리 `tail`(31일에 끝남)이 이 병동 1일로 이어진다.

    `own` 이 비면 파견은 10월 20일부터(이 병동 10월 칸은 19일 D 까지), 있으면 파견은 `tail` 첫날부터이고 그 앞 날들이 이 병동 칸
    `own` 이다(꼬리가 이 병동 칸 + 상대 병동 칸으로 이어진다). `g2_prev=False` 면 상대 병동 10월 근무표가 없고(꼬리 없음 — D35),
    `own_prev=False` 면 이 병동 10월 근무표가 없다."""
    start = OCT_DAYS - len(tail) + 1 if own else 20
    if not own_prev:
        g1 = None
    elif own:
        g1 = {**{k: v for k, v in PREV_OCT.items() if k != "N07"}, "N07": tail_cells(own, start - 1)}
    else:
        g1 = prev_oct_away("N07")
    name = f"ret1-{own + '|' if own else ''}{tail}-{cfg}{'' if g2_prev else '-noG2prev'}{'' if own_prev else '-noOwnPrev'}"
    return BoundaryCase(name=name, focus="N07", boundaries=(("after", 0),),
                        assignments=[n07_out(d(10, start), d(10, OCT_DAYS), status="completed")],
                        g2_oct={"N07": tail_cells(tail, OCT_DAYS)} if g2_prev else {}, g1_oct=g1,
                        config=CFG[cfg], g2_nurses=())


def _g1_oct_until(case: BoundaryCase, last_day: int) -> BoundaryCase:
    """이 병동 10월 근무표 칸이 모든 간호사에게 `last_day` 일까지만 있는 달 — 그 근무표의 꼬리 날짜가 말일보다 일찍 끝난다."""
    g1 = {nid: {day: c for day, c in cells.items() if day <= last_day} for nid, cells in (case.g1_oct or {}).items()}
    return dataclasses.replace(case, name=f"{case.name}-g1until{last_day}", g1_oct=g1)


#: 1일 복귀(G4 · D35 — 11q 의 `G4-return-monthstart` 와 함께): 상대 병동 전월 꼬리 × 설정 · 이 병동 전월 근무표 없음 · 꼬리가 이 병동
#: 칸과 상대 병동 칸으로 이어지는 달(파견이 꼬리 안에서 시작 — 이 병동 칸을 날짜로 맞추는가 · 이 병동 근무표 칸이 28일에 끝나도
#: 꼬리는 전월 말일까지 잇는가).
RETURN_DAY1 = [return_day1(t, c) for t, c in (("ODDDDD", "X"), ("OOONNN", "X"), ("OOOODE", "Y"))]
RETURN_DAY1 += [return_day1("OOOONN", "Y", own_prev=False), return_day1("DDD", "X", own="ODD"),
                return_day1("N", "Y", own="OOOON"), _g1_oct_until(return_day1("DDD", "X", own="ODD"), 28)]

def depart_n07(head: str, cfg: str, *, end_day: int = 14, after: bool = False, **over) -> BoundaryCase:
    """N07 이 11월 10일부터 `end_day` 일까지 G2 파견 — G2 11월 근무표가 먼저 있고 N07 의 첫 칸(10일부터)이 `head`('-' 는 칸 없음)다
    (출발 경계 — 이 병동 마지막 날은 9일 · EI-11 11w-2 · G3 · D34). `after` 면 복귀 경계(`end_day` 다음 날)도 본다."""
    cells = {10 + i: c for i, c in enumerate(head) if c != "-"}
    bounds = (("before", 9),) + ((("after", end_day),) if after else ())
    return BoundaryCase(name=f"dep-{head}-{cfg}{'' if end_day == 14 else f'-to{end_day}'}", focus="N07", boundaries=bounds,
                        assignments=[n07_out(d(11, 10), d(11, end_day))], g2_nov={"N07": cells}, config=CFG[cfg],
                        g2_nurses=(), **over)


def depart_in_end(head: str, cfg: str) -> BoundaryCase:
    """파견 온 X01(11/20~25)이 26일 원 병동 G2 로 돌아간다 — G2 11월 근무표의 X01 26일부터 칸이 `head`(출발 경계 · 11w-2).
    합류 앞 G2 꼬리는 OFF 라 합류 경계 칸은 없다."""
    cells = {**tail_cells("OOOOOO", 19), **{26 + i: c for i, c in enumerate(head) if c != "-"}}
    return BoundaryCase(name=f"dep-inEnd-{head}-{cfg}", focus="X01", boundaries=(("before", 25),),
                        assignments=[x01_in(20, 25)], g2_nov={"X01": cells}, config=CFG[cfg])


def transfer_out_n08(start_day: int, head: str, cfg: str) -> BoundaryCase:
    """N08 이 11월 `start_day` 일부터 G2 로 병동이동 — G2 11월 근무표의 N08 첫 칸이 `head`(출발 경계 · 11w-2)."""
    cells = {start_day + i: c for i, c in enumerate(head) if c != "-"}
    return BoundaryCase(name=f"tout{start_day}-{head}-{cfg}", focus="N08", boundaries=(("before", start_day - 1),),
                        assignments=[transfer_out("N08", d(11, start_day))], g2_nov={"N08": cells}, config=CFG[cfg],
                        g2_nurses=())


#: 출발 경계(G3 · 11w-2) — 파견 출발 × 상대 첫 칸(전이·연속근무·N 이어 붙이기·회복은 엔진 몫) · 짧은 파견 · 상대 근무가 K 를 넘음
#: · 병동이동(월말 근처 포함) · 파견 온 간호사의 원 병동 복귀. 엔진 시험·계약 시험이 같이 쓴다(계약은 #1·#2·#3 이 있는 것만).
DEPARTURES = [
    depart_n07("NNOOD", "Y"), depart_n07("EEOOD", "X"), depart_n07("ODDOO", "Y"), depart_n07("NNOOD", "X"),
    depart_n07("DD", "X", end_day=11, after=True), depart_n07("DDDDD", "X", end_day=15),
    depart_n07("DDDDDD", "X", end_day=15), transfer_out_n08(28, "DDD", "X"), depart_in_end("DDDOO", "Y"),
]

#: 하드락 #6 재료 — X01 이 상대 병동에서 11월 1~19일에 나이트 12개(3N 뒤 2OFF 지킴)를 서고 20~30일 G1 로 온다.
NIGHT_CAP_G2 = "NNNOONNNOONNNOONNNO"


def night_cap_case(cfg: str = "X") -> BoundaryCase:
    """#6 '간호사 1인당 월 나이트' — 같은 달 상대 병동 나이트를 이 병동 몫에서 빼야 한다(리뷰 MEDIUM)."""
    return BoundaryCase(name=f"n6-in-G2N{NIGHT_CAP_G2.count('N')}-{cfg}", focus="X01", boundaries=(("after", 19),),
                        assignments=[x01_in(20, 30)], g2_nov={"X01": tail_cells(NIGHT_CAP_G2, 19)}, config=CFG[cfg])


def build_ward(case: BoundaryCase):
    ward = base_ward(10)
    ward.other_groups = [OTHER_GROUP_ID]
    others = [nurse_row(nid, 20 + i, group_id=OTHER_GROUP_ID) for i, nid in enumerate(case.g2_nurses)]
    ward.extras = [_rows("Nurse", [{**r, **stamp()} for r in others]),
                   _rows("NurseAssignment", case.assignments)]
    if case.g2_nov:
        ward.extras.append(_schedule_seed("g2nov0000001", OTHER_GROUP_ID, 11, case.g2_nov_status, case.g2_nov, "g2n",
                                          version=2 if case.g2_nov_issued else 1))
    if case.g2_nov_issued:
        ward.extras.append(_schedule_seed("g2nov0000002", OTHER_GROUP_ID, 11, "issued", case.g2_nov_issued, "g2i"))
    if case.g2_oct:
        ward.extras.append(_schedule_seed("g2oct0000001", OTHER_GROUP_ID, 10, case.g2_oct_status, case.g2_oct, "g2o"))
    if case.g1_oct is not None:
        ward.extras.append(_schedule_seed("g1oct0000001", GROUP_ID, 10, "issued", case.g1_oct, "g1o"))
    if case.fixed_wanted:
        ward.config.update(apply_confirmed_wanted=True)
        ward.extras.append(_rows("FixedWantedEntry", _fixed_wanted_rows(case.fixed_wanted)))
    ward.config.update(case.config)
    return ward


def scenario_for(case: BoundaryCase):
    base = SCENARIOS["S6"]
    return dataclasses.replace(base, name=f"BND-{case.name}", seed=partial(seed_ward, build_ward(case)),
                               patches=base.patches + tuple(case.patches))


def run_to_l1(engine, case: BoundaryCase):
    """프로덕션 경로를 L2 직전까지 — (L0 첫 시도, L1 첫 시도, 실행 결과)."""
    rec = run_with_output(engine, scenario_for(case)).result
    l0 = rec.layers["L0"][0] if rec.layers.get("L0") else None
    l1 = rec.layers["L1"][0] if rec.layers.get("L1") else None
    return l0, l1, rec


# ── 사다리·탐색 시나리오(리뷰 HIGH — 시도마다 월중 경계 칸) ─────────────────────────


@dataclasses.dataclass(frozen=True)
class LadderBoundary:
    """사다리 기록기로 돌리는 경계 시나리오 — 처음 지나는 지점 순서(`sites`)와 경계 간호사(`focus`)."""

    scenario: object
    sites: tuple[str, ...]
    focus: tuple[str, ...]


def _s1l_with_return(make_ward=s1l_ward):
    """S1-L 재료(주말휴무 3·월N 상한 8 — config_override 없는 탐색 3곳까지 닿는다) + N07 파견 11/9~11(평일) · 상대 꼬리 OONNN.

    합류(새 간호사)를 더하면 원인 진단이 '정책 과제약'(lagrangian :290) 앞에서 λ 단계로 넘어가 하네스 solve 차단에 끝난다 —
    복귀만 둔다(명단 수 그대로). 파견을 평일에 둬 주말 머릿수 조건(scenario_ladder.py ②)을 지킨다."""
    ward = make_ward()
    ward.other_groups = [OTHER_GROUP_ID]
    # EI-10g5 — 복귀 뒤 강제 OFF(상대 꼬리 OONNN → 복귀 직후 이틀 OFF)가 주말에 걸리면 이 병동은(주말에도 하루 9명이 정확히 가용) 일자 용량 부족으로
    #   사전 점검에 막힌다 — 이 시험이 보려는 것은 사다리가 월중 경계 칸을 시도마다 지키는지라 엔진까지 닿아야 한다. 파견을 11/9~11 로 앞당겨
    #   강제 OFF 가 평일(수·목)에 떨어지게 한다(그 날 가용 11 ≥ 9).
    ward.extras += [_rows("NurseAssignment", [n07_out(d(11, 9), d(11, 11))]),
                    _schedule_seed("g2nov0000001", OTHER_GROUP_ID, 11, "draft", {"N07": tail_cells("OONNN", 11)}, "g2n")]
    return ward


def ladder_boundary_scenarios() -> dict[str, LadderBoundary]:
    """이름 → 사다리 경계 시나리오. S1-L 재료는 사다리 4단 + 탐색 전부(스냅숏 기준 MUS·UNDIAG · config_override 없는 주말휴무
    다인·단일·야간한도 개인)를, S1-LB 재료는 거기에 사다리 4차(`_ladder_retry` — 하드 복구·누적 해제 두 번)를, S6 재료(합류 + 복귀)는
    사다리 4단 + MUS 진단까지 지난다(그 뒤 원인 진단의 λ 단계에서 하네스가 멈춘다). 엔진을 부르는 12곳 중 A1 탐색(성공 경로 + 위반
    수가 있어야 한다 — test_ladder.py 머리말)만 빠진다."""
    s1l, s1lb = LADDER_SCENARIOS["S1-L"], LADDER_SCENARIOS["S1-LB"]
    s6 = BoundaryCase(name="ladder-in+ret-Y", focus="X01", boundaries=(("after", 19),),
                      assignments=[x01_in(20, 25), n07_out(d(11, 10), d(11, 14))],
                      g2_nov={"X01": tail_cells("OOOONN", 19), "N07": tail_cells("OOONN", 14)}, config=CFG["Y"])
    s6_scen = scenario_for(s6)
    return {
        "S1L-ret": LadderBoundary(
            scenario=dataclasses.replace(s1l, name="S1L-BND-ret", seed=partial(seed_ward, _s1l_with_return())),
            sites=("first_attempt", "team_min_soft", "same_shift_soft", "isolated_work_soft", "n2n_min_gap_release",
                   "mus_diag", "undiag_probe", "weekend_multi", "weekend_single", "night_limit_single"),
            focus=("N07",)),
        # S1-LB 재료 — 사다리 4차 '전날 N 금지 최소화'(`_ladder_retry` 헬퍼 — 호출 지점 ban_night_minimize_retry)까지 지난다.
        "S1LB-ret": LadderBoundary(
            scenario=dataclasses.replace(s1lb, name="S1LB-BND-ret", seed=partial(seed_ward, _s1l_with_return(s1lb_ward))),
            sites=("first_attempt", "team_min_soft", "same_shift_soft", "isolated_work_soft", "ban_night_minimize_retry",
                   "n2n_min_gap_release", "mus_diag", "undiag_probe", "weekend_multi", "weekend_single",
                   "night_limit_single"),
            focus=("N07",)),
        "S6-in+ret": LadderBoundary(
            scenario=dataclasses.replace(s6_scen, stop_at="ladder", patches=s6_scen.patches + (ladder_env,)),
            sites=("first_attempt", "team_min_soft", "same_shift_soft", "isolated_work_soft", "mus_diag"),
            focus=("X01", "N07")),
    }


def attempt_configs(layers: dict) -> list[tuple[str, dict]]:
    """사다리 기록 층의 시도마다 (지점, 그 시도의 엔진 config) — 첫 시도 L1 에 시도 차이(`delta.config_data`)를 얹어 되살린다."""
    first = layers["L1"][0]["config_data"]
    out = []
    for att in layers.get("attempts") or []:
        delta = (att.get("delta") or {}).get("config_data") or {"changed": {}, "removed": []}
        cfg = {k: v for k, v in first.items() if k not in set(delta.get("removed") or [])}
        cfg.update(delta.get("changed") or {})
        out.append((att["site"], cfg))
    return out


def first_seen(sites) -> tuple[str, ...]:
    seen: list[str] = []
    for site in sites:
        if site not in seen:
            seen.append(site)
    return tuple(seen)


# ── 독립 위반 판정(CLAUDE.md 하드락 표 — 엔진 코드를 쓰지 않는다) ─────────────────────────


def main_code(code) -> str:
    c = str(code or "").strip().upper()
    if c in ("", "-"):
        return "-"
    if c in ("O", "OFF", "주", "연", "보", "수"):
        return "O"
    return c if c in ("D", "E", "N", "M") else "W"


def _runs(seq: list[str], members) -> list[tuple[int, int]]:
    out, i = [], 0
    while i < len(seq):
        if seq[i] not in members:
            i += 1
            continue
        j = i
        while j < len(seq) and seq[j] in members:
            j += 1
        out.append((i, j))
        i = j
    return out


def _rules(cfg: dict) -> dict:
    ban = bool(cfg.get("ban_shift_transitions"))
    return {"K": int(cfg.get("max_conseq_work") or 5), "L": 3 if cfg.get("allow_three_consecutive_nights") else 2,
            "bans": {pair for pair, key in BANNED_PAIRS.items() if ban and cfg.get(key, True)},
            "two3": bool(cfg.get("two_offs_after_three_nig")), "two2": bool(cfg.get("two_offs_after_two_nig")),
            "one": bool(cfg.get("ban_single_night")), "n_cap": int(cfg.get("max_nig_per_month") or 15)}


def hard_violations(outside: dict[int, str], mine: dict[int, str], cfg: dict) -> list[str]:
    """바깥 근무 + 이 병동 배정을 이어 붙인 하드락 위반 — 이 병동이 배정한 날이 하나라도 걸린 것만.

    '-'(모름·칸 없음)는 연속을 끊고, 회복(#4·#5)은 뒤 두 칸을 다 알 때만, 1N(#7)은 그 N 이 이 병동 배정이고 양옆을 알 때만 본다.
    #6 은 간호사 1인당 월이라 같은 달 바깥 나이트(상대 병동)도 더한다 — 이 병동이 그 달 나이트를 하나라도 배정했을 때만.
    """
    mine = {k: v for k, v in mine.items() if v != "-"}
    tl = {**outside, **mine}
    if not mine:
        return []
    lo, hi = min(tl), max(tl)
    idx = list(range(lo, hi + 1))
    seq = [tl.get(i, "-") for i in idx]
    r = _rules(cfg)

    def hit(a: int, b: int) -> bool:
        return any(idx[i] in mine for i in range(max(a, 0), min(b, len(seq) - 1) + 1))

    out = [f"#1 연속근무{j - i}>{r['K']} @{idx[i]}" for i, j in _runs(seq, WORK) if j - i > r["K"] and hit(i, j - 1)]
    out += [f"#2 {seq[i - 1]}->{seq[i]} @{idx[i]}" for i in range(1, len(seq))
            if (seq[i - 1], seq[i]) in r["bans"] and hit(i - 1, i)]
    for i, j in _runs(seq, {"N"}):
        n, nxt = j - i, seq[j:j + 2]
        if n > r["L"] and hit(i, j - 1):
            out.append(f"#3 N{n}>{r['L']} @{idx[i]}")
        rule = "#4" if (n >= 3 and r["two3"]) else ("#5" if (n >= 2 and r["two2"]) else None)
        if rule and len(nxt) == 2 and "-" not in nxt and nxt != ["O", "O"] and hit(i, j + 1):
            out.append(f"{rule} N{n} 뒤 {''.join(nxt)} @{idx[j]}")
        if r["one"] and n == 1 and idx[i] in mine and i > 0 and j < len(seq) and "-" not in (seq[i - 1], seq[j]):
            out.append(f"#7 1N @{idx[i]}")
    month_n = sum(1 for k, v in tl.items() if 0 <= k < NDAYS and v == "N")
    if any(0 <= k < NDAYS and v == "N" for k, v in mine.items()) and month_n > r["n_cap"]:
        out.append(f"#6 월N {month_n}>{r['n_cap']}")
    return out


def _span(direction: str, day: int, length: int) -> list[int]:
    return list(range(day, day + length)) if direction == "after" else list(range(day - length, day))


def boundary_only(case: BoundaryCase, cfg: dict, max_len: int = 6, active: set[int] | None = None) -> list[dict[int, str]]:
    """경계만의 위반 이어짐 전부(짧은 것부터) — 바깥 근무와 이어 붙이면 위반이고 이 병동 배정만으로는 위반이 아닌 것.

    `active` 를 주면 그 날(이 병동이 배정하는 날) 안의 이어짐만 본다.
    """
    outside = case.outside()
    found: list[dict[int, str]] = []
    for direction, day in case.boundaries:
        for length in range(1, max_len + 1):
            days = _span(direction, day, length)
            if active is not None and not set(days) <= active:
                break
            for codes in itertools.product(CODES, repeat=length):
                cont = dict(zip(days, codes, strict=True))
                if hard_violations(outside, cont, cfg) and not hard_violations({}, cont, cfg):
                    found.append(cont)
    return found


def minimal(conts: list[dict[int, str]]) -> list[dict[int, str]]:
    """더 짧은 위반 이어짐을 품은 것을 뺀다(짧은 것부터 들어온다고 본다) — 경계 칸이 짧은 것을 막으면 긴 것도 막힌다."""
    keep: list[dict[int, str]] = []
    for cont in conts:
        if not any(all(cont.get(k) == v for k, v in short.items()) for short in keep):
            keep.append(cont)
    return keep


@dataclasses.dataclass(frozen=True)
class Cells:
    """엔진 입력(L1)의 간호사 경계 칸 — 강제 OFF 날 · {날: 금지 코드} · OFF 창 · 차단일 · 개인 월 N 상한(#6)."""

    forced: frozenset
    forbidden: dict
    windows: tuple
    blocked: frozenset
    n_max: int | None


def l1_cells(l1: dict, nid: str) -> Cells:
    """L1(엔진 입력 전체)에서 간호사 `nid` 의 경계 칸을 꺼낸다."""
    cfg = l1["config_data"]
    ic = cfg.get("initial_constraints") if isinstance(cfg.get("initial_constraints"), dict) else {}
    forced = frozenset(int(x) for x in ((ic.get("forced_off") or {}).get(nid) or ()))
    forb = {int(k): {str(c) for c in v or ()} for k, v in ((ic.get("forbidden") or {}).get(nid) or {}).items()}
    ow = cfg.get("off_window_constraints") if isinstance(cfg.get("off_window_constraints"), dict) else {}
    windows = tuple((int(w[0]), int(w[1])) for w in (ow.get(nid) or ()))
    blocked = frozenset(int(x) for x in ((cfg.get("blocked_by_nurse_id") or {}).get(nid) or ()))
    nurse = next((n for n in l1.get("nurses_data") or () if str(n.get("nurse_id")) == nid), {})
    n_max = nurse.get("n_max")
    return Cells(forced, forb, windows, blocked, None if n_max is None else int(n_max))


def excluded(cont: dict[int, str], cells: Cells) -> bool:
    """경계 칸이 그 이어짐(다른 날은 자유)을 막는가 — 강제 OFF 날의 비-O · 금지 코드 · OFF 창 · 개인 월 N 상한.

    OFF 창은 엔진과 같게 읽는다(`fallback_lex` OFF 창 — 차단일은 창에서 빼고, 남는 날이 없으면 그 창은 아무것도 요구하지 않는다).
    예전엔 차단일을 몰라 '창 안 차단일에 OFF' 를 허용으로 셌다(리뷰 LOW — 엔진은 막는데 계약은 틈으로 본 오탐).
    """
    if any(c != "O" and day in cells.forced for day, c in cont.items()):
        return True
    if any(c in cells.forbidden.get(day, ()) for day, c in cont.items()):
        return True
    if cells.n_max is not None and sum(1 for c in cont.values() if c == "N") > cells.n_max:
        return True
    for a, b in cells.windows:
        free = [x for x in range(a, b + 1) if x not in cells.blocked]
        if free and all(cont.get(x, "O") != "O" for x in free):
            return True
    return False


# ── 엔진 stage1 ──────────────────────────────────────────────────────────


class _Stage1Stop(BaseException):
    """stage1 첫 풀이 뒤 엔진을 끊는 표지(`Exception` 이 아니라 엔진의 광범위 except 에 안 잡힌다)."""


@dataclasses.dataclass
class Stage1:
    status: str
    rows: dict[str, dict[int, str]]
    probes: list[str]


def _x_index(model) -> dict[tuple[int, int, int], int]:
    out = {}
    for i, var in enumerate(model.proto.variables):
        parts = var.name.split("_")
        if len(parts) == 4 and parts[0] == "x":
            out[(int(parts[1]), int(parts[2]), int(parts[3]))] = i
    return out


def _probe(model, xi, shift_types, nurse_idx, force: dict[str, dict[int, str]], time_limit: float) -> str:
    from ortools.sat.python import cp_model

    probe_model = model.clone()
    for nid, cells in force.items():
        for day, code in cells.items():
            key = (nurse_idx[nid], day, shift_types.index(code))
            assert key in xi, f"강제 칸에 변수가 없다(배정 차단일·활동일 밖) — {nid} {day} {code}"
            probe_model.Add(probe_model.get_int_var_from_proto_index(xi[key]) == 1)
    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = time_limit
    solver.parameters.num_search_workers = 8
    solver.parameters.stop_after_first_solution = True
    return solver.StatusName(solver.Solve(probe_model))


def run_stage1(monkeypatch, l1: dict, probes=(), *, time_limit: float = 20.0) -> Stage1:
    """L1 을 실제 엔진에 넣어 stage1 첫 풀이의 상태·배정(첫 해)과 강제 탐침 상태를 받는다(엔진 7파일 무수정 — 감싸기만)."""
    from services.cp_sat import fallback_lex
    from services.cp_sat_basic import CPSATBasicEngine

    holder: dict = {}
    original_cfg, original_solve = CPSATBasicEngine.create_config_from_db, fallback_lex._solve_traced
    ids = [str(n.get("nurse_id")) for n in l1["nurses_data"]]
    nurse_idx = {nid: i for i, nid in enumerate(ids)}

    def cfg_spy(self, config_data):
        cfg = original_cfg(self, config_data)
        holder["shift_types"] = list(cfg.shift_types)
        return cfg

    def solve_spy(solver, model, prefix, phase):
        assert str(phase).startswith("stage1["), f"stage1 전에 다른 풀이가 왔다(phase={phase})"
        xi, shift_types = _x_index(model), holder["shift_types"]
        holder["probes"] = [_probe(model, xi, shift_types, nurse_idx, force, time_limit) for force in probes]
        solver.parameters.max_time_in_seconds = time_limit
        solver.parameters.stop_after_first_solution = True
        status = original_solve(solver, model, prefix, phase)
        holder["status"] = fallback_lex._cp_sat_status_to_text(status)
        if holder["status"] in ("OPTIMAL", "FEASIBLE"):
            rows: dict[str, dict[int, str]] = {nid: {} for nid in ids}
            for (n, day, s), i in xi.items():
                if solver.Value(model.get_int_var_from_proto_index(i)):
                    rows[ids[n]][day] = main_code(shift_types[s])
            holder["rows"] = rows
        raise _Stage1Stop

    monkeypatch.setattr(CPSATBasicEngine, "create_config_from_db", cfg_spy)
    monkeypatch.setattr(fallback_lex, "_solve_traced", solve_spy)
    monkeypatch.delenv("AIDE_ENABLE_MUS_REGISTRY", raising=False)
    monkeypatch.delenv("AIDE_MAX_CONSEC_WORK", raising=False)
    with contextlib.redirect_stdout(io.StringIO()), contextlib.suppress(_Stage1Stop):
        CPSATBasicEngine().generate_roster(
            copy.deepcopy(l1["nurses_data"]), copy.deepcopy(l1.get("prefs_data") or []),
            copy.deepcopy(l1["config_data"]), l1["year"], l1["month"], copy.deepcopy(l1["shift_manage_data"]),
            grade_strategy=l1.get("grade_strategy") or "COMBINED", grade_config=copy.deepcopy(l1.get("grade_config")),
            time_limit_seconds=60, randomize=False, seed=1)
    assert "status" in holder, "엔진이 stage1 풀이에 닿지 못했다(엔진 사전 검사에서 끝났다)"
    return Stage1(status=holder["status"], rows=holder.get("rows") or {}, probes=holder["probes"])
