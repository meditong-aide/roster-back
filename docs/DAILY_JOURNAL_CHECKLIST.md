# Daily Journal Checklist (Notion)

Use this checklist when writing or updating today's page under `개발일지`.

## 1) Find or create today's page
- Confirm parent page is `개발일지`:
  - `https://www.notion.so/311879e089a581e5bfc2e3cfadb7d824`
- Check whether today's child page already exists.
- If not found, create a new child page for today.
- If found, append/update that same page.

## 2) Gather source material from commits
- Review commits made today with branch context.
- Focus on what was delivered, not commit hashes.
- Group changes by feature or improvement theme.

## 3) Write the journal with plain bullet points
- Keep entries short and easy to scan.
- Write in non-technical language so non-developers can understand quickly.
- Include these three sections:
  - `오늘 진행한 기능`
  - `세부 작업`
  - `기대 효과`

## 4) Quality check before saving
- No commit hashes included.
- No deep implementation detail unless business behavior changed.
- Each bullet clearly explains value in practical terms.
- The page reflects only today's work.

## Quick example
- `오늘 진행한 기능`
  - 간호사 스케줄 확정 전 검증 흐름을 정리해 오류 가능성을 줄였음
- `세부 작업`
  - 입력값 확인 조건을 보강하고, 실패 시 안내 문구를 더 이해하기 쉽게 수정함
  - 권한 확인 순서를 정리해 잘못된 접근이 먼저 차단되도록 개선함
- `기대 효과`
  - 스케줄 등록 과정에서 실수와 재작업이 줄고, 운영자가 원인을 더 빨리 파악할 수 있음
