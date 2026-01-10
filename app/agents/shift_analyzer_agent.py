import json
from pydantic import BaseModel
from typing import List, TypedDict, Annotated, operator, Dict
from google import genai
from google.genai import types
from langgraph.graph import StateGraph, END
from langchain_mcp_adapters.client import MultiServerMCPClient
from langgraph.prebuilt import create_react_agent
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_anthropic import ChatAnthropic
from langchain_openai import ChatOpenAI
import os
from langchain_core.messages import SystemMessage, HumanMessage
from collections import defaultdict
import dotenv
from datetime import datetime
try:
    import tiktoken
except Exception:
    tiktoken = None
            # * 답변 시 알 수 없는 정보를 요구한다면, 아래 도구 목록을 참고해, 필요하다면 한 번에 하나의 tool 을 호출할 수 있습니다. 
            # 도구 호출이 적절치 않을 경우 직접 답변만 작성하세요.  
            # {tools[0]}
            # * 답변 작성을 위한 processor는 도구가 필요함을 캐치하고, 어떤 도구와 어떤 인자가 필요할 지 정확히 명시해야 합니다.

dotenv.load_dotenv()


def collector(state):
    """
    information collector
    """
    return state


def init_data(state):
    """
    initialize data
    """
    return state


# ------------------------------
# 비용/토큰 유틸리티
# ------------------------------
def _krw_per_usd() -> float:
    try:
        return float(os.getenv("KRW_PER_USD", "1350"))
    except Exception:
        return 1350.0


def _pricing_per_1k(model_name: str) -> tuple[float, float]:
    name = (model_name or "").lower()
    input_usd, output_usd = 0.003, 0.009
    if "claude" in name:
        input_usd, output_usd = 0.003, 0.015
    elif "gpt-4o" in name:
        input_usd, output_usd = 0.005, 0.015
    elif "gemini" in name:
        input_usd, output_usd = 0.00035, 0.00105
    return input_usd, output_usd


def _encoding_for_model(model_name: str):
    name = (model_name or "").lower()
    try:
        if "gpt" in name or "o" in name:
            return tiktoken.get_encoding("o200k_base")
        return tiktoken.get_encoding("cl100k_base")
    except Exception:
        return None


def _count_tokens(text: str, model_name: str) -> int:
    if not text:
        return 0
    enc = _encoding_for_model(model_name)
    if enc is None:
        return max(1, int(len(text) / 4))
    try:
        return len(enc.encode(text))
    except Exception:
        return max(1, int(len(text) / 4))


def _count_messages_tokens(messages: List[str], model_name: str) -> int:
    return sum(_count_tokens(m or "", model_name) for m in messages)


def _compute_cost(prompt_tokens: int, completion_tokens: int, model_name: str) -> dict:
    in_per_1k, out_per_1k = _pricing_per_1k(model_name)
    input_usd = (prompt_tokens / 1000.0) * in_per_1k
    output_usd = (completion_tokens / 1000.0) * out_per_1k
    total_usd = input_usd + output_usd
    rate = _krw_per_usd()
    return {
        "model": model_name,
        "pricing_per_1k_usd": {"input": in_per_1k, "output": out_per_1k},
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
        },
        "cost_usd": {
            "input": round(input_usd, 6),
            "output": round(output_usd, 6),
            "total": round(total_usd, 6),
        },
        "cost_krw": {
            "input": int(round(input_usd * rate)),
            "output": int(round(output_usd * rate)),
            "total": int(round(total_usd * rate)),
            "krw_per_usd": rate,
        },
    }


class ShiftSubgraph(TypedDict):
    requests: List[str]
    n_requests: int
    phase: int
    # mcp_agent: object
    shift_result: Annotated[list, operator.add]
    model: object
    # mcp_tools: object
    year: int
    month: int
    weekend_holiday: Dict[str, List[str]]

class shiftResponse(TypedDict):
    shift: str 
    date: List[int] 
    score: List[float] 
    request: List[str] # 추가

class shiftAnalyzer(BaseModel):
    processor: str
    request_type: str | None 
    request_type_reason: str 
    request_importance: str | None 
    request_importance_reason: str | None 
    result: shiftResponse | None 


