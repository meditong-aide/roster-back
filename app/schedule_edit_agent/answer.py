"""답변 문장 — 결과에서 정해진 틀로 만든다(LLM 이 쓰지 않는다 · 숫자·사실을 지어낼 여지를 없앤다)."""

from __future__ import annotations

from collections import Counter
from typing import List, Optional, Sequence

from services.schedule_edit.context import EditContext
from services.schedule_edit.requests import (
    SHAPE_COUNTED, SHAPE_LABEL, BalanceTarget, CountTarget, CoverTarget, EditRequest, LeaveCell, SetCell, ShapeTarget,
    WishTarget, balance_counts, count_in, cover_count, shape_count, shape_values, transfer_label, wish_count,
    wish_targets,
)

_SHIFT_WORD = {"D": "D", "E": "E", "N": "N", "M": "M", "O": "OFF", "OFF": "OFF"}
#: 칸 지정이 이보다 많으면 칸마다 쓰지 않고 사람별 칸 수로(버전 되돌리기 · 11-04)
CELLS_SHOWN = 6
#: 버전 비교 — 한 사람에 보일 칸·보일 사람 수(넘으면 '외 n') · 전부는 카드의 펼친 목록·표 미리보기에
COMPARE_CELLS = 4
COMPARE_PEOPLE = 5
#: 생성 후처리의 이름(`services.schedule_edit.postprocess.KINDS`)
SOURCE_WORD = {"annual_leave_swap": "자동 연차전환"}
#: 자동 연차전환을 건너뛴 이유(off_swap report.skip) → 문장
_SWAP_SKIP = {
    "disabled": "이 근무표 설정은 자동 연차전환이 꺼져 있습니다. 근무표 설정에서 켜야 합니다.",
    "no_target": "연차로 바꿀 근무 코드가 정해져 있지 않습니다. 근무코드 설정에서 '연차 전환 대상'을 정해 주세요.",
    "target_is_work": "연차 전환 대상 코드가 '근무' 종류라 바꾸지 않습니다. 근무코드 설정을 확인해 주세요.",
    "no_baseline": "기준 OFF 수가 설정돼 있지 않아 바꾸지 않습니다.",
    "no_off_code": "이 병동에 OFF 코드가 없어 바꾸지 않습니다.",
    "no_config": "이 근무표가 쓴 설정을 찾지 못해 돌릴 수 없습니다.",
}


def describe_requests(ctx: EditContext, requests: Sequence[EditRequest]) -> str:
    """예) '김민지 14일 OFF, 이영희 15일 N 빼기, 박서준 15일 그대로'.

    지금 근무와 같은 근무로 둔 칸은 '그대로 두기'(revise_edit keep)다 — 바꾸는 요청이 따로 있을 때만 그렇게 쓴다.
    """
    parts: List[str] = []
    keeps = any(not _same(ctx, r) for r in requests)
    cells = [r for r in requests if isinstance(r, (SetCell, LeaveCell)) and not (keeps and _same(ctx, r))]
    grouped = len(cells) > CELLS_SHOWN            # 버전 되돌리기처럼 칸이 많으면 사람별 칸 수로(11-04)
    if grouped:
        per = Counter(r.nurse_id for r in cells)
        parts.append(", ".join(f"{ctx.names.get(n, n)} 칸 {k}개" for n, k in per.items()) + " 지정")
    kept_by_day = Counter(r.day for r in requests if keeps and _same(ctx, r))
    for r in requests:
        if isinstance(r, CoverTarget):
            band = f"({r.grade}등급까지)" if r.grade is not None else ""
            parts.append(f"{r.day + 1}일 {r.code}{band} {cover_count(ctx, ctx.main, r)}→{r.target}명")
            continue
        if isinstance(r, BalanceTarget):
            parts.append(_describe_balance(ctx, r))
            continue
        if isinstance(r, WishTarget):
            parts.append(_describe_wish(ctx, r))
            continue
        if isinstance(r, ShapeTarget):
            parts.append(_describe_shape(ctx, r))
            continue
        who = ctx.names.get(r.nurse_id, r.nurse_id)
        if isinstance(r, CountTarget):
            parts.append(_describe_count(ctx, who, r))
            continue
        if keeps and _same(ctx, r):
            if r.code == "O" and kept_by_day[r.day] < 3:
                parts.append(f"{who} {r.day + 1}일 OFF 유지")       # 기간 OFF 중 이미 OFF 인 날(평가 11-03)
            elif kept_by_day[r.day] < 3:
                parts.append(f"{who} {r.day + 1}일 그대로")
            elif f"{r.day + 1}일 {kept_by_day[r.day]}명 그대로" not in parts:
                parts.append(f"{r.day + 1}일 {kept_by_day[r.day]}명 그대로")
        elif isinstance(r, (SetCell, LeaveCell)) and grouped:
            continue
        elif isinstance(r, SetCell):
            parts.append(f"{who} {r.day + 1}일 {_SHIFT_WORD.get(r.code, r.code)}")
        elif isinstance(r, LeaveCell):
            parts.append(f"{who} {r.day + 1}일 {ctx.leave_codes.get(r.code, r.code)}")
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


