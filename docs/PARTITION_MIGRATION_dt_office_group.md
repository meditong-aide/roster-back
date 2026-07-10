# call_history / ui_events 파티션 재설계 — dt / office_id / group_id (3단)

작성 2026-07-10. 대상: Firehose 4개 스트림 + Athena(Glue) 4개 테이블.
현재 상태는 AWS 실조회로 확인함. **AWS 변경 명령은 사용자가 실행**(Claude는 설계·명령 제공).

---

## 1. 현재 상태 (실조회)

| 스트림 | S3 prefix(현재) | 동적파티셔닝 |
|---|---|---|
| roster-call-history | `call_history/dt=!{timestamp:yyyy-MM-dd}/` | OFF |
| roster-call-history-dev | `call_history_dev/dt=!{timestamp:yyyy-MM-dd}/` | OFF |
| roster-ui-events | `ui_events/dt=!{timestamp:yyyy-MM-dd}/` | OFF |
| roster-ui-events-dev | `ui_events_dev/dt=!{timestamp:yyyy-MM-dd}/` | OFF |

- 버킷(4개 공용): `roster-call-history-702166530338`
- 역할: `arn:aws:iam::702166530338:role/firehose-roster-call-history-role`
- 버퍼: 5MB / 60s. region ap-northeast-2. Account 702166530338.
- Athena DB `roster_analytics`, 테이블 `call_history`, `call_history_dev`, `ui_events`, `ui_events_dev` — 파티션키 `dt`(string) 1개, projection dt=date, JsonSerDe.

## 2. 목표
S3 물리 파티션을 **dt / office_id / group_id 3단**으로:
```
call_history/dt=2026-07-10/office_id=102576/group_id=1025764296a2/…
```
테넌트(병원·병동)별 파티션 프루닝 → 스캔량↓·비용↓·격리↑.

## 3. ★핵심 제약 — 스트림 재생성 필수
Firehose **동적 파티셔닝(Dynamic Partitioning)은 스트림 생성 시에만 활성화 가능**하다(기존 스트림 수정 불가).
→ 4개 스트림을 **재생성**해야 한다. 로거는 fire-and-forget(전송 실패=로그만·요청 무영향)이라 재생성 중 짧은 로그유실은 사용자 영향 없음.

레코드에서 파티션 키를 뽑기 위해 **MetadataExtraction 프로세서(JQ)** 로 office_id/group_id 추출(누락 시 `none`).
앱 레코드는 이미 각 줄 끝에 `\n` 을 붙이므로 **AppendDelimiterToRecord 프로세서는 넣지 않는다**(이중 개행 방지).
office_id/group_id 는 백엔드=JWT, 프론트 ui_events=쿠키에서 채워짐(비로그인/미상 → `none`).

## 4. Firehose 재생성 설정 (call_history 예시)

`create-delivery-stream` 입력 JSON (`stream-call-history.json`):
```json
{
  "DeliveryStreamName": "roster-call-history",
  "DeliveryStreamType": "DirectPut",
  "ExtendedS3DestinationConfiguration": {
    "RoleARN": "arn:aws:iam::702166530338:role/firehose-roster-call-history-role",
    "BucketARN": "arn:aws:s3:::roster-call-history-702166530338",
    "Prefix": "call_history/dt=!{timestamp:yyyy-MM-dd}/office_id=!{partitionKeyFromQuery:office_id}/group_id=!{partitionKeyFromQuery:group_id}/",
    "ErrorOutputPrefix": "errors/call_history/!{firehose:error-output-type}/dt=!{timestamp:yyyy-MM-dd}/",
    "BufferingHints": { "SizeInMBs": 64, "IntervalInSeconds": 60 },
    "CompressionFormat": "GZIP",
    "DynamicPartitioningConfiguration": {
      "Enabled": true,
      "RetryOptions": { "DurationInSeconds": 300 }
    },
    "ProcessingConfiguration": {
      "Enabled": true,
      "Processors": [
        {
          "Type": "MetadataExtraction",
          "Parameters": [
            { "ParameterName": "MetadataExtractionQuery",
              "ParameterValue": "{office_id:(.office_id // \"none\"),group_id:(.group_id // \"none\")}" },
            { "ParameterName": "JsonParsingEngine", "ParameterValue": "JQ-1.6" }
          ]
        }
      ]
    }
  }
}
```
> ★DynamicPartitioning 은 버퍼 `SizeInMBs >= 64` 필요(5MB 불가). 지연이 걱정되면 IntervalInSeconds 를 낮게(최소 60).

