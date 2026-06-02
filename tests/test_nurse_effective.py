"""간호사 시점-effective 헬퍼 + reconcile 회귀 테스트 (그룹 변경 모델 Phase 1~2).

핵심 회귀 가드:
- target_shift_types=[] (컬럼 기본값) 은 "모든 시프트 금지" override 가 아니라 "미설정" →
  Nurse 폴백. 이를 override 로 오인하면 프리셉티 등이 배정 불가가 된다.
- reconcile 이 빈 리스트를 불일치로 잡지 않아야 한다 (false positive 방지).

참조: docs/NURSE_GROUP_CHANGE_MODEL.md, NURSE_ASSIGNMENT_CRON_DESIGN.md
"""
from __future__ import annotations

from datetime import date

import pytest

from db.models import Nurse, NurseAssignment
from services.nurse_effective import (
    _is_unset_override,
    get_active_assignment,
    get_active_assignments_batch,
    get_nurse_effective_attr,
    apply_effective_attrs_to_nurse,
)
from services.assignment_service import reconcile_nurse_attrs


GID_A = "groupA"
GID_B = "groupB"
OFFICE = "office1"


# ── 1. _is_unset_override 순수 단위 ──────────────────────────

@pytest.mark.parametrize("val,expected", [
    (None, True),
    ([], True),
    ("", True),
    ({}, True),
    (0, False),        # 의미값 (예: weekend_off=0)
    (False, False),    # 의미값
    ("D", False),
    (["D"], False),
    (1, False),
    (True, False),
    ("groupB", False),
])
def test_is_unset_override(val, expected):
    assert _is_unset_override(val) is expected


# ── fixture: 간호사 + assignment 헬퍼 ────────────────────────

def _mk_nurse(db, nurse_id="n1", group_id=GID_A, **kw):
    n = Nurse(nurse_id=nurse_id, group_id=group_id, office_id=OFFICE,
              account_id=kw.pop("account_id", f"acc_{nurse_id}"),
              name=kw.pop("name", "테스트"), active=kw.pop("active", 1), **kw)
    db.add(n)
    db.flush()
    return n


def _mk_assignment(db, nurse_id="n1", *, reason="병동이동", status="active",
                   start=date(2026, 1, 1), end=None, source=GID_A, target=GID_B, **targets):
    a = NurseAssignment(
        nurse_id=nurse_id, source_group_id=source, target_group_id=target,
        office_id=OFFICE, start_date=start, end_date=end, reason=reason, status=status,
        **targets,
    )
    db.add(a)
    db.flush()
    return a


# ── 2. get_nurse_effective_attr ─────────────────────────────

def test_effective_attr_fallback_when_no_assignment(db):
    n = _mk_nurse(db, group_id=GID_A)
    val = get_nurse_effective_attr(db, n, "group_id", date(2026, 6, 1))
    assert val == GID_A


def test_effective_attr_override_when_active(db):
    n = _mk_nurse(db, group_id=GID_A)
    _mk_assignment(db, target=GID_B, start=date(2026, 5, 1))
    val = get_nurse_effective_attr(db, n, "group_id", date(2026, 6, 1))
    assert val == GID_B


def test_effective_attr_empty_list_falls_back(db):
    """target_shift_types=[] 는 override 아님 → Nurse 폴백 (핵심 회귀).

    참고: allowed_shifts 는 Nurse 실컬럼이 아니라 런타임 주입 속성이라
    getattr 기본값 None 으로 폴백된다.
    """
    n = _mk_nurse(db, group_id=GID_A)
    _mk_assignment(db, reason="프리셉티", target=None, target_shift_types=[])
    val = get_nurse_effective_attr(db, n, "allowed_shifts", date(2026, 6, 1))
    assert val is None  # [] override 무시, Nurse 폴백(getattr 기본 None)


def test_effective_attr_out_of_range_falls_back(db):
    """assignment 기간 밖이면 폴백."""
    n = _mk_nurse(db, group_id=GID_A)
    _mk_assignment(db, target=GID_B, start=date(2026, 7, 1))  # 미래
    val = get_nurse_effective_attr(db, n, "group_id", date(2026, 6, 1))
    assert val == GID_A


# ── 3. apply_effective_attrs_to_nurse ───────────────────────

def test_apply_skips_empty_list(db):
    n = _mk_nurse(db, group_id=GID_A)
    a = _mk_assignment(db, reason="프리셉티", target=None, target_shift_types=[])
    apply_effective_attrs_to_nurse(db, n, date(2026, 6, 1), assignment=a)
    assert getattr(n, "allowed_shifts", None) is None  # [] 안 박힘
    assert n.group_id == GID_A                         # target=None 안 박힘


def test_apply_applies_real_value(db):
    n = _mk_nurse(db, group_id=GID_A)
    a = _mk_assignment(db, target=GID_B)
    apply_effective_attrs_to_nurse(db, n, date(2026, 6, 1), assignment=a)
    assert n.group_id == GID_B


# ── 4. reconcile_nurse_attrs (빈 리스트 false-positive 회귀) ──

def test_reconcile_no_mismatch_for_empty_list(db):
    """프리셉티 target_shift_types=[] 가 불일치로 안 잡혀야 한다."""
    _mk_nurse(db, nurse_id="p1", group_id=GID_A)
    _mk_assignment(db, nurse_id="p1", reason="프리셉티", target=None, target_shift_types=[])
    result = reconcile_nurse_attrs(db, as_of=date(2026, 6, 1))
    assert result["mismatch_count"] == 0, result["mismatches"]


def test_reconcile_detects_real_mismatch(db):
    """실제 불일치(Nurse.group != active transfer target)는 잡아야 한다."""
    _mk_nurse(db, nurse_id="m1", group_id=GID_A)
    _mk_assignment(db, nurse_id="m1", reason="병동이동", target=GID_B, start=date(2026, 5, 1))
    result = reconcile_nurse_attrs(db, as_of=date(2026, 6, 1))
    attrs = {m["attr"] for m in result["mismatches"] if m["nurse_id"] == "m1"}
    assert "group_id" in attrs


def test_reconcile_batch_lookup_latest(db):
    """여러 활성 row 중 가장 최근 start_date 우선."""
    _mk_nurse(db, nurse_id="b1", group_id=GID_A)
    _mk_assignment(db, nurse_id="b1", target="oldgrp", start=date(2026, 1, 1))
    _mk_assignment(db, nurse_id="b1", target="newgrp", start=date(2026, 5, 1))
    cache = get_active_assignments_batch(db, ["b1"], date(2026, 6, 1))
    assert cache["b1"].target_group_id == "newgrp"
