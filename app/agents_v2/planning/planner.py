"""Planner — LLM 이 요청을 DAG plan 으로(LLMCompiler [1]).

**의존 복합**(한 작업 출력이 다음 입력)만 다중 task plan 을 낸다. 단일/독립/단순은
None 반환 → 호출부가 기존 ReAct 로 처리(opt-in, ReAct 무변경).

설계: docs/AGENT_DAG_PLANNING_DESIGN.md §3·§6.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from agents_v2.planning.plan import Plan, PlanTask

logger = logging.getLogger(__name__)

_PLANNER_SYS = """너는 간호사 근무 스케줄링 에이전트의 **플래너**다.
사용자 요청을 실행 계획(DAG)으로 만든다. 출력은 JSON 하나뿐.

형식:
{"tasks": [
  {"id":"t1","skill":"<스킬명>","args":{...},"deps":[],"kind":"read"},
  {"id":"t2","skill":"<스킬명>","args":{...},"deps":["t1"],"kind":"mutate"}
]}

규칙 — **의존을 정확히 판단하는 게 핵심이다**. 무조건 순차로 엮지 마라.
task 간 **실제 의존이 있을 때만** deps 를 넣는다. 의존 유형 3가지:
  1) **데이터 흐름**: 앞 작업의 출력을 뒤가 입력으로 쓴다 → 뒤가 앞에 의존.
     args 값에 `$t1.field.path` 참조를 넣는다(예: "$t1.candidates[0].name").
     예) "대체자 찾아서 그 사람 배정" → t1 추천(read) → t2 배정(mutate, deps=[t1]).
  2) **덮어쓰기(override)**: 같은 대상에 **넓은 설정 뒤 좁은 설정** → 좁은 것이 넓은 것에 의존.
     예) "8월 전체 322" 다음 "주말 222" → 주말이 전체에 deps=[전체] (안 그럼 전체가 주말을 덮음).
  3) **설정→실행**: 설정 변경들 다음에 **생성/검증/재조정** → 그 실행이 설정들 전부에 의존.
     예) "...설정하고 돌려줘(생성)" → 생성 task 가 앞 설정 task 들에 deps.

**그 외 서로 다른 대상·겹치지 않는 설정은 독립 → deps 를 비운다**(억지 순서 금지).
  예) "데이 필요인원 5명, off 10개, 김민지 등급 3" → 셋 다 무관, deps=[] (병렬).

**집합 대상 작업은 쪼개지 마라(중요)**: 대상이 "전원/전부/모두/제출된 거 다/낸 사람 전부"처럼
**집합**이면, 그 집합은 **스킬이 내부에서 해소**한다(스킬은 개별 대상 필터를 안 주면 전체를 처리).
따라서 "조회(누가 냈나)→각각 처리" 체인으로 **분해하지 마라**. **단일 태스크 하나**로 낸다.
  예) "제출된 원티드 다 승인해줘" → bulk_mutation **한 개**(승인 대상 조회 태스크 불필요).
  예) "미제출 확인하고 마감 연장하고 낸 거 전원 승인" → 마감연장 1개 + 전원승인 1개 (미제출 조회
     태스크는 넣지 마라; 승인 스킬이 대상 집합을 스스로 찾는다).

**대타(빈 슬롯 충원)는 recommend_candidates 를 여러 날짜로 반복하지 마라**: recommend 는
**하루 한 자리 추천만**이다. "빈 날 전부 채우기 / 기존 표 빈칸만 메꾸기"는 **지원 안 함**
(확정표 유지한 채 부분 채우기 기능 없음). 여러 날 재배치가 필요하면 근무표 **재생성
(generate_schedule)** 단일 태스크로 낸다(전체를 다시 푼다 — 다른 근무도 바뀔 수 있음).
recommend→assign 을 날짜마다 fan-out 하지 마라.
  예) "파견 보내고 다시 짜줘" → 파견(assign) + generate.

