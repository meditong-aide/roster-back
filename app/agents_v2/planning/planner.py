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

규칙:
- **의존 복합**일 때만 다중 task 를 만든다 = 한 작업의 출력을 다음 작업이 입력으로 쓰는 경우.
  예) "대체자 찾아서 그 사람 배정" → t1=추천(read), t2=배정(mutate, t1 결과 사용).
- **단일/독립/단순** 요청이면 `{"tasks": []}` 를 반환한다(그럼 시스템이 기존 방식으로 처리).
  예) "근무표 보여줘", "김민지 등급 3으로" → {"tasks": []}.
- 다음 task 가 이전 출력을 쓰면 args 값에 `$t1.field.path` 참조를 넣는다(예: "$t1.candidates[0].name").
- kind: 조회=read / 변경=mutate.
- **아래 목록의 스킬만** 쓴다. 확신 없으면 {"tasks": []}.
- 설명·주석 없이 JSON 만 출력.

사용 가능한 스킬:
"""


def _skills_brief(skill_tools: list[dict]) -> str:
    lines = []
    for t in skill_tools:
        name = t.get("name", "")
        desc = (t.get("description") or "").strip().splitlines()
        head = desc[0].strip() if desc else ""
        lines.append(f"- {name}: {head[:80]}")
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


def build_plan(llm: Any, user_message: str, skill_tools: list[dict]) -> Plan | None:
    """요청 → DAG plan. 의존 복합만 다중 task, 그 외 None(ReAct fallback)."""
    sys = _PLANNER_SYS + _skills_brief(skill_tools)
    try:
        resp = llm.chat(
            [{"role": "system", "content": sys}, {"role": "user", "content": user_message}],
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