def _describe_balance(ctx: EditContext, r: BalanceTarget, grid=None) -> str:
    """예) 'N 근무 수 고르게 3~9회(차이 6) → 차이 1 이하 · 15명' · 'OFF 수 모두 9회로 · 15명'."""
    word = "·".join(_SHIFT_WORD.get(c, c) for c in sorted(r.codes))
    counts = balance_counts(ctx, ctx.main if grid is None else grid, r).values()
    low, high = (min(counts), max(counts)) if counts else (0, 0)
    if r.target is not None:
        return f"{word} 수 모두 {r.target}회로 · {len(r.members)}명"
    goal = "" if r.spread is None else f" → 차이 {r.spread} 이하"
    return f"{word} 근무 수 고르게 {low}~{high}회(차이 {high - low}){goal} · {len(r.members)}명"


def _describe_wish(ctx: EditContext, r: WishTarget) -> str:
    """예) '원티드 반영(전원 · 미반영 37개)' · '원티드 반영(장원영 · 15~20일 · 미반영 3개)'."""
    who = " · ".join(ctx.names.get(n, n) for n in r.nurses) if r.nurses else "전원"
    when = "" if not r.ranged else (f" · {r.first_day + 1}일" if r.first_day == r.last_day
                                    else f" · {r.first_day + 1}~{r.last_day + 1}일")
    return f"원티드 반영({who}{when} · 미반영 {len(wish_targets(ctx, r))}개)"


def _shape_scope(ctx: EditContext, r: ShapeTarget) -> str:
    who = "·".join(ctx.names.get(n, n) for n in r.nurses) if r.nurses else "전원"
    return who + (f" · {r.first_day + 1}~{r.last_day + 1}일" if r.ranged else "")


def _describe_shape(ctx: EditContext, r: ShapeTarget) -> str:
    """예) '하루짜리 OFF 줄이기(전원 · 지금 12개)' · '하루짜리 OFF 3개 이하로(전원 · 지금 12개)'(수를 말했을 때) ·
    '장원영 연속 근무 4일 이하로(지금 최대 5일)' · '김태희 주말 근무 3회 이하로(지금 최대 5회)'."""
    now = shape_count(ctx, ctx.main, r)
    if r.kind in SHAPE_COUNTED:
        if r.said and r.at_most is not None:
            limit = f"{r.at_most}개 이하로" if r.at_most else "0개로"
            return f"{SHAPE_LABEL[r.kind]} {limit}({_shape_scope(ctx, r)} · 지금 {now}개)"
        return f"{SHAPE_LABEL[r.kind]} 줄이기({_shape_scope(ctx, r)} · 지금 {now}개)"
    unit = "일" if r.kind == "work_run" else "회"
    limit = f"{r.at_most}{unit} 이하로" if r.at_most else f"0{unit}로"
    return f"{_shape_scope(ctx, r)} {SHAPE_LABEL[r.kind]} {limit}(지금 최대 {now}{unit})"


