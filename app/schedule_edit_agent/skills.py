"""스킬 — 얇은 어댑터(②층). AIDE 와 같은 모양이다(사용자 지시 11-06 · 나중에 AIDE 와 합친다).

  · 도구 정의(이름·설명·인자 스키마·읽기/미리보기/쓰기)는 `descriptions.py`, 이름 → 실행 함수는 `registry.py`
    (`@register(name)`), 한 번 실행의 단계(권한 → 문맥 주입 → 이름 확정 → 실행 → 기록)는 `middleware.py`.
  · 여기는 실행 함수와, 그 앞 단계에서 쓸 것 — 이름 확정(`GROUNDERS`)·문맥 주입 전에 정할 고칠 첫날(`CONTEXT_FIRST`).
  · LLM 이 고르는 도구(후보 제안·요청 고치기·조회 등)와 카드 버튼(적용·넘기기·다듬기·레버)은 같은 레지스트리에 있고,
    권한 단계가 부른 곳을 가른다. 쓰기는 적용 하나뿐이고 새 버전(draft)만 만든다.
"""

from __future__ import annotations

import dataclasses
import logging
import time
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Dict, List, Optional, Sequence, Tuple

from sqlalchemy.orm import Session

from schedule_edit_agent.answer import (
    SOURCE_WORD, answer_for, check_answer, compare_answer, describe_requests, fresh_notes, lookup_answer,
    postprocess_answer, revert_lead, status_text,
)
from schedule_edit_agent.descriptions import CHECK_TOPICS, LOOKUP_KINDS, REPLY_KINDS, UNSUPPORTED_TOPICS
from schedule_edit_agent.grounding import SHIFT_WORD, ground_changes, resolve_day, resolve_nurse, resolve_shift
from schedule_edit_agent.registry import register
from services.schedule_edit.apply import ApplyError, apply_to_new_version
from db.models import Schedule
from services.schedule_edit import versions
from services.schedule_edit.journal import record_event
from services.schedule_edit.postprocess import ANNUAL_LEAVE_SWAP, RUNNERS, as_result
from services.schedule_edit.postprocess import KINDS as POSTPROCESS_KINDS
from services.schedule_edit.precheck import (
    BLOCKED_HEAD, Finding, Precheck, apply_findings, finding_id, finding_to_json, precheck,
)
from services.schedule_edit.rule_check import ALL as CHECK_ALL
from services.schedule_edit.requests import (
    PLAN, SHAPE_COUNTED, SHAPE_LABEL, TRANSFERS, BalanceTarget, CountTarget, CoverTarget, EditRequest, GiveUpWish,
    GradeFloor, SetCell, ShapeTarget, StaffingPlan, WishTarget, balance_counts, count_cells, eul, eun, fixed_in_plan, grade_gaps,
    is_satisfied, plan_ambiguous, plan_capacity, plan_label, plan_people, plan_sig, shape_count, staffing_bounds,
    staffing_gaps,
    balance_members, count_in, days_text, edit_from_note, first_open_day, transfer_label, wish_count, wish_targets,
    work_down, work_targets,
)
from services.schedule_edit.serialize import allow_to_json, lever_to_dict, requests_to_json, result_to_dict
from services.schedule_edit.levers import find_levers, lever_allow
from services.schedule_edit.session import EditSession, allow_lever_id, keep, propose, reject, stage
from services.schedule_edit.solver import (
    BALANCE_MAX_CHANGES, HOLD_CONSENT, SHAPE_MAX_CHANGES, WISH_MAX_CHANGES, EditResult, apply_candidate,
    max_wish_count, min_balance_spread, min_shape_count, prepare, probe_plan,
    propose_edits,
)

logger = logging.getLogger(__name__)

#: 채팅 한 번의 응답 시간 상한(초) — CloudFront 30초 안에 들어와야 한다. 경로에 들어온 순간부터 센다
#: (슬롯 대기·판정 재료 로드·LLM·솔버 모두 이 안 · 검토 HIGH-2). 솔버 예산은 남은 시간이다.
CHAT_BUDGET = 25.0
#: 남은 시간이 이보다 적으면 풀지 않고 '다시 보내 주세요' — 기한을 넘겨 솔버를 돌리지 않는다
MIN_SOLVE = 3.0
OUT_OF_TIME = ("이번 말씀은 아직 반영되지 않았습니다 — 응답 시간 안에 찾기에 시간이 부족했습니다. "
               "같은 말을 한 번 더 보내 주세요.")
#: 바꾸기 전 확인의 마지막 줄
CONFIRM_ASK = "그래도 진행할까요? 진행해도 이번 수정에만 해당하고 병동 설정은 바뀌지 않습니다."


@dataclass
class Turn:
    db: Session
    user: Any
    conversation_id: str
    turn_no: int
    session: EditSession
    today: date
    deadline: float
    #: 적용으로 근무표가 바뀌면 새 schedule_id — 대화가 그 버전으로 옮겨 간다
    switched_to: Optional[str] = None
    #: 대상이 바뀌어도 이어 가는 값(`Conversation.carry`) — 고칠 수 있는 첫날 (년, 월, 0부터)
    carry: Dict[str, Any] = field(default_factory=dict)
    #: 이번 답에 붙일 사실(앞 요청을 새 첫날에 맞추며 생긴 것 · 참고 문구로 처음 한 번만)
    notes: List[str] = field(default_factory=list)
    #: 이번 실행의 단계 기록(`middleware.MiddlewareStep`) — 첫 결과 기록의 payload 에 steps 로 한 번 붙는다
    trace: Optional[List[Any]] = None
    traced: bool = False

    def remaining(self) -> float:
        return self.deadline - time.monotonic()

    def open_from(self) -> int:
        """바꿀 수 있는 첫날(0부터) — 오늘까지는 닫고, 사용자가 고칠 첫날을 정했으면(ctx.edit_from) 그 앞도 닫는다."""
        return first_open_day(self.session.ctx, self.today)

    def record(self, event: str, payload: Dict[str, Any], *, elapsed_ms: Optional[int] = None,
               new_schedule_id: Optional[str] = None) -> None:
        if self.trace is not None and not self.traced and event != "lock":
            # 고칠 첫날 기록(lock)은 첫날만 담는 칸이라 붙이지 않는다 — 재기동 복원이 그 칸을 그대로 읽는다
            payload, self.traced = {**payload, "steps": [step.to_dict() for step in self.trace]}, True
        record_event(self.db, self.session.schedule_id, event, payload,
                     created_by=getattr(self.user, "account_id", None), new_schedule_id=new_schedule_id,
                     conversation_id=self.conversation_id, turn_no=self.turn_no, elapsed_ms=elapsed_ms)


@dataclass
class Outcome:
    answer: str
    preview: Optional[Dict[str, Any]] = None
    #: 화면에 일으킬 일(agent_kit 효과 어휘) — 적용 뒤 버전 바꾸기
    effects: List[Dict[str, Any]] = field(default_factory=list)
    #: 화면에서 펼쳐 둘 후보 번호(이 안에서 다듬기)
    focus: Optional[int] = None
    #: 버튼이 지금 상태와 안 맞아 아무것도 안 했다 — 흔적에 남기지 않는다
    failed: bool = False


# ── LLM 이 고르는 스킬 ───────────────────────────────────────────────────────


@register("propose_edit")
def propose_edit(turn: Turn, params: Dict[str, Any]) -> Outcome:
    """고칠 첫날(edit_from_day)은 문맥 주입 단계가 먼저 정했고(`CONTEXT_FIRST`), 말한 요청은 이름 확정 단계가 근무표
    안에서 확정했다(`_ground_propose` — 이번 요청의 기간·검사가 새 기준을 쓴다)."""
    if not params.get("changes") and params.get("edit_from_day") is not None:
        return _locked(turn)
    return _propose(turn, params["grounded"]["requests"])


def _ground_propose(turn: Turn, params: Dict[str, Any]) -> Tuple[Optional[Dict[str, Any]], List[str]]:
    """기준일만 정한 말('5일까지는 고정해')은 확정할 요청이 없다 — 실행이 `_locked` 로 답한다."""
    if not params.get("changes") and params.get("edit_from_day") is not None:
        return None, []
    grounded = ground_changes(turn.session.ctx, params.get("changes") or [], turn.open_from(), today=turn.today)
    return {"requests": grounded.requests}, list(grounded.asks)


@register("revise_edit")
def revise_edit(turn: Turn, params: Dict[str, Any]) -> Outcome:
    """고칠 첫날은 문맥 주입 단계가, 더할 요청·뺄 칸·둘 칸은 이름 확정 단계가 먼저 정했다(`_ground_revise`).
    기준일만 정한 말이 앞 요청을 다시 찾을 때(`_locked`)는 확정할 것이 없어 여기서 같은 확정을 부른다."""
    ctx = turn.session.ctx
    if not turn.session.requests:
        if params.get("edit_from_day") is not None and not any(params.get(k) for k in ("add", "remove", "keep")):
            return _locked(turn)
        return _clarify(turn, ["고칠 앞 요청이 없습니다. 무엇을 바꿀지 처음부터 말씀해 주세요."])
    open_from = turn.open_from()
    grounded = params.get("grounded") or _ground_revise(turn, params)[0]
    added, removed, kept = grounded["add"], grounded["removed"], grounded["kept"]
    # 날만 말한 빼기('15일 요청 빼') — 그날 인원 요청도 같이 뺀다(사람을 짚지 않아 칸으로는 안 걸린다 · 검토 11-03)
    removed_days = _whole_days(ctx, removed)
    moved, facts = _from_open(ctx, turn.session.requests, open_from)
    turn.notes += facts
    reqs = [r for r in moved
            if (r.nurse_id, r.day) not in removed and (r.nurse_id, None) not in removed
            and not (isinstance(r, CoverTarget) and r.day in removed_days)]
    if params.get("transfer") in TRANSFERS:
        # 말로 바꾼 방식은 이미 고른 방식도 덮어쓴다('아니 같은 팀으로' · 검토 L4) — 버튼은 정하지 않은 것만
        reqs = [_decided(r, params["transfer"], force=True) for r in reqs]
    for r in added:
        old = next((x for x in reqs if isinstance(x, StaffingPlan)), None)
        if isinstance(r, StaffingPlan) and old is not None and len(r.alternatives) == 1:
            r = _patched(old, r)                  # '평일 D 는 3~4명' — 앞 계획의 모든 안에 덧붙인다
        # 같은 대상 요청은 새 것이 덮어쓴다('A 15일 E' 뒤 'A 15일 연차' · 검토 L1)
        reqs = [x for x in reqs if not _same_target(x, r)]
        if r not in reqs:
            reqs.append(r)
    # '21일은 그대로 둬' — 요청한 칸이 그 날이면 그 요청을 뺀다(평가 11-03: 요청은 남기고 그날 다른 칸만 고정해 모순).
    # 그날 전체를 두라면 그날 인원 요청도 뺀다(남기면 그날을 다 묶은 채 인원을 바꾸라는 모순 · 검토 11-03)
    kept_days = _whole_days(ctx, kept)
    reqs = [r for r in reqs if isinstance(r, (CountTarget, BalanceTarget, WishTarget, ShapeTarget, *PLAN)) or _is_keep(ctx, r)
            or (r.day not in kept_days if isinstance(r, CoverTarget) else (r.nurse_id, r.day) not in kept)]
    asked = {(r.nurse_id, r.day) for r in reqs}
    for n, d in sorted(kept - asked):
        if ctx.is_free(n, d) and d >= open_from:     # 고정 칸·고치지 않는 날은 원래 안 바뀐다
            reqs.append(SetCell(n, d, ctx.main[n][d]))
    if not any(not _is_keep(ctx, r) for r in reqs):
        # 날을 정한 턴이면 정한 날 안내를 앞에 — 남은 요청이 없다는 말만으로는 날이 정해졌는지 모른다(검토 11-05 LOW-3)
        note = edit_from_note(ctx, open_from, turn.today)
        return _clarify(turn, fresh_notes(turn.session, [note] if note else [])
                        + ["남은 변경 요청이 없습니다. 무엇을 바꿀지 말씀해 주세요."])
    return _propose(turn, reqs)


def _ground_revise(turn: Turn, params: Dict[str, Any]) -> Tuple[Optional[Dict[str, Any]], List[str]]:
    """더할 요청·뺄 칸·둘 칸 — 앞 요청이 없으면 확정하지 않는다(실행이 '고칠 앞 요청이 없습니다'로 먼저 답한다)."""
    if not turn.session.requests:
        return None, []
    ctx = turn.session.ctx
    asks: List[str] = []
    add = ground_changes(ctx, params["add"], turn.open_from(), today=turn.today) if params.get("add") else None
    if add is not None:
        asks += add.asks
    removed = _removals(ctx, params.get("remove") or [], asks)
    kept = _cells(ctx, params.get("keep") or [], asks)
    return {"add": list(add.requests) if add is not None else [], "removed": removed, "kept": kept}, asks


