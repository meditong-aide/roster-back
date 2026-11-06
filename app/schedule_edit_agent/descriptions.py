"""수정 도우미 도구 정의 — LLM 함수 호출에 넘기는 JSON 스키마를 한 목록에 모은다(AIDE `agents_v2/skills/descriptions.py`
와 같은 모양 · 사용자 지시 11-06 'AIDE 도구 형식 그대로, 권한은 근무표 수정만').

  · SKILL_TOOLS  : LLM 이 고르는 도구 {name, description, parameters}. 매 턴 같은 글자로 맨 앞에 가 공급자 프롬프트
                   캐시가 걸린다 — 날짜·상태는 넣지 않는다(상태는 `prompt.state_summary` 가 대화 뒤에 붙인다).
  · BUTTON_TOOLS : 카드 버튼 — 모양은 같지만 LLM 에 보이지 않는다(화면이 서버가 준 input 을 그대로 보낸다).
  · SKILL_MODES  : read · preview · write. 쓰기는 적용(apply_edit) 하나뿐이고 새 버전(draft)만 만든다(`middleware` 권한 단계).
실행 함수는 `skills.py` 가 `registry.register(name)` 으로 붙인다. 이 목록은 수정 도우미 전용이다 — AIDE 의
`SKILL_REGISTRY` 와 섞지 않고, AIDE 스킬(조회·일괄 수정·설정 변경·생성 등)은 여기에 없다.
"""

from __future__ import annotations

from typing import Any, Dict, List

from schedule_edit_agent.answer import SHAPE
from schedule_edit_agent.grounding import SHIFTS
from services.schedule_edit import versions
from services.schedule_edit.postprocess import KINDS as POSTPROCESS_KINDS
from services.schedule_edit.requests import SHAPE_LABEL, TRANSFERS
from services.schedule_edit.rule_check import ALL as CHECK_ALL
from services.schedule_edit.rule_check import TOPICS as CHECK_RULE_TOPICS

# ── 인자 스키마 ─────────────────────────────────────────────────────────────

#: 인원 계획 한 줄(staffing.plans 의 항목)
_STAFF_LINE = {
    "type": "object",
    "properties": {
        "days": {"type": "string", "enum": ["all", "weekday", "weekend"],
                 "description": "all=매일·그 외(따로 말한 날 말고 전부) · weekday=평일(월~금) · weekend=주말(토·일)"},
        "dates": {"type": "array", "items": {"type": "integer", "minimum": 1, "maximum": 31},
                  "description": "특정 날을 말했을 때만(days 대신)"},
        "shift": {"type": "string", "enum": ["D", "E", "N", "M"]},
        "count": {"type": "integer", "minimum": 0,
                  "description": "숫자 하나만 말했을 때('3.3.3' → D·E·N 각 3 · '미드 1' → M 1)"},
        "min": {"type": "integer", "minimum": 0, "description": "범위·이상을 말했을 때('3~4명' → 3 · '3명 이상' → 3)"},
        "max": {"type": "integer", "minimum": 0, "description": "범위·이하를 말했을 때('3~4명' → 4)"},
    },
    "required": ["shift"],
}