def shape_note(ctx: EditContext, r: ShapeTarget, cand) -> str:
    """1안의 결과 — 예) '하루짜리 OFF 12개 → 3개' · '가장 긴 연속 근무 5일 → 4일' · '주말 근무 김태희 5회 → 3회'."""
    from services.schedule_edit.solver import apply_candidate
    grid = apply_candidate(ctx, cand)
    if r.kind in SHAPE_COUNTED:
        return f"{SHAPE_LABEL[r.kind]} {shape_count(ctx, ctx.main, r)}개 → {shape_count(ctx, grid, r)}개"
    if r.kind == "work_run":
        return f"가장 긴 연속 근무 {shape_count(ctx, ctx.main, r)}일 → {shape_count(ctx, grid, r)}일"
    before, after = shape_values(ctx, ctx.main, r), shape_values(ctx, grid, r)
    moved = [f"{ctx.names.get(n, n)} {before[n]}→{after[n]}회" for n in before if before[n] != after.get(n)]
    return "주말 근무: " + (", ".join(moved[:5]) + (f" 외 {len(moved) - 5}명" if len(moved) > 5 else "") if moved else "변화 없음")


def wish_note(ctx: EditContext, r: WishTarget, cand) -> str:
    """후보로 넣는 원티드 수 — '미반영 37개 중 30개를 넣습니다.'"""
    from services.schedule_edit.solver import apply_candidate      # 답 모듈이 솔버를 늘 들고 있지 않게
    targets = wish_targets(ctx, r)
    got = wish_count(ctx, apply_candidate(ctx, cand), targets)
    return f"미반영 {len(targets)}개 중 {got}개를 넣습니다."


def wish_skipped_note(ctx: EditContext, r: WishTarget) -> str:
    """원티드 반영 범위 안에서 도우미가 넣지 않는 원티드(휴가 코드·바꿀 수 없는 칸)."""
    who = set(r.nurses)
    seen = [(kind, code) for n, d, kind, code in ctx.wish_skipped
            if r.first_day <= d <= r.last_day and (not who or n in who)]     # 지난 날·요청 밖은 세지 않는다(검토 L3)
    leave = sum(1 for kind, code in seen if kind == "code" and code in ctx.leave_codes)
    other = sum(1 for kind, code in seen if kind == "code" and code not in ctx.leave_codes)
    locked = sum(1 for kind, _c in seen if kind == "locked")
    parts = ([f"휴가 원티드 {leave}개는 '○○ ○일 연차 넣어줘'처럼 따로 말씀해 주세요"] if leave else []) + \
        ([f"근무·OFF·휴가가 아닌 원티드 {other}개는 대상이 아닙니다"] if other else []) + \
        ([f"바꿀 수 없는 칸(파견·프리셉티 등)의 원티드 {locked}개는 대상이 아닙니다"] if locked else [])
    return ". ".join(parts) + "." if parts else ""


def balance_note(ctx: EditContext, r: BalanceTarget, cand) -> str:
    """맞춘 뒤 범위와 대상에서 뺀 사람 — '맞춘 뒤 5~6회(차이 1). 대상에서 뺀 사람: 임윤아(N 전담·고정 근무·그 근무 불가)'."""
    from services.schedule_edit.solver import apply_candidate   # 답 문장 모듈이 솔버를 늘 들고 있지 않게
    counts = balance_counts(ctx, apply_candidate(ctx, cand), r).values()
    low, high = min(counts), max(counts)
    text = f"맞춘 뒤 {low}~{high}회(차이 {high - low})."
    left = [ctx.names.get(n, n) for n in ctx.nurse_ids if n not in r.members]
    if left:
        shown = ", ".join(left[:4]) + (f" 외 {len(left) - 4}명" if len(left) > 4 else "")
        text += f" 대상에서 뺀 사람: {shown}(N 전담·고정 근무·그 근무를 할 수 없는 사람)."
    return text