def _lock(turn: Turn, params: Dict[str, Any]) -> Optional[str]:
    """'5일까지는 그대로'·'6일부터 고쳐' — 고칠 수 있는 첫날을 정한다(사용자 결정 11-05). 되물을 문장, 없으면 None.
    대화 내내 이어 간다(`Conversation.carry` → `EditAgent.session_for`). 바뀌면 기록한다 — 재기동 뒤 `store.restore` 가
    마지막 값을 되살린다."""
    said = params.get("edit_from_day")
    if said is None:
        return None
    ctx = turn.session.ctx
    try:
        day = int(said)
    except (TypeError, ValueError):
        return "고칠 수 있는 첫날을 숫자로 알려 주세요. 예) '6일부터 고쳐줘'"
    if not 1 <= day <= ctx.days:
        return f"고칠 수 있는 첫날을 {ctx.month}월 안의 날짜로 알려 주세요. 예) '6일부터 고쳐줘'"
    if ctx.edit_from != day - 1:
        ctx.edit_from = day - 1
        turn.record("lock", {"edit_from_day": day, "year": ctx.year, "month": ctx.month})
    turn.carry["edit_from"] = (ctx.year, ctx.month, ctx.edit_from)     # 적용 뒤 새 버전에서도(대상이 바뀌면 state 는 버린다)
    return None


def _locked(turn: Turn) -> Outcome:
    """기준일만 정한 말('5일까지는 고정해') — 앞 요청이 있으면 새 첫날로 다시 찾는다(고쳐 말하기 `revise_edit` 와 같은 길 ·
    옛 후보 카드가 남지 않게). 앞 요청이 없을 때만 정해진 답으로 끝낸다."""
    if turn.session.requests:
        return revise_edit(turn, {})
    ctx = turn.session.ctx
    open_from = turn.open_from()
    note = edit_from_note(ctx, open_from, turn.today)
    turn.session.shown_notes.add(note)          # 다음 제안 답에 같은 참고 문구를 다시 붙이지 않는다
    return _clarify(turn, [note if open_from >= ctx.days else f"{note} 바꿀 내용을 말씀해 주세요."])


def _from_open(ctx, reqs: List[EditRequest], open_from: int) -> Tuple[List[EditRequest], List[str]]:
    """앞 요청을 지금 고칠 수 있는 첫날에 맞춘다 — 기준일을 새로 정하면('5일까지는 그대로') 기간 요청은 그날부터로
    줄이고(개수 요청은 닫힌 날에 이미 있는 수를 목표에서 빼 합을 지킨다), 닫힌 날의 '그대로 두기'는 뺀다(원래 안 바뀐다).
    닫힌 날을 짚은 칸 요청·기간이 모두 닫힌 요청은 그대로 둬 요청 검사가 이유를 말하게 한다.
    반환: (맞춘 요청, 답에 붙일 사실) — 개수 요청의 닫힌 날에 이미 목표보다 많으면 바꿀 수 있는 몫을 0 으로 깎고 그 사실을 알린다
    (거절로 끝내지 않는다 · 검토 11-05 L1).
    정한 날은 앞뒤로 바뀔 수 있어 늘 원래 요청에서 다시 맞춘다 — 줄였던 요청은 원래 첫날·목표(`asked_*`)를 들고 있어, 날을
    다시 당기면 그만큼 되살아난다(Codex 11-05). 기간을 말하지 않은 원티드·근무 모양과 고르게 맞추기는 '고칠 수 있는 첫날부터'라
    새 첫날을 그대로 따른다(당기면 넓어진다). 개수 요청·기간을 말한 요청은 원래 범위 밖으로는 넓히지 않는다(말한 적 없는 날이다)."""
    out: List[EditRequest] = []
    facts: List[str] = []
    for r in reqs:
        if _is_keep(ctx, r) and r.day < open_from:
            continue
        if isinstance(r, (BalanceTarget, *PLAN)) or (isinstance(r, (WishTarget, ShapeTarget)) and not r.ranged):
            if open_from <= r.last_day and r.first_day != open_from:
                r = dataclasses.replace(r, first_day=open_from)
                if isinstance(r, BalanceTarget):
                    r = dataclasses.replace(r, members=balance_members(ctx, r.codes, open_from))
        elif isinstance(r, (WishTarget, ShapeTarget, CountTarget)):
            first = r.first_day if r.asked_first is None else r.asked_first
            start = max(first, open_from)
            if start > r.last_day:
                pass                         # 기간이 모두 닫혔다 — 그대로 둬 요청 검사가 이유를 말하게(되살릴 값도 그대로)
            elif not isinstance(r, CountTarget):
                r = dataclasses.replace(r, first_day=start, asked_first=None if start == first else first)
            else:
                target = r.target if r.asked_target is None else r.asked_target
                # 지금 근무표로 센다 — 적용하면 새 버전이라 앞 요청이 이어지지 않아(`session_for` 의 same) 닫힌 날 칸이 요청 때와 같다
                held = count_cells(ctx, ctx.main, r.nurse_id, r.codes, range(first, start))
                if held > target:
                    word = "·".join(SHIFT_WORD.get(c, c) for c in sorted(r.codes))
                    facts.append(f"{ctx.names.get(r.nurse_id, r.nurse_id)} {word}은(는) 고치지 않는 날"
                                 f"({days_text(first, start - 1)})에 이미 {held}개가 있어 그만큼은 남습니다.")
                cut = start != first
                r = dataclasses.replace(r, first_day=start, target=max(0, target - held),
                                        asked_first=first if cut else None, asked_target=target if cut else None)
        out.append(r)
    return out, facts


def _patched(old: StaffingPlan, new: StaffingPlan) -> StaffingPlan:
    """덧붙인 인원 줄 — 앞 계획의 안마다 뒤에 붙인다(같은 날·근무는 뒤 줄이 덮는다 · `staffing_bounds`). 고른 안·동의는 다시 정한다."""
    return StaffingPlan(tuple(alt + new.alternatives[0] for alt in old.alternatives), old.first_day, old.last_day,
                        count_fixed=new.count_fixed if new.count_fixed is not None else old.count_fixed)


def _removals(ctx, items, asks: List[str]) -> set:
    """빼기 — (간호사, 날) 또는 날 없이 (간호사, None)=그 간호사 요청 전부."""
    out = set()
    for item in items:
        if item.get("day") is None and item.get("nurse"):
            nurse, ask = resolve_nurse(ctx, item.get("nurse"))
            if ask:
                asks.append(ask)
            else:
                out.add((nurse, None))
        else:
            out |= _cells(ctx, [item], asks)
    return out


def _same_target(old: EditRequest, new: EditRequest) -> bool:
    """새 요청이 덮어쓸 앞 요청 — 칸 요청은 같은 칸, 그날 인원은 같은 날·근무·등급끼리. 개수 요청은 쌓는다."""
    if isinstance(old, CoverTarget) or isinstance(new, CoverTarget):
        return (isinstance(old, CoverTarget) and isinstance(new, CoverTarget)
                and (old.day, old.code, old.grade) == (new.day, new.code, new.grade))
    if isinstance(old, BalanceTarget) or isinstance(new, BalanceTarget):
        return isinstance(old, BalanceTarget) and isinstance(new, BalanceTarget) and old.codes == new.codes
    if isinstance(old, PLAN) or isinstance(new, PLAN):
        return type(old) is type(new)            # 인원 계획·등급 하한은 하나씩 — 새 것이 덮는다(덧붙임은 `_patched`)
    if isinstance(old, WishTarget) or isinstance(new, WishTarget):
        return isinstance(old, WishTarget) and isinstance(new, WishTarget)
    if isinstance(old, ShapeTarget) or isinstance(new, ShapeTarget):
        return isinstance(old, ShapeTarget) and isinstance(new, ShapeTarget) and old.kind == new.kind
    if isinstance(old, CountTarget) or isinstance(new, CountTarget):
        return False
    return (old.nurse_id, old.day) == (new.nurse_id, new.day)


def _whole_days(ctx, cells: set) -> set:
    """그날 모든 사람 칸이 들어 있는 날 — 날만 말한 빼기·두기."""
    return {d for _n, d in cells if d is not None and all((n, d) in cells for n in ctx.nurse_ids)}


def _decided(r: EditRequest, transfer: str, force: bool = False) -> EditRequest:
    if isinstance(r, CountTarget) and (r.transfer is None or force):
        return dataclasses.replace(r, transfer=transfer)     # 줄이기 전 원래 첫날·목표(asked_*)도 함께 이어 간다
    return r


def _cells(ctx, items, asks: List[str]) -> set:
    out = set()
    for item in items:
        day, ask = resolve_day(ctx, item.get("day"))
        if ask:
            asks.append(ask)
            continue
        if item.get("nurse"):
            nurse, ask = resolve_nurse(ctx, item.get("nurse"))
            if ask:
                asks.append(ask)
                continue
            out.add((nurse, day))
        else:
            out.update((n, day) for n in ctx.nurse_ids)
    return out


def _is_keep(ctx, r: EditRequest) -> bool:
    return isinstance(r, SetCell) and ctx.main[r.nurse_id][r.day] == r.code


#: 아직 못 하는 변경 — 답 문장과 처음 화면 안내가 같은 목록을 쓴다
UNSUPPORTED = "자동 연차전환 밖의 후처리(보건휴가·수면 OFF 등)"


def reply_text(kind: str, session: EditSession, topic: Optional[str] = None) -> str:
    ctx = session.ctx
    if kind == "unsupported" and topic in UNSUPPORTED_TOPICS:
        return UNSUPPORTED_TOPICS[topic]
    return {
        "who": "누구의 근무를 바꿀까요? 이름을 알려 주세요.",
        "when": "며칠의 근무인가요?",
        "what": "어떤 근무로 바꿀까요? (D·E·N·OFF)",
        "outside_month": f"이 근무표는 {ctx.year}년 {ctx.month}월입니다. {ctx.month}월 안의 날짜로 말씀해 주세요.",
        "settings": "인원·규칙·OFF 수 같은 설정은 여기서 바꿀 수 없습니다. 설정 화면에서 바꿔 주세요.",
        "unsupported": (f"그런 종류의 변경({UNSUPPORTED})은 "
                        "아직 여기서 못 합니다. 누구의 며칠 근무를 어떻게 바꿀지, 또는 누구의 어떤 근무를 몇 개로 할지로 "
                        "말씀해 주시면 찾아 드립니다."),
        "other": "여기서는 이 근무표의 칸을 바꾸는 요청만 도와드릴 수 있습니다.",
        "status": status_text(session),
    }.get(kind, "무엇을 바꿀지 다시 말씀해 주세요. 예) '김민지 14일 OFF', '15일 나이트 이영희랑 바꿔 줘'")


@register("reply_only")
def reply_only(turn: Turn, params: Dict[str, Any]) -> Outcome:
    kind = params.get("kind") if params.get("kind") in REPLY_KINDS else "unclear"
    answer = reply_text(kind, turn.session, params.get("topic"))
    turn.record("clarify", {"kind": kind, "asks": [answer], "answer": answer})
    return Outcome(answer=answer)


# ── 조회(근무표를 바꾸지 않는다 · 사용자 결정 11-03) ─────────────────────────────────────────



@register("answer_confirm")
def answer_confirm(turn: Turn, params: Dict[str, Any]) -> Outcome:
    session = turn.session
    if not session.asking:
        if session.last is not None and session.last.candidates:
            # 적용은 버튼으로만 — 말로 저장하지 않는다(쓰기는 적용 버튼 하나뿐)
            return Outcome(answer="적용은 카드의 버튼으로 해 주세요. 누르기 전까지 근무표는 바뀌지 않습니다.")
        return Outcome(answer=reply_text("unclear", session))
    return _answer(turn, go=bool(params.get("go")))


@register("lookup")
def lookup(turn: Turn, params: Dict[str, Any]) -> Outcome:
    grounded = params["grounded"]
    answer = lookup_answer(turn.session.ctx, params.get("kind"), grounded["nurse"], grounded["day"], grounded["code"])
    turn.record("clarify", {"lookup": params.get("kind"), "answer": answer})
    return Outcome(answer=answer)


def _ground_lookup(turn: Turn, params: Dict[str, Any]) -> Tuple[Optional[Dict[str, Any]], List[str]]:
    ctx = turn.session.ctx
    kind = params.get("kind")
    asks: List[str] = []
    nurse = day = code = None
    if kind in ("person_count", "person_day"):
        nurse, ask = resolve_nurse(ctx, params.get("nurse"))
        asks += [ask] if ask else []
    if kind in ("day_shift", "person_day"):
        day, ask = resolve_day(ctx, params.get("day"))
        asks += [ask] if ask else []
    if kind in ("day_shift", "person_count"):
        code, ask = resolve_shift(ctx, params.get("shift"))
        asks += [ask] if ask else []
    if kind not in LOOKUP_KINDS:
        asks.append("무엇을 찾을지 다시 말씀해 주세요. 예) '15일 나이트 누구야', '장원영 N 몇 개야'")
    return {"nurse": nurse, "day": day, "code": code}, asks




@register("check_rules")
def check_rules(turn: Turn, params: Dict[str, Any]) -> Outcome:
    session = turn.session
    topic = params.get("topic") if params.get("topic") in CHECK_TOPICS else CHECK_ALL
    last = session.last
    candidates = list(last.candidates) if last is not None and last.status == "ok" else []
    rank, focus = params.get("rank"), None
    if isinstance(rank, int) and candidates:
        if not 1 <= rank <= len(candidates):
            return Outcome(answer=f"후보는 {len(candidates)}개입니다. 1~{len(candidates)}안 중에서 말씀해 주세요.")
        candidates, focus = [candidates[rank - 1]], rank
    answer = check_answer(session, topic, candidates, turn.open_from())
    turn.record("clarify", {"check": topic, "rank": focus, "answer": answer})
    return Outcome(answer=answer, focus=focus)


