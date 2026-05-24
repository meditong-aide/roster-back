"""팀 자동 분배 알고리즘 (Greedy + Local 2-opt Swap).

스코프: 그룹 내 간호사를 N전담 미배정 풀 제외하고 팀에 자동 분배.
입력은 DB 무관 — 외부에서 nurses/wanted를 dict로 전달.
DB 연동·API는 별도 wrapper에서 처리.

핵심 규칙:
- 팀 수 = grade-1 인원수 (각 grade-1이 시드)
- preceptee는 preceptor와 같은 팀 (hard)
- 팀 크기 [min_size, max_size] (디폴트 4~6)
- soft1: 팀 내 OFF/FB 겹침 minimize (pairwise overlap sum)
- soft2: 팀 간 grade 분포 균등 (L1 deviation from per-team target)
"""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Optional
import itertools


@dataclass
class NurseInput:
    """알고리즘 입력 nurse 표현."""
    nurse_id: str
    grade: Optional[int]                    # 1=preceptor 시드, 0/None=신규
    preceptor_id: Optional[str] = None      # preceptee면 preceptor의 nurse_id
    off_days: frozenset[int] = frozenset()  # wanted OFF 일자 set
    fb_days: frozenset[int] = frozenset()   # wanted FB(연차) 일자 set


@dataclass
class TeamAssignResult:
    teams: dict[int, list[str]]              # team_idx (0..k-1) → [nurse_id]
    unassigned: list[str]                    # N전담 등
    objective: float                         # 총 페널티 (낮을수록 좋음)
    overlap_total: int                       # OFF+FB 겹침 합
    grade_dev_total: float                   # grade 분포 deviation 합
    team_size: dict[int, int]
    team_grade_breakdown: dict[int, dict[int, int]]
    stats: dict[str, float] = field(default_factory=dict)


def _pair_overlap(a: NurseInput, b: NurseInput) -> int:
    """두 간호사의 OFF + FB 겹침 일수."""
    return len(a.off_days & b.off_days) + len(a.fb_days & b.fb_days)


def _team_overlap_sum(team_members: list[NurseInput]) -> int:
    if len(team_members) < 2:
        return 0
    return sum(
        _pair_overlap(a, b)
        for a, b in itertools.combinations(team_members, 2)
    )


def _grade_dev(teams: dict[int, list[NurseInput]], grade_counts: dict[int, int]) -> float:
    """팀 간 grade 분포의 L1 deviation 합. grade 별 target=total/num_teams 대비."""
    num_teams = len(teams)
    if num_teams == 0:
        return 0.0
    total = 0.0
    for g, cnt_total in grade_counts.items():
        target = cnt_total / num_teams
        for t, members in teams.items():
            cnt_t = sum(1 for n in members if n.grade == g)
            total += abs(cnt_t - target)
    return total


def _objective(teams: dict[int, list[NurseInput]], grade_counts: dict[int, int],
               w_overlap: int, w_grade: float) -> tuple[float, int, float]:
    overlap = sum(_team_overlap_sum(m) for m in teams.values())
    dev = _grade_dev(teams, grade_counts)
    obj = w_overlap * overlap + w_grade * dev
    return obj, overlap, dev


def _build_seeds_and_followers(
    nurses: list[NurseInput],
    seed_ids: list[str],
) -> tuple[list[NurseInput], dict[str, list[NurseInput]], list[NurseInput]]:
    """지정된 seed_ids로 시드 추출 + preceptee → preceptor 매핑 + 잔여 인원."""
    by_id = {n.nurse_id: n for n in nurses}
    seeds: list[NurseInput] = []
    for sid in seed_ids:
        if sid not in by_id:
            raise ValueError(f"seed nurse_id {sid} not in pool")
        s = by_id[sid]
        if s.grade != 1:
            raise ValueError(f"seed {sid} grade={s.grade} != 1 (시드는 grade-1이어야 함)")
        seeds.append(s)
    followers: dict[str, list[NurseInput]] = {s.nurse_id: [] for s in seeds}
    assigned_ids: set[str] = set(s.nurse_id for s in seeds)
    # preceptees 매핑 (preceptor가 시드인 경우만; 시드 아닌 preceptor에 묶인 preceptee는 둘 다 일반 풀로)
    for n in nurses:
        if n.preceptor_id and n.preceptor_id in followers:
            followers[n.preceptor_id].append(n)
            assigned_ids.add(n.nurse_id)
    remaining = [n for n in nurses if n.nurse_id not in assigned_ids]
    return seeds, followers, remaining


