"""attach_n_exact_to_nurses 그룹 스코프 회귀.

버그: 근무자관리 표(/nurses?year&month)의 n_exact 주입이 cross-group 폴백을 둬서
파견 inbound 간호사가 home 그룹(예 9B)의 나이트개수를 조회 중인 병동(9A) 표에서도
끌어와 보였음. 수정: view_group_id(조회 병동) 기준으로만 주입, 폴백 제거.
"""
from __future__ import annotations

from db.models import NurseMonthlyLimit
from services.nurse_service import attach_n_exact_to_nurses


def _limit(db, nid, gid, n_exact):
    db.add(NurseMonthlyLimit(
        nurse_id=nid, group_id=gid, year=2026, month=6, n_exact=n_exact,
    ))
    db.flush()


def test_inbound_n_exact_not_leaked_to_view_group(db):
    """9B에만 한도가 있는 inbound 간호사를 9A로 조회 → 9A엔 주입 안 됨(누수 차단)."""
    _limit(db, "450065", "9B", 1)
    nurses = [{"nurse_id": "450065", "group_id": "9B"}]  # inbound dict(group_id=home)
    attach_n_exact_to_nurses(db, nurses, 2026, 6, view_group_id="9A")
    assert nurses[0].get("n_exact") is None


def test_view_group_injects_own_limit(db):
    """같은 간호사를 home(9B)으로 조회 → 9B 한도 주입."""
    _limit(db, "450065", "9B", 1)
    nurses = [{"nurse_id": "450065", "group_id": "9B"}]
    attach_n_exact_to_nurses(db, nurses, 2026, 6, view_group_id="9B")
    assert nurses[0]["n_exact"] == 1


def test_no_view_group_uses_own_group(db):
    """view_group_id 미지정(master office-wide 등) → 각 nurse 자기 group_id 기준."""
    _limit(db, "450065", "9B", 1)
    nurses = [{"nurse_id": "450065", "group_id": "9B"}]
    attach_n_exact_to_nurses(db, nurses, 2026, 6)
    assert nurses[0]["n_exact"] == 1


def test_home_member_injected_in_view(db):
    """home 멤버는 조회 병동 한도가 정상 주입."""
    _limit(db, "h1", "9A", 3)
    nurses = [{"nurse_id": "h1", "group_id": "9A"}]
    attach_n_exact_to_nurses(db, nurses, 2026, 6, view_group_id="9A")
    assert nurses[0]["n_exact"] == 3
