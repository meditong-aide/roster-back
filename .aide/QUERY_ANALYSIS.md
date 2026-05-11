# 유저 예시 쿼리 분석 — v3 설계 검증

> 각 쿼리가 현재 v3 설계(tool-calling loop + domain knowledge prompt)에서 
> 어떻게 처리되는지 분석. 안 되는 것은 원인과 해결책 명시.

---

## 원티드 관련

### 1. "이번 달 9병동 원티드 신청 내역 보여줘"
**처리**: ✅ 1 turn
```
LLM → query_schedule(scope="wanted_submissions")
```
- group_id는 session context에서 주입
- year/month도 session context
- 결과: 간호사별 원티드 목록

### 2. "김민지 간호사가 4월에 올린 원티드만 조회해줘"
**처리**: ✅ 1 turn
```
LLM → query_schedule(scope="wanted_submissions", nurse_name="김민지")
```
- skill 내부: resolve_nurse("김민지") → nurse_id
- 없거나 2명이면 clarification 반환
- 4월 → domain knowledge에서 "month 지정 시 해당 월로 조회" 규칙 적용

**필요한 변경**: query_schedule에 `month` 파라미터 추가 (현재 session context 고정)
```python
"month": {"type": "integer", "description": "조회할 월 (생략 시 현재 월)"}
```

### 3. "야간근무 원티드만 따로 확인할 수 있어?"
**처리**: ✅ 1-2 turn
```
Turn 1: LLM → query_schedule(scope="wanted_submissions", shift_name="나이트")
```
- skill 내부: resolve_shift("나이트") → shift_gb='나이트'인 shift_id들
- NurseShiftRequest에서 해당 shift로 필터
- year/month 모호하면 domain knowledge 규칙에 따라 clarify

### 4. "지난주에 등록된 원티드 중 취소된 건 몇 건이야?"
**처리**: ⚠️ 2-3 turn (clarification 필요)
```
Turn 1: LLM은 domain knowledge에서 "취소"의 모호성을 인지
        → "취소가 어떤 의미인가요? 
           1) 간호사가 직접 철회한 원티드
           2) 수간호사가 조정판에서 미반영(is_applied=False)한 건"

Turn 2: (사용자 응답 후)
        → query_schedule(scope="wanted_submissions", date_range="지난주")
        또는
        → query_schedule(scope="wanted_adjustment", date_range="지난주")
          + is_applied=False 필터

Turn 3: LLM이 개수 집계하여 답변
```

**domain knowledge가 핵심**: "취소된 건"의 모호성과 clarification 시점을 LLM이 알아야 함.

**필요한 변경**: 
- query_schedule에 `date_range` 파라미터 실제 구현 (지난주 → 날짜 범위 변환)
- wanted_adjustment scope에 is_applied 필터 추가

### 5. "이번 달 원티드 신청 많은 순으로 직원별 집계해줘"
**처리**: ✅ 1-2 turn
```
Turn 1: LLM → query_schedule(scope="wanted_submissions")
Turn 2: LLM이 결과 데이터를 직접 집계/정렬하여 마크다운 표로 답변
```
- LLM은 JSON 결과에서 nurse_id별 count → sort 가능
- 데이터가 많으면 skill에서 aggregation operation 추가 고려

**대안**: analyze_report(scope="wanted", analysis_type="distribution") 으로도 가능

### 6. "3월 2주차 D근무 원티드 누가 올렸는지 보여줘"
**처리**: ✅ 1 turn
```
LLM → query_schedule(scope="wanted_submissions", shift_name="D", date_range="3월 2주차")
```
- skill 내부: "3월 2주차" → 3월 8일~14일로 변환 (resolve_date_range 필요)
- shift_name="D" → resolve_shift로 shift_id 확인
- 3월 → month=3 파라미터 사용

**필요한 변경**:
- `resolve_date_range()` 함수 추가 ("N주차", "지난주" 등 범위 변환)
- skill에 month override 파라미터

