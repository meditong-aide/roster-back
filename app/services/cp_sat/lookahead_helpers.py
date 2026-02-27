"""룩어헤드(Lookahead) 확장 호라이즌 계산 헬퍼.

당월 D_phys + 룩어헤드 K_lookahead 일을 확장 구간으로 두었을 때,
D_ext, leave_ext, 물리 구간 여부 등을 순수 함수로 제공한다.
기존 로직 변경을 최소화하기 위해 cp_model에 의존하지 않는다.
"""

from __future__ import annotations


def get_D_ext(D_phys: int, K_lookahead: int) -> int:
    """확장 일수 = 물리 일수 + 룩어헤드 일수.

    Args:
        D_phys: 당월 물리 일수 (rs.num_days).
        K_lookahead: 룩어헤드 일수 (0이면 비활성).

    Returns:
        D_ext: D_phys + K_lookahead. K_lookahead가 0이면 D_phys.

    예시:
        get_D_ext(31, 2) -> 33
        get_D_ext(31, 0) -> 31
    """
    if K_lookahead <= 0:
        return D_phys
    return D_phys + K_lookahead


def compute_leave_ext(
    leave: list[int],
    D_phys: int,
    D_ext: int,
) -> list[int]:
    """간호사별 확장 구간 마지막 일자(inclusive).

    당월 말까지 근무 가능한 간호사(leave[n] >= D_phys - 1)는
    룩어헤드 끝날까지 변수를 갖고, 당월 중 퇴사자는 leave[n] 유지.

    Args:
        leave: 기존 leave (inclusive 마지막 일자, 0..D_phys-1).
        D_phys: 당월 물리 일수.
        D_ext: 확장 일수 (D_phys + K_lookahead).

    Returns:
        leave_ext[n]: inclusive 마지막 일자. leave[n] >= D_phys - 1 이면 D_ext - 1, else leave[n].

    예시:
        D_phys=31, D_ext=33, leave=[30, 15, 30] -> [32, 15, 32]
    """
    last_phys = D_phys - 1
    last_ext = D_ext - 1
    return [
        last_ext if l >= last_phys else l
        for l in leave
    ]


def physical_range(D_phys: int):
    """당월(물리) 일자 범위. 월총량 합산 시 사용.

    Args:
        D_phys: 당월 물리 일수.

    Returns:
        range(0, D_phys) (0-based, D_phys 미포함).
    """
    return range(D_phys)


def is_physical_day(d: int, D_phys: int) -> bool:
    """일자 d가 물리 구간(당월)에 속하는지 여부.

    Args:
        d: 0-based 일자 인덱스.
        D_phys: 당월 물리 일수.

    Returns:
        d < D_phys 이면 True.
    """
    return 0 <= d < D_phys


def month_total_day_range(T0: int, T1: int, D_phys: int) -> range:
    """월총량(min/max OFF, max N) 합산에 쓸 일자 범위.

    룩어헤드가 있어도 월총량은 당월 D_phys까지만 합산한다.
    inclusive 규약: T0 ~ min(T1, D_phys-1).

    Args:
        T0: 해당 간호사 첫 근무일(join[n]).
        T1: 확장 구간 마지막 일자(leave_ext[n]). 월총량에서는 물리 끝으로 자른다.
        D_phys: 당월 물리 일수.

    Returns:
        range(T0, min(T1, D_phys - 1) + 1)
    """
    last_phys = D_phys - 1
    end = min(T1, last_phys)
    if T0 > end:
        return range(0)  # empty
    return range(T0, end + 1)
