"""프리셉티 솔버 컨텍스트 중앙 빌더.

엔진 4곳(cp_sat_basic / fallback_lex / postprocess_off / feasibility_alerts)이
프리셉티 membership(누가)·WHO(어느 프리셉터)·WHEN(어느 날) 을 **한 곳**에서 결정하도록
통합한다. 진실 = config map `preceptee_period_by_nurse_id`(nurse_preceptee_period period 유래).
**캐시(nurses.preceptor_id) 미사용** — 시점-무시 캐시로 인한 follow 누수(기간 종료 후 따라감)를 구조적으로 차단.

설계: docs/NURSE_PRECEPTEE_PERIOD_DESIGN.md §6.
"""
from __future__ import annotations


def _build_id_to_idx(nurses) -> dict[str, int]:
    out: dict[str, int] = {}
    for i, nu in enumerate(nurses):
        key = getattr(nu, "db_id", None)
        if key is None:
            key = getattr(nu, "nurse_id", i)
        out[str(key)] = i
    return out


def build_preceptee_context(nurses, config_map, num_days: int,
                            id_to_idx: dict | None = None) -> dict[int, tuple[int | None, frozenset]]:
    """config map → {preceptee_idx: (preceptor_idx|None, frozenset(day_idx))}.

    config_map 형태(신): {nurse_id: {"preceptor_id": pid, "days": set(0-based)}}.
    하위호환(구): {nurse_id: set(days)} — 이 경우 preceptor_idx=None(호출부가 캐시 폴백).

    규칙:
      - days 가 비었거나 키 부재 → 그 달 프리셉티 아님(제외). "기간 종료 후 follow" 차단의 핵심.
      - days == 전체월 → 무기한/전체월 follow (그대로 유지, default 분기 없음).
      - preceptor_idx 해석 실패(명단 밖 등) → None (호출부가 캐시로 폴백 가능).
    """
    if id_to_idx is None:
        id_to_idx = _build_id_to_idx(nurses)
    ctx: dict[int, tuple[int | None, frozenset]] = {}
    for nid, info in (config_map or {}).items():
        idx = id_to_idx.get(str(nid))
        if idx is None:
            continue
        if isinstance(info, dict):
            days = info.get("days") or set()
            pid = info.get("preceptor_id")
        else:  # 구 형태: set(days) 직접
            days = info or set()
            pid = None
        if not days:
            continue  # 그 달 프리셉티 아님(종료/미겹침)
        pidx = id_to_idx.get(str(pid)) if pid is not None else None
        ctx[idx] = (pidx, frozenset(days))
    return ctx