def _same(ctx: EditContext, r: EditRequest) -> bool:
    return isinstance(r, SetCell) and ctx.main[r.nurse_id][r.day] == r.code


def fresh_notes(session, notes: Sequence[str]) -> List[str]:
    """아직 보이지 않은 참고 문구만 — 보인 것으로 표시한다(같은 대화에서 매 답마다 붙지 않게)."""
    new = [n for n in notes if n not in session.shown_notes]
    session.shown_notes.update(new)
    return new


def answer_for(ctx: EditContext, requests: Sequence[EditRequest], result, notes: Optional[Sequence[str]] = None) -> str:
    """notes: 붙일 참고 문구(없으면 결과의 것 전부) — 대화에서는 `fresh_notes` 로 처음 한 번만."""
    head = describe_requests(ctx, requests)
    notes = [f"참고: {n}" for n in (result.notes if notes is None else notes)]
    if result.status == "ok":
        first = result.candidates[0]
        lo, hi = first.span
        when = f"{lo + 1}일" if lo == hi else f"{lo + 1}~{hi + 1}일"
        found = (f"바꿀 방법을 찾았습니다: {first.changed_count}칸({when})" if len(result.candidates) == 1 else
                 f"바꿀 방법 {len(result.candidates)}가지를 찾았습니다. 가장 적게 바꾸는 1안은 {first.changed_count}칸({when})")
        lines = [f"{head} — {found}. 카드를 누르면 표에서 미리 볼 수 있습니다."]
        moved = _count_changes(ctx, requests, first)
        if moved:
            lines.append(f"근무 수 변화: {moved}")
        if first.quality:
            lines.append(f"1안은 규칙을 모두 지키지만 근무 모양이 나빠지는 곳이 있습니다(예: {first.quality[0]}).")
        lines += [n for n in (cover_note(ctx, r) for r in requests if isinstance(r, CoverTarget)) if n]
        lines += [balance_note(ctx, r, first) for r in requests if isinstance(r, BalanceTarget)]
        lines += [wish_note(ctx, r, first) for r in requests if isinstance(r, WishTarget)]
        lines += [shape_note(ctx, r, first) for r in requests if isinstance(r, ShapeTarget)]
        lines += [n for n in (wish_skipped_note(ctx, r) for r in requests if isinstance(r, WishTarget)) if n]
        lines += list(getattr(result, "extra", []) or [])
        # 설정 카드 안내는 맨 뒤 — 이 안의 결과를 먼저 말한다
        if result.levers and result.levers[0].gain:
            lines.append(f"설정 하나를 이번만 넘기시면 {result.levers[0].gain}를 넣는 방법도 아래에 있습니다.")
        elif result.levers:
            fewest = min(lv.changes[1] for lv in result.levers)
            lines.append(f"설정 하나를 이번만 넘기시면 {first.changed_count}칸 대신 {fewest}칸만 바꾸는 방법도 아래에 있습니다.")
        return "\n".join(lines + notes)
    if result.status == "already":
        shape = next((r for r in requests if isinstance(r, ShapeTarget)), None)
        if shape is not None and shape.kind in SHAPE_COUNTED and not shape_count(ctx, ctx.main, shape):
            return "\n".join([f"바꿀 수 있는 칸에는 고칠 곳이 없습니다 — {SHAPE_LABEL[shape.kind]} 0개"
                              f"({_shape_scope(ctx, shape)})."] + notes)
        wish = next((r for r in requests if isinstance(r, WishTarget)), None)
        if wish is not None:
            skipped = wish_skipped_note(ctx, wish)
            return "\n".join(["바꿀 수 있는 칸의 원티드는 모두 반영되어 있습니다."] + ([skipped] if skipped else [])
                             + notes)
        return "\n".join([f"{head} — 이미 그렇게 되어 있습니다."] + notes)
    lines = list(result.reasons) or ["방법을 찾지 못했습니다."]
    if result.levers:
        lines.append("아래 설정 중 하나를 이번만 넘기도록 고르시면 그 안에서 다시 찾습니다.")
    return "\n".join([f"{head}:"] + lines + notes) if head else "\n".join(lines + notes)