### 7. "내가 올린 5월 3일 원티드 취소해줘"
**처리**: ✅ 2 turn (approval)
```
Turn 1: LLM → bulk_mutation(scope="wanted_submissions", action="cancel", 
                            nurse_name="나"(=session의 현재 사용자), date="5월 3일",
                            preview_only=true)
        → {preview: "5월 3일 D 원티드 취소 예정"}

Turn 2: 사용자 확인 → bulk_mutation(preview_only=false)
```
- "내가" = session context의 nurse_id
- domain knowledge: "내/제/나의" = 현재 로그인 사용자

**필요한 변경**: "나"/"내" 를 session context의 nurse_id로 매핑하는 규칙을 domain knowledge에 추가

### 8. "원티드 마감일을 이번 주 금요일까지로 수정해줘"
**처리**: ⚠️ 2-3 turn
```
Turn 1: LLM → query_schedule(scope="wanted_submissions", operation="count") 
        → 현재 wanted 캠페인 상태 확인 (status, exp_date)
        
        여러 월의 wanted가 open이면:
        → "현재 4월과 5월 원티드가 열려있습니다. 어느 것의 마감일을 변경할까요?"

Turn 2: (확인 후) → update_wanted_deadline(exp_date="이번 주 금요일")
```

**❌ 현재 설계에 없는 skill**: wanted 캠페인 메타데이터(exp_date) 수정 skill 필요
**해결**: `update_constraint` skill을 확장하거나 새 skill 추가

### 9. "김수현 간호사의 원티드 메모를 '개인사유'로 수정해줘"
**처리**: ✅ 1 turn (거부)
```
LLM은 domain knowledge의 권한 규칙을 확인:
  "일반 간호사는 다른 간호사의 데이터 수정 불가"

→ session context의 user_role이 HN이 아니면:
  "다른 간호사의 원티드 메모를 수정할 권한이 없습니다."

→ HN이면: bulk_mutation으로 처리 (preview → confirm)
```

**domain knowledge가 핵심**: 권한 규칙을 system prompt에서 알려줘야 LLM이 판단 가능

---

## 근무표 관련

### 10. "이번 달 9병동 전체 근무표 보여줘"
**처리**: ✅ 1 turn
```
LLM → query_schedule(scope="schedule")
```
- skill 내부: resolve_target_schedule() → IssuedRoster(마감) 우선, 없으면 최신 version
- domain knowledge: "마감 근무표 우선, 없으면 최종 근무표" 규칙 내장

### 11. "김민지 간호사의 다음 주 근무만 조회해줘"
**처리**: ✅ 1 turn
```
LLM → query_schedule(scope="schedule", nurse_name="김민지", date_range="다음 주")
```
- resolve_nurse + resolve_date_range 내부 처리

### 12. "4월 3주차 야간근무자 명단 알려줘"
**처리**: ⚠️ 1-2 turn
```
Turn 1: LLM은 domain knowledge에서 "야간 근무자 vs 야간 전담" 구분 인지
        → "해당 기간에 야간 시프트가 배정된 간호사인가요, 
           야간 전담(is_night_nurse) 간호사인가요?"

Turn 2: (확인 후) 
        → query_schedule(scope="schedule", shift_name="나이트", date_range="4월 3주차")
```
- skill 내부: shift_name="나이트" → Shift 테이블에서 shift_gb='나이트'인 코드 수집 → 필터
- domain knowledge에 clarification trigger 등록됨

### 13. "이번 달 D/E/N 근무 인원 분포를 날짜별로 보여줘"
**처리**: ✅ 1 turn
```
LLM → analyze_report(scope="schedule", analysis_type="headcount")
```
또는
```
LLM → query_schedule(scope="schedule")
→ LLM이 결과에서 날짜별 D/E/N 카운트하여 표로 답변
```

