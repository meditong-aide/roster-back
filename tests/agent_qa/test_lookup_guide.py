"""lookup_guide (help-doc RAG) — 검색기 + 스킬 + 배선 + grounding 무결성.

라이브 E2E(2026-07): '퇴사자 삭제/근무표 생성' → lookup_guide 호출 → grounded 답변(재직
드리프트 0), '급여 명세서' → found=false → abstain. 여기선 결정론 요소만 회귀 방지.
"""

from __future__ import annotations

from agents_v2.guide.retriever import retrieve
from agents_v2.guide.help_corpus import HELP_CHUNKS
from agents_v2.skills.lookup_guide import lookup_guide


def test_retrieve_relevant_chunk():
    r = retrieve("퇴사자 명단에서 삭제하는 법", k=2)
    assert r and r[0]["id"] == "member_delete"


def test_retrieve_alias_normalization():
    # '빼줘'(→삭제), '쌤'(→간호사) 같은 별칭이 검색에 반영
    r = retrieve("근무자 명단에서 빼줘", k=1)
    assert r and r[0]["id"] == "member_delete"


def test_retrieve_abstains_on_unsupported():
    # 급여/카톡 등 미지원 → 최고점 미달 → 빈 결과
    assert retrieve("급여 명세서 뽑는 법", k=2) == []
    assert retrieve("오늘 날씨 어때", k=2) == []


def test_lookup_guide_skill_found_and_notfound():
    ok = lookup_guide(None, {"query": "근무표 어떻게 만들어"})
    assert ok["found"] is True and ok["guides"]
    no = lookup_guide(None, {"query": "급여 명세서 출력"})
    assert no["found"] is False and no["guides"] == []


def test_help_corpus_has_no_fabricated_status():
    # grounding 무결성: 존재하지 않는 '재직 상태 변경' 표현이 코퍼스에 없어야
    for c in HELP_CHUNKS:
        assert "재직" not in c["body"], f"{c['id']} 에 '재직' 표현 있음"


def test_wiring_skill_and_category():
    from agents_v2.skills.descriptions import SKILL_TOOLS
    from agents_v2.router import CATEGORY_TOOLS, VALID_CATEGORIES
    assert "lookup_guide" in {t["name"] for t in SKILL_TOOLS}
    assert "help" in VALID_CATEGORIES
    assert "lookup_guide" in CATEGORY_TOOLS["help"]
    assert "lookup_guide" in CATEGORY_TOOLS["navigation"]
