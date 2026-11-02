"""답변 문장 — 결과에서 정해진 틀로 만든다(LLM 이 쓰지 않는다 · 숫자·사실을 지어낼 여지를 없앤다)."""

from __future__ import annotations

from collections import Counter
from typing import List, Sequence

from services.schedule_edit.context import EditContext
from services.schedule_edit.requests import CountTarget, EditRequest, SetCell, count_in, transfer_label

_SHIFT_WORD = {"D": "D", "E": "E", "N": "N", "M": "M", "O": "OFF", "OFF": "OFF"}


def describe_requests(ctx: EditContext, requests: Sequence[EditRequest]) -> str:
    """예) '김민지 14일 OFF, 이영희 15일 N 빼기, 박서준 15일 그대로'.

    지금 근무와 같은 근무로 둔 칸은 '그대로 두기'(revise_edit keep)다 — 바꾸는 요청이 따로 있을 때만 그렇게 쓴다.
    """
    parts: List[str] = []
    keeps = any(not _same(ctx, r) for r in requests)
    kept_by_day = Counter(r.day for r in requests if keeps and _same(ctx, r))
    for r in requests:
        who = ctx.names.get(r.nurse_id, r.nurse_id)
        if isinstance(r, CountTarget):
            parts.append(_describe_count(ctx, who, r))
            continue
        if keeps and _same(ctx, r):
            if kept_by_day[r.day] < 3:
                parts.append(f"{who} {r.day + 1}일 그대로")
            elif f"{r.day + 1}일 {kept_by_day[r.day]}명 그대로" not in parts:
                parts.append(f"{r.day + 1}일 {kept_by_day[r.day]}명 그대로")
        elif isinstance(r, SetCell):
            parts.append(f"{who} {r.day + 1}일 {_SHIFT_WORD.get(r.code, r.code)}")
        else:
            codes = "·".join(_SHIFT_WORD.get(c, c) for c in sorted(r.codes))
            parts.append(f"{who} {r.day + 1}일 {codes} 빼기")
    return ", ".join(parts)


def _describe_count(ctx: EditContext, who: str, r: CountTarget) -> str:
    """예) '장원영 N 5→3회(3~30일 · 같은 등급 간호사가 메우기)'."""
    word = "·".join(_SHIFT_WORD.get(c, c) for c in sorted(r.codes))
    extra = [] if (r.first_day, r.last_day) == (0, ctx.days - 1) else [f"{r.first_day + 1}~{r.last_day + 1}일"]
    if r.transfer and r.transfer != "auto":      # 기본값(가장 적게 바꾸기)은 적지 않는다 — 고른 것처럼 보이지 않게
        extra.append(transfer_label(ctx, r, r.transfer))
    return f"{who} {word} {count_in(ctx, ctx.main, r)}→{r.target}회" + (f"({' · '.join(extra)})" if extra else "")


def _same(ctx: EditContext, r: EditRequest) -> bool:
    return isinstance(r, SetCell) and ctx.main[r.nurse_id][r.day] == r.code


def answer_for(ctx: EditContext, requests: Sequence[EditRequest], result) -> str:
    head = describe_requests(ctx, requests)
    notes = [f"참고: {n}" for n in result.notes]
    if result.status == "ok":
        first = result.candidates[0]
        lo, hi = first.span
        when = f"{lo + 1}일" if lo == hi else f"{lo + 1}~{hi + 1}일"
        lines = [f"{head} — 바꿀 방법 {len(result.candidates)}가지를 찾았습니다. "
                 f"가장 적게 바꾸는 안은 {first.changed_count}칸({when})입니다. 카드를 눌러 표에서 확인해 보세요."]
        moved = _count_changes(ctx, requests, first)
        if moved:
            lines.append(f"근무 수 변화: {moved}")
        if first.quality:
            lines.append(f"이 안은 규칙은 모두 지키지만 {first.quality[0]} 등 근무 모양이 조금 나빠집니다.")
        return "\n".join(lines + notes)
    if result.status == "already":
        return "\n".join([f"{head} — 이미 그렇게 되어 있습니다."] + notes)
    lines = list(result.reasons) or ["방법을 찾지 못했습니다."]
    if result.levers:
        lines.append("아래 설정 중 하나를 이번만 넘기도록 고르시면 그 안에서 다시 찾습니다.")
    return "\n".join([f"{head}:"] + lines + notes) if head else "\n".join(lines + notes)


def _count_changes(ctx: EditContext, requests: Sequence[EditRequest], cand) -> str:
    """개수 요청이면 그 근무 수가 바뀐 사람을 — '장원영 N 5→3, 김지원 N 5→7'."""
    asked = {c for r in requests if isinstance(r, CountTarget) for c in r.codes}
    keys = {("OFF" if c == "O" else c) for c in asked}       # 후보 counts 는 OFF 를 'OFF' 로(휴가 뺀 순수 OFF)
    parts = [f"{ctx.names.get(n, n)} {_SHIFT_WORD.get(c, c)} {b}→{a}"
             for n, by in cand.counts.items() for c, (b, a) in by.items() if c in keys and a != b]
    return ", ".join(parts)


def status_text(session) -> str:
    """'지금 어떻게 돼 있어?' 같은 말에 — 대화 상태에서만 만든다(바뀌지 않은 걸 바뀌었다고 말하지 않게)."""
    ctx = session.ctx
    lines = [f"지금 요청: {describe_requests(ctx, session.requests) or '없음'}."]
    last = session.last
    if last is None:
        lines.append("아직 찾은 결과가 없습니다.")
    elif last.status == "ok" and last.candidates:
        lines.append(f"바꿀 방법 {len(last.candidates)}가지를 찾아 두었습니다(가장 적게 바꾸는 안 "
                     f"{last.candidates[0].changed_count}칸). 근무표는 카드에서 '적용'을 누르기 전까지 바뀌지 않습니다.")
    else:
        lines.append("마지막으로 찾았을 때 방법이 없었습니다. 근무표는 바뀌지 않았습니다.")
    return " ".join(lines)