# ── 생성 후처리(사용자 결정 11-03 — 만들 때 하는 후처리도 쓸 수 있는 범위) ─────────────────────


@register("run_postprocess")
def run_postprocess(turn: Turn, params: Dict[str, Any]) -> Outcome:
    kind = params.get("kind") if params.get("kind") in POSTPROCESS_KINDS else ANNUAL_LEAVE_SWAP
    started = time.monotonic()
    session = turn.session
    result = RUNNERS[kind](turn.db, session, turn.today)
    answer = postprocess_answer(session.ctx, result)
    # 솔버 요청과는 다른 일이다 — 지금 요청·넘긴 묶음·기준 안을 비우고 이 결과만 든다(적용은 다른 후보와 같은 길)
    session.requests, session.avoid_people, session.hint = [], [], None
    session.last = (as_result(session, result) if result.changes
                    else EditResult("already", reasons=[answer], source=kind))
    preview = result_to_dict(session.ctx, session.last) if result.changes else None
    turn.record("propose", {"requests": [], "postprocess": kind, "report": result.report, "held": result.held,
                            "result": preview, "answer": answer, "allow": allow_to_json(session.allow),
                            "avoid": []}, elapsed_ms=_ms(started))
    return Outcome(answer=answer, preview=preview)


# ── 버전 비교·되돌리기(11-04 · 사용자 결정 '통째는 열기·일부는 새 버전') ──────────────────────



@register("compare_versions")
def compare_versions(turn: Turn, params: Dict[str, Any]) -> Outcome:
    ctx = turn.session.ctx
    current, target, nurse = (params["grounded"][k] for k in ("current", "target", "nurse"))
    from services.schedule_compare_service import compare_schedules   # 화면 [버전 비교]와 같은 숫자
    data = compare_schedules(turn.db, target, current)
    answer = compare_answer(versions.label(target), versions.label(current), data, nurse, ctx.names.get(nurse or ""))
    # compare_schedules(그 버전, 지금) — from = 그 버전 값, to = 지금 값. 표 미리보기는 지금(before) → 그 버전(after)이고,
    # 그 버전에 값이 없는 칸은 표에 그릴 값이 없어 뺀다(목록·제목 수는 답 문장과 같게 전부 · 검토 11-04 HIGH)
    mine = [c for c in data.get("cells", []) if nurse is None or c["nurse_id"] == nurse]
    preview = {"status": "compare", "target": _version_brief(target), "label": versions.label(target),
               "cells": [{"nurse_id": c["nurse_id"], "name": c["nurse_name"] or c["nurse_id"],
                          "day": int(c["date"][8:10]), "before": (c.get("to") or {}).get("shift_id"),
                          "after": (c.get("from") or {}).get("shift_id")} for c in mine]}
    turn.record("clarify", {"compare": _version_brief(target), "nurse": nurse, "answer": answer})
    return Outcome(answer=answer, preview=preview)


@register("revert_version")
def revert_version(turn: Turn, params: Dict[str, Any]) -> Outcome:
    ctx = turn.session.ctx
    grounded = params["grounded"]
    current, target = grounded["current"], grounded["target"]
    if not _revert_names(params) and params.get("day") is None:
        return _open_version(turn, current, target)
    nurses, first, last = grounded["nurses"], grounded["first"], grounded["last"]
    other = versions.load_version_cells(turn.db, ctx, target.schedule_id)
    plan = versions.revert_plan(ctx, other, nurses, first, last, turn.open_from())
    who = "·".join(ctx.names.get(n, n) for n in nurses)
    when = (f"{first + 1}일" if first == last else f"{first + 1}~{last + 1}일") if params.get("day") is not None else ""
    lead = revert_lead(versions.label(target), " · ".join(p for p in (who, when) if p),
                       plan, [ctx.names.get(n, n) for n in plan.missing])
    if not plan.requests:
        turn.record("clarify", {"revert": _version_brief(target), "answer": lead})
        return Outcome(answer=lead)
    if len(plan.requests) > versions.REVERT_MAX_CELLS:
        # 칸 상한이 지정 칸 수만큼 커진다 — 한 턴에 풀 만큼으로(검토 11-04 M1)
        ask = (f"{versions.label(target)}에서 가져올 칸이 {len(plan.requests)}개라 한 번에 찾기엔 많습니다. 사람이나 기간을 "
               f"좁혀 주시거나, 통째로 돌아가려면 'VER{target.version}으로 되돌려줘'(그 버전 열기)라고 말씀해 주세요.")
        return _clarify(turn, [ask])
    outcome = _propose(turn, plan.requests)
    outcome.answer = lead + "\n" + outcome.answer
    return outcome


def _ground_compare(turn: Turn, params: Dict[str, Any]) -> Tuple[Optional[Dict[str, Any]], List[str]]:
    current, target, ask = _pick_version(turn, params)
    nurse = None
    if not ask and str(params.get("nurse") or "").strip():
        nurse, ask = resolve_nurse(turn.session.ctx, params.get("nurse"))
    return {"current": current, "target": target, "nurse": nurse}, [ask] if ask else []


def _revert_names(params: Dict[str, Any]) -> List[str]:
    return [str(n).strip() for n in params.get("nurses") or [] if str(n or "").strip()]


def _ground_revert(turn: Turn, params: Dict[str, Any]) -> Tuple[Optional[Dict[str, Any]], List[str]]:
    """버전을 먼저 — 못 고르면 그것만 되묻는다. 사람·기간을 말하지 않았으면 그 버전 열기라 더 확정하지 않는다."""
    ctx = turn.session.ctx
    current, target, ask = _pick_version(turn, params)
    if ask:
        return None, [ask]
    out: Dict[str, Any] = {"current": current, "target": target, "nurses": [], "first": 0, "last": ctx.days - 1}
    names = _revert_names(params)
    if not names and params.get("day") is None:
        return out, []
    nurses, asks = [], []
    for said in names:
        nid, ask = resolve_nurse(ctx, said)
        asks += [ask] if ask else []
        nurses += [nid] if nid and nid not in nurses else []
    first, last = 0, ctx.days - 1
    if params.get("day") is not None:
        first, ask = resolve_day(ctx, params.get("day"))
        asks += [ask] if ask else []
        last = first if first is not None else last
        if params.get("to_day") is not None:
            last, ask = resolve_day(ctx, params.get("to_day"))
            asks += [ask] if ask else []
    if asks:
        return None, asks
    if first > last:
        first, last = last, first
    out.update(nurses=nurses, first=first, last=last)
    return out, []


def _current_schedule(turn: Turn):
    return turn.db.query(Schedule).filter(Schedule.schedule_id == turn.session.schedule_id).first()


def _pick_version(turn: Turn, params: Dict[str, Any]):
    """(지금 버전 행, 고른 버전 행, 되물을 문장)."""
    current = _current_schedule(turn)
    if current is None:
        return None, None, "지금 근무표를 찾지 못했습니다."
    target, ask = versions.pick_version(turn.db, current, params.get("which"), number=params.get("number"),
                                        on=params.get("date"), name=params.get("name"))
    return current, target, ask


def _version_brief(s) -> Dict[str, Any]:
    return {"schedule_id": s.schedule_id, "version": int(s.version), "status": s.status or "draft",
            "name": s.name or ""}


def _open_version(turn: Turn, current, target) -> Outcome:
    """통째로 되돌리기 = 그 버전 열기 — 저장하지 않는다. 대화도 그 버전으로 옮겨 간다(적용과 같은 효과)."""
    turn.switched_to = target.schedule_id
    brief = _version_brief(target)
    turn.record("open", {"from": _version_brief(current), "to": brief})
    return Outcome(
        answer=(f"{eul(versions.label(target))} 엽니다. 지금 보던 {eun(versions.label(current))} 그대로 남습니다. "
                f"일부만 되돌리려면 '장원영만 VER{target.version}처럼 해줘'처럼 사람이나 날짜를 말씀해 주세요."),
        effects=[{"type": "switch_version", "opened": True, **brief}])


@register("open_version")
def open_version(turn: Turn, params: Dict[str, Any]) -> Outcome:
    current = _current_schedule(turn)
    target = next((v for v in (versions.versions_of(turn.db, current) if current is not None else [])
                   if v.schedule_id == params.get("schedule_id") and v.schedule_id != current.schedule_id), None)
    if target is None:
        raise SkillError("그 버전을 열 수 없습니다. 버전 목록에서 골라 주세요.")
    return _open_version(turn, current, target)


# ── 카드 버튼(LLM 없음) ──────────────────────────────────────────────────────



@register("apply_edit")
def apply_edit(turn: Turn, params: Dict[str, Any]) -> Outcome:
    """적용 전에 이 안으로 나빠지는 것(넘기는 설정·원티드 칸·근무 모양)을 모아, 앞에서 동의하지 않은 것이 있으면
    먼저 묻는다(사용자 지시 11-03). [이대로 적용](confirmed)이면 그대로 저장하고, 받아들인 것을 적용 기록에 남긴다."""
    cand = _candidate(turn, params)
    session = turn.session
    started = time.monotonic()
    found = apply_findings(session.ctx, session.rules, cand, session.requests)
    pending = [f for f in found if f.id not in session.confirmed]
    # [이대로 적용]은 그 카드에 보인 것만 받아들인다 — 낡은 카드 버튼으로 다른 안을 확인 없이 적용하지 않게(검토 L1)
    seen = set(params.get("seen") or [])
    if pending and not (params.get("confirmed") and {f.id for f in pending} <= seen):
        return _ask_apply(turn, cand, pending, started)
    try:
        saved = apply_to_new_version(turn.db, turn.session, cand, turn.user,
                                     request_text=str(params.get("request_text") or ""), name=params.get("name"),
                                     today=turn.today, conversation_id=turn.conversation_id, turn_no=turn.turn_no,
                                     accepted=[finding_to_json(f) for f in found])
    except ApplyError as exc:
        turn.record("error", {"action": "apply", "code": exc.code, "message": exc.message},
                    elapsed_ms=_ms(started))
        return Outcome(answer=exc.message, failed=True)
    turn.switched_to = saved.get("new_schedule_id")
    name = saved.get("new_name") or ""
    return Outcome(
        answer=f"새 버전으로 저장했습니다: {name}(VER{saved.get('new_version')}). 화면을 새 버전으로 바꿉니다.",
        effects=[{"type": "switch_version", "schedule_id": turn.switched_to,
                  "version": saved.get("new_version"), "status": saved.get("status", "draft"),
                  "name": name}])


def _ask_apply(turn: Turn, cand, pending: List[Finding], started: float) -> Outcome:
    session, ctx = turn.session, turn.session.ctx
    source = getattr(session.last, "source", None)
    which = f"{SOURCE_WORD.get(source, source)} 안" if source else f"{cand.rank}안"
    question = f"{which}을 적용하면 지금보다 나빠지는 것이 있습니다. 그래도 적용할까요?"
    answer = "\n".join([question] + [f"· {f.text}" for f in pending])
    preview = result_to_dict(ctx, session.last)
    # 참고 문구는 후보를 보일 때 이미 말했다 — 확인 질문에 다시 붙이지 않는다(4차 평가 11-03)
    preview.update({"status": "confirm_apply", "rank": cand.rank, "question": question, "notes": [],
                    "findings": [finding_to_json(f) for f in pending]})
    turn.record("check", {"rank": cand.rank, "findings": [finding_to_json(f) for f in pending]},
                elapsed_ms=_ms(started))
    return Outcome(answer=answer, preview=preview, focus=cand.rank)


@register("back_to_candidates")
def back_to_candidates(turn: Turn, params: Dict[str, Any]) -> Outcome:
    last = turn.session.last
    if last is None or not last.candidates:
        raise SkillError("돌아갈 후보가 없습니다. 바꿀 내용을 다시 말씀해 주세요.")
    rank = params.get("rank")
    return Outcome(answer="적용하지 않았습니다. 다른 안을 고르거나 바꿀 내용을 말씀해 주세요.",
                   focus=rank if isinstance(rank, int) and 1 <= rank <= len(last.candidates) else None)


@register("retry_request")
def retry_request(turn: Turn, params: Dict[str, Any]) -> Outcome:
    if not turn.session.requests:
        raise SkillError("다시 찾을 요청이 없습니다. 바꿀 내용을 말씀해 주세요.")
    return _propose(turn, turn.session.requests)


@register("confirm_request")
def confirm_request(turn: Turn, params: Dict[str, Any]) -> Outcome:
    pick = params.get("pick")
    return _answer(turn, go=True, pick=str(pick) if pick else None)


@register("cancel_request")
def cancel_request(turn: Turn, params: Dict[str, Any]) -> Outcome:
    out = _answer(turn, go=False)
    if params.get("restate"):
        out.answer = RESTATE_ASK
    return out


#: 인원 계획 확인의 [인원을 다시 말하기] — 근무표는 그대로 두고 새 인원을 기다린다(사용자 지시 11-05)
RESTATE_ASK = "근무표는 그대로입니다. 인원을 다시 말씀해 주세요. 예) '평일 D 3~4명·E 3명·N 3명', '주말 D 2명'"


