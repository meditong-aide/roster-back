"""Router 품질(오프라인) — 라벨 질의셋의 '매핑 recall'.

여기서는 **gold 카테고리가 주어졌을 때** category→tools 매핑이 '필요 tool'을
포함하는지(=매핑 recall)를 검증한다. 실제 LLM 분류 정확도는 라이브(US-R4)에서 측정.
"""

from __future__ import annotations

import pytest

from agents_v2.router import resolve_tools

# (질의, gold 카테고리, 그 질의를 처리하려면 scoped set 에 반드시 있어야 하는 tool)
# 수간호사 관점 — navigate/read/mutate/generate/validate/analyze/recommend/settings 도메인 커버.
LABELED_QUERIES: list[tuple[str, list[str], str]] = [
    ("5월 근무표 보여줘", ["read"], "navigate"),            # 조건없는 전체보기 = 화면이동
    ("김민지 5월 근무 보여줘", ["read"], "query_schedule"),  # 특정 간호사 = 인라인 조회
    ("원티드 미제출자 누구야", ["read"], "query_schedule"),  # 집계
    ("팀 어디서 바꿔", ["navigation"], "navigate"),
    ("등급 설정 화면 띄워줘", ["navigation"], "navigate"),
    ("A팀 나이트 최소 2명으로", ["settings_people"], "manage_team_min"),
    ("나이트에 시니어 최소 2명", ["settings_people"], "manage_grade"),
    ("김민지 5월 10일 D를 N으로 바꿔줘", ["mutate"], "bulk_mutation"),
    ("5월 근무표 생성해줘", ["generate"], "generate_schedule"),
    ("4월 위반사항 뭐야", ["validate_repair"], "validate_schedule"),
    ("야간 분포 공정성 분석해줘", ["analyze"], "analyze_report"),
    ("5월 3일 데이 빈자리 대체자 추천", ["recommend"], "recommend_candidates"),
    ("야간 최대 7회로 바꿔줘", ["settings_rules"], "update_constraint"),
    ("김민지 야간전담으로 바꿔줘", ["settings_people"], "update_person_attr"),
]

# 복합 의도(multi-label) — 두 tool 모두 scoped set 에 있어야.
MULTILABEL_QUERIES: list[tuple[str, list[str], list[str]]] = [
    ("김민지 원티드 취소하고 대체자 추천해줘", ["mutate", "recommend"],
     ["bulk_mutation", "recommend_candidates"]),
]


@pytest.mark.parametrize("query,gold,needed", LABELED_QUERIES)
def test_mapping_recall(query, gold, needed):
    """gold 카테고리 → resolve_tools 가 필요 tool 을 포함(매핑 recall=100%)."""
    scoped = resolve_tools(gold)
    assert needed in scoped, f"[{query}] gold={gold} → {needed} 누락 (scoped={scoped})"


@pytest.mark.parametrize("query,gold,needed_list", MULTILABEL_QUERIES)
def test_multilabel_mapping_recall(query, gold, needed_list):
    scoped = set(resolve_tools(gold))
    missing = [t for t in needed_list if t not in scoped]
    assert not missing, f"[{query}] gold={gold} → {missing} 누락"


def test_labeled_set_covers_all_domains():
    """질의셋이 9개 카테고리를 모두 한 번 이상 라벨로 사용(커버리지)."""
    used = set()
    for _, gold, _ in LABELED_QUERIES:
        used.update(gold)
    for _, gold, _ in MULTILABEL_QUERIES:
        used.update(gold)
    from agents_v2.router import VALID_CATEGORIES
    missing = set(VALID_CATEGORIES) - used
    assert not missing, f"라벨셋이 안 건드린 카테고리: {missing}"
