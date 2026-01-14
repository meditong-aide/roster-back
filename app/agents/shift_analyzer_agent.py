import json
from pydantic import BaseModel
from typing import List, TypedDict, Annotated, Dict
from collections import defaultdict
from langgraph.graph import StateGraph, END
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_anthropic import ChatAnthropic
from langchain_openai import ChatOpenAI
import os
from langchain_core.messages import SystemMessage, HumanMessage
import dotenv
from datetime import datetime
import operator
try:
    import tiktoken
except Exception:
    tiktoken = None

dotenv.load_dotenv()


def collector(state):
    """information collector"""
    return state


def init_data(state):
    """initialize data"""
    return state


# ────────────────────────────────────────────────────────────────
# 토큰/비용 계산 유틸리티
# ────────────────────────────────────────────────────────────────
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
    elif "gpt-4o" in name or "gpt-4.1" in name:
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
    shift_result: Annotated[list, operator.add]
    model: object
    year: int
    month: int
    weekend_holiday: Dict[str, List[str]]


class shiftResponse(TypedDict):
    shift: str
    date: List[int]
    score: List[float]


class shiftAnalyzer(BaseModel):
    processor: str
    request_type: str | None
    request_type_reason: str
    request_importance: str | None
    request_importance_reason: str | None
    result: shiftResponse | None


class shiftAnalyzerPrompt:
    def __init__(self, context, year: int, month: int, weekend_holiday: Dict[str, List[str]] | None = None):
        self.system = f"""
# GOAL:
You are the "Preference & Avoidance Score Extractor" for the nurse scheduling system.
Input sentences (Korean/English/mixed natural language) → must be converted into structured JSON.

## 1. Request Type (Weight Modifier)
| Type    | Description                          | Modifier |
|---------|--------------------------------------|----------|
| off     | Forced OFF                           | × 2      |
| shift   | Specific Shift assignment            | × 1.9    |
| keep    | Recurring request (weekly, etc.)     | × 1.8    |
| pattern | Rules like "DD→N", "N followed by O" | × 1.7    |
| other   | Non-policy items                     | –        |

## 2. Request Importance (Base Weight)
| Score | Priority/Reason                              | Allowed Type             |
|-------|----------------------------------------------|--------------------------|
| 5     | Legal/Hospital mandatory                     | shift/keep/off           |
| 4     | Life/Health crisis                           | off/shift                |
| 3     | Social/Family duties                         | off/shift/pattern        |
| 2     | Important personal plans                     | off/shift/keep/pattern   |
| 1     | Preference/Convenience                       | keep/pattern             |
| 0     | Out of policy/unsupported                    | other                    |

Final weight = Importance Score × Type Modifier

## 3. Mandatory Mapping Rules
- "Day shift" → "D", "Evening" → "E", "Night" → "N", "Off" → "O"
- Weight range: 0 ~ 5 (decimals allowed)
- Negative expressions ("말고", "빼고", "제외") → do NOT infer alternatives

## 4. Output JSON Schema
{{
  "processor": "string",
  "request_type": "off|shift|keep|pattern|other|null",
  "request_type_reason": "string",
  "request_importance": 0-5 or null,
  "request_importance_reason": "string",
  "result": {{
    "shift": "D|E|N|O|...",
    "date": [1,2,3,...],
    "score": [1.0,2.5,...]
  }} or null
}}

이 지침을 엄격히 따르세요. 불필요한 설명은 출력하지 마세요.
"""

        weekends_json = json.dumps((weekend_holiday or {}).get("weekends", []), ensure_ascii=False)
        holidays_json = json.dumps((weekend_holiday or {}).get("holidays", []), ensure_ascii=False)
        self.human = f"""
# CONTEXT:
{year}년 {month}월 기준 근무 희망 요청입니다.
주말은 {weekends_json} 입니다.
공휴일은 {holidays_json} 입니다.

{context}
# OUTPUT:
"""


