# main 배포 가이드 — roster (2026-08 dev 누적분)

> 대상: `roster-back` · `roster_front` 의 `dev` → `main`
> 규모: 백엔드 **122 커밋**. 이번 세션분만이 아니라 **dev 에 쌓인 전량**이 나간다.
> 실행: 배포는 사용자가 한다. 이 문서는 순서·확인 항목·되돌리기만 적는다.

---

## 0. 한 장 요약

| 항목 | 상태 |
|---|---|
| **DDL** | ✅ **추가 실행 없음** — 신규 테이블 4종·컬럼 8개가 운영에 이미 있다(§2) |
| **데이터 백필** | ⚠️ **`nurse_night_cycle` 227행 미실행** — 없어도 화면은 뜨지만 값이 부정확하다(§3) |
| **배포 순서** | 백엔드 → 프론트. 둘 다 `main` push 로 자동 |
| **처음 도는 것** | 빌드 게이트 · 배포 헬스 게이트 · CloudWatch 로그 · API 키 `.env` 이관(§4) |
| **되돌리기** | `git revert` 후 재푸시. DDL 이 없어 스키마 롤백은 불필요 |

---

## 1. 무엇이 나가는가

이번 세션에서 만든 것 넷이 포함된다.

- **배포 파이프라인 4중 방어** — mcp 2.0 크래시 루프 차단 · 빌드 게이트 · 로그 드라이버 복구 · 시크릿 분리
- **기피 확정 게이트** — 간호사 기피는 수간호사가 조정판에서 확정해야 생성에 반영
- **선호↔기피 상충 해소 + 발화별 원티드 스냅샷**
- **수면OFF 주기 조회·재계산 EP** + 미개설 월 전월 폴백

★ 나머지 118 커밋(shadow 배선 · ontology/graph · core-guided 등)은 각 작업 시점의 검증에 의존한다.
이 문서가 보증하는 범위는 위 넷이다.

---

## 2. DDL — 추가 실행 없음 (확인 완료)

`origin/main...origin/dev` 의 `models.py` 변경 전량을 운영 스키마와 대조했다.

**신규 테이블 4종 — 전부 존재**

```
banned_wanted_entries (13컬럼) · nurse_leave_period (11) ·
nurse_night_cycle (11) · wanted_monthly_memo (6)
```

**기존 테이블 추가 컬럼 8개 — 전부 존재**

```
shifts.health_leave_target · shifts.sleep_off_target
roster_config.health_leave_enabled · health_leave_weekend
roster_config.sleep_off_enabled · sleep_off_cycle
banned_wanted_entries.source        (varchar NOT NULL DEFAULT 'hn')
nurse_night_cycle.sleep_off_seq
```

확인 쿼리(재검증이 필요하면):

```sql
SELECT t.name AS tbl, c.name AS col
  FROM eun_roster.sys.columns c
  JOIN eun_roster.sys.tables t ON t.object_id = c.object_id
 WHERE c.name IN ('health_leave_target','sleep_off_target','health_leave_enabled',
                  'health_leave_weekend','sleep_off_enabled','sleep_off_cycle',
                  'sleep_off_seq','source');
```

---

## 3. 데이터 백필 — `nurse_night_cycle` 만 남았다

### 현재

운영 `nurse_night_cycle` **0행**. 테이블은 있고 데이터만 없다.
8월 확정본을 임포트할 때 알림을 막으려고 `publish` 라우터를 안 타고 ORM 으로 직접 넣어서,
`upsert_night_cycle_snapshot` 이 호출되지 않았다.

### 없어도 배포는 된다

미개설 월 폴백이 전월 확정본에서 즉석 계산한다. 51-RN 실측:

```
2026-08 → source=projected · 25/26명 값 있음
2026-09 → source=projected · 25/26명 값 있음
```

### ★ 다만 rebuild 로는 못 채운다

`schedule_entries.shift_id` 에는 `'N'` 만 저장되고 **N1~N15 연번은 엑셀에만 있다.**
`fetch_anchor` 는 앵커가 없으면 `(0,0,0)` 을 돌려주므로, rebuild 는 이전 누적을 모른 채
0부터 다시 센다. 실측(51-RN 8월 확정본):

