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
            # grade_counts 는 None→-1 로 집계하므로 멤버 비교도 동일 정규화해야
            # None(미입력) 간호사가 0으로 오판돼 균등화에서 빠지지 않는다.
            cnt_t = sum(1 for n in members
                        if (n.grade if n.grade is not None else -1) == g)
            total += abs(cnt_t - target)
    return total


def _size_dev(teams: dict[int, list[NurseInput]]) -> float:
    """팀 크기의 L1 편차 합 (target=total/num_teams). 0이면 완전 균등."""
    k = len(teams)
    if k == 0:
        return 0.0
    total = sum(len(m) for m in teams.values())
    target = total / k
    return sum(abs(len(m) - target) for m in teams.values())


def _objective(teams: dict[int, list[NurseInput]], grade_counts: dict[int, int],
               w_overlap: int, w_grade: float,
               home_cluster: dict[str, int] | None = None,
               w_churn: float = 0.0, w_size: float = 0.0) -> tuple[float, int, float]:
    """obj = w_overlap·OFF겹침 + w_grade·grade편차 + w_size·인원편차 + w_churn·이동수.

    home_cluster(nurse_id→원래 cluster idx)와 w_churn 지정 시, 원래 cluster가 아닌 곳에
    배정된 간호사 수만큼 페널티 (옵션2: 불필요한 병동 이동 억제).
    w_size 지정 시 팀 크기 편차(_size_dev)도 최소화 — 인원 균등."""
    overlap = sum(_team_overlap_sum(m) for m in teams.values())
    dev = _grade_dev(teams, grade_counts)
    churn = 0
    if home_cluster and w_churn:
        for t, members in teams.items():
            for n in members:
                h = home_cluster.get(n.nurse_id)
                if h is not None and h != t:
                    churn += 1
    size_pen = w_size * _size_dev(teams) if w_size else 0.0
    obj = w_overlap * overlap + w_grade * dev + w_churn * churn + size_pen
    return obj, overlap, dev


def _build_seeds_and_followers(
    nurses: list[NurseInput],
    seed_ids: list[str],
    fixed_ids: set[str] | None = None,
    allow_non_g1_seed: bool = False,
) -> tuple[list[NurseInput], dict[str, list[NurseInput]], list[NurseInput]]:
    """지정된 seed_ids로 시드 추출 + preceptee → preceptor 매핑 + 잔여 인원.

    fixed_ids(미참여=현재 클러스터 고정 인원)는 follower/remaining 에서 제외 — 호출부가 직접 배치.

    allow_non_g1_seed=True 면 grade!=1 시드도 허용한다. G1 인원이 팀 수보다 적을 때
    auto_assign_teams 가 비-G1 앵커로 시드를 보충하는 graceful 경로용(막지 않고 있는 대로 분배).
    """
    fixed_ids = fixed_ids or set()
    by_id = {n.nurse_id: n for n in nurses}
    seeds: list[NurseInput] = []
    for sid in seed_ids:
        if sid not in by_id:
            raise ValueError(f"seed nurse_id {sid} not in pool")
        s = by_id[sid]
        if s.grade != 1 and not allow_non_g1_seed:
            raise ValueError(f"seed {sid} grade={s.grade} != 1 (시드는 grade-1이어야 함)")
        seeds.append(s)
    followers: dict[str, list[NurseInput]] = {s.nurse_id: [] for s in seeds}
    assigned_ids: set[str] = set(s.nurse_id for s in seeds)
    # preceptees 매핑 (preceptor가 시드인 경우만; 고정 인원은 제외 — 호출부가 직접 배치)
    for n in nurses:
        if (
            n.preceptor_id
            and n.preceptor_id in followers
            and n.nurse_id not in fixed_ids
        ):
            followers[n.preceptor_id].append(n)
            assigned_ids.add(n.nurse_id)
    remaining = [
        n for n in nurses
        if n.nurse_id not in assigned_ids and n.nurse_id not in fixed_ids
    ]
    return seeds, followers, remaining


