# 라우터 모델 선정 — 왜 gpt-5.4-nano 인가 (2026-05-29)

## 배경
agent_v3 앞단의 **2단계 라우터**는 사용자 질의를 9개 카테고리로 분류해 메인 루프에
넘길 tool subset 을 좁힌다. 이 분류 호출은 매 턴 1회 추가로 발생하므로, 메인 turn 모델
(gpt-5.5, 고성능·고가)을 그대로 쓰면 비용·지연이 불필요하게 커진다. 분류는 작은 모델로
충분하다는 가설하에, **라우터 전용 저가 모델**을 실측으로 골랐다.

## 후보
실제 OpenAI API 가용 모델 기준 (※ `gpt-5.1-mini`, `gpt-5.5-mini` 는 **실존하지 않음**):
- `gpt-5.4-nano` — 미니 라인 최저가
- `gpt-5.4-mini`
- `gpt-4.1-mini`
- `gpt-5.5` — 현행 메인(기준선)

## 방법론 (`scripts/router_model_bench.py`)
- 수간호사 관점 **라벨 질의 14개** × **모델당 10회** = 모델당 140 호출.
- 측정 지표:
  - **분기 정확도(accuracy)** = `필요 tool ∈ resolve_tools(분류결과)` 비율. 즉 라우터가
    그 질의를 처리할 tool 을 scoped set 에 제대로 포함시켰는가(=올바른 분기).
  - **지연(latency)** — classify 1회 호출 시간 (avg / p95).
  - **비용** — LLMResponse 토큰 사용량 → `cost.compute_cost`(USD). $/1k calls 환산.

## 1차 결과 — 미니들이 ~80%로 저조
| model | acc% | avg ms | $/1k calls |
|---|---|---|---|
| gpt-5.4-nano | 79.3% | 827 | $0.094 |
| gpt-5.4-mini | 83.6% | 653 | $0.351 |
| gpt-4.1-mini | 82.1% | 675 | $0.177 |
| gpt-5.5 (기준선) | 100.0% | 3015 | $1.465 |

## "퀄 안좋으면 왜그런지 재확인" — 원인 진단
미스를 per-query 로 까보니 **모델 무능이 아니라 카테고리 경계 혼동**(체계적):
1. **"김민지 야간전담으로 바꿔줘"** → 모두 `mutate` 선택(필요: `settings_people`/update_person_attr).
   "바꿔"가 근무 변경(mutate)으로 읽힘. 간호사 *속성* 변경인데.
2. **"시니어/A팀 최소 N명"** → `settings_rules` 선택(필요: `settings_people`).
   "최소 인원"이 병동 규칙으로 읽힘. 등급/팀 최소인원인데.
3. **"위반사항 뭐야"** → `read` 선택(필요: `validate_repair`).

→ 픽스: `_CLASSIFY_SYSTEM`(router.py)에 위 3개 경계 disambiguation 문장 추가
(키워드 lookup 아님 — 경계를 가르치는 의미 설명).

## 2차 결과 — 미니 전부 ~100%
| model | acc% | avg ms | $/1k calls |
|---|---|---|---|
| **gpt-5.4-nano** | **100.0%** | 777 | **$0.118** |
| gpt-5.4-mini | 100.0% | 629 (최저 지연) | $0.439 |
| gpt-4.1-mini | 97.9% | 790 | $0.224 |

## 결정 — gpt-5.4-nano
- **정확도** 100% (기준선 gpt-5.5 와 동률).
- **비용** $0.118/1k calls — gpt-5.5($1.465) 대비 **~12배 저렴**.
- **지연** 777ms — gpt-5.5(3015ms) 대비 **~4배 빠름** (5.4-mini 가 629ms 로 약간 더
  빠르지만 비용이 3.7배라 nano 채택).
- 종합: 정확도·비용·지연 3박자에서 nano 가 최적. 분류는 소형 모델로 충분하다는 가설 확인.

## 적용 방법
- `app/agents_v2/llm_client.py` `get_router_llm_client()` → 기본 `gpt-5.4-nano`,
  `ROUTER_MODEL` env 로 override 가능.
- prod 진입점(`chat_router._get_agent`, `test_chat_router._run_v3`)이 이 함수로 router_llm 주입.
- **메인 turn 모델은 gpt-5.5 유지** (도구 호출·추론·답변 품질 위해).

## 주의 / 한계
- nano 는 **라우팅(분류) 용도로만** 검증됨. 메인 agent 루프(도구 호출+다단계 추론+답변
  생성)에 nano 를 쓰는 건 별개 문제이며 **벤치마크되지 않음** — 바꾸려면 별도 검증 필요.
- `cost.MODEL_PRICING` 단가는 2026-05 검색 기준 **추정치**. 정확한 최신가는
  openai.com/api/pricing 로 갱신.
- 재현: `uv run python scripts/router_model_bench.py`
  (서브셋: `BENCH_MODELS="gpt-5.4-nano,gpt-4.1-mini" uv run python scripts/router_model_bench.py`)
