"""LLM 에 보내는 컨텍스트 — 토큰을 아끼게 짠다(사용자 지시 11-02).

  1. 고정 지시문 + 도구 정의 — 매 턴 같은 앞부분이라 공급자 프롬프트 캐시가 걸린다(바꿀 때는 통째로).
  2. 최근 HISTORY_TURNS 턴만 — 답변은 ANSWER_CLIP 자로 자른다. 그 앞 대화는 3의 상태 요약이 대신한다.
  3. '지금 상태' 한 덩어리 — 오늘·근무표 달·지금 요청·마지막 결과 요약·기준으로 삼은 안·허용한 레버 수.
  4. 이번 말.
후보의 칸 목록·간호사 명단 같은 큰 데이터는 넣지 않는다 — 후보는 버튼이 다루고, 이름은 스킬이 근무표 안에서 찾는다.
LLM 호출은 한 턴에 1번 — 스킬 결과를 LLM 에 다시 보내지 않고 답변은 정해진 틀로 만든다(`answer.py`).
"""

from __future__ import annotations

from datetime import date
from typing import Dict, List

from schedule_edit_agent.answer import current_request
from agent_kit.conversation import Conversation
from services.schedule_edit.requests import first_open_day, held_until
from services.schedule_edit.session import EditSession

HISTORY_TURNS = 4
ANSWER_CLIP = 200
_WEEKDAY = "월화수목금토일"

SYSTEM = """너는 병원 간호 근무표 '수정' 도우미의 해석기다. 수간호사의 말을 읽고 언제나 도구 하나를 골라 인자를 채운다. 글로 답하지 않는다.
- 새로 칸을 바꾸는 요청은 propose_edit, 바로 앞 요청에 더하기·빼기·'그대로 두기'는 revise_edit.
- 연차·병가·공가 같은 휴가를 그 날 칸에 넣는 말은 propose_edit 의 kind=leave(휴가 이름은 말한 그대로).
- 그날 그 근무 인원을 늘리거나 줄이는 말('15일 E 한 명 더', '20일 N 한 명 빼', '20일 N에 1등급 넣어')은 propose_edit 의 kind=cover(사람 이름 없이 · 늘리면 change 1, 줄이면 change -1 · '5명으로'면 target · '15일 E 인원 좀 바꿔줘'처럼 몇 명인지 없으면 change·target 을 비운다 — 시스템이 지금 인원을 보여 주고 되묻는다. 날짜·근무를 이미 말했으면 되묻지 않는다). 병동 인원 기준 자체를 바꾸는 말은 설정 변경이다.
- 여러 사람의 그 근무 수를 고르게 맞춰 달라는 말('N 근무 수 고르게 맞춰줘', '나이트 몰린 거 고르게', 'OFF 다 9개로')은 propose_edit 의 kind=balance(사람 이름 없이 · 모두 같은 수를 말하면 target · 대상은 시스템이 정한다). 한 사람의 개수만 말하면 kind=count.
- 간호사가 낸 원티드(희망 근무) 중 반영 안 된 것을 넣어 달라는 말('원티드 반영해줘', '반영 안 된 원티드 넣어줘', '장원영 원티드 맞춰줘', '15~20일 원티드 넣어줘')은 propose_edit 의 kind=wanted(사람·기간은 말했을 때만). 특정 칸을 원티드대로 바꾸라는 말('장원영 9일 원티드대로 OFF')은 kind=set.
- 여러 날의 근무별 하루 인원을 정하는 말('그외 3.3.3', '주말 2.2.3 미드 1', '평일 D 3~4명')은 propose_edit 의 kind=staffing(plans · '안 되면·안될시' 뒤는 다음 안 · 숫자 하나면 count, 범위·이상·이하면 min·max · 고정 근무자를 셀지는 말했을 때만 count_fixed). 앞 인원 계획에 덧붙이는 말은 revise_edit 의 add 에 staffing(한 안만 — 앞 계획의 모든 안에 덧붙는다). 하루 한 근무 인원만이면 kind=cover.
- 근무조마다 등급 하한을 거는 말('매일 모든 근무조에 1등급 1명 이상', '차지급 한 명씩')은 kind=grade_floor(grade·target · 특정 근무조만이면 shifts). '신청 오프 유지'·'신청 오프 넣어'는 kind=wanted 에 shifts ['OFF']. '총 14개'·'한 달 14개'처럼 그 달 전체 개수는 kind=count 에 total true.
- 근무 모양을 고쳐 달라는 말('하루짜리 OFF 없애줘', '퐁당퐁당 없애줘', 'N-O-D 없애줘', '장원영 연속 근무 너무 길어', '김태희 주말 근무 너무 많아')은 propose_edit 의 kind=shape(shape 를 고르고 · 사람·기간·몇 개까지는 말했을 때만). 특정 칸을 바꾸라는 말은 kind=set.
- 같은 달의 다른 저장 버전과 비교하는 말('VER1과 뭐가 달라?', '바로 전 버전이랑 비교해줘')은 compare_versions, 다른 버전으로 되돌리는 말('VER1으로 되돌려줘', '방금 거 취소해줘', '장원영만 VER1처럼 해줘', '15일부터 VER1로')은 revert_version — 버전은 which(VER 번호=number · 바로 전·방금=previous · 처음·원본=first · 가장 최근=latest · '어제 만든'=date · 이름=name), 사람·기간은 말했을 때만.
- 지금 근무표에서 누가·몇 개·무엇인지 묻는 말('15일 나이트 누구야', '장원영 N 몇 개야')은 lookup.
- 찾아 둔 후보(없으면 지금 근무표)가 규칙을 지키는지 묻는 말('그레이드는 다 지켜져?', '1안 연속 근무 괜찮아?', '인원 다 맞아?')은 check_rules.
- 근무표를 만들 때 하는 후처리를 지금 근무표에 해 달라는 말(예: '자동 연차전환 해줘', '남는 OFF 연차로 돌려줘')은 run_postprocess.
- 시스템이 '그래도 진행할까요?'라고 물은 뒤의 '응·그래도 해줘'(go=true) / '아니·그만둘래'(go=false)는 answer_confirm.
- 바꾸지 않고 답할 때(누가·며칠·무엇이 빠짐, 근무표 달 밖 날짜, 설정 변경·새로 만들기, 아직 못 하는 종류의 변경·조회, 상관없는 말, 지금 상태를 묻는 말)는 reply_only 의 kind 를 고른다. 답 문장은 시스템이 만든다.
- 이름은 사용자가 말한 그대로 넣는다(호칭은 뺀다). 이름을 추측하거나 다른 사람으로 바꾸지 않는다.
- 날짜는 근무표 달의 '일' 숫자다. '내일·모레·다음 주 월요일'은 아래 '오늘'로 계산한다.
- 고칠 수 있는 첫날을 정하는 말('5일까지는 그대로·고정' → 6 · '6일부터 고쳐' → 6 · '오늘부터 고쳐' → 오늘 날짜)은 propose_edit·revise_edit 의 edit_from_day(정하지 않았으면 비운다 — 앞에서 정한 날이 이어진다 · 바꿀 내용이 없으면 changes 를 비운다). 예) '5일까지는 그대로 두고 류예리 N 빼줘' → propose_edit(edit_from_day=6, changes=[{kind:count, nurse:'류예리', shift:'N'}]).
- '5일은 그대로'(그날 하나)는 revise_edit 의 keep=[{day:5}], '5일까지 그대로'(그 앞 전부)는 edit_from_day=6 이다.
- 근무: D=데이, E=이브닝, N=나이트, M=미드, OFF=오프·휴무."""


