"""바꾸기 전 산술 점검 — 요청대로 바꾸면 '반드시' 어기게 되는 것을 풀기 전에 짚고, 적용 직전에 나빠지는 것을 모은다.

수간호사가 요청을 던지면 무조건 바꾸지 않는다(사용자 지시 11-03). 풀기 전에 요청 칸·바꿀 수 없는 칸(요청한 첫날 앞·
묶인 칸)·**전월 근무 꼬리**만으로 셈해, 어떤 방법으로 바꿔도 지금보다 나빠지는 규칙이 있으면 먼저 알리고 동의를 받는다.

셈은 규칙 부등식(`rules.Ineq` — 전월 꼬리를 음수 날로 이미 담고 있어 메인 솔버와 같은 창이다)의 하한이다. 바꿀 수 있는
칸은 그 식에 가장 유리한 값으로 두고 세므로, 짚은 것은 어떤 해로도 피할 수 없다(틀린 경고가 없다). 연쇄로만 생기는
넘김은 여기서 못 짚는다 — 풀이가 불가·레버로 알리고, 적용 직전 확인(`apply_findings`)이 실제 후보로 다시 모은다.

  · 막힘   : 하드락(연속 근무·전환 금지·나이트 규칙·월 나이트 상한)과 이 도우미가 넘길 수 없는 설정 — 풀지 않고 이유만
  · 확인   : 레버 대상 설정(인원·등급·OFF 수·개인 한도·N 간격 등) — 동의하면 그만큼 이번만 넘기고 푼다.
             그날 인원 요청이 넘기는 인원 설정 · 직접 짚은 원티드 칸도 먼저 묻는다
  · 적용 전: 고른 안으로 지금보다 나빠지는 설정·원티드·근무 모양 전부(앞에서 동의한 것은 빼고 묻는다)

동의한 것의 id 는 위치와 양이다 — 같은 것을 이 대화에서 다시 묻지 않고, 양이 늘면 다시 묻는다.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, FrozenSet, Iterable, List, Optional, Sequence, Set, Tuple

from services.schedule_edit.context import FIXED_WANTED, Cell, EditContext
from services.schedule_edit.explain import (
    HARD_LOCK_FAMILIES, Bend, _conflict_sentence, _day_label, _shown, bend_text, family_label,
)
from services.schedule_edit.levers import LEVER_FAMILIES, lever_allow
from services.schedule_edit.model import coverage_relief, cover_relief, edit_codes, is_waived, requested_cells
from services.schedule_edit.requests import (
    AvoidCell, BalanceTarget, CountTarget, CoverTarget, EditRequest, SetCell, count_cells, count_in, cover_count,
    is_satisfied, request_problem,
)
from services.schedule_edit.rules import WISH, Ineq, RuleSet, cell, coverage_bounds, value_of
from services.schedule_edit.serialize import allow_from_json, allow_to_json
from services.schedule_edit.solver import Candidate, candidate_regressions, prepare

#: 한 달 개수 규칙 — 개수 요청·'모두 같은 수' 맞추기의 목표로 하한을 더 조인다
MONTH_FAMILIES = ("개인한도", "OFF수", "월나이트")
#: 확정 원티드 칸의 위치 이름 — 묶인 칸이라 규칙 부등식이 없다(직접 짚을 때만 풀린다)
FIXED_WISH = "확정원티드"
#: 막혔을 때 첫 줄
BLOCKED_HEAD = ("바꾸기 전에 셈해 보니(전월 근무까지 넣어) 어떤 방법으로도 넘길 수 없는 규칙에 걸려 이 요청은 들어줄 수 "
                "없습니다.")
_WORD = {"O": "OFF"}


@dataclass
class Finding:
    """알리고 동의받을 것 하나."""

    id: str
    text: str
    kind: str                      # setting · cover · wanted · quality
    #: 동의하면 이번만 넘길 양(레버 대상 설정만 · 그날 인원 요청·짚은 원티드는 이미 풀이가 봐준다)
    allow: Dict[Tuple, int] = field(default_factory=dict)


@dataclass
class Precheck:
    blocked: List[str] = field(default_factory=list)
    families: List[str] = field(default_factory=list)
    findings: List[Finding] = field(default_factory=list)

    def pending(self, confirmed: Iterable[str]) -> List[Finding]:
        done = set(confirmed)
        return [f for f in self.findings if f.id not in done]


def finding_id(key: Tuple, amount: int) -> str:
    return "/".join(str(k) for k in key) + f"|{amount}"


# ── 풀기 전 ──────────────────────────────────────────────────────────────────


def precheck(ctx: EditContext, rules: Optional[RuleSet], requests: Sequence[EditRequest], *, open_from: int,
             allow: Optional[Dict[Tuple, int]] = None) -> Precheck:
    """allow: 이 대화에서 이미 허용한 넘김(고른 레버·앞서 동의한 것) — 그만큼은 다시 묻지 않는다."""
    requests = list(requests)
    if not requests or any(request_problem(ctx, r, open_from) for r in requests) \
            or all(is_satisfied(ctx, r) for r in requests):
        return Precheck()          # 성립하지 않거나 이미 된 요청은 풀이가 그대로 알린다
    pctx, prules, preqs = prepare(ctx, rules, requests)
    start = min(r.day for r in preqs)
    free = {(n, d) for n in pctx.nurse_ids for d in range(start, pctx.days) if pctx.is_free(n, d)}
    forced = {(r.nurse_id, r.day): r.code for r in preqs if isinstance(r, SetCell)}
    banned: Dict[Cell, Set[str]] = {}
    for r in preqs:
        if isinstance(r, AvoidCell):
            banned.setdefault((r.nurse_id, r.day), set()).update(r.codes)
    months = _month_counts(pctx, preqs, free)
    touched = set(forced) | set(banned)
    granted = dict(lever_allow(allow))
    for key, amount in coverage_relief(pctx, prules, preqs, lambda *_: 1).items():
        granted[key] = granted.get(key, 0) + amount       # 그날 인원 요청·'그날 인원 줄이기'가 이미 봐주는 양(가장 크게)
    waived = requested_cells(pctx, preqs)
    codes = set(edit_codes(pctx))
    out = Precheck()
    for ineq in prules.ineqs:
        month = _month_of(ineq, months)
        if (month is None and not any((t.nurse_id, t.day) in touched for t in ineq.terms)) \
                or is_waived(ineq.key, waived):
            continue
        low = _lower_bound(pctx, ineq, forced, banned, free, codes)
        if month is not None:
            lo, hi = month
            low = max(low, lo if ineq.terms[0].coef > 0 else -hi)
        total = low - max(ineq.cap, value_of(pctx, pctx.main, ineq))
        if total <= granted.get(ineq.key, 0):
            continue
        family = ineq.key[0]
        if family in HARD_LOCK_FAMILIES or family not in LEVER_FAMILIES:
            text = _blocked_text(pctx, ineq, forced, banned, free, low)
            if text not in out.blocked:
                out.blocked.append(text)
                out.families = sorted(set(out.families) | {family})
            continue
        out.findings.append(Finding(finding_id(ineq.key, total), bend_text(Bend(family, ineq.key, total), pctx.names),
                                    "setting", {ineq.key: total - granted.get(ineq.key, 0)}))
    out.findings = _cover_findings(pctx, preqs) + _wanted_findings(ctx, requests) + out.findings
    return out


def _lower_bound(ctx: EditContext, ineq: Ineq, forced: Dict[Cell, str], banned: Dict[Cell, Set[str]],
                 free: Set[Cell], codes: Set[str]) -> int:
    """식의 가장 작은 값 — 요청 칸은 요청대로, 바꿀 수 있는 칸은 식에 가장 유리하게, 나머지(전월 꼬리 포함)는 지금 값."""
    total = ineq.const
    for t in ineq.terms:
        at = (t.nurse_id, t.day)
        if at in forced:
            values = {forced[at]}
        elif at in free:
            values = (codes - banned.get(at, set())) or codes
        else:
            values = {cell(ctx, ctx.main, t.nurse_id, t.day)}
        if t.coef > 0:
            total += t.coef if values <= t.codes else 0
        elif values & t.codes:
            total += t.coef
    return total


def _month_counts(ctx: EditContext, requests: Sequence[EditRequest],
                  free: Set[Cell]) -> Dict[Tuple[str, FrozenSet[str]], Tuple[int, int]]:
    """개수 요청·'모두 같은 수' 맞추기가 정하는 그 사람 그 근무의 한 달 개수 (최소, 최대).
    개수 요청은 기간 밖 그 근무가 줄이는 요청이면 늘지 않고, 늘리는 요청이면 줄지 않는다(`model.add_requests`)."""
    out: Dict[Tuple[str, FrozenSet[str]], Tuple[int, int]] = {}
    for r in requests:
        if isinstance(r, CountTarget):
            inside = set(r.days())
            outside = [d for d in range(ctx.days) if d not in inside]
            now = count_cells(ctx, ctx.main, r.nurse_id, r.codes, outside)
            fixed = count_cells(ctx, ctx.main, r.nurse_id, r.codes, [d for d in outside if (r.nurse_id, d) not in free])
            opened = sum(1 for d in outside if (r.nurse_id, d) in free)
            lower = r.target < count_in(ctx, ctx.main, r)
            out[(r.nurse_id, frozenset(r.codes))] = ((r.target + fixed, r.target + now) if lower
                                                     else (r.target + now, r.target + fixed + opened))
        elif isinstance(r, BalanceTarget) and r.target is not None:
            for n in r.members:
                out[(n, frozenset(r.codes))] = (r.target, r.target)
    return out


def _month_of(ineq: Ineq, months: Dict[Tuple[str, FrozenSet[str]], Tuple[int, int]]) -> Optional[Tuple[int, int]]:
    if ineq.key[0] not in MONTH_FAMILIES or not ineq.terms:
        return None
    return months.get((ineq.key[1], frozenset(ineq.terms[0].codes)))


def _blocked_text(ctx: EditContext, ineq: Ineq, forced: Dict[Cell, str], banned: Dict[Cell, Set[str]],
                  free: Set[Cell], low: int) -> str:
    key = ineq.key
    if key[0] in MONTH_FAMILIES:
        codes = sorted(ineq.terms[0].codes)
        word = "·".join(_WORD.get(c, c) for c in codes)
        subject = "이" if codes[-1] in ("N", "M") else "가"          # 엔·엠은 받침이 있다
        who = ctx.names.get(key[1], key[1])
        if ineq.terms[0].coef > 0:
            return f"{who} — 요청대로면 이 달 {word}{subject} 적어도 {low}회라 {family_label(key)} {ineq.cap}회를 넘습니다."
        return (f"{who} — 요청대로면 이 달 {word}{subject} 많아야 {-low}회라 {family_label(key)} {-ineq.cap}회에 "
                "못 미칩니다.")
    hit = sorted({(t.nurse_id, t.day) for t in ineq.terms if (t.nurse_id, t.day) in forced or
                  (t.nurse_id, t.day) in banned}, key=lambda c: (c[1], c[0]))
    others = [t for t in ineq.terms if (t.nurse_id, t.day) not in hit]
    if len(hit) == 1 and not any((t.nurse_id, t.day) in free for t in others):
        at = hit[0]
        options = [forced[at]] if at in forced else sorted(set(edit_codes(ctx)) - banned.get(at, set()))
        sentence = _conflict_sentence(ctx, ineq, at, options, others)
        if sentence:
            return sentence
    fixed = {}
    for t in sorted(others, key=lambda t: t.day):
        at = (t.nurse_id, t.day)
        if at not in free and cell(ctx, ctx.main, t.nurse_id, t.day) in t.codes:
            fixed[at] = f"{_day_label(t.day)} {_shown(ctx, t.nurse_id, t.day)}"
    who = " · ".join(dict.fromkeys(ctx.names.get(n, n) for n, _d in hit)) or ctx.names.get(key[1], key[1] or "")
    days = "·".join(str(d + 1) for d in sorted({d for _n, d in hit}))
    tail = f"({', '.join(fixed.values())} — 이미 정해져 바꿀 수 없는 칸)" if fixed else ""
    return f"{who} {days}일 요청대로 바꾸면 {family_label(key)}에 걸립니다{tail}."


def _cover_findings(ctx: EditContext, requests: Sequence[EditRequest]) -> List[Finding]:
    """그날 인원 요청이 넘기는 최소·최대 인원 — 요청이 곧 동의였지만(풀이가 봐준다) 무엇을 넘기는지 먼저 묻는다."""
    out = []
    for r in requests:
        if not isinstance(r, CoverTarget) or r.grade is not None:
            continue
        need, upper, _over = coverage_bounds(ctx, r.day, r.code)
        now = cover_count(ctx, ctx.main, r)
        for key in cover_relief(ctx, r):
            if key[0] == "인원최소":
                total = (need - r.target) - max(0, need - now)
                text = f"{r.day + 1}일 {r.code} {now}명 → {r.target}명 — 일별 최소 인원 {need}명보다 {need - r.target}명 적음"
            elif key[0] == "인원최대" and upper is not None:
                total = (r.target - upper) - max(0, now - upper)
                text = f"{r.day + 1}일 {r.code} {now}명 → {r.target}명 — 일별 최대 인원 {upper}명보다 {r.target - upper}명 많음"
            else:
                continue                    # 필요 인원보다 많이(품질)는 적용 전 확인에서
            if total > 0:
                out.append(Finding(finding_id(key, total), text, "cover"))
    return out


def _wanted_findings(ctx: EditContext, requests: Sequence[EditRequest]) -> List[Finding]:
    """직접 짚은 원티드 칸 — 요청이 이기지만(사용자 지시 11-03) 그 원티드를 못 지킨다는 것을 먼저 묻는다."""
    out = []
    for n, d in sorted(requested_cells(ctx, list(requests)), key=lambda c: (c[1], c[0])):
        if ctx.frozen.get((n, d)) == FIXED_WANTED:
            key, label = (FIXED_WISH, n, d), "확정 원티드"
        elif ctx.main[n][d] in ctx.wishes.get((n, d), ()):
            key, label = (WISH, n, d, "".join(sorted(ctx.wishes[(n, d)]))), "원티드"
        else:
            continue
        out.append(Finding(finding_id(key, 1), f"{ctx.names.get(n, n)} {d + 1}일 {_shown(ctx, n, d)} — {label}로 받은 "
                                               "칸이라 바꾸면 그 원티드를 지키지 못함", "wanted"))
    return out


# ── 적용 직전 ────────────────────────────────────────────────────────────────


def apply_findings(ctx: EditContext, rules: Optional[RuleSet], candidate: Candidate,
                   requests: Sequence[EditRequest]) -> List[Finding]:
    """이 안을 적용하면 지금보다 나빠지는 것 전부 — 넘기는 설정(고른 레버·그날 인원 포함)·원티드 칸·근무 모양."""
    out = []
    for key, (before, after) in sorted(candidate_regressions(ctx, rules, candidate, list(requests)).items(),
                                       key=lambda kv: str(kv[0])):
        total = after - before
        if key[0] == WISH:
            out.append(Finding(finding_id(key, total), _changed_wish(ctx, candidate, key[1], key[2], "원티드"),
                               "wanted"))
        else:
            out.append(Finding(finding_id(key, total), bend_text(Bend(key[0], key, total), ctx.names), "setting"))
    for ch in candidate.changes:
        if ctx.frozen.get((ch.nurse_id, ch.day)) == FIXED_WANTED:
            out.append(Finding(finding_id((FIXED_WISH, ch.nurse_id, ch.day), 1),
                               _changed_wish(ctx, candidate, ch.nurse_id, ch.day, "확정 원티드"), "wanted"))
    out += [Finding(f"품질|{q}", q, "quality") for q in candidate.quality]
    return out


def _changed_wish(ctx: EditContext, candidate: Candidate, nurse_id: str, day: int, label: str) -> str:
    ch = next((c for c in candidate.changes if (c.nurse_id, c.day) == (nurse_id, day)), None)
    moved = f" {ch.before or '빈 칸'}→{ch.after}" if ch is not None else ""
    return f"{ctx.names.get(nurse_id, nurse_id)} {day + 1}일 {label} 칸{moved}"


# ── 기록·복원 ────────────────────────────────────────────────────────────────


def finding_to_json(f: Finding) -> Dict[str, Any]:
    return {"id": f.id, "text": f.text, "kind": f.kind, "allow": allow_to_json(f.allow)}


def finding_from_json(item: Dict[str, Any]) -> Finding:
    return Finding(str(item.get("id")), str(item.get("text") or ""), str(item.get("kind") or "setting"),
                   allow_from_json(item.get("allow") or []))
