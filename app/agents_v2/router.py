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
    # 순수 화면 이동/폼 프리필/병동 컨텍스트 전환 + 비파괴 UI 명령(엑셀 다운로드 등)
    "navigation": ["navigate", "prefill", "switch_ward", "invoke"],
    # 근무표/원티드/간호사/시프트/설정값 조회 (view-vs-derive gray → navigate 번들)
    # [LIVE_LLM_CARVE 2026-06-22] '생성 끝났어?' / '마감일 어때?' / '한도 넘은 사람' 같은
    # 조회 발화가 read 직격 시그널이라 read 카테고리에 mutation 스킬의 조회 op 도 번들.
    "read": [
        "query_schedule", "navigate", "invoke",
        "log_feedback",
        "query_generation_job", "manage_wanted_deadline", "manage_wanted_limits",
    ],
    # 근무/원티드 변경
    #   manage_wanted_deadline: 마감일 변경 / 즉시 마감
    #   manage_wanted_limits: 한도 초과자 조회 / 초과분 정리
    # [LIVE_LLM_CARVE 2026-06-22] '팀 추가/삭제' = mutate 본질이라 manage_teams 도 번들.
    "mutate": [
        "bulk_mutation", "manage_wanted_deadline", "manage_wanted_limits",
        "log_feedback",
        "manage_teams", "query_schedule", "publish_schedule",
        # [EVAL 2026-07-20] '김민지 5월 N 4번으로 맞춰줘' 같은 개인 월한도 변경이 mutate 로
        # 분류돼 update_monthly_limit(settings_rules 전용)을 놓쳤음 → 다중 배선으로 recall 복구.
        "update_monthly_limit",
    ],
    # 근무표 자동 생성 (생성 vs roster_create 화면 gray → navigate 번들)
    # query_generation_job: '생성 어디까지?' 같은 상태 조회 동반 가능.
    # resolve_infeasibility: 실패 후 '어떻게 풀어?' 해결 옵션 카탈로그.
    "generate": [
        "generate_schedule", "query_generation_job",
        "resolve_infeasibility", "navigate", "query_schedule", "invoke",
        # 근무표 확정/발행(생성과 인접한 lifecycle 연산).
        "publish_schedule",
    ],
    # 제약 위반 검증 / 교정 제안 (실패 후 해결 옵션 흐름도 인접 — resolve_infeasibility 번들)
    "validate_repair": [
        "validate_schedule", "repair_schedule",
        "log_feedback",
        "resolve_infeasibility", "query_schedule",
    ],
    # 분포·공정성·통계 분석
    "analyze": ["analyze_report", "query_schedule", "log_feedback"],
    # 대체/교체 간호사 추천
    "recommend": ["recommend_candidates", "query_schedule"],
    # 병동 규칙/제약/월 한도 정책 변경 (설정 화면 gray → navigate/prefill 번들)
    "settings_rules": [
        "update_constraint", "update_monthly_limit",
        "log_feedback",
        "query_schedule", "navigate", "prefill",
    ],
    # 등급/팀 최소인원/팀 CRUD/개인 속성 (근무자관리 화면 gray → navigate/prefill 번들)
    "settings_people": [
        "manage_grade", "manage_team_min", "manage_teams", "update_person_attr",
        "log_feedback",
        "manage_mutual_exclusion",
        "query_schedule",  # 속성 조회('야간전담이야?', '프리셉터 누구') → 읽기 tool 필요
        "navigate", "prefill",
        # [EVAL 2026-07-20] '박혜미 5월 D 최소 8회' 등 개인 월한도가 사람 발화라
        # settings_people 로 분류돼 update_monthly_limit 을 놓쳤음 → 다중 배선.
        "update_monthly_limit",
    ],
}


# ── 매니페스트 파생 병합 ────────────────────────────────────────
# @skill 로 선언된 신규 스킬의 categories 를 CATEGORY_TOOLS 에 자동 병합.
# (VALID_CATEGORIES 계산 전에 수행 — 매니페스트가 새 카테고리를 쓰더라도 포함되도록.)
def _merge_manifest_categories() -> None:
    from agents_v2.skills.manifest import (
        load_manifest_skills,
        manifest_category_tools,
    )

    load_manifest_skills()
    for cat, names in manifest_category_tools().items():
        bucket = CATEGORY_TOOLS.setdefault(cat, [])
        for n in names:
            if n not in bucket:
                bucket.append(n)


_merge_manifest_categories()

VALID_CATEGORIES: frozenset[str] = frozenset(CATEGORY_TOOLS)