```
대조 26명 · 일치 5 · 불일치 21
  정지인  실제 12 ≠ 계산  4
  김효정  실제  6 ≠ 계산 12
  김현지  실제  0 ≠ 계산  5
```

→ **시드 백필이 유일한 경로다.**

### 절차

```bash
# 227명 · 13개 병동(인천의료원 전 그룹) · 2026-08 스냅샷
cd roster-back
EUN_DB_NAME=eun_roster uv run python tools/leave_analysis/backfill_night_cycle.py
```

입력: `tools/leave_analysis/night_cycle_seed.json`
스크립트 끝에 `assert after == len(seed)` 검증이 들어 있다.

**실행 전에 알아야 할 것 셋**

- ★ **dry-run 이 없다.** 바로 commit 한다. 되돌리려면 해당 월 행을 지운다:
  `DELETE FROM eun_roster.dbo.nurse_night_cycle WHERE year=2026 AND month=8`
- `assert` 가 **테이블 전체 행수**를 본다(월별이 아니다). 지금 운영이 0행이라 227 로 맞지만,
  다른 달 앵커가 이미 있으면 실패한다. 재실행은 안전하다(UPDATE 로 떨어져 행수 불변).
- `sleep_off_seq` 는 채우지 않는다 — 시드의 `sleep` 은 부여 *날짜* 목록(`[29]`)이라
  이 컬럼에 넣을 값이 아니다. **배정 로직에는 영향이 없다**:
  `advance_cycle` 이 `prev_sleep_seq` 를 반환값에 누적만 하고 `seq`·`pending`·`sleep_count`
  계산에는 쓰지 않는다(보고용 누적 카운터).

### ★★ 순서 규칙

```
① 시드 백필  → 2026-08 앵커 227행
② 이후 달만  → 마감 시 자동 축적, 또는 POST /nurse-period/night-cycle/rebuild (2026-09~)
```

**`rebuild` 를 2026-08 이하로 돌리면 안 된다** — 방금 심은 정확한 앵커를
0부터 계산한 값으로 덮어쓴다. EP 에 아직 그 가드가 없다.

---

## 4. 이번에 처음 도는 것 넷

`6ff6f76`(배포 파이프라인 방어)이 main 빌드에서 처음 작동한다.

### ① 빌드 게이트

Dockerfile 이 `python -c "import app.main"` 을 돌린다. 실패하면 **빌드가 멈추고 ECR 에 안 올라간다.**
이게 붉게 나는 건 정상 동작이다 — 깨진 이미지가 배포되는 것보다 낫다.
(dev 사고: mcp 2.0 이 전이 의존으로 올라와 import 가 깨진 이미지가 그대로 배포됐고, 2619회 크래시했다)

### ② 배포 헬스 게이트

컨테이너 기동 후 `/health/basic` 을 60초 안에 확인한다. 200 이 안 나오면 job 실패.

### ③ CloudWatch 로그

`--log-driver awslogs` 가 붙어 `/ecs/aide-server-prod` 에 **처음 쌓이기 시작한다.**
지금까지 데몬 기본값(`none`)이라 통째로 버려지고 있었다.
★ 대신 `docker logs` 는 안 된다(awslogs 드라이버 특성).

### ④ ★ API 키가 이미지 → `.env` 로 이동

가장 위험한 항목이다. Dockerfile 의 `ENV` 굽기를 제거했으므로,
`.env` 에 키가 안 실리면 **앱은 정상으로 뜨는데 LLM 만 조용히 죽는다.**

확인할 시크릿명: `PROD_ENV_GOOGLE` · `PROD_ENV_ANTHROPIC` · `PROD_ENV_OPENAI`

---

## 5. 배포 순서

```
① roster-back  dev → main  push      (백엔드 EP 먼저)
② roster_front dev → main  push      (화면)
```