def cover_note(ctx: EditContext, r) -> str:
    """그날 인원 요청으로 넘기는 인원 설정 — 요청한 것이 곧 동의지만 무엇을 넘기는지는 말한다.
    예) '요청대로 15일 E를 필요 인원(3명)보다 1명 많이 둡니다(이번만 · 병동 설정은 그대로).'"""
    from services.schedule_edit.model import cover_relief     # 모델을 답 문장 모듈이 늘 들고 있지 않게
    from services.schedule_edit.rules import coverage_bounds
    need, upper, over = coverage_bounds(ctx, r.day, r.code)
    base = {"인원최소": ("최소 인원", need, "적게"), "인원최대": ("최대 인원", upper, "많이"),
            "인원초과": ("필요 인원", over, "많이")}
    parts = [f"{base[k[0]][0]}({base[k[0]][1]}명)보다 {v}명 {base[k[0]][2]}"
             for k, v in cover_relief(ctx, r).items() if k[0] in base]
    if not parts:
        return ""
    eul = {"N": "을", "M": "을"}.get(r.code, "를")        # 엔·엠은 받침이 있다
    return f"요청대로 {r.day + 1}일 {r.code}{eul} " + " · ".join(parts) + " 둡니다(이번만 · 병동 설정은 그대로)."


def _count_changes(ctx: EditContext, requests: Sequence[EditRequest], cand) -> str:
    """개수 요청이면 그 근무 수가 바뀐 사람을 — '장원영 N 5→3, 김지원 N 5→7'."""
    asked = {c for r in requests if isinstance(r, (CountTarget, BalanceTarget)) for c in r.codes}
    keys = {("OFF" if c == "O" else c) for c in asked}       # 후보 counts 는 OFF 를 'OFF' 로(휴가 뺀 순수 OFF)
    parts = [f"{ctx.names.get(n, n)} {_SHIFT_WORD.get(c, c)} {b}→{a}"
             for n, by in cand.counts.items() for c, (b, a) in by.items() if c in keys and a != b]
    # 고르게 맞추기는 여러 사람이 바뀐다 — 앞 6명만(나머지는 카드·표에서)
    return ", ".join(parts[:6]) + (f" 외 {len(parts) - 6}명" if len(parts) > 6 else "")


def current_request(session) -> str:
    """지금 요청 한 줄 — 생성 후처리로 낸 결과면 그 이름, 아니면 솔버 요청."""
    last = session.last
    if last is not None and getattr(last, "source", None):
        return SOURCE_WORD.get(last.source, last.source)
    return describe_requests(session.ctx, session.requests)


def status_text(session) -> str:
    """'지금 어떻게 돼 있어?' 같은 말에 — 대화 상태에서만 만든다(바뀌지 않은 걸 바뀌었다고 말하지 않게)."""
    lines = [f"지금 요청: {current_request(session) or '없음'}."]
    last = session.last
    if last is None:
        lines.append("아직 찾은 결과가 없습니다.")
    elif last.status == "ok" and last.candidates:
        lines.append(f"바꿀 방법 {len(last.candidates)}가지를 찾아 두었습니다(가장 적게 바꾸는 안 "
                     f"{last.candidates[0].changed_count}칸). 근무표는 카드에서 '적용'을 누르기 전까지 바뀌지 않습니다.")
    elif last.reasons and (last.status == "already" or getattr(last, "source", None)):
        lines.append(last.reasons[0])           # 생성 후처리를 돌렸지만 바뀔 칸이 없었던 이유 등(평가 11-03)
    elif last.status == "already":
        lines.append("이미 그렇게 되어 있어 바꿀 칸이 없습니다. 근무표는 바뀌지 않았습니다.")
    elif last.status == "invalid":
        lines.append("요청을 받을 수 없었습니다" + (f": {last.reasons[0]}" if last.reasons else ".")
                     + " 근무표는 바뀌지 않았습니다.")
    elif last.status == "timeout":
        lines.append("시간 안에 방법을 찾지 못했습니다. 근무표는 바뀌지 않았습니다.")
    else:
        lines.append("마지막으로 찾았을 때 방법이 없었습니다. 근무표는 바뀌지 않았습니다.")
    return " ".join(lines)


