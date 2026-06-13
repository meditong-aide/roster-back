"""P0-A·B (Group Scope SSOT): nurse_tools 의 month-aware 읽기·쓰기 검증.

dev 의 시점 기반 SSOT(NurseTeamPeriod / group_members_in_month) 와
agent 스킬의 정합성 회귀 차단:

  - 읽기: year/month 가 주어지면 group_members_in_month 로 멤버 한정,
          NurseTeamPeriod 로 team 필터 → 전입/전출 반영.
  - 쓰기: update_nurse_attributes_batch 의 team_id 변경이 NurseTeamPeriod 에도
          기록되어 SSOT 와 캐시가 동기화.

month 인자 미지정 시엔 옛 캐시 경로 그대로(후방 호환).
"""

from __future__ import annotations

from agents_v2.tools import nurse_tools


# ── 내부 유틸 동작 ───────────────────────────────────────────


def test_month_member_ids_none_when_year_or_month_missing():
    """year/month 한쪽만 있으면 None — 캐시 경로로 폴백."""
    assert nurse_tools._month_member_ids(None, "GRP001", None, 6) is None
    assert nurse_tools._month_member_ids(None, "GRP001", 2026, None) is None
    assert nurse_tools._month_member_ids(None, "GRP001", None, None) is None


def test_resolve_month_team_none_when_year_or_month_missing():
    assert nurse_tools._resolve_month_team(None, "N001", "GRP001", None, 6) is None
    assert nurse_tools._resolve_month_team(None, "N001", "GRP001", 2026, None) is None


def test_resolve_month_team_silent_on_failure():
    """services 임포트나 호출 실패 시 None — 호출부는 캐시 경로로 폴백."""

    class _BadDb:
        def query(self, *a, **kw):
            raise RuntimeError("simulated db error")

    assert nurse_tools._resolve_month_team(_BadDb(), "N001", "GRP001", 2026, 6) is None


def test_month_member_ids_silent_on_failure():
    class _BadDb:
        def query(self, *a, **kw):
            raise RuntimeError("boom")

    assert nurse_tools._month_member_ids(_BadDb(), "GRP001", 2026, 6) is None


# ── update_nurse_attributes_batch 시그니처 — year/month 받아들이나 ──


def test_update_signature_accepts_year_month_kwargs():
    """signature 회귀 — skill 이 year/month=None 으로 호출해도 깨지지 않아야."""
    import inspect

    sig = inspect.signature(nurse_tools.update_nurse_attributes_batch)
    assert "year" in sig.parameters
    assert "month" in sig.parameters
    # 기본값 None — 호출 부담 없이 후방 호환.
    assert sig.parameters["year"].default is None
    assert sig.parameters["month"].default is None


def test_get_nurses_in_group_signature_accepts_year_month_kwargs():
    import inspect

    sig = inspect.signature(nurse_tools.get_nurses_in_group)
    assert "year" in sig.parameters
    assert "month" in sig.parameters


def test_filter_nurses_signature_accepts_year_month_kwargs():
    import inspect

    sig = inspect.signature(nurse_tools.filter_nurses)
    assert "year" in sig.parameters
    assert "month" in sig.parameters


def test_search_nurses_by_name_signature_accepts_year_month_kwargs():
    import inspect

    sig = inspect.signature(nurse_tools.search_nurses_by_name)
    assert "year" in sig.parameters
    assert "month" in sig.parameters