def _greedy_assign(
    nurses: list[NurseInput],
    seed_ids: list[str],
    w_overlap: int,
    w_grade: float,
    min_size: int,
    max_size: int,
    max_sizes: list[int] | None = None,
    min_sizes: list[int] | None = None,
    home_cluster: dict[str, int] | None = None,
    w_churn: float = 0.0,
    fixed: dict[str, int] | None = None,
    w_size: float = 0.0,
    allow_non_g1_seed: bool = False,
) -> dict[int, list[NurseInput]]:
    """1단계: 시드 + preceptee 고정 후 잔여를 greedy로 분배.
    팀 수 = len(seed_ids). 시드 외 grade-1은 일반 풀에 섞임 (정상).

    max_sizes/min_sizes 가 주어지면 클러스터별(인덱스=시드 순서) 상/하한을 적용한다
    (옵션2 그룹별 정원). 없으면 균일 min_size/max_size.
    home_cluster/w_churn: 원래 cluster 유지 보상(옵션2 이동 억제).
    fixed(nurse_id→cluster idx): 미참여=현재 클러스터 고정. 미리 배치하고 잔여 분배에서 제외.
    """
    fixed = fixed or {}
    by_id = {n.nurse_id: n for n in nurses}
    seeds, followers, remaining = _build_seeds_and_followers(
        nurses, seed_ids, fixed_ids=set(fixed.keys()),
        allow_non_g1_seed=allow_non_g1_seed,
    )
    teams: dict[int, list[NurseInput]] = {}
    for i, s in enumerate(seeds):
        teams[i] = [s] + followers[s.nurse_id]
    # 고정 인원 pre-place (현재 클러스터에 그대로)
    for nid, ci in fixed.items():
        if nid in by_id and ci in teams:
            teams[ci].append(by_id[nid])

    grade_counts = _grade_count_total(nurses)
    cap = lambda t: (max_sizes[t] if max_sizes else max_size)  # noqa: E731

    remaining_sorted = sorted(
        remaining,
        key=lambda n: (-(n.grade or -1), -len(n.off_days) - len(n.fb_days)),
    )
    for n in remaining_sorted:
        best_t = -1
        best_obj = float('inf')
        for t in teams:
            if len(teams[t]) >= cap(t):
                continue
            teams[t].append(n)
            obj, _, _ = _objective(teams, grade_counts, w_overlap, w_grade,
                                   home_cluster, w_churn, w_size)
            if obj < best_obj:
                best_obj = obj
                best_t = t
            teams[t].pop()
        if best_t == -1:
            # 모두 cap 도달 — 가장 여유 있는(자기 cap 대비 가장 덜 찬) 팀에 강제
            best_t = min(teams.keys(), key=lambda t: len(teams[t]) - cap(t))
        teams[best_t].append(n)

    if min_sizes:
        _min_fill(teams, seed_ids, nurses, min_sizes, w_overlap, w_grade,
                  grade_counts, home_cluster, w_churn, set(fixed.keys()))
    return teams


def _min_fill(
    teams: dict[int, list[NurseInput]],
    seed_ids: list[str],
    nurses: list[NurseInput],
    min_sizes: list[int],
    w_overlap: int,
    w_grade: float,
    grade_counts: dict[int, int],
    home_cluster: dict[str, int] | None = None,
    w_churn: float = 0.0,
    fixed_ids: set[str] | None = None,
) -> None:
    """클러스터별 하한(min_sizes) 미달 시, 자기 하한 위에 있는 클러스터에서
    movable 간호사를 objective 증가 최소가 되도록 옮겨 채운다 (in-place)."""
    seedset = set(seed_ids)
    fixed_ids = fixed_ids or set()
    has_pre = {n.preceptor_id for n in nurses if n.preceptor_id}

    def movable(n: NurseInput) -> bool:
        return (n.nurse_id not in seedset
                and n.nurse_id not in fixed_ids
                and n.preceptor_id is None
                and n.nurse_id not in has_pre)

    for _ in range(1000):  # 안전 상한
        deficit = [t for t in teams if len(teams[t]) < min_sizes[t]]
        if not deficit:
            break
        t_def = deficit[0]
        best = None  # (obj, t_src, nurse)
        for t_src in teams:
            if t_src == t_def or len(teams[t_src]) <= min_sizes[t_src]:
                continue
            for n in list(teams[t_src]):
                if not movable(n):
                    continue
                teams[t_src].remove(n); teams[t_def].append(n)
                obj, _, _ = _objective(teams, grade_counts, w_overlap, w_grade,
                                       home_cluster, w_churn)
                teams[t_def].remove(n); teams[t_src].append(n)
                if best is None or obj < best[0]:
                    best = (obj, t_src, n)
        if best is None:
            break  # 더 옮길 movable 없음 (하한 달성 불가 — 호출부가 tolerance로 흡수)
        _, t_src, n = best
        teams[t_src].remove(n); teams[t_def].append(n)


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
    home_cluster: dict[str, int] | None = None,
    w_churn: float = 0.0,
    fixed_ids: set[str] | None = None,
    w_size: float = 0.0,
) -> dict[int, list[NurseInput]]:
    """2-opt swap: 시드/preceptee가 아닌 간호사 페어를 swap해서 obj 감소 시 반영.
    preceptor를 가진 nurse(=preceptee)는 단독 swap 금지 (preceptor와 같은 팀 hard 유지).
    Preceptees가 있는 preceptor도 단독 swap 금지 (preceptee가 따라와야 하므로).
    fixed_ids(미참여 고정)도 swap 금지."""
    grade_counts = _grade_count_total(nurses)
    by_id = {n.nurse_id: n for n in nurses}
    fixed_ids = fixed_ids or set()
    has_preceptees = set()
    for n in nurses:
        if n.preceptor_id:
            has_preceptees.add(n.preceptor_id)

    def is_movable(n: NurseInput) -> bool:
        if n.nurse_id in seed_ids or n.nurse_id in fixed_ids:
            return False  # 시드/고정 인원
        if n.preceptor_id is not None:
            return False  # preceptee는 단독 swap 금지 (preceptor 따라감)
        if n.nurse_id in has_preceptees:
            return False  # preceptees를 갖는 nurse는 swap 시 preceptee 분리될 수 있어 금지
        return True

    current_obj, _, _ = _objective(teams, grade_counts, w_overlap, w_grade,
                                    home_cluster, w_churn, w_size)
    for _ in range(max_iterations):
        # 한 번의 개선 swap을 적용하면 즉시 스캔을 재시작한다.
        # (accept 시 teams 멤버십이 바뀌므로 변형된 리스트를 계속 순회하면
        #  revert 의 remove() 가 'x not in list' 로 깨진다 — 스냅샷 순회 + break)
        found = False
        team_ids = list(teams.keys())
        for ta_idx, ta in enumerate(team_ids):
            for tb in team_ids[ta_idx + 1:]:
                for na in list(teams[ta]):
                    if not is_movable(na):
                        continue
                    for nb in list(teams[tb]):
                        if not is_movable(nb):
                            continue
                        # try swap
                        teams[ta].remove(na); teams[ta].append(nb)
                        teams[tb].remove(nb); teams[tb].append(na)
                        new_obj, _, _ = _objective(teams, grade_counts, w_overlap, w_grade,
                                                   home_cluster, w_churn, w_size)
                        if new_obj < current_obj - 1e-6:
                            current_obj = new_obj
                            found = True
                            break
                        # revert (멤버십 복원 — 순서만 바뀜)
                        teams[ta].remove(nb); teams[ta].append(na)
                        teams[tb].remove(na); teams[tb].append(nb)
                    if found:
                        break
                if found:
                    break
            if found:
                break
        if not found:
            break
    return teams