def postprocess_answer(ctx: EditContext, result) -> str:
    """생성 후처리(지금은 자동 연차전환)를 미리 돌린 결과 — 바뀌는 칸 수와, 안 바뀐 몫의 이유를 서비스 숫자 그대로."""
    report = result.report or {}
    if report.get("skip"):
        return _SWAP_SKIP.get(report["skip"], "자동 연차전환을 돌릴 수 없습니다.")
    head = f"자동 연차전환(OFF 기준 {report.get('baseline')}개 · 바꿀 코드 '{report.get('target_shift')}')"
    notes: List[str] = []
    if report.get("kept_off_for_week_floor"):
        notes.append(f"기준을 넘는 OFF {report['kept_off_for_week_floor']}칸은 주 2회 휴일 하한을 지키려면 그대로 둬야 합니다")
    if report.get("protected"):
        notes.append(f"{report['protected']}칸은 나이트 뒤 회복 OFF·원티드 반영 칸·주휴라 바꾸지 않습니다")
    if report.get("skipped_n_only"):
        notes.append(f"N 전담 {report['skipped_n_only']}명은 바꾸지 않습니다")
    if result.held:
        notes.append(f"지난 날·바꿀 수 없는 칸 {result.held}칸은 바꾸지 않습니다")
    if not result.changes:
        if not report.get("excess_total"):
            return f"{head}을 돌려 봤지만 기준을 넘는 OFF 가 없어 바꿀 칸이 없습니다."
        return f"{head}을 돌려 봤지만 바꿀 칸이 없습니다. " + " · ".join(notes) + "."
    people = len({c.nurse_id for c in result.changes})
    lines = [f"{head}을 지금 근무표에 돌리면 OFF {len(result.changes)}칸을 연차로 바꿉니다({people}명). "
             "카드를 눌러 표에서 확인해 보세요."]
    if notes:
        lines.append("참고: " + " · ".join(notes) + ".")
    lines.append("연차 잔여는 보지 않습니다(근무표를 만들 때의 자동 전환과 같습니다).")
    return "\n".join(lines)


def lookup_answer(ctx: EditContext, kind: str, nurse, day, code) -> str:
    """조회 답 — 지금 저장된 근무표에서만 센다(찾아 둔 후보는 아직 근무표가 아니다)."""
    from schedule_edit_agent.grounding import count_status
    word = _SHIFT_WORD.get(code, code)
    if kind == "person_count":
        return count_status(ctx, nurse, code) + " (지금 저장된 근무표 기준)"
    if kind == "person_day":
        raw, main = ctx.raw[nurse][day], ctx.main[nurse][day]
        shown = ctx.leave_codes.get(raw, raw) if raw else "빈 칸"
        detail = f"({_SHIFT_WORD.get(main, main)})" if main and raw and _SHIFT_WORD.get(main, main) != raw else ""
        return f"{ctx.names.get(nurse, nurse)} {day + 1}일은 {shown}{detail}입니다(지금 저장된 근무표 기준)."
    names = [ctx.names.get(n, n) for n in ctx.nurse_ids
             if ctx.main[n][day] == code and not (code == "O" and (n, day) in ctx.vacation)]
    if code == "O":
        away = [f"{ctx.names.get(n, n)}({ctx.leave_codes.get(ctx.raw[n][day], ctx.raw[n][day])})"
                for n in ctx.nurse_ids if (n, day) in ctx.vacation]
        tail = f" 휴가 {len(away)}명: {', '.join(away)}." if away else ""
    else:
        tail = ""
    if not names:
        return f"{day + 1}일 {word}인 사람이 없습니다.{tail} (지금 저장된 근무표 기준)"
    return f"{day + 1}일 {word} {len(names)}명: {', '.join(names)}.{tail} (지금 저장된 근무표 기준)"


