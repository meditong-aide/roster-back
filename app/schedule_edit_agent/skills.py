"""스킬 — 얇은 어댑터(②층). AIDE `@register` 와 같은 모양으로 둬서 나중에 AIDE 와 섞을 수 있게 한다.

  · llm=True  : LLM 이 고르는 스킬(설명·인자 스키마를 LLM 에 보여 준다) — 후보 제안, 요청 고치기
  · llm=False : 카드 버튼 — LLM 을 거치지 않는다(적용, 넘기기, 이걸로 다듬기, 레버 고르기)
mode 는 read · preview · write. 쓰기는 적용 하나뿐이고 새 버전(draft)만 만든다.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Callable, Dict, List, Optional

from sqlalchemy.orm import Session

from schedule_edit_agent.answer import answer_for, describe_requests, status_text
from schedule_edit_agent.grounding import SHIFT_WORD, SHIFTS, ground_changes, resolve_day, resolve_nurse
from services.schedule_edit.apply import ApplyError, apply_to_new_version
from services.schedule_edit.journal import record_event
from services.schedule_edit.requests import (
    TRANSFERS, CountTarget, EditRequest, SetCell, count_in, first_open_day, transfer_label, work_down, work_targets,
)
from services.schedule_edit.serialize import allow_to_json, requests_to_json, result_to_dict
from services.schedule_edit.session import EditSession, allow_lever_id, keep, propose, reject, stage
from services.schedule_edit.solver import apply_candidate, propose_edits

#: 채팅 한 번의 응답 시간 상한(초) — CloudFront 30초 안에 들어와야 한다. 경로에 들어온 순간부터 센다
#: (슬롯 대기·판정 재료 로드·LLM·솔버 모두 이 안 · 검토 HIGH-2). 솔버 예산은 남은 시간이다.
CHAT_BUDGET = 25.0
#: 남은 시간이 이보다 적으면 풀지 않고 '다시 보내 주세요' — 기한을 넘겨 솔버를 돌리지 않는다
MIN_SOLVE = 3.0
OUT_OF_TIME = ("이번 말씀은 아직 반영되지 않았습니다 — 응답 시간 안에 찾기에 시간이 부족했습니다. "
               "같은 말을 한 번 더 보내 주세요.")


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

    def remaining(self) -> float:
        return self.deadline - time.monotonic()

    def open_from(self) -> int:
        """바꿀 수 있는 첫날(0부터) — 오늘 이전은 닫는다."""
        return first_open_day(self.session.ctx, self.today)

    def record(self, event: str, payload: Dict[str, Any], *, elapsed_ms: Optional[int] = None,
               new_schedule_id: Optional[str] = None) -> None:
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


@dataclass
class Skill:
    name: str
    description: str
    parameters: Dict[str, Any]
    mode: str
    llm: bool
    run: Callable[[Turn, Dict[str, Any]], Outcome]

    def tool(self) -> Dict[str, Any]:
        """LLM 도구 정의(OpenAI function 모양 — `agents_v2.llm_client` 가 감싼다)."""
        return {"name": self.name, "description": self.description, "parameters": self.parameters}


SKILLS: Dict[str, Skill] = {}


def register(name: str, *, description: str, parameters: Dict[str, Any], mode: str, llm: bool):
    def deco(func: Callable[[Turn, Dict[str, Any]], Outcome]):
        SKILLS[name] = Skill(name, description, parameters, mode, llm, func)
        return func
    return deco


def llm_tools() -> List[Dict[str, Any]]:
    return [s.tool() for s in SKILLS.values() if s.llm]


# ── 인자 스키마 ─────────────────────────────────────────────────────────────

_CHANGE = {
    "type": "object",
    "properties": {
        "kind": {"type": "string", "enum": ["set", "avoid", "swap", "off_period", "count"],
                 "description": "set=그 칸을 이 근무로 · avoid=그 칸에서 이 근무(들)를 빼기(무엇으로 바꿀지는 맡김) · "
                                "swap=두 사람의 그날 근무 맞바꾸기 · off_period=day 부터 to_day 까지 OFF · "
                                "count=날짜를 정하지 않고 그 사람의 그 근무 개수를 바꾸기(어느 날인지는 맡김)"},
        "nurse": {"type": "string", "description": "사용자가 말한 간호사 이름 그대로(호칭 '선생님' 등은 빼고)"},
        "other_nurse": {"type": "string", "description": "swap 의 상대 간호사 이름"},
        "day": {"type": "integer", "minimum": 1, "maximum": 31,
                "description": "날짜(그 달의 일). count 에서는 기간을 말했을 때만 그 첫날"},
        "to_day": {"type": "integer", "minimum": 1, "maximum": 31,
                   "description": "off_period·count 기간의 마지막 날(포함). count 에서 기간을 말하지 않았으면 비운다"},
        "change": {"type": "integer", "description": "count: 늘리거나 줄일 개수(2개 빼 → -2, 하나 더 → 1)"},
        "target": {"type": "integer", "minimum": 0, "description": "count: '6개로' 처럼 목표 개수를 말했을 때"},
        "transfer": {"type": "string", "enum": list(TRANSFERS),
                     "description": "count: 상대를 어떻게 정할지 사용자가 이미 말했을 때만(빠지는 근무를 누가 메울지 / "
                                    "누구 것을 넘겨받을지). same_grade=같은 등급 · same_team=같은 팀 · fair=그 근무가 "
                                    "적은(넘겨받을 땐 많은) 사람 · reduce_coverage=메우지 않고 그날 인원 줄이기 · "
                                    "add_coverage=넘겨받지 않고 그날 인원 늘리기 · auto=가장 적게 바꾸기"},
        "shift": {"type": "string", "enum": list(SHIFTS),
                  "description": "set 의 근무. D=데이 E=이브닝 N=나이트 M=미드 OFF=휴무·오프"},
        "shifts": {"type": "array", "items": {"type": "string", "enum": list(SHIFTS)},
                   "description": "avoid 에서 뺄 근무들"},
    },
    "required": ["kind", "nurse"],
}
_CELL = {
    "type": "object",
    "properties": {
        "nurse": {"type": "string", "description": "간호사 이름. 비우면 그날 모든 간호사"},
        "day": {"type": "integer", "minimum": 1, "maximum": 31,
                "description": "날짜. remove 에서 비우면 그 간호사의 요청 전부(개수 요청 포함)"},
    },
}


# ── LLM 이 고르는 스킬 ───────────────────────────────────────────────────────


@register(
    "propose_edit",
    description=(
        "지금 근무표에서 요청한 칸을 바꾸는 방법을 찾는다. 규칙(하드락·설정)은 모두 지키면서 가장 적게 바꾸는 "
        "후보를 최대 3개 낸다. 새 요청을 받을 때 쓴다(이전 요청은 버린다). "
        "예) '김민지 14일 오프로 해줘' → changes=[{kind:set, nurse:'김민지', day:14, shift:'OFF'}] · "
        "'15일 나이트 이영희랑 박서준 바꿔줘' → [{kind:swap, nurse:'이영희', other_nurse:'박서준', day:15}] · "
        "'정다은 20일 나이트 빼줘' → [{kind:avoid, nurse:'정다은', day:20, shifts:['N']}] · "
        "'장원영 N 2개만 빼줘' → [{kind:count, nurse:'장원영', shift:'N', change:-2}] · "
        "'장원영 나이트 너무 많아'(개수 없음) → [{kind:count, nurse:'장원영', shift:'N'}](시스템이 현황을 보여 주고 되묻는다)"),
    parameters={"type": "object", "properties": {"changes": {"type": "array", "items": _CHANGE}},
                "required": ["changes"]},
    mode="preview", llm=True)
def propose_edit(turn: Turn, params: Dict[str, Any]) -> Outcome:
    grounded = ground_changes(turn.session.ctx, params.get("changes") or [], turn.open_from())
    if grounded.asks:
        return _clarify(turn, grounded.asks)
    return _propose(turn, grounded.requests)


@register(
    "revise_edit",
    description=(
        "바로 앞 요청을 고쳐 다시 찾는다. add=요청 더하기, remove=그 칸 요청 빼기, keep=이 칸(또는 그날 전체)은 "
        "지금 근무 그대로 두기. 예) '그리고 이영희 16일도 오프' → add · '15일은 건드리지 마' → keep=[{day:15}] · "
        "'김민지 14일 건은 취소' → remove=[{nurse:'김민지', day:14}] · "
        "개수 요청에서 빠지는 근무를 메우는 방식을 말하면 transfer('같은 등급으로' → same_grade)"),
    parameters={"type": "object", "properties": {
        "add": {"type": "array", "items": _CHANGE},
        "remove": {"type": "array", "items": _CELL},
        "keep": {"type": "array", "items": _CELL},
        "transfer": {"type": "string", "enum": list(TRANSFERS)},
    }},
    mode="preview", llm=True)
def revise_edit(turn: Turn, params: Dict[str, Any]) -> Outcome:
    ctx = turn.session.ctx
    if not turn.session.requests:
        return _clarify(turn, ["고칠 앞 요청이 없습니다. 무엇을 바꿀지 처음부터 말씀해 주세요."])
    asks: List[str] = []
    add = ground_changes(ctx, params["add"], turn.open_from()) if params.get("add") else None
    if add is not None:
        asks += add.asks
    removed = _removals(ctx, params.get("remove") or [], asks)
    kept = _cells(ctx, params.get("keep") or [], asks)
    if asks:
        return _clarify(turn, asks)
    reqs = [r for r in turn.session.requests
            if (r.nurse_id, r.day) not in removed and (r.nurse_id, None) not in removed]
    if params.get("transfer") in TRANSFERS:
        # 말로 바꾼 방식은 이미 고른 방식도 덮어쓴다('아니 같은 팀으로' · 검토 L4) — 버튼은 정하지 않은 것만
        reqs = [_decided(r, params["transfer"], force=True) for r in reqs]
    for r in (add.requests if add else []):
        if r not in reqs:
            reqs.append(r)
    asked = {(r.nurse_id, r.day) for r in reqs}
    for n, d in sorted(kept - asked):
        if ctx.is_free(n, d):     # 고정 칸은 원래 안 바뀐다
            reqs.append(SetCell(n, d, ctx.main[n][d]))
    if not any(not _is_keep(ctx, r) for r in reqs):
        return _clarify(turn, ["남은 변경 요청이 없습니다. 무엇을 바꿀지 말씀해 주세요."])
    return _propose(turn, reqs)


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


def _decided(r: EditRequest, transfer: str, force: bool = False) -> EditRequest:
    if isinstance(r, CountTarget) and (r.transfer is None or force):
        return CountTarget(r.nurse_id, r.codes, r.target, r.first_day, r.last_day, transfer)
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


#: 바꾸지 않는 답의 종류 → 정해진 문장(LLM 글을 사용자에게 보내지 않는다 · 검토 M3)
REPLY_KINDS = ("who", "when", "what", "outside_month", "settings", "unsupported", "other", "status", "unclear")
#: 아직 못 하는 변경 — 답 문장과 처음 화면 안내가 같은 목록을 쓴다
UNSUPPORTED = "그날 인원·근무 모양·휴가·원티드·여러 사람 고르게 맞추기, 근무 수 묻기"


def reply_text(kind: str, session: EditSession) -> str:
    ctx = session.ctx
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


@register(
    "reply_only",
    description=(
        "근무표를 바꾸지 않고 답할 때 부른다. 답 문장은 시스템이 정해진 틀로 만든다. kind: who=누구인지 빠짐 · "
        "when=며칠인지 빠짐 · what=무엇으로 바꿀지 빠짐 · outside_month=근무표 달 밖의 날짜 · settings=인원·규칙·OFF 수 "
        "같은 설정 변경이나 근무표 새로 만들기 · unsupported=근무표를 바꾸는 말이지만 아직 못 하는 종류(그날 인원 "
        "늘리기·줄이기, 연속 근무·주말 몰림 같은 근무 모양, 휴가·교육 넣기, 원티드 반영, 여러 사람 고르게 맞추기, "
        "'몇 개야'·'누구야' 같은 조회) · other=근무표와 상관없는 말 · status=지금 요청·결과가 어떤지 묻는 말"
        "(예: '바꿔 줬어?', '지금 뭐 하고 있었지?') · unclear=그 밖에 알아듣기 어려움"),
    parameters={"type": "object", "properties": {"kind": {"type": "string", "enum": list(REPLY_KINDS)}},
                "required": ["kind"]},
    mode="read", llm=True)
def reply_only(turn: Turn, params: Dict[str, Any]) -> Outcome:
    kind = params.get("kind") if params.get("kind") in REPLY_KINDS else "unclear"
    answer = reply_text(kind, turn.session)
    turn.record("clarify", {"kind": kind, "asks": [answer], "answer": answer})
    return Outcome(answer=answer)


# ── 카드 버튼(LLM 없음) ──────────────────────────────────────────────────────

_RANK = {"type": "object", "properties": {"rank": {"type": "integer", "minimum": 1}}, "required": ["rank"]}


@register("apply_edit", description="고른 후보로 새 버전(draft)을 만든다.",
          parameters={"type": "object", "properties": {"rank": {"type": "integer", "minimum": 1},
                                                       "name": {"type": "string"},
                                                       "request_text": {"type": "string"}},
                      "required": ["rank"]},
          mode="write", llm=False)
def apply_edit(turn: Turn, params: Dict[str, Any]) -> Outcome:
    cand = _candidate(turn, params)
    started = time.monotonic()
    try:
        saved = apply_to_new_version(turn.db, turn.session, cand, turn.user,
                                     request_text=str(params.get("request_text") or ""), name=params.get("name"),
                                     today=turn.today, conversation_id=turn.conversation_id, turn_no=turn.turn_no)
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


@register("reject_candidate", description="이 후보는 싫다 — 같은 사람들을 바꾸는 안은 빼고 다시 찾는다.",
          parameters=_RANK, mode="preview", llm=False)
def reject_candidate(turn: Turn, params: Dict[str, Any]) -> Outcome:
    cand = _candidate(turn, params)
    reject(turn.session, cand)
    turn.record("reject", {"rank": cand.rank, "people": sorted(cand.affected)})
    return _propose(turn, turn.session.requests)


@register("keep_candidate", description="이 후보를 기준으로 이어서 다듬는다(다음 탐색의 시작 해).",
          parameters=_RANK, mode="read", llm=False)
def keep_candidate(turn: Turn, params: Dict[str, Any]) -> Outcome:
    cand = _candidate(turn, params)
    keep(turn.session, cand)
    turn.record("keep", {"rank": cand.rank, "people": sorted(cand.affected)})
    return Outcome(answer=f"{cand.rank}번 안을 기준으로 이어서 다듬습니다. 더 바꾸거나 그대로 둘 곳을 말씀해 주세요.",
                   focus=cand.rank)


@register("choose_lever", description="레버를 고른다 — 그 설정을 그만큼만 넘기도록 허용하고 다시 찾는다.",
          parameters={"type": "object", "properties": {"lever_id": {"type": "string"}}, "required": ["lever_id"]},
          mode="preview", llm=False)
def choose_lever(turn: Turn, params: Dict[str, Any]) -> Outcome:
    try:
        lever = allow_lever_id(turn.session, str(params.get("lever_id") or ""))
    except KeyError:
        return Outcome(answer="그 레버는 지금 결과에 없습니다. 다시 찾은 결과에서 골라 주세요.", failed=True)
    turn.record("lever", {"lever_id": params.get("lever_id"), "lose": lever.lose,
                          "allow": allow_to_json(lever.allow), "allow_total": allow_to_json(turn.session.allow)})
    return _propose(turn, turn.session.requests)


@register("choose_transfer", description="개수 요청에서 빠지는 근무를 메우는 방식을 고르고 그 방식으로 찾는다.",
          parameters={"type": "object", "properties": {"choice_id": {"type": "string", "enum": list(TRANSFERS)}},
                      "required": ["choice_id"]},
          mode="preview", llm=False)
def choose_transfer(turn: Turn, params: Dict[str, Any]) -> Outcome:
    choice = params.get("choice_id")
    pending = next((r for r in turn.session.requests if isinstance(r, CountTarget) and r.transfer is None), None)
    if pending is None:
        raise SkillError("고를 개수 요청이 없습니다. 다시 요청해 주세요.")
    shown = _SHOW_DOWN if work_down(turn.session.ctx, pending) else _SHOW_UP
    if choice not in shown:                      # 보여 준 선택지만(방향과 맞지 않는 인원 완화 · 검토 P1)
        raise SkillError("보여 드린 선택지 중에서 골라 주세요.")
    turn.record("choose", {"choice_id": choice})
    return _propose(turn, [_decided(r, choice) for r in turn.session.requests])


# ── 공용 ────────────────────────────────────────────────────────────────────


class SkillError(Exception):
    """버튼 인자가 지금 상태와 안 맞는다(없는 후보 번호 등) — 사용자에게 그대로 보여 준다."""


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
    undecided = next((r for r in requests if isinstance(r, CountTarget) and r.transfer is None), None)
    if undecided is not None:
        return _choose_transfer(turn, requests, undecided)
    ctx = turn.session.ctx
    if turn.remaining() < MIN_SOLVE:
        turn.record("error", {"stage": "budget", "requests": requests_to_json(requests),
                              "remaining_ms": int(turn.remaining() * 1000)})
        return Outcome(answer=OUT_OF_TIME)
    started = time.monotonic()
    result = propose(turn.session, requests, today=turn.today, time_budget=turn.remaining())
    preview = result_to_dict(ctx, result)
    answer = answer_for(ctx, requests, result)
    turn.record("propose", {"requests": requests_to_json(requests), "described": describe_requests(ctx, requests),
                            "result": preview, "answer": answer,
                            "allow": allow_to_json(turn.session.allow),
                            "avoid": [sorted(p) for p in turn.session.avoid_people]},
                elapsed_ms=_ms(started))
    return Outcome(answer=answer, preview=preview)


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
    answer = "\n".join(lines + [f"참고: {n}" for n in ctx.notes])
    preview = {"status": "choose_transfer", "choices": ordered, "direction": "down" if down else "up",
               "question": question, "reasons": [], "notes": list(ctx.notes), "candidates": [], "levers": [],
               "blocking": []}
    turn.record("propose", {"requests": requests_to_json(requests), "described": describe_requests(ctx, requests),
                            "result": preview, "answer": answer, "allow": allow_to_json(session.allow),
                            "avoid": [sorted(p) for p in session.avoid_people]}, elapsed_ms=_ms(started))
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
        before = sum(1 for d in req.days() if ctx.main[m][d] in req.codes)
        after = sum(1 for d in req.days() if grid[m][d] in req.codes)
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
        return sum(1 for d in days if ctx.main[m][d] in req.codes)

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


def _clarify(turn: Turn, asks: List[str]) -> Outcome:
    answer = "\n".join(dict.fromkeys(asks))
    turn.record("clarify", {"asks": asks, "answer": answer})
    return Outcome(answer=answer)


def _ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)