반환 조건:
- 위 의존이 **하나라도** 있으면 그 구조로 plan 을 낸다.
- **단일 작업**이거나 **모든 작업이 완전 독립**(의존 하나도 없음)이면 `{"tasks": []}` (→ 시스템이 기존 방식으로 병렬 처리).
  예) "근무표 보여줘", "김민지 등급 3으로" → {"tasks": []}.
- kind: 조회=read / 변경=mutate. **아래 목록의 스킬만** 쓴다. 확신 없으면 {"tasks": []}.
- 설명·주석 없이 JSON 만 출력.

사용 가능한 스킬:
"""


def _skills_brief(skill_tools: list[dict]) -> str:
    """스킬 이름 + 한줄설명 + **파라미터 스키마(이름·enum)**. args 를 정확히 만들게 하려면 필수."""
    lines = []
    for t in skill_tools:
        name = t.get("name", "")
        desc = (t.get("description") or "").strip().splitlines()
        head = desc[0].strip() if desc else ""
        props = (t.get("parameters") or {}).get("properties") or {}
        params = []
        for pk, pv in props.items():
            enum = pv.get("enum")
            pdesc = (pv.get("description") or "").strip()
            tag = f"={enum}" if enum else (f"({pdesc[:24]})" if pdesc else "")
            params.append(f"{pk}{tag}")
        pline = ("\n    params: " + ", ".join(params)) if params else ""
        lines.append(f"- {name}: {head[:80]}{pline}")
    return "\n".join(lines)


def _parse_plan(text: str) -> Plan | None:
    if not text:
        return None
    s = text.strip()
    if s.startswith("```"):  # 코드펜스 제거
        s = s.strip("`")
        s = s[s.find("{"):] if "{" in s else s
    try:
        obj = json.loads(s[s.find("{"): s.rfind("}") + 1])
    except (ValueError, json.JSONDecodeError):
        return None
    raw = obj.get("tasks")
    if not isinstance(raw, list):
        return None
    tasks = []
    for r in raw:
        if not isinstance(r, dict) or not r.get("id") or not r.get("skill"):
            return None
        tasks.append(PlanTask(
            id=str(r["id"]), skill=str(r["skill"]),
            args=r.get("args") or {},
            deps=[str(d) for d in (r.get("deps") or [])],
            kind="mutate" if str(r.get("kind", "read")).lower() == "mutate" else "read",
        ))
    return Plan(tasks=tasks)


def build_plan(llm: Any, user_message: str, skill_tools: list[dict],
               *, feedback: str | None = None) -> Plan | None:
    """요청 → DAG plan. 의존 복합만 다중 task, 그 외 None(ReAct fallback).

    feedback: 직전 plan 실행이 실패했을 때 그 관찰(어느 task 가 왜 실패, 이전 구조)을 넣어
    **교정된 plan** 을 다시 만들게 한다(LLMCompiler replan). 여전히 교정 불가면 None → ReAct.
    """
    sys = _PLANNER_SYS + _skills_brief(skill_tools)
    user = user_message
    if feedback:
        user = (f"{user_message}\n\n[직전 계획 실행 실패 — 교정해서 다시 계획하라]\n{feedback}\n"
                "위 실패를 피하도록 args/스킬/의존을 고쳐라. 교정이 불가능하면 {\"tasks\": []} 를 반환하라.")
    try:
        resp = llm.chat(
            [{"role": "system", "content": sys}, {"role": "user", "content": user}],
            tools=[],
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("[planner] LLM 호출 실패: %s", e)
        return None
    plan = _parse_plan(getattr(resp, "text", None))
    if plan is None or len(plan.tasks) <= 1:
        return None  # 단일/단순/파싱실패 → ReAct
    try:
        plan.validate()
    except ValueError as e:
        logger.warning("[planner] 부정 plan(%s) → ReAct fallback", e)
        return None
    return plan