def _balance_sizes(
    teams: dict[int, list[NurseInput]],
    nurses: list[NurseInput],
    seed_ids: set[str],
    w_overlap: int,
    w_grade: float,
    w_size: float,
    grade_counts: dict[int, int],
    home_cluster: dict[str, int] | None = None,
    w_churn: float = 0.0,
    fixed_ids: set[str] | None = None,
    max_sizes: list[int] | None = None,
    min_sizes: list[int] | None = None,
) -> dict[int, list[NurseInput]]:
    """인원 균등화 — 1:1 swap이 못 하는 크기 조정. 가장 큰 팀에서 movable 1명을
    가장 작은 팀으로 이동(obj 최소가 되는 인원 선택)해 max−min ≤ 1 까지.

    movable: 시드/고정/프리셉티/프리셉터(짝) 제외 → G1 시드는 안 옮겨져 각 팀 G1≥1 유지,
    프리셉터 짝도 분리되지 않음. max_sizes/min_sizes(옵션2 정원밴드) 범위는 위반하지 않는다.
    """
    fixed_ids = fixed_ids or set()
    has_pre = {n.preceptor_id for n in nurses if n.preceptor_id}

    def movable(n: NurseInput) -> bool:
        return (n.nurse_id not in seed_ids
                and n.nurse_id not in fixed_ids
                and n.preceptor_id is None
                and n.nurse_id not in has_pre)

    def lo(t: int) -> int:
        return min_sizes[t] if min_sizes else 0

    def hi(t: int) -> int:
        return max_sizes[t] if max_sizes else 10**9

    for _ in range(1000):  # 안전 상한
        sizes = {t: len(m) for t, m in teams.items()}
        t_big = max(sizes, key=lambda t: sizes[t])
        t_small = min(sizes, key=lambda t: sizes[t])
        if sizes[t_big] - sizes[t_small] <= 1:
            break
        # 정원밴드 위반 방지: 큰 팀이 하한 초과 & 작은 팀이 상한 미만일 때만
        if sizes[t_big] <= lo(t_big) or sizes[t_small] >= hi(t_small):
            break
        best = None  # (obj, nurse)
        for n in list(teams[t_big]):
            if not movable(n):
                continue
            teams[t_big].remove(n); teams[t_small].append(n)
            obj, _, _ = _objective(teams, grade_counts, w_overlap, w_grade,
                                   home_cluster, w_churn, w_size)
            teams[t_small].remove(n); teams[t_big].append(n)
            if best is None or obj < best[0]:
                best = (obj, n)
        if best is None:
            break  # 큰 팀에 옮길 movable 없음
        teams[t_big].remove(best[1]); teams[t_small].append(best[1])
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
    max_sizes: list[int] | None = None,
    min_sizes: list[int] | None = None,
    home_cluster: dict[str, int] | None = None,
    w_churn: float = 0.0,
    fixed: dict[str, int] | None = None,
    w_size: float = 50.0,
) -> TeamAssignResult:
    """팀 자동 분배 진입점.

    Hard: 팀 크기 [min_size, max_size], preceptee→preceptor 같은 팀.
    Soft: 각 팀에 grade-1 ≥ 1(가능한 만큼), OFF/FB 겹침 minimize, grade 분포 균등, 인원 균등.
    N전담 미배정 인원은 입력 단계에서 제외하고 전달.

    seed_ids 지정 시 그걸 시드로. 안 주면 grade-1 중 첫 num_teams명 자동 선정.
      G1 인원이 num_teams 보다 적으면 막지 않고(과거엔 ValueError) 비-G1 앵커로 시드를
      보충해 전원 팀 배정을 보장한다 — '이동자 팀마다 G1' 은 best-effort, 없으면 없는 대로.
    max_sizes/min_sizes (클러스터=시드 순서) 지정 시 클러스터별 정원 밴드 적용(옵션2).
    """
    allow_non_g1_seed = False
    if seed_ids is None:
        if num_teams is None:
            raise ValueError("num_teams 또는 seed_ids 중 하나는 필요")
        g1 = [n for n in nurses if n.grade == 1]
        seeds_sel = list(g1[:num_teams])
        if len(seeds_sel) < num_teams:
            # G1 부족: 막지 않고(과거엔 ValueError) 비-G1 앵커로 시드를 보충한다.
            #   "이동자 팀마다 G1" 은 가능한 만큼만(soft) 충족하고, 부족분은 정규(grade-2)
            #   →신규 순으로 앵커를 세워 전원이 숫자팀을 받게 한다(없으면 없는 대로 분배).
            #   프리셉티는 시드 금지 — preceptor 와 같은 팀을 따라가야 하므로.
            allow_non_g1_seed = True
            chosen = {n.nurse_id for n in seeds_sel}
            fillers = sorted(
                (n for n in nurses
                 if n.nurse_id not in chosen and not n.preceptor_id),
                key=lambda n: (n.grade is None or n.grade == 0, str(n.nurse_id)),
            )
            for n in fillers:
                if len(seeds_sel) >= num_teams:
                    break
                seeds_sel.append(n)
            # 인원 자체가 팀 수보다 적으면 가능한 팀 수로 축소(빈 팀 생성 방지)
            num_teams = len(seeds_sel)
            print(
                f"[TeamAssign] G1({len(g1)}) < 팀수 → 비-G1 앵커 보충(graceful): "
                f"seeds={len(seeds_sel)} (요청 num_teams 일부는 비-G1 시드)"
            )
        seed_ids = [n.nurse_id for n in seeds_sel]
    if max_sizes is not None and len(max_sizes) != len(seed_ids):
        raise ValueError("max_sizes 길이가 클러스터 수와 다릅니다.")
    if min_sizes is not None and len(min_sizes) != len(seed_ids):
        raise ValueError("min_sizes 길이가 클러스터 수와 다릅니다.")
    _fixed_ids = set(fixed.keys()) if fixed else None
    grade_counts = _grade_count_total(nurses)
    teams = _greedy_assign(nurses, seed_ids, w_overlap, w_grade, min_size, max_size,
                           max_sizes=max_sizes, min_sizes=min_sizes,
                           home_cluster=home_cluster, w_churn=w_churn, fixed=fixed,
                           w_size=w_size, allow_non_g1_seed=allow_non_g1_seed)
    teams = _enforce_preceptee_follow(teams, nurses)
    # 인원 균등화 (1:1 swap이 못 하는 크기 조정) → 정밀 swap → 짝 재검증
    teams = _balance_sizes(teams, nurses, set(seed_ids), w_overlap, w_grade, w_size,
                           grade_counts, home_cluster=home_cluster, w_churn=w_churn,
                           fixed_ids=_fixed_ids, max_sizes=max_sizes, min_sizes=min_sizes)
    teams = _local_swap_optimize(teams, nurses, set(seed_ids), w_overlap, w_grade,
                                 swap_iterations, home_cluster=home_cluster,
                                 w_churn=w_churn, fixed_ids=_fixed_ids, w_size=w_size)
    teams = _enforce_preceptee_follow(teams, nurses)  # swap 후 재검증
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