# ── 규칙 질문(사용자 지시 11-03 — 후보를 본 뒤 '그레이드는 다 지켜져?') ──────────────────────────

#: 규칙 질문에서 한 줄에 보일 곳 수 — 나머지는 '외 n곳'
CHECK_SHOWN = 3
#: 규칙이 아니라 근무 모양(품질)을 묻는 주제
SHAPE = "shape"


def check_answer(session, topic: str, candidates: Sequence) -> str:
    """찾아 둔 후보(없으면 지금 저장된 근무표)가 그 규칙을 지키는지. 후보는 '모두 지킴'으로 뭉뚱그리지 않는다 —
    원래 어긋나 있던 곳은 그대로 둘 수 있어서(더 나빠지지만 않게 찾는다) 그걸 따로 말한다."""
    from services.schedule_edit.rule_check import check_candidate, check_current, configured, topic_of
    ctx = session.ctx
    if topic == SHAPE:
        return _shape_answer(candidates)
    label, families = topic_of(topic)
    if not configured(session.rules, families):
        if topic == "wanted":
            return "지금 근무표에 반영돼 있는 원티드가 없어 따질 것이 없습니다."
        return f"이 근무표 설정에는 {label} 규칙이 없어 따질 것이 없습니다."
    if not candidates:
        row = check_current(ctx, session.rules, families)
        if not row.kept:
            return f"{label}: 지금 저장된 근무표는 모두 지킵니다."
        return f"{label}: 지금 저장된 근무표에서 어긋난 곳 {len(row.kept)}곳 — {_few(row.kept)}."
    rows = [check_candidate(ctx, session.rules, c, session.requests, families) for c in candidates]
    if len(rows) == 1:
        return f"{label}: " + _check_line(rows[0])
    if len({(tuple(r.new), tuple(r.kept), r.eased) for r in rows}) == 1:
        # 후보끼리 결과가 같으면 한 줄로(같은 문장을 세 번 읽지 않게)
        return f"{label}: 후보 {len(rows)}개 모두 " + _check_line(rows[0], name="")
    return "\n".join([f"{label}:"] + [f"· {_check_line(row)}" for row in rows])


def _check_line(row, name: Optional[str] = None) -> str:
    who = f"{row.rank}안은 " if name is None else name
    eased = f" 지금 어긋나 있던 {row.eased}곳은 이 안으로 풀립니다." if row.eased else ""
    if not row.new and not row.kept:
        return f"{who}지킵니다.{eased}" if name == "" else f"{who}모두 지킵니다.{eased}"
    parts = []
    if row.new:
        parts.append(f"{who}이번만 넘기는 곳이 {len(row.new)}곳 있습니다(고른 설정·요청으로 넘긴 것) — {_few(row.new)}.")
    else:
        parts.append(f"{who}새로 어기는 곳이 없습니다.")
    if row.kept:
        parts.append(f"원래 근무표에서 어긋나 있던 {len(row.kept)}곳은 그대로입니다(더 나빠지지 않음) — {_few(row.kept)}.")
    return " ".join(parts) + eased