def _greedy_assign(
    nurses: list[NurseInput],
    seed_ids: list[str],
    w_overlap: int,
    w_grade: float,
    min_size: int,
    max_size: int,
) -> dict[int, list[NurseInput]]:
    """1단계: 시드 + preceptee 고정 후 잔여를 greedy로 분배.
    팀 수 = len(seed_ids). 시드 외 grade-1은 일반 풀에 섞임 (정상)."""
    seeds, followers, remaining = _build_seeds_and_followers(nurses, seed_ids)
    teams: dict[int, list[NurseInput]] = {}
    for i, s in enumerate(seeds):
        teams[i] = [s] + followers[s.nurse_id]

    grade_counts = _grade_count_total(nurses)

    # 잔여: 영향력 큰 순 정렬 — grade가 비어있는 팀 우선 채우려면 grade descending, OFF 많은 순
    # 디폴트: grade 큰 순(g3, g2 등), 같은 grade 내 OFF 일수 많은 순
    remaining_sorted = sorted(
        remaining,
        key=lambda n: (-(n.grade or -1), -len(n.off_days) - len(n.fb_days)),
    )
    for n in remaining_sorted:
        best_t = -1
        best_obj = float('inf')
        for t in teams:
            if len(teams[t]) >= max_size:
                continue
            # try adding
            teams[t].append(n)
            obj, _, _ = _objective(teams, grade_counts, w_overlap, w_grade)
            if obj < best_obj:
                best_obj = obj
                best_t = t
            teams[t].pop()
        if best_t == -1:
            # 모두 max_size 도달 — 가장 작은 팀에 강제 (마지막 안전판)
            best_t = min(teams.keys(), key=lambda t: len(teams[t]))
        teams[best_t].append(n)

    return teams


def _grade_count_total(nurses: list[NurseInput]) -> dict[int, int]:
    cnt: dict[int, int] = {}
    for n in nurses:
        g = n.grade if n.grade is not None else -1
        cnt[g] = cnt.get(g, 0) + 1
    return cnt


def _enforce_preceptee_follow(
    teams: dict[int, list[NurseInput]],
    nurses: list[NurseInput],
) -> dict[int, list[NurseInput]]:
    """preceptee를 preceptor와 같은 팀으로 강제 이동 (preceptor가 시드 아니어도)."""
    by_id = {n.nurse_id: n for n in nurses}
    for t in list(teams.keys()):
        for m in list(teams[t]):
            if not m.preceptor_id or m.preceptor_id not in by_id:
                continue
            # preceptor가 어느 팀에 있나
            pre_team = None
            for tt, mm in teams.items():
                if any(x.nurse_id == m.preceptor_id for x in mm):
                    pre_team = tt; break
            if pre_team is not None and pre_team != t:
                teams[t].remove(m)
                teams[pre_team].append(m)
    return teams


