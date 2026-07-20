"""DAG 계획(LLMCompiler식) — 의존 복합 요청을 명시적 plan 으로 처리.

설계: docs/AGENT_DAG_PLANNING_DESIGN.md, ADR 0003 §2.
opt-in — 기존 ReAct 루프는 무변경. 의존 복합만 이 경로를 탄다.
"""
