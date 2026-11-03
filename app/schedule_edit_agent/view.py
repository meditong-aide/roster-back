"""근무표 수정 결과 → 화면 블록(agent_kit 어휘). 화면은 이 블록을 그리기만 한다.

  · 후보(ok)          : 답 문장 + 후보 카드(고른 카드에 사람별 근무 수·나빠지는 모양·바뀌는 칸·버튼·표 미리보기)
  · 방식 고르기       : 묻는 문장 + 하나 고르기(미리 풀어 본 결과·추천·안 되는 이유)
  · 불가(infeasible)  : 이유 문장 + 막는 설정 + 레버 카드(잃는 것·얻는 것·버튼)
  · 후보(ok)에 레버   : 후보 카드 아래, 설정 하나를 이번만 넘기면 바꾸는 칸이 줄어들 때만 레버 카드(몇 칸 → 몇 칸)
  · 바꾸기 전 확인    : 묻는 문장 + 피할 수 없는 것 카드([그래도 진행]·[바꾸지 않기])
  · 적용 전 확인      : 묻는 문장 + 그 안 카드(나빠지는 것·표 미리보기·[이대로 적용]·[다른 안 보기])
  · 적용              : 저장 안내(새 버전으로 바꾸기는 효과 switch_version)
  · 문장만 온 턴      : 문장 + 지금 들고 있는 결과(후보·레버)를 다시 — 패널은 언제나 지금 상태 전체다
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Dict, List, Optional

from agent_kit import blocks as B
from schedule_edit_agent.agent import Reply
from schedule_edit_agent.answer import SOURCE_WORD
from schedule_edit_agent.skills import choice_parts
from services.schedule_edit.requests import CountTarget, transfer_label, work_down
from services.schedule_edit.serialize import lever_id, result_to_dict
from services.schedule_edit.session import EditSession

LEVER_NOTE = ("연속 근무·나이트 규칙·근무 순서 같은 하드락은 넘길 수 없습니다. "
              "이번 수정에만 적용되고 병동 설정은 바뀌지 않습니다.")
CHOICE_NOTE = "선택지마다 미리 한 번 풀어 본 결과입니다. 고르면 그 방식으로 후보를 3개까지 다시 찾습니다."
CONFIRM_NOTE = ("요청한 칸과 바꿀 수 없는 칸(지난 날·확정 칸·전월 근무)만으로 셈한 것이라 어떤 방법으로도 피할 수 "
                "없습니다. 진행해도 이번 수정에만 해당하고 병동 설정은 바뀌지 않습니다.")
APPLY_NOTE = "적용하면 새 버전으로 저장되고 이전 버전은 그대로 남습니다."
#: 후보 카드 밑 — 카드를 보며 규칙을 물을 수 있다는 것(사용자 지시 11-03 '그레이드는 다 지켜져? 등 추가 문의')
CANDIDATE_NOTE = "카드를 보며 '1안 등급 다 지켜져?', '연속 근무 괜찮아?'처럼 물어보셔도 됩니다."
#: 근무 수를 보일 순서(대표코드) — OFF 는 맨 끝
_COUNT_ORDER = ("D", "E", "N", "M", "OFF")


def blocks_for(reply: Reply, session: Optional[EditSession]) -> List[B.Block]:
    switched = next((e for e in reply.effects if e.get("type") == "switch_version"), None)
    if switched is not None:
        name = switched.get("name") or "수정본"
        version = switched.get("version")
        where = f"{name}(VER{version})" if version is not None else name
        return [B.notice(f"{where}으로 저장했습니다. 이전 버전은 그대로 남고, 바뀐 칸은 표에 주황 테두리로 보입니다.",
                         tone="success", title="새 버전으로 저장했습니다")]
    preview = reply.preview
    if preview is None and session is not None and session.last is not None:
        preview = result_to_dict(session.ctx, session.last)     # 문장만 온 턴 — 보던 결과를 그대로 다시
        shown = [B.text(reply.answer, tone="error" if reply.failed else "normal")]
    else:
        shown = [B.text(_lead(reply.answer, preview), tone="error" if reply.failed else "normal")]
    if not preview:
        return shown
    status = preview.get("status")
    if status == "ok":
        cards = [_candidate_cards(preview["candidates"], reply.focus, preview.get("source"))]
        if preview.get("levers"):
            # 후보는 있지만 설정 하나를 이번만 넘기면 더 적게 바꿀 수 있을 때만(사용자 제안 11-03)
            cards.append(_lever_cards(preview["levers"]))
        return shown + cards
    if status == "choose_transfer":
        return shown + [_transfer_choice(preview)]
    if status == "confirm":
        return shown + [_confirm_card(preview)]
    if status == "confirm_apply":
        return shown + [_apply_confirm_card(preview)]
    if status == "infeasible" and preview.get("levers"):
        # 막는 설정은 카드 제목이 하나씩 말한다 — 내부 규칙 이름 줄을 따로 두지 않는다(ui-craft 11-03)
        return shown + [_lever_cards(preview["levers"])]
    return shown


def _lead(answer: str, preview: Optional[Dict[str, Any]]) -> str:
    """방식 고르기·확인에서는 묻는 문장과 참고만 — 선택지·짚은 것은 아래 블록이 보인다."""
    if preview and preview.get("status") in ("choose_transfer", "confirm", "confirm_apply") \
            and preview.get("question"):
        return "\n".join([preview["question"]] + [f"참고: {n}" for n in preview.get("notes") or []])
    return answer


# ── 후보 카드 ───────────────────────────────────────────────────────────────


def _candidate_cards(candidates: List[Dict[str, Any]], focus: Optional[int], source: Optional[str] = None) -> B.Block:
    items = [_candidate_card(c, source) for c in candidates]
    return B.cards(items, key=_key(candidates), selected=str(focus) if focus else None,
                   footnote=None if source else CANDIDATE_NOTE)


def _candidate_card(c: Dict[str, Any], source: Optional[str] = None) -> Dict[str, Any]:
    """후보 카드. 생성 후처리(source)로 낸 안은 하나뿐이고 서비스 규칙 그대로라 '적용'만 둔다."""
    rank = c["rank"]
    people = [p["name"] for p in c["affected"]]
    lines = []
    if c["quality"]:
        lines.append(B.line("규칙은 모두 지키지만 근무 모양이 나빠집니다: " + _few(c["quality"]), tone="warning"))
    changes = c["changes"]
    actions = [B.action("이 안 적용", "apply_edit", {"rank": rank}, style="primary")]
    if source is None:
        actions += [B.action("이 안에서 다듬기", "keep_candidate", {"rank": rank}),
                    B.action("이 사람들 말고", "reject_candidate", {"rank": rank})]
    title = f"{SOURCE_WORD.get(source, source)} · " if source else f"{rank}안 · "
    return B.card(
        str(rank), f"{title}{c['changed_count']}칸 · {_span(c)}",
        summary=" · ".join(people), chips=_person_chips(c), lines=lines,
        details={"label": f"바뀌는 칸 {len(changes)}개",
                 "items": [f"{x['name']} {x['day']}일 {x['before'] or '빈 칸'} → {x['after']}" for x in changes]},
        actions=actions,
        overlay={"kind": "schedule_cells",
                 "cells": [{"nurse_id": x["nurse_id"], "day": x["day"], "before": x["before"], "after": x["after"]}
                           for x in changes]})


#: 근무 모양 항목을 한 줄에 몇 개까지 — 넘으면 '외 n개'(4차 평가: 14개가 한 줄로 이어져 읽히지 않았다 · 전부는 펼친 목록·표에)
SHAPE_SHOWN = 5


def _few(items: List[str]) -> str:
    more = f" 외 {len(items) - SHAPE_SHOWN}개" if len(items) > SHAPE_SHOWN else ""
    return " · ".join(items[:SHAPE_SHOWN]) + more


def _span(c: Dict[str, Any]) -> str:
    start, end = int(c["span"]["from"][8:10]), int(c["span"]["to"][8:10])
    return f"{start}일" if start == end else f"{start}~{end}일"


def _person_chips(c: Dict[str, Any]) -> List[Dict[str, str]]:
    """사람별 — 근무 수가 바뀐 것은 'N 5→3', 수는 그대로면 바뀐 칸 수."""
    chips = []
    for person in c["affected"]:
        counts = c["counts"].get(person["nurse_id"], {})
        moved = [f"{code} {counts[code][0]}→{counts[code][1]}" for code in _COUNT_ORDER
                 if code in counts and counts[code][0] != counts[code][1]]
        cells = sum(1 for x in c["changes"] if x["nurse_id"] == person["nurse_id"])
        chips.append({"label": person["name"], "text": " · ".join(moved) if moved else f"{cells}칸"})
    return chips


def _key(candidates: List[Dict[str, Any]]) -> str:
    """같은 결과면 같은 값 — 화면이 사용자가 고른 카드를 그대로 둔다."""
    raw = json.dumps([[c["rank"], [(x["nurse_id"], x["day"], x["after"]) for x in c["changes"]]]
                      for c in candidates], ensure_ascii=False)
    return hashlib.sha1(raw.encode()).hexdigest()[:12]


# ── 방식 고르기 ─────────────────────────────────────────────────────────────


def _transfer_choice(preview: Dict[str, Any]) -> B.Block:
    options = []
    for c in preview["choices"]:
        if c["feasible"]:
            options.append(B.option(c["choice_id"], c["label"], summary=" · ".join(choice_parts(c, quality=False)),
                                    badge="추천" if c["recommended"] else None, notes=c["quality"]))
        else:
            options.append(B.option(c["choice_id"], c["label"], disabled_reason=c.get("reason") or "방법 없음"))
    recommended = next((c["choice_id"] for c in preview["choices"] if c["recommended"]), None)
    down = preview.get("direction") != "up"
    return B.choice("transfer", options, legend="메우는 방식" if down else "넘겨받을 곳",
                    submit_label="이 방식으로 찾기", action_name="choose_transfer", param="choice_id",
                    selected=recommended, footnote=CHOICE_NOTE)


# ── 확인 ────────────────────────────────────────────────────────────────────


def _confirm_card(preview: Dict[str, Any]) -> B.Block:
    items = preview.get("findings") or []
    card = B.card("confirm", f"그대로 바꾸면 생기는 것 {len(items)}가지",
                  lines=[B.line(f["text"], tone="warning") for f in items],
                  actions=[B.action("그래도 진행", "confirm_request", style="primary"),
                           B.action("바꾸지 않기", "cancel_request")])
    return B.cards([card], selectable=False, footnote=CONFIRM_NOTE)


def _apply_confirm_card(preview: Dict[str, Any]) -> B.Block:
    """고른 안 하나 — 표 미리보기(overlay)는 그대로 두고, 나빠지는 것과 [이대로 적용]·[다른 안 보기]만."""
    rank = int(preview["rank"])
    c = next(x for x in preview["candidates"] if x["rank"] == rank)
    base = _candidate_card(c, preview.get("source"))
    items = preview.get("findings") or []
    lines = [B.line(f["text"], tone="warning") for f in items if f.get("kind") != "quality"]
    shape = [f["text"] for f in items if f.get("kind") == "quality"]
    if shape:
        lines.append(B.line("근무 모양이 나빠짐: " + _few(shape), tone="warning"))
    card = B.card(str(rank), f"{base['title']} · 적용 전 확인", summary=base["summary"], chips=base["chips"],
                  lines=lines, details=base["details"], overlay=base["overlay"],
                  actions=[B.action("이대로 적용", "apply_edit",
                                    {"rank": rank, "confirmed": True, "seen": [f["id"] for f in items]},
                                    style="primary"),
                           B.action("다른 안 보기", "back_to_candidates", {"rank": rank})])
    return B.cards([card], key=f"confirm-{_key([c])}", selected=str(rank), footnote=APPLY_NOTE)


def retry_card() -> B.Block:
    """새로 고친 뒤 이어 볼 때 찾아 둔 후보가 없으면(서버가 다시 떴거나 메모리에서 내려갔다) — 같은 요청으로 다시 찾기."""
    card = B.card("retry", "찾아 둔 후보를 다시 불러와야 합니다",
                  lines=[B.line("잠시 비워 둔 사이 앞서 찾은 후보가 없어졌습니다. 같은 요청으로 다시 찾습니다.")],
                  actions=[B.action("같은 요청으로 다시 찾기", "retry_request", style="primary")])
    return B.cards([card], selectable=False)


# ── 레버 ────────────────────────────────────────────────────────────────────


def _lever_cards(levers: List[Dict[str, Any]]) -> B.Block:
    items = []
    for lv in levers:
        # 제목 = 넘기는 설정 — 카드끼리 무엇이 다른지가 제목에서 보인다(같은 제목이 반복됐다 · ui-craft 11-03)
        # 기간('n일 동안')은 뺐다 — 칸 수와 나란히 두면 무엇의 기간인지 안 읽혔다(문구 다듬기 11-03)
        now, after = lv["changes"]["now"], lv["changes"]["after"]
        if lv.get("gain"):    # 원티드 반영 — 얻는 것을 칸 수가 아니라 넣는 원티드 수로
            lines = [B.line(f"이 설정을 이번만 넘기시면 {lv['gain']}를 넣을 수 있습니다({after}칸 바꿈)")]
        elif now is None:
            lines = [B.line(f"이 설정을 이번만 넘기시면 {after}칸을 바꿔 요청을 들어줄 수 있습니다")]
        else:   # 후보가 있을 때 — '이 설정을 넘기면 이렇게 줄어든다'(사용자 지시 11-03 '어떤 설정을 바꾸시면 어떻게 가능하다')
            lines = [B.line(f"이 설정을 이번만 넘기시면 바꾸는 칸이 {now}칸 → {after}칸으로 줄어듭니다")]
        lines += [B.line(q, tone="warning") for q in lv["quality"]]
        items.append(B.card(lv["lever_id"], " · ".join(lv["lose"]), lines=lines,
                            actions=[B.action("이 설정만 넘기고 다시 찾기", "choose_lever",
                                              {"lever_id": lv["lever_id"]}, style="primary")]))
    return B.cards(items, selectable=False, footnote=LEVER_NOTE)


def step_label(action: str, params: Dict[str, Any], session: Optional[EditSession]) -> Optional[str]:
    """버튼을 흔적 한 줄로 — 누르기 전 상태(후보 사람·레버·방식 이름)로 만든다."""
    rank = params.get("rank")
    last = session.last if session is not None else None
    if action == "apply_edit":
        source = getattr(last, "source", None)
        which = f"{SOURCE_WORD.get(source, source)}" if source else f"{rank}안"
        return f"{which} 확인하고 적용" if params.get("confirmed") else f"{which} 적용"
    if action == "confirm_request":
        return "그래도 진행"
    if action == "cancel_request":
        return "바꾸지 않기"
    if action == "back_to_candidates":
        return "다른 안 보기"
    if action == "retry_request":
        return "같은 요청으로 다시 찾기"
    if action == "keep_candidate":
        return f"{rank}안을 기준으로 다듬기"
    if action == "reject_candidate":
        names = []
        if last is not None and isinstance(rank, int) and 1 <= rank <= len(last.candidates):
            ctx = session.ctx
            names = [ctx.names.get(n, n) for n in sorted(last.candidates[rank - 1].affected)]
        return (" · ".join(names) if names else f"{rank}안의 사람들") + " 말고 다시 찾기"
    if action == "choose_lever":
        lever = next((lv for lv in (last.levers if last is not None else [])
                      if lever_id(lv.allow) == params.get("lever_id")), None)
        return "이번만 넘김: " + ", ".join(lever.lose) if lever is not None else "설정 하나를 이번만 넘김"
    if action == "choose_transfer":
        return _transfer_step(session, params.get("choice_id"))
    return None


def _transfer_step(session: Optional[EditSession], choice_id: Any) -> str:
    pending = next((r for r in session.requests if isinstance(r, CountTarget) and r.transfer is None),
                   None) if session is not None else None
    if pending is None or not choice_id:
        return f"메우는 방식: {choice_id or ''}"
    prefix = "메우는 방식" if work_down(session.ctx, pending) else "넘겨받을 곳"
    return f"{prefix}: {transfer_label(session.ctx, pending, str(choice_id))}"
