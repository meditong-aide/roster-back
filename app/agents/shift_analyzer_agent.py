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
from datetime import datetime, date, timedelta
import operator
from services.holiday_pack import tool_get_weekends, tool_get_holidays
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
    # 추가
    allowed_shift_map: Dict[str, str]


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
    def __init__(self, context, year: int, month: int, weekend_holiday: Dict = None,
                 allowed_shift_map: Dict[str, str] = None):
        allowed_shift_map = allowed_shift_map or {}

        # 1. 코드 → 이름 역매핑 (코드가 나오면 바로 이름 매핑)
        code_to_name = {code: name for code, name in allowed_shift_map.items()}

        # 2. 동적 매핑 테이블 + 키워드
        table = "## 지원 Shift 코드 매핑 (이 코드/이름/키워드가 나오면 무조건 해당 코드로)\n"
        table += "| 코드 | 이름 | 추가 키워드 (포함되면 이 코드로) |\n"
        table += "|------|------|-------------------------------|\n"
        for code, name in allowed_shift_map.items():
            keywords = f'"{code}", "{name}", "{name}로", "{name} 신청", "{name} 줘"'
            table += f"| {code} | {name} | {keywords} |\n"

        # 3. 강력한 규칙
        rules = "## 절대 지켜야 할 규칙\n"
        rules += "- 위 테이블의 코드나 이름/키워드가 1개라도 나오면 **무조건 해당 shift 코드**로 출력하세요.\n"
        rules += "- '로 줘', '신청해', '하고 싶어' 같은 표현 뒤에 코드/이름이 붙으면 100% shift로 인식\n"
        rules += "- '생로 줘' → '생', '법로 줘' → '법' 등 코드 직접 나오면 그대로 사용\n"
        rules += "- '휴가', '쉬고', 'Off'가 있어도 shift 이름/코드가 우선 (절대 'O'로 바꾸지 마세요)\n"
        rules += "- importance: '법정', '필수', '보수', '생리' 등 포함 시 5점 고정\n"
        rules += "- 날짜는 입력 그대로 사용. {month}월 범위 내이면 무조건 result에 넣으세요.\n"
        rules += "- result=None 절대 금지. shift가 인식되면 무조건 result 출력.\n"
        
        # 패턴 변환 지시 추가
        rules += """
        - '매주', '매월', '격주', '주말', '평일' 같은 반복 패턴이 나오면:
            - date: [] 로 두지 말고, **현재 {month}월의 해당 요일/조건에 맞는 모든 날짜 리스트**를 채워서 반환하세요.
            - 예: "매주 수요일은 D" → date: [수요일인 날짜들, 예: 5, 12, 19, 26]
            - "주말은 OFF" → date: [주말 날짜들]
            - "평일 E" → date: [평일 날짜들]
        - date 리스트는 반드시 1~31 사이의 정수 리스트로, 빈 리스트 [] 금지 (패턴이면 월 전체 적용)
        - 날짜 계산 시 윤년/월 말일 고려, 정확히 계산하세요.
        """

        # 4. 동적 Few-shot (코드 직접 언급 케이스 강조)
        few_shots = "## Few-shot Examples\n"
        for code, name in allowed_shift_map.items():
            few_shots += f'- "10일 {code}로 줘" → {{"shift": "{code}", "date": [10], "score": [5.0]}}\n'
            few_shots += f'- "{name} 15일 신청" → {{"shift": "{code}", "date": [15], "score": [4.0]}}\n'
            few_shots += f'- "매주 {code}" → {{"shift": "{code}", "date": [월의 해당 요일 날짜들], "score": [5.0] * n}}\n'

        self.system = f"""
# ROLE
You are a strict, no-hallucination shift preference parser for nurses.
사용자 입력을 위 규칙에 따라 **정확히** 구조화하세요.

{table}

{rules}

{few_shots}

## OUTPUT
- result.shift는 테이블의 코드 중 **정확히 하나**만 사용
- score: 0~10 (의무/법정 관련 5 이상)
- result=None 금지 (shift 인식되면 무조건 출력)

## SCHEMA
{{
  "processor": str,
  "request_type": str|null,
  "request_type_reason": str,
  "request_importance": int|null,
  "request_importance_reason": str,
  "result": {{"shift": str, "date": list[int], "score": list[float]}} | null
}}
"""

        weekends_json = json.dumps((weekend_holiday or {}).get("weekends", []), ensure_ascii=False)
        holidays_json = json.dumps((weekend_holiday or {}).get("holidays", []), ensure_ascii=False)
        self.human = f"""
# CONTEXT ({year}년 {month}월)
주말: {weekends_json}
공휴일: {holidays_json}
사용자 요청: {context}

정확히 파싱하세요. shift 인식되면 result에 반드시 넣으세요.
"""