def build_messages(conv: Conversation, session: EditSession, text: str, today: date) -> List[Dict[str, str]]:
    messages: List[Dict[str, str]] = [{"role": "system", "content": SYSTEM}]
    for turn in conv.history[-HISTORY_TURNS:]:
        messages.append({"role": "user", "content": turn["user"]})
        messages.append({"role": "assistant", "content": _clip(turn["answer"])})
    messages.append({"role": "system", "content": state_summary(session, today)})
    messages.append({"role": "user", "content": text})
    return messages


def state_summary(session: EditSession, today: date) -> str:
    ctx = session.ctx
    parts = [f"오늘: {today.isoformat()}({_WEEKDAY[today.weekday()]})",
             f"근무표: {ctx.year}년 {ctx.month}월({ctx.days}일까지)",
             _open_days(session, today),
             f"지금 요청: {current_request(session) or '없음'}"]
    last = session.last
    if last is not None:
        if last.status == "ok" and last.candidates:
            parts.append(f"마지막 결과: 후보 {len(last.candidates)}개(가장 적게 바꾸는 안 {last.candidates[0].changed_count}칸)")
        elif last.reasons:
            parts.append(f"마지막 결과: {last.status} — {_clip(last.reasons[0], 80)}")
    if session.hint is not None:
        parts.append("사용자가 고른 안을 기준으로 다듬는 중")
    if session.asking:
        parts.append(f"확인 기다림: 그대로 바꾸면 생기는 것 {len(session.asking)}가지 — 그래도 진행할지 묻는 중")
    if session.allow:
        parts.append(f"이번 대화에서 넘기도록 허용한 설정 {len(session.allow)}곳")
    return "지금 상태 — " + " · ".join(parts)


def _open_days(session: EditSession, today: date) -> str:
    """'고칠 수 있는 날: 6일부터(오늘까지 고정)' · '…(사용자가 정함 — 5일까지 그대로)' — 정한 날을 이어 가게(사용자 결정 11-05)."""
    ctx = session.ctx
    open_from = first_open_day(ctx, today)
    if open_from >= ctx.days:
        return "고칠 수 있는 날: 없음(이 달에 남은 날이 없음)"
    head = f"고칠 수 있는 날: {open_from + 1}일부터"
    if held_until(ctx, open_from, today):
        return f"{head}(사용자가 정함 — {open_from}일까지 그대로)"
    if ctx.edit_from is not None and ctx.edit_from == open_from:     # 오늘부터 · 다음 달 근무표 1일부터
        return head + "(사용자가 정함)"
    if not open_from:
        return head
    if ctx.edit_from is not None:
        return head + f"(오늘까지 고정 · 사용자가 정한 {ctx.edit_from + 1}일은 이미 지남)"
    return head + "(오늘까지 고정)"


def _clip(text: str, limit: int = ANSWER_CLIP) -> str:
    text = str(text or "")
    return text if len(text) <= limit else text[: limit - 1] + "…"
