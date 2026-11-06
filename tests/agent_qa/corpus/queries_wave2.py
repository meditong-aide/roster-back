"""Wave-2 corpus — 팀 CRUD + 원티드 한도 정리 발화 모음.

대상 스킬:
  - manage_teams (팀 CRUD)
  - manage_wanted_limits (원티드 한도 초과 조회/정리)

엔트리 형식: (phrase, expected_category, expected_tool_in_scope)
"""

from __future__ import annotations

# ── manage_teams ────────────────────────────────────────
# [NAV_FIRST_TEAMS 2026-06-19] 조건 없는 목록 발화('팀 목록 보여줘' 등)는
# navigate(nurse_management/team_setting) 로 빠짐 → 본 corpus 에서는 제외.
# 단일 팀 조회 + mutation 만 본 스킬 담당.
MANAGE_TEAMS_PHRASES: list[tuple[str, str, str]] = [
    ("A팀 멤버 누구야?", "settings_people", "manage_teams"),
    ("신생아실 팀 추가해줘", "settings_people", "manage_teams"),
    ("A팀 이름을 응급실로 바꿔줘", "settings_people", "manage_teams"),
    ("C팀 삭제해줘", "settings_people", "manage_teams"),
    ("B팀 없애줘", "settings_people", "manage_teams"),
    ("1팀 추가해", "settings_people", "manage_teams"),
]


# ── manage_wanted_limits ────────────────────────────────
MANAGE_WANTED_LIMITS_PHRASES: list[tuple[str, str, str]] = [
    ("7월 원티드 한도 넘은 사람 누구야?", "mutate", "manage_wanted_limits"),
    ("이번 달 원티드 한도 초과자 보여줘", "mutate", "manage_wanted_limits"),
    ("박지은 7월 원티드 초과분 정리해줘", "mutate", "manage_wanted_limits"),
    ("김민지 원티드 초과 삭제", "mutate", "manage_wanted_limits"),
    ("이번 달 원티드 한도 넘은 명단", "mutate", "manage_wanted_limits"),
]


ALL_WAVE2_PHRASES: list[tuple[str, str, str]] = (
    MANAGE_TEAMS_PHRASES + MANAGE_WANTED_LIMITS_PHRASES
)
