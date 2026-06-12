# dev queries

Agent 개발 방향 레퍼런스. `/api/agent/chat/send` 를 통해 들어온 사용자 발화를
router 9 카테고리별 markdown 파일로 append-only 로 누적한다.

- 누적 위치: `app/agents_v2/harness/dev_queries/<category>.md`
- 캡처 시점: agent_v3 의 라우팅 직후 (`agent_v3.py` `log_dev_query` 호출)
- 카테고리: `navigation`, `read`, `mutate`, `generate`, `validate_repair`,
  `analyze`, `recommend`, `settings_rules`, `settings_people`
  (분류 실패/저신뢰 → `unrouted.md`)
- multi-category 쿼리: 매칭된 모든 카테고리 파일에 기록 (cross-reference 용)
- 항목 포맷:
  ```
  ## <ISO timestamp> — conv `<short id>` [— also: <other cats>]

  > <user message>
  ```

## 활용

- 새 skill 설계 시 — 해당 카테고리 MD 를 읽어 실제 사용자가 어떤 표현을 쓰는지 파악
- description 갱신 시 — 자주 들어오는 변형을 prompt 예시에 반영
- 테스트 케이스 추가 — 실제 발화를 corpus 에 그대로 옮겨 회귀 보호

## Git 정책

각 MD 는 기본 `.gitignore` 됨 (개인 로그라 diff 노이즈 회피). 특정 카테고리를
팀 자산으로 공유하고 싶으면 `git add -f <category>.md` 로 강제 stage.