async def shift_analyzer(state):
    phase = state['phase']
    context = state['requests'][phase]
    year = state['year']
    month = state['month']
    weekend_holiday = state['weekend_holiday']

    shift_analyzer_prompt = shiftAnalyzerPrompt(context, year, month, weekend_holiday)
    print(f"\n[shift_analyzer] 요청 문장: {context}")
    print(f"[shift_analyzer] 프롬프트 human 부분:\n{shift_analyzer_prompt.human[:300]}...\n")

    models_to_try = [
        ChatOpenAI(model="gpt-4.1-mini-2025-04-14", openai_api_key=os.getenv("OPENAI_API_KEY")),
        ChatAnthropic(model="claude-3-7-sonnet-20250219", anthropic_api_key=os.getenv("ANTHROPIC_API_KEY")),
        ChatGoogleGenerativeAI(model="gemini-2.0-flash", google_api_key=os.getenv("GOOGLE_API_KEY"))
    ]

    sr = None
    used_model_name = ""
    for i, client in enumerate(models_to_try):
        try:
            print(f"Shift Analyzer: {i+1}차 모델 시도 중... {client.model_name}")
            llm = client.with_structured_output(shiftAnalyzer)
            response = await llm.ainvoke([
                SystemMessage(content=shift_analyzer_prompt.system),
                HumanMessage(content=shift_analyzer_prompt.human)
            ])
            sr = response
            print(f"Shift Analyzer 응답: {sr}")
            used_model_name = getattr(client, "model", "") or used_model_name
            break
        except Exception as e:
            print(f"Shift Analyzer: {i+1}차 모델 오류 - {e}")
            if i == len(models_to_try) - 1:
                from types import SimpleNamespace
                sr = SimpleNamespace()
                sr.result = {"shift": "O", "date": [], "score": []}

    # 토큰 계산 및 로깅
    model_name_for_calc = used_model_name or "gpt-4.1-mini"
    prompt_tokens = _count_messages_tokens([shift_analyzer_prompt.system, shift_analyzer_prompt.human], model_name_for_calc)
    completion_json = json.dumps(sr.result if sr and sr.result else {}, ensure_ascii=False)
    completion_tokens = _count_tokens(completion_json, model_name_for_calc)
    cost_info = _compute_cost(prompt_tokens, completion_tokens, model_name_for_calc)
    print(f"[Shift Analyzer] 토큰/비용: {cost_info['usage']} tokens / {cost_info['cost_krw']['total']}원")

    if sr and sr.result:
        sr.result['request'] = [context] * len(sr.result.get('date', []))
        return {"shift_result": [sr.result]}
    else:
        return {"shift_result": []}


from services.holiday_pack import tool_get_weekends, tool_get_holidays


