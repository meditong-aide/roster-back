"""교대 코드 정규화 유틸리티 모듈.

이 모듈은 `services/cp_sat_basic.py`에서 사용하던 교대 코드 정규화 로직을 분리한 것입니다.
특히 Wanted 기반의 '휴가/공가/근무'처럼 shift_gb가 없거나, OFF/주 등의 별도 표기가 들어와도
엔진 내부에서는 D/E/N/O/W 메인 코드로 일관되게 다루도록 합니다.
"""

from __future__ import annotations

from typing import Any


def build_shift_normalizer(
    shift_defs: list[dict] | None,
) -> tuple[dict[str, str], dict[str, str]]:
    """Shift ID를 알고리즘용 메인 코드(D/E/N/O/W/주)로 정규화하는 매핑을 생성한다.

    Args:
        shift_defs: shift_id, default_shift, shift_gb, type 등을 포함한 사전 리스트

    Returns:
        (id_to_main, main_to_id):
            - id_to_main: shift_id(대문자) → 메인 코드(D/E/N/O/W/주)
            - main_to_id: 메인 코드 → 대표 shift_id(초기값은 자기 자신, 매핑되면 우선 적용)

    Notes:
        - type이 '휴가'/'공가'면 메인 코드는 O로 처리한다.
        - type이 '근무'면 메인 코드는 W로 처리한다. (커버리지 요구치와 분리 목적)
    """
    canonical = {"D", "E", "N", "O", "주", "W"}
    id_to_main: dict[str, str] = {}
    main_to_id: dict[str, str] = {c: c for c in canonical}

    for row in shift_defs or []:
        raw_id = str(row.get("shift_id") or "").strip()
        if not raw_id:
            continue
        sid_upper = raw_id.upper()
        raw_default = str(row.get("default_shift") or "").strip().upper()
        raw_gb = str(row.get("shift_gb") or "").strip().upper()
        shift_type = str(row.get("type") or "").strip()

        if sid_upper in {"OFF"}:
            sid_upper = "O"

        main_code = None
        skip_main_to_id = False
        if raw_default in canonical:
            main_code = raw_default
        elif raw_gb in canonical:
            main_code = raw_gb
        elif sid_upper in canonical:
            main_code = sid_upper
        elif shift_type in {"휴가", "공가"}:
            main_code = "O"
            if sid_upper != "O":
                skip_main_to_id = True  # 휴가/공가가 canonical O를 덮어쓰지 않도록 방지
        elif shift_type == "근무":
            main_code = "W"
            if sid_upper != "W":
                skip_main_to_id = True  # 근무형은 가상 코드 W만 사용

        if not main_code:
            continue

        id_to_main[sid_upper] = main_code
        if not skip_main_to_id:
            current = main_to_id.get(main_code)
            if current in {None, main_code} or sid_upper == main_code:
                main_to_id[main_code] = raw_id

    return id_to_main, main_to_id


def normalize_shift_code(raw_code: Any, id_to_main: dict[str, str]) -> str | None:
    """입력 근무코드를 메인 코드(D/E/N/O/W/주)로 정규화한다.

    Args:
        raw_code: 입력 코드(shift_id, default_shift, shift_gb, 'OFF', '주' 등)
        id_to_main: build_shift_normalizer가 만든 shift_id → 메인코드 매핑

    Returns:
        정규화된 메인 코드 문자열 또는 None

    Examples:
        - raw_code='off' → 'O'
        - raw_code='D' → 'D'
        - raw_code='특근A' (id_to_main에 매핑되어 있으면) → 'W' 등
    """
    code = str(raw_code or "").strip()
    upper = code.upper()
    if upper in {"OFF"}:
        return "O"
    if upper in {"D", "E", "N", "O", "주", "W"}:
        return upper
    return id_to_main.get(upper)


