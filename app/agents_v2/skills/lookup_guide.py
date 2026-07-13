"""lookup-guide skill — 사용 방법/절차(how-to) 질문에 도움말을 검색해 grounding 제공.

절차 안내를 tool description 패러프레이즈로 지어내지 않고(드리프트 방지), 별도 도움말
코퍼스에서 BM25 로 관련 청크를 retrieve 해 그 원문으로만 답하게 한다. 검색 결과가 비면
'해당 기능 정보 없음'으로 정직하게 abstain. 읽기 전용(무권한).
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from agents_v2.skills.manifest import skill

LOOKUP_GUIDE_SCHEMA: dict = {
    "name": "lookup_guide",
    "description": (
        "사용 방법·절차를 묻는 '어떻게 하나요' 류 온보딩 질문에 사용한다. 도움말에서 관련 "
        "내용을 검색해 돌려준다. ⚠️ 반환된 guides 의 content 에 **근거해서만** 답하고, "
        "거기 없는 화면·버튼·상태(예: '재직 상태 변경')를 지어내지 마라. found=false 이면 "
        "'해당 기능에 대한 안내 정보가 없습니다'라고 정직하게 답하라."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "사용자의 방법/절차 질문(원문 그대로 전달).",
            },
        },
        "required": ["query"],
    },
}


@skill(
    "lookup_guide",
    LOOKUP_GUIDE_SCHEMA,
    # help(주): 절차/방법 질문. navigation: how-to 가 화면이동으로 이어지는 경우 함께 스코프.
    categories=["help", "navigation"],
    mutation=False,
    hn_only=False,
    grounds=[],
    trigger_hint=(
        "사용 방법·절차 안내('어떻게 하나요', '~하는 법', '어디서 하죠', 방법/절차), "
        "퇴사자 삭제/근무자 추가/근무표 생성 등 how-to 온보딩 질문"
    ),
    postcondition=lambda d: isinstance(d, dict),
)
def lookup_guide(db: Session, params: dict) -> Any:
    from agents_v2.guide.retriever import retrieve

    query = params.get("query") or ""
    guides = retrieve(query, k=3)
    if not guides:
        return {
            "found": False,
            "guides": [],
            "note": "해당 절차에 대한 도움말이 없습니다. 지어내지 말고 못 한다고 답하세요.",
        }
    return {
        "found": True,
        "guides": [{"title": g["title"], "content": g["body"]} for g in guides],
    }