_CHANGE = {
    "type": "object",
    "properties": {
        "kind": {"type": "string",
                 "enum": ["set", "avoid", "swap", "off_period", "count", "leave", "cover", "balance", "wanted", "shape",
                          "staffing", "grade_floor"],
                 "description": "set=그 칸을 이 근무로 · avoid=그 칸에서 이 근무(들)를 빼기(무엇으로 바꿀지는 맡김) · "
                                "swap=두 사람의 그날 근무 맞바꾸기 · off_period=day 부터 to_day 까지 OFF · "
                                "count=날짜를 정하지 않고 그 사람의 그 근무 개수를 바꾸기(어느 날인지는 맡김) · "
                                "leave=그 날(또는 day~to_day)에 연차·휴가·공가 같은 휴가 코드 넣기(근무일에만) · "
                                "cover=그날 그 근무 인원 늘리기·줄이기(nurse 없이 day·shift + change 또는 target · "
                                "몇 명인지 말하지 않았으면 둘 다 비운다 · "
                                "'1등급 넣어'처럼 등급을 말하면 grade) · "
                                "balance=여러 사람의 그 근무 수를 고르게(nurse 없이 shift · '모두 9개로'면 target · "
                                "'차이 1 이하로'면 spread · 대상은 시스템이 그 근무를 할 수 있는 사람으로 정한다) · "
                                "wanted=반영 안 된 원티드(간호사가 낸 희망 근무) 넣기(사람·기간을 말했을 때만 nurse·day·to_day · "
                                "몇 개를 넣을지는 시스템이 정한다) · "
                                "shape=근무 모양 고치기(shape 를 함께 · 사람·기간을 말했을 때만 nurse·day·to_day · "
                                "몇 개·몇 일·몇 회 이하로라고 말했을 때만 target) · "
                                "staffing=여러 날의 근무별 하루 인원 계획(nurse 없이 plans · '3.3.3' 처럼 여러 날 인원을 "
                                "말할 때 · 하루 한 근무만이면 cover) · "
                                "grade_floor=근무조마다 그 등급까지인 사람 몇 명 이상(nurse 없이 grade·target · "
                                "특정 근무조만 말했으면 shifts)"},
        "plans": {"type": "array", "items": {"type": "array", "items": _STAFF_LINE},
                  "description": "staffing: 차례대로 시도할 인원 계획 — 안 하나는 줄 목록. '안 되면·안될시' 뒤는 다음 안이다"
                                 "(그 안에 말하지 않은 날·근무는 앞 안 그대로 다시 적는다). 예) '그외 3.3.3 안될시 주말 2.2.3 "
                                 "미드 1' → [[매일 D3·E3·N3], [평일 D3·E3·N3, 토일 D2·E2·N3·M1]]"},
        "count_fixed": {"type": "boolean",
                        "description": "staffing·grade_floor: 고정 근무자도 인원에 셀지 사용자가 말했을 때만(말하지 "
                                       "않았으면 비운다 — 시스템이 묻는다)"},
        "total": {"type": "boolean",
                  "description": "count: '총 14개'·'한 달 14개'처럼 그 달 전체 개수를 말했을 때 true(target 이 한 달 개수)"},
        "shape": {"type": "string", "enum": list(SHAPE_LABEL),
                  "description": "shape: isolated_off=근무 사이 OFF 하루(하루짜리 OFF·고립 OFF) · isolated_work=OFF 사이 하루 근무"
                                 "(퐁당퐁당) · pattern=N-O-D·N-O-E·E-O-D 같은 근무 순서 · work_run=연속 근무가 김 · "
                                 "weekend=주말 근무가 몰림·많음"},
        "spread": {"type": "integer", "minimum": 0,
                   "description": "balance: 사람 사이 개수 차이를 몇 이하로 할지 말했을 때만(말하지 않으면 비운다 — "
                                  "시스템이 줄일 수 있는 만큼 줄인다)"},
        "grade": {"type": "integer", "minimum": 1,
                  "description": "cover: 그 등급까지인 사람을 넣거나 뺄 때('20일 N에 1등급 넣어' → grade 1·change 1 · "
                                 "'1등급 빼' → grade 1·change -1). grade 를 쓰면 change 도 넣는다 · grade_floor: 그 등급까지"
                                 "('1등급(차지·서브차지)' → 1)"},
        "leave": {"type": "string",
                  "description": "leave 의 휴가 이름 — 사용자가 말한 그대로(연차·병가·공가·생휴 등). 말하지 않았으면 비운다"},
        "nurse": {"type": "string", "description": "사용자가 말한 간호사 이름 그대로(호칭 '선생님' 등은 빼고)"},
        "other_nurse": {"type": "string", "description": "swap 의 상대 간호사 이름"},
        "day": {"type": "integer", "minimum": 1, "maximum": 31,
                "description": "날짜(그 달의 일). count 에서는 기간을 말했을 때만 그 첫날"},
        "to_day": {"type": "integer", "minimum": 1, "maximum": 31,
                   "description": "off_period·count 기간의 마지막 날(포함). count 에서 기간을 말하지 않았으면 비운다"},
        "change": {"type": "integer",
                   "description": "count·cover: 늘리거나 줄일 개수·인원(2개 빼 → -2, 하나 더 → 1, 한 명 빼 → -1)"},
        "target": {"type": "integer", "minimum": 0,
                   "description": "count·cover·balance: '6개로'·'4명으로'·'모두 9개로' 처럼 목표 개수·인원을 말했을 때 · "
                                  "shape: '연속 근무 4일까지만'·'주말 2번만'·'하루짜리 OFF 3개까지' 처럼 말했을 때 · "
                                  "grade_floor: 근무조마다 몇 명 이상(말하지 않았으면 비운다 — 1명)"},
        "transfer": {"type": "string", "enum": list(TRANSFERS),
                     "description": "count: 상대를 어떻게 정할지 사용자가 이미 말했을 때만(빠지는 근무를 누가 메울지 / "
                                    "누구 것을 넘겨받을지). same_grade=같은 등급 · same_team=같은 팀 · fair=그 근무가 "
                                    "적은(넘겨받을 땐 많은) 사람 · reduce_coverage=메우지 않고 그날 인원 줄이기 · "
                                    "add_coverage=넘겨받지 않고 그날 인원 늘리기 · auto=가장 적게 바꾸기"},
        "shift": {"type": "string", "enum": list(SHIFTS),
                  "description": "set 의 근무. D=데이 E=이브닝 N=나이트 M=미드 OFF=휴무·오프"},
        "shifts": {"type": "array", "items": {"type": "string", "enum": list(SHIFTS)},
                   "description": "avoid 에서 뺄 근무들 · wanted: 그 근무의 원티드만('신청 오프 유지'·'신청 오프 넣어' → "
                                  "['OFF'] · 말하지 않았으면 비운다) · grade_floor: 그 근무조만(모든 근무조면 비운다)"},
    },
    "required": ["kind", "nurse"],
}


