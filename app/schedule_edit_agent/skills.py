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
from schedule_edit_agent.grounding import SHIFTS, ground_changes, resolve_day, resolve_nurse
from services.schedule_edit.apply import ApplyError, apply_to_new_version
from services.schedule_edit.journal import record_event
from services.schedule_edit.requests import EditRequest, SetCell
from services.schedule_edit.serialize import allow_to_json, requests_to_json, result_to_dict
from services.schedule_edit.session import EditSession, allow_lever_id, keep, propose, reject

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

    def record(self, event: str, payload: Dict[str, Any], *, elapsed_ms: Optional[int] = None,
               new_schedule_id: Optional[str] = None) -> None:
        record_event(self.db, self.session.schedule_id, event, payload,
                     created_by=getattr(self.user, "account_id", None), new_schedule_id=new_schedule_id,
                     conversation_id=self.conversation_id, turn_no=self.turn_no, elapsed_ms=elapsed_ms)


@dataclass
class Outcome:
    answer: str
    preview: Optional[Dict[str, Any]] = None
    ui_actions: List[Dict[str, Any]] = field(default_factory=list)


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
        "kind": {"type": "string", "enum": ["set", "avoid", "swap", "off_period"],
                 "description": "set=그 칸을 이 근무로 · avoid=그 칸에서 이 근무(들)를 빼기(무엇으로 바꿀지는 맡김) · "
                                "swap=두 사람의 그날 근무 맞바꾸기 · off_period=day 부터 to_day 까지 OFF"},
        "nurse": {"type": "string", "description": "사용자가 말한 간호사 이름 그대로(호칭 '선생님' 등은 빼고)"},
        "other_nurse": {"type": "string", "description": "swap 의 상대 간호사 이름"},
        "day": {"type": "integer", "minimum": 1, "maximum": 31, "description": "날짜(그 달의 일)"},
        "to_day": {"type": "integer", "minimum": 1, "maximum": 31, "description": "off_period 의 마지막 날(포함)"},
        "shift": {"type": "string", "enum": list(SHIFTS),
                  "description": "set 의 근무. D=데이 E=이브닝 N=나이트 M=미드 OFF=휴무·오프"},
        "shifts": {"type": "array", "items": {"type": "string", "enum": list(SHIFTS)},
                   "description": "avoid 에서 뺄 근무들"},
    },
    "required": ["kind", "nurse", "day"],
}
_CELL = {
    "type": "object",
    "properties": {
        "nurse": {"type": "string", "description": "간호사 이름. 비우면 그날 모든 간호사"},
        "day": {"type": "integer", "minimum": 1, "maximum": 31},
    },
    "required": ["day"],
}


# ── LLM 이 고르는 스킬 ───────────────────────────────────────────────────────


@register(
    "propose_edit",
    description=(
        "지금 근무표에서 요청한 칸을 바꾸는 방법을 찾는다. 규칙(하드락·설정)은 모두 지키면서 가장 적게 바꾸는 "
        "후보를 최대 3개 낸다. 새 요청을 받을 때 쓴다(이전 요청은 버린다). "
        "예) '김민지 14일 오프로 해줘' → changes=[{kind:set, nurse:'김민지', day:14, shift:'OFF'}] · "
        "'15일 나이트 이영희랑 박서준 바꿔줘' → [{kind:swap, nurse:'이영희', other_nurse:'박서준', day:15}] · "
        "'정다은 20일 나이트 빼줘' → [{kind:avoid, nurse:'정다은', day:20, shifts:['N']}]"),
    parameters={"type": "object", "properties": {"changes": {"type": "array", "items": _CHANGE}},
                "required": ["changes"]},
    mode="preview", llm=True)
def propose_edit(turn: Turn, params: Dict[str, Any]) -> Outcome:
    grounded = ground_changes(turn.session.ctx, params.get("changes") or [])
    if grounded.asks:
        return _clarify(turn, grounded.asks)
    return _propose(turn, grounded.requests)


@register(
    "revise_edit",
    description=(
        "바로 앞 요청을 고쳐 다시 찾는다. add=요청 더하기, remove=그 칸 요청 빼기, keep=이 칸(또는 그날 전체)은 "
        "지금 근무 그대로 두기. 예) '그리고 이영희 16일도 오프' → add · '15일은 건드리지 마' → keep=[{day:15}] · "
        "'김민지 14일 건은 취소' → remove=[{nurse:'김민지', day:14}]"),
    parameters={"type": "object", "properties": {
        "add": {"type": "array", "items": _CHANGE},
        "remove": {"type": "array", "items": _CELL},
        "keep": {"type": "array", "items": _CELL},
    }},
    mode="preview", llm=True)