### 14. "신규 간호사만 따로 모아서 이번 달 근무표 보여줘"
**처리**: ⚠️ 2-3 turn (clarification + multi-step)
```
Turn 1: domain knowledge에서 "신규 간호사" 모호성 인지
        → "신규 간호사의 기준이 무엇인가요? 
           1) 이번 달 입사
           2) 경력 1년 미만
           3) 기타"

Turn 2: (사용자: "이번 달 입사")
        → query_schedule(scope="nurse_info")  [간호사 목록 + joining_date]
        
Turn 3: → query_schedule(scope="schedule")  [근무표]
        LLM이 joining_date 기준 필터링하여 해당 간호사들만 표시
```

**개선안**: query_schedule에 `nurse_filter` 파라미터 추가 (joining_date, grade, experience 등)
```python
"nurse_filter": {
    "type": "object",
    "properties": {
        "min_joining_date": {"type": "string"},
        "max_experience": {"type": "integer"},
        "grade": {"type": "integer"},
    }
}
```

### 15. "이번 주말 근무자 중 Grade 1이 누구인지 알려줘"
**처리**: ✅ 1-2 turn
```
Turn 1: LLM → query_schedule(scope="schedule", date_range="이번 주말")
        → 주말 근무자 + nurse_id 리스트
        
        (nurse 정보에 grade가 포함되어 있으면 1 turn)
        (없으면)
Turn 2: LLM → query_schedule(scope="nurse_info")
        → grade 정보 확보 → 교차 필터링
```

**개선안**: schedule 조회 결과에 nurse 기본 정보(name, grade, team) 포함시키면 1 turn으로 가능.
현재 schedule_tools.get_schedule_entries가 nurse 정보를 조인하는지 확인 필요.

### 16. "9A 병동에서 연속 3일 이상 야간근무인 사람 찾아줘"
**처리**: ⚠️ 2-3 turn
```
Turn 1: LLM → "몇 월 근무표를 확인할까요?" (domain knowledge: year/month clarify)
Turn 2: → "마감 근무표, 최신 작업 버전 중 어느 것을 확인할까요?"
Turn 3: → validate_schedule() 
        또는 query_schedule(scope="schedule", shift_name="나이트") 
        → LLM이 연속일 분석
```

**고려사항**: 연속 N일 분석은 LLM이 JSON 데이터에서 직접 할 수 있지만, 
데이터가 많으면 validate_schedule에 해당 검증 로직을 위임하는 게 정확.

### 17. "현재 근무표에서 OFF가 가장 적은 사람 순으로 보여줘"
**처리**: ✅ 1-2 turn
```
Turn 1: LLM → query_schedule(scope="schedule")  [전체 근무표]
Turn 2: LLM이 nurse별 OFF 카운트 → 오름차순 정렬 → 표로 답변
```
또는
```
Turn 1: LLM → analyze_report(scope="schedule", analysis_type="fairness", shift_name="OFF")
```

### 18. "4월 12일 김민지 간호사를 D에서 E로 바꿔줘"
**처리**: ✅ 3 turn (verify + preview + confirm)
```
Turn 1: LLM → query_schedule(scope="schedule", nurse_name="김민지", date="4월 12일")
        → {shift_id: "N", ...}  (현재 N이지 D가 아님!)

Turn 2: domain knowledge 규칙에 따라:
        → "김민지 간호사의 4/12 근무는 현재 D가 아니라 N입니다. N→E로 변경할까요?"

Turn 3: (사용자 확인)
        → bulk_mutation(scope="schedule", action="change_shift", 
                       nurse_name="김민지", date="4월 12일", 
                       new_shift_name="E", preview_only=false)
```

**domain knowledge가 핵심**: "시프트 변경 시 원래 값이 예상과 다르면 확인" 규칙.

### 19. "박지은 간호사의 4월 20일 OFF를 연차로 수정해줘"
**처리**: ⚠️ 2-3 turn (clarification)
```
Turn 1: domain knowledge에서 "근무표 vs 원티드 조정판" 구분 인지
        → "근무표에서 직접 수정할까요, 원티드 조정판에서 수정할까요?"

Turn 2: (사용자: "근무표에서")
        → bulk_mutation(scope="schedule", action="change_shift",
                       nurse_name="박지은", date="4월 20일",
                       new_shift_name="연차", preview_only=true)
        → {preview: "OFF → V(연차) 변경 예정"}

Turn 3: 사용자 확인 → 실행
```