class shiftAnalyzerPrompt:
    def __init__(self, context: str, year: int, month: int, 
                 weekend_holiday: Dict[str, List[str]] | None = None, 
                 allowed_shifts: str = '',
                 allowed_shift_map: dict = None):  # 핵심 추가!
        """
        allowed_shift_map: {'생': '생리휴가(무급)', '경': '경조휴가', ...}
        """
        self.allowed_list = [s.strip() for s in allowed_shifts.split(",") if s.strip()] if allowed_shifts else []
        self.allowed_str = allowed_shifts or "없음"
        self.allowed_shift_map = allowed_shift_map or {}

        # 기본 근무 코드 (항상 고정)
        base_mappings = {
            "D": ["데이", "day", "D", "아침근무", "오전근무", "데이근무"],
            "E": ["이브닝", "evening", "E", "오후근무", "이브닝근무"],
            "N": ["나이트", "night", "N", "야간", "밤근무", "나이트근무"],
            "O": ["오프", "off", "O", "휴무", "쉬고", "쉬", "off"],
        }

        mapping_rules = []
        few_shot_examples = []

        # D/E/N/O 기본 매핑
        for code, synonyms in base_mappings.items():
            if code in self.allowed_list:
                examples = ", ".join(synonyms)
                mapping_rules.append(f'- "{examples}" → "{code}" 코드로 변환')
                few_shot_examples.append(f"""
                # CONTEXT:
                    "16일 {synonyms[0]}로 부탁드려요"
                # OUTPUT:
                    {{"processor": "기본 근무 '{code}' 요청", "request_type": "shift", "request_importance": 2, "result": {{"shift": "{code}", "date": [16], "score": [3.8], "request": ["16일 {synonyms[0]}로 부탁드려요"]}}}}
                """)

        # ===== 동적 특수 코드 처리 (하드코딩 완전 제거) =====
        special_codes = [code for code in self.allowed_list if code not in base_mappings]

        for code in special_codes:
            name = self.allowed_shift_map.get(code, code)
            display_name = name.split('(')[0].strip()  # "생리휴가(무급)" → "생리휴가"

            mapping_rules.append(
                f'- "{display_name}", "{name}", "{code}" 등이 언급되면 → 반드시 "{code}" 코드로 유지 (D/E/N/O로 변환 절대 금지!)'
            )

            # 중요도: 특수 휴가/교육은 높게 (4~5)
            importance_score = 4.0 if code in ["연", "경", "생", "병"] else 3.5
            type_multiplier = 1.9  # shift 타입
            final_score = importance_score * type_multiplier

            few_shot_examples.append(f"""
            # CONTEXT:
                "16일에 {display_name} 부탁드려요"
            # OUTPUT:
                {{"processor": "특수 코드 '{code}' ({name}) 요청 → 정확히 '{code}' 유지", 
                 "request_type": "shift", "request_importance": {importance_score}, 
                 "request_importance_reason": "법적/건강/가족 관련 휴가", 
                 "result": {{"shift": "{code}", "date": [16], "score": [{final_score:.1f}], "request": ["16일에 {display_name} 부탁드려요"]}}}}
            """)

            few_shot_examples.append(f"""
            # CONTEXT:
                "이번 달 10일 {name} 써도 될까요?"
            # OUTPUT:
                {{"processor": "특수 코드 '{code}' 감지 → '{code}'로 강제 매핑", 
                 "result": {{"shift": "{code}", "date": [10], "score": [{final_score:.1f}]}}}} 
            """)

        # 불허용 처리
        disallow_rule = """
        - 허용 목록에 없는 코드는 result에 포함 금지
        - 대신 processor에 "희망근무 선택 불가" 설명 추가
        """

        dynamic_rules = "\n".join(mapping_rules)
        dynamic_few_shots = "\n".join(few_shot_examples[:8])  # 토큰 절약

        weekends_json = json.dumps((weekend_holiday or {}).get("weekends", []), ensure_ascii=False)
        holidays_json = json.dumps((weekend_holiday or {}).get("holidays", []), ensure_ascii=False)

        self.system = f"""
        ## GOAL:
        You are the "Shift Preference Score Extractor".
        자연어 요청 → structured JSON으로 변환 (shift, date, score, request)

        ## 이번 달 허용 근무코드 (최우선 준수!)
        - 허용 코드: {self.allowed_str}
        - result.shift에는 이 코드만 사용 가능
        - 특수 코드(생, 경, 연, 교후 등)는 절대 D/E/N/O로 변환하지 마세요!

        ## 변환 규칙 (현재 그룹 정책 기반)
        {dynamic_rules}
        {disallow_rule}

        ## 중요도 × 타입 가중치 계산
        - 연차/경조/생리/병가: 중요도 4~5 → 최종 점수 7.6~9.5
        - 교육: 중요도 3.5
        - 일반 근무: 중요도 2

        ## 동적 Few-Shot 예시
        {dynamic_few_shots}

        ## 출력 주의
        - request 필드에 원문 요청 저장
        - score는 소수점 1자리까지
        """

        self.human = f"""
        # CONTEXT: 
            {year}년 {month}월 근무 희망 요청
            주말: {weekends_json}
            공휴일: {holidays_json}

            {context}
        # OUTPUT:
        """