# 분류 LLM 에 주는 작은 system 프롬프트 (도메인 지식 X — 비용 최소화가 목적).
_CLASSIFY_SYSTEM = (
    "너는 간호사 근무 스케줄링 에이전트의 **라우터**다. "
    "사용자 발화의 의도를 보고 아래 카테고리 중 해당하는 것을 1개 이상 고른다.\n"
    "판단 기준: 표현(보여줘/리스트업)이 아니라 **'화면으로 데려가면 되나(navigation)'** vs "
    "**'데이터를 조회/가공해 답해야 하나(read/analyze 등)'** vs **'무언가를 바꾸나(mutate/settings)'** 다.\n\n"
    "카테고리:\n"
    "- navigation: 특정 화면/섹션으로 이동하거나 띄우기만 하면 되는 의도. '어디서/어디로/띄워/가자/화면'. "
    "'명단에서 삭제/근무자 삭제/근무자 제외'도 근무자 관리 화면에서 처리하므로 여기(navigation).\n"
    "- help: 기능 **사용 방법·절차**를 묻는 온보딩 질문. '어떻게 하나요/~하는 법/방법/절차/어디서 "
    "하죠'(예: '퇴사자 삭제하는 법', '근무표 어떻게 만들어', '근무자 추가 방법'). "
    "화면 이동이 동반될 수 있어 navigation 과 함께 골라도 된다.\n"
    "- read: 근무표·원티드·간호사정보·시프트·설정값 등을 조회/집계해서 보여줘야 하는 의도. "
    "단 '위반/위반사항/왜 안 짜였나'는 read 가 아니라 validate_repair.\n"
    "- mutate: **근무표 셀이나 원티드 항목**을 추가/변경/취소/승인/거부. "
    "⚠️ 간호사 '속성'(야간전담·팀 배정·직급·경력·퇴사)을 바꾸는 건 mutate 가 아니라 settings_people. "
    "'명단 삭제/근무자 삭제'도 mutate 아님 → navigation.\n"
    "- generate: 근무표를 새로 자동 생성/재생성하는 의도.\n"
    "- validate_repair: 제약 위반 검증·'위반사항 뭐야'·왜 안 짜였는지·교정/재조정 제안.\n"
    "- analyze: 분포·공정성·통계·비교 분석 리포트.\n"
    "- recommend: 빈 자리/교체에 누가 가능한지 후보 추천.\n"
    "- settings_rules: **병동 전체** 근무 규칙/제약 변경·조회. 예: 연속근무 한도(최대 며칠), "
    "월 오프 수, 시프트 필요인원(데이/이브닝/나이트 몇 명), **야간 규칙(2연속/3연속 야간, "
    "야간 후 2오프)**, **이브닝 다음날 데이 금지**, 야간 균등, 미드 사용, 간호사별 월 시프트 한도(d_min/d_max). "
    "등급별·팀별 최소 인원은 여기 아님 → settings_people.\n"
    "- settings_people: 등급(grade)별 인원('시니어 최소 2명'), 팀별 최소 인원('A팀 나이트 최소 2명'), "
    "간호사 **개인 속성** 변경·조회. 개인 속성 예: 야간전담·팀 배정·병동 이동·직급·경력·"
    "**프리셉터/멘토 지정**·**고정근무(평일 데이/나이트 고정)**·**메모/비고**·주말휴무·주휴·"
    "**원티드 최대/한도**·**퇴사 처리(퇴사일)**. "
    "이런 속성을 '바꿔/지정/추가'뿐 아니라 '누구야/돼있어/야?'로 **조회**해도 여기.\n\n"
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
    # B14: 분류 신뢰도 — 1.0(단일 카테고리, 명확) / 0.7(2~3 복합) / 0.4(4+ 과다)
    # / 0.0(fallback). LLM 자체 confidence 가 아닌 분류 결과 shape 기반 휴리스틱.
    confidence: float = 0.0

    def to_dict(self) -> dict:
        return {
            "categories": self.categories,
            "tool_count": len(self.tool_names),
            "tool_names": self.tool_names,
            "fallback_used": self.fallback_used,
            "confidence": self.confidence,
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


# 실험 토글: 매니페스트 trigger_hint 를 분류 프롬프트에 주입할지. 기본 True(프로덕션).
# 라우팅 recall 전/후 측정 시 False 로 내려 baseline 을 잰다.
INJECT_MANIFEST_HINTS = True


def _build_classify_system() -> str:
    """분류 system 프롬프트 = 기본 + 매니페스트 신규 스킬 트리거 어휘(파생)."""
    if not INJECT_MANIFEST_HINTS:
        return _CLASSIFY_SYSTEM
    from agents_v2.skills.manifest import load_manifest_skills, manifest_router_hints

    load_manifest_skills()
    return _CLASSIFY_SYSTEM + manifest_router_hints()


def _classify_raw(llm: LLMClient, message: str):
    """분류 1회 — (categories, LLMResponse|None). 실패 시 ([], None)."""
    messages = [
        {"role": "system", "content": _build_classify_system()},
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


def _confidence_for(categories: list[str]) -> float:
    """카테고리 결과 shape → 신뢰도 휴리스틱.

    fallback(빈 list) = 0.0
    1개 카테고리 = 1.0 (명확)
    2~3개 = 0.7 (복합 의도)
    4개+ = 0.4 (LLM 과다 선택 — 노이즈 의심)
    """
    n = len(categories)
    if n == 0:
        return 0.0
    if n == 1:
        return 1.0
    if n <= 3:
        return 0.7
    return 0.4


def route(llm: LLMClient, message: str) -> RouterResult:
    """발화 → RouterResult. 분류가 비면 fallback(전체 tool). 토큰/모델도 함께 반환."""
    cats, resp = _classify_raw(llm, message)
    it = resp.input_tokens if resp else 0
    ot = resp.output_tokens if resp else 0
    mdl = resp.model if resp else None
    confidence = _confidence_for(cats)
    if not cats:
        result = RouterResult(
            categories=[], tool_names=list(ALL_TOOL_NAMES), fallback_used=True,
            input_tokens=it, output_tokens=ot, model=mdl, confidence=confidence,
        )
    else:
        result = RouterResult(
            categories=cats, tool_names=resolve_tools(cats), fallback_used=False,
            input_tokens=it, output_tokens=ot, model=mdl, confidence=confidence,
        )
    # B14: 한 줄 구조화 로그 — 디버깅/품질 모니터링용.
    logger.info(
        "[router] cats=%s conf=%.2f tools=%d fallback=%s tokens=%d/%d model=%s",
        result.categories, result.confidence, len(result.tool_names),
        result.fallback_used, it, ot, mdl,
    )
    return result


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
