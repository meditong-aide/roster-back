from __future__ import annotations

from collections.abc import Sequence


def normalize_main_bucket(
    code: object,
    *,
    code2main: dict[str, str] | None = None,
    shift_id_to_main_map: dict[str, str] | None = None,
) -> str:
    raw = str(code or "").strip().upper()
    if not raw:
        return ""
    mapped = (code2main or {}).get(raw) or (shift_id_to_main_map or {}).get(raw) or raw
    if mapped in {"OFF", "주"}:
        return "O"
    return str(mapped).strip().upper()


def compute_main_bucket_indices(
    shift_types: Sequence[object],
    *,
    target_main: str,
    code2main: dict[str, str] | None = None,
    shift_id_to_main_map: dict[str, str] | None = None,
) -> list[int]:
    target = str(target_main or "").strip().upper()
    if not target:
        return []
    return [
        idx
        for idx, shift_code in enumerate(shift_types)
        if normalize_main_bucket(
            shift_code,
            code2main=code2main,
            shift_id_to_main_map=shift_id_to_main_map,
        )
        == target
    ]
