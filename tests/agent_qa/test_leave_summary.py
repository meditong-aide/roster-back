"""휴가 자동부여 결과 노출 — query_schedule(leave_summary) + 생성잡 성공 응답.

배경: 생성 서비스가 roster_data["leave_summary"] 를 싣지만(5bd13ed) 에이전트 경로는
비동기라 그 dict 를 못 본다. 확정된 표를 되읽어 같은 답을 내는지 검증한다.
"""
from datetime import datetime

import pytest

from agents_v2.middleware import execute_skill
from agents_v2.schemas.session_context import SessionContext
from agents_v2.tools import leave_tools
from db.models import RosterJob, Schedule, ScheduleEntry, Shift


def _hn():
    return SessionContext(office_id="OFF001", group_id="GRP001", year=2026, month=8,
                          nurse_id="N001", nurse_name="김민지", user_role="HN")


@pytest.fixture
def leave_codes(db, seed_data):
    """보건휴가(BG)·수면OFF(SO) 타깃 코드 등록."""
    db.add_all([
        Shift(shift_id="BG", office_id="OFF001", group_id="GRP001", name="보건휴가", id=101,
              color="#f0f", shift_gb="휴가", sequence=101,
              health_leave_target=True, sleep_off_target=False),
        Shift(shift_id="SO", office_id="OFF001", group_id="GRP001", name="수면오프", id=102,
              color="#0ff", shift_gb="오프", sequence=102,
              health_leave_target=False, sleep_off_target=True),
    ])
    db.flush()


def _schedule_with(db, grants: list[tuple[str, int, str]], *, schedule_id="SCHLV000001"):
    """grants=[(nurse_id, day, shift_id)] 로 8월 표 하나를 만든다."""
    db.add(Schedule(schedule_id=schedule_id, office_id="OFF001", group_id="GRP001",
                    year=2026, month=8, version=1, status="draft", dropped=False))
    db.flush()
    for i, (nid, day, sid) in enumerate(grants):
        db.add(ScheduleEntry(entry_id=f"LV{i:04d}", schedule_id=schedule_id,
                             nurse_id=nid, work_date=datetime(2026, 8, day), shift_id=sid))
    db.flush()
    return schedule_id


# ── tool ────────────────────────────────────────────────


def test_summary_counts_people_and_days(db, seed_data, leave_codes):
    _schedule_with(db, [("N002", 10, "BG"), ("N003", 12, "BG"),
                        ("N002", 5, "SO"), ("N002", 20, "SO")])
    s = leave_tools.summarize_leave_grants(db, "GRP001", 2026, 8)

    assert s["found"] is True
    assert s["health_leave"]["nurse_count"] == 2 and s["health_leave"]["granted_count"] == 2
    assert s["sleep_off"]["nurse_count"] == 1 and s["sleep_off"]["granted_count"] == 2
    so = s["sleep_off"]["nurses"][0]
    assert so["nurse"] == "박지은" and so["days"] == [5, 20]
    assert "보건휴가 2명 2건" in s["message"] and "수면OFF 1명 2건" in s["message"]


def test_summary_reports_zero_not_silence(db, seed_data, leave_codes):
    """부여가 0건이면 '없음' 이라고 말해야 한다 — 침묵은 미설정과 구분이 안 된다."""
    _schedule_with(db, [])
    s = leave_tools.summarize_leave_grants(db, "GRP001", 2026, 8)
    assert s["health_leave"]["granted_count"] == 0
    assert "보건휴가 없음" in s["message"] and "수면OFF 없음" in s["message"]


def test_summary_none_when_codes_absent(db, seed_data):
    """타깃 코드가 없으면 기능 미사용 — None(호출부가 안내 문구로 처리)."""
    assert leave_tools.summarize_leave_grants(db, "GRP001", 2026, 8) is None