async def create_shift_analyzer(parent_state):
    """
    Shift 분석기 생성 및 실행 - 최종 버전
    - case_results + query_shift → 병합 (중복 제거)
    - case_results만 → LLM 생략
    - query_shift만 → LLM 처리
    """
    case_results = parent_state.get('case_results')
    requests = parent_state.get('query_shift', [])
    year = parent_state['year']
    month = parent_state['month']

    # 주말/공휴일 정보 준비
    try:
        weekends = tool_get_weekends(year, month)
        holidays = tool_get_holidays(year, month)
        for d in weekends:
            if datetime.strptime(d, "%Y-%m-%d").weekday() == 6:
                if d not in holidays:
                    holidays.append(d)
        weekend_holiday = {"weekends": weekends, "holidays": holidays}
    except Exception as e:
        print(f"주말/공휴일 계산 오류: {e}")
        weekend_holiday = {"weekends": [], "holidays": []}

    print(f"[create_shift_analyzer] case_results 있음? {bool(case_results)} | requests 개수: {len(requests)}")

    # ────────────────────────────────────────────────────────────────
    # CASE 1: case_results + 새 요청 → 병합 + 중복 제거
    # ────────────────────────────────────────────────────────────────
    if case_results is not None and requests:
        print(f"[병합 모드] case {len(case_results)}건 + 새 요청 {len(requests)}건")

        # case_results → shift_result 형태로 변환
        shift_groups = defaultdict(lambda: {'date': [], 'score': [], 'request': []})
        for item in case_results:
            shift_type = item.get('shift')
            day = item.get('date')
            score = item.get('score', 1.0)
            req = item.get('request', '기존 데이터')
            shift_groups[shift_type]['date'].append(day)
            shift_groups[shift_type]['score'].append(score)
            shift_groups[shift_type]['request'].append(req)

        case_shift_result = [
            {'shift': k, 'date': v['date'], 'score': v['score'], 'request': v['request']}
            for k, v in shift_groups.items()
        ]

        # 새 요청 LLM 처리
        llm = ChatGoogleGenerativeAI(
            model="gemini-2.0-flash",
            temperature=0,
            google_api_key=os.getenv("GOOGLE_API_KEY"),
        )

        graph = StateGraph(ShiftSubgraph)
        graph.add_node("init_data", init_data)
        graph.add_node("collector", collector)
        graph.set_entry_point('init_data')

        for n in range(len(requests)):
            def create_shift_node(n):
                async def wrapped_shift(state):
                    state['phase'] = n
                    return await shift_analyzer(state)
                return wrapped_shift
            graph.add_node(f'shift_analyzer{n}', create_shift_node(n))
            graph.add_edge('init_data', f'shift_analyzer{n}')
            graph.add_edge(f'shift_analyzer{n}', "collector")

        graph.add_edge('collector', END)
        graph_app = graph.compile()

        try:
            result = await graph_app.ainvoke({
                "requests": requests,
                "model": llm,
                "year": year,
                "month": month,
                "weekend_holiday": weekend_holiday,
            })
            new_shift_results = result.get('shift_results', [{}])[0].get('shift_result', [])
        except Exception as e:
            print(f"[새 요청 처리 오류] {e}")
            new_shift_results = []

        # 병합 + 중복 제거 (새 요청 우선)
        combined = []
        seen = set()  # (shift, date)로 중복 체크
        for group in [new_shift_results, case_shift_result]:
            for item in group:
                shift = item['shift']
                for d, s, r in zip(item['date'], item['score'], item['request']):
                    key = (shift, d)
                    if key in seen:
                        continue
                    seen.add(key)
                    combined.append({
                        'shift': shift,
                        'date': [d],
                        'score': [s],
                        'request': [r]
                    })

        print(f"[병합 완료] 총 {len(combined)}건 (중복 제거 후)")
        return {"shift_results": [{'shift_result': combined}]}

    # ────────────────────────────────────────────────────────────────
    # CASE 2: case_results만 → LLM 생략
    # ────────────────────────────────────────────────────────────────
    elif case_results is not None:
        print(f"[case만] {len(case_results)}건 처리 - LLM 생략")

        shift_groups = defaultdict(lambda: {'date': [], 'score': [], 'request': []})
        for item in case_results:
            shift_type = item.get('shift')
            day = item.get('date')
            score = item.get('score', 1.0)
            req = item.get('request', '단순 희망')
            shift_groups[shift_type]['date'].append(day)
            shift_groups[shift_type]['score'].append(score)
            shift_groups[shift_type]['request'].append(req)

        shift_result_list = [
            {'shift': k, 'date': v['date'], 'score': v['score'], 'request': v['request']}
            for k, v in shift_groups.items()
        ]

        return {"shift_results": [{'shift_result': shift_result_list}]}

    # ────────────────────────────────────────────────────────────────
    # CASE 3: 새 요청만 → LLM 처리
    # ────────────────────────────────────────────────────────────────
    else:
        print(f"[새 요청만] {len(requests)}건 처리")

        if not requests:
            print("[빈 요청] → 빈 결과 반환")
            return {"shift_results": []}

        llm = ChatGoogleGenerativeAI(
            model="gemini-2.0-flash",
            temperature=0,
            google_api_key=os.getenv("GOOGLE_API_KEY"),
        )

        graph = StateGraph(ShiftSubgraph)
        graph.add_node("init_data", init_data)
        graph.add_node("collector", collector)
        graph.set_entry_point('init_data')

        for n in range(len(requests)):
            def create_shift_node(n):
                async def wrapped_shift(state):
                    state['phase'] = n
                    return await shift_analyzer(state)
                return wrapped_shift
            graph.add_node(f'shift_analyzer{n}', create_shift_node(n))
            graph.add_edge('init_data', f'shift_analyzer{n}')
            graph.add_edge(f'shift_analyzer{n}', "collector")

        graph.add_edge('collector', END)
        graph_app = graph.compile()

        try:
            result = await graph_app.ainvoke({
                "requests": requests,
                "model": llm,
                "year": year,
                "month": month,
                "weekend_holiday": weekend_holiday,
            })
            print(f"[새 요청 결과] {result}")
            return {"shift_results": [result]}
        except Exception as e:
            print(f"[그래프 실행 오류] {e}")
            return {"shift_results": []}