반대로 하면 프론트가 없는 EP 를 호출해 "수면OFF 주기 정보를 불러오지 못했습니다" 가 뜬다.
※ 프론트에 구 백엔드 폴백이 있어 데이터가 깨지지는 않는다.

| | 트리거 | 산출 |
|---|---|---|
| roster-back | `push: main` → `deploy-ecr.yml` | ECR `aide-backend-prod` → EC2 `i-0cd48e37710855967` 컨테이너 `aide-server-prod` |
| roster_front | `push: main` → `deploy-prod.yml` | S3 `s3-aide-frontend-prod-web` + CloudFront `E34UU9DNQGUUQQ` 무효화 |

---

## 6. 배포 후 확인

### 즉시

```bash
# ALB 타깃 헬스 (healthy 여야 한다)
aws elbv2 describe-target-health --region ap-northeast-2 \
  --target-group-arn $(aws elbv2 describe-target-groups --region ap-northeast-2 \
      --names aide-ec2-prod-tg --query 'TargetGroups[0].TargetGroupArn' --output text) \
  --query 'TargetHealthDescriptions[].{T:Target.Id,S:TargetHealth.State}' --output table
```

SSM 으로 컨테이너 상태(재시작 수가 0 이어야 한다):

```
docker inspect aide-server-prod --format "Restarts={{.RestartCount}} Status={{.State.Status}}"
docker inspect aide-server-prod --format "{{.HostConfig.LogConfig.Type}}"   # awslogs 여야 함
docker exec aide-server-prod sh -c "env | awk -F= '/API_KEY/ {print \$1, length(\$2)}'"
```

### 체크리스트

- [ ] ALB 타깃 **healthy**
- [ ] 컨테이너 `Restarts=0` · `Status=running`
- [ ] 로그 드라이버 `awslogs` · CloudWatch `/ecs/aide-server-prod` 에 유입
- [ ] 런타임 API 키 3종 **present** (길이로 대조)
- [ ] 이미지에 시크릿 **미포함** — `docker image inspect <img> --format '{{range .Config.Env}}{{println .}}{{end}}' | grep API_KEY` 가 비어야 한다
- [ ] LLM 기능(AIDE 챗) 동작
- [ ] 근무자 관리 → 간호사 → **수면OFF 주기 패널** 표시

★ ALB 헬스체크가 `Interval 300초 / Healthy 5회` 라 **healthy 전환에 최대 25분** 걸린다.
배포 게이트가 60초 안에 확인해주므로, 그 사이 502 가 보이면 이 대기 때문이다.

---

## 7. 배포 영향이 0 인 것 (확인 완료)

| 변경 | 근거 |
|---|---|
| 기피 확정 게이트 | 운영 `banned_wanted_entries` 의 `source='nurse'` **0건** — 갑자기 빠질 기피가 없다 |
| 수면OFF EP | 신설 2개 · 기존 경로 무수정(+182, 삭제 0) |
| `apply-all` EP | 신설. 프론트가 아직 안 부른다 |

---

## 8. 되돌리기

DDL 이 없으므로 **스키마 롤백이 불필요**하다. 코드만 되돌리면 된다.

```bash
git revert -m 1 <merge-commit>     # 또는 문제 커밋만
git push origin main               # 재배포 트리거
```

컨테이너만 급히 되돌리려면 이전 이미지 태그로 재기동한다(ECR `aide-backend-prod` 에
`prod-<git-sha>` 로 남아 있다).

★ 시드 백필(§3)을 이미 실행했다면 **되돌릴 필요가 없다** — 앵커는 코드와 무관한
데이터이고, 값이 틀렸다면 다시 심으면 된다.

---

## 9. 남은 항목 (배포와 무관 · 추적용)

- `leave_summary` 프론트 미배선 — 생성 응답에 보건휴가·수면OFF 부여/이월 건수가 실리는데 화면에 안 뜬다
- 원티드 관리보드 프론트 미배선 6건 — "원티드 전체 미반영" 이 기피를 안 끄는 것 등
- `rebuild` EP 에 시드 월 보호 가드 없음(§3)
- `tools/leave_analysis/*.json` gitignore 미적용 — 실명·사번 448명분
