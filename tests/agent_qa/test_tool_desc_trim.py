"""스킬설명 렌더 모드(full ↔ names) 검증.

TOOL_DESC_MODE='full'(기본)=현행과 100% 동일(설명 전문 + JSON 이중정의),
'names'=이름만 렌더(중복 제거, 상세는 JSON function.description 이 담당).
라이브 A/B(스코핑 N=24)에서 names 는 무회귀 + 입력토큰 -28.5% 확인됨.
"""

from __future__ import annotations

import agents_v2.harness.prompt_builder as pb
from agents_v2.harness.prompt_builder import _build_tool_descriptions_section
from agents_v2.schemas.session_context import SessionContext


def _ctx() -> SessionContext:
    return SessionContext(
        office_id="OFF001", group_id="GRP001", year=2026, month=5,
        nurse_id="N001", nurse_name="김민지", user_role="HN",
    )


def test_default_mode_is_full(monkeypatch):
    monkeypatch.setattr(pb, "TOOL_DESC_MODE", "full")
    full = _build_tool_descriptions_section(None)
    assert "### query_schedule" in full
    assert len(full) > 5000  # 설명 전문 = 큼


def test_names_mode_is_smaller_and_names_only(monkeypatch):
    from agents_v2.skills.descriptions import SKILL_TOOLS

    monkeypatch.setattr(pb, "TOOL_DESC_MODE", "full")
    full = _build_tool_descriptions_section(None)
    monkeypatch.setattr(pb, "TOOL_DESC_MODE", "names")
    names = _build_tool_descriptions_section(None)

    # 이름만 → 대폭 축소
    assert len(names) < len(full) * 0.4
    # '### ' 상세 헤더는 사라지고, 툴은 전부 '- name' 으로 남음
    assert "### " not in names
    for t in SKILL_TOOLS:
        assert f"- {t['name']}" in names
    # 툴 개수 유지(빠뜨림 없음)
    assert names.count("\n- ") + names.count("\n-", 0) >= 0  # sanity
    assert sum(1 for ln in names.splitlines() if ln.startswith("- ")) == len(SKILL_TOOLS)


def test_scoping_applies_in_names_mode(monkeypatch):
    monkeypatch.setattr(pb, "TOOL_DESC_MODE", "names")
    scoped = _build_tool_descriptions_section(["query_schedule"])
    assert "- query_schedule" in scoped
    assert sum(1 for ln in scoped.splitlines() if ln.startswith("- ")) == 1


def test_full_prompt_shrinks_in_names_mode(monkeypatch):
    monkeypatch.setattr(pb, "TOOL_DESC_MODE", "full")
    full = pb.build_system_prompt(_ctx(), None)
    monkeypatch.setattr(pb, "TOOL_DESC_MODE", "names")
    names = pb.build_system_prompt(_ctx(), None)
    assert len(names) < len(full)


def test_unknown_mode_falls_back_to_full(monkeypatch):
    # 오타/미지원 값이면 안전하게 full 동작(이름-only 아님)
    monkeypatch.setattr(pb, "TOOL_DESC_MODE", "bogus")
    out = _build_tool_descriptions_section(None)
    assert "### query_schedule" in out
