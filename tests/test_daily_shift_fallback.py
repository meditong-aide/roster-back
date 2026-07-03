"""daily-shift 월 조회의 shift_manage 템플릿 fallback + 그룹 스코프 가드 테스트.

배경: shift_manage 기본 슬롯은 '시프트 관리' 화면(GET /shift-manage)을 열 때만 lazy
생성됐다. 그 화면을 거치지 않고 GET /daily-shift 로 바로 진입한 병동은 템플릿이 없어
"shift_manage 템플릿(RN)이 없습니다." 500 이 났다. 이제 daily-shift 초기화가 템플릿이
없으면 공용 기본값(D3/E3/N2/M0)을 직접 seeding 후 진행한다(어느 화면을 먼저 열든 복구).

추가로 GET/PUT 에 resolve_effective_group 가드를 붙여 외부 병동 접근을 403 으로 차단한다.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from db.models import Office, Group, ShiftManage, DailyShift
from routers.daily_shift import get_month


def _stub():
    return SimpleNamespace(
        nurse_id="hn1", account_id="hn1", office_id="o1", group_id="g1",
        is_master_admin=True, is_head_nurse=True, original_group_id=None,
    )


def _seed(db):
    db.add(Office(office_id="o1", office_name="H"))
    db.add(Group(group_id="g1", group_name="g1", office_id="o1"))
    db.flush()


def _run(coro):
    return asyncio.run(coro)


def test_daily_shift_seeds_default_when_no_template(db):
    """shift_manage 가 전혀 없는 병동도 500 대신 기본값으로 초기화된다.

    - month_summary = D3/E3/N2/M0 (공용 DEFAULT_SHIFT_MANAGE_SLOTS)
    - shift_manage RN 슬롯 1/2/3/5 가 실제로 seeding 됨
    - daily_shift 31 일치(2025/07) 생성
    """
    _seed(db)
    assert db.query(ShiftManage).count() == 0  # 사전: 템플릿 없음

    resp = _run(get_month(office_id="o1", group_id="g1", year=2025, month=7,
                          current_user=_stub(), db=db))

    assert resp["month_summary"]["D_count"] == 3
    assert resp["month_summary"]["E_count"] == 3
    assert resp["month_summary"]["N_count"] == 2
    assert resp["month_summary"]["M_count"] == 0
    # 템플릿이 실제로 생겼는지(다음 조회부터는 fallback 안 탐)
    rn = (db.query(ShiftManage)
            .filter(ShiftManage.office_id == "o1", ShiftManage.group_id == "g1",
                    ShiftManage.nurse_class == "RN").all())
    assert sorted(s.shift_slot for s in rn) == [1, 2, 3, 5]
    # daily_shift 실제 일자 생성(7월=31일)
    assert len(resp["date"]["D_count"]) == 31


def test_daily_shift_guard_blocks_foreign_group(db):
    """관리하지 않는 병동 group_id 로 조회 시 403(그룹 스코프 가드)."""
    _seed(db)
    with pytest.raises(HTTPException) as ei:
        _run(get_month(office_id="o1", group_id="gX", year=2025, month=7,
                       current_user=_stub(), db=db))
    assert ei.value.status_code == 403


def test_daily_shift_idempotent_second_call_no_reseed(db):
    """두 번째 조회는 fallback 을 다시 타지 않고 기존 daily_shift 를 그대로 반환."""
    _seed(db)
    _run(get_month(office_id="o1", group_id="g1", year=2025, month=7,
                   current_user=_stub(), db=db))
    before = db.query(DailyShift).filter(DailyShift.group_id == "g1").count()
    _run(get_month(office_id="o1", group_id="g1", year=2025, month=7,
                   current_user=_stub(), db=db))
    after = db.query(DailyShift).filter(DailyShift.group_id == "g1").count()
    assert before == after  # 중복 생성 없음
    assert db.query(ShiftManage).filter(ShiftManage.nurse_class == "RN").count() == 4