def test_summary_when_no_schedule(db, seed_data, leave_codes):
    s = leave_tools.summarize_leave_grants(db, "GRP001", 2026, 8)
    assert s["found"] is False and "아직 없어" in s["message"]


def test_summary_ignores_dropped_and_takes_latest_version(db, seed_data, leave_codes):
    db.add(Schedule(schedule_id="SCHLVOLD001", office_id="OFF001", group_id="GRP001",
                    year=2026, month=8, version=1, status="draft", dropped=True))
    db.add(ScheduleEntry(entry_id="LVOLD", schedule_id="SCHLVOLD001", nurse_id="N002",
                         work_date=datetime(2026, 8, 3), shift_id="BG"))
    db.flush()
    _schedule_with(db, [("N003", 9, "BG")], schedule_id="SCHLVNEW001")

    s = leave_tools.summarize_leave_grants(db, "GRP001", 2026, 8)
    assert s["health_leave"]["granted_count"] == 1
    assert s["health_leave"]["nurses"][0]["nurse"] == "이수정"


def test_summary_note_discloses_carryover_blind_spot(db, seed_data, leave_codes):
    """되읽기로는 다음 달 이월(carried_out)을 알 수 없다 — 그 한계를 명시해야."""
    _schedule_with(db, [("N002", 10, "BG")])
    s = leave_tools.summarize_leave_grants(db, "GRP001", 2026, 8)
    assert "이월" in s["note"]


# ── query_schedule scope ────────────────────────────────


def test_query_schedule_leave_summary_scope(db, seed_data, leave_codes):
    _schedule_with(db, [("N002", 10, "BG")])
    res = execute_skill(db, "query_schedule",
                        {"scope": "leave_summary", "year": 2026, "month": 8}, _hn())
    assert res.data["found"] is True and res.data["health_leave"]["granted_count"] == 1


def test_query_schedule_leave_summary_without_codes(db, seed_data):
    res = execute_skill(db, "query_schedule",
                        {"scope": "leave_summary", "year": 2026, "month": 8}, _hn())
    assert res.data["found"] is False and "설정돼 있지 않" in res.data["message"]


# ── 생성잡 성공 응답 ────────────────────────────────────


def test_generation_job_success_includes_leave_summary(db, seed_data, leave_codes):
    sid = _schedule_with(db, [("N002", 10, "BG"), ("N003", 11, "SO")])
    db.add(RosterJob(job_id="job-lv-1", office_id="OFF001", group_id="GRP001",
                     nurse_id="N001", status="SUCCESS", progress=100, result_roster_id=sid))
    db.commit()

    res = execute_skill(db, "query_generation_job", {}, _hn())
    d = res.data
    assert d["status"] == "SUCCESS"
    assert "leave_summary" in d, "성공 응답이 휴가 부여 결과를 침묵하면 안 된다"
    assert "2026년 8월 근무표 생성을 완료했어요" in d["message"]
    assert "보건휴가 1명 1건" in d["message"]
    assert (d["year"], d["month"]) == (2026, 8)


def test_generation_job_success_without_schedule_is_unchanged(db, seed_data, leave_codes):
    """result_roster_id 가 없으면 요약은 생략 — 부가정보가 상태 응답을 깨면 안 된다."""
    db.add(RosterJob(job_id="job-lv-2", office_id="OFF001", group_id="GRP001",
                     nurse_id="N001", status="SUCCESS", progress=100))
    db.commit()
    res = execute_skill(db, "query_generation_job", {}, _hn())
    assert res.data["status"] == "SUCCESS" and "leave_summary" not in res.data


def test_generation_job_running_unaffected(db, seed_data, leave_codes):
    db.add(RosterJob(job_id="job-lv-3", office_id="OFF001", group_id="GRP001",
                     nurse_id="N001", status="RUNNING", progress=40))
    db.commit()
    res = execute_skill(db, "query_generation_job", {}, _hn())
    assert res.data["status"] == "RUNNING" and "leave_summary" not in res.data
