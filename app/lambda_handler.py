# app/lambda_handler.py
"""AWS Lambda 진입점 — SQS event source mapping에 의해 자동 호출.

운영 흐름:
    SQS 메시지 도착 → AWS Lambda 서비스가 handler() 호출
        → 각 record body 파싱 → worker.process_job(payload) 위임
        → 결과 반환 (Lambda 정상 종료 시 SQS 메시지 자동 삭제)

ECS task와 코드 공유:
    실제 솔버 실행 로직은 worker.process_job 에 있으며,
    이 모듈은 SQS event ↔ payload 변환만 담당하는 얇은 어댑터.
"""
import json
import logging
import os
import sys

# Lambda task root에서 app/ 하위 모듈을 import할 수 있도록 sys.path 보강.
# 컨테이너 image에서 worker.py가 /var/task/app/worker.py 또는 ${LAMBDA_TASK_ROOT}/worker.py 위치.
_LAMBDA_TASK_ROOT = os.environ.get("LAMBDA_TASK_ROOT", "/var/task")
_APP_DIR = os.path.join(_LAMBDA_TASK_ROOT, "app")
for _p in (_APP_DIR, _LAMBDA_TASK_ROOT):
    if _p not in sys.path:
        sys.path.insert(0, _p)

# worker.py가 자체적으로 sys.path를 조작하므로 import 시점에 부수 효과 발생 가능.
# 그래도 process_job 만 사용하므로 영향 없음.
from worker import process_job  # noqa: E402

logger = logging.getLogger()
logger.setLevel(logging.INFO)


def handler(event: dict, context) -> dict:
    """SQS event source mapping에 의해 호출되는 진입점.

    인자:
        event: SQS event JSON. event["Records"]가 메시지 배열.
            각 record["body"]는 SQS 메시지 본문(JSON 문자열).
        context: Lambda runtime context (현재 미사용).

    반환:
        성공 시 dict: {"statusCode": 200, "results": [...]}.
        Lambda 정상 반환 → SQS 메시지 자동 삭제 (ack).

    예외 처리 정책:
        - **ValueError** (payload 자체 부적절): 재시도 무의미하므로 ack 처리 (정상 반환).
        - **JSONDecodeError, 그 외 Exception**: 그대로 raise → SQS visibility timeout 후 재시도.
          DB 일시 단절·네트워크 오류 등 transient 실패가 영구 손실되지 않도록 보장한다.
          반복 실패 보호는 SQS RedrivePolicy(maxReceiveCount + DLQ)로 인프라 측에서 담당.
    """
    records = event.get("Records", []) if isinstance(event, dict) else []
    logger.info("[lambda] SQS records 수신: %d건", len(records))

    results = []
    for record in records:
        body = record.get("body", "") if isinstance(record, dict) else ""
        try:
            payload = json.loads(body) if body else {}
        except json.JSONDecodeError as e:
            logger.error("[lambda] body JSON 파싱 실패 (재시도 트리거): %s", str(e))
            raise

        try:
            result = process_job(payload)
        except ValueError as e:
            # payload 자체 오류는 재시도해도 동일하게 실패 → 정상 반환으로 메시지 삭제(ack)
            logger.error("[lambda] payload 오류 (재시도 무의미, ack): %s", str(e))
            result = {"status": "failed", "error": str(e), "payload_invalid": True}
        # 그 외 Exception은 의도적으로 catch하지 않음 → SQS retry 트리거.

        results.append(result)

    return {"statusCode": 200, "results": results}
