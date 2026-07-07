"""E2E: 퇴사자 대응 부분 재생성 (REAL CP-SAT 솔버 실행).

services.resignation_partial_resolve_service.partial_resolve_on_resignation 의 전체 경로를
실제 솔버로 구동한다:
  1) March 2026 병동을 시딩하고 generate_roster_service 로 base 근무표를 실제 생성.
  2) cutoff=2026-03-16 으로 특정 간호사를 퇴사 처리, cutoff 이후만 재최적화.
  3) 핵심 보장을 실측·검증:
       - 동결 prefix(cutoff 이전)가 원본과 verbatim 동일한지
       - 퇴사자가 cutoff 이후 근무에서 빠졌는지
       - cutoff 이후 커버리지 미달이 summary.warnings 에 노출되는지
  4) 결과 표를 print (pytest -s).

주의: app/ 소스는 수정하지 않는다. 테스트에서만 시딩/구동한다.
"""

from __future__ import annotations

import time as _time
from datetime import date, datetime, time

import pytest
from fastapi import HTTPException

from db.models import (
    Group,
    Nurse,
    Office,
    RosterConfig,
    Schedule,
    ScheduleEntry,
    Shift,
    ShiftManage,
    Team,
    Wanted,
)
from schemas.auth_schema import User as UserSchema
from schemas.roster_schema import RosterRequest
from services.resignation_partial_resolve_service import (
    _entries_grid,
    _is_work,
    _main_code,
    _shift_meta,
    partial_resolve_on_resignation,
)
from services.roster_create_service import generate_roster_service

OFFICE = "o1"
GROUP = "A"
YEAR = 2026
MONTH = 3
DAYS_IN_MONTH = 31
CUTOFF = date(2026, 3, 16)  # day index 15

DAY_REQ = 2
EVE_REQ = 2
NIG_REQ = 1


def _user():
    return UserSchema(
        nurse_id="HN", account_id="acc_HN", office_id=OFFICE, group_id=GROUP,
        is_head_nurse=True, is_master_admin=False, name="수간",
        EmpSeqNo="", EmpAuthGbn="", mb_part="", office_name="병원", mb_part_name="",
        official_title_name=None, is_nurse_registered=True,
        hn_auth="HN", original_group_id=GROUP, gw_useYN="Y", qpis_useYN="Y",
    )