def _shape_answer(candidates: Sequence) -> str:
    if not candidates:
        return "근무 모양은 바꿀 안을 찾은 뒤, 그 안이 지금보다 나빠지는지로 알려 드립니다."
    lines = [(f"{c.rank}안은 나빠지는 곳이 없습니다." if not c.quality else
              f"{c.rank}안은 나빠지는 곳이 {len(c.quality)}곳 있습니다 — {_few(c.quality)}.") for c in candidates]
    if len(lines) == 1:
        return "근무 모양(규칙 위반은 아님): " + lines[0]
    return "\n".join(["근무 모양(규칙 위반은 아님):"] + [f"· {line}" for line in lines])


def _few(items: Sequence[str]) -> str:
    shown = ", ".join(items[:CHECK_SHOWN])
    return shown + (f" 외 {len(items) - CHECK_SHOWN}곳" if len(items) > CHECK_SHOWN else "")


# ── 버전 비교·되돌리기(11-04) ───────────────────────────────────────────────


def compare_answer(target_label: str, current_label: str, data: dict, nurse: Optional[str] = None,
                   nurse_name: Optional[str] = None) -> str:
    """`compare_schedules`(화면 [버전 비교]와 같은 숫자) → 문장. 방향은 '그 버전 → 지금'(그 버전 값→지금 값)."""
    cells = [c for c in data.get("cells", []) if nurse is None or c["nurse_id"] == nurse]
    scope = f"{nurse_name or nurse} " if nurse else ""
    head = f"{target_label} → 지금 버전 {current_label}"
    if not cells:
        lines = [f"{head}: {scope}칸이 모두 같습니다."]
    else:
        people = list(dict.fromkeys(c["nurse_id"] for c in cells))
        lines = [f"{head}: {scope}다른 칸 {len(cells)}개 · {len(people)}명"]
        for n in people[:COMPARE_PEOPLE]:
            mine = [c for c in cells if c["nurse_id"] == n]
            shown = " · ".join(f"{int(c['date'][8:10])}일 {_code(c['from'])}→{_code(c['to'])}"
                               for c in mine[:COMPARE_CELLS])
            more = f" 외 {len(mine) - COMPARE_CELLS}칸" if len(mine) > COMPARE_CELLS else ""
            lines.append(f"· {mine[0]['nurse_name'] or n} {len(mine)}칸: {shown}{more}")
        if len(people) > COMPARE_PEOPLE:
            lines.append(f"· 외 {len(people) - COMPARE_PEOPLE}명")
    if not nurse:
        added, removed = len(data.get("added_nurses") or []), len(data.get("removed_nurses") or [])
        if added or removed:
            lines.append(f"명단: 지금 버전에만 있는 사람 {added}명 · {target_label.split('(')[0]}에만 있는 사람 {removed}명")
    return "\n".join(lines)


def _code(cell: Optional[dict]) -> str:
    code = (cell or {}).get("shift_id")
    return _SHIFT_WORD.get(str(code).upper(), str(code)) if code else "빈 칸"


def revert_lead(target_label: str, scope: str, plan, missing_names: Sequence[str]) -> str:
    """일부 되돌리기 첫 줄 — 가져오는 칸 수와 그대로 두는 칸(이유별)."""
    from services.schedule_edit.versions import SKIP_WORD
    kept = " · ".join(f"{SKIP_WORD.get(k, k)} {v}칸" for k, v in plan.skipped.items() if v)
    from services.schedule_edit.requests import eun, gwa
    absent = f" {eun('·'.join(missing_names))} {target_label.split('(')[0]}에 없습니다." if missing_names else ""
    if not plan.requests:
        why = (f"다른 칸 {plan.differ}개는 모두 그대로 둬야 하는 칸입니다({kept})" if plan.differ
               else "칸이 모두 같습니다")
        return f"{gwa(target_label)} {scope}에서 되돌릴 칸이 없습니다 — {why}.{absent}"
    rest = f" 다른 칸 {plan.differ}개 중 {kept}은 그대로 둡니다." if kept else ""
    return f"{target_label}에서 {scope} 칸 {len(plan.requests)}개를 가져옵니다.{rest}{absent}"