#: 고칠 수 있는 첫날(사용자 결정 11-05) — 정한 날은 대화 내내 이어 간다(다른 날을 말하면 덮어쓴다)
_EDIT_FROM = {"type": "integer", "minimum": 1, "maximum": 31,
              "description": "사용자가 고칠 수 있는 첫날을 정했을 때만: 'N일까지는 그대로·고정' → N+1 · 'N일부터 고쳐' → N · "
                             "'오늘부터 고쳐' → 오늘 날짜 · 말하지 않았으면 비운다(앞에서 정한 날이 이어진다)"}


_CELL = {
    "type": "object",
    "properties": {
        "nurse": {"type": "string", "description": "간호사 이름. 비우면 그날 모든 간호사"},
        "day": {"type": "integer", "minimum": 1, "maximum": 31,
                "description": "날짜. remove 에서 비우면 그 간호사의 요청 전부(개수 요청 포함)"},
    },
}


#: 바꾸지 않는 답의 종류 → 정해진 문장(LLM 글을 사용자에게 보내지 않는다 · 검토 M3)
REPLY_KINDS = ("who", "when", "what", "outside_month", "settings", "unsupported", "other", "status", "unclear")


#: 아직 못 하는 종류별 안내(reply_only kind=unsupported 의 topic · 평가 11-03: 조회·되돌리기에도 같은 목록이 나갔다)
UNSUPPORTED_TOPICS = {
    "postprocess": "그 후처리는 아직 여기서 못 합니다. 지금은 자동 연차전환만 할 수 있습니다.",
}


LOOKUP_KINDS = ("day_shift", "person_count", "person_day")


CHECK_TOPICS = (*CHECK_RULE_TOPICS, SHAPE, CHECK_ALL)


_VERSION_PARAMS = {
    "which": {"type": "string", "enum": list(versions.WHICH),
              "description": "어느 버전 — number=VER 번호('VER1') · previous=바로 전·방금 전('방금 거 취소') · "
                             "first=처음·원본 · latest=가장 최근 · date=그날 만든 버전('어제 만든 거' — 오늘로 계산) · "
                             "name=버전 이름을 말했을 때"},
    "number": {"type": "integer", "minimum": 1}, "date": {"type": "string", "description": "YYYY-MM-DD"},
    "name": {"type": "string"},
}


