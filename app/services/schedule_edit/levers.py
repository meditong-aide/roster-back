"""레버 — '무엇을 손해 보면 무엇을 얻는지' 찾아 보여 주고, 사용자가 고르면 그만큼만 허용해 다시 탐색한다.

근무표는 미분이 안 되는 이산 문제라 역전파 대신 유한 차분을 쓴다(사용자 지시 11-02). 레버 대상 규칙에
넘김(slack)을 허용하되 넘김 1 마다 비용 `BEND_COST`(주 loss 단위)를 물리고 다시 푼다. 주 loss(칸·기간)가
그 비용보다 더 줄면 그 넘김 묶음이 레버다. 하나 찾을 때마다 같은 묶음을 금지하고 다시 풀어 서로 다른
레버를 찾는다. 사용자가 레버를 고르면 `propose_edits(allow=lever.allow)` 로 다시 탐색한다 — 손해를 하나씩
골라 가며 품질을 맞춘다.

반사실(what-if) 설명과 같은 방향 — OptiChat(INFORMS J. Data Science 2025) · Korikov·Beck(CP 2021) ·
Senthooran(CP 2021). 레버 선택 기록은 병동별 계수 학습(기획 8장)의 재료다.

★ 하드락·허용 근무형·기피 근무·주말휴무는 레버가 아니다(자격·약속된 근무 형태 · 하드락 정책).
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

from ortools.sat.python import cp_model

from services.schedule_edit.context import EditContext
from services.schedule_edit.explain import Bend, add_rules_soft, bend_text
from services.schedule_edit.model import (
    EditModel, add_cell_vars, add_requests, add_span, edit_codes, forbid_same_people, free_cells, grid_relief,
    is_waived, model_relief, new_solver, read_solution, requested_cells,
)
from services.schedule_edit.requests import EditRequest
from services.schedule_edit.rules import Grid, RuleSet, build_rules, measure, new_violations

logger = logging.getLogger(__name__)

#: 원티드는 넣지 않는다 — 원티드 칸은 수간호사가 그 칸을 직접 짚을 때만 바꾼다(사용자 지시 11-03). 다른 요청을
#: 풀려고 원티드를 넘기자는 카드를 내밀지 않는다(불가 설명에서만 알린다 · `explain.Explanation.sentences`).
LEVER_FAMILIES = {"OFF수", "개인한도", "인원최소", "인원최대", "등급최소", "N간격", "같은근무연속",
                  "고립근무", "상호배제"}
#: 넘김 1 의 비용 — 주 loss 단위(칸 1 = 2). 6 = 칸 3개어치. 병동별 학습으로 조정할 계수(P4).
BEND_COST = 6
MAX_LEVERS = 3
#: 넘기는 설정 하나의 비용 — 주 loss·넘김량 합보다 커서 '넘기는 설정 개수'가 먼저 줄어든다
FLAG_COST = 1000
#: 레버 찾기 전체 시간 예산과 풀이 한 번 상한
LEVER_SECONDS = 6.0
SOLVE_SECONDS = 2.0
MAX_CHANGES = 20


def lever_allow(allow: Optional[Dict[Tuple, int]]) -> Dict[Tuple, int]:
    """레버 대상 규칙의 넘김만 남긴다 — 밖에서 들어온 허용량(화면이 돌려보낸 JSON 등)에 하드락 위치가
    섞여도 모델·검증 어디에서도 봐주지 않게 하는 관문(검토 11-02 M2). 버린 것은 경고로 남긴다."""
    out, dropped = {}, []
    for key, amount in (allow or {}).items():
        if isinstance(key, tuple) and key and key[0] in LEVER_FAMILIES and int(amount) > 0:
            out[key] = int(amount)
        else:
            dropped.append(key)
    if dropped:
        logger.warning("[schedule_edit] 레버가 아닌 허용량 버림: %s", dropped[:5])
    return out


@dataclass
class Lever:
    #: 적용할 허용량 {규칙 위치 key: 양} — 지금까지 고른 레버와 합쳐 `propose_edits(allow=…)` 에 넘긴다
    allow: Dict[Tuple, int]
    #: 잃는 것 — 넘기는 설정 문장
    lose: List[str]
    #: 얻는 것 — (지금 최선, 레버 적용 후). 지금 후보가 없으면 앞 값이 None
    changes: Tuple[Optional[int], int]
    span: Tuple[Optional[int], int]
    #: 레버를 적용한 해에서 나빠지는 품질(분배 격차·근무 모양)
    quality: List[str] = field(default_factory=list)


def find_levers(ctx: EditContext, requests: Sequence[EditRequest], best=None, *, rules: Optional[RuleSet] = None,
                allow: Optional[Dict[Tuple, int]] = None, max_levers: int = MAX_LEVERS,
                seconds: float = LEVER_SECONDS, screen=None, avoid_people: Sequence = ()) -> List[Lever]:
    """best: 지금 최선 후보(`Candidate`) — 있으면 그보다 loss 가 줄어드는 레버만, 없으면 '가능해지는' 레버.

    screen·avoid_people 은 재탐색(`propose_edits`)과 같게 — 화면에서 나빠지는 해와 이 대화에서 넘긴 사람
    묶음은 레버 해로도 쓰지 않는다. 안 그러면 레버를 골라 다시 찾았을 때 후보가 안 나온다(검토 11-02 M4).
    """
    rules = rules or build_rules(ctx)
    allow = lever_allow(allow)
    em, slacks = _lever_model(ctx, rules, list(requests), allow)
    for people in avoid_people:
        forbid_same_people(em, set(people))
    if best is not None:
        em.model.Add(em.loss() <= best.loss - 1)
    flags = _used_flags(em, slacks)
    # 넘기는 설정 개수가 먼저 — 사용자가 손해를 하나씩 골라 가게(사용자 지시 11-02). 개수 상한을 가정으로
    # 걸던 방식은 상한 안의 해를 2초 안에 못 찾고 시간만 쓰는 일이 잦아(dev 무작위 72건 중 4건 +6초 · 11-02)
    # 목적식 맨 앞 항으로 옮겼다.
    em.model.Minimize(FLAG_COST * sum(flags.values()) + em.loss() + BEND_COST * sum(s for s, _f, _k in slacks))
    out: List[Lever] = []
    deadline = time.monotonic() + seconds
    for _ in range(max_levers * 3):
        if len(out) >= max_levers or deadline - time.monotonic() <= 0.2:
            break
        used, solver = _solve_once(em, slacks, min(SOLVE_SECONDS, deadline - time.monotonic()))
        if not used:
            break
        grid = read_solution(em, solver)
        lever = _to_lever(ctx, rules, em, grid, used, allow, best, list(requests), screen)
        if lever is None:
            # 이 해만 못 쓴다(화면에서 나빠짐 등) — 같은 넘김을 다른 사람으로 다시 찾는다
            forbid_same_people(em, {n for n, d in em.free if grid[n][d] != em.base[n][d]})
            continue
        out.append(lever)
        # 같은 묶음은 다시 안 내고, 다음 레버는 첫 레버보다 넘기는 설정이 많지 않게
        em.model.Add(sum(flags[k] for _f, k, _v in used) <= len(used) - 1)
        if len(out) == 1:
            em.model.Add(sum(flags.values()) <= len(used))
    return out


def _solve_once(em: EditModel, slacks, seconds: float):
    """한 번 풀어 쓴 넘김 목록. 해가 없거나 넘김 없이 풀렸으면 빈 목록(넘김 없는 더 나은 해는 레버가 아니다)."""
    solver = new_solver(seconds)
    if solver.Solve(em.model) not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        return [], solver
    return [(f, k, solver.Value(s)) for (s, f, k) in slacks if solver.Value(s) > 0], solver


def _lever_model(ctx: EditContext, rules: RuleSet, requests: List[EditRequest], allow: Dict[Tuple, int]):
    start = min(r.day for r in requests)
    em = EditModel(ctx=ctx, base=ctx.main, free=free_cells(ctx, range(start, ctx.days)), codes=edit_codes(ctx))
    add_cell_vars(em)
    slacks = add_rules_soft(em, rules, requested_cells(ctx, requests), allow,
                             softenable=lambda family: family in LEVER_FAMILIES,
                             relief=model_relief(em, rules, requests))
    add_requests(em, requests)
    add_span(em)
    em.model.Add(em.total_changes() <= MAX_CHANGES)
    return em, slacks


def _used_flags(em: EditModel, slacks) -> Dict[Tuple, object]:
    """넘김을 썼는가(0/1) — 같은 묶음 금지 조건에 쓴다."""
    flags = {}
    for s, _f, key in slacks:
        flag = em.model.NewBoolVar(f"used_{len(flags)}")
        em.model.Add(s >= 1).OnlyEnforceIf(flag)
        em.model.Add(s == 0).OnlyEnforceIf(flag.Not())
        flags[key] = flag
    return flags


def _to_lever(ctx: EditContext, rules: RuleSet, em: EditModel, grid: Grid, used, allow: Dict[Tuple, int],
              best, requests: List[EditRequest], screen=None) -> Optional[Lever]:
    # solver 가 levers 를 부르므로 지연 import
    from services.schedule_edit.solver import cell_change, quality_notes, screen_problems
    granted = dict(allow)
    for _f, key, amount in used:
        granted[key] = granted.get(key, 0) + amount
    if not _within(ctx, rules, grid, granted, requests):
        return None
    cells = [(n, d) for n, d in em.free if grid[n][d] != em.base[n][d]]
    regress = screen_problems(screen, [cell_change(ctx, n, d, grid[n][d]) for n, d in cells],
                              {**granted, **grid_relief(ctx, rules, requests, grid)})
    if regress:
        logger.info("[schedule_edit] 레버 해가 화면에서 나빠져 제외: %s", regress[:3])
        return None
    days = sorted({d for _n, d in cells})
    return Lever(
        allow={key: amount for _f, key, amount in used},
        lose=[bend_text(Bend(f, k, a), ctx.names) for f, k, a in used],
        changes=(best.changed_count if best else None, len(cells)),
        span=((best.span[1] - best.span[0]) if best else None, (days[-1] - days[0]) if days else 0),
        quality=quality_notes(ctx, rules, grid),
    )


def _within(ctx: EditContext, rules: RuleSet, grid: Grid, granted: Dict[Tuple, int],
            requests: List[EditRequest]) -> bool:
    """레버 해가 허용량 안에서만 넘겼는지 — 모델과 같은 부등식으로 다시 잰다(어긋나면 코드 결함)."""
    waived, relief = requested_cells(ctx, requests), grid_relief(ctx, rules, requests, grid)
    worse = {k: v for k, v in new_violations(measure(ctx, ctx.main, rules), measure(ctx, grid, rules)).items()
             if not is_waived(k, waived) and v[1] > v[0] + granted.get(k, 0) + relief.get(k, 0)}
    if worse:
        logger.error("[schedule_edit] 레버 해가 허용량을 넘음: %s", sorted(worse.items())[:5])
    return not worse
