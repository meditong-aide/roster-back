"""능력 경계(capability boundary) 섹션 — 환각 방지 규칙이 시스템프롬프트에 인코딩되는지.

행동 검증(미지원 질의 abstention)은 라이브로 확인 완료: '급여 명세서', '카카오톡 공유'
→ '직접 처리 불가 + 실제 대안'(엑셀), 지원 질의는 과도거부 없이 정상.
"""

from __future__ import annotations

import agents_v2.harness.prompt_builder as pb
from agents_v2.schemas.session_context import SessionContext


def _ctx() -> SessionContext:
    return SessionContext(
        office_id="OFF001", group_id="GRP001", year=2026, month=5,
        nurse_id="N001", nurse_name="김민지", user_role="HN",
    )


def test_capability_boundary_in_prompt():
    p = pb.build_system_prompt(_ctx())
    assert "능력 경계" in p
    # 핵심 규칙 3요소: UI 지어내기 금지 / 처리불가 정직 / 되묻기
    assert "지어내" in p
    assert "직접 처리해 드릴 수 없습니다" in p


def test_capability_boundary_is_stable_prefix():
    # 캐싱을 위해 ctx 무관(가변 아님) — 서로 다른 ctx 에서 섹션 텍스트 동일
    a = pb._build_capability_boundary_section()
    b = pb._build_capability_boundary_section()
    assert a == b and "navigate" in a