@register("choose_staffing")
def choose_staffing(turn: Turn, params: Dict[str, Any]) -> Outcome:
    session = turn.session
    choice = params.get("choice_id")
    plan = next((r for r in session.requests if isinstance(r, StaffingPlan)), None)
    if plan is None:
        raise SkillError("고를 인원 계획이 없습니다. 다시 요청해 주세요.")
    if choice in ("count_fixed", "exclude_fixed"):
        if plan.count_fixed is not None:
            raise SkillError("고정 근무자를 셀지는 이미 정했습니다. 바꾸려면 말로 해 주세요.")
        changed = dataclasses.replace(plan, count_fixed=choice == "count_fixed")
    elif choice in ("exact", "at_least"):
        changed = _with_exactness(session.ctx, plan, choice == "exact")
        if changed == plan:
            raise SkillError("정확히·이상을 고를 인원이 없습니다. 바꾸려면 말로 해 주세요.")
    else:
        raise SkillError("보여 드린 선택지 중에서 골라 주세요.")
    turn.record("choose", {"choice_id": choice})
    return _propose(turn, [changed if r is plan else r for r in session.requests])


def _with_exactness(ctx, plan: StaffingPlan, exact: bool) -> StaffingPlan:
    """숫자만 말한 줄 중 지금 판보다 적어 결과가 달라지는 줄 — 모든 안에서 고른 대로(같은 말에서 나온 숫자다)."""
    alternatives = []
    for i, alt in enumerate(plan.alternatives):
        vague = {j for j, _r, _d in plan_ambiguous(ctx, dataclasses.replace(plan, chosen=i), i)}
        alternatives.append(tuple(dataclasses.replace(r, exact=exact) if j in vague else r for j, r in enumerate(alt)))
    return dataclasses.replace(plan, alternatives=tuple(alternatives))


@register("reject_candidate")
def reject_candidate(turn: Turn, params: Dict[str, Any]) -> Outcome:
    cand = _apply_only_guard(turn, _candidate(turn, params))
    reject(turn.session, cand)
    turn.record("reject", {"rank": cand.rank, "people": sorted(cand.affected)})
    return _propose(turn, turn.session.requests)


@register("keep_candidate")
def keep_candidate(turn: Turn, params: Dict[str, Any]) -> Outcome:
    cand = _apply_only_guard(turn, _candidate(turn, params))
    keep(turn.session, cand)
    turn.record("keep", {"rank": cand.rank, "people": sorted(cand.affected)})
    return Outcome(answer=f"{cand.rank}번 안을 기준으로 이어서 다듬습니다. 더 바꾸거나 그대로 둘 곳을 말씀해 주세요.",
                   focus=cand.rank)


@register("choose_lever")
def choose_lever(turn: Turn, params: Dict[str, Any]) -> Outcome:
    try:
        lever = allow_lever_id(turn.session, str(params.get("lever_id") or ""))
    except KeyError:
        return Outcome(answer="그 레버는 지금 결과에 없습니다. 다시 찾은 결과에서 골라 주세요.", failed=True)
    turn.record("lever", {"lever_id": params.get("lever_id"), "lose": lever.lose,
                          "allow": allow_to_json(lever.allow), "allow_total": allow_to_json(turn.session.allow)})
    # 원티드 반영은 넘긴 만큼 더 넣을 수 있다 — 넣을 수는 `_resolve_wish` 가 매번 다시 찾는다
    return _propose(turn, turn.session.requests)


@register("choose_transfer")
def choose_transfer(turn: Turn, params: Dict[str, Any]) -> Outcome:
    choice = params.get("choice_id")
    pending = next((r for r in turn.session.requests if isinstance(r, CountTarget) and r.transfer is None), None)
    if pending is None:
        raise SkillError("고를 개수 요청이 없습니다. 다시 요청해 주세요.")
    shown = _SHOW_DOWN if work_down(turn.session.ctx, pending) else _SHOW_UP
    if any(isinstance(r, PLAN) for r in turn.session.requests):
        shown = tuple(p for p in shown if p not in PLAN_LOCKED_TRANSFERS)
    if choice not in shown:                      # 보여 준 선택지만(방향과 맞지 않는 인원 완화 · 검토 P1)
        raise SkillError("보여 드린 선택지 중에서 골라 주세요.")
    turn.record("choose", {"choice_id": choice})
    return _propose(turn, [_decided(r, choice) for r in turn.session.requests])


# ── 앞 단계(`middleware`)가 쓰는 표 ─────────────────────────────────────────

#: 이름 확정 — (확정한 값 dict 또는 None, 되물을 문장). 확정한 값은 실행 인자 grounded 로 간다
GROUNDERS = {"propose_edit": _ground_propose, "revise_edit": _ground_revise, "lookup": _ground_lookup,
             "compare_versions": _ground_compare, "revert_version": _ground_revert}
#: 문맥 주입 단계에서 먼저 정할 것 — 사용자가 말한 고칠 수 있는 첫날(되물을 문장 또는 None)
CONTEXT_FIRST = {"propose_edit": _lock, "revise_edit": _lock}


# ── 공용 ────────────────────────────────────────────────────────────────────


class SkillError(Exception):
    """버튼 인자가 지금 상태와 안 맞는다(없는 후보 번호 등) — 사용자에게 그대로 보여 준다."""


def _apply_only_guard(turn: Turn, cand):
    """생성 후처리로 낸 안은 서비스 규칙 그대로라 적용만 — 다듬기·넘기기는 빈 요청으로 솔버를 돌려 엉뚱한 답을 낸다(검토 M1)."""
    if getattr(turn.session.last, "source", None):
        raise SkillError("이 안은 '적용'만 할 수 있습니다. 다른 방법이 필요하면 바꿀 내용을 말씀해 주세요.")
    return cand


def _candidate(turn: Turn, params: Dict[str, Any]):
    last = turn.session.last
    try:
        rank = int(params.get("rank"))
    except (TypeError, ValueError):
        raise SkillError("몇 번 안인지 알려 주세요.") from None
    if last is None or not 1 <= rank <= len(last.candidates):
        raise SkillError("그 후보는 지금 결과에 없습니다. 다시 찾아 주세요.")
    return last.candidates[rank - 1]


def _propose(turn: Turn, requests: List[EditRequest]) -> Outcome:
    session = turn.session
    started = time.monotonic()
    check = _precheck(turn, requests)
    if check.blocked:
        return _blocked(turn, requests, check, started)
    asking = check.pending(session.confirmed)
    if asking:
        return _ask(turn, requests, asking, started)
    session.asking, session.held = [], None
    planned = any(isinstance(r, PLAN) for r in requests)
    if planned:
        # 인원 계획과 함께 온 개수 요청 중 이미 그렇게 된 것은 메울 것이 없다 — 방식을 묻지 않는다
        requests = [_decided(r, "auto") if isinstance(r, CountTarget) and r.transfer is None
                    and is_satisfied(session.ctx, r) else r for r in requests]
    undecided = next((r for r in requests if isinstance(r, CountTarget) and r.transfer is None), None)
    if undecided is not None:
        return _choose_transfer(turn, requests, undecided)
    if turn.remaining() < MIN_SOLVE:
        turn.record("error", {"stage": "budget", "requests": requests_to_json(requests),
                              "remaining_ms": int(turn.remaining() * 1000)})
        return Outcome(answer=OUT_OF_TIME)
    if planned:
        requests, asked, info = _resolve_plan(turn, requests)
        if asked is not None:
            return asked
        probe = info.get("probe")
        result = propose(session, requests, today=turn.today, time_budget=turn.remaining() - 1,
                         start=probe.grid if probe is not None else None)
        if result.status == "ok":
            _plan_followup(turn, requests, result, info)
        return _finish(turn, requests, result, started)
    requests, why = _resolve_balance(turn, requests)
    requests, wish = _resolve_wish(turn, requests)
    requests, shape = _resolve_shape(turn, requests)
    # 원티드 반영은 '하나 더' 레버를 찾을 시간을 남긴다(검토 11-03 L2)
    budget = turn.remaining() if wish is None else max(MIN_SOLVE, turn.remaining() - WISH_LEVER_SECONDS - 2)
    result = propose(session, requests, today=turn.today, time_budget=budget,
                     start=(wish or shape or {}).get("grid"))
    if wish is not None and result.status != "ok" and wish["best"] > 1 and turn.remaining() >= MIN_SOLVE:
        # 그 수를 넣는 해가 화면 판정 등으로 다 빠졌다 — 하나 덜 넣는 수로 한 번 더(검토 11-03 M2)
        requests = [dataclasses.replace(r, at_least=wish["best"] - 1) if isinstance(r, WishTarget) else r
                    for r in requests]
        result = propose(session, requests, today=turn.today, time_budget=turn.remaining() - 1)
    loosen = {it["index"]: it["best"] + 1 for it in (shape or {}).get("items", [])
              if it["best"] is not None and it["best"] + 1 < it["now"]}
    if shape is not None and result.status != "ok" and loosen and turn.remaining() >= MIN_SOLVE:
        # 근무 모양도 같다 — 찾은 수로 후보가 다 빠지면 하나 덜 줄이는 수로 한 번 더
        requests = [dataclasses.replace(r, at_most=loosen[i]) if i in loosen else r for i, r in enumerate(requests)]
        result = propose(session, requests, today=turn.today, time_budget=turn.remaining() - 1)
    result.notes = list(result.notes) + why
    if wish is not None and result.status == "ok":
        _wish_followup(turn, requests, result, wish)
    if shape is not None and result.status == "ok":
        _shape_followup(turn, requests, result, shape)
    return _finish(turn, requests, result, started)


def _finish(turn: Turn, requests: List[EditRequest], result: EditResult, started: float) -> Outcome:
    ctx = turn.session.ctx
    preview = result_to_dict(ctx, result)
    # 답과 미리보기 모두 처음 한 번만(검토 L3) — 사용자가 정한 기준일 안내도 같은 관례로
    preview["notes"] = fresh_notes(turn.session, _open_notes(turn) + list(result.notes))
    answer = answer_for(ctx, requests, result, notes=preview["notes"])
    turn.record("propose", {"requests": requests_to_json(requests), "described": describe_requests(ctx, requests),
                            "result": preview, "answer": answer,
                            "allow": allow_to_json(turn.session.allow),
                            "avoid": [sorted(p) for p in turn.session.avoid_people]},
                elapsed_ms=_ms(started))
    return Outcome(answer=answer, preview=preview)


def _precheck(turn: Turn, requests: List[EditRequest], start: Optional[int] = None) -> Precheck:
    """바꾸기 전 산술 점검(`precheck`) — 점검이 깨져도 수정 흐름은 막지 않는다(풀이·적용 전 확인이 다시 본다)."""
    session = turn.session
    try:
        return precheck(session.ctx, session.rules, requests, open_from=turn.open_from(), allow=session.allow,
                        start=start)
    except Exception as exc:   # noqa: BLE001 — 기록만 남기고 점검 없이 이어 간다
        logger.exception("[schedule_edit] 사전 점검 실패")
        turn.record("error", {"stage": "precheck", "error": f"{type(exc).__name__}: {exc}"[:500]})
        return Precheck()


def _blocked(turn: Turn, requests: List[EditRequest], check: Precheck, started: float) -> Outcome:
    """넘길 수 없는 규칙에 반드시 걸린다 — 풀지 않고 이유만(전월 근무까지 넣어 셈한 것)."""
    session = turn.session
    stage(session, requests)
    session.asking, session.held = [], None
    session.last = EditResult("infeasible", reasons=[BLOCKED_HEAD] + check.blocked, notes=list(session.ctx.notes),
                              blocking=check.families)
    return _finish(turn, requests, session.last, started)


def _ask(turn: Turn, requests: List[EditRequest], asking: List[Finding], started: float) -> Outcome:
    """요청대로면 어떤 방법으로도 피할 수 없는 것을 먼저 알리고 동의를 묻는다(사용자 지시 11-03).
    묻기 전 상태는 들고 있다가 [바꾸지 않기]면 되돌린다."""
    session, ctx = turn.session, turn.session.ctx
    if session.held is None:
        session.held = (list(session.requests), session.last, list(session.avoid_people))
    stage(session, requests)
    session.asking = list(asking)
    question = (f"{describe_requests(ctx, requests)} — 바꾸기 전에 셈해 보니(전월 근무 포함) 어떤 방법으로 바꿔도 "
                "아래가 생깁니다.")
    answer = "\n".join([question] + [f"· {f.text}" for f in asking] + [CONFIRM_ASK])
    preview = {"status": "confirm", "question": question, "findings": [finding_to_json(f) for f in asking],
               "reasons": [], "notes": [], "candidates": [], "levers": [], "blocking": []}
    turn.record("propose", {"requests": requests_to_json(requests), "described": describe_requests(ctx, requests),
                            "result": preview, "answer": answer, "allow": allow_to_json(session.allow),
                            "avoid": [sorted(p) for p in session.avoid_people]}, elapsed_ms=_ms(started))
    return Outcome(answer=answer, preview=preview)


