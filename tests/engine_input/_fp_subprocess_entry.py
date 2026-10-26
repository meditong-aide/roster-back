"""모델 지문 하위 프로세스 진입점 — PYTHONHASHSEED 결정론 확인용(설계서 §7.1 EI-1c).

pytest 없이 그대로 돈다(`python _fp_subprocess_entry.py <variant> <시나리오>`, cwd=merged 루트).
상위 `tests/conftest.py` 를 직접 import 해 `db.client2` 패치 + `app/` 경로 삽입 부수효과를 그대로
재현하고, `engine`·`engine_input_tables` 두 픽스처가 하던 테이블 생성을 그대로 한다.
표준출력 마지막 줄에 지문 기록 JSON 한 줄을 낸다.

외부 접촉 차단(2026-10-13 보강) — 이 경로는 `tests/engine_input/conftest.py` 의
`_block_external_access`(autouse 픽스처)를 안 거친다. 같은 프로세스 경로(`test_no_external_
contact`)는 그 픽스처로 보장받지만, 이 하위 프로세스는 pytest 가 아니라서 가드가 자동으로 안
걸렸다. 위험은 낮다(앱이 읽는 DB 호스트 env 는 MS_DB_HOST·DB_HOST 둘뿐이고 둘 다 루프백으로
덮이며, db.client2 도 가짜 모듈이다 — 실측), 그래도 같은 가드를 똑같이 걸고 막힌 시도 수를
기록에 남긴다.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_TESTS_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_TESTS_DIR))

import conftest as _root_conftest  # noqa: E402 — db.client2 패치 + app/ 경로 삽입(부수효과로 돈다)

from engine_input.conftest import (  # noqa: E402
    BLOCKED_ATTEMPTS,
    _install_aws_guard,
    _install_db_driver_guard,
    _install_socket_guard,
    _install_sqlalchemy_guard,
    assert_sqlite_backends,
)


def _prepare_engine():
    """상위 `engine`·`engine_input_tables` 픽스처를 pytest 없이 재현한다."""
    import db.models as models

    _root_conftest._patch_autoincrement_for_sqlite()
    engine = _root_conftest._test_engine
    _root_conftest._TestBase.metadata.create_all(bind=engine, tables=_root_conftest._REQUIRED_TABLES)
    extra_tables = [models.NurseLeavePeriod.__table__, models.NurseNightCycle.__table__]
    extra_tables[0].metadata.create_all(bind=engine, tables=extra_tables, checkfirst=True)
    return engine


def main() -> int:
    variant, name = sys.argv[1], sys.argv[2]
    with pytest.MonkeyPatch.context() as mp:
        _install_socket_guard(mp)
        _install_aws_guard(mp)
        _install_db_driver_guard(mp)
        _install_sqlalchemy_guard(mp)
        engine = _prepare_engine()
        assert_sqlite_backends(engine)
        from engine_input.model_fingerprint import (
            build_fingerprint_record,
            run_fingerprint,
        )

        result, holder = run_fingerprint(engine, name, variant)
        record = build_fingerprint_record(name, variant, result, holder)
        record["blocked_external_attempts"] = len(BLOCKED_ATTEMPTS)
        assert not BLOCKED_ATTEMPTS, f"외부 접촉 시도 차단됨(가드가 잡아야 할 일이 없어야 한다): {BLOCKED_ATTEMPTS}"
    print(json.dumps(record, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