async def shift_analyzer(state):
    phase = state['phase']
    context = state['requests'][phase]
    year = state['year']
    month = state['month']
    weekend_holiday = state['weekend_holiday']
    allowed_shifts = state.get('allowed_shifts', 'O, E, N, D')
    allowed_shift_map = state.get('allowed_shift_map', {})  # 핵심 추가!

    shift_analyzer_prompt = shiftAnalyzerPrompt(
        context, year, month, weekend_holiday, allowed_shifts, allowed_shift_map
    )

    models_to_try = [
        ChatOpenAI(model="gpt-4.1-mini-2025-04-14", openai_api_key=os.getenv("OPENAI_API_KEY")),
        ChatAnthropic(model="claude-3-7-sonnet-20250219", anthropic_api_key=os.getenv("ANTHROPIC_API_KEY")),
        ChatGoogleGenerativeAI(model="gemini-2.0-flash", google_api_key=os.getenv("GOOGLE_API_KEY"))
    ]

    sr = None
    used_model_name = ""
    for i, client in enumerate(models_to_try):
        try:
            print(f"Shift Analyzer: {i+1}차 시도 - {client}")
            llm = client.with_structured_output(shiftAnalyzer)
            response = await llm.ainvoke([
                SystemMessage(content=shift_analyzer_prompt.system),
                HumanMessage(content=shift_analyzer_prompt.human)
            ])
            sr = response
            used_model_name = getattr(client, "model", "")
            print(f"Shift Analyzer 성공: {sr}")
            break
        except Exception as e:
            print(f"Shift Analyzer 오류 ({i+1}차): {e}")
            if i == len(models_to_try) - 1:
                sr = shiftAnalyzer(processor="모델 실패", request_type=None, request_importance=None, result=None)

    # 비용 계산
    model_name_for_calc = used_model_name or "unknown"
    prompt_tokens = _count_messages_tokens([shift_analyzer_prompt.system, shift_analyzer_prompt.human], model_name_for_calc)
    completion_tokens = _count_tokens(json.dumps(sr.dict() if sr else {}), model_name_for_calc)
    cost_info = _compute_cost(prompt_tokens, completion_tokens, model_name_for_calc)
    print(f"Shift Analyzer 비용: {cost_info}")

    if sr.result:
        sr.result['request'] = [context] * len(sr.result.get('date', []))
    else:
        sr.result = {"shift": "", "date": [], "score": [], "request": []}

    return {"shift_result": [sr.result] if sr.result['date'] else []}


from services.holiday_pack import tool_get_weekends, tool_get_holidays
from services.holiday_pack import get_weekends as _get_weekends, get_korean_public_holidays as _get_holidays, serialise as _serialise
from langchain_core.tools import tool
from agents.query_analyzer_agent import query_analyzer