_RANK = {"type": "object", "properties": {"rank": {"type": "integer", "minimum": 1}}, "required": ["rank"]}

def _bare(schema: Any) -> Any:
    """설명(description)만 뺀 같은 모양 — 앞 도구와 같은 항목을 다시 설명하지 않을 때(고쳐 말하기 add · 사용자 선택 11-06)."""
    if isinstance(schema, dict):
        return {k: _bare(v) for k, v in schema.items() if k != "description"}
    if isinstance(schema, list):
        return [_bare(v) for v in schema]
    return schema


# ── LLM 이 고르는 도구 ───────────────────────────────────────────────────────

SKILL_TOOLS: List[Dict[str, Any]] = [
    {
        "name": "propose_edit",
        "description": ("지금 근무표에서 요청한 칸을 바꾸는 방법을 찾는다. 규칙(하드락·설정)은 모두 지키면서 가장 적게 바꾸는 "
                        "후보를 최대 3개 낸다. 새 요청을 받을 때 쓴다(이전 요청은 버린다). "
                        "예) '김민지 14일 오프로 해줘' → changes=[{kind:set, nurse:'김민지', day:14, shift:'OFF'}] · "
                        "'15일 나이트 이영희랑 박서준 바꿔줘' → [{kind:swap, nurse:'이영희', other_nurse:'박서준', day:15}] · "
                        "'정다은 20일 나이트 빼줘' → [{kind:avoid, nurse:'정다은', day:20, shifts:['N']}] · "
                        "'장원영 N 2개만 빼줘' → [{kind:count, nurse:'장원영', shift:'N', change:-2}] · "
                        "'장원영 나이트 너무 많아'(개수 없음) → [{kind:count, nurse:'장원영', shift:'N'}](시스템이 현황을 보여 주고 되묻는다) · "
                        "'N 근무 수 고르게 맞춰줘' → [{kind:balance, shift:'N'}] · 'OFF 다 9개로 맞춰줘' → [{kind:balance, shift:'OFF', target:9}] · "
                        "'하루짜리 OFF 없애줘' → [{kind:shape, shape:'isolated_off', nurse:''}] · '장원영 연속 근무 너무 길어' → "
                        "[{kind:shape, shape:'work_run', nurse:'장원영'}] · '김태희 주말 근무 너무 많아' → [{kind:shape, shape:'weekend', nurse:'김태희'}] · "
                        "'5일까지는 고정해'(바꿀 내용 없음) → edit_from_day=6 · changes 는 비운다 · "
                        "'류예리 총 오프 14개, 나이트 없음. 그외 3.3.3 안될시 주말 2.2.3 미드 1. 신청 오프 유지' → "
                        "[{kind:count, nurse:'류예리', shift:'OFF', target:14, total:true}, {kind:count, nurse:'류예리', shift:'N', "
                        "target:0, total:true}, {kind:staffing, nurse:'', plans:[[{days:'all', shift:'D', count:3}, {days:'all', "
                        "shift:'E', count:3}, {days:'all', shift:'N', count:3}], [{days:'weekday', shift:'D', count:3}, {days:'weekday', "
                        "shift:'E', count:3}, {days:'weekday', shift:'N', count:3}, {days:'weekend', shift:'D', count:2}, "
                        "{days:'weekend', shift:'E', count:2}, {days:'weekend', shift:'N', count:3}, {days:'weekend', shift:'M', "
                        "count:1}]]}, {kind:wanted, nurse:'', shifts:['OFF']}]"),
        "parameters": {"type": "object", "properties": {"changes": {"type": "array", "items": _CHANGE},
                                                        "edit_from_day": _EDIT_FROM}},
    },
    {
        "name": "revise_edit",
        "description": ("바로 앞 요청을 고쳐 다시 찾는다. add=요청 더하기, remove=그 칸 요청 빼기, keep=이 칸(또는 그날 전체)은 "
                        "지금 근무 그대로 두기. 예) '그리고 이영희 16일도 오프' → add · '15일은 건드리지 마' → keep=[{day:15}] · "
                        "'김민지 14일 건은 취소' → remove=[{nurse:'김민지', day:14}] · '15일 인원 요청은 빼' → remove=[{day:15}] · "
                        "개수 요청에서 빠지는 근무를 메우는 방식을 말하면 transfer('같은 등급으로' → same_grade) · "
                        "'그리고 5일까지는 그대로 둬' → edit_from_day=6(앞 요청을 6일부터로 다시 찾는다) · "
                        "인원 계획에 덧붙이는 말 '평일 D 는 3~4명' → add=[{kind:staffing, nurse:'', plans:[[{days:'weekday', shift:'D', "
                        "min:3, max:4}]]}](앞 계획의 모든 안에 덧붙는다) · '매일 모든 근무조에 1등급 1명 이상' → "
                        "add=[{kind:grade_floor, nurse:'', grade:1, target:1}]"),
        "parameters": {"type": "object", "properties": {
            "add": {"type": "array", "items": _bare(_CHANGE),
                    "description": "더할 요청 — propose_edit 의 changes 항목과 같은 모양·뜻"},
            "remove": {"type": "array", "items": _CELL},
            "keep": {"type": "array", "items": _CELL},
            "transfer": {"type": "string", "enum": list(TRANSFERS)},
            "edit_from_day": _EDIT_FROM,
        }},
    },
    {
        "name": "reply_only",
        "description": ("근무표를 바꾸지 않고 답할 때 부른다. 답 문장은 시스템이 정해진 틀로 만든다. kind: who=누구인지 빠짐 · "
                        "when=며칠인지 빠짐 · what=무엇으로 바꿀지 빠짐 · outside_month=근무표 달 밖의 날짜 · settings=병동 설정값 자체"
                        "(하루 인원 기준·규칙·OFF 기준 수)를 바꾸는 말이나 근무표 새로 만들기 — '사람들 OFF·근무 수를 고르게 맞춰 줘'는 "
                        "reply_only 가 아니라 propose_edit 의 kind=balance · unsupported=근무표를 바꾸는 말이지만 아직 못 하는 종류 — "
                        "topic 을 함께 고른다(연속 근무·주말 몰림·하루짜리 OFF 같은 근무 모양은 propose_edit 의 kind=shape, "
                        "다른 버전과 비교·되돌리기는 compare_versions·revert_version): "
                        "postprocess=자동 연차전환 밖의 생성 후처리(보건휴가·수면 OFF 등) · other=근무표와 상관없는 말 · "
                        "status=지금 요청·결과가 어떤지 묻는 말"
                        "(예: '바꿔 줬어?', '지금 뭐 하고 있었지?') · unclear=그 밖에 알아듣기 어려움"),
        "parameters": {"type": "object", "properties": {"kind": {"type": "string", "enum": list(REPLY_KINDS)},
                                                        "topic": {"type": "string", "enum": list(UNSUPPORTED_TOPICS)}},
                       "required": ["kind"]},
    },
    {
        "name": "answer_confirm",
        "description": ("시스템이 '그래도 진행할까요?'라고 물은 뒤 사용자가 말로 답할 때만. 진행하자는 말('응', '그래도 해줘', "
                        "'괜찮아 바꿔')은 go=true, 그만두자는 말('아니', '그럼 하지 마', '취소')은 go=false. 다른 내용을 새로 "
                        "요청하거나 고쳐 말하면 이것 말고 propose_edit·revise_edit."),
        "parameters": {"type": "object", "properties": {"go": {"type": "boolean"}}, "required": ["go"]},
    },
    {
        "name": "lookup",
        "description": ("근무표를 바꾸지 않고 지금 저장된 근무표에서 찾아 답한다(조회). kind: day_shift=그날 그 근무가 누구인지"
                        "('15일 나이트 누구야') · person_count=그 사람의 그 달 근무 개수('장원영 N 몇 개야', '권은비 OFF 몇 개') · "
                        "person_day=그 사람의 그날 근무('장원영 15일 뭐야')."),
        "parameters": {"type": "object", "properties": {
               "kind": {"type": "string", "enum": list(LOOKUP_KINDS)},
               "nurse": {"type": "string", "description": "사용자가 말한 간호사 이름 그대로"},
               "day": {"type": "integer", "minimum": 1, "maximum": 31},
               "shift": {"type": "string", "enum": list(SHIFTS)}}, "required": ["kind"]},
    },
    {
        "name": "check_rules",
        "description": ("근무표를 바꾸지 않고, 찾아 둔 후보(없으면 지금 저장된 근무표)가 규칙을 지키는지 묻는 말에 답한다"
                        "('그레이드는 다 지켜져?', '2안 나이트 규칙 괜찮아?', '인원 다 맞아?', '문제없는 거지?'). topic: "
                        "grade=등급(그레이드)별 최소 인원 · coverage=그날 근무 인원(최소·최대) · team=팀별 최소 인원 · "
                        "consecutive=연속 근무·같은 근무 연속·하루만 근무 · night=나이트 규칙(연속 N·N 뒤 OFF·1N·N 간격·월 N 상한·"
                        "휴가 전날 N) · transition=근무 전환 금지(N→D 등) · off=OFF 수·월 근무 한도 · shift_rule=허용 근무형·기피 근무·"
                        "주말휴무 · mutex=상호배제 · wanted=반영돼 있던 원티드를 지키는지 · shape=근무 모양(N-O-D 같은 순서·하루짜리 OFF·"
                        "몰림) · all=어느 규칙인지 말하지 않음('다 지켜져?'). rank=사용자가 '1안'·'두 번째 안'처럼 후보를 짚었을 때만."),
        "parameters": {"type": "object", "properties": {
               "topic": {"type": "string", "enum": list(CHECK_TOPICS)},
               "rank": {"type": "integer", "minimum": 1}}, "required": ["topic"]},
    },
    {
        "name": "run_postprocess",
        "description": ("근무표를 만들 때 서비스가 저장 직전에 하는 후처리를 지금 근무표에 다시 돌려 본다(저장 전 미리보기 · 적용하면 "
                        "새 버전). kind: annual_leave_swap=자동 연차전환(병동 OFF 기준을 넘는 OFF 를 연차 코드로 — '자동 연차전환 "
                        "해줘', '남는 OFF 연차로 돌려줘', '연차 전환 써줘')."),
        "parameters": {"type": "object", "properties": {"kind": {"type": "string", "enum": list(POSTPROCESS_KINDS)}},
                       "required": ["kind"]},
    },
    {
        "name": "compare_versions",
        "description": ("근무표를 바꾸지 않고 같은 달의 다른 저장 버전과 지금 버전의 다른 칸을 보여 준다('VER1과 뭐가 달라?', "
                        "'바로 전 버전이랑 비교해줘', '처음 버전과 비교', '장원영 VER1이랑 뭐가 달라'). nurse 는 말했을 때만."),
        "parameters": {"type": "object", "properties": {**_VERSION_PARAMS, "nurse": {"type": "string"}},
                       "required": ["which"]},
    },
    {
        "name": "revert_version",
        "description": ("같은 달의 다른 저장 버전으로 되돌린다. 사람·기간을 말하지 않으면 그 버전을 화면에 연다(저장 없음 · 지금 버전은 "
                        "그대로 — 'VER1으로 되돌려줘', '방금 거 취소해줘'=which previous). 사람(nurses)·기간(day~to_day)을 말하면 "
                        "그 칸만 그 버전 값으로 바꾸는 안을 찾는다('장원영만 VER1처럼 해줘', '15일부터 VER1로 되돌려줘')."),
        "parameters": {"type": "object", "properties": {
               **_VERSION_PARAMS,
               "nurses": {"type": "array", "items": {"type": "string"}, "description": "말한 간호사 이름들 그대로"},
               "day": {"type": "integer", "minimum": 1, "maximum": 31,
                       "description": "기간 첫날 — '15일부터'면 day=15·to_day=그 달 마지막 날, '10일까지'면 day=1·to_day=10"},
               "to_day": {"type": "integer", "minimum": 1, "maximum": 31}},
               "required": ["which"]},
    },
]

