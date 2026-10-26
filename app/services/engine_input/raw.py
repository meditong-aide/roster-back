"""raw 컬럼 헬퍼 — 오프라인 전용(EI-9d).

`app/services/roster_create_service.py`(이하 rcs)는 `roster_config`·`weekly_off_settings`
의 모델 밖(raw) 컬럼 3종을 **그룹 최신 행** 규약으로 읽는다. 이 모듈은 9b(오프라인 합본)가
같은 값을 같은 선택 규칙으로 읽기 위한 것이다 — **운영 호출 자리는 하나도 바꾸지 않는다**
(D23). 호출처는 지금 0 이고(9b 가 생기면 `engine_input/builder.py` 뿐), 레지스트리 정본
키는 `fixed_holiday_off_yn`·`n2n_min_gap`·`weekly_off_settings_activate` 세 가지뿐이다
(`call_base_id` 는 9d 가 아니라 콜 후처리 `load_call_code_map` 이 읽는다 — EI9_SPEC.md §2.3).

★ `fixed_holiday_off_yn`·`n2n_min_gap` 은 rcs 의 운영 함수(`_holiday_off_enabled`·
  `_n2n_min_gap_setting`)에 **위임**한다. 선택 규칙(컬럼 존재 확인 → 그룹 최신 행, n2n 은
  NOT NULL 행만)이 구조적으로 같아 사본 SQL 을 따로 두지 않는다 — 드리프트날 사본이 없다.
★ `weekly_off_settings_activate` 는 rcs 가 **인라인**(함수가 아님)이라 위임할 자리가 없다.
  운영을 건드리지 않기 위해 같은 SQL·같은 예외 규칙의 **오프라인 사본**을 둔다(O-7). 드리프트는
  `tests/engine_input/test_raw.py` 의 오라클(운영 inline 원문 사본) 차분 테스트로 막는다.
★ BR-5(INFORMATION_SCHEMA '있음'만 캐시)는 여기서 하지 않는다 — 9d 는 오프라인 전용이라
  이득이 없고, 한 프로세스에서 시나리오마다 DDL 이 달라지는 하네스에서 캐시가 오염된다
  (EI-13c 에서 프로덕션이 9d 를 쓰게 될 때 `clear_raw_cache()` 와 함께 적용한다).
"""
from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.orm import Session

#: 레지스트리 정본 키(D9) — 이 셋 말고는 `KeyError`.
RAW_SETTING_KEYS: tuple[str, ...] = (
    "fixed_holiday_off_yn", "n2n_min_gap", "weekly_off_settings_activate",
)


@dataclass(frozen=True)
class RawRead:
    """raw 컬럼 하나를 읽은 결과. `reader` 는 실제로 위임한 함수 이름(기록·디버깅용)."""

    key: str
    value: object
    reader: str


def read_raw_setting(db: Session, key: str, *, office_id: str, group_id: str) -> RawRead:
    """레지스트리 raw 키 하나를 오프라인에서 읽는다(운영과 같은 선택 규칙, DB 조회 1회).

    모르는 키는 `KeyError`. 예외를 삼키지 않는다 — 위임한 운영 함수·사본 함수가 이미 자기
    예외를 삼키고 기본값(False·0)으로 떨어뜨리므로 여기서 다시 감쌀 것이 없다.
    """
    if key == "fixed_holiday_off_yn":
        from services.roster_create_service import _holiday_off_enabled

        return RawRead(key, _holiday_off_enabled(db, group_id), "_holiday_off_enabled")
    if key == "n2n_min_gap":
        from services.roster_create_service import _n2n_min_gap_setting

        return RawRead(key, _n2n_min_gap_setting(db, group_id), "_n2n_min_gap_setting")
    if key == "weekly_off_settings_activate":
        value = _weekly_off_settings_activate(db, office_id=office_id, group_id=group_id)
        return RawRead(key, value, "_weekly_off_settings_activate")
    raise KeyError(f"알 수 없는 raw 키: {key} (등록된 키: {RAW_SETTING_KEYS})")


def _weekly_off_settings_activate(db: Session, *, office_id: str, group_id: str) -> bool:
    """`weekly_off_settings.activate` — rcs 인라인(roster_create_service.py :6008-:6026)의
    오프라인 사본. SQL 문자열·예외 규칙이 원문과 같다(O-7, 드리프트는 오라클 차분 테스트로 막는다).
    """
    from services.roster_create_service import _table_exists

    try:
        if not _table_exists(db, "weekly_off_settings"):
            return False
        row = db.execute(
            text(
                "SELECT TOP 1 activate "
                "FROM weekly_off_settings "
                "WHERE office_id = :office_id AND group_id = :group_id"
            ),
            {"office_id": office_id, "group_id": group_id},
        ).fetchone()
        return bool(row.activate) if row else False
    except Exception:  # noqa: BLE001 — 원문과 같은 폭(공휴일·n2n 헬퍼와 동일 규약)
        return False
