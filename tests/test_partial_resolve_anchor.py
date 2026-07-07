"""부분 재생성 '변경 최소화 앵커' 수식 PoC (설계문서 §4.1①).

fallback_objectives.build_fallback_stage3_objective_terms 에 추가된 앵커 항과
'동일한 수식'을 ortools 로 직접 세워, 다음을 증명한다:

1. 앵커만으로 재-solve 하면 원본이 그대로 재현된다(변경 0).
2. 한 명이 퇴사(자유 셀에서 제거 + coverage 공급 제외)하면,
   앵커가 나머지 인원의 원본 셀을 최대한 지키고 '빈자리를 메우는 데 필요한
   최소한'만 바꾼다.
3. 형태 B(touched_n)는 변경을 '되도록 적은 인원'에 몰아 tie-break 한다.

핵심 트릭: 원본 시프트 s*(n,d) 에 대해 X[n,d,s*] 가 곧 '원본 유지' 지시자.
  - 형태 A(셀):   obj += w_cell * X[n,d,s*]      (유지 보상)
  - 형태 B(인원): touched_n >= 1 - X[n,d,s*],  obj += -w_nurse * touched_n
"""
from __future__ import annotations

from ortools.sat.python import cp_model

# 시프트 인덱스: D, E, N, O
D_, E_, N_, O_ = 0, 1, 2, 3
SHIFTS = [D_, E_, N_, O_]
WORK = [D_, E_, N_]
NUM_DAYS = 4
NUM_NURSES = 4

# 원본 근무표(퇴사 전, 커버리지 D/E/N 각 1명 충족, 매일 1명 OFF)
#   n0=D, n1=E, n2=N, n3=O  (매일 동일)
ORIGINAL = {
    (n, d): shift
    for d in range(NUM_DAYS)
    for n, shift in enumerate([D_, E_, N_, O_])
}


def _build_base_model(resigned: set[int] | None):
    """X 변수 + one-hot + 커버리지(D/E/N>=1) 하드. 퇴사자는 전일 OFF 고정."""
    resigned = resigned or set()
    m = cp_model.CpModel()
    X = {}
    for n in range(NUM_NURSES):
        for d in range(NUM_DAYS):
            for s in SHIFTS:
                X[(n, d, s)] = m.NewBoolVar(f"x_{n}_{d}_{s}")
            m.Add(sum(X[(n, d, s)] for s in SHIFTS) == 1)
    # 퇴사자: 전 기간 OFF 고정(공급 제외)
    for n in resigned:
        for d in range(NUM_DAYS):
            m.Add(X[(n, d, O_)] == 1)
    # 커버리지 하드: 매일 D/E/N 각 >= 1
    for d in range(NUM_DAYS):
        for s in WORK:
            m.Add(sum(X[(n, d, s)] for n in range(NUM_NURSES)) >= 1)
    return m, X


def _add_anchor(m, X, free_nurses, w_cell: int, w_nurse: int):
    """설계문서 §4.1① 앵커 항과 동일 수식."""
    obj = []
    by_nurse: dict[int, list[tuple[int, int]]] = {}
    for n in free_nurses:
        for d in range(NUM_DAYS):
            s_star = ORIGINAL[(n, d)]
            if w_cell > 0:
                obj.append(w_cell * X[(n, d, s_star)])
            by_nurse.setdefault(n, []).append((d, s_star))
    if w_nurse > 0:
        for n, cells in by_nurse.items():
            touched = m.NewBoolVar(f"touched_{n}")
            for (d, s_star) in cells:
                m.Add(touched >= 1 - X[(n, d, s_star)])
            obj.append(-w_nurse * touched)
    return obj


def _solve(m, X, obj):
    m.Maximize(sum(obj))
    solver = cp_model.CpSolver()
    solver.parameters.num_search_workers = 1
    st = solver.Solve(m)
    assert st in (cp_model.OPTIMAL, cp_model.FEASIBLE)
    result = {}
    for n in range(NUM_NURSES):
        for d in range(NUM_DAYS):
            for s in SHIFTS:
                if solver.Value(X[(n, d, s)]) == 1:
                    result[(n, d)] = s
    return result


def _change_count(result, nurses):
    return sum(
        1 for n in nurses for d in range(NUM_DAYS)
        if result[(n, d)] != ORIGINAL[(n, d)]
    )


def test_anchor_reproduces_original_when_no_resignation():
    """퇴사 없음 + 앵커만 → 원본 그대로 재현(변경 0)."""
    m, X = _build_base_model(resigned=set())
    free = list(range(NUM_NURSES))
    obj = _add_anchor(m, X, free, w_cell=1, w_nurse=0)
    result = _solve(m, X, obj)
    assert _change_count(result, free) == 0


def test_anchor_fills_vacancy_with_minimal_change():
    """n0 퇴사 → 빈 D 자리를 최소 변경으로 메움. n1(E)/n2(N)는 원본 유지."""
    m, X = _build_base_model(resigned={0})
    free = [1, 2, 3]  # 비퇴사자만 자유
    obj = _add_anchor(m, X, free, w_cell=1, w_nurse=5)
    result = _solve(m, X, obj)

    # 커버리지 유지
    for d in range(NUM_DAYS):
        for s in WORK:
            assert sum(1 for n in range(NUM_NURSES) if result[(n, d)] == s) >= 1

    # n1, n2 는 원본 유지(E, N) — 앵커가 지킴
    for d in range(NUM_DAYS):
        assert result[(1, d)] == E_
        assert result[(2, d)] == N_

    # 빈 D 는 원래 OFF 였던 n3 가 흡수 → n3 만 바뀜(touched=1명)
    for d in range(NUM_DAYS):
        assert result[(3, d)] == D_

    # 변경은 n3 에만 국한(형태 B 효과)
    assert _change_count(result, [1, 2]) == 0
    assert _change_count(result, [3]) == NUM_DAYS


def test_form_b_concentrates_changes_on_fewer_nurses():
    """형태 B(w_nurse>0)는 동일 셀 변경 수라도 '적은 인원'에 몰아준다."""
    m, X = _build_base_model(resigned={0})
    free = [1, 2, 3]
    obj = _add_anchor(m, X, free, w_cell=1, w_nurse=5)
    result = _solve(m, X, obj)
    touched_nurses = sum(
        1 for n in free
        if any(result[(n, d)] != ORIGINAL[(n, d)] for d in range(NUM_DAYS))
    )
    assert touched_nurses == 1  # n3 한 명만
