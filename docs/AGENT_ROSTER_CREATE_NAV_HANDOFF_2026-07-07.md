# 에이전트 navigate/invoke — roster_create 워크스페이스 핸드오프 (2026-07-07)

프론트 '근무표 만들기'(`/roster_create`)가 모달 중심 워크스페이스로 개편됨에 따라, 에이전트의
client-action 계약을 확장했다. **백엔드 작업은 완료**(아래 §A), **프론트는 §B의 배선만 구현하면 된다.**

관련: [ADR 0003](adr/0003-agent-orchestration-contracts.md) 흐름, 기존 스펙
[AGENT_CLIENT_ACTION_TOOL_SPEC_2026-05-28.md](AGENT_CLIENT_ACTION_TOOL_SPEC_2026-05-28.md),
switch_ward 핸드오프 패턴.

## 설계 원칙 (변경 없음)

- 백엔드는 **논리적 이름**(target/sub/command)만 emit. route/컴포넌트 문자열은 프론트가 SSOT로 소유.
- target/sub/command 는 **closed enum**. enum 밖이면 ui_action 없이 텍스트 안내(백엔드가 차단).
- role-aware: `roster_create`·`excel_download` 는 HN/ADM 전용(백엔드 권한 게이트가 이미 강제).
- SSOT 브릿지: 백엔드 `NAVIGATE_TARGETS` → `scripts/dump_client_action_targets.py` →
  프론트 `src/service/clientActionTargets.generated.json` 자동 생성(이미 반영됨).

## 결정 (2026-07-07)

1. **navigate 세밀도 = 모달 단위.** "인력 설정 열어줘"면 roster_create 화면 이동이 아니라
   **ModalManpower를 바로 연다.**
2. **액션 범위 = navigate + 안전 invoke(엑셀)만.** 발행/저장/삭제/빈근무표생성 같은 **파괴적
   액션은 에이전트가 트리거하지 않는다** — 화면만 열고(sub 없이) 사용자가 UI 버튼을 누른다.
   유일한 예외가 비파괴 `invoke(excel_download)`.

---

## §A. 백엔드가 emit 하는 ui_action (완료)

### A-1. navigate — roster_create 모달 서브 (신규 7개)

```json
{ "action": "navigate", "target": "roster_create", "sub": "<모달>" }
```

| sub | 의미 | 프론트 모달(참고) | 트리거 예시 발화 |
|---|---|---|---|
| `manpower` | 필요인원/인력 설정 | ModalManpower | "인력 설정 열어줘", "필요인원 바꾸려고" |
| `wanted_config` | 원티드 반영 설정 | ModalWantedConfig | "원티드 설정 열어줘" |
| `deadline` | 원티드 마감일 설정 | SetDeadLine | "마감일 설정할래" |
| `off_request` | 오프 요청 목록 | ModalOffRequestList | "오프 요청 보여줘" |
| `quick_config` | 생성 옵션(퀵) 설정 | RosterCreateConfigModal | "생성 옵션 설정" |
| `emergency` | 긴급 대체 찾기 | ModalEmergencyReplacement | "긴급 대체 찾아줘" (화면 오픈) |
| `version` | 버전 선택 패널 | 버전 선택 UI | "버전 바꿀래" |

- `sub` 없이 `{target:"roster_create"}` 만 오면 = 워크스페이스 화면만 진입(모달 안 엶).
- 파괴적 액션 발화("발행해줘" 등)는 백엔드가 sub 없이 roster_create 로 보내고 "화면에서
  직접 눌러 주세요"라고 안내하도록 프롬프트됨.

### A-2. invoke — 비파괴 UI 명령 (신규)

```json
{ "action": "invoke", "command": "excel_download", "params": { ... optional } }
```

| command | 의미 | 대응 프론트 동작(참고) |
|---|---|---|
| `excel_download` | 현재 표시 중인 근무표를 엑셀로 내보내기 | `scheduleDownload_excel` (ButtonGroup) |

- closed enum. 현재 `excel_download` 하나. 파괴적 명령(publish/save/delete)은 **등록 안 됨**.
- `params` 는 선택(없으면 현재 워크스페이스 상태 대상). 내부 id 금지, 이름 기반.

---

## §B. 프론트가 구현할 것 (TODO)

기존 client-action 디스패처(navigate/prefill/switch_ward 처리하던 곳)에 아래 두 갈래만 추가.

### B-1. navigate + target=roster_create + sub → 모달 오픈

`/roster_create` 진입 후, `sub` 값에 따라 해당 모달의 open state 를 켠다. 현재 각 모달은
`Roster_create.tsx` 의 로컬 useState 로 열리므로, **sub → setter 매핑 핸들러**가 필요하다.

```ts
// 예시 (실제 setter 명은 프론트 SSOT)
const SUB_TO_MODAL: Record<string, () => void> = {
  manpower:      () => openManpowerModal(),
  wanted_config: () => openWantedConfigModal(),
  deadline:      () => openDeadlineModal(),
  off_request:   () => openOffRequestModal(),
  quick_config:  () => openQuickConfigModal(),
  emergency:     () => openEmergencyModal(),
  version:       () => openVersionPanel(),
};
// ui_action 수신 시: navigate(/roster_create) → 마운트 후 SUB_TO_MODAL[sub]?.()
```

- 이미 `/roster_create` 에 있으면 route 이동 없이 모달만 오픈.
- 다른 화면에서 왔으면 route 이동 완료(모달 대상 상태 준비) 후 오픈. (마운트 타이밍 주의 —
  쿼리파라미터나 초기 open-intent prop 방식 권장.)
- 매핑에 없는 sub 는 무시하고 화면만 진입(백엔드가 이미 enum 검증하므로 도달 드묾).

### B-2. invoke + command → 버튼 동작 실행

```ts
const COMMAND_TO_HANDLER: Record<string, () => void> = {
  excel_download: () => scheduleDownload_excel(), // 현재 버전 대상
};
// ui_action 수신 시: COMMAND_TO_HANDLER[command]?.()
```

- `excel_download` 는 현재 표시 중인 근무표/버전을 대상으로 한다(사용자가 방금 보던 것).
- 표시 중인 버전이 없으면 프론트에서 안내(백엔드는 상태를 모름).

## §C. 하지 않는 것 (스코프 밖)

- 발행/마감철회(publish/unpublish), 저장, 삭제, 빈 근무표 생성, 복사 → **에이전트 트리거 없음.**
  발화가 와도 백엔드는 roster_create 화면만 열고 사용자 클릭을 안내. (안전성 — 이미 UI 확인모달 존재.)
- `config`(=/roster_configure, 근무코드 설정) target 은 변경 없음. roster_create 와 별개 화면 유지.

## §D. 백엔드 변경 파일 (참고)

- `app/agents_v2/skills/client_actions.py` — NAVIGATE_TARGETS[roster_create].subs 7개, INVOKE_COMMANDS, build_ui_action(invoke), command_permission_error
- `app/agents_v2/middleware.py` — invoke 권한 분기
- `app/agents_v2/skills/descriptions.py` — navigate sub enum + invoke tool schema
- `app/agents_v2/router.py` — invoke 카테고리 배선(navigation/read/generate)
- `app/agents_v2/contract/client_action_targets.json` + 프론트 generated.json — dump 재생성
- 테스트: `tests/agent_qa/test_client_action_navigate.py`(+12), `test_router.py` 기대값 갱신
