"""부분 재생성 오케스트레이션 통합 테스트 (솔버 미실행, DB 매핑/복원/diff 검증).

- _apply_partial_resolve_context: prefix fixed_cells + suffix anchor(코드) 매핑
- _restore_prefix_from_base: 동결 prefix verbatim 복원(변형 보존)
- build_resignation_diff_response: cells_changed에서 퇴사자 제외(#5) + kind 분류
"""
from __future__ import annotations

from datetime import date, datetime
from types import SimpleNamespace

from db.models import Nurse, Schedule, ScheduleEntry
from services.roster_create_service import _apply_partial_resolve_context
from services.resignation_partial_resolve_service import (
    _restore_prefix_from_base,
    build_resignation_diff_response,
)

YEAR, MONTH = 2026, 3
CUTOFF = date(2026, 3, 16)  # cutoff_idx = 15


def _entry(schedule_id, nurse_id, day, shift_id):
    return ScheduleEntry(
        entry_id=f"{schedule_id[:4]}{nurse_id[:3]}{day:03d}"[:16],
        schedule_id=schedule_id,
        nurse_id=nurse_id,
        work_date=datetime(YEAR, MONTH, day),
        shift_id=shift_id,
    )


def test_apply_partial_resolve_context_builds_prefix_and_anchor(db):
    base_id = "baseAAAAAAAA"
    # 원본: n_res=D(퇴사), n_a=E, n_b=O — 전월(prefix)·후월(suffix) 모두 매일 동일
    for day in range(1, 32):
        db.add(_entry(base_id, "n_res", day, "D"))
        db.add(_entry(base_id, "n_a", day, "E"))
        db.add(_entry(base_id, "n_b", day, "O"))
    db.flush()

    nurses = [
        SimpleNamespace(nurse_id="n_res", sequence=1, experience=5),
        SimpleNamespace(nurse_id="n_a", sequence=2, experience=3),
        SimpleNamespace(nurse_id="n_b", sequence=3, experience=1),
    ]
    current_user = SimpleNamespace(office_id="o1", group_id="g1")
    req = SimpleNamespace(year=YEAR, month=MONTH)
    config_dict: dict = {}
    ctx = {
        "base_schedule_id": base_id,
        "resigned_nurse_id": "n_res",
        "cutoff_date": CUTOFF,
        "w_cell": 1,
        "w_nurse": 5,
    }

    combined = _apply_partial_resolve_context(
        db=db, current_user=current_user, req=req,
        nurses_for_engine=nurses, config_dict=config_dict,
        combined_fixed_cells=[], partial_resolve_context=ctx,
    )

    # prefix(day<15) fixed_cells: 3 nurses × 15 days = 45
    prefix_cells = [c for c in combined if c.get("fixed_source") == "partial_resolve_prefix"]
    assert len(prefix_cells) == 45
    assert all(c["day_index"] < 15 for c in prefix_cells)
    # prefix는 코드로 정규화되어 고정
    codes = {c["shift"] for c in prefix_cells}
    assert codes == {"D", "E", "O"}

    anchor = config_dict["partial_resolve_anchor"]
    orig = anchor["orig"]
    # suffix(day>=15) 비퇴사자만 anchor: n_a, n_b × 16 days = 32
    assert len(orig) == 32
    # 값은 '코드'(문자열) — s_idx 아님(#4)
    assert all(isinstance(v, str) for v in orig.values())
    # n_res의 suffix는 orig에 없어야 함 — nurse_index로 확인
    # (n_res sequence=1 → engine index 0)
    assert all(idx != 0 for (idx, _d) in orig.keys()), "퇴사자(index0)는 anchor 제외"
    assert anchor["w_cell"] == 1 and anchor["w_nurse"] == 5


def test_restore_prefix_from_base_preserves_variant(db):
    base_id = "baseBBBBBBBB"
    draft_id = "draftBBBBBBB"
    base = Schedule(schedule_id=base_id, year=YEAR, month=MONTH, version=1)
    draft = Schedule(schedule_id=draft_id, year=YEAR, month=MONTH, version=2)
    db.add_all([base, draft])
    # 원본 prefix: 특정 변형 "D2" (수동 편집 가정)
    db.add(_entry(base_id, "n_a", 3, "D2"))
    # draft prefix: 솔버가 generic "D"로 저장 + suffix
    db.add(_entry(draft_id, "n_a", 3, "D"))
    db.add(_entry(draft_id, "n_a", 20, "E"))  # suffix — 건드리면 안 됨
    db.flush()

    _restore_prefix_from_base(db=db, base_schedule=base, draft_schedule=draft, cutoff_date=CUTOFF)

    rows = (
        db.query(ScheduleEntry)
        .filter(ScheduleEntry.schedule_id == draft_id)
        .all()
    )
    by_day = {r.work_date.day: r.shift_id for r in rows}
    assert by_day[3] == "D2"   # prefix 변형 복원
    assert by_day[20] == "E"   # suffix 보존


def test_diff_excludes_resigned_from_cells_changed(db):
    base_id = "baseCCCCCCCC"
    draft_id = "draftCCCCCCC"
    base = Schedule(schedule_id=base_id, year=YEAR, month=MONTH, version=1,
                    office_id="o1", group_id="g1", config_id=None)
    draft = Schedule(schedule_id=draft_id, year=YEAR, month=MONTH, version=2,
                     office_id="o1", group_id="g1", config_id=None)
    db.add_all([base, draft])
    for n, name in [("n_res", "퇴사자"), ("n_a", "에이"), ("n_b", "비")]:
        db.add(Nurse(nurse_id=n, account_id=n, name=name, office_id="o1", group_id="g1"))
    # suffix(day16..31): base n_res=D, n_a=E, n_b=O
    for day in range(16, 32):
        db.add(_entry(base_id, "n_res", day, "D"))
        db.add(_entry(base_id, "n_a", day, "E"))
        db.add(_entry(base_id, "n_b", day, "O"))
        # draft: n_res 없음(결원), n_a 그대로 E, n_b는 O→D(빈자리 흡수)
        db.add(_entry(draft_id, "n_a", day, "E"))
        db.add(_entry(draft_id, "n_b", day, "D"))
    db.flush()

    resigned = db.query(Nurse).filter(Nurse.nurse_id == "n_res").first()
    resp = build_resignation_diff_response(
        db=db, base_schedule=base, draft_schedule=draft,
        resigned_nurse=resigned, cutoff_date=CUTOFF,
    )

    # n_b만 16일 변경(O→D). 퇴사자 vacated 셀은 cells_changed 제외(#5)
    assert resp["summary"]["cells_changed"] == 16
    assert resp["summary"]["nurses_touched"] == 1
    by_id = {c["nurse_id"]: c for c in resp["changed_nurses"]}
    # 퇴사자는 표시엔 남되 kind=resigned
    assert by_id["n_res"]["changes"][0]["kind"] == "resigned"
    # n_b는 off_to_work
    assert by_id["n_b"]["changes"][0]["kind"] == "off_to_work"
    assert "n_a" not in by_id  # 변경 없음
