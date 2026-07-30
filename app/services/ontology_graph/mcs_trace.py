"""Deletion-based MCS 추적기 — 복합 하드 모순을 조건 단위로 쫓아간다 (MUS·reify 없음).

핵심: 하나의 최소 수선집합(MCS)을 찾는 건 NP-hard 가 아니라 **선형 재solve**다.
전부 완화(→feasible)에서 시작해, 각 항목을 하나씩 '다시 강제'해보며 그래도 feasible
이면 그 항목은 충돌에 불필요 → 제거. 끝나면 남은 것이 최소 수선집합.

  relaxed = 전체
  for it in items:                      # ← 조합(2^N) 아님, 항목당 1번(선형)
      if resolve(relaxed - {it}): relaxed -= {it}
  return relaxed                        # 이걸 풀면 feasible = 추적된 복합 원인

2단계 입도: (1) family 단위로 싸게 얽힌 그룹 지목 → (2) 그 family 안에서만 instance
(간호사·날) 단위 drill-down → 실제 조합의 셀까지 추적. 완전성 손실 없음(family 가
얽혔다 → 그 안에서 좁힘). resolve_fn 은 엔진 비의존(주입식)이라 mock 으로 검증 가능.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Hashable, Iterable


@dataclass
class ConflictTrace:
    families: list[str]                               # 최소 수선 family 집합
    instances_by_family: dict[str, list[Any]]         # family → 지목된 instance(간호사/날)
    solve_count: int                                  # 총 재solve 횟수(선형 확인용)
    feasible_when_all_relaxed: bool                   # 전부 완화 시 feasible 였나
    certificate: str = ""


def minimal_correction_set(
    resolve_fn: Callable[[frozenset], bool],
    items: Iterable[Hashable],
    *,
    logger: Callable[[str], None] | None = None,
) -> tuple[frozenset, int] | None:
    """deletion-based MCS. resolve_fn(relaxed:frozenset)->feasible. (min_set, solve_count).

    전부 완화해도 infeasible 이면 None(카탈로그 밖 원인). 재solve = len(items)+1 (선형).
    """
    items = list(items)
    solves = 1
    if not resolve_fn(frozenset(items)):
        return None                                   # 전부 완화도 실패 → 이 축 밖
    relaxed = set(items)
    for it in items:
        trial = frozenset(relaxed - {it})
        solves += 1
        if resolve_fn(trial):                         # it 안 풀어도 feasible → 불필요
            relaxed = set(trial)
            if logger:
                logger(f"  [MCS] '{it}' 불필요(제거)")
        elif logger:
            logger(f"  [MCS] '{it}' 필수(유지)")
    return frozenset(relaxed), solves


def trace_conflict(
    family_resolve: Callable[[frozenset], bool],
    families: Iterable[str],
    *,
    instance_resolve: Callable[[str, frozenset], bool] | None = None,
    instances_by_family: dict[str, list[Any]] | None = None,
    logger: Callable[[str], None] | None = None,
) -> ConflictTrace:
    """복합 충돌 추적: family MCS → (선택) family 별 instance drill-down.

    family_resolve(relaxed_families) -> feasible.
    instance_resolve(family, relaxed_instances) -> feasible  (그 family 만 부분완화).
    """
    fams = list(families)
    total_solves = 0

    # 1) family 단위 최소 수선집합
    res = minimal_correction_set(family_resolve, fams, logger=logger)
    if res is None:
        return ConflictTrace(families=[], instances_by_family={}, solve_count=1,
                             feasible_when_all_relaxed=False,
                             certificate="전체 완화로도 해소 불가 — 모델 밖 원인(데이터/고정셀 등) 의심.")
    min_fams, solves = res
    total_solves += solves

    # 2) 각 family 안에서 instance drill-down (culprit 간호사/날 좁히기)
    inst_by_fam: dict[str, list[Any]] = {}
    if instance_resolve and instances_by_family:
        for f in min_fams:
            insts = instances_by_family.get(f) or []
            if not insts:
                continue
            sub = minimal_correction_set(
                lambda relaxed, _f=f: instance_resolve(_f, relaxed), insts,
                logger=(lambda m, _f=f: logger(f"  [{_f}] {m}")) if logger else None)
            if sub is not None:
                culprits, ss = sub
                total_solves += ss
                inst_by_fam[f] = sorted(culprits, key=lambda x: str(x))

    fam_list = sorted(min_fams)
    parts = []
    for f in fam_list:
        ci = inst_by_fam.get(f)
        parts.append(f"{f}" + (f"({', '.join(str(c) for c in ci)})" if ci else ""))
    cert = ("복합 충돌 추적: 다음을 동시에 풀어야 feasible → " + " + ".join(parts)
            if fam_list else "충돌 원인 미검출.")

    return ConflictTrace(families=fam_list, instances_by_family=inst_by_fam,
                         solve_count=total_solves, feasible_when_all_relaxed=True,
                         certificate=cert)
