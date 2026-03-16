"""간호사별 work_shifts를 반영하여 최종 근무 코드를 대체하는 모듈."""

from __future__ import annotations


def apply_work_shift_overrides(
    *,
    roster_map: dict[str, list[str]],
    nurses_data: list[dict],
    shift_definitions: list[dict] | None,
) -> dict[str, list[str]]:
    """work_shifts 설정을 사용해 간호사별 근무 코드를 맞춤 대체한다.

    Args:
        roster_map: 간호사별 최종 근무표(shift_id 또는 메인 코드).
        nurses_data: DB에서 가져온 간호사 원본 데이터 목록.
        shift_definitions: shifts 테이블에서 가져온 정의 목록.

    Returns:
        간호사별 work_shifts 우선순위를 반영한 근무표 사전.

    Notes:
        - 메인 코드(D/E/N)만 대상이며, OFF/주/휴가 등은 그대로 유지한다.
        - 동일한 shift_id가 office/group 별로 중복될 수 있으므로, office_id/group_id가 맞는 정의를 우선한다.
    """
    if not roster_map or not nurses_data or not shift_definitions:
        return roster_map

    def _normalize_default_shift(token: object) -> str | None:
        raw = str(token or "").strip()
        if not raw:
            return None
        upper = raw.upper()
        if upper in {"OFF", "주"}:
            return "O"
        if upper in {"D", "E", "N", "O", "M", "W"}:
            return upper
        return None

    shift_meta_index: dict[str, list[dict[str, str | None]]] = {}
    main_by_shift_id: dict[str, str] = {}
    for row in shift_definitions or []:
        shift_id_raw = str(row.get("shift_id") or "").strip()
        if not shift_id_raw:
            continue
        shift_id_key = shift_id_raw.upper()
        main_code = _normalize_default_shift(row.get("default_shift"))
        if main_code:
            main_by_shift_id[shift_id_key] = main_code
        meta = {
            "shift_id": shift_id_raw,
            "main_code": main_code,
            "office_id": row.get("office_id"),
            "group_id": row.get("group_id"),
        }
        shift_meta_index.setdefault(shift_id_key, []).append(meta)

    if not shift_meta_index:
        return roster_map

    def _select_shift_meta(
        shift_id_key: str, nurse_office: str | None, nurse_group: str | None
    ) -> dict[str, str | None] | None:
        """office/group이 일치하는 shift 정의를 우선 선택한다."""
        candidates = shift_meta_index.get(shift_id_key) or []
        if not candidates:
            return None
        for meta in candidates:
            office_ok = (
                not meta.get("office_id")
                or not nurse_office
                or str(meta["office_id"]) == nurse_office
            )
            group_ok = (
                not meta.get("group_id")
                or not nurse_group
                or str(meta["group_id"]) == nurse_group
            )
            if office_ok and group_ok:
                return meta
        return candidates[0]

    for nurse_row in nurses_data:
        nurse_id_raw = nurse_row.get("nurse_id") or nurse_row.get("db_id")
        if nurse_id_raw is None:
            continue
        roster_key = (
            nurse_id_raw
            if nurse_id_raw in roster_map
            else str(nurse_id_raw)
            if str(nurse_id_raw) in roster_map
            else None
        )
        if roster_key is None:
            continue
        work_shifts = nurse_row.get("work_shifts") or []
        if not isinstance(work_shifts, list) or not work_shifts:
            continue
        nurse_office = (
            str(nurse_row.get("office_id")).strip()
            if nurse_row.get("office_id") is not None
            else None
        )
        nurse_group = (
            str(nurse_row.get("group_id")).strip()
            if nurse_row.get("group_id") is not None
            else None
        )
        target_by_main: dict[str, str] = {}
        for ws in work_shifts:
            ws_raw = str(ws or "").strip()
            if not ws_raw:
                continue
            ws_key = ws_raw.upper()
            meta = _select_shift_meta(ws_key, nurse_office, nurse_group)
            if not meta:
                continue
            main_code = meta.get("main_code")
            if not main_code and ws_key in {"D", "E", "N", "M"}:
                main_code = ws_key
            if main_code not in {"D", "E", "N", "M"}:
                continue
            # 첫 번째로 매칭된 shift_id를 우선 적용한다.
            target_by_main.setdefault(main_code, ws_raw)

        if not target_by_main:
            continue

        updated_schedule: list[str] = []
        for entry in roster_map.get(roster_key, []):
            entry_str = str(entry)
            entry_key = entry_str.upper()
            entry_main = None
            if entry_key in {"D", "E", "N", "M"}:
                entry_main = entry_key
            elif entry_key in main_by_shift_id:
                entry_main = main_by_shift_id[entry_key]
            if entry_main and entry_main in target_by_main:
                updated_schedule.append(target_by_main[entry_main])
            else:
                updated_schedule.append(entry)
        roster_map[roster_key] = updated_schedule

    return roster_map