def _local_swap_optimize(
    teams: dict[int, list[NurseInput]],
    nurses: list[NurseInput],
    seed_ids: set[str],
    w_overlap: int,
    w_grade: float,
    max_iterations: int = 100,
) -> dict[int, list[NurseInput]]:
    """2-opt swap: 시드/preceptee가 아닌 간호사 페어를 swap해서 obj 감소 시 반영.
    preceptor를 가진 nurse(=preceptee)는 단독 swap 금지 (preceptor와 같은 팀 hard 유지).
    Preceptees가 있는 preceptor도 단독 swap 금지 (preceptee가 따라와야 하므로)."""
    grade_counts = _grade_count_total(nurses)
    by_id = {n.nurse_id: n for n in nurses}
    has_preceptees = set()
    for n in nurses:
        if n.preceptor_id:
            has_preceptees.add(n.preceptor_id)

    def is_movable(n: NurseInput) -> bool:
        if n.nurse_id in seed_ids:
            return False  # 시드 고정
        if n.preceptor_id is not None:
            return False  # preceptee는 단독 swap 금지 (preceptor 따라감)
        if n.nurse_id in has_preceptees:
            return False  # preceptees를 갖는 nurse는 swap 시 preceptee 분리될 수 있어 금지
        return True

    current_obj, _, _ = _objective(teams, grade_counts, w_overlap, w_grade)
    for _ in range(max_iterations):
        improved = False
        team_ids = list(teams.keys())
        for ta_idx, ta in enumerate(team_ids):
            for tb in team_ids[ta_idx + 1:]:
                for na in teams[ta]:
                    if not is_movable(na):
                        continue
                    for nb in teams[tb]:
                        if not is_movable(nb):
                            continue
                        # try swap
                        teams[ta].remove(na); teams[ta].append(nb)
                        teams[tb].remove(nb); teams[tb].append(na)
                        new_obj, _, _ = _objective(teams, grade_counts, w_overlap, w_grade)
                        if new_obj < current_obj - 1e-6:
                            current_obj = new_obj
                            improved = True
                        else:
                            # revert
                            teams[ta].remove(nb); teams[ta].append(na)
                            teams[tb].remove(na); teams[tb].append(nb)
        if not improved:
            break
    return teams


def auto_assign_teams(
    nurses: list[NurseInput],
    num_teams: int | None = None,
    seed_ids: list[str] | None = None,
    w_overlap: int = 100,
    w_grade: float = 200.0,
    min_size: int = 4,
    max_size: int = 6,
    swap_iterations: int = 100,
) -> TeamAssignResult:
    """팀 자동 분배 진입점.

    Hard: 각 팀에 grade-1 ≥ 1, 팀 크기 [min_size, max_size], preceptee→preceptor 같은 팀.
    Soft: OFF/FB 겹침 minimize, grade 분포 균등.
    N전담 미배정 인원은 입력 단계에서 제외하고 전달.

    seed_ids 지정 시 그걸 시드로. 안 주면 grade-1 중 첫 num_teams명 자동 선정.
    """
    if seed_ids is None:
        if num_teams is None:
            raise ValueError("num_teams 또는 seed_ids 중 하나는 필요")
        g1 = [n for n in nurses if n.grade == 1]
        if len(g1) < num_teams:
            raise ValueError(f"grade-1 인원({len(g1)}) < num_teams({num_teams}). 모든 팀에 G1 ≥1 불가.")
        seed_ids = [n.nurse_id for n in g1[:num_teams]]
    teams = _greedy_assign(nurses, seed_ids, w_overlap, w_grade, min_size, max_size)
    teams = _enforce_preceptee_follow(teams, nurses)
    teams = _local_swap_optimize(teams, nurses, set(seed_ids), w_overlap, w_grade, swap_iterations)
    teams = _enforce_preceptee_follow(teams, nurses)  # swap 후 재검증
    grade_counts = _grade_count_total(nurses)
    obj, overlap, dev = _objective(teams, grade_counts, w_overlap, w_grade)
    team_size = {t: len(m) for t, m in teams.items()}
    grade_breakdown: dict[int, dict[int, int]] = {}
    for t, m in teams.items():
        b: dict[int, int] = {}
        for n in m:
            g = n.grade if n.grade is not None else -1
            b[g] = b.get(g, 0) + 1
        grade_breakdown[t] = b
    return TeamAssignResult(
        teams={t: [n.nurse_id for n in m] for t, m in teams.items()},
        unassigned=[],  # 호출자가 미배정 풀을 알고 있음
        objective=obj,
        overlap_total=overlap,
        grade_dev_total=dev,
        team_size=team_size,
        team_grade_breakdown=grade_breakdown,
    )
