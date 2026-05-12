"""Roster Log Forwarder — CloudWatch Subscription Filter 처리기.

운영 흐름:
    /aws/lambda/roster-solver-{dev,prod} 의 JSON 로그
        ↓ CloudWatch Subscription Filter (자동)
    이 Lambda 함수 (event.awslogs.data 에 gzip+base64 로 수신)
        ↓ JSON 파싱 → office_id/group_id/nurse_id 추출
    /roster/office/{office_id}/{group_id}/{nurse_id} 로 fanout
        - log group 없으면 동적 생성 + retention 정책 적용
        - log stream 이름은 job_id

거버넌스:
    - 같은 office 의 모든 호출 이력을 하나의 prefix 아래 시각적 트리로 탐색
    - 권한 분리(RBAC) 가능 — 특정 office prefix 만 읽기 권한 부여
    - 진단 데이터(JSON 형식) 그대로 forward — 검색/필터 즉시 가능

의존성: boto3 (Lambda runtime 자체 포함). 외부 패키지 없음.
"""
import base64
import gzip
import json
import os
from collections import defaultdict
from typing import Any

import boto3
from botocore.exceptions import ClientError

logs_client = boto3.client('logs')

# 동적으로 생성한 log group / stream 을 invocation 간 캐싱하여
# Lambda container 재사용 시 createLogGroup / createLogStream 중복 호출 방지.
GROUP_CACHE: set[str] = set()
STREAM_CACHE: set[tuple[str, str]] = set()

LOG_GROUP_PREFIX = os.environ.get('LOG_GROUP_PREFIX', '/roster')
RETENTION_DAYS = int(os.environ.get('RETENTION_DAYS', '30'))


def handler(event: dict, context) -> dict:
    """Subscription Filter event 처리.

    event 구조:
        {"awslogs": {"data": "<gzip+base64 encoded payload>"}}

    payload (디코드 후):
        {
          "messageType": "DATA_MESSAGE",
          "owner": "...", "logGroup": "...", "logStream": "...",
          "subscriptionFilters": ["..."],
          "logEvents": [{"id":"...","timestamp":..., "message":"..."}, ...]
        }

    target log group 형식:
        /roster/{env}/{office_id}/{group_id}/{nurse_id}
        env 는 source log group suffix 로 판단
            /aws/lambda/roster-solver-dev  → dev
            /aws/lambda/roster-solver-prod → prod
            그 외 → unknown
    """
    payload_b64 = event['awslogs']['data']
    payload_gz = base64.b64decode(payload_b64)
    payload_json = gzip.decompress(payload_gz)
    log_data = json.loads(payload_json)

    # Source log group 으로 env 판단 (메시지 내용 의존 X)
    source_log_group = log_data.get('logGroup', '')
    if source_log_group.endswith('-prod'):
        env = 'prod'
    elif source_log_group.endswith('-dev'):
        env = 'dev'
    else:
        env = 'unknown'

    log_events = log_data.get('logEvents', [])

    # (target_group, stream_name) 별로 묶어서 batch put_log_events 호출.
    # 같은 invocation 안에 같은 office/group/nurse/job 메시지가 여러 개 있을 가능성 있음.
    grouped: dict[tuple[str, str], list[dict]] = defaultdict(list)
    skipped = 0

    # Subscription Filter pattern 을 빈 문자열(전체 통과)로 운영하므로 plain print 도 흘러온다.
    # 솔버는 invocation 시작에 powertools Logger 가 office/group/nurse/job_id 포함 JSON 을 먼저 출력
    # → 이후 발생하는 모든 stdout(plain print 포함) 은 직전 JSON 의 context 로 동일 nurse stream 에 라우팅.
    # Subscription Filter event 의 logEvents 는 단일 source stream 의 시간순 chunk 이므로
    # 같은 invocation 안에서는 last_context 가 유효.
    last_context: dict | None = None

    for ev in sorted(log_events, key=lambda e: e.get('timestamp', 0)):
        message = ev.get('message', '')

        try:
            log_json = json.loads(message)
        except json.JSONDecodeError:
            log_json = None

        if log_json is not None:
            office_id = log_json.get('office_id')
            group_id = log_json.get('group_id')
            nurse_id = log_json.get('nurse_id')
            job_id = log_json.get('job_id') or 'default'
            if all([office_id, group_id, nurse_id]):
                last_context = {
                    'office_id': office_id,
                    'group_id': group_id,
                    'nurse_id': nurse_id,
                    'job_id': job_id,
                }

        if last_context is None:
            # 첫 JSON 컨텍스트 도착 전 출력(Lambda init 등) — skip.
            # 원본 /aws/lambda/roster-solver-{env} log group 에는 그대로 남아 있음.
            skipped += 1
            continue

        target_group = (
            f"{LOG_GROUP_PREFIX}/{env}/{last_context['office_id']}/"
            f"{last_context['group_id']}/{last_context['nurse_id']}"
        )
        grouped[(target_group, last_context['job_id'])].append({
            'timestamp': ev['timestamp'],
            'message': message,
        })

    forwarded = 0
    failed = 0

    for (target_group, stream_name), events in grouped.items():
        if not _ensure_group_and_stream(target_group, stream_name):
            failed += len(events)
            continue
        try:
            logs_client.put_log_events(
                logGroupName=target_group,
                logStreamName=stream_name,
                logEvents=sorted(events, key=lambda e: e['timestamp']),
            )
            forwarded += len(events)
        except ClientError as e:
            print(f"put_log_events failed: group={target_group}, stream={stream_name}, error={e}")
            failed += len(events)

    return {
        'statusCode': 200,
        'forwarded': forwarded,
        'skipped': skipped,
        'failed': failed,
        'total': len(log_events),
    }


def _ensure_group_and_stream(target_group: str, stream_name: str) -> bool:
    """log group / stream 이 없으면 생성. 캐시에 등록.

    반환:
        성공/이미 존재 시 True, 그 외 False (forwarding 포기).
    """
    if target_group not in GROUP_CACHE:
        try:
            logs_client.create_log_group(logGroupName=target_group)
            try:
                logs_client.put_retention_policy(
                    logGroupName=target_group,
                    retentionInDays=RETENTION_DAYS,
                )
            except ClientError as e_ret:
                # retention policy 실패해도 group 자체는 살아있으므로 forwarding 진행.
                print(f"put_retention_policy failed: {target_group}, {e_ret}")
        except ClientError as e:
            if e.response['Error']['Code'] != 'ResourceAlreadyExistsException':
                print(f"create_log_group failed: {target_group}, {e}")
                return False
        GROUP_CACHE.add(target_group)

    stream_key = (target_group, stream_name)
    if stream_key not in STREAM_CACHE:
        try:
            logs_client.create_log_stream(
                logGroupName=target_group,
                logStreamName=stream_name,
            )
        except ClientError as e:
            if e.response['Error']['Code'] != 'ResourceAlreadyExistsException':
                print(f"create_log_stream failed: {target_group}/{stream_name}, {e}")
                return False
        STREAM_CACHE.add(stream_key)

    return True
