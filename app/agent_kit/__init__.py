"""공통 에이전트 틀 — 화면 입력부터 응답까지 서버가 모든 짐을 진다(사용자 지시 11-02 · 확장 대비).

  · `blocks`       화면 어휘(블록·버튼·효과) — 화면은 이것만 그린다
  · `conversation` 대화 보관(메모리 + 에이전트 기록에서 복원)·잠금·한도
  · `base`         에이전트가 채울 것(authorize·intro·handle·restore·transcript)과 등록부
  · `runtime`      한 턴 실행 순서(끝난 대화 이어 받기·대상 확인·흔적·대상 이동)
  · `router`       `/api/agents/{agent_id}/…` 경로 하나
첫 에이전트: 근무표 수정 도우미(`schedule_edit_agent`). AIDE 는 아직 따로(`/api/agent/chat`) — 같은 블록·효과
어휘로 옮겨 탈 수 있게 해 두었다(`navigate`·`prefill` 은 효과 종류).
"""
