"""'불가'의 이유 — 같은 칸 수 상한 안에서 무엇을 얼마나 넘겨야 가능한지 한 번 풀어서 알려 준다.

규칙을 다 지키며 상한 칸 이내로는 해가 없다는 게 증명된 뒤에만 부른다(solver 의 한 달 단계).
하드락은 그대로 지키고, 나머지 규칙(인원·등급·OFF 수·개인 한도·N 간격 등)만 '넘겨도 되지만 넘긴 만큼
벌점'으로 두고 `넘김 × 1000 + 바뀐 칸` 을 최소화한다(최소 수정 집합 MCS 를 최적화로 근사 ·
Senthooran CP 2021 의 '사람이 받아들일 복구' 와 같은 방향).
  · 넘김 > 0 → '이 설정을 이만큼 넘기면 가능' — **이유로만** 쓰고 후보로 내지 않는다(하드락 정책 · 결정 ③).
  · 해 없음  → 넘길 수 있는 걸 다 넘겨도 하드락에 막힌다.

★ 칸 수 상한을 풀면 안 된다. 실측(dev 72건): 상한 없는 넘김 모델은 8초에 수렴하지 못해 '139칸 필요'
  같은 증명 안 된 숫자를 냈다. 상한 20칸을 걸면 같은 요청이 대부분 1초 안에 최적·불가로 끝난다.
★ 처음엔 규칙 종류마다 가정 리터럴을 두고 core 를 줄였는데, 실측(7B 11월)에서 core 가 거의 모든
  규칙이었고 줄이는 데 23초가 걸렸다(규칙 하나를 빼면 탐색이 커져 불가 증명이 시간 안에 안 남).
★ OR-Tools 9.15 `repair_hint=True` 는 다중 작업자에서 프로세스를 죽인다(MinimizeL1DistanceWithHint
  check 실패) — 쓰지 않는다.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from ortools.sat.python import cp_model

from services.schedule_edit.context import EditContext
from services.schedule_edit.model import (
    EditModel, add_cell_vars, add_requests, add_team_cover, edit_codes, free_cells, is_waived, model_relief,
    new_solver, read_solution, requested_cells,
)
from services.schedule_edit.requests import BalanceTarget, CountTarget, CoverTarget, EditRequest, SetCell
from services.schedule_edit.rules import WISH, Grid, RuleSet, cell, value_of

#: 넘김 1 의 무게 — 칸 하나 더 바꾸는 것보다 훨씬 무겁게(넘김을 먼저 줄인다)
BEND_WEIGHT = 1000

#: CLAUDE.md 하드락 — 이 풀이에서도 넘기지 않는다
HARD_LOCK_FAMILIES = {"연속근무", "전환금지", "연속나이트", "3N후OFF", "2N후OFF", "2N후OFF(화면)",
                      "월나이트", "1N"}

FAMILY_LABEL = {
    "연속근무": "연속 근무일 상한", "전환금지": "N→D·E→D·N→E 같은 전환 금지", "연속나이트": "나이트 연속 상한",
    "3N후OFF": "나이트 3일 뒤 OFF 2일", "2N후OFF": "나이트 2일 뒤 OFF 2일",
    "2N후OFF(화면)": "나이트 2일 뒤 OFF 2일(화면 기준)", "월나이트": "월 나이트 상한",
    "1N": "나이트 단독(1N) 금지", "N간격": "나이트 블록 간 최소 간격", "같은근무연속": "같은 근무 연속 상한",
    "고립근무": "OFF 사이 하루 근무 금지", "금지근무": "허용 근무형·기피 근무", "휴무전날N": "휴가 전날 나이트 금지",
    "주말휴무": "주말휴무 대상자 규칙", "개인한도": "개인 월 근무 한도", "OFF수": "개인 OFF 수",
    "인원최소": "일별 최소 인원", "인원최대": "일별 최대 인원", "등급최소": "등급별 최소 인원",
    "상호배제": "같은 근무 피하기 짝", "원티드": "간호사 원티드(희망) 반영 칸",
}

#: 전환금지는 쌍마다 문구가 다르다(rules `_transition_pairs`) — MID 두 쌍은 use_mid 병동에서만 생긴다
TRANSITION_LABEL = {
    "ND": "N→D 전환 금지", "ED": "E→D 전환 금지", "NE": "N→E 전환 금지",
    "xM": "M 전날 E·N 금지(M 은 전날 D·OFF·M 일 때만)", "MD": "M→D 전환 금지",
}


def family_label(key: Tuple) -> str:
    """규칙 위치 key → 사람이 읽는 이름. 전환금지는 어느 쌍인지까지."""
    if key[0] == "전환금지" and len(key) > 3 and key[3] in TRANSITION_LABEL:
        return TRANSITION_LABEL[key[3]]
    return FAMILY_LABEL.get(key[0], key[0])


@dataclass
class Bend:
    """넘겨야 하는 규칙 하나 — 위치(key)와 넘김 양."""

    family: str
    key: Tuple
    amount: int


@dataclass
class Explanation:
    status: str = "unknown"          # bends · hard_lock · found · unknown
    bends: List[Bend] = field(default_factory=list)
    #: 넘김 없이 해가 나온 경우(앞 단계가 시간 안에 못 찾은 것) — 규칙을 다 지키는 해
    grid: Optional[Grid] = None
    proven: bool = False

    @property
    def families(self) -> List[str]:
        return sorted({b.family for b in self.bends})

    def sentences(self, names: Dict[str, str], max_changes: int, list_bends: bool = True) -> List[str]:
        """list_bends=False: 넘길 설정을 레버 카드가 하나씩 보여 줄 때 — 같은 묶음을 문장으로 또 늘어놓지 않는다(ui-craft 11-03)."""
        if self.status == "hard_lock":
            return [f"다른 설정을 다 넘겨도 하드락 규칙(연속 근무·전환 금지·나이트 규칙 등)에 막힙니다(바꾸는 칸 "
                    f"{max_changes}개까지 살펴봄). 하드락은 풀 수 없습니다."]
        if self.status == "bends":
            # 한 해가 함께 넘긴 묶음이다 — 줄마다 '이걸 넘기면 가능' 이라 쓰면 하나만 넘겨도 되는 것처럼 읽힌다
            head = [] if self.proven else ["(시간 안에 더 나은 방법이 없는지 끝까지 확인하지는 못했습니다)"]
            more = f" 외 {len(self.bends) - 5}곳" if len(self.bends) > 5 else ""
            together = "을 함께" if len(self.bends) > 1 else "을"
            wish = [b for b in self.bends if b.family == WISH]
            tail = [f"원티드가 반영된 칸({', '.join(_wish_cell(b, names) for b in wish[:3])})은 바꾸지 않았습니다. "
                    "바꿔도 되면 그 칸을 직접 말씀해 주세요."] if wish else []
            listed = [f"다음 설정{together} 이번만 넘기면 가능합니다: "
                      + " · ".join(bend_text(b, names) for b in self.bends[:5]) + more] if list_bends else []
            return head + listed + tail
        return ["시간 안에 이유를 좁히지 못했습니다. 요청을 나눠서 다시 시도해 주세요."]


def _wish_cell(bend: Bend, names: Dict[str, str]) -> str:
    _fam, nurse, day = bend.key[:3]
    return f"{names.get(nurse, nurse)} {day + 1}일"


def explain_month(ctx: EditContext, rules: RuleSet, requests: Sequence[EditRequest],
                  max_changes: int, seconds: float, days: Sequence[int],
                  allow: Optional[Dict[Tuple, int]] = None) -> Explanation:
    """규칙을 다 지키며 max_changes 칸 이내로는 안 될 때 — 같은 범위·상한 안에서 무엇을 넘겨야 하는지.

    days 는 후보 탐색과 같은 '바꿀 수 있는 날'(요청한 가장 이른 날부터 말일)이다.
    """
    em = EditModel(ctx=ctx, base=ctx.main, free=free_cells(ctx, days), codes=edit_codes(ctx))
    add_cell_vars(em)
    slacks = add_rules_soft(em, rules, requested_cells(ctx, list(requests)), allow or {},
                            relief=model_relief(em, rules, list(requests)))
    add_requests(em, requests)
    em.model.Add(em.total_changes() <= max_changes)
    em.model.Minimize(BEND_WEIGHT * sum(s for s, _f, _k in slacks) + em.total_changes())
    solver = new_solver(seconds)
    status = solver.Solve(em.model)
    if status == cp_model.INFEASIBLE:
        return Explanation(status="hard_lock", proven=True)
    if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        return Explanation()
    bends = [Bend(f, k, solver.Value(s)) for s, f, k in slacks if solver.Value(s) > 0]
    if not bends:
        return Explanation(status="found", grid=read_solution(em, solver))
    # 넘김이 꼭 필요하다는 증명 — 최적이거나, 목적 하한이 넘김 1개 무게 이상
    proven = status == cp_model.OPTIMAL or solver.BestObjectiveBound() >= BEND_WEIGHT
    return Explanation(status="bends", proven=proven,
                       bends=sorted(bends, key=lambda b: (-b.amount, str(b.key))))


def direct_conflicts(ctx: EditContext, rules: RuleSet, requests: Sequence[EditRequest],
                     days: Sequence[int]) -> List[Tuple[str, str]]:
    """요청 칸이 '바꿀 수 없는 칸'과 하드락으로 바로 부딪치는 곳 — 풀이 없이 짚는다.

    예) 13일 N 은 요청일 앞이라 못 바꾸는데 14일을 D 로 해 달라 → N→D 금지. 연쇄를 요청일 뒤로만
    허용하면서(11-02) 불가의 대부분이 이 꼴이라, '하드락에 막힘' 대신 어느 칸 때문인지 알려 준다.
    """
    open_cells = {(n, d) for n in ctx.nurse_ids for d in days if ctx.is_free(n, d)}
    options = {(r.nurse_id, r.day): _request_options(ctx, r) for r in requests
               if not isinstance(r, (CountTarget, CoverTarget, BalanceTarget))}
    out: List[Tuple[str, str]] = []
    for ineq in rules.ineqs:
        if ineq.key[0] not in HARD_LOCK_FAMILIES:
            continue
        hit = [(t.nurse_id, t.day) for t in ineq.terms if (t.nurse_id, t.day) in options]
        others = [t for t in ineq.terms if (t.nurse_id, t.day) not in options]
        if len(hit) != 1 or any((t.nurse_id, t.day) in open_cells for t in others):
            continue
        sentence = _conflict_sentence(ctx, ineq, hit[0], options[hit[0]], others)
        if sentence and (ineq.key[0], sentence) not in out:
            out.append((ineq.key[0], sentence))
    return out


def _request_options(ctx: EditContext, req: EditRequest) -> List[str]:
    if isinstance(req, SetCell):
        return [req.code]
    return [c for c in edit_codes(ctx) if c not in req.codes
            and c not in ctx.forbidden.get((req.nurse_id, req.day), set())]


def _conflict_sentence(ctx: EditContext, ineq, target, codes: List[str], others) -> Optional[str]:
    """요청 칸에 넣을 수 있는 근무가 전부 그 하드락을 새로 어기면 문장, 아니면 None."""
    fixed = sum(t.coef for t in others if cell(ctx, ctx.main, t.nurse_id, t.day) in t.codes)
    bound = max(ineq.cap, value_of(ctx, ctx.main, ineq))
    mine = [t for t in ineq.terms if (t.nurse_id, t.day) == target]
    for code in codes:
        if ineq.const + fixed + sum(t.coef for t in mine if code in t.codes) <= bound:
            return None
    n, d = target
    parts = ", ".join(f"{_day_label(t.day)} {_shown(ctx, t.nurse_id, t.day)}"
                      for t in sorted(others, key=lambda t: t.day))
    want = _AS_CODE[codes[0]] if len(codes) == 1 else "/".join(codes) + " 어느 것으로"
    return (f"{ctx.names.get(n, n)} {d + 1}일을 {want} 하면 {family_label(ineq.key)}에 "
            f"걸립니다({parts} — 이 칸들은 이미 정해져 바꿀 수 없습니다).")


def _shown(ctx: EditContext, nurse_id: str, day: int) -> str:
    """화면에 보이는 코드 — 병동 코드가 대표코드와 다르면 함께(예: MID 를 D2 로 쓰는 병동 'D2(M)')."""
    main = cell(ctx, ctx.main, nurse_id, day)
    raw = ctx.raw.get(nurse_id, [None] * ctx.days)[day] if 0 <= day < ctx.days else None
    if raw and main and raw != main and main != "W":
        return f"{raw}({main})"
    return raw or main or ""


#: 'D로' · 'N으로' — 받침에 맞춘 조사까지
_AS_CODE = {"D": "D로", "E": "E로", "N": "N으로", "M": "M으로", "O": "OFF로"}


def _day_label(day: int) -> str:
    if day >= 0:
        return f"{day + 1}일"
    return "전월 말일" if day == -1 else f"전월 말일 {-day - 1}일 전"


def add_rules_soft(em: EditModel, rules: RuleSet, waived, allow: Dict[Tuple, int],
                    softenable: Optional[Callable[[str], bool]] = None,
                    relief: Optional[Dict[Tuple, object]] = None) -> List[Tuple[object, str, Tuple]]:
    """softenable 인 규칙은 넘김 변수(slack)를 붙이고 나머지는 그대로 건다(기본: 하드락만 하드).

    팀 최소는 아직 넘김 대상이 아니다.
    """
    soft = softenable or (lambda family: family not in HARD_LOCK_FAMILIES)
    slacks = []
    for ineq in rules.ineqs:
        if not em.touches(ineq.terms) or is_waived(ineq.key, waived):
            continue
        base = value_of(em.ctx, em.base, ineq)
        expr = ineq.const + sum(t.coef * em.ind(t) for t in ineq.terms)
        bound = max(ineq.cap, base) + allow.get(ineq.key, 0) + (relief or {}).get(ineq.key, 0)
        family = ineq.key[0]
        if not soft(family):
            em.model.Add(expr <= bound)
            continue
        slack = em.model.NewIntVar(0, len(ineq.terms) + abs(ineq.cap) + 1, f"bend_{len(slacks)}")
        em.model.Add(expr <= bound + slack)
        slacks.append((slack, family, ineq.key))
    for cover in rules.covers:
        add_team_cover(em, cover)
    return slacks


#: 넘김 양의 단위 — 숫자만 쓰면 명·회·일 중 무엇인지 모른다(ui-craft 11-03). 없으면 '회'
BEND_UNIT = {"OFF수": "개", "인원최소": "명", "인원최대": "명", "인원초과": "명", "등급최소": "명",
             "같은근무연속": "일", "금지근무": "칸", WISH: "칸"}


def bend_text(bend: Bend, names: Dict[str, str]) -> str:
    """예) '김예나 개인 OFF 수 상한을 1개 넘김' · '14일 일별 최소 인원(D) 1명 넘김' · '20일 등급별 최소 인원(D 1등급까지) 1명 넘김'."""
    _fam, nurse, day, extra, side = (list(bend.key) + [None] * 5)[:5]
    who = names.get(nurse, nurse) if nurse else ""
    when = f"{day + 1}일" if isinstance(day, int) and day >= 0 else ""
    what = family_label(bend.key)
    if bend.family == "등급최소" and "≤" in str(extra):
        code, _sep, grade = str(extra).partition("≤")
        what += f"({code} {grade}등급까지)"
    elif extra not in (None, "") and bend.family not in ("OFF수", "개인한도", "전환금지"):
        what += f"({extra})"
    if bend.family == "개인한도":
        what += f"({str(extra).upper()})"
    amount = f"{bend.amount}{BEND_UNIT.get(bend.family, '회')}"
    tail = f"{side}을 {amount} 넘김" if side else f"{amount} 넘김"
    return " ".join(p for p in (who, when, what, tail) if p)