# ── 카드 버튼(LLM 에 보이지 않는다) ───────────────────────────────────────────

BUTTON_TOOLS: List[Dict[str, Any]] = [
    {
        "name": "open_version",
        "description": "비교 카드의 '그 버전 열기' — 그 버전을 화면에 연다(저장 없음).",
        "parameters": {"type": "object", "properties": {"schedule_id": {"type": "string"}},
                       "required": ["schedule_id"]},
    },
    {
        "name": "apply_edit",
        "description": "고른 후보로 새 버전(draft)을 만든다.",
        "parameters": {"type": "object", "properties": {"rank": {"type": "integer", "minimum": 1},
                                                        "name": {"type": "string"},
                                                        "request_text": {"type": "string"},
                                                        "confirmed": {"type": "boolean"},
                                                        "seen": {"type": "array", "items": {"type": "string"}}},
                       "required": ["rank"]},
    },
    {
        "name": "back_to_candidates",
        "description": "적용 전 확인에서 '다른 안 보기' — 적용하지 않고 후보로 돌아간다.",
        "parameters": {"type": "object", "properties": {"rank": {"type": "integer", "minimum": 1}}},
    },
    {
        "name": "retry_request",
        "description": "지금 요청으로 다시 찾는다 — 서버가 다시 떠 찾아 둔 후보가 없어졌을 때.",
        "parameters": {"type": "object", "properties": {}},
    },
    {
        "name": "confirm_request",
        "description": ("바꾸기 전 확인에서 '그래도 진행' — 짚은 것을 이번만 넘기고 후보를 찾는다. "
                         "pick 이 있으면 둘 중 고른 그 하나만 받아들인다."),
        "parameters": {"type": "object", "properties": {"pick": {"type": "string"}}},
    },
    {
        "name": "cancel_request",
        "description": ("바꾸기 전 확인에서 '바꾸지 않기' — 이 요청을 거두고 앞 상태로 돌아간다. "
                         "restate=true 면 인원 계획 확인의 '인원을 다시 말하기'(근무표는 그대로)."),
        "parameters": {"type": "object", "properties": {"restate": {"type": "boolean"}}},
    },
    {
        "name": "choose_staffing",
        "description": ("인원 계획에서 정하지 않은 것을 고른다 — count_fixed=고정 근무자도 센다 · "
                        "exclude_fixed=빼고 센다 · exact=숫자만 말한 인원을 정확히 · at_least=그 수 이상."),
        "parameters": {"type": "object", "properties": {"choice_id": {"type": "string",
                                                                      "enum": ["count_fixed", "exclude_fixed",
                                                                               "exact", "at_least"]}},
                       "required": ["choice_id"]},
    },
    {
        "name": "reject_candidate",
        "description": "이 후보는 싫다 — 같은 사람들을 바꾸는 안은 빼고 다시 찾는다.",
        "parameters": _RANK,
    },
    {
        "name": "keep_candidate",
        "description": "이 후보를 기준으로 이어서 다듬는다(다음 탐색의 시작 해).",
        "parameters": _RANK,
    },
    {
        "name": "choose_lever",
        "description": "레버를 고른다 — 그 설정을 그만큼만 넘기도록 허용하고 다시 찾는다.",
        "parameters": {"type": "object", "properties": {"lever_id": {"type": "string"}}, "required": ["lever_id"]},
    },
    {
        "name": "choose_transfer",
        "description": "개수 요청에서 빠지는 근무를 메우는 방식을 고르고 그 방식으로 찾는다.",
        "parameters": {"type": "object", "properties": {"choice_id": {"type": "string", "enum": list(TRANSFERS)}},
                       "required": ["choice_id"]},
    },
]

#: 도구마다 읽기·미리보기·쓰기 — 권한 단계가 본다(쓰기는 버튼으로만)
SKILL_MODES: Dict[str, str] = {
    "propose_edit": "preview",
    "revise_edit": "preview",
    "reply_only": "read",
    "answer_confirm": "preview",
    "lookup": "read",
    "check_rules": "read",
    "run_postprocess": "preview",
    "compare_versions": "read",
    "revert_version": "preview",
    "open_version": "read",
    "apply_edit": "write",
    "back_to_candidates": "read",
    "retry_request": "preview",
    "confirm_request": "preview",
    "cancel_request": "read",
    "choose_staffing": "preview",
    "reject_candidate": "preview",
    "keep_candidate": "read",
    "choose_lever": "preview",
    "choose_transfer": "preview",
}