async def create_shift_analyzer(parent_state):
    """
    Shift 분석기 생성 및 실행
    
    Args:
        parent_state: 부모 그래프의 상태 (query_analyzer 결과 등 포함)
        
    Returns:
        Dict: {"shift_results": [...] } 형태
    """
    # ======================================================================
    # case_results가 있으면 LLM 호출 없이 바로 반환 (기존 선택 유지)
    # ======================================================================
    case_results = parent_state.get('case_results')
    if case_results is not None:
        print("Shift Analyzer: case_results 감지 - LLM 호출 생략, 기존 데이터 직접 변환")
        print(f"case_results: {case_results}")

        shift_groups = defaultdict(lambda: {'date': [], 'score': [], 'request': []})
        
        for item in case_results:
            shift_type = item.get('shift')
            if not shift_type:
                continue
            date_val = item.get('date')
            score_val = item.get('score', 1.0)
            request_val = item.get('request', '단순 희망')
            
            shift_groups[shift_type]['date'].append(date_val)
            shift_groups[shift_type]['score'].append(score_val)
            shift_groups[shift_type]['request'].append(request_val)
        
        shift_result_list = [
            {
                'shift': shift_type,
                'date': data['date'],
                'score': data['score'],
                'request': data['request']
            }
            for shift_type, data in shift_groups.items()
            if data['date']  # 빈 경우 제외
        ]
        
        return {"shift_results": [{'shift_result': shift_result_list}] if shift_result_list else []}

    # ======================================================================
    # 일반 경로: LLM을 사용한 shift 분석
    # ======================================================================
    requests = parent_state.get('query_shift', [])
    if not requests:
        print("Shift Analyzer: query_shift 없음 → 빈 결과 반환")
        return {"shift_results": []}

    year = parent_state['year']
    month = parent_state['month']

    # 주말/공휴일 정보 계산
    try:
        weekends = tool_get_weekends(year, month)
        holidays = tool_get_holidays(year, month)
        # 일요일은 공휴일에 추가 (중복 방지)
        for d in weekends:
            if datetime.strptime(d, "%Y-%m-%d").weekday() == 6:  # 일요일
                if d not in holidays:
                    holidays.append(d)
        weekend_holiday = {"weekends": weekends, "holidays": holidays}
    except Exception as e:
        print(f"주말/공휴일 계산 오류: {e}")
        weekend_holiday = {"weekends": [], "holidays": []}

    print(f"Shift Analyzer: 처리할 요청 수 = {len(requests)}")
    print(f"weekend_holiday: {weekend_holiday}")

    # allowed_shift_map 가져오기 (wanted_service.py에서 전달되어야 함)
    allowed_shift_map = parent_state.get('allowed_shift_map', {})
    allowed_shifts_str = parent_state.get('allowed_shifts', 'O, E, N, D')

    # 그래프 정의
    graph = StateGraph(ShiftSubgraph)
    graph.add_node("init_data", init_data)
    graph.add_node("collector", collector)
    graph.set_entry_point('init_data')

    n_requests = len(requests)
    for n in range(n_requests):
        def create_shift_node(n):
            async def wrapped_shift(state):
                state['phase'] = n
                return await shift_analyzer(state)
            return wrapped_shift
        
        node_name = f'shift_analyzer_{n}'
        graph.add_node(node_name, create_shift_node(n))
        graph.add_edge('init_data', node_name)
        graph.add_edge(node_name, "collector")

    graph.add_edge('collector', END)
    graph_app = graph.compile()

    # 그래프 실행
    try:
        result = await graph_app.ainvoke({
            "requests": requests,
            "year": year,
            "month": month,
            "weekend_holiday": weekend_holiday,
            "allowed_shifts": allowed_shifts_str,
            "allowed_shift_map": allowed_shift_map,  # 핵심: 동적 Few-shot에 사용됨
            "phase": 0  # 초기값
        })
        print(f"Shift Analyzer 그래프 실행 완료: {result}")
    except Exception as e:
        print(f"Shift Analyzer 그래프 실행 중 오류 발생: {e}")
        import traceback
        traceback.print_exc()
        result = {"shift_result": []}

    # 최종 반환 형식 통일
    # result는 collector에서 누적된 {"shift_result": [...]}
    shift_results = result.get("shift_result", [])
    # if not shift_results:
    #     print("Shift Analyzer: 최종 shift_result 없음 → 빈 배열 반환")
    #     shift_results = []
    unique_results = []
    seen = set()
    for item in shift_results:
        key = (item['shift'], tuple(item['date']), tuple(item['score']))
        if key not in seen:
            seen.add(key)
            unique_results.append(item)

    return {"shift_results": [{'shift_result': shift_results}]}
