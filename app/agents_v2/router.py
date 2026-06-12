"""Query router — 2단계 tool 스코핑.

메인 에이전트 루프 앞단에서 질의를 **LLM이 카테고리로 분류**하고,
`CATEGORY_TOOLS` 배선 맵으로 메인 루프에 넘길 tool subset 을 추출한다.

설계 원칙 (CLAUDE.md):
- 분류(classify)는 LLM 의미판단(top-down) — 키워드/regex lookup 금지.
- category → tools 는 **단순 wiring 맵**(자연어 파싱이 아님).
- coarse 스코핑: gray-zone(navigate vs read/generate, 설정 read vs 화면이동)은
  카테고리에 navigate/prefill 을 번들 → navigate-vs-read 미세결정은 메인 프롬프트의
  전역 원칙(NAV_FIRST)이 맡는다.
- fallback: 분류 실패/저신뢰/unknown → 전체 tool. **절대 hard-block(빈 set) 안 함.**
  (DeterministicClient 는 text 분류를 못 내므로 자동 fallback → 기존 테스트 경로 보존.)
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field

from agents_v2.llm_client import LLMClient
from agents_v2.skills.descriptions import SKILL_TOOLS

logger = logging.getLogger(__name__)

# 전체 tool 이름 (SKILL_TOOLS 정의 순서 보존 — scoped subset 도 이 순서로 정렬).
ALL_TOOL_NAMES: list[str] = [t["name"] for t in SKILL_TOOLS]

# ── 카테고리 → tool 배선 맵 (wiring, NL 파싱 아님) ──
# gray-zone 도메인엔 navigate/prefill 을 번들해 메인 프롬프트가 화면이동 vs 조회/실행을 가른다.
CATEGORY_TOOLS: dict[str, list[str]] = {
    # 순수 화면 이동/폼 프리필
    "navigation": ["navigate", "prefill"],
    # 근무표/원티드/간호사/시프트/설정값 조회 (view-vs-derive gray → navigate 번들)
    "read": ["query_schedule", "navigate"],
    # 근무/원티드 변경
    "mutate": ["bulk_mutation", "query_schedule"],
    # 근무표 자동 생성 (생성 vs roster_create 화면 gray → navigate 번들)
    "generate": ["generate_schedule", "navigate", "query_schedule"],
    # 제약 위반 검증 / 교정 제안
    "validate_repair": ["validate_schedule", "repair_schedule", "query_schedule"],
    # 분포·공정성·통계 분석
    "analyze": ["analyze_report", "query_schedule"],
    # 대체/교체 간호사 추천
    "recommend": ["recommend_candidates", "query_schedule"],
    # 병동 규칙/제약/월 한도 정책 변경 (설정 화면 gray → navigate/prefill 번들)
    "settings_rules": [
        "update_constraint", "update_monthly_limit",
        "query_schedule", "navigate", "prefill",
    ],
    # 등급/팀 최소인원/개인 속성 설정 (근무자관리 화면 gray → navigate/prefill 번들)
    "settings_people": [
        "manage_grade", "manage_team_min", "update_person_attr",
        "navigate", "prefill",
    ],
}

VALID_CATEGORIES: frozenset[str] = frozenset(CATEGORY_TOOLS)

# 분류 LLM 에 주는 작은 system 프롬프트 (도메인 지식 X — 비용 최소화가 목적).
_CLASSIFY_SYSTEM = (
    "너는 간호사 근무 스케줄링 에이전트의 **라우터**다. "
    "사용자 발화의 의도를 보고 아래 카테고리 중 해당하는 것을 1개 이상 고른다.\n"
    "판단 기준: 표현(보여줘/리스트업)이 아니라 **'화면으로 데려가면 되나(navigation)'** vs "
    "**'데이터를 조회/가공해 답해야 하나(read/analyze 등)'** vs **'무언가를 바꾸나(mutate/settings)'** 다.\n\n"
    "카테고리:\n"
    "- navigation: 특정 화면/섹션으로 이동하거나 띄우기만 하면 되는 의도. '어디서/어디로/띄워/가자/화면'.\n"
    "- read: 근무표·원티드·간호사정보·시프트·설정값 등을 조회/집계해서 보여줘야 하는 의도. "
    "단 '위반/위반사항/왜 안 짜였나'는 read 가 아니라 validate_repair.\n"
    "- mutate: **근무표 셀이나 원티드 항목**을 추가/변경/취소/승인/거부. "
    "⚠️ 간호사 '속성'(야간전담·팀 배정·직급·경력)을 바꾸는 건 mutate 가 아니라 settings_people.\n"
    "- generate: 근무표를 새로 자동 생성/재생성하는 의도.\n"
    "- validate_repair: 제약 위반 검증·'위반사항 뭐야'·왜 안 짜였는지·교정/재조정 제안.\n"
    "- analyze: 분포·공정성·통계·비교 분석 리포트.\n"
    "- recommend: 빈 자리/교체에 누가 가능한지 후보 추천.\n"
    "- settings_rules: **병동 전체** 정책(연속근무 한도, 월 근무 한도, 주말 정책 등). "
    "등급별·팀별 최소 인원은 여기 아님 → settings_people.\n"
    "- settings_people: 등급(grade)별 인원('시니어 최소 2명'), 팀별 최소 인원('A팀 나이트 최소 2명'), "
    "간호사 개인 속성(야간전담/팀 배정/직급/경력 등) 변경·조회.\n\n"
    "복합 의도면 여러 개 고른다. "
    "오직 JSON 배열만 출력한다. 예: [\"read\"] 또는 [\"mutate\",\"recommend\"]. "
    "확실치 않으면 빈 배열 []."
)


@dataclass
class RouterResult:
    """라우팅 결과."""

    categories: list[str] = field(default_factory=list)
    tool_names: list[str] = field(default_factory=list)
    fallback_used: bool = False
    # 분류 호출 토큰/모델 — 사용량 기록(usage.record_llm_usage)용.
    input_tokens: int = 0
    output_tokens: int = 0
    model: str | None = None

    def to_dict(self) -> dict:
        return {
            "categories": self.categories,
            "tool_count": len(self.tool_names),
            "tool_names": self.tool_names,
            "fallback_used": self.fallback_used,
        }


def resolve_tools(categories: list[str]) -> list[str]:
    """카테고리 list → tool 이름 subset (SKILL_TOOLS 순서 보존).

    알 수 없는 카테고리만 있거나 빈 list 면 **전체 tool**(fallback). 절대 빈 list 반환 안 함.
    """
    known = [c for c in categories if c in VALID_CATEGORIES]
    if not known:
        return list(ALL_TOOL_NAMES)
    wanted: set[str] = set()
    for c in known:
        wanted.update(CATEGORY_TOOLS[c])
    # SKILL_TOOLS 정의 순서대로 정렬 (프롬프트/스키마 순서 안정화)
    scoped = [name for name in ALL_TOOL_NAMES if name in wanted]
    return scoped or list(ALL_TOOL_NAMES)


def _classify_raw(llm: LLMClient, message: str):
    """분류 1회 — (categories, LLMResponse|None). 실패 시 ([], None)."""
    messages = [
        {"role": "system", "content": _CLASSIFY_SYSTEM},
        {"role": "user", "content": message},
    ]
    try:
        resp = llm.chat(messages, tools=[])
    except Exception as e:  # noqa: BLE001
        logger.warning("[router] classify LLM call failed: %s", e)
        return [], None
    if not resp.is_text or not resp.text:
        return [], resp
    cats = [c for c in _parse_categories(resp.text) if c in VALID_CATEGORIES]
    return cats, resp


def classify(llm: LLMClient, message: str) -> list[str]:
    """LLM 으로 발화를 카테고리 list 로 분류. 실패/비-text 응답이면 빈 list.

    빈 list 는 route() 에서 fallback(전체 tool) 을 유발한다.
    """
    return _classify_raw(llm, message)[0]


def route(llm: LLMClient, message: str) -> RouterResult:
    """발화 → RouterResult. 분류가 비면 fallback(전체 tool). 토큰/모델도 함께 반환."""
    cats, resp = _classify_raw(llm, message)
    it = resp.input_tokens if resp else 0
    ot = resp.output_tokens if resp else 0
    mdl = resp.model if resp else None
    if not cats:
        return RouterResult(
            categories=[], tool_names=list(ALL_TOOL_NAMES), fallback_used=True,
            input_tokens=it, output_tokens=ot, model=mdl,
        )
    return RouterResult(
        categories=cats, tool_names=resolve_tools(cats), fallback_used=False,
        input_tokens=it, output_tokens=ot, model=mdl,
    )


def _parse_categories(text: str) -> list[str]:
    """LLM text 에서 JSON 배열을 추출/파싱. 실패 시 빈 list."""
    s = text.strip()
    # ```json ... ``` 코드펜스 제거
    if s.startswith("```"):
        s = s.strip("`")
        if s.lower().startswith("json"):
            s = s[4:]
        s = s.strip()
    # 배열 구간만 슬라이스 (앞뒤 잡설 방어)
    lo, hi = s.find("["), s.rfind("]")
    if lo == -1 or hi == -1 or hi < lo:
        return []
    try:
        parsed = json.loads(s[lo : hi + 1])
    except (ValueError, TypeError):
        return []
    if not isinstance(parsed, list):
        return []
    return [str(x).strip() for x in parsed if isinstance(x, str)]