def _answer(turn: Turn, go: bool, pick: Optional[str] = None) -> Outcome:
    """[그래도 진행] — 동의한 것을 기억하고(레버 대상 설정은 그만큼 이번만 넘긴다) 푼다.
    [바꾸지 않기] — 묻기 전 상태로 돌아간다(근무표는 그대로)."""
    session = turn.session
    asking = session.asking
    if not asking:
        raise SkillError("지금 확인을 기다리는 요청이 없습니다. 바꿀 내용을 다시 말씀해 주세요.")
    if go and pick is not None:
        # 둘 중 하나를 고르는 확인('모자라는 칸 남기기' / '원티드 포기') — 고른 것만 받아들인다
        asking = [f for f in asking if f.id == pick]
        if not asking:
            raise SkillError("보여 드린 선택지 중에서 골라 주세요.")
    session.asking = []
    if not go:
        held, session.held = session.held, None
        if held is not None:
            session.requests, session.last, session.avoid_people = list(held[0]), held[1], list(held[2])
        else:
            session.requests, session.last = [], None
        turn.record("cancel", {"ids": [f.id for f in asking], "requests": requests_to_json(session.requests),
                               "allow": allow_to_json(session.allow),
                               "avoid": [sorted(p) for p in session.avoid_people]})
        kept = " 앞서 찾은 후보는 아래에 그대로 있습니다." if session.last is not None and session.last.candidates else ""
        return Outcome(answer="바꾸지 않았습니다. 근무표는 그대로입니다." + kept)
    for f in asking:
        session.confirmed.add(f.id)
        for key, amount in lever_allow(f.allow).items():
            session.allow[key] = session.allow.get(key, 0) + amount
    turn.record("confirm", {"ids": [f.id for f in asking], "accepted": [f.text for f in asking],
                            "allow_total": allow_to_json(session.allow)})
    # 되돌릴 지점(held)은 풀이에 들어갈 때 비운다 — 동의 뒤 다시 물으면 처음 상태로 돌아가야 한다(검토 M3)
    return _propose(turn, session.requests)


# ── 여러 날 인원·등급 계획(32장 · 사용자 지시 11-05 '판단이 갈리면 묻는다') ─────────────────────────

#: 동의로 넘기는 양의 위치 — 인원 계획의 모자라는 칸 · 등급 하한의 부족 근무조(`levers.CONSENT_FAMILIES`)
#: 동의 종류 — 열쇠는 (종류, 계획 구분값, None, None) · 원티드 포기는 (종류, 간호사, 날, 계획 구분값) · 묶은 품질 규칙 넘김은
#: (HOLD_CONSENT, 계획 구분값, *규칙 위치). 계획 구분값(`requests.plan_sig`)이 들어 그 계획에만 걸린다(검토 H2)
SHORT, GRADE, GIVE_UP = "인원부족", "등급부족", "원티드포기"


def consent_key(kind: str, sig: str) -> Tuple:
    return (kind, sig, None, None)


#: 계획 하나를 미리 풀어 보는 시간 상한(초)과 남은 시간 중 쓸 몫 — 모자라는 칸·부족 근무조를 끝까지 확인해야 묻는 수가 맞다
#: (작업자 2 실측: 6초로는 0 인 등급 부족을 1로 보고 물었다)
PLAN_PROBE_SECONDS = 10.0
PLAN_PROBE_SHARE = 0.5
#: 모자라는 칸·부족 근무조를 문장에 몇 개까지 보일지(나머지는 수만)
PLAN_SHOWN = 12
_HOLD_WORD = {"연속OFF": "연속 OFF", "패턴": "근무 순서", "고립OFF": "하루짜리 OFF"}
#: 미리 풀기가 시간 안에 안 끝났을 때 — 얼마나 모자라는지 모르니 안을 내지 않는다(검토 H1)
PLAN_TIMEOUT = ("시간 안에 확인하지 못했습니다 — 모자라는 칸·등급 하한을 끝까지 셈하지 못해 안을 내지 않았습니다. 근무표는 "
                "바뀌지 않았습니다. 같은 요청으로 다시 찾거나 요청을 나눠 주세요.")


def _resolve_plan(turn: Turn, requests: List[EditRequest]) -> Tuple[List[EditRequest], Optional[Outcome], Dict[str, Any]]:
    """인원 계획·등급 하한을 풀기 전에 정할 것 — 요청에 답이 있으면 그대로, 없으면 엔진이 고르지 않고 묻는다(사용자 지시 11-05).
      1. 계획한 근무를 하는 고정 근무자가 있는데 셀지 정하지 않았으면 [센다]/[빼고 센다].
      2. 안마다 차례대로 — 산술(필요 > 근무 가능)이나 풀어 보니 모자라면 다음 안(요청에 있으니 묻지 않는다). 숫자만 말한 줄이
         지금 판보다 적어 '정확히/이상'에 따라 결과가 갈리면 묻는다. 한 번 풀어 보고(`probe_plan`) 묶은 품질 규칙을 넘겨야 하면 ·
         마지막 안도 모자라면(반영된 원티드를 포기하면 모자람 0 이 되는지도 함께) · 등급 하한이 모자라면 그것을 보이고 동의를
         묻는다(동의 전에는 그런 안을 내지 않는다). 아예 안 풀리면 원티드 포기·레버를, 시간 안에 못 셈하면 안을 내지 않는다.
    동의는 그 계획에만 걸린다(계획 구분값 · 검토 H2). 반환: (풀 요청, 물을 답 또는 None, 답에 쓸 정보)."""
    session, ctx = turn.session, turn.session.ctx
    plan = next((r for r in requests if isinstance(r, StaffingPlan)), None)
    grade = next((r for r in requests if isinstance(r, GradeFloor)), None)
    if plan is not None and plan.count_fixed is None:
        fixed = fixed_in_plan(ctx, plan)
        if fixed:
            return requests, _ask_fixed(turn, requests, fixed), {}
    sig = plan_sig(requests)
    allowed = {kind: session.allow.get(consent_key(kind, sig), 0) for kind in (SHORT, GRADE)}
    requests = _given_up(ctx, requests, {(k[1], k[2]) for k in session.allow
                                         if k[0] == GIVE_UP and len(k) > 3 and k[3] == sig})
    # 원티드 반영은 계획과 함께면 모두 넣는다('신청 오프 유지' · 32장) — 가장 많이 넣을 수를 따로 찾지 않는다.
    # 넣을 수는 늘 남은 대상으로 다시 정한다(포기한 원티드를 뺀 뒤 옛 수가 남으면 다시 막힌다)
    requests = [dataclasses.replace(r, at_least=len(wish_targets(ctx, r))) if isinstance(r, WishTarget) else r
                for r in requests]
    held = {tuple(k[2:]): v for k, v in session.allow.items() if k and k[0] == HOLD_CONSENT and len(k) > 2 and k[1] == sig}
    skipped: List[str] = []
    count = len(plan.alternatives) if plan is not None else 1
    for i in range(count):
        last = i == count - 1
        p_i = dataclasses.replace(plan, chosen=i, short=None) if plan is not None else None
        label = plan_label(plan.rules(i)) if plan is not None else ""
        arithmetic = False
        if p_i is not None:
            need, cap = plan_capacity(ctx, p_i, requests=requests)
            if need > cap:
                arithmetic = True
                why = f"{label}은 필요 {need}칸 > 근무 가능 {cap}칸(쉬는 날·휴가·바꿀 수 없는 칸 반영)"
                if not last:
                    skipped.append(why)
                    continue
                if allowed[SHORT] < need - cap:
                    # 넘기는 양은 산술로 모자라는 수만 — 풀어 보니 더 모자라면 아래에서 다시 묻는다(재검토 LOW-1)
                    key = consent_key(SHORT, sig)
                    finding = Finding(finding_id(key, need - cap), "모자라는 칸을 가장 적게 남기는 안으로 찾기", "plan",
                                      {key: need - cap - allowed[SHORT]})
                    head = f"{why} — 어떤 방법으로도 다 채울 수 없습니다."
                    return requests, _ask_consent(turn, requests, head, [finding], skipped, restate=True), {}
            vague = plan_ambiguous(ctx, p_i)
            if vague:
                return requests, _ask_exact(turn, requests, vague, skipped), {}
        g_i = None
        if grade is not None:
            g_i = dataclasses.replace(grade, m_days=_m_days(ctx, p_i), short=None,
                                      count_fixed=plan.count_fixed if plan is not None else grade.count_fixed)
        trial = [p_i if r is plan else g_i if r is grade else r for r in requests]
        if turn.remaining() < MIN_SOLVE:
            return requests, Outcome(answer=OUT_OF_TIME), {}
        probe = probe_plan(ctx, trial, rules=session.rules, today=turn.today,
                           seconds=min(PLAN_PROBE_SECONDS, max(1.0, turn.remaining() * PLAN_PROBE_SHARE)),
                           allow=session.allow, avoid_people=session.avoid_people)
        if probe.status == "infeasible":
            return requests, _plan_blocked(turn, requests, trial, skipped, label, sig), {}
        if probe.status == "timeout":
            return requests, _plan_stopped(turn, requests, skipped), {}
        if probe.status != "ok":
            return trial, None, {"skipped": skipped, "label": label}     # 잘못된 요청은 요청 검사가 그대로 알린다
        bends = {k: v - held.get(k, 0) for k, v in probe.hold.items() if v > held.get(k, 0)}
        if bends:
            findings = [Finding(finding_id((HOLD_CONSENT, sig) + k, v), _hold_text(ctx, k, v), "plan",
                                {(HOLD_CONSENT, sig) + k: v})
                        for k, v in sorted(bends.items(), key=lambda kv: (str(kv[0][0]),
                                                                          kv[0][2] if isinstance(kv[0][2], int) else 0))]
            head = (f"{label or describe_requests(ctx, requests)}대로 하려면 지금보다 나빠지지 않게 묶은 근무 모양"
                    "(연속 OFF·근무 순서·하루짜리 OFF) 중 아래를 넘겨야 합니다.")
            return requests, _ask_consent(turn, requests, head, findings, skipped), {}
        # 끝까지 확인하지 못했으면 단정하지 않는다 — '시간 안에 찾은 방법은'(원티드 반영과 같은 규약 · 검토 11-03 M3)
        found = "" if probe.proven else "시간 안에 찾은 방법으로는 "
        unsure = "" if probe.proven else " 더 줄일 수 있는지 끝까지 확인하지 못했습니다."
        short = sum(x for _d, _c, x in probe.short)
        if short and not last and not arithmetic:
            skipped.append(f"{label}은 {found or '풀어 보니 '}{short}칸이 모자랍니다({_gap_text(probe.short)})")
            continue
        if short > allowed[SHORT]:
            key = consent_key(SHORT, sig)
            findings = [Finding(finding_id(key, short), f"모자라는 {short}칸을 남기는 안으로 찾기", "plan",
                                {key: short - allowed[SHORT]})]
            choices = [{"id": findings[0].id, "label": "모자라는 칸 남기고 찾기"}]
            give = _give_up_for_short(turn, trial, sig, held)
            if give is not None:
                findings.append(give)
                choices.append({"id": give.id, "label": "원티드 포기하고 찾기"})
            head = f"{label}로 하면 {found}{short}칸이 모자랍니다 — {_gap_text(probe.short)}.{unsure}"
            return requests, _ask_consent(turn, requests, head, findings, skipped, restate=True,
                                          choices=choices if len(findings) > 1 else None), {}
        gshort = sum(x for _d, _c, x in probe.grade)
        if gshort > allowed[GRADE]:
            key = consent_key(GRADE, sig)
            finding = Finding(finding_id(key, gshort), f"등급 하한이 모자라는 근무조 {gshort}곳을 남기는 안으로 찾기",
                              "plan", {key: gshort - allowed[GRADE]})
            head = f"{found}{describe_grade_short(grade)}을 다 채울 수 없습니다 — {_gap_text(probe.grade)}.{unsure}"
            return requests, _ask_consent(turn, requests, head, [finding], skipped), {}
        final = [dataclasses.replace(p_i, short=short) if r is plan else
                 dataclasses.replace(g_i, short=gshort) if r is grade else r for r in requests]
        return final, None, {"skipped": skipped, "label": label, "probe": probe}
    return requests, None, {"skipped": skipped}


def _given_up(ctx, requests: List[EditRequest], cells: set) -> List[EditRequest]:
    """고른 '원티드 포기' — 넣을 원티드면 넣지 않고(WishTarget.skip), 지금 반영된 원티드면 그 칸을 짚은 것으로(GiveUpWish)."""
    if not cells:
        return requests
    targets = {(n, d) for r in requests if isinstance(r, WishTarget) for n, d, _c in wish_targets(ctx, r)}
    out = [dataclasses.replace(r, skip=tuple(sorted(set(r.skip) | (cells & targets)))) if isinstance(r, WishTarget)
           else r for r in requests if not isinstance(r, GiveUpWish)]
    kept = sorted(cells - targets, key=lambda c: (c[1], c[0]))
    return out + ([GiveUpWish(tuple(kept))] if kept else [])


def _give_up_finding(ctx, lost, sig: str, tail: str) -> Finding:
    """'원티드 n칸을 포기하면 …' — 고르면 그 칸들을 포기한다(그 계획에만 · 검토 H2)."""
    cells = [(n, d) for n, d, _c in lost]
    names = " · ".join(f"{ctx.names.get(n, n)} {d + 1}일 {'·'.join(SHIFT_WORD.get(c, c) for c in sorted(codes))}"
                       for n, d, codes in lost[:PLAN_SHOWN])
    more = f" 외 {len(cells) - PLAN_SHOWN}칸" if len(cells) > PLAN_SHOWN else ""
    return Finding(finding_id((GIVE_UP, sig, len(cells)), len(cells)),
                   f"원티드 {len(cells)}칸을 포기하면 {tail}: {names}{more}", "plan",
                   {(GIVE_UP, n, d, sig): 1 for n, d in cells})


#: 모자라는 칸이 남을 때 '원티드 포기하면 모자람 0' 을 찾는 시간 몫(사용자는 풀이가 한 번 더 도는 시간 증가를 알고 골랐다)
PLAN_GIVE_UP_SHARE = 0.4


