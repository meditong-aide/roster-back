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


class queryAnalyzerPrompt:
    def __init__(self, context, year, month, allowed_shift_map: dict = None):
        """
        프롬프트 클래스
        """
        allowed_shift_map = allowed_shift_map or {}
        
        # 동적 Few-shot 예시 생성
        dynamic_shift_examples = ""
        if allowed_shift_map:
            dynamic_shift_examples = "## 지원되는 shift 코드 매핑 (반드시 이 코드로 변환하세요)\n"
            dynamic_shift_examples += "사용자가 아래 shift 이름을 언급하면 반드시 해당 shift_id로 변환하여 Shift 리스트에 넣으세요.\n\n"
            for code, name in allowed_shift_map.items():
                dynamic_shift_examples += f'- "{name}", "{name} 신청", "{name}로 해줘", "{name} 하고 싶어" → Shift: ["{code}"]\n'
            dynamic_shift_examples += "\n예시:\n"
            dynamic_shift_examples += '- "10일 생리휴가" → Shift: ["10일은 생로 줘"]\n'
            dynamic_shift_examples += '- "15일 법정교육" → Shift: ["15일은 법로 줘"]\n'
            dynamic_shift_examples += '- "20일 필수교육 빼줘" → Except: ["20일은 필 말고"], Shift: []\n'
        
        self.system = f"""
        ## GOAL:
            You are the "Nurse Preference Preprocessor."  
            Convert Korean natural language input ➜ into a categorized List (JSON), decomposed and normalized.

        ## 1. Task Objectives
            1. If multiple dates, shifts, preferences, and except are mixed in a single sentence, split them into separate items by meaning.
            2. Each element in the Shift / Preference / Except / Others category must contain only a single piece of content.  
            예)  
            - "5/5는 쉬고 싶고, 5/6은 E로 줘" →  
                `"Shift": ["5/5은 OFF로 줘", "5/6은 E로 줘"]`
            3. If a date is omitted in an instruction ("그 외엔…"), supplement with the previous date to avoid loss of information.  
            예) "5/5는 N, 그 외엔 E" →  
                `"Shift": ["5/5은 N로 줘", "5/5 제외 나머지는 E로 줘"]`
            4. Repetitive/pattern requests (e.g., "주말엔 쉬고 싶다", "매주 수요일은 OFF")  
            must never be expanded into all dates; record as **one rule-based item**.  
            - 예) "주말엔 쉬고 싶다" → `"Shift": ["매주 주말은 O로 줘"]`  
            - 예) "수요일은 OFF" → `"Shift": ["매주 수요일은 O로 줘"]`  
            - 예) "평일엔 D, 주말엔 O" → `"Shift": ["평일은 D로 줘", "주말은 O로 줘"]`
            - 예) "10일은 D 말고" → `"Except": ["10일은 D 말고"]`
            - 예) "수요일은 E 빼줘" → `"Except": ["수요일은 E 빼줘"]`
            5. Absolutely no duplication/mixing: do not put OFF and E together in one element.
            6. Final JSON Keys:
                - Chat ― small talk unrelated to scheduling
                - Shift ― requests for dates/shifts/OFF
                - Preference ― coworker together/avoid preferences
                - Except ― negative/exclusion requests (e.g., 말고, 빼고, 제외, 안 돼)
                - Others ― requests not fitting the above
                * Empty categories must remain [].
                * Element order must follow the input sequence.

        ## 2. Mandatory Rules
            {dynamic_shift_examples}
            
            | Expression | Conversion Example |
            | - | - |
            | Day shift | "D" |
            | Evening   | "E" |
            | Night     | "N" |
            | Off/휴무    | "O" |
            | Date formats | `M/D` or `M월 D일` are all allowed, but keep the original form in output |
            | Periodic expressions | "매주", "주말", "평일", "격주" etc. remain as rule-based items |
        
        ## 필수 규칙 재강조
            - show_in_preference=True인 shift 이름(생리휴가, 법정교육, 필수교육, 휴가, 공가, 보수교육 등)은 **반드시** 해당 shift_id("생", "법", "필", "휴", "공", "보" 등)로 변환하여 Shift에 넣으세요.
            - Shift 요청이 명확하면 **절대 빈 리스트로 반환하지 마세요**.
            - "생리휴가", "법정교육" 같은 단어가 나오면 무조건 Shift 카테고리로 분류하세요.
            - 날짜 + shift 이름 조합은 Shift에 우선 배치하세요.

        ## Preference(선호/비선호 동료) 분류 원칙
            **핵심 원칙: 특정 동료의 이름(또는 호칭)이 언급되고, 그 사람에 대한 어떠한 감정·의견·평가·요청이 함께 표현되면 무조건 Preference로 분류합니다.**

            - "이름/호칭"의 형태: 성명, 이름만, "~간호사", "~쌤", "~씨", "~이/~랑/~한테" 등 사람을 지칭하는 모든 표현
            - "감정/의견"의 형태: 제한 없음. 긍정·부정·중립, 구어·문어, 존댓말·반말, 직접적·간접적 표현 모두 포함
            - 여러 사람이 한 문장에 나오면 **각각 개별 Preference 항목으로 분리**
            - Shift/날짜 요청과 섞여 있어도 사람에 대한 부분은 반드시 Preference로 분리
            - **절대로 사람에 대한 감정/의견/관계 표현을 Chat이나 Others로 분류하지 마세요**

            분류 판단 기준:
            | 입력에 포함된 요소 | 분류 |
            | - | - |
            | 사람 이름 + 어떤 감정/의견/평가/관계 표현 | **Preference** |
            | 사람 이름 + 같이/따로 근무 요청 | **Preference** |
            | 사람 이름 없이 일반 대화 | Chat |
            | 사람 이름 없이 근무/날짜 요청 | Shift / Except |

        ## 3. Processing Guidelines
            * Keep periodic expressions like "매주/주말/평일" exactly as in the original text, never expand into dates.
            * Uninterpretable sentences or ambiguous expressions must be placed in Others.

            
            # CONTEXT:
                "5/5는 쉬고 싶고, 5/19는 나이트 후 OFF, 그리고 정간호사, 문지영이는 좀 꺼졌으면 좋겠어"

            # OUTPUT:
                {{
                "processor": "5/5는 쉬고싶다 했으므로 O, 5/19는 나이트, 그리고 그 후 OFF 달라고 했으니 5/20은 O로 처리, 정간호사, 문지영 관련은 preference로 처리하되, 순화적용",
                "Chat": [],
                "Shift": [
                    "5/5은 쉬고 싶고",
                    "5/19는 N,
                    "5/20은 O"
                ],
                "Preference": [
                    "정간호사랑은 겹치기 싫어요", "문지영이랑은 겹치기 싫어요"
                ],
                "Except": [],
                "Others": []
                }}
            
            # CONTEXT:
                "8,9일은 데이 빼줘. 주말은 쉬고싶어"

            # OUTPUT:
                {{
                "processor": "8, 9일은 데이 빼달라고 했으므로 제외규칙 상 Others로 처리, 주말은 쉬고싶어는 shift로 처리",
                "Chat": [],
                "Shift": ["주말은 쉬고싶어"],
                "Preference": [],
                "Except": ["8, 9일은 데이 빼줘"],
                "Others": []
                }}

            # CONTEXT:
                "유지민 권은비 너무좋아"

            # OUTPUT:
                {{
                "processor": "유지민, 권은비에 대한 긍정적 감정 표현이므로 각각 Preference로 분류",
                "Chat": [],
                "Shift": [],
                "Preference": [
                    "유지민이랑 같이 근무하고 싶어요", "권은비랑 같이 근무하고 싶어요"
                ],
                "Except": [],
                "Others": []
                }}

            # CONTEXT:
                "5일은 D 넣어주고 박서준 간호사는 되도록 안 겹쳤으면, 이수현이는 괜찮아"

            # OUTPUT:
                {{
                "processor": "5일 D는 Shift, 박서준은 비선호 Preference, 이수현은 선호 Preference로 분류",
                "Chat": [],
                "Shift": ["5일은 D로 줘"],
                "Preference": [
                    "박서준 간호사랑은 안 겹쳤으면 좋겠어요", "이수현이랑은 같이 근무해도 괜찮아요"
                ],
                "Except": [],
                "Others": []
                }}

            # CONTEXT:
                "홍길동 선호 김영희 비선호"

            # OUTPUT:
                {{
                "processor": "홍길동 선호, 김영희 비선호 각각 Preference로 분류",
                "Chat": [],
                "Shift": [],
                "Preference": [
                    "홍길동이랑 같이 근무하고 싶어요", "김영희랑은 겹치기 싫어요"
                ],
                "Except": [],
                "Others": []
                }}

            # CONTEXT:
                "최지은이랑 일하면 힘들어서 좀 빼줬으면.. 그리고 주말은 쉬고싶어"

            # OUTPUT:
                {{
                "processor": "최지은에 대한 부정적 의견은 Preference, 주말 쉬고싶은 건 Shift로 분류",
                "Chat": [],
                "Shift": ["주말은 쉬고싶어"],
                "Preference": [
                    "최지은이랑은 겹치기 싫어요"
                ],
                "Except": [],
                "Others": []
                }}

            # CONTEXT:
                "10일 N 주세요. 아 그리고 정민아 쌤이랑 같은 날 되면 좋겠고, 한서연은 좀 피하고 싶어요"

            # OUTPUT:
                {{
                "processor": "10일 N은 Shift, 정민아 쌤 선호와 한서연 비선호는 각각 Preference로 분류",
                "Chat": [],
                "Shift": ["10일은 N으로 줘"],
                "Preference": [
                    "정민아 쌤이랑 같은 날 되면 좋겠어요", "한서연이랑은 피하고 싶어요"
                ],
                "Except": [],
                "Others": []
                }}

        """
        
        self.human=f"""
            # CONTEXT: 
            {year}년 {month}월의 근무표를 짜기 위해서 다음과 같은 요청을 받았습니다.
            {context}
            # OUTPUT:
        """