@pytest.fixture
def ward(db):
    """March 2026 소규모 병동. HN 1 + 엔진 간호사 9명, D=2/E=2/N=1, off_days=8."""
    db.add(Office(office_id=OFFICE, office_name="병원"))
    db.add(Group(group_id=GROUP, group_name="A병동", office_id=OFFICE, hn_id=["HN"]))
    db.flush()

    db.add(Team(office_id=OFFICE, group_id=GROUP, team_id=1, team_name="A팀"))
    db.add(Team(office_id=OFFICE, group_id=GROUP, team_id=2, team_name="B팀"))
    db.flush()

    # ── Shifts (seed_data 형태 그대로: shift_gb 로 main-code 매핑) ──
    shifts_data = [
        ("D_A", "D", "데이", "근무", time(7, 0), time(15, 0), 1, 0, "D"),
        ("E_A", "E", "이브닝", "근무", time(15, 0), time(23, 0), 2, 0, "E"),
        ("N_A", "N", "나이트", "근무", time(23, 0), time(7, 0), 3, 0, "N"),
        ("OFF_A", "OFF", "오프", "오프", None, None, 4, 0, "OFF"),
        ("V_A", "V", "연차", "휴가", None, None, 5, 0, "V"),
        ("G_A", "공가", "공가", "공가", None, None, 6, 0, None),
        ("M_A", "M", "미드", "근무", time(10, 0), time(18, 0), 7, 0, "M"),
        ("WO_A", "WO", "주휴", "오프", None, None, 8, 1, None),
    ]
    for i, (sid, name, gb, stype, st, et, seq, is_wo, default) in enumerate(shifts_data):
        db.add(Shift(
            shift_id=sid, office_id=OFFICE, group_id=GROUP, name=name, shift_gb=gb,
            type=stype, start_time=st, end_time=et, color="#000", sequence=seq,
            allday=0, auto_schedule=1, is_weekly_off=is_wo, default_shift=default,
            show_in_preference=(stype == "근무"), id=i + 1,
        ))
    db.flush()

    # ── Nurses: 1 head nurse caller (also works) + 9 engine nurses ──
    # (nid, name, grade, experience, team_id)
    roster = [
        ("HN", "수간", 4, 12, 1),
        ("N01", "간호1", 4, 10, 1),
        ("N02", "간호2", 3, 8, 2),
        ("N03", "간호3", 3, 7, 1),
        ("N04", "간호4", 2, 5, 2),
        ("N05", "간호5", 2, 4, 1),
        ("N06", "간호6", 2, 4, 2),
        ("N07", "간호7", 1, 3, 1),
        ("N08", "간호8", 1, 3, 2),
        ("N09", "간호9", 1, 2, 1),
    ]
    for seq, (nid, name, grade, exp, team_id) in enumerate(roster, start=1):
        db.add(Nurse(
            nurse_id=nid, group_id=GROUP, office_id=OFFICE, account_id=f"acc_{nid}",
            name=name, grade=grade, experience=exp, role="RN", team_id=team_id,
            is_head_nurse=(nid == "HN"), hn_auth=("HN" if nid == "HN" else None),
            active=1, sequence=seq, joining_date=datetime(2024, 1, 1),
            preceptor_id=None, is_weekend_off=False,
            work_shifts=["D", "E", "N"], allowed_shifts=[], fixed_shift=None,
            enable_aide=True, resignation_date=None,
        ))
    db.flush()

    # ── Roster config (작지만 solvable) ──
    db.add(RosterConfig(
        config_id=1, config_version="v1", office_id=OFFICE, group_id=GROUP,
        day_req=DAY_REQ, eve_req=EVE_REQ, nig_req=NIG_REQ,
        min_exp_per_shift=1, req_exp_nurses=1,
        two_offs_per_week=True, max_nig_per_month=8,
        three_seq_nig=False, two_offs_after_three_nig=True, two_offs_after_two_nig=False,
        banned_day_after_eve=True, max_conseq_work=5, off_days=8,
        shift_priority=0.5, weekend_shift_ratio=0.5, patient_amount=30,
        sequential_offs=True, even_nights=True, nod_noe=True, not_one_night=True,
        use_mid=False, preceptor_gauge=5, preceptee_on=False, preceptee_shift_count=False,
        weekly_off_group=False, team_balance_enable=True, team_balance_gauge=5,
        team_balance_mode="balanced", off_placement_mode=0,
        fixed_wanted_use_yn=False, show_level=True, show_preceptor=True,
    ))
    db.flush()

    # ── ShiftManage (요구 인원) ──
    for slot, (main, sid, mp) in enumerate(
        [("D", "D_A", DAY_REQ), ("E", "E_A", EVE_REQ), ("N", "N_A", NIG_REQ)], start=1
    ):
        db.add(ShiftManage(
            office_id=OFFICE, group_id=GROUP, nurse_class="RN", shift_slot=slot,
            main_code=main, codes=[sid], manpower=mp,
        ))
    db.flush()

    # ── Wanted campaign (게이트 통과) ──
    db.add(Wanted(
        group_id=GROUP, year=YEAR, month=MONTH,
        exp_date=datetime(2026, 2, 25), status="requested",
    ))
    db.flush()
    db.commit()
    return db


def _grid_for(db, schedule_id, days=DAYS_IN_MONTH):
    entries = (
        db.query(ScheduleEntry)
        .filter(ScheduleEntry.schedule_id == schedule_id)
        .all()
    )
    return _entries_grid(entries, days)