def _give_up_for_short(turn: Turn, trial: List[EditRequest], sig: str, held: Dict[Tuple, int]) -> Optional[Finding]:
    """해는 있는데 모자라는 칸이 남는다 — 지금 반영된 원티드(넣을 원티드 포함)를 포기하면 모자람 0 이 되는지(사용자 결정 11-05).
    되면 가장 적게 포기하는 안 하나를 선택지로. 그 안도 묶은 품질 규칙을 더 넘겨야 하면 내지 않는다."""
    session, ctx = turn.session, turn.session.ctx
    if turn.remaining() < MIN_SOLVE:
        return None
    give = probe_plan(ctx, trial, rules=session.rules, today=turn.today,
                      seconds=max(1.0, turn.remaining() * PLAN_GIVE_UP_SHARE), allow=session.allow,
                      avoid_people=session.avoid_people, release_wishes=True, short_first=True)
    if give.status != "ok" or not give.wish_lost or give.short or any(v > held.get(k, 0) for k, v in give.hold.items()):
        return None
    return _give_up_finding(ctx, give.wish_lost, sig, "모자람 0")


def _plan_stopped(turn: Turn, requests: List[EditRequest], skipped: List[str]) -> Outcome:
    """미리 풀기가 시간 안에 안 끝났다 — 얼마나 모자라는지 모르니 안을 내지 않는다(검토 H1). [같은 요청으로 다시 찾기]로 잇는다."""
    session = turn.session
    stage(session, requests)
    session.last = EditResult("timeout", reasons=[PLAN_TIMEOUT] + [f"앞 계획은 안 됩니다: {x}." for x in skipped],
                              notes=list(session.ctx.notes))
    return _finish(turn, requests, session.last, time.monotonic())


#: 계획이 막혔을 때 레버를 찾는 시간 상한(초) — 칸 상한은 고칠 수 있는 칸 수(검토 L2)
PLAN_LEVER_SECONDS = 5.0


def _plan_blocked(turn: Turn, requests: List[EditRequest], trial: List[EditRequest], skipped: List[str],
                  label: str, sig: str) -> Outcome:
    """계획이 아예 안 풀린다(요청한 개수·원티드·규칙이 함께 안 맞는다) — '방법이 없습니다'와 함께 어떤 원티드를 포기하면
    되는지(고르면 그 칸을 짚은 것으로 보고 진행 · [RD-29])와 넘길 설정(레버 · 하드락은 넘기지 않는다)을 같은 자리에 보인다.
    둘 다 없으면 이유를 찾아 보인다(사용자 결정 11-05)."""
    from services.schedule_edit.explain import explain_month
    from services.schedule_edit.model import free_cells
    from services.schedule_edit.solver import PROBE_FREE_SHORT
    session, ctx = turn.session, turn.session.ctx
    head = f"{label or describe_requests(ctx, requests)}대로는 지금 규칙과 함께 정한 요청(개수·원티드 등)을 모두 지키며 풀 방법이 없습니다."
    findings: List[Finding] = []
    give = probe_plan(ctx, trial, rules=session.rules, today=turn.today,
                      seconds=max(1.0, turn.remaining() * PLAN_GIVE_UP_SHARE), allow=session.allow,
                      avoid_people=session.avoid_people, release_wishes=True)
    if give.status == "ok" and give.wish_lost:
        findings.append(_give_up_finding(ctx, give.wish_lost, sig, "찾을 수 있습니다"))
    # 레버는 '가능해지는' 설정을 찾는다 — 모자람은 뒤에서 따로 묻으니 넉넉히 두고, 칸 상한은 고칠 수 있는 칸 수까지(검토 L2)
    loose = [dataclasses.replace(r, short=PROBE_FREE_SHORT) if isinstance(r, PLAN) else r for r in trial]
    pctx, prules, preqs = prepare(ctx, session.rules, loose)
    start = min(r.day for r in preqs)
    cap = len(free_cells(pctx, range(start, pctx.days)))
    levers = []
    if turn.remaining() > MIN_SOLVE + 1:
        levers = find_levers(pctx, preqs, None, rules=prules, allow=session.allow, max_levers=2,
                             seconds=min(PLAN_LEVER_SECONDS, turn.remaining() - MIN_SOLVE), screen=session.screen,
                             avoid_people=session.avoid_people, max_changes=cap)
    reasons = [head]
    if not findings and not levers and turn.remaining() > MIN_SOLVE + 1:
        why = explain_month(pctx, prules, preqs, cap, min(PLAN_LEVER_SECONDS, turn.remaining() - MIN_SOLVE),
                            days=range(start, pctx.days), allow=session.allow)
        reasons += why.sentences(pctx.names, cap, list_bends=True)
    stage(session, requests)
    result = EditResult("infeasible", reasons=reasons, levers=levers, notes=list(ctx.notes))
    if findings:
        out = _ask_consent(turn, requests, head, findings, skipped, levers=levers, title="원티드를 포기하면 찾을 수 있습니다")
        session.last = result                          # 레버 카드를 고를 수 있게(choose_lever 는 마지막 결과의 레버만)
        return out
    session.last = result
    return _finish(turn, requests, result, time.monotonic())


def describe_grade_short(grade: Optional[GradeFloor]) -> str:
    return f"근무조마다 {grade.grade}등급까지 {grade.count}명" if grade is not None else "등급 하한"


def _m_days(ctx, plan: Optional[StaffingPlan]) -> Tuple[int, ...]:
    """등급 하한에 M 근무조를 더할 날 — 인원 계획에 M 이 있는 날(32장)."""
    if plan is None:
        return ()
    return tuple(sorted(d for d, by in staffing_bounds(ctx, plan).items() if by.get("M", (0, None))[0] > 0))


def _gap_text(gaps) -> str:
    """'12일 D 1 · 19일 E 1' — 앞 PLAN_SHOWN 개만."""
    shown = [f"{d + 1}일 {SHIFT_WORD.get(c, c)}" + (f" {x}" if x > 1 else "") for d, c, x in gaps[:PLAN_SHOWN]]
    return " · ".join(shown) + (f" 외 {len(gaps) - PLAN_SHOWN}곳" if len(gaps) > PLAN_SHOWN else "")


def _hold_text(ctx, key: Tuple, amount: int) -> str:
    """넘길 품질 규칙 한 곳 — '류예리 6일부터 연속 OFF 3일 넘음' · '허건 12일 하루짜리 OFF'."""
    family, nid, day = key[0], key[1], key[2]
    who = ctx.names.get(nid, nid)
    if family == "연속OFF":
        return f"{who} {day + 1}일부터 연속 OFF {key[3]}일 넘음(+{amount})"
    if family == "패턴":
        return f"{who} {day + 1}일부터 {key[3]} 순서(+{amount})"
    return f"{who} {day + 1}일 {_HOLD_WORD.get(family, family)}(+{amount})"


def _ask_fixed(turn: Turn, requests: List[EditRequest], fixed: List[Tuple[str, str]]) -> Outcome:
    """고정 근무자를 하루 인원에 셀지 — 기본값으로 고르지 않는다(사용자 지시 11-05)."""
    ctx = turn.session.ctx
    who: Dict[str, List[str]] = {}
    for n, c in fixed:
        who.setdefault(n, []).append(SHIFT_WORD.get(c, c))
    listed = " · ".join(f"{ctx.names.get(n, n)}(고정 {'·'.join(cs)})" for n, cs in who.items())
    question = f"{describe_requests(ctx, requests)} — 고정 근무자도 하루 인원에 셀까요? ({listed})"
    options = [{"choice_id": "count_fixed", "label": "센다", "summary": "고정 근무자의 근무도 그날 인원에 넣어 셉니다"},
               {"choice_id": "exclude_fixed", "label": "빼고 센다", "summary": "고정 근무자를 빼고 나머지로 인원을 맞춥니다"}]
    return _ask_choice(turn, requests, "fixed", question, options)


def _ask_exact(turn: Turn, requests: List[EditRequest], vague, skipped: Sequence[str] = ()) -> Outcome:
    """숫자만 말한 인원이 지금 판보다 적은 날이 있다 — 정확히 그 수인지, 그 수 이상인지에 따라 결과가 갈린다."""
    ctx = turn.session.ctx
    plan = next(r for r in requests if isinstance(r, StaffingPlan))
    lines = []
    for _i, rule, days in vague:
        where = {"all": "매일", "weekday": "평일", "weekend": "토일"}.get(rule.scope, "정한 날")
        lines.append(f"{where} {SHIFT_WORD.get(rule.code, rule.code)} {rule.low}명 — 지금 그보다 많은 날 {len(days)}일"
                     f"({'·'.join(str(d + 1) for d in days[:6])}일{' 등' if len(days) > 6 else ''})")
    question = "\n".join([f"{describe_requests(ctx, requests)} — 숫자만 말씀하신 인원을 정확히 그 수로 맞출지, 그 수 "
                          "이상이면 될지 골라 주세요. 지금 근무표에 그보다 많은 날이 있어 결과가 달라집니다."]
                         + [f"앞 계획은 안 돼 다음 계획으로 갑니다: {x}." for x in skipped])
    options = [{"choice_id": "exact", "label": "정확히 그 수", "summary": "많은 날은 그 수로 줄입니다"},
               {"choice_id": "at_least", "label": "그 수 이상", "summary": "최대는 병동 인원 설정대로 둡니다"}]
    return _ask_choice(turn, requests, "exact", question, options, lines, plan)


def _ask_choice(turn: Turn, requests: List[EditRequest], kind: str, question: str, options: List[Dict[str, Any]],
                lines: Sequence[str] = (), plan: Optional[StaffingPlan] = None) -> Outcome:
    """인원 계획에서 정할 것을 고르게 한다 — 요청만 바꿔 두고(`stage`) 기록한다(재기동 뒤에도 같은 질문으로 이어진다)."""
    session, ctx = turn.session, turn.session.ctx
    stage(session, requests)
    answer = "\n".join([question] + [f"· {x}" for x in lines]
                       + [f"{i}. {o['label']} — {o['summary']}" for i, o in enumerate(options, 1)])
    preview = {"status": "choose_plan", "kind": kind, "question": "\n".join([question] + [f"· {x}" for x in lines]),
               "choices": options, "reasons": [], "notes": [], "candidates": [], "levers": [], "blocking": []}
    turn.record("propose", {"requests": requests_to_json(requests), "described": describe_requests(ctx, requests),
                            "result": preview, "answer": answer, "allow": allow_to_json(session.allow),
                            "avoid": [sorted(p) for p in session.avoid_people]})
    return Outcome(answer=answer, preview=preview)


def _ask_consent(turn: Turn, requests: List[EditRequest], head: str, findings: List[Finding], skipped: List[str],
                 restate: bool = False, levers: Sequence = (), title: str = "이렇게 찾을까요?",
                 choices: Optional[List[Dict[str, str]]] = None) -> Outcome:
    """동의를 묻는다(바꾸기 전 확인과 같은 [그래도 진행]/[바꾸지 않기] · 인원은 [인원을 다시 말하기]도) — 앞 안이 안 된 이유도 먼저."""
    session, ctx = turn.session, turn.session.ctx
    if session.held is None:
        session.held = (list(session.requests), session.last, list(session.avoid_people))
    stage(session, requests)
    session.asking = list(findings)
    lead = [f"앞 계획은 안 됩니다: {x}." for x in skipped]
    question = "\n".join([f"{describe_requests(ctx, requests)} — {head}"] + lead)
    lever_line = ["또는 아래 설정 중 하나를 이번만 넘기도록 고르시면 그 안에서 다시 찾습니다."] if levers else []
    ask = ["둘 중 하나를 골라 주세요. 진행해도 이번 수정에만 해당하고 병동 설정은 바뀌지 않습니다."] if choices else [PLAN_CONFIRM_ASK]
    answer = "\n".join([question] + [f"· {f.text}" for f in findings] + lever_line + ask)
    preview = {"status": "confirm", "question": question, "findings": [finding_to_json(f) for f in findings],
               "title": title, "footnote": PLAN_CONFIRM_NOTE, "restate": restate, "choices": choices,
               "reasons": [], "notes": [], "candidates": [], "levers": [lever_to_dict(lv) for lv in levers],
               "blocking": []}
    turn.record("propose", {"requests": requests_to_json(requests), "described": describe_requests(ctx, requests),
                            "result": preview, "answer": answer, "allow": allow_to_json(session.allow),
                            "avoid": [sorted(p) for p in session.avoid_people]})
    return Outcome(answer=answer, preview=preview)


PLAN_CONFIRM_ASK = "이렇게 찾을까요? 진행해도 이번 수정에만 해당하고 병동 설정은 바뀌지 않습니다."
PLAN_CONFIRM_NOTE = "동의하시기 전에는 모자라는 칸·넘기는 근무 모양이 있는 안을 내지 않습니다."


