"""B8: analyze_report / generate_schedule / recommend_candidates / repair_schedule
대표 발화 corpus.

이전엔 이 4개 스킬의 자연어 발화→카테고리 라우팅 회귀를 막을 corpus 가 없어,
description 수정 후 분류가 깨져도 e2e 까지 안 가면 발견 못 함.

엔트리 형식: (phrase, expected_category, expected_tool_in_scope)
  - expected_category: router 가 분류해야 할 카테고리 ID
  - expected_tool_in_scope: CATEGORY_TOOLS[expected_category] 에 포함돼야 할 스킬명

이 corpus 는 두 단계 회귀를 잡는다:
  1) router 분류 정확성 (LLM 의존 — 통합 테스트에서 별도 검증)
  2) CATEGORY_TOOLS wiring 일관성 (단위테스트 — 카테고리에 스킬이 살아있나)
"""

from __future__ import annotations

# ── analyze_report (분포·공정성·통계·비교) ───────────────────
ANALYZE_REPORT_PHRASES: list[tuple[str, str, str]] = [
    ("이번 달 OFF 분포 보여줘", "analyze", "analyze_report"),
    ("팀별 나이트 분배 공정성 분석해줘", "analyze", "analyze_report"),
    ("4월하고 5월 시프트 비교 리포트", "analyze", "analyze_report"),
    ("간호사별 근무 부담 통계", "analyze", "analyze_report"),
    ("이번 달 공정성 점수 어때?", "analyze", "analyze_report"),
    ("주말 근무 분포 분석", "analyze", "analyze_report"),
    ("팀 A vs 팀 B 근무량 비교", "analyze", "analyze_report"),
]


# ── generate_schedule (근무표 자동 생성/재생성) ────────────────
GENERATE_SCHEDULE_PHRASES: list[tuple[str, str, str]] = [
    ("근무표 만들어줘", "generate", "generate_schedule"),
    ("5월 근무표 생성해", "generate", "generate_schedule"),
    ("이번 달 근무표 자동으로 짜줘", "generate", "generate_schedule"),
    ("근무표 새로 생성", "generate", "generate_schedule"),
    ("다음 달 스케줄 자동 생성해줘", "generate", "generate_schedule"),
    ("근무표 다시 만들어", "generate", "generate_schedule"),
]


# ── recommend_candidates (대체/교체 후보 추천) ─────────────────
RECOMMEND_CANDIDATES_PHRASES: list[tuple[str, str, str]] = [
    ("5월 3일 나이트 누가 대체 가능해?", "recommend", "recommend_candidates"),
    ("김민지 대신 들어갈 사람 추천해줘", "recommend", "recommend_candidates"),
    ("이 자리 후보 누구?", "recommend", "recommend_candidates"),
    ("빈 자리 채울 사람 추천", "recommend", "recommend_candidates"),
    ("이날 교체 가능한 간호사 알려줘", "recommend", "recommend_candidates"),
    ("대체 후보 보여줘", "recommend", "recommend_candidates"),
]


# ── repair_schedule (위반 검증 + 교정/재조정) ──────────────────
REPAIR_SCHEDULE_PHRASES: list[tuple[str, str, str]] = [
    ("근무표 위반사항 검증해줘", "validate_repair", "validate_schedule"),
    ("왜 이렇게 짜였어?", "validate_repair", "validate_schedule"),
    ("이 근무표 교정해", "validate_repair", "repair_schedule"),
    ("위반 있는지 봐줘", "validate_repair", "validate_schedule"),
    ("문제되는 부분 재조정해줘", "validate_repair", "repair_schedule"),
    ("규칙 위반 어디 있어?", "validate_repair", "validate_schedule"),
]


# 한 자리 모음 — 테스트에서 일괄 반복용
ALL_ADVANCED_PHRASES: list[tuple[str, str, str]] = (
    ANALYZE_REPORT_PHRASES
    + GENERATE_SCHEDULE_PHRASES
    + RECOMMEND_CANDIDATES_PHRASES
    + REPAIR_SCHEDULE_PHRASES
)