def test_resignation_partial_resolve_e2e(ward):
    db = ward
    user = _user()

    # ── STEP 1: base 근무표 실제 생성 (partial context 없이) ──
    base_req = RosterRequest(year=YEAR, month=MONTH, group_id=GROUP, config_id=1)
    t0 = _time.time()
    base_data = generate_roster_service(base_req, user, db)
    base_gen_secs = _time.time() - t0
    base_schedule_id = str(base_data["schedule_id"])
    assert base_schedule_id, "base 근무표 schedule_id 가 비었다"

    shift_meta, _ = _shift_meta(db, OFFICE, GROUP)
    base_grid = _grid_for(db, base_schedule_id)

    # ── STEP 2: cutoff 이후 실제로 근무하는 퇴사자 선택 ──
    cutoff_idx = CUTOFF.day - 1  # 15
    resigned_id = None
    for nid, row in base_grid.items():
        if nid == "HN":
            continue
        if any(_is_work(row[d], shift_meta) for d in range(cutoff_idx, DAYS_IN_MONTH)):
            resigned_id = nid
            break
    assert resigned_id is not None, "cutoff 이후 근무하는 간호사를 찾지 못했다"

    # ── STEP 3: 부분 재생성 호출 (REAL 솔버 재실행) ──
    t1 = _time.time()
    diff = partial_resolve_on_resignation(
        db=db,
        current_user=user,
        schedule_id=base_schedule_id,
        resigned_nurse_id=resigned_id,
        cutoff_date=CUTOFF,
    )
    partial_secs = _time.time() - t1

    draft_id = str(diff["schedule_id"])
    draft_grid = _grid_for(db, draft_id)

    # ── STEP 4: MEASURE ──
    # prefix_identical
    prefix_mismatches = []
    for nid, base_row in base_grid.items():
        if nid == resigned_id:
            continue
        draft_row = draft_grid.get(nid, ["-"] * DAYS_IN_MONTH)
        for d in range(cutoff_idx):
            if base_row[d] != draft_row[d]:
                prefix_mismatches.append((nid, d + 1, base_row[d], draft_row[d]))
    prefix_identical = len(prefix_mismatches) == 0

    # resigned_absent_suffix
    resigned_draft_row = draft_grid.get(resigned_id, ["-"] * DAYS_IN_MONTH)
    resigned_suffix_work_days = [
        d + 1 for d in range(cutoff_idx, DAYS_IN_MONTH)
        if _is_work(resigned_draft_row[d], shift_meta)
    ]
    resigned_absent_suffix = len(resigned_suffix_work_days) == 0

    # coverage on/after cutoff (measured independently)
    req = {"D": DAY_REQ, "E": EVE_REQ, "N": NIG_REQ}
    shortfalls = []
    for d in range(cutoff_idx, DAYS_IN_MONTH):
        counts = {"D": 0, "E": 0, "N": 0}
        for nid, row in draft_grid.items():
            main = _main_code(row[d], shift_meta)
            if main in counts:
                counts[main] += 1
        for code, need in req.items():
            if counts[code] < need:
                shortfalls.append((d + 1, code, counts[code], need))

    # ── 품질 워치리스트: 싱글 N / 회복 OFF(3N2O) — base vs draft 비교 ──
    def _mains(row):
        return [_main_code(row[d], shift_meta) for d in range(DAYS_IN_MONTH)]

    def _single_n_days(m):
        out = []
        for d in range(len(m)):
            if m[d] != "N":
                continue
            prev_n = d > 0 and m[d - 1] == "N"
            next_n = d < len(m) - 1 and m[d + 1] == "N"
            if not prev_n and not next_n:
                out.append(d + 1)
        return out

    def _recovery_viol(m, min_block=3, need_off=2):
        # 3연속 이상 N 블록 뒤 need_off OFF 미달(월말 여유 있을 때만 위반)
        viols, d, n = [], 0, len(m)
        while d < n:
            if m[d] == "N":
                start = d
                while d < n and m[d] == "N":
                    d += 1
                if d - start >= min_block:
                    off_after, j = 0, d
                    while j < n and m[j] == "O" and off_after < need_off:
                        off_after, j = off_after + 1, j + 1
                    if off_after < need_off and (n - d) >= need_off:
                        viols.append((start + 1, d - start, off_after))
            else:
                d += 1
        return viols

    def _tally(grid):
        sn, rv = 0, 0
        for nid, row in grid.items():
            if nid == resigned_id:
                continue
            m = _mains(row)
            sn += len(_single_n_days(m))
            rv += len(_recovery_viol(m))
        return sn, rv

    base_single_n, base_recov = _tally(base_grid)
    draft_single_n, draft_recov = _tally(draft_grid)

    summary = diff["summary"]
    cells_changed = summary["cells_changed"]
    nurses_touched = summary["nurses_touched"]
    warnings = summary["warnings"]
    changed_nurses = diff["changed_nurses"]

    # example changes
    examples = []
    for cn in changed_nurses[:3]:
        for ch in cn["changes"][:2]:
            examples.append(
                (cn["name"], ch["day"], f'{ch["from"]}->{ch["to"]}', ch["kind"])
            )

    # ── STEP 5: PRINT RESULTS TABLE ──
    print("\n" + "=" * 68)
    print("  RESIGNATION PARTIAL-RESOLVE E2E — REAL SOLVER RESULTS")
    print("=" * 68)
    print(f"  base schedule_id        : {base_schedule_id}")
    print(f"  draft schedule_id       : {draft_id}")
    print(f"  resigned nurse          : {resigned_id} "
          f"({diff['resigned_nurse']['name']})")
    print(f"  cutoff_date             : {CUTOFF.isoformat()} (day index {cutoff_idx})")
    print(f"  month                   : {YEAR}-{MONTH:02d} ({DAYS_IN_MONTH} days)")
    print(f"  requirements            : D={DAY_REQ} E={EVE_REQ} N={NIG_REQ}")
    print(f"  engine nurses in base   : {len([n for n in base_grid if base_grid[n]])}")
    print("-" * 68)
    print(f"  prefix_identical        : {prefix_identical} "
          f"(mismatches={len(prefix_mismatches)})")
    print(f"  resigned_absent_suffix  : {resigned_absent_suffix} "
          f"(work days after cutoff={resigned_suffix_work_days})")
    print(f"  cells_changed           : {cells_changed}")
    print(f"  nurses_touched          : {nurses_touched}")
    print(f"  changed_nurses count    : {len(changed_nurses)}")
    print(f"  coverage shortfalls     : {len(shortfalls)}")
    if shortfalls:
        for (day, code, have, need) in shortfalls[:10]:
            print(f"      day {day:2d} {code}: {have}/{need}")
    print(f"  summary.warnings        : {len(warnings)}")
    for w in warnings[:10]:
        print(f"      - {w}")
    print("-" * 68)
    print("  quality watchlist (non-resigned, base -> draft):")
    print(f"      single-N (isolated)   : {base_single_n} -> {draft_single_n}")
    print(f"      3N2O recovery viol    : {base_recov} -> {draft_recov}")
    print("-" * 68)
    print("  example changes (nurse, day, from->to, kind):")
    for ex in examples:
        print(f"      {ex}")
    print("-" * 68)
    print(f"  base generation time    : {base_gen_secs:6.2f} s")
    print(f"  partial_resolve time    : {partial_secs:6.2f} s")
    print("=" * 68 + "\n")

    # ── STEP 6: HARD GUARANTEES ──
    assert set(diff.keys()) >= {
        "schedule_id", "base_schedule_id", "cutoff_date",
        "resigned_nurse", "summary", "changed_nurses",
    }
    assert diff["base_schedule_id"] == base_schedule_id
    assert diff["cutoff_date"] == CUTOFF.isoformat()
    assert prefix_identical, f"동결 prefix 불일치: {prefix_mismatches[:10]}"
    assert resigned_absent_suffix, (
        f"퇴사자가 cutoff 이후 근무 잔존: {resigned_suffix_work_days}"
    )
    # 커버리지 미달은 하드 실패 아님(빡센 병동일 수 있음). 단, 미달이 있으면
    # summary.warnings 에 반드시 노출돼야 한다(조용한 미달 금지).
    if shortfalls:
        assert len(warnings) > 0, (
            f"커버리지 미달({len(shortfalls)}건)이 있는데 warnings 가 비었다"
        )
    # 재-solve가 품질을 악화시키지 않는다 — 싱글 N / 3N2O 회복 위반이 원본보다 늘면 안 됨
    # (앵커는 Stage3라 Stage2 안전 위반을 새로 만들 수 없다는 구조적 보장의 실측 검증).
    assert draft_single_n <= base_single_n, (
        f"싱글 N 증가: base={base_single_n} draft={draft_single_n}"
    )
    assert draft_recov <= base_recov, (
        f"3N2O 회복 위반 증가: base={base_recov} draft={draft_recov}"
    )


def test_cutoff_out_of_range_rejected(ward):
    """cutoff_date 가 schedule 월 범위를 벗어나면 400 (base schedule 존재 전제)."""
    db = ward
    user = _user()
    base_req = RosterRequest(year=YEAR, month=MONTH, group_id=GROUP, config_id=1)
    base_data = generate_roster_service(base_req, user, db)
    base_id = str(base_data["schedule_id"])
    with pytest.raises(HTTPException) as ei:
        partial_resolve_on_resignation(
            db=db, current_user=user, schedule_id=base_id,
            resigned_nurse_id="N01", cutoff_date=date(2026, 4, 5),
        )
    assert ei.value.status_code == 400