def _plan_followup(turn: Turn, requests: List[EditRequest], result: EditResult, info: Dict[str, Any]) -> None:
    """찾은 안 — 쓴 계획과 앞 계획이 안 된 이유(산술 한 줄), 모자라는 칸, 등급 부족 근무조, 사람별 바뀐 칸, 날마다 인원."""
    ctx = turn.session.ctx
    grid = apply_candidate(ctx, result.candidates[0])
    plan = next((r for r in requests if isinstance(r, StaffingPlan)), None)
    grade = next((r for r in requests if isinstance(r, GradeFloor)), None)
    lines = [f"앞 계획은 안 돼 다음 계획으로 찾았습니다 — {x}." for x in info.get("skipped") or []]
    gave = [c for r in requests if isinstance(r, WishTarget) for c in r.skip]
    gave += [c for r in requests if isinstance(r, GiveUpWish) for c in r.cells]
    if gave:
        lines.append(f"포기한 원티드 {len(gave)}칸: " + " · ".join(f"{ctx.names.get(n, n)} {d + 1}일"
                                                             for n, d in sorted(gave, key=lambda c: (c[1], c[0]))))
    if plan is not None:
        gaps = staffing_gaps(ctx, grid, plan)
        lines.append(f"쓴 계획: {plan_label(plan.rules())}"
                     + (f" · 모자라는 칸 {sum(h - x for _d, _c, h, x in gaps)}: "
                        + _gap_text([(d, c, low - have) for d, c, low, have in gaps]) if gaps else " · 모자라는 칸 없음"))
    if grade is not None:
        g = grade_gaps(ctx, grid, grade)
        lines.append(f"{describe_grade_short(grade)} — " + (f"모자라는 근무조 {len(g)}: {_gap_text(g)}" if g
                                                            else "모든 근무조 채움"))
    per: Dict[str, int] = {}
    for ch in result.candidates[0].changes:
        per[ch.nurse_id] = per.get(ch.nurse_id, 0) + 1
    lines.append("사람별 바뀐 칸: " + " · ".join(f"{ctx.names.get(n, n)} {k}" for n, k in
                                              sorted(per.items(), key=lambda kv: (-kv[1], kv[0]))))
    if plan is not None:
        codes = [c for c in ("D", "E", "N", "M") if any(c in by for by in staffing_bounds(ctx, plan).values())]
        days = []
        for d in range(plan.first_day, plan.last_day + 1):
            people = plan_people(ctx, d, plan.count_fixed)
            counts = "·".join(str(sum(1 for n in people if grid[n][d] == c)) for c in codes)
            days.append(f"{d + 1}{'월화수목금토일'[ctx.day_date(d).weekday()]} {counts}")
        lines.append(f"날마다 인원({'·'.join(codes)}): " + " · ".join(days))
    result.extra = list(result.extra) + lines


#: 고르게 맞추기에서 가장 작은 차이를 찾는 데 쓰는 시간 상한(초) — 나머지로 후보를 찾는다
BALANCE_SECONDS = 6.0


def _resolve_balance(turn: Turn, requests: List[EditRequest]) -> Tuple[List[EditRequest], List[str]]:
    """차이를 정하지 않은 고르게 맞추기 — 줄일 수 있는 가장 작은 차이를 찾아 확정 조건으로 바꾼다.
    못 줄이면(지금 차이가 가장 작거나 시간 안에 못 찾음) 지금보다 1 작은 차이로 풀어 불가 이유·넘길 설정을 보인다.
    산술로 맞출 수 있는 차이(합을 사람 수로 나눈 나머지가 있으면 1, 없으면 0)보다 크면 왜인지 한 줄을 돌려준다."""
    pending = next((r for r in requests if isinstance(r, BalanceTarget) and r.spread is None and r.target is None),
                   None)
    if pending is None:
        return requests, []
    ctx = turn.session.ctx
    counts = list(balance_counts(ctx, ctx.main, pending).values())
    now, floor = max(counts) - min(counts), (0 if sum(counts) % len(counts) == 0 else 1)
    best, proven = min_balance_spread(ctx, requests, rules=turn.session.rules, today=turn.today,
                                      seconds=min(BALANCE_SECONDS, max(0.5, turn.remaining() / 3)),
                                      allow=turn.session.allow)
    spread = best if best is not None else max(0, now - 1)
    why = []
    if best is not None and best > floor:
        why.append(f"산술로는 차이 {floor}까지 맞출 수 있지만, 지금 규칙을 모두 지키며 바꾸는 칸 {BALANCE_MAX_CHANGES}개 안에서는 "
                   + (f"차이 {best}보다 줄일 방법이 없습니다." if proven
                      else f"시간 안에 차이 {best}보다 작은 방법을 찾지 못했습니다."))
    return [dataclasses.replace(r, spread=spread) if r is pending else r for r in requests], why


#: 원티드 반영에서 넣을 수 있는 가장 많은 수를 찾는 데 쓰는 시간 상한(초)과 '하나 더' 레버를 찾는 시간 상한
WISH_SECONDS = 6.0
WISH_LEVER_SECONDS = 4.0
#: 못 넣는 원티드의 이유를 몇 개까지 문장으로, 그 이유를 찾으려 점검할 칸 상한
WISH_REASONS = 3
WISH_REASON_TRIES = 10


def _resolve_wish(turn: Turn, requests: List[EditRequest]) -> Tuple[List[EditRequest], Optional[Dict[str, Any]]]:
    """넣을 수를 정하지 않은 원티드 반영 — 규칙을 모두 지키며 넣을 수 있는 가장 많은 수를 찾아 확정 조건으로 바꾼다
    (사용자 결정 11-03). 하나도 못 넣으면 1개로 풀어 불가 이유·넘길 설정을 보인다. 반환: (요청, {best, total, proven})."""
    pending = next((r for r in requests if isinstance(r, WishTarget)), None)
    if pending is None:
        return requests, None
    # 넣을 수는 늘 다시 찾는다 — 정해 둔 수가 남으면 넘기기·고쳐 말하기·확인 뒤에 '방법 없음'이 된다(검토 11-03 M1).
    # 사용자가 넣을 수를 정하는 말은 받지 않으므로 at_least 는 늘 이 함수가 정한 값이다
    requests = [dataclasses.replace(r, at_least=None) if r is pending else r for r in requests]
    pending = next(r for r in requests if isinstance(r, WishTarget))
    best, proven, total, grid = max_wish_count(turn.session.ctx, requests, rules=turn.session.rules,
                                               today=turn.today,
                                               seconds=min(WISH_SECONDS, max(0.5, turn.remaining() / 3)),
                                               allow=turn.session.allow,
                                               avoid_people=turn.session.avoid_people)
    if not total:
        return requests, None                   # 반영 안 된 원티드가 없다 — 풀이가 '이미 그렇게 되어 있음'으로
    at_least = best if best else 1
    info = {"best": best or 0, "total": total, "proven": proven, "grid": grid}
    return [dataclasses.replace(r, at_least=at_least) if r is pending else r for r in requests], info


#: 근무 모양에서 줄일 수 있는 가장 작은 개수를 찾는 시간 상한(초)
SHAPE_SECONDS = 6.0


def _resolve_shape(turn: Turn, requests: List[EditRequest]) -> Tuple[List[EditRequest], Optional[Dict[str, Any]]]:
    """줄일 수를 말하지 않은 근무 모양(하루짜리 OFF·퐁당퐁당·근무 순서) — 규칙을 지키며 줄일 수 있는 가장 작은 개수를 찾아
    상한으로 정한다(원티드 반영과 같은 틀 · 사용자 결정 11-04). 하나도 못 줄이면 하나 줄인 상한으로 풀어 불가 이유·넘길
    설정을 보인다. 정해 둔 수는 늘 다시 찾는다(넘기기·고쳐 말하기 뒤에 '방법 없음'이 되지 않게). 사용자가 말한 수(said)는
    그대로 둔다(검토 11-04 M1). 여럿이면 말한 순서대로 하나씩 — 앞에서 정한 수를 지킨 채 다음 것을 줄인다(M2 · 앞 요청이
    먼저 줄어 뒤 요청이 덜 줄 수 있다)."""
    pending = [i for i, r in enumerate(requests)
               if isinstance(r, ShapeTarget) and r.kind in SHAPE_COUNTED and not r.said]
    if not pending:
        return requests, None
    requests = [dataclasses.replace(r, at_most=None) if i in pending else r for i, r in enumerate(requests)]
    seconds = min(SHAPE_SECONDS, max(0.5, turn.remaining() / 3)) / len(pending)
    items, grid = [], None
    for i in pending:
        best, proven, now, found = min_shape_count(turn.session.ctx, requests, target=requests[i],
                                                   rules=turn.session.rules, today=turn.today, seconds=seconds,
                                                   allow=turn.session.allow, avoid_people=turn.session.avoid_people)
        if not now:                             # 바꿀 수 있는 칸에 그 모양이 없다 — 풀이가 '이미 그렇게 되어 있음'으로
            requests[i] = dataclasses.replace(requests[i], at_most=0)
            continue
        at_most = best if best is not None and best < now else now - 1
        requests[i] = dataclasses.replace(requests[i], at_most=at_most)
        items.append({"index": i, "best": best, "now": now, "proven": proven})
        grid = found or grid
    if not items:
        return requests, None
    return requests, {"items": items, "grid": grid}


def _shape_followup(turn: Turn, requests: List[EditRequest], result: EditResult, shape: Dict[str, Any]) -> None:
    """1안으로도 다 없애지 못하면 왜인지 한 줄씩 — 끝까지 확인했으면 단정, 아니면 '시간 안에'(원티드 반영과 같은 규약)."""
    ctx = turn.session.ctx
    grid = apply_candidate(ctx, result.candidates[0])
    lines = []
    for item in shape["items"]:
        req = requests[item["index"]] if item["index"] < len(requests) else None
        if not isinstance(req, ShapeTarget):
            continue
        left = shape_count(ctx, grid, req)
        if not left:
            continue
        why = (f"규칙을 모두 지키며 바꾸는 칸 {SHAPE_MAX_CHANGES}개 안에서는 더 줄일 방법이 없습니다." if item["proven"]
               else "시간 안에 더 줄이는 방법을 찾지 못했습니다.")
        lines.append(f"남은 {SHAPE_LABEL[req.kind]} {left}개는 {why}")
    result.extra = list(result.extra) + lines


def _wish_followup(turn: Turn, requests: List[EditRequest], result: EditResult, wish: Dict[str, Any]) -> None:
    """후보를 찾은 원티드 반영 — 못 넣는 원티드의 이유(요청 첫날부터 다 바꿀 수 있다고 봐도 하드락에 걸리는 것)와,
    설정 하나를 이번만 넘기면 하나 더 넣을 수 있는지(레버 카드 · 얻는 것 = 후보가 넣은 수 + 1)."""
    session, ctx = turn.session, turn.session.ctx
    req = next(r for r in requests if isinstance(r, WishTarget))
    grid = apply_candidate(ctx, result.candidates[0])
    targets = wish_targets(ctx, req)
    got = wish_count(ctx, grid, targets)
    left = [(n, d, codes) for n, d, codes in targets if grid[n][d] not in codes]
    blocked = []
    for n, d, codes in left[:WISH_REASON_TRIES]:          # 점검 횟수 상한(검토 11-03 L1)
        if len(blocked) >= WISH_REASONS:
            break
        check = _precheck(turn, [SetCell(n, d, sorted(codes)[0])], start=req.first_day)
        if check.blocked:
            blocked.append(check.blocked[0])          # '김태희 8일을 D로 하면 E→D 전환 금지에 걸립니다(7일 E …)'
    if left:
        rest = len(left) - len(blocked)
        # 끝까지 확인하지 못했으면 단정하지 않는다(검토 11-03 M3)
        why = ("다른 원티드·인원 규칙과 함께 맞출 수 없습니다." if wish["proven"]
               else "시간 안에 함께 넣는 방법을 찾지 못했습니다.")
        lines = [f"못 넣는 원티드 {len(left)}개:"] + [f"· {b}" for b in blocked]
        if rest:
            lines.append(f"· {'나머지 ' if blocked else ''}{rest}개는 {why}")
        result.extra = list(result.extra) + lines
    if not left or turn.remaining() < WISH_LEVER_SECONDS + 2:
        return
    more = [dataclasses.replace(r, at_least=got + 1) if r is req else r for r in requests]
    pctx, prules, preqs = prepare(ctx, session.rules, more)
    levers = find_levers(pctx, preqs, None, rules=prules, allow=session.allow, max_levers=2,
                         seconds=min(WISH_LEVER_SECONDS, turn.remaining() - 2), screen=session.screen,
                         avoid_people=session.avoid_people, max_changes=WISH_MAX_CHANGES)
    for lv in levers:
        lv.gain = f"원티드 {got + 1}개"
    result.levers = levers


#: 상대를 정하는 방식을 미리 풀 때 하나에 쓰는 시간 상한·하한(초)과, 미리 풀기 뒤 답을 만들 몫으로 남길 시간
PROBE_SECONDS = 3.0
PROBE_MIN = 0.8
PROBE_RESERVE = 1.5
#: 보여 주는 순서 — 기준안(가장 적게 바꾸기)은 맨 끝. 근무가 줄면 '그날 인원 줄이기', 늘면 '그날 인원 늘리기'
_SHOW_DOWN = ("same_grade", "same_team", "fair", "reduce_coverage", "auto")
_SHOW_UP = ("same_grade", "same_team", "fair", "add_coverage", "auto")


