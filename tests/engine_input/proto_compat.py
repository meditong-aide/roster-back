"""CP-SAT `model.Proto()` 읽기 — ortools 판과 무관한 표준 protobuf 변환(2026-10-14,
ortools 9.12.4544 → 9.15.6755 전환 사고 대응).

ortools 9.12 는 `CpModel.proto`/`.Proto()` 가 그대로 `ortools.sat.cp_model_pb2.CpModelProto`
(표준 protobuf 메시지)였다 — `SerializeToString`·`WhichOneof`·`HasField`·`DESCRIPTOR` 가
전부 됐다. 9.13+(설치판 9.15.6755)부터는 pybind 래퍼
(`ortools.sat.python.cp_model_helper.CpModelProto`)로 바뀌어 그 메서드들이 없다 — 공개
메서드는 `copy_from`·`merge_from`·`merge_text_format`·`parse_text_format`·`has_*`/`clear_*`
와 필드 접근자뿐이다(실측).

표준 메시지로 바꾸는 확실한 방법은 `CpModel.export_to_file(경로)`(확장자가 "txt" 로 끝나지
않으면 바이너리 — docstring) 로 내보낸 뒤 `cp_model_pb2.CpModelProto.FromString(bytes)` 로
다시 읽는 것이다. 텍스트 경로(`text_format.Parse(str(model.proto), ...)`)도 바이트 단위로
같은 결과를 내지만, 제약 수천 개 모델에서는 텍스트 경로가 바이너리 경로보다 약 200배
느리다(실측, 2026-10-14 — 제약 3,889·변수 6,161 모델에서 텍스트 0.54~0.67초 vs 바이너리
0.003초). 그래서 바이너리 경로를 쓴다.

9.12 는 `model.proto` 가 이미 `cp_model_pb2.CpModelProto`(표준 메시지)이므로 그 경로에서는
변환 없이 그대로 돌려준다 — 이 함수는 어느 판에서도 그대로 쓸 수 있다.
"""

from __future__ import annotations

import os
import tempfile

from google.protobuf import message as _pb_message
from ortools.sat import cp_model_pb2


def model_to_pb(model) -> cp_model_pb2.CpModelProto:
    """CP-SAT 모델 → 표준 `cp_model_pb2.CpModelProto`(판과 무관, 위 모듈 docstring 참고).

    `model` 은 다 지어진(아직 풀지 않은) `cp_model.CpModel` 이어야 한다 — 이 함수는 solve 를
    절대 부르지 않는다."""
    raw = model.proto
    if isinstance(raw, _pb_message.Message):
        return raw  # 9.12 — 이미 표준 메시지.
    fd, path = tempfile.mkstemp(suffix=".pb")
    os.close(fd)
    try:
        if not model.export_to_file(path):
            raise RuntimeError("proto_compat.model_to_pb: export_to_file 실패")
        with open(path, "rb") as f:
            return cp_model_pb2.CpModelProto.FromString(f.read())
    finally:
        os.unlink(path)
