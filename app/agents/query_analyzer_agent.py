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


class ShiftItem(BaseModel):
    """Shift 요청 항목 (텍스트 + 사유 + 극성)"""
    text: str  # 날짜+코드 형태의 요청 텍스트
    comment: str | None  # 사유 (없으면 null)
    # 극성: 그 근무를 원하는가(want) 피하는가(avoid).
    #   과거에는 Except 카테고리로 어휘("말고/빼고/제외/안 돼")를 매칭해 갈랐는데,
    #   한국어 완곡 부정은 열린 집합이라 "부담스러워요"/"피했으면 좋겠어요" 같은 표현이
    #   Shift 로 새어들어가 기피가 희망으로 뒤집혔다(실측 확인). 어휘 목록이 아니라
    #   문장 의도를 묻는 독립 축으로 분리한다.
    polarity: str = "want"


class queryAnalyzer(BaseModel):
    processor: str
    Chat: List[str]
    Shift: List[ShiftItem]  # {text, comment} 구조로 변경
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

            # 동적 예시 (allowed_shift_map 기반)
            dynamic_shift_examples += "\n### 동적 Few-shot 예시:\n"
            example_codes = list(allowed_shift_map.items())[:3]  # 처음 3개만 사용
            for i, (code, name) in enumerate(example_codes):
                day = 10 + i * 5
                dynamic_shift_examples += f'- "{day}일 {name}" → Shift: [{{"text": "{day}일은 {code}로 줘", "comment": null}}]\n'

            # 사유 포함 동적 예시
            dynamic_shift_examples += "\n### 사유 포함 예시 (존댓말 형식):\n"
            reason_examples = [
                ("지방 출장", "지방 출장이 있습니다"),
                ("가족 행사", "가족 행사가 있습니다"),
                ("병원 진료", "병원 진료가 있습니다"),
            ]
            for i, ((code, name), (reason_short, reason_polite)) in enumerate(zip(example_codes, reason_examples)):
                day = 3 + i * 2
                dynamic_shift_examples += f'- "{day}일 {reason_short}으로 {name}" → Shift: [{{"text": "{day}일은 {code}로 줘", "comment": "{reason_polite}"}}]\n'

        self.system = f"""
        ## GOAL:
            You are the "Nurse Preference Preprocessor."
            Convert Korean natural language input ➜ into a categorized List (JSON), decomposed and normalized.

        ## 1. Task Objectives
            1. If multiple dates, shifts, preferences, and except are mixed in a single sentence, split them into separate items by meaning.
            2. Each element in the Shift / Preference / Except / Others category must contain only a single piece of content.
            예)
            - "5/5는 쉬고 싶고, 5/6은 E로 줘" →
                `"Shift": [{{"text": "5/5은 OFF로 줘", "comment": null}}, {{"text": "5/6은 E로 줘", "comment": null}}]`
            3. If a date is omitted in an instruction ("그 외엔…"), supplement with the previous date to avoid loss of information.
            예) "5/5는 N, 그 외엔 E" →
                `"Shift": [{{"text": "5/5은 N로 줘", "comment": null}}, {{"text": "5/5 제외 나머지는 E로 줘", "comment": null}}]`
            4. Repetitive/pattern requests (e.g., "주말엔 쉬고 싶다", "매주 수요일은 OFF")
            must never be expanded into all dates; record as **one rule-based item**.
            - 예) "주말엔 쉬고 싶다" → `"Shift": [{{"text": "매주 주말은 O로 줘", "comment": null}}]`
            - 예) "수요일은 OFF" → `"Shift": [{{"text": "매주 수요일은 O로 줘", "comment": null}}]`
            - 예) "평일엔 D, 주말엔 O" → `"Shift": [{{"text": "평일은 D로 줘", "comment": null}}, {{"text": "주말은 O로 줘", "comment": null}}]`
            - 예) "10일은 D 말고" →
                `"Shift": [{{"text": "10일은 D", "comment": null, "polarity": "avoid"}}]`
            - 예) "수요일은 E 빼줘" →
                `"Shift": [{{"text": "매주 수요일은 E", "comment": null, "polarity": "avoid"}}]`
            - 예) "10일 나이트는 좀 부담스러워요" →
                `"Shift": [{{"text": "10일은 N", "comment": "부담이 됩니다", "polarity": "avoid"}}]`
            - 예) "가능하면 15일 나이트는 피했으면 좋겠어요" →
                `"Shift": [{{"text": "15일은 N", "comment": null, "polarity": "avoid"}}]`
            5. Absolutely no duplication/mixing: do not put OFF and E together in one element.
            6. Final JSON Keys:
                - Chat ― small talk unrelated to scheduling
                - Shift ― requests for dates/shifts/OFF
                  (각 항목은 {{"text": "...", "comment": "...", "polarity": "want"|"avoid"}} 형태)
                - Preference ― coworker together/avoid preferences (사람 이름이 나오는 경우만)
                - Except ― (사용 중단) 항상 [] 로 두세요. 부정 요청도 Shift 에 polarity="avoid" 로 넣습니다.
                - Others ― requests not fitting the above
                * Empty categories must remain [].
                * Element order must follow the input sequence.

        ## 1-2. polarity 판정 (가장 중요)
            근무/날짜 요청은 **전부 Shift 에 넣고**, 그 요청이 그 근무를
            **원하는 것인지(want) 피하려는 것인지(avoid)** 를 polarity 로 표시합니다.

            - 판정 기준은 **특정 단어가 있느냐가 아니라 문장 전체의 의도**입니다.
              "말고/빼고/제외" 같은 단어가 없어도 회피 의도면 avoid 입니다.
            - 완곡하거나 간접적인 표현도 회피 의도면 avoid 입니다.
              예: "부담스러워요", "힘들 것 같아요", "피했으면 좋겠어요", "가능하면 안 했으면",
                  "싫어요", "만 아니면", "자신이 없어요", "곤란해요"
            - text 는 polarity 와 무관하게 **원문에 언급된 근무코드를 그대로** 씁니다.
              회피 요청이라고 해서 다른 근무나 O 로 바꿔 추론하지 마세요.
              예) "10일 나이트는 부담스러워요" → text "10일은 N", polarity "avoid" (O 로 바꾸면 오답)
            - **제외구가 오히려 희망인 문장에 주의**하세요. 문장의 주된 요청이 무엇인지 보세요.
              예) "10일 빼고는 다 D로 줘" → 주된 요청은 D 희망입니다.
                  → Shift: [{{"text": "10일 제외 나머지는 D로 줘", "comment": null, "polarity": "want"}}]
                  (avoid 만들지 마세요)
            - **"빼달라/제외해달라" 는 두 가지로 갈립니다. 뒤에 다른 요청이 붙었는지 보세요.**

              (가) **뒤에 적용 대상 요청이 있으면** = 그 요청의 적용 범위를 좁히는 말입니다.
                   빠진 범위는 **언급이 없는 것**으로 두고 **항목을 만들지 마세요.**
                   없는 요청을 avoid 로 만들면 그 날들이 하드 금지로 굳어집니다.
                   예) "주말 빼고 평일에 D 위주로" → 요청은 평일 D 하나뿐입니다.
                       → Shift: [{{"text": "평일은 D로 줘", "comment": null, "polarity": "want"}}]
                       (주말 항목을 만들지 마세요. "주말은 O", "주말은 D" 어느 쪽도 오답입니다)
                   예) "주말 제외 전부 N" →
                       Shift: [{{"text": "주말 제외 나머지는 N로 줘", "comment": null, "polarity": "want"}}]
                   예) "평일만 근무하고 싶어요" →
                       Shift: [{{"text": "평일은 근무로 줘", "comment": null, "polarity": "want"}}]

              (나) **뒤에 다른 요청이 없고 근무코드도 안 나오면** = 그 날(범위) 근무에서
                   빼달라, 즉 **OFF 희망**입니다. want 로 만드세요. avoid 가 아닙니다.
                   avoid 로 만들면 "쉬고 싶다" 가 "쉬지 마라" 로 뒤집힙니다.
                   예) "주말은 빼주세요" →
                       Shift: [{{"text": "매주 주말은 O로 줘", "comment": null, "polarity": "want"}}]
                   예) "8일은 빼주세요" →
                       Shift: [{{"text": "8일은 O로 줘", "comment": null, "polarity": "want"}}]
                   예) "주말 근무는 빼주세요" →
                       Shift: [{{"text": "매주 주말은 O로 줘", "comment": null, "polarity": "want"}}]
                   ★ "근무", "스케줄" 같은 일반 명사는 근무코드가 아닙니다. 이것들과 함께
                     쓰인 "빼달라/제외/안 넣어달라/비워달라" 도 전부 (나)입니다.
                   ★ **질문·완곡 형태도 요청입니다.** "~가능할까요", "~해주실 수 있나요",
                     "~하면 안 될까요" 를 Chat 으로 보내지 마세요. 빈 결과가 됩니다.
                   예) "8일 근무 제외 가능할까요" →
                       Shift: [{{"text": "8일은 O로 줘", "comment": null, "polarity": "want"}}]
                   예) "8일은 비워주세요" →
                       Shift: [{{"text": "8일은 O로 줘", "comment": null, "polarity": "want"}}]

              ★ (나)와 달리 **근무코드가 명시된 회피**는 그 코드로 avoid 입니다.
                예) "8일 D는 빼주세요" → Shift: [{{"text": "8일은 D", "comment": null, "polarity": "avoid"}}]
                예) "주말 나이트는 힘들어요" → Shift: [{{"text": "매주 주말은 N", "comment": null, "polarity": "avoid"}}]
              ★ 그 범위를 **적극적으로 요청**하는 문장은 그대로 want 입니다.
                예) "주말엔 쉬고 싶어요" → Shift: [{{"text": "매주 주말은 O로 줘", "comment": null, "polarity": "want"}}]
                예) "평일엔 D, 주말엔 O" → 둘 다 요청이므로 두 항목 모두 polarity "want"
            - 같은 날짜에 want 와 avoid 를 동시에 만들지 마세요. 대체 근무가 지정되면 want 만 남깁니다.
              예) "10일은 E 말고 D로 줘" →
                  Shift: [{{"text": "10일은 D로 줘", "comment": null, "polarity": "want"}}]

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
        ## 의도 파악 규칙 (중요! — 반드시 번호 순서대로 우선 적용)
            **1. "근무" 키워드가 있으면 → 해당 shift 요청**
            - "아침근무", "오전근무" → Day(D)
            - "저녁근무" → Evening(E)
            - "밤근무", "야간근무" → Night(N)

            **2. 명시적 shift 코드 + 사유/이유 → 하나의 Shift 항목, 사유는 comment [최우선 규칙]**
            - 사용자가 특정 shift 코드(D, E, N, O 또는 allowed_shift_map의 코드)를 **명시적으로** 요청하면서
              같은 문맥에서 사유·이유·상황을 함께 언급한 경우,
              **절대로 사유를 별도의 Shift 항목(예: O)으로 분리하지 말고**, 해당 shift 항목의 comment에 넣으세요.
            - 이 규칙은 아래 3번(일정→Off) 규칙보다 **항상 우선**합니다.
            - 판별 기준: 한 문장 또는 연속 문장에서 shift 코드가 이미 특정되어 있고,
              뒤이어 나오는 내용이 새로운 날짜·shift 코드 없이 사유/이유만 설명하면 → comment로 처리
            - 예시:
              * "수요일 N주세요. 아침에 치과 치료있어요"
                → Shift: [{{"text": "매주 수요일은 N로 줘", "comment": "아침에 치과 치료가 있습니다"}}]
                (❌ 절대 O 요청으로 분리 금지!)
              * "10일 E로 넣어줘. 오후에 병원 가야해"
                → Shift: [{{"text": "10일은 E로 줘", "comment": "오후에 병원 방문이 있습니다"}}]
                (❌ 절대 O 요청으로 분리 금지!)
              * "매주 금요일 D줘. 아이 등원시켜야해서"
                → Shift: [{{"text": "매주 금요일은 D로 줘", "comment": "아이 등원이 있습니다"}}]

            **3. 일정/약속 키워드가 있고, shift 코드 지정이 없으면 → Off(O)**
            - "외래 방문", "병원", "진료", "면접", "약속", "행사" 등 → 근무 불가 상황
            - **단, 위 2번에 해당하면(shift 코드가 이미 명시된 경우) 이 규칙 적용 금지!**
            - 예시 (shift 코드 명시 없이 일정만 언급한 경우에만 O):
              * "24일 아침 외래 방문" → 24일은 O로 (shift 코드 언급 없음)
              * "10일 저녁 약속 있어요" → 10일은 O로 (shift 코드 언급 없음)

        ## 사유(comment) 추출 규칙 (중요!)
            - Shift의 각 항목은 {{"text": "...", "comment": "..."}} 형태입니다.
            - text: 날짜+코드 형태로 정규화된 요청
            - comment: 날짜/코드를 제외한 사유 (없으면 null)

            ### comment 변환 원칙 (필수!)
            **원칙 1: 순화** - 민감하거나 사적인 내용은 "개인 사정이 있습니다"로 일반화
            **원칙 2: 존댓말** - 단어/반말은 "~입니다", "~가 있습니다" 형태로 문장화
            **원칙 3: 간결함** - 핵심 사유만 남기고 불필요한 디테일 제거

            ### 변환 예시
            | 원본 | comment |
            |------|---------|
            | "밤에 업소 나가서" | "저녁에 개인 일정이 있습니다" |
            | "생일이라" | "생일입니다" |
            | "아이 병원 진료가 있어요" | 그대로 유지 |
            | 사유 없음 | null |

        ## 3. Processing Guidelines
            * Keep periodic expressions like "매주/주말/평일" exactly as in the original text, never expand into dates.
            * Uninterpretable sentences or ambiguous expressions must be placed in Others.


            # CONTEXT:
                "5/5는 쉬고 싶고, 5/19는 나이트 후 OFF, 8일은 데이 빼줘, 그리고 정간호사, 문지영이는 좀 꺼졌으면 좋겠어"

            # OUTPUT:
                {{
                "processor": "5/5는 쉬고싶다 했으므로 O, 5/19는 나이트, 그 후 OFF 달라고 했으니 5/20은 O로 처리, 8일 데이 제외는 회피 의도라 polarity=avoid, 정간호사/문지영 관련은 preference로 순화 처리",
                "Chat": [],
                "Shift": [
                    {{"text": "5/5은 O로 줘", "comment": "쉬고 싶어서요", "polarity": "want"}},
                    {{"text": "5/19는 N로 줘", "comment": null, "polarity": "want"}},
                    {{"text": "5/20은 O로 줘", "comment": null, "polarity": "want"}},
                    {{"text": "8일은 D", "comment": null, "polarity": "avoid"}}
                ],
                "Preference": [
                    "정간호사랑은 겹치기 싫어요", "문지영이랑은 겹치기 싫어요"
                ],
                "Except": [],
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
                "Shift": [{{"text": "5일은 D로 줘", "comment": null}}],
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
                "Shift": [{{"text": "매주 주말은 O로 줘", "comment": null}}],
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
                "Shift": [{{"text": "10일은 N으로 줘", "comment": null}}],
                "Preference": [
                    "정민아 쌤이랑 같은 날 되면 좋겠어요", "한서연이랑은 피하고 싶어요"
                ],
                "Except": [],
                "Others": []
                }}

            # CONTEXT:
                "매주 수요일 N주세요. 아침에 치과 치료있어요"

            # OUTPUT:
                {{
                "processor": "매주 수요일 N 요청 + 아침 치과 치료는 N의 사유(comment)로 처리. 별도 O로 분리하지 않음 (의도 파악 규칙 2번 적용)",
                "Chat": [],
                "Shift": [{{"text": "매주 수요일은 N로 줘", "comment": "아침에 치과 치료가 있습니다"}}],
                "Preference": [],
                "Except": [],
                "Others": []
                }}

            # CONTEXT:
                "15일 E로 해주세요. 오후에 아이 병원 데려가야해요"

            # OUTPUT:
                {{
                "processor": "15일 E 요청 + 오후 아이 병원은 E의 사유(comment)로 처리. 별도 O로 분리하지 않음 (의도 파악 규칙 2번 적용)",
                "Chat": [],
                "Shift": [{{"text": "15일은 E로 줘", "comment": "오후에 아이 병원 진료가 있습니다"}}],
                "Preference": [],
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
        ChatAnthropic(model="claude-haiku-4-5-20251001", anthropic_api_key=os.getenv("ANTHROPIC_API_KEY")),
        ChatGoogleGenerativeAI(model="gemini-2.0-flash", google_api_key=os.getenv("GOOGLE_API_KEY"))
    ]

    messages = [
        SystemMessage(content=prompt.system),
        HumanMessage(content=prompt.human)
    ]

    chat = []
    shift = []
    preference = []
    except_ = []
    others = []
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
            model_name_str = getattr(client, 'model', None) or getattr(client, 'model_name', 'unknown')
            print(f'[LLM 시도 {idx}] {model_name_str}')
            llm = client.with_structured_output(queryAnalyzer)
            resp = await llm.ainvoke(messages)
            used_model = model_name_str
            chat = resp.Chat
            shift = resp.Shift  # List[ShiftItem]
            preference = resp.Preference
            except_ = resp.Except
            others = resp.Others
            print(f'[LLM 성공] Shift: {shift}')
            break
        except Exception as e:
            print(f'[LLM 실패 {idx}] {e}')
            if idx == len(models_to_try):
                print('[모든 LLM 실패] 기본값 사용')

    # 디버깅 출력
    print("[QUERY ANALYZER 최종 출력]")
    print(f"query_shift: {shift}")
    print(f"query_others: {others}")
    print(f"query_chat: {chat}")

    # 토큰 계산
    model_name = used_model or getattr(models_to_try[0], 'model', None) or getattr(models_to_try[0], 'model_name', '')
    pt = _count_messages_tokens([prompt.system, prompt.human], model_name)

    # ShiftItem을 dict로 변환하여 JSON 직렬화
    shift_for_json = [
        {"text": s.text, "comment": s.comment, "polarity": getattr(s, "polarity", "want")}
        for s in shift
    ] if shift else []
    ct_json = json.dumps({"Chat": chat, "Shift": shift_for_json, "Preference": preference, "Except": except_, "Others": others}, ensure_ascii=False)
    ct = _count_tokens(ct_json, model_name)
    cost = _compute_cost(pt, ct, model_name)
    print(f'[토큰] {cost["usage"]} / {cost["cost_krw"]["total"]}원')

    # polarity 로 선호(want)/기피(avoid)를 가른다.
    #   want  → query_shift  → create_shift_analyzer
    #   avoid → query_except → create_avoid_analyzer
    # 레거시 Except 카테고리(구 프롬프트/구 모델 응답)는 avoid 로 흡수한다 — 프롬프트는
    # 이제 Except 를 비우도록 지시하지만, 모델이 옛 습관대로 채워도 유실되지 않게 한다.
    want_items = [s for s in (shift or []) if str(getattr(s, "polarity", "want")).lower() != "avoid"]
    avoid_items = [s for s in (shift or []) if str(getattr(s, "polarity", "want")).lower() == "avoid"]

    shift_texts = [s.text for s in want_items]
    shift_comments = [s.comment for s in want_items]
    avoid_texts = [s.text for s in avoid_items] + list(except_ or [])
    print(f"[polarity 분리] want={len(shift_texts)}건, avoid={len(avoid_texts)}건"
          f"{' (레거시 Except ' + str(len(except_)) + '건 흡수)' if except_ else ''}")

    ret = {
        "query_chat": chat,
        "query_shift": shift_texts,  # 기존 호환성 유지 (want 만)
        "query_shift_comments": shift_comments,  # 새로 추가: 사유 리스트
        "query_preference": preference,
        "query_except": avoid_texts,
        "query_others": others,
        "model": models_to_try[0]
    }
    if case_results:
        ret["case_results"] = case_results

    return ret