async def query_analyzer(state):
    print('[query_analyzer] 입력 state:', state)
    context = state['request']
    year = state['year']
    month = state['month']
    
    allowed_shift_map = state.get('allowed_shift_map', {})
    
    prompt = queryAnalyzerPrompt(
        context=context, 
        year=year, 
        month=month,
        allowed_shift_map=allowed_shift_map
    )

    models_to_try = [
        ChatOpenAI(model="gpt-4.1-mini-2025-04-14", openai_api_key=os.getenv("OPENAI_API_KEY")),
        ChatAnthropic(model="claude-3-7-sonnet-20250219", anthropic_api_key=os.getenv("ANTHROPIC_API_KEY")),
        ChatGoogleGenerativeAI(model="gemini-2.0-flash", google_api_key=os.getenv("GOOGLE_API_KEY"))
    ]

    messages = [
        SystemMessage(content=prompt.system),
        HumanMessage(content=prompt.human)
    ]

    chat = shift = preference = except_ = others = []
    used_model = ""
    case_results = None

    # case 처리 (항상 수행, 하지만 LLM 호출은 무조건)
    if state.get('case'):
        print('[case 감지] case_results 생성 시작')
        case_results = []
        for c in state['case']:
            reason = c.get('reason')
            if reason == '기존 데이터에서 로드됨':
                continue
            date_str = c.get('date')
            shift_type = c.get('shift')
            if not date_str or not shift_type:
                continue
            try:
                day = int(str(date_str).split('-')[2]) if '-' in str(date_str) else int(date_str)
                case_results.append({
                    'date': day,
                    'shift': shift_type,
                    'score': 1.0,
                    'request': '단순 희망'
                })
            except:
                continue
        print(f'[case 처리 완료] {len(case_results)}건')

    # LLM 호출 → case 유무 상관없이 항상 실행 (새 요청 처리 보장)
    for idx, client in enumerate(models_to_try, 1):
        try:
            print(f'[LLM 시도 {idx}] {client.model_name}')
            llm = client.with_structured_output(queryAnalyzer)
            resp = await llm.ainvoke(messages)
            used_model = client.model_name
            chat, shift, preference, except_, others = resp.Chat, resp.Shift, resp.Preference, resp.Except, resp.Others
            print(f'[LLM 성공] Shift: {shift}')
            break
        except Exception as e:
            print(f'[LLM 실패 {idx}] {e}')
            if idx == len(models_to_try):
                print('[모든 LLM 실패] 기본값 사용')
    
    # elqjrlddyd fhrm
    print("[QUERY ANALYZER 최종 출력]")
    print(f"query_shift: {shift}")
    print(f"query_others: {others}")
    print(f"query_chat: {chat}")

    # 토큰 계산
    model_name = used_model or models_to_try[0].model_name
    pt = _count_messages_tokens([prompt.system, prompt.human], model_name)
    ct_json = json.dumps({"Chat": chat, "Shift": shift, "Preference": preference, "Except": except_, "Others": others}, ensure_ascii=False)
    ct = _count_tokens(ct_json, model_name)
    cost = _compute_cost(pt, ct, model_name)
    print(f'[토큰] {cost["usage"]} / {cost["cost_krw"]["total"]}원')

    ret = {
        "query_chat": chat,
        "query_shift": shift,
        "query_preference": preference,
        "query_except": except_,
        "query_others": others,
        "model": models_to_try[0]
    }
    if case_results:
        ret["case_results"] = case_results

    return ret