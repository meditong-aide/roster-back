"""B12: CLAUDE.md 안티패턴 회귀 차단 sentinel.

CLAUDE.md (## Anti-Patterns) — agent v2 도메인에선:
  1) regex / pattern dict / lookup table 로 intent 파싱 금지
  2) bottom-up fragment grounding 금지
  3) _WORKFLOW_PATTERNS / _STATUS_PATTERNS 같은 fixed enum lookup 금지
  4) UNRESOLVED fragment 로 planner 차단 금지
  5) 병원/병동 별 데이터를 system prompt 에 박지 않음 (thin memory)

본 sentinel 은 위 패턴이 재등장하지 않도록 정적 검사. 허용 예외(`_MUTATION_SKILLS`
같은 wiring 맵, `_CONFIRM_WORDS` 같은 boundary classifier)는 화이트리스트로 명시.

검사 대상 디렉토리: app/agents_v2/
"""

from __future__ import annotations

import re
from pathlib import Path

# ── 검사 범위 ────────────────────────────────────────────────


_AGENTS_V2 = Path(__file__).resolve().parents[2] / "app" / "agents_v2"


def _python_files() -> list[Path]:
    return [p for p in _AGENTS_V2.rglob("*.py") if "__pycache__" not in p.parts]


# ── 허용된 wiring/boundary 맵 (intent 파싱 아님) ──────────────


_ALLOWED_LOOKUP_NAMES = frozenset({
    # wiring (intent → tool subset)
    "CATEGORY_TOOLS",
    "NAVIGATE_TARGETS",
    "_MUTATION_SKILLS",
    "MODEL_PRICING",
    "ALL_TOOL_NAMES",
    "SKILL_REGISTRY",
    "SKILL_TOOLS",
    "VALID_CATEGORIES",
    "_BY_COLUMN",
    "TARGET_ROUTES",
    "CONFIG_SECTION_TABS",
    # boundary classifier (security/approval)
    "_CONFIRM_WORDS",
    "_DENY_WORDS",
    "_VALID_SHIFT_CODES",
    "_KOREAN_PHRASE_ALIASES",
    "_LATIN_TOKEN_ALIASES",
    "_CLEAR_TOKENS",
    "_FIXED_SHIFT_OFF_ALIASES",
    "_SHIFT_CLARIFY_OPTIONS",
    "_CATEGORY_LABEL",
    "_GRADE_NAMES",
    "_SYSTEM_SENTINELS",  # B2 output check
    # internal constants
    "_PER_TOKEN",
    "_MAX_INJECT_CHARS",
    "_MAX_INJECTED_FACTS",
    "_CONFIRM_MAX_LEN",
    "MAX_INPUT_LENGTH",
    "_PREVIEW_DIRECTIVE",
    "_APPLY_NOTE",
})


# 안티패턴 이름 패턴 — 재등장 차단
_FORBIDDEN_NAME_RE = re.compile(
    r"\b(_?WORKFLOW_PATTERNS|_?STATUS_PATTERNS|_?INTENT_PATTERNS|"
    r"_?WORKFLOW_KEYWORDS|_?STATUS_KEYWORDS|_?INTENT_KEYWORDS|"
    r"PATTERN_DICT|LOOKUP_TABLE|"
    r"_?HOSPITAL_DATA|_?GROUP_FACTS_DICT|_?WARD_DICT)\b"
)

# 병원 식별자 박힘 차단 (thin memory)
_HOSPITAL_NAME_RE = re.compile(r"시화병원|9B 병동")


# ── 검사 ────────────────────────────────────────────────────


def test_no_forbidden_intent_patterns_in_agents_v2():
    """_WORKFLOW_PATTERNS / _STATUS_PATTERNS 등 intent lookup 재등장 금지."""
    offenders: list[tuple[Path, int, str]] = []
    for path in _python_files():
        # security 모듈은 정의상 regex/패턴 dict 가 정당 — 제외
        if "security" in path.parts:
            continue
        # 본 sentinel 파일 자체 제외
        if path.name == Path(__file__).name:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        for i, line in enumerate(text.splitlines(), 1):
            m = _FORBIDDEN_NAME_RE.search(line)
            if m:
                offenders.append((path, i, line.strip()))
    assert not offenders, (
        "CLAUDE.md 안티패턴 식별자 재등장 — 제거 또는 별도 허용 리뷰 필요:\n"
        + "\n".join(f"  {p}:{ln}  {src}" for p, ln, src in offenders)
    )


def test_no_hospital_specific_data_in_prompts():
    """system prompt 후보(descriptions, agent_v3, middleware)에 병원 식별자 박힘 금지.

    test corpus / 테스트 파일은 제외 — 시드 데이터에선 등장 가능.
    """
    offenders: list[tuple[Path, int]] = []
    targets = [
        _AGENTS_V2 / "skills" / "descriptions.py",
        _AGENTS_V2 / "agent_v3.py",
        _AGENTS_V2 / "middleware.py",
        _AGENTS_V2 / "harness" / "prompt_builder.py",
    ]
    for path in targets:
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8")
        for i, line in enumerate(text.splitlines(), 1):
            if _HOSPITAL_NAME_RE.search(line):
                offenders.append((path, i))
    assert not offenders, (
        "병원 식별자가 prompt 후보 파일에 박혀있음 — thin memory 원칙 위반:\n"
        + "\n".join(f"  {p}:{ln}" for p, ln in offenders)
    )


def test_descriptions_has_no_lookup_anti_pattern():
    """descriptions.py 는 자연어 description 만. regex/pattern/lookup 키워드 등장 금지."""
    path = _AGENTS_V2 / "skills" / "descriptions.py"
    text = path.read_text(encoding="utf-8")
    forbidden_kw_re = re.compile(r"\b(re\.compile|re\.match|re\.search|regex)\b")
    offenders = [
        (i, line.strip())
        for i, line in enumerate(text.splitlines(), 1)
        if forbidden_kw_re.search(line)
    ]
    assert not offenders, (
        "descriptions.py 에 regex 호출 등장 — LLM-first 원칙 위반:\n"
        + "\n".join(f"  {ln}  {src}" for ln, src in offenders)
    )


def test_allowed_lookup_names_still_used():
    """화이트리스트가 stale 하지 않은지 — 적어도 핵심 wiring 맵은 살아있어야 함."""
    text = (_AGENTS_V2 / "router.py").read_text(encoding="utf-8")
    assert "CATEGORY_TOOLS" in text
    text = (_AGENTS_V2 / "skills" / "client_actions.py").read_text(encoding="utf-8")
    assert "NAVIGATE_TARGETS" in text