4개 스트림 = 이 JSON을 스트림명·base prefix 만 바꿔 적용:

| 스트림명 | Prefix base |
|---|---|
| roster-call-history | `call_history/` |
| roster-call-history-dev | `call_history_dev/` |
| roster-ui-events | `ui_events/` |
| roster-ui-events-dev | `ui_events_dev/` |

## 5. 컷오버 플랜

### 옵션 A — 무손실(권장): 신규 이름 + env 플립
1. 신규 스트림 4개 생성(예: `roster-call-history-p3` …) — 위 설정.
2. EC2 `.env` + `.github/workflows/deploy-ecr.yml` 의 `CALL_HISTORY_FIREHOSE_STREAM` / `UI_EVENTS_FIREHOSE_STREAM` 을 신규 이름으로 교체 → 재배포.
3. 신규 경로에 데이터 적재 확인.
4. 구 스트림 4개 삭제.
- 장점: 로그유실 0. 단점: env/워크플로 수정.

### 옵션 B — 동일 이름 재생성(간단)
1. `aws firehose delete-delivery-stream --delivery-stream-name roster-call-history` (4개)
2. 위 JSON으로 `create-delivery-stream` (4개, 동일 이름)
3. 앱 env 무변경(스트림명 동일).
- 장점: 앱/워크플로 무변경. 단점: 삭제~생성 사이 짧은 로그유실(fire-and-forget이라 사용자 무영향).

## 6. Athena(Glue) 재정의

> ★최종 적용(2026-07-10 실행 결과): 처음엔 아래 injected projection(3키)으로 만들었으나, "조회 시 `office_id`·`group_id` 등호 WHERE 필수"(`CONSTRAINT_VIOLATION`) 제약이 커서 **office_id/group_id 를 파티션에서 데이터 컬럼으로 전환**했다. 최종 파티션은 **`dt` 하나(projection date)** 뿐이고 office_id/group_id 는 일반 컬럼(레코드 JSON에서 읽음). S3 3키 물리 레이아웃(`dt=…/office_id=…/group_id=…/`)은 그대로이며 Athena 가 `dt` 하위 중첩경로를 재귀로 읽는다. 이러면 WHERE 없이 전체·크로스테넌트 조회가 가능하고(테넌트 필터는 선택), 재생성 전 옛 flat 데이터도 조회된다. 비로그인 레코드는 office_id 컬럼이 NULL(경로는 `none`). 실제 적용 DDL 은 `scratchpad/athena_convert.sh`, 조회뷰(v_*)는 `athena_views.sh`. 아래 injected DDL 은 엄격 테넌트 스코프가 필요할 때의 대안으로 남겨둔다.

기존 테이블은 파티션키가 `dt` 1개뿐이라 **DROP + CREATE**(7월 신설·데이터 극소). S3 데이터는 유지됨.
office_id/group_id 는 **파티션키로 승격**(데이터컬럼에서 제거·JSON엔 남지만 serde가 무시). 백엔드 신규 컬럼 `page/section/action/summary` 추가.