async def shift_analyzer(state):
    """
    shift_analyzer: 단일 요청 문장을 분석하여 shift_result를 생성
    - LLM 호출 후, 결과가 예상과 다를 경우 allowed_shift_map 기반으로 보정
    """
    requests = state['requests']
    phase = state['phase']
    request_text = requests[phase]  # 현재 처리 중인 한 문장

    print(f"[shift_analyzer] 요청 문장: {request_text}")

    # allowed_shift_map 가져오기
    allowed_shift_map = state.get('allowed_shift_map', {})
    if not allowed_shift_map:
        print("[CRITICAL] shift_analyzer: allowed_shift_map이 비어있음! 프롬프트에 shift 정보 없음 - 기본값 사용")

    print(f"[shift_analyzer] allowed_shift_map: {allowed_shift_map}")

    # weekend_holiday 정보 (날짜 검증용)
    weekend_holiday = state.get('weekend_holiday', {"weekends": [], "holidays": []})

    # 프롬프트 생성 (기존 방식 그대로 사용)
    prompt = shiftAnalyzerPrompt(
        context=request_text,
        year=state['year'],
        month=state['month'],
        weekend_holiday=weekend_holiday,
        allowed_shift_map=allowed_shift_map
    )

    # LLM 모델 설정 (기존과 동일하게 백업 체계 유지)
    models_to_try = [
        ChatOpenAI(model="gpt-4.1-mini-2025-04-14", openai_api_key=os.getenv("OPENAI_API_KEY")),
        ChatAnthropic(model="claude-3-7-sonnet-20250219", anthropic_api_key=os.getenv("ANTHROPIC_API_KEY")),
        ChatGoogleGenerativeAI(model="gemini-2.0-flash", google_api_key=os.getenv("GOOGLE_API_KEY"))
    ]

    messages = [
        SystemMessage(content=prompt.system),
        HumanMessage(content=prompt.human)
    ]

    json_answer = {
        "processor": "기본값 설정",
        "request_type": None,
        "request_type_reason": "처리 실패",
        "request_importance": None,
        "request_importance_reason": "처리 실패",
        "result": None
    }
    used_model_name = ""

    for i, client in enumerate(models_to_try):
        try:
            print(f"Shift Analyzer: {i+1}차 모델 시도 중... {getattr(client, 'model', 'unknown')}")
            llm = client.with_structured_output(shiftAnalyzer)

            response = await llm.ainvoke(messages)
            used_model_name = getattr(client, "model", "") or used_model_name

            # ★★★ LLM 파싱 오류 방지: response가 dict인지 체크 ★★★
            if isinstance(response, dict):
                json_answer = response
            elif hasattr(response, 'dict'):
                json_answer = response.dict()
            else:
                raise ValueError("LLM 응답 형식 오류: dict 또는 Pydantic 객체 아님")

            print(f"Shift Analyzer 응답: {json_answer}")

            # ★★★ 패턴 후처리: date가 []이면 서버에서 요일 계산 ★★★
            if json_answer["result"] and json_answer["result"].get("date") == []:
                request_text_lower = request_text.lower()
                year = state['year']
                month = state['month']

                try:
                    first_day = date(year, month, 1)
                    next_month = month + 1 if month < 12 else 1
                    next_year = year if month < 12 else year + 1
                    last_day = (date(next_year, next_month, 1) - timedelta(days=1)).day

                    weekday_map = {
                        '월요일': 0, '월': 0,
                        '화요일': 1, '화': 1,
                        '수요일': 2, '수': 2,
                        '목요일': 3, '목': 3,
                        '금요일': 4, '금': 4,
                        '토요일': 5, '토': 5,
                        '일요일': 6, '일': 6,
                        '주말': [5, 6],
                        '평일': [0, 1, 2, 3, 4]
                    }

                    target_weekdays = None
                    for kw, wd in weekday_map.items():
                        if kw in request_text_lower:
                            target_weekdays = wd if isinstance(wd, list) else [wd]
                            print(f"[POST-FIX] 패턴 감지: '{kw}' → weekday {target_weekdays}")
                            break

                    if target_weekdays is not None:
                        filled_dates = []
                        current = first_day
                        while current.month == month:
                            if current.weekday() in target_weekdays:
                                filled_dates.append(current.day)
                            current += timedelta(days=1)

                        if filled_dates:
                            json_answer["result"]["date"] = filled_dates
                            json_answer["result"]["score"] = [5.0] * len(filled_dates)
                            print(f"[POST-FIX] date [] → {filled_dates} (패턴 '{request_text}' 처리)")
                except Exception as e:
                    print(f"[POST-FIX] 날짜 계산 오류: {e}")

            break

        except Exception as e:
            error_msg = str(e).lower()
            print(f"Shift Analyzer: {i+1}차 모델 오류 - {e}")
            if "429" in error_msg or "rate" in error_msg or "quota" in error_msg:
                if i < len(models_to_try) - 1:
                    continue
            else:
                if i < len(models_to_try) - 1:
                    continue
                else:
                    print("Shift Analyzer: 모든 모델 실패, 기본값 사용")
                    break

    # 토큰/비용 계산 (기존 그대로)
    model_name_for_calc = used_model_name or (getattr(models_to_try[0], "model", "") or "")
    prompt_tokens = _count_messages_tokens([prompt.system, prompt.human], model_name_for_calc)
    completion_json = json.dumps(json_answer, ensure_ascii=False)
    completion_tokens = _count_tokens(completion_json, model_name_for_calc)
    cost_info = _compute_cost(prompt_tokens, completion_tokens, model_name_for_calc)
    print(f"[Shift Analyzer] 토큰 사용량: {cost_info['usage']}, 비용(USD/KRW): {cost_info['cost_usd']} / {cost_info['cost_krw']}")

    # 최종 shift_result 형태로 반환 (기존 코드와 호환)
    shift_result = []
    if json_answer["result"]:
        shift_result.append({
            "shift": json_answer["result"]["shift"],
            "date": json_answer["result"]["date"],
            "score": json_answer["result"]["score"],
            "request": [request_text]  # 원본 텍스트 기록
        })

    return {"shift_result": shift_result}


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
            max_retries=3
        )

        graph = StateGraph(ShiftSubgraph)
        graph.add_node("init_data", init_data)
        graph.add_node("collector", collector)
        graph.set_entry_point('init_data')

        for n in range(len(requests)):
            def create_shift_node(n):
                async def wrapped_shift(state):
                    state['phase'] = n
                    state['allowed_shift_map'] = parent_state.get('allowed_shift_map', {})
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
                "allowed_shift_map": parent_state.get('allowed_shift_map', {})
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
                    state['allowed_shift_map'] = parent_state.get('allowed_shift_map', {})
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
                "allowed_shift_map": parent_state.get('allowed_shift_map', {})
            })
            print(f"[새 요청 결과] {result}")
            return {"shift_results": [result]}
        except Exception as e:
            print(f"[그래프 실행 오류] {e}")
            return {"shift_results": []}