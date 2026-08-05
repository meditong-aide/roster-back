import operator
from langgraph.graph import StateGraph, END
from typing import TypedDict, Annotated, List, Dict, Any
from agents.query_analyzer_agent import query_analyzer
from agents.shift_analyzer_agent import create_shift_analyzer, create_avoid_analyzer
from agents.preference_analyzer_agent import create_preference_analyzer
from langgraph.prebuilt import create_react_agent
from langchain_mcp_adapters.client import MultiServerMCPClient
# from agents.collector_agent import collector

def collector(state):
    """
    최종 정보 수집기: 모든 분석 결과를 통합하여 반환
    """
    return {}


class ContextAnalyticsState(TypedDict):
    """
    근무 희망 분석 그래프의 상태 타입
    
    Attributes:
        request: 유저 입력 쿼리 (자연어)
        schema: 간호사 정보 스키마
        query_shift: shift 관련 쿼리 리스트
        query_except: 기피(부정/제외) 쿼리 리스트 — "10일은 E 빼줘"
        query_preference: preference 관련 쿼리 리스트
        query_chat: 잡담 관련 쿼리 리스트
        query_others: 기타 쿼리 리스트
        shift_results: shift 분석 결과 (누적)
        avoid_results: 기피 분석 결과 (누적). shift_results 와 같은 형태이고 의미만 다르다
        preference_results: preference 분석 결과 (누적)
        model: 사용할 LLM 모델
        case: 기존 DB에서 로드된 case 리스트 (예: [{'date': '2025-07-01', 'shift': 'D', 'reason': '...'}])
        case_results: case 처리 결과 (shift_analyzer에서 사용)
        year: 근무표 년도
        month: 근무표 월
    """
    request: str | None
    schema: object
    query_shift: List[str]
    query_shift_comments: List[str | None]  # query_analyzer에서 추출한 사유 리스트
    # query_analyzer 는 예전부터 Except 를 뽑아냈지만 이 상태에 선언이 없어 그래프
    # 경계에서 통째로 버려지고 있었다("10일은 E 피하고 싶습니다" 가 무시됨).
    query_except: List[str]
    query_preference: List[str]
    query_chat: List[str]
    query_others: List[str]
    shift_results: Annotated[list, operator.add]
    avoid_results: Annotated[list, operator.add]
    preference_results: Annotated[list, operator.add]
    model: object
    year: int
    month: int
    case: List[Dict[str, Any]] | None
    case_results: List[Dict[str, Any]] | None
    # 추가
    allowed_shift_map: Dict[str, str]

    

def GraphGenerate():
    """
    근무 희망 분석 그래프 생성
    
    Returns:
        CompiledGraph: 컴파일된 그래프 객체
    
    Notes:
        query_analyzer → shift/preference analyzer (병렬) → collector 순서로 실행
        case_results가 있으면 shift_analyzer에서 LLM 호출 없이 바로 반환
    """
    graph = StateGraph(ContextAnalyticsState)
    graph.add_node('query_analyzer', query_analyzer)
    graph.add_node('create_shift_analyzer', create_shift_analyzer)
    graph.add_node('create_avoid_analyzer', create_avoid_analyzer)
    graph.add_node('create_preference_analyzer', create_preference_analyzer)
    graph.add_node('collector', collector)

    graph.set_entry_point('query_analyzer')
    graph.add_edge('query_analyzer', 'create_shift_analyzer')
    graph.add_edge('query_analyzer', 'create_avoid_analyzer')
    graph.add_edge('query_analyzer', 'create_preference_analyzer')

    graph.add_edge('create_shift_analyzer', "collector")
    graph.add_edge('create_avoid_analyzer', 'collector')
    graph.add_edge('create_preference_analyzer', 'collector')

    graph.add_edge('collector', END)

    app = graph.compile()
    return app