### call_history (prod 예시)
```sql
DROP TABLE IF EXISTS roster_analytics.call_history;
CREATE EXTERNAL TABLE roster_analytics.call_history (
  ts string, method string, path string, query string,
  status int, dur_ms int,
  account_id string, nurse_id string, name string, role string,
  ip string, ua string, req_id string,
  page string, section string, action string, summary string
)
PARTITIONED BY (dt string, office_id string, group_id string)
ROW FORMAT SERDE 'org.openx.data.jsonserde.JsonSerDe'
LOCATION 's3://roster-call-history-702166530338/call_history/'
TBLPROPERTIES (
  'projection.enabled'='true',
  'projection.dt.type'='date',
  'projection.dt.range'='2026-07-01,NOW',
  'projection.dt.format'='yyyy-MM-dd',
  'projection.dt.interval'='1',
  'projection.dt.interval.unit'='DAYS',
  'projection.office_id.type'='injected',
  'projection.group_id.type'='injected',
  'storage.location.template'='s3://roster-call-history-702166530338/call_history/dt=${dt}/office_id=${office_id}/group_id=${group_id}/'
);
```
- `office_id`/`group_id` = **injected** 타입 → 쿼리에 반드시 `WHERE office_id='…' AND group_id='…'` 필요(테넌트 스코프 분석엔 정상·오히려 안전). 크로스테넌트 자유쿼리가 필요하면 injected 대신 **Glue 크롤러/파티션 등록**으로 대체.
- `changes`(값 배열)·`target`(대상 id)은 S3 JSON에 남아 있으나 컬럼 미선언 → 필요 시 `json_extract`. **`summary`(string) 하나로 "어느 화면·무슨 액션·무슨 값" 사람이 읽기 가능**(예: `근무자관리 · 간호사 정보 수정(nurse_id=445872) — 등급=2, 팀=3`).

### ui_events (prod 예시)
```sql
DROP TABLE IF EXISTS roster_analytics.ui_events;
CREATE EXTERNAL TABLE roster_analytics.ui_events (
  event string, page string, section string, session_id string,
  account_id string, nurse_id string, props string,
  ip string, ua string, ts string
)
PARTITIONED BY (dt string, office_id string, group_id string)
ROW FORMAT SERDE 'org.openx.data.jsonserde.JsonSerDe'
LOCATION 's3://roster-call-history-702166530338/ui_events/'
TBLPROPERTIES (
  'projection.enabled'='true',
  'projection.dt.type'='date','projection.dt.range'='2026-07-01,NOW',
  'projection.dt.format'='yyyy-MM-dd','projection.dt.interval'='1','projection.dt.interval.unit'='DAYS',
  'projection.office_id.type'='injected','projection.group_id.type'='injected',
  'storage.location.template'='s3://roster-call-history-702166530338/ui_events/dt=${dt}/office_id=${office_id}/group_id=${group_id}/'
);
```
- dev 테이블(`call_history_dev`, `ui_events_dev`)은 LOCATION/template 의 base prefix 를 `call_history_dev/` · `ui_events_dev/` 로 바꿔 동일 생성.

## 7. 체크리스트 (사용자 실행)
- [ ] Firehose 4개 재생성(옵션 A 또는 B)
- [ ] (옵션 A면) env/워크플로 스트림명 교체 + 재배포
- [ ] Athena 4개 테이블 DROP+CREATE(위 DDL·dev는 base prefix 교체)
- [ ] dev에서 변경 API 1회 호출 → S3 `call_history_dev/dt=…/office_id=…/group_id=…/` 경로 생성 확인
- [ ] Athena `SELECT summary FROM call_history_dev WHERE dt='2026-07-10' AND office_id='102560' AND group_id='…'` 조회 확인

## 8. 요약
- 3단 파티션의 유일한 걸림돌 = Firehose 동적파티셔닝(스트림 재생성 필요). 그 외는 표준.
- 백엔드 코드(app/call_action_catalog.py + main.py 미들웨어)는 이미 완료 — `summary`/`page`/`section`/`action` 을 로그에 넣고 있어 Athena 컬럼만 추가하면 바로 사람이 읽는 액션 로그가 된다.