### 20. "9병동 전체에서 야간근무 편차가 크지 않게 근무표 다시 조정해줘"
**처리**: ✅ 2-3 turn
```
Turn 1: LLM → analyze_report(scope="schedule", analysis_type="fairness", shift_name="나이트")
        → {variance: 3.2, min: 4, max: 8, ...}

Turn 2: LLM → repair_schedule()  
        또는 "야간 편차를 줄여서 재생성할까요?" → generate_schedule

Turn 3: 사용자 확인 → 실행
```

### 21. "신규 1명이 항상 프리셉터와 같은 조에 들어가도록 수정해줘"
**처리**: ⚠️ 3+ turn (복합)
```
Turn 1: "신규 간호사 기준 clarify" (이번달 입사? 프리셉티 지정된 사람?)
Turn 2: query_schedule(scope="nurse_info") → preceptor_id 확인
Turn 3: update_constraint(field="preceptor_gauge", value=...) 
        또는 domain-specific처리
```

**한계**: 이 쿼리는 현재 스킬 체계로는 완전 자동화 어려움. 
preceptor 관련 제약조건 수정은 update_constraint로 가능하지만, 
"같은 조" 개념은 팀 배정 + 시프트 동기화가 필요. → **추후 기능 확장 시 대응**

### 22. "각 팀에서 D/E/N이 최소 1명씩 나오도록 근무표 다시 맞춰줘"
**처리**: ⚠️ 사용자 언급대로 추후 기능 변경 예정
```
→ "이 기능은 현재 팀 밸런스 설정으로 부분적으로 지원됩니다.
    team_balance_enable=True, team_balance_mode 설정을 조정할까요?
    단, 팀당 최소 인원 보장은 추후 업데이트 예정입니다."
```

### 23. "Grade 1이 없는 날짜만 찾아서 배치 수정안 추천해줘"
**처리**: ✅ 2-3 turn
```
Turn 1: LLM → validate_schedule()  
        → {violations: [{type: "grade_coverage", dates: ["4/5", "4/12"], ...}]}

Turn 2: LLM → recommend_candidates(date="4월 5일", shift_name=...)
        → {candidates: [{name: "김영희", grade: 1, ...}]}

Turn 3: LLM이 날짜별 추천 결과를 종합하여 답변
```

---

## 종합 분석

### ✅ 현재 설계로 처리 가능 (16/23)
쿼리 1, 2, 3, 5, 6, 7, 9, 10, 11, 13, 15, 17, 18, 19, 20, 23

### ⚠️ Domain Knowledge 추가로 해결 (4/23)  
쿼리 4 (취소 의미), 12 (야간근무자 vs 전담), 14 (신규 기준), 16 (연속일 분석)
→ DOMAIN_KNOWLEDGE.md에 이미 반영됨

### ❌ Skill 확장 필요 (2/23)
쿼리 8 (원티드 마감일 수정 → 새 skill 또는 기존 확장)
쿼리 14 (nurse_filter 파라미터 → 기존 skill 확장)

### 🔮 추후 기능 확장 필요 (1/23)
쿼리 21 (프리셉터 동일 조), 22 (팀 최소 인원)

### 필요한 설계 변경 사항

| 변경 | 내용 | 중요도 |
|---|---|---|
| month 파라미터 추가 | query_schedule에 month override | 높음 |
| resolve_date_range() | "N주차", "지난주", "이번 주말" 등 | 높음 |
| nurse_filter 파라미터 | joining_date, grade, experience 필터 | 중간 |
| schedule 결과에 nurse 정보 포함 | grade, team 조인 | 중간 |
| wanted 메타 수정 skill | exp_date 등 캠페인 설정 변경 | 낮음 (드문 케이스) |
| "나/내" → 현재 사용자 매핑 | domain knowledge에 규칙 추가 | 높음 |
