# (기획 보류) 필요 인원 완화 해결 옵션 — demand_override

작성: 2026-07-22 · 상태: **기획만, 개발 보류**(필요 시 착수)

## 배경
CAPACITY_TOTAL_SHORTAGE(월 총 근무 가능일 < 총 수요) 해결책은 세 가지:
1. 간호사 추가(수동) 2. **연속근무 등 하드 설정 완화**(이미 auto 버튼) 3. **일자별 필요 인원 낮추기**.
현재 3번은 텍스트 안내만 있고 auto 버튼이 없다.

## 왜 config_override로는 안 되나
수요는 `config_dict`가 아니라 **`shift_manage_data`(DailyShift에서 뽑은 별도 구조)**로 솔버에
주입된다(`roster_create_service._run_cp_sat_basic:2698` "ShiftManage 요구인원은 호출부에서
주입한다"). 따라서 config_override에 `daily_shift_requirements_by_day`를 넣어도 솔버 수요는
안 바뀐다. → config_override와 **대칭인 별도 override 경로**가 필요.

## 설계안 (구현 시)
- resolution_option.apply 에 `demand_override`(날짜×근무 델타 또는 목표 배열)를 담는다.
- `demand_override` 파라미터를 generate → `_run_cp_sat_basic` → 수요 주입부까지 배선.
- 감축 분포는 precheck evidence 의 `shortage`(부족량) + `bottleneck_days` 로 계산.
- 재계산으로 검증(verified) → 연속근무 레버처럼 원클릭.

## 두 가지 적용 방식
- **1회성(transient)**: 이번 생성에만, DailyShift 원본 미변경(config_override 철학과 동일). 안전.
- **영구(persist)**: 그 달의 DailyShift 레코드를 실제로 낮춤. "운영 결정이 데이터에 반영"되어
  일관적이지만 **미리보기·원복(스냅샷)·감사 로그**가 반드시 동반돼야 안전.

## 보류 이유
- 이 병동 수요는 **균일**(매일 8/8/8, demand_uniform=true)이라 "총 N명분을 어느 날/근무에서
  뺄지"가 자연스러운 정답이 없다 → 자동 분포는 임의적. 미리보기/사용자 확정 UX가 커진다.
- 현재는 **"필요 인원 화면으로 이동 + 목표 부족량 안내"**(manual_navigate)만으로 충분.
  auto 적용은 수요가 균일하지 않거나 사용자 요구가 명확해지면 착수.

## 착수 시 체크리스트
- [ ] `demand_override` 배선(generate→engine, 1회성 우선)
- [ ] 감축 분포 계산기(shortage + bottleneck)
- [ ] (영구 시) DailyShift 스냅샷·원복·감사 로그
- [ ] resolution 옵션 + 프론트 apply 경로(기존 config_override/treatment_ids 와 동일 패턴)
- [ ] 카드에 델타 미리보기 표시