def revise_edit(turn: Turn, params: Dict[str, Any]) -> Outcome:
    ctx = turn.session.ctx
    if not turn.session.requests:
        return _clarify(turn, ["고칠 앞 요청이 없습니다. 무엇을 바꿀지 처음부터 말씀해 주세요."])
    asks: List[str] = []
    add = ground_changes(ctx, params["add"]) if params.get("add") else None
    if add is not None:
        asks += add.asks
    removed = _cells(ctx, params.get("remove") or [], asks)
    kept = _cells(ctx, params.get("keep") or [], asks)
    if asks:
        return _clarify(turn, asks)
    reqs = [r for r in turn.session.requests if (r.nurse_id, r.day) not in removed]
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
REPLY_KINDS = ("who", "when", "what", "outside_month", "settings", "other", "status", "unclear")


def reply_text(kind: str, session: EditSession) -> str:
    ctx = session.ctx
    return {
        "who": "누구의 근무를 바꿀까요? 이름을 알려 주세요.",
        "when": "며칠의 근무인가요?",
        "what": "어떤 근무로 바꿀까요? (D·E·N·OFF)",
        "outside_month": f"이 근무표는 {ctx.year}년 {ctx.month}월입니다. {ctx.month}월 안의 날짜로 말씀해 주세요.",
        "settings": "인원·규칙·OFF 수 같은 설정은 여기서 바꿀 수 없습니다. 설정 화면에서 바꿔 주세요.",
        "other": "여기서는 이 근무표의 칸을 바꾸는 요청만 도와드릴 수 있습니다.",
        "status": status_text(session),
    }.get(kind, "무엇을 바꿀지 다시 말씀해 주세요. 예) '김민지 14일 OFF', '15일 나이트 이영희랑 바꿔 줘'")


@register(
    "reply_only",
    description=(
        "근무표를 바꾸지 않고 답할 때 부른다. 답 문장은 시스템이 정해진 틀로 만든다. kind: who=누구인지 빠짐 · "
        "when=며칠인지 빠짐 · what=무엇으로 바꿀지 빠짐 · outside_month=근무표 달 밖의 날짜 · settings=인원·규칙·OFF 수 "
        "같은 설정 변경이나 근무표 새로 만들기 · other=근무표와 상관없는 말 · status=지금 요청·결과가 어떤지 묻는 말"
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
        return Outcome(answer=exc.message)
    turn.switched_to = saved.get("new_schedule_id")
    name = saved.get("new_name") or ""
    return Outcome(
        answer=f"새 버전으로 저장했습니다: {name}(VER{saved.get('new_version')}). 화면을 새 버전으로 바꿉니다.",
        ui_actions=[{"type": "switch_version", "schedule_id": turn.switched_to}])


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
    return Outcome(answer=f"{cand.rank}번 안을 기준으로 이어서 다듬습니다. 더 바꾸거나 그대로 둘 곳을 말씀해 주세요.")


@register("choose_lever", description="레버를 고른다 — 그 설정을 그만큼만 넘기도록 허용하고 다시 찾는다.",
          parameters={"type": "object", "properties": {"lever_id": {"type": "string"}}, "required": ["lever_id"]},
          mode="preview", llm=False)
def choose_lever(turn: Turn, params: Dict[str, Any]) -> Outcome:
    try:
        lever = allow_lever_id(turn.session, str(params.get("lever_id") or ""))
    except KeyError:
        return Outcome(answer="그 레버는 지금 결과에 없습니다. 다시 찾은 결과에서 골라 주세요.")
    turn.record("lever", {"lever_id": params.get("lever_id"), "lose": lever.lose,
                          "allow": allow_to_json(lever.allow), "allow_total": allow_to_json(turn.session.allow)})
    return _propose(turn, turn.session.requests)


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
    actions = [{"type": "preview_candidates"}] if result.candidates else []
    return Outcome(answer=answer, preview=preview, ui_actions=actions)


def _clarify(turn: Turn, asks: List[str]) -> Outcome:
    answer = "\n".join(dict.fromkeys(asks))
    turn.record("clarify", {"asks": asks, "answer": answer})
    return Outcome(answer=answer)


def _ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)
