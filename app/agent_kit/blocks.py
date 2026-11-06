"""화면 어휘 — 패널에 그릴 것(블록)·버튼·화면에 일으킬 일(효과). 모든 에이전트가 같은 어휘로 쓴다.

화면은 이 어휘만 알면 어떤 에이전트든 그린다 — 새 에이전트·새 요청 종류를 붙일 때 화면을 고치지 않는다.
  · 블록: text · notice · trail · cards · choice · examples. 화면은 모르는 종류를 건너뛴다(오래된 화면도 안 깨진다).
  · 버튼(action): 화면은 뜻을 모른 채 `input` 을 그대로 다시 보낸다. 문구·순서·강조도 서버가 정한다.
  · 예시(examples)의 calls: items(문장)와 같은 자리에 미리 정한 도구 호출 `{action, params}`(없으면 null). 새 화면은
    calls[i] 가 있으면 `{kind:"text", text: items[i], action, params}` 를 바로 보내고(해석이 붙은 말 — 서버는 LLM 을
    거치지 않고 그 도구를 부르며 대화·흔적·기록에는 말로 남는다), 없으면 입력칸에 채운다. 옛 화면은 calls 를 몰라
    지금처럼 문장만 쓴다 — items 는 늘 문장 목록이다(검토 M1).
  · 카드의 overlay: 화면(호스트)이 아는 종류만 그린다 — 근무표 칸 미리보기는 `schedule_cells`.
  · 효과(effects): 호스트가 아는 종류만 처리한다 — `switch_version`(근무표 버전 바꾸기), 나중에 AIDE 의
    `navigate`·`prefill` 도 같은 자리에 온다.
값은 JSON 으로 그대로 나가는 dict 다(화면 계약: 계약 문서 '공통 에이전트 틀' 장).
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional

Block = Dict[str, Any]

TONES = ("normal", "muted", "info", "success", "warning", "error")


def text(body: str, *, tone: str = "normal") -> Block:
    return {"type": "text", "text": body, "tone": tone}


def notice(body: str, *, tone: str = "info", title: Optional[str] = None) -> Block:
    return {"type": "notice", "tone": tone, "title": title, "text": body}


def trail(request: Optional[str], steps: Iterable[str]) -> Block:
    """대화 흔적 — 지금 요청 한 줄과 그 뒤 고른 것들(시안: 흉내 낸 채팅 기록 대신 최소 흔적)."""
    return {"type": "trail", "request": request, "steps": list(steps)}


def action(label: str, name: str, params: Optional[Dict[str, Any]] = None, *, style: str = "secondary") -> Dict[str, Any]:
    """버튼 — 누르면 화면이 `input` 을 그대로 보낸다. style: primary · secondary · link."""
    return {"label": label, "style": style, "input": {"kind": "action", "action": name, "params": dict(params or {})}}


def line(body: str, *, tone: str = "normal") -> Dict[str, Any]:
    return {"text": body, "tone": tone}


def card(card_id: str, title: str, *, badge: Optional[str] = None, summary: Optional[str] = None,
         chips: Iterable[Dict[str, str]] = (), lines: Iterable[Dict[str, Any]] = (),
         details: Optional[Dict[str, Any]] = None, actions: Iterable[Dict[str, Any]] = (),
         overlay: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """카드 — 고르면 펼쳐져 chips·lines·details·actions 와 overlay(표 미리보기)를 보인다.

    summary 는 접혔을 때 한 줄, chips 는 {label, text}, details 는 {label, items:[문장]} (펼쳐 볼 목록).
    """
    return {"id": card_id, "title": title, "badge": badge, "summary": summary, "chips": list(chips),
            "lines": list(lines), "details": details, "actions": list(actions), "overlay": overlay}


def cards(items: List[Dict[str, Any]], *, key: Optional[str] = None, selectable: bool = True,
          selected: Optional[str] = None, footnote: Optional[str] = None) -> Block:
    """카드 묶음. key 가 같으면 같은 결과 — 화면은 사용자가 고른 카드를 그대로 둔다(다르면 selected 로)."""
    first = items[0]["id"] if items else None
    return {"type": "cards", "key": key, "selectable": selectable,
            "selected": (selected or first) if selectable else None, "items": items, "footnote": footnote}


def option(option_id: str, label: str, *, summary: str = "", badge: Optional[str] = None,
           notes: Iterable[str] = (), disabled_reason: Optional[str] = None) -> Dict[str, Any]:
    return {"id": option_id, "label": label, "summary": summary, "badge": badge, "notes": list(notes),
            "disabled_reason": disabled_reason}


def choice(choice_id: str, options: List[Dict[str, Any]], *, legend: str, submit_label: str, action_name: str,
           param: str, selected: Optional[str] = None, footnote: Optional[str] = None) -> Block:
    """하나 고르기 — 화면은 고른 option id 를 submit.input.params[param] 에 넣어 보낸다."""
    usable = [o["id"] for o in options if not o["disabled_reason"]]
    pick = selected if selected in usable else (usable[0] if usable else None)
    return {"type": "choice", "id": choice_id, "legend": legend, "options": options, "selected": pick,
            "submit": {**action(submit_label, action_name, style="primary"), "param": param},
            "footnote": footnote}


def call(name: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """예시 하나의 미리 정한 도구 호출(examples 의 calls 칸)."""
    return {"action": name, "params": dict(params or {})}


def examples(label: str, items: Iterable[str], calls: Optional[Iterable[Optional[Dict[str, Any]]]] = None) -> Block:
    """누르면 입력칸에 채워지는 예시 문장. calls 는 items 와 같은 길이의 도구 호출(`call()` · 없는 항목은 None) —
    하나라도 있을 때만 싣는다."""
    block: Block = {"type": "examples", "label": label, "items": list(items)}
    if calls is not None:
        calls = list(calls)
        if len(calls) != len(block["items"]):
            raise ValueError("calls 는 items 와 같은 길이여야 합니다")
        if any(c is not None for c in calls):
            block["calls"] = calls
    return block
