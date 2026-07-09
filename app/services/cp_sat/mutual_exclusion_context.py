"""상호 근무 배제 솔버 컨텍스트 빌더.

config map(`mutual_exclusion_by_nurse_id`, nurse_mutual_exclusion_period 유래)을
솔버 인덱스 페어 리스트로 변환한다. 양방향 저장이라 frozenset 로 dedup(days 합집합).
`preceptee_context.build_preceptee_context` 패턴 미러.
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


def build_mutual_exclusion_context(nurses, config_map, num_days: int,
                                   id_to_idx: dict | None = None) -> list:
    """config map → [(a_idx, b_idx, frozenset(day_idx))].

    config_map 형태: {nurse_id: {"partner_id": pid, "days": set(0-based)}}.
    규칙:
      - 명단 밖(idx None)·자기자신·빈 days → 제외.
      - 양방향 저장(A→B, B→A) → frozenset((a,b)) 로 dedup, days 는 합집합.
    반환: (a<b 정렬된) 페어 + 활성일 집합 리스트.
    """
    if id_to_idx is None:
        id_to_idx = _build_id_to_idx(nurses)
    acc: dict[frozenset, set] = {}
    for nid, info in (config_map or {}).items():
        a = id_to_idx.get(str(nid))
        if a is None:
            continue
        if isinstance(info, dict):
            pid = info.get("partner_id")
            days = info.get("days") or set()
        else:  # 하위호환: {nid: set(days)} 형태는 partner 정보 없음 → 스킵
            continue
        if pid is None or not days:
            continue
        b = id_to_idx.get(str(pid))
        if b is None or b == a:
            continue
        key = frozenset((a, b))
        acc.setdefault(key, set()).update(days)
    out = []
    for key, days in acc.items():
        a, b = sorted(key)
        out.append((a, b, frozenset(days)))
    return out
