import operator
from langgraph.graph import StateGraph, END
from typing import TypedDict, Annotated, List, Dict, Any
from agents.query_analyzer_agent import query_analyzer
from agents.shift_analyzer_agent import create_shift_analyzer  # 기존 import
from agents.preference_analyzer_agent import create_preference_analyzer
from langgraph.prebuilt import create_react_agent
from langchain_mcp_adapters.client import MultiServerMCPClient
from sqlalchemy.orm import Session


def collector(state):
    """
    최종 정보 수집기: 모든 분석 결과를 통합하여 반환
    """
    return state


class ContextAnalyticsState(TypedDict):
    request: str | None
    schema: object
    query_shift: List[str]
    query_preference: List[str]
    query_chat: List[str]
    query_others: List[str]
    shift_results: Annotated[list, operator.add]
    preference_results: Annotated[list, operator.add]
    model: object
    year: int
    month: int
    case: List[Dict[str, Any]] | None
    case_results: List[Dict[str, Any]] | None
    db: Session
    group_id: str


def GraphGenerate():
    graph = StateGraph(ContextAnalyticsState)
    graph.add_node('query_analyzer', query_analyzer)

    # ★ 핵심 수정: async wrapper로 만들어 await 실행
    async def create_shift_analyzer_wrapper(state):
        year = state['year']
        month = state['month']
        db = state['db']
        group_id = state['group_id']
        
        # weekend_holiday는 state에 없으면 빈 dict (기존 코드에서 계산 안 됨)
        weekend_holiday = state.get('weekend_holiday', {})

        # create_shift_analyzer 호출 (인자 전달)
        analyzer_func = create_shift_analyzer(year, month, weekend_holiday, db, group_id)
        
        # analyzer_func는 async 함수를 반환하므로 await
        result = await analyzer_func(state)
        return result

    graph.add_node('create_shift_analyzer', create_shift_analyzer_wrapper)  # async wrapper 등록
    graph.add_node('create_preference_analyzer', create_preference_analyzer)
    graph.add_node('collector', collector)

    graph.set_entry_point('query_analyzer')
    graph.add_edge('query_analyzer', 'create_shift_analyzer')
    graph.add_edge('query_analyzer', 'create_preference_analyzer')

    graph.add_edge('create_shift_analyzer', "collector")
    graph.add_edge('create_preference_analyzer', 'collector')

    graph.add_edge('collector', END)

    app = graph.compile()
    return app