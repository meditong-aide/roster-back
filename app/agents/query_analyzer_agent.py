import json
from pydantic import BaseModel
from typing import List, TypedDict, Annotated, operator
from google import genai
from google.genai import types
import dotenv
from langgraph.prebuilt import create_react_agent
from langchain_mcp_adapters.client import MultiServerMCPClient
import pprint
from langchain_anthropic import ChatAnthropic
from langchain_openai import ChatOpenAI
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_core.messages import SystemMessage, HumanMessage
import os
try:
    import tiktoken
except Exception:
    tiktoken = None

dotenv.load_dotenv()

class queryAnalyzer(BaseModel):
    processor: str
    Chat: List[str] 
    Shift: List[str] 
    Preference: List[str]
    Except: List[str]
    Others: List[str] 
    year: int
    month: int


# ------------------------------
# 비용/토큰 유틸리티
# ------------------------------
def _krw_per_usd() -> float:
    try:
        return float(os.getenv("KRW_PER_USD", "1350"))
    except Exception:
        return 1350.0


def _pricing_per_1k(model_name: str) -> tuple[float, float]:
    """
    모델별 1K 토큰당 (입력, 출력) USD 비용을 반환.
    값은 최신 요금과 다를 수 있으니 환경에 맞게 조정 필요.
    """
    name = (model_name or "").lower()
    # 기본값 (보수적)
    input_usd, output_usd = 0.003, 0.009
    if "gpt-4o" in name:
        # OpenAI GPT-4o (약 $5/$15 per 1M)
        input_usd, output_usd = 0.005, 0.015
    elif "claude" in name:
        # Anthropic Claude Sonnet (약 $3/$15 per 1M)
        input_usd, output_usd = 0.003, 0.015
    elif "gemini" in name:
        # Google Gemini Flash (약 $0.35/$1.05 per 1M)
        input_usd, output_usd = 0.00035, 0.00105
    return input_usd, output_usd


def _encoding_for_model(model_name: str):
    name = (model_name or "").lower()
    try:
        if "gpt" in name or "o" in name:
            return tiktoken.get_encoding("o200k_base")
        # 범용 기본 인코딩
        return tiktoken.get_encoding("cl100k_base")
    except Exception:
        return None


def _count_tokens(text: str, model_name: str) -> int:
    if not text:
        return 0
    enc = _encoding_for_model(model_name)
    if enc is None:
        # 대략적 근사치 (문자 수 / 4)
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


def parse_allowed_shifts(shift_data: str) -> str:
    """
    shifts 테이블 문자열에서 show_in_preference=1인 shift_id만 추출
    """
    allowed = []
    lines = shift_data.strip().split("\n")
    for line in lines:
        columns = line.split()
        if len(columns) < 18:
            continue
        shift_id = columns[0]
        show_in_preference = columns[-1]
        if show_in_preference == "1":
            allowed.append(shift_id)
    
    allowed_str = ", ".join(sorted(set(allowed)))
    return allowed_str if allowed_str else "없음"


class queryAnalyzerPrompt:
    def __init__(self, context: str, year: int, month: int, 
                 allowed_shifts: str = '', 
                 shift_data: str = '', 
                 allowed_shift_map: dict = None):  # 새로 추가: shift_id -> name 매핑
        """
        allowed_shift_map 예시: {'D': 'Day', '생': '생리휴가(무급)', '경': '경조휴가', ...}
        """
        # 1. allowed_shifts 파싱 (기존 로직 유지)
        raw_allowed = parse_allowed_shifts(shift_data) if shift_data else allowed_shifts
        self.allowed_list = [s.strip() for s in raw_allowed.split(",") if s.strip()]
        self.allowed_str = raw_allowed if raw_allowed else "없음"

        # allowed_shift_map이 없으면 빈 딕셔너리 (호환성 유지)
        self.allowed_shift_map = allowed_shift_map or {}

        # 2. 동적 매핑 규칙 + Few-Shot 예시 생성
        mapping_rules = []
        few_shot_examples = []

        # 기본 근무 코드 매핑 (항상 고정 - 병원 공통)
        base_mappings = {
            "D": ["데이", "day", "D", "아침근무", "오전근무", "데이근무"],
            "E": ["이브닝", "evening", "E", "오후근무", "이브닝근무"],
            "N": ["나이트", "night", "N", "야간", "밤근무", "나이트근무"],
            "O": ["오프", "off", "O", "휴무", "쉬고", "쉬", "off"],
        }

        # D/E/N/O에 대한 기본 규칙 + 예시 생성 (항상 포함)
        for code, synonyms in base_mappings.items():
            if code in self.allowed_list:  # 허용된 경우만
                examples = ", ".join(synonyms)
                mapping_rules.append(f'- "{examples}" → "{code}" 코드로 변환')
                few_shot_examples.append(f"""
                # CONTEXT:
                    "15일 {synonyms[0]}로 해주세요"
                # OUTPUT:
                    {{"processor": "기본 근무 코드 '{code}' 매핑", "Chat": [], "Shift": ["15일은 {code}로 줘"], "Preference": [], "Except": [], "Others": []}}
                """)

        # ===== 동적 특수 코드 처리 (핵심!) =====
        # D/E/N/O를 제외한 모든 허용 코드에 대해 자동 생성
        special_codes = [code for code in self.allowed_list if code not in base_mappings]

        for code in special_codes:
            name = self.allowed_shift_map.get(code, code)  # name 있으면 사용, 없으면 코드 자체
            # 자연어 표현 추정: "생리휴가(무급)" → "생리휴가", "경조휴가" → "경조휴가" 등
            # 괄호 제거하고 주요 키워드 추출 (간단하게)
            display_name = name.split('(')[0].strip()  # "생리휴가(무급)" → "생리휴가"

            mapping_rules.append(f'- "{display_name}", "{name}", "{code}" 등이 언급되면 → 반드시 "{code}" 코드로 변환 (절대 D/E/N/O로 바꾸지 말 것)')

            # Few-shot 예시 1: 일반 요청
            few_shot_examples.append(f"""
            # CONTEXT:
                "16일에 {display_name} 부탁드려요"
            # OUTPUT:
                {{"processor": "특수 코드 '{code}' ({name}) 요청 감지 → Shift 처리", "Chat": [], "Shift": ["16일은 {code}로 줘"], "Preference": [], "Except": [], "Others": []}}
            """)

            # Few-shot 예시 2: 변형 표현
            few_shot_examples.append(f"""
            # CONTEXT:
                "이번 달 10일에 {name} 써도 될까요?"
            # OUTPUT:
                {{"processor": "특수 코드 '{code}' 요청 → 정확히 '{code}' 유지", "Chat": [], "Shift": ["10일은 {code}로 줘"], "Preference": [], "Except": [], "Others": []}}
            """)

        # 불허용 코드 처리 규칙 (기존 유지)
        disallow_rule = """
        - 허용 목록에 없는 코드를 요청하면 → Shift에 포함 금지
        - 대신 Others에 원문 기록 + "희망근무 선택 불가합니다. 수간호사에게 문의하세요" 설명 추가
        """

        # 문자열화 (토큰 절약 위해 few_shot은 최대 8개 제한)
        dynamic_rules = "\n".join(mapping_rules)
        dynamic_few_shots = "\n".join(few_shot_examples[:8])

        # 3. 전체 system 프롬프트 조합
        self.system = f"""
        ## GOAL:
            You are the "Nurse Preference Preprocessor."
            Convert Korean natural language input into categorized JSON.

        ## 이번 달 허용 근무코드 (가장 중요! 반드시 준수!)
        - 허용 코드: {self.allowed_str}
        - 이 목록에 있는 코드만 Shift 카테고리에 포함 가능합니다.
        - 특수 코드(예: 생, 경, 연, 교후 등)는 절대 D/E/N/O로 변환하지 말고 그대로 유지하세요.

        ## 자연어 → 코드 변환 규칙 (이번 그룹 정책 기반 동적 생성)
        {dynamic_rules}
        {disallow_rule}

        ## 처리 규칙
        1. 하나의 문장에 여러 요청이 섞여 있으면 의미 단위로 분리
        2. Shift 배열의 각 요소는 하나의 요청만 포함
        3. 패턴 요청("주말은 쉬고", "매주 수요일 OFF")은 날짜 확장 금지
        4. 기존 희망과 새 요청 충돌 시 새 요청 우선
        5. 모호하거나 불허용 요청은 Others로 이동
        6. 빈 배열은 [] 유지

        ## 동적 Few-Shot 예시 (현재 허용 코드 기반)
        {dynamic_few_shots}

        ## 참고 예시
        # CONTEXT:
            "5/5는 쉬고 싶고, 5/19는 나이트 후 OFF"
        # OUTPUT:
            {{"processor": "...", "Shift": ["5/5은 O로 줘", "5/19는 N로 줘", "5/20은 O로 줘"], ...}}
        """

        self.human = f"""
        # CONTEXT: 
        {year}년 {month}월의 근무 희망 요청입니다.
        {context}
        # OUTPUT:
        """


async def query_analyzer(state):
    print('state keys:', state.keys())  # 디버깅용

    context = state['request']
    year = state['year']
    month = state['month']

    # 기존 전달 방식 유지 (호환성)
    allowed_shifts = state.get('allowed_shifts', '없음')
    shift_data = state.get('shift_data', '')

    # 새로 추가: DB에서 가져온 shift_id → name 매핑 (wanted_service.py에서 전달해야 함)
    allowed_shift_map = state.get('allowed_shift_map', {})  # 예: {'생': '생리휴가(무급)', '경': '경조휴가', ...}

    # 백업 모델들 (기존 유지)
    models_to_try = [
        ChatOpenAI(
            model="gpt-4.1-mini-2025-04-14",
            openai_api_key=os.getenv("OPENAI_API_KEY"),
        ),
        ChatAnthropic(
            model="claude-3-7-sonnet-20250219",
            anthropic_api_key=os.getenv("ANTHROPIC_API_KEY"),
        ),
        ChatGoogleGenerativeAI(
            model="gemini-2.0-flash",
            google_api_key=os.getenv("GOOGLE_API_KEY"),
        )
    ]

    # ===== case 처리 로직 (기존과 동일) =====
    enhanced_context = None
    if state.get('case') is not None:
        if context:  # 새 요청 있음 → 기존 case와 병합해서 LLM에 전달
            print("case 존재 + 새로운 요청 있음 → LLM으로 병합 처리")
            existing_cases = []
            for c in state['case']:
                if c.get('reason') != '기존 데이터에서 로드됨':
                    date_str = c.get('date', '')
                    if '-' in date_str:
                        day = date_str.split('-')[2].lstrip('0')
                    else:
                        day = date_str
                    shift = c.get('shift', '')
                    if day and shift:
                        existing_cases.append(f"{day}일: {shift}")

            enhanced_context = f"""
            [기존 희망 근무 - 변경하지 말고 유지하세요. 새 요청과 충돌 시 새 요청을 우선 적용하세요]
            {', '.join(existing_cases) if existing_cases else '없음'}

            [새로운 요청 - 반드시 반영하세요]
            {context}
            """
        else:  # 새 요청 없음 → 과거 case 그대로 반환 (LLM 호출 생략)
            print("case만 존재 → 과거 데이터 그대로 반환")
            case_results = []
            for c in state['case']:
                if c.get('reason') != '기존 데이터에서 로드됨':
                    date_str = c.get('date', '')
                    try:
                        if '-' in date_str:
                            day = int(date_str.split('-')[2])
                        else:
                            day = int(date_str)
                    except:
                        continue
                    shift = c.get('shift', '')
                    if day and shift:
                        case_results.append({
                            'date': day,
                            'shift': shift,
                            'score': 1.0,
                            'request': '단순 희망'
                        })
            return {
                "case_results": case_results,
                "query_chat": [],
                'query_shift': [],
                'query_preference': [],
                'model': models_to_try[0]
            }
    # ===== case 처리 끝 =====

    # 프롬프트 생성 (enhanced_context 있으면 그걸 사용)
    final_context = enhanced_context or context
    query_analyzer_prompt = queryAnalyzerPrompt(
        final_context,
        year,
        month,
        allowed_shifts=allowed_shifts,
        shift_data=shift_data,
        allowed_shift_map=allowed_shift_map  # 핵심 추가!
    )

    messages = [
        SystemMessage(content=query_analyzer_prompt.system),
        HumanMessage(content=query_analyzer_prompt.human)
    ]

    chat = []
    shift = []
    preference = []
    except_ = []
    others = []
    used_model_name = ""

    # LLM 호출 루프 (기존과 동일)
    for i, client in enumerate(models_to_try):
        try:
            print(f"Query Analyzer: {i+1}차 모델 시도 중..., 모델: {client.model if hasattr(client, 'model') else client}")

            llm = client.with_structured_output(queryAnalyzer)
            response = await llm.ainvoke(messages)
            used_model_name = getattr(client, "model_name", "") or getattr(client, "model", "") or used_model_name

            print("\n\n\nresponse", response, "\n\n\n")

            chat = response.Chat
            shift = response.Shift
            preference = response.Preference
            except_ = response.Except
            others = response.Others

            print(f"Query Analyzer: {i+1}차 모델 성공!", response)
            break

        except Exception as e:
            error_msg = str(e).lower()
            print(f"Query Analyzer: {i+1}차 모델 오류 - {e}")

            if ("429" in error_msg or "rate" in error_msg or
                "529" in error_msg or "service unavailable" in error_msg or
                "quota" in error_msg or "limit" in error_msg):
                if i < len(models_to_try) - 1:
                    print(f"Query Analyzer: {i+2}차 백업 모델로 재시도...")
                    continue
                else:
                    print("Query Analyzer: 모든 백업 모델 실패, 기본값 사용")
                    break
            else:
                if i < len(models_to_try) - 1:
                    print(f"Query Analyzer: 예상치 못한 오류, {i+2}차 백업 모델로 재시도...")
                    continue
                else:
                    print("Query Analyzer: 모든 모델 실패, 기본값 사용")
                    break

    # 토큰/비용 계산 (기존 유지)
    model_name_for_calc = used_model_name or (getattr(models_to_try[0], "model", "") or "")
    prompt_tokens = _count_messages_tokens([query_analyzer_prompt.system, query_analyzer_prompt.human], model_name_for_calc)
    completion_json = json.dumps({
        "Chat": chat,
        "Shift": shift,
        "Preference": preference,
        "Except": except_,
        "Others": others
    }, ensure_ascii=False)
    completion_tokens = _count_tokens(completion_json, model_name_for_calc)
    cost_info = _compute_cost(prompt_tokens, completion_tokens, model_name_for_calc)

    print(f"Query Analyzer 답변: query_chat: {chat}, query_shift: {shift}, query_preference: {preference}, query_except: {except_}, query_others: {others}")
    print(f"토큰 사용량: {cost_info['usage']}, 비용(USD/KRW): {cost_info['cost_usd']} / {cost_info['cost_krw']}")

    return {
        "query_chat": chat,
        'query_shift': shift,
        'query_preference': preference,
        'query_except': except_,
        'query_others': others,
        'model': models_to_try[0]
    }