def _choose_transfer(turn: Turn, requests: List[EditRequest], req: CountTarget) -> Outcome:
    """개수 요청 — 상대를 어떻게 정할지 방식마다 미리 풀어 보여 주고 묻는다(사용자 결정 11-02).

    · 요청자의 근무가 줄면(N 빼기·OFF 늘리기) 빠지는 근무를 누가 메울지, 늘면(N 더·OFF 빼기) 누구 것을 넘겨받을지.
    · 가장 적게 바꾸는 안에서 상대가 없으면(다른 사람의 그 근무가 안 움직이면) 묻지 않는다.
    · 어느 방식도 안 되면 묻지 않고 '가장 적게 바꾸기'로 불가 이유·레버를 낸다. 모두 시간 부족이면 그렇게 안내.
    """
    ctx, session = turn.session.ctx, turn.session
    if any(isinstance(r, PLAN) for r in requests):
        return _choose_transfer_plain(turn, requests, req)
    if turn.remaining() < MIN_SOLVE:
        turn.record("error", {"stage": "budget", "requests": requests_to_json(requests)})
        return Outcome(answer=OUT_OF_TIME)
    started = time.monotonic()
    down = work_down(ctx, req)
    policies = [p for p in (_SHOW_DOWN if down else _SHOW_UP) if _applicable(ctx, req, p)]
    # auto 먼저(묻지 않아도 되는지) — 그다음 남은 방식을 남은 시간으로 나눠, 방식마다 다시 잰다(검토 L2)
    order = ["auto"] + [p for p in policies if p != "auto"]
    choices: Dict[str, Dict[str, Any]] = {}
    for i, p in enumerate(order):
        per = min(PROBE_SECONDS, (turn.remaining() - PROBE_RESERVE) / (len(order) - i))
        choices[p] = _probe(turn, requests, req, p, per) if per >= PROBE_MIN else _skipped(ctx, req, p)
        if p == "auto" and choices[p]["feasible"] and not choices[p]["partners"]:
            return _propose(turn, [_decided(r, "auto") for r in requests])
    if not any(c["feasible"] for c in choices.values()):
        if all(c.get("timed_out") for c in choices.values()):
            turn.record("error", {"stage": "budget", "requests": requests_to_json(requests)})
            return Outcome(answer=OUT_OF_TIME)          # '방법 없음' 과 다르다(검토 L3)
        return _propose(turn, [_decided(r, "auto") for r in requests])
    ordered = [choices[p] for p in policies if p in choices]
    _mark_same_as_auto(ordered)
    _recommend(ordered)
    for c in ordered:
        c.pop("_cells", None)
    stage(session, requests)
    word, moved = _word(req), abs(count_in(ctx, ctx.main, req) - req.target)
    ask = (f"빠지는 {word} {moved}개를 어떻게 메울지" if down
           else f"늘어나는 {word} {moved}개를 누구에게서 넘겨받을지")
    question = f"{describe_requests(ctx, requests)} — {ask} 골라 주세요."
    lines = [question] + [f"{i}. {_choice_line(c)}" for i, c in enumerate(ordered, 1)]
    notes = fresh_notes(session, _open_notes(turn) + list(ctx.notes))
    answer = "\n".join(lines + [f"참고: {n}" for n in notes])
    preview = {"status": "choose_transfer", "choices": ordered, "direction": "down" if down else "up",
               "question": question, "reasons": [], "notes": notes, "candidates": [], "levers": [],
               "blocking": []}
    turn.record("propose", {"requests": requests_to_json(requests), "described": describe_requests(ctx, requests),
                            "result": preview, "answer": answer, "allow": allow_to_json(session.allow),
                            "avoid": [sorted(p) for p in session.avoid_people]}, elapsed_ms=_ms(started))
    return Outcome(answer=answer, preview=preview)


#: 인원 계획과 함께면 고를 수 없는 메우는 방식 — 그날 인원은 계획이 정한다(사용자 결정 11-05)
PLAN_LOCKED_TRANSFERS = ("reduce_coverage", "add_coverage")


def _choose_transfer_plain(turn: Turn, requests: List[EditRequest], req: CountTarget) -> Outcome:
    """인원 계획과 함께 온 개수 요청 — 메우는 방식은 묻되 방식마다 미리 풀어 보지 않는다(계획 풀이가 무거워 다섯 번 풀면
    응답 시간을 넘는다 · 사용자 결정 11-05). 선택지만 보이고 고른 방식으로 한 번만 푼다. 그날 인원 줄이기·늘리기는 뺀다."""
    ctx, session = turn.session.ctx, turn.session
    down = work_down(ctx, req)
    choices = [{"choice_id": p, "label": transfer_label(ctx, req, p)}
               for p in (_SHOW_DOWN if down else _SHOW_UP)
               if p not in PLAN_LOCKED_TRANSFERS and _applicable(ctx, req, p)]
    stage(session, requests)
    word, moved = _word(req), abs(count_in(ctx, ctx.main, req) - req.target)
    ask = (f"빠지는 {word} {moved}개를 어떻게 메울지" if down
           else f"늘어나는 {word} {moved}개를 누구에게서 넘겨받을지")
    question = f"{describe_requests(ctx, requests)} — {ask} 골라 주세요(인원 계획과 함께라 미리 풀어 보지 않습니다)."
    answer = "\n".join([question] + [f"{i}. {c['label']}" for i, c in enumerate(choices, 1)])
    preview = {"status": "choose_transfer", "probed": False, "choices": choices,
               "direction": "down" if down else "up", "question": question, "reasons": [], "notes": [],
               "candidates": [], "levers": [], "blocking": []}
    turn.record("propose", {"requests": requests_to_json(requests), "described": describe_requests(ctx, requests),
                            "result": preview, "answer": answer, "allow": allow_to_json(session.allow),
                            "avoid": [sorted(p) for p in session.avoid_people]})
    return Outcome(answer=answer, preview=preview)


def _word(req: CountTarget) -> str:
    """사람에게 보일 근무 이름 — 근무 개수 요청이면 그 근무, OFF 요청이면 '근무'(빠지거나 느는 것은 근무다)."""
    work = set(req.codes) & {"D", "E", "N", "M"}
    return "·".join(SHIFT_WORD[c] for c in sorted(work)) if work else "근무"


def _applicable(ctx, req: CountTarget, policy: str) -> bool:
    if policy == "same_grade":
        return ctx.grade_of.get(req.nurse_id) is not None
    if policy == "same_team":
        return ctx.team_of.get(req.nurse_id) is not None
    return True


def _probe(turn: Turn, requests: List[EditRequest], req: CountTarget, policy: str, seconds: float) -> Dict[str, Any]:
    """그 방식으로 한 번 풀어 본다(후보 1개 · 불가 이유는 찾지 않음) — 세션은 바꾸지 않는다.

    상대(partners)는 요청 기간 안에서 센다 — fair 의 기준과 같은 단위(검토 N3).
    """
    ctx, session = turn.session.ctx, turn.session
    lower = req.target < count_in(ctx, ctx.main, req)
    if policy == "fair" and not _has_partner(ctx, req, lower):
        word = "OFF" if not (set(req.codes) & {"D", "E", "N", "M"}) else _word(req)
        side = "적은" if lower else "많은"
        return {**_skipped(ctx, req, policy), "timed_out": False,
                "reason": f"그 기간 {word}이(가) {req.target}회보다 {side} 간호사가 없습니다"}
    res = propose_edits(ctx, [_decided(r, policy) for r in requests], rules=session.rules, screen=session.screen,
                        max_candidates=1, time_budget=seconds, today=turn.today, allow=session.allow,
                        avoid_people=session.avoid_people, explain=False)
    out: Dict[str, Any] = {"choice_id": policy, "label": transfer_label(ctx, req, policy), "feasible": res.status == "ok",
                           "recommended": False, "partners": [], "coverage_changes": [], "quality": []}
    if res.status != "ok":
        out["reason"] = "방법 없음" if res.status == "infeasible" else "시간 안에 찾지 못함"
        out["timed_out"] = res.status == "timeout"
        return out
    cand = res.candidates[0]
    grid = apply_candidate(ctx, cand)
    for m in cand.affected:
        if m == req.nurse_id:
            continue
        before = count_cells(ctx, ctx.main, m, req.codes, req.days())
        after = count_cells(ctx, grid, m, req.codes, req.days())
        if (after > before) if lower else (after < before):
            out["partners"].append({"nurse_id": m, "name": ctx.names.get(m, m), "before": before, "after": after})
    for d in req.days():
        for code in sorted(work_targets(req)):
            before = sum(1 for n in ctx.nurse_ids if ctx.main[n][d] == code)
            after = sum(1 for n in ctx.nurse_ids if grid[n][d] == code)
            if after != before:
                out["coverage_changes"].append({"day": d + 1, "code": code, "before": before, "after": after})
    out.update(changed_count=cand.changed_count, span={"from": cand.span[0] + 1, "to": cand.span[1] + 1},
               quality=list(cand.quality[:3]),
               _cells=sorted((c.nurse_id, c.day, c.after_main) for c in cand.changes))   # 같은 안 판정용(내보내지 않음)
    return out


def _has_partner(ctx, req: CountTarget, lower: bool) -> bool:
    """fair 상대가 있나 — 요청 기간 안 개수로(검토 N1). 줄이는 요청이면 목표보다 적고 그 근무를 할 수 있는 사람,
    늘리는 요청이면 목표보다 많은 사람."""
    days = list(req.days())
    work = set(req.codes) & {"D", "E", "N", "M"}

    def count(m: str) -> int:
        return count_cells(ctx, ctx.main, m, req.codes, days)

    def can_take(m: str) -> bool:
        return any(ctx.is_free(m, d) and (not work or c not in ctx.forbidden.get((m, d), set()))
                   for d in days for c in req.codes)

    def can_give(m: str) -> bool:     # 그 기간에 바꿀 수 있는 그 근무 칸이 있어야 내줄 수 있다(검토 P2)
        return any(ctx.is_free(m, d) and ctx.main[m][d] in req.codes for d in days)

    others = [m for m in ctx.nurse_ids if m != req.nurse_id]
    if lower:
        return any(count(m) < req.target and can_take(m) for m in others)
    return any(count(m) > req.target and can_give(m) for m in others)


def _mark_same_as_auto(choices: List[Dict[str, Any]]) -> None:
    """미리 푼 결과가 '가장 적게 바꾸기'와 같은 방식은 그렇게 적는다(같은 안을 다른 이름으로 고르게 하지 않게)."""
    auto = next((c for c in choices if c["choice_id"] == "auto"), None)
    if not auto or not auto["feasible"]:
        return
    for c in choices:
        if c is not auto and c["feasible"] and c.get("_cells") == auto.get("_cells"):
            c["same_as_auto"] = True


def _skipped(ctx, req: CountTarget, policy: str) -> Dict[str, Any]:
    return {"choice_id": policy, "label": transfer_label(ctx, req, policy), "feasible": False, "recommended": False,
            "partners": [], "coverage_changes": [], "quality": [], "reason": "시간 안에 확인하지 못함",
            "timed_out": True}


def _recommend(choices: List[Dict[str, Any]]) -> None:
    """추천 — 설정을 넘기지 않는 것(그날 인원을 바꾸지 않는 것)이 먼저, 그다음 품질 악화가 적고 칸이 적은 것.
    '가장 적게 바꾸기'와 같은 안이면 그 이름(가장 적게 바꾸기)으로 추천한다."""
    pool = [c for c in choices if c["feasible"]]
    best = min(pool, key=lambda c: (c["choice_id"] in ("reduce_coverage", "add_coverage"), len(c["quality"]),
                                    c["changed_count"], c["choice_id"] != "auto"))
    if best.get("same_as_auto"):
        best = next(c for c in pool if c["choice_id"] == "auto")
    best["recommended"] = True


def _choice_line(c: Dict[str, Any]) -> str:
    if not c["feasible"]:
        return f"{c['label']} — {c['reason']}"
    return f"{c['label']} — " + " · ".join(choice_parts(c)) + (" [추천]" if c["recommended"] else "")


def choice_parts(c: Dict[str, Any], *, quality: bool = True) -> List[str]:
    """미리 푼 방식 하나의 요약 조각 — 답 문장과 화면 선택지가 같이 쓴다(화면은 근무 모양을 따로 보여 quality=False)."""
    parts = [f"{c['changed_count']}칸"]
    if c["partners"]:
        parts.append(", ".join(f"{r['name']} {r['before']}→{r['after']}" for r in c["partners"][:4]))
    if c["choice_id"] in ("reduce_coverage", "add_coverage") and c["coverage_changes"]:
        bound = "최소" if c["choice_id"] == "reduce_coverage" else "최대"
        parts.append(", ".join(f"{x['day']}일 {SHIFT_WORD[x['code']]} {x['before']}→{x['after']}명"
                               for x in c["coverage_changes"][:4]) + f"(일별 {bound} 인원을 이번만 넘김)")
    if quality and c["quality"]:
        parts.append(c["quality"][0])
    if c.get("same_as_auto"):
        parts.append("가장 적게 바꾸기와 같은 안")
    return parts


def _open_notes(turn: Turn) -> List[str]:
    """사용자가 정한 기준일 안내('6일부터 고칩니다(5일까지는 그대로 둡니다).')와 앞 요청을 그날에 맞추며 생긴 사실 —
    참고 문구라 대화에서 처음 한 번만 붙는다."""
    note = edit_from_note(turn.session.ctx, turn.open_from(), turn.today)
    return ([note] if note else []) + list(turn.notes)


def _clarify(turn: Turn, asks: List[str]) -> Outcome:
    answer = "\n".join(dict.fromkeys(asks))
    turn.record("clarify", {"asks": asks, "answer": answer})
    return Outcome(answer=answer)


def _ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)
