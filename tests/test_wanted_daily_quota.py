"""원티드 일자별 신청 한도(병동 전체 + 팀별) — 제출 검사·작성 화면 조회·팀 한도 설정.

검증 대상(기획 `.omc/plans/wanted_team_daily_limit_plan.md` 4장 + 11-04 결정 `.omc/plans/wanted_team_daily_limit_1104.md`):
- **팀 우선**: 내 팀 한도(날짜 지정 > 팀 기본값)가 있으면 팀 기준만, 없으면 병동 일자 한도 · 기타(팀 미배정·
  비활성 팀)는 병동 기준 · 0 = 그 기준으로 마감
- 세는 대상 = 이 병동 **활성** 간호사의 휴가·휴무 계열 '희망' **최신 제출본**만(임시저장·철회·근무코드·공가 제외)
- 팀 판정 = 근무자관리 규칙(1일 기준, 월중 합류자는 그 달 첫 팀, 비활성 팀은 기타)
- 제출 경로(wanted_entries · 구형 data) 둘 다 409(`DAILY_QUOTA_EXCEEDED`)로 막고 다른 팀은 통과
- 작성 화면 조회(daily_quota — 적용 기준·한도·자리) · 설정 조회·저장(근무자 목록·적용 결과·경고)
- 정합성(11-04 검토): 철회는 그 달 제출본을 모두 내림 · AIDE 편집은 이전 제출본을 내림 · AIDE 간호사 본인
  쓰기는 한도·마감을 거침 · 잠금 대기 상한

시드 팀: A팀(1) = N001·N002·N005 / B팀(2) = N003·N004·N006 (팀 구간 없음 → nurses.team_id 폴백).
"""
from contextlib import contextmanager
from datetime import date, datetime

import pytest
import services.wanted_daily_quota_service as quota
from agents_v2.tools import wanted_tools
from db.models import (
    Nurse,
    NursePairRequest,
    NurseShiftRequest,
    NurseTeamPeriod,
    Shift,
    Team,
    Wanted,
    WantedConfig,
    WantedRequest,
    WantedTeamLimit,
)
from schemas.auth_schema import User as UserSchema
from schemas.roster_schema import PreferenceData, PreferenceSubmit
from services.preferences_service import (
    PreferenceConflictError,
    PreferenceQuotaError,
    PreferenceValidationError,
    retract_submission_service,
    save_wanted_entries_service,
    submit_preferences_service,
)
from sqlalchemy.exc import OperationalError

Y, M = 2026, 11
G = "GRP001"
V = "V_GRP001"        # 휴가 — 셈 대상
D = "D_GRP001"        # 근무 — 셈 대상 아님
GONGGA = "G_GRP001"   # 공가 — 셈 대상 아님(화면 기준과 같다)
MONTH = f"{Y}-{M:02d}"


@pytest.fixture
def env(db, seed_data, monkeypatch):
    # 서비스의 db.commit() 이 conftest 단일 트랜잭션(롤백 격리)을 깨지 않도록 flush 로 치환.
    db.commit = db.flush  # type: ignore[assignment]
    bind = db.get_bind()
    WantedConfig.__table__.create(bind=bind, checkfirst=True)
    WantedTeamLimit.__table__.create(bind=bind, checkfirst=True)
    # 저장소 확인 캐시가 다른 테스트로 새지 않게(이 모듈 밖에서는 표가 없다 — '없음' 캐시도 비운다).
    monkeypatch.setattr(quota, "_TEAM_LIMIT_READY", False)
    monkeypatch.setattr(quota, "_TEAM_LIMIT_MISSING_UNTIL", 0.0)
    db.query(Shift).filter(Shift.shift_id.in_([V, GONGGA])).update(
        {Shift.show_in_preference: True}, synchronize_session=False)
    db.add(Wanted(group_id=G, year=Y, month=M, status="requested", exp_date=datetime(2099, 1, 1)))
    db.flush()
    return db


def _request(db, nurse_id, days: dict, *, submitted=True, request_id=1, submitted_at=None):
    db.add(WantedRequest(
        nurse_id=nurse_id, request_id=request_id, month=MONTH, group_id=G, request="",
        is_submitted=submitted, created_at=datetime(2026, 10, 1),
        submitted_at=(submitted_at or datetime(2026, 10, 20)) if submitted else None,
    ))
    for i, (day, code) in enumerate(sorted(days.items()), start=1):
        db.add(NurseShiftRequest(
            nurse_id=nurse_id, request_id=request_id, detailed_request_id=i,
            shift_date=date(Y, M, day), group_id=G, shift=code, score=10.0,
            partial_request="", comment="",
        ))
    db.flush()
    return request_id


def _team_limit(db, team_id, limit, day=None):
    db.add(WantedTeamLimit(group_id=G, year=Y, month=M, team_id=team_id,
                           target_date=date(Y, M, day) if day else None, max_requests=limit))
    db.flush()


def _ward_limit(db, day, limit):
    db.add(WantedConfig(group_id=G, year=Y, month=M, target_date=date(Y, M, day), max_requests=limit))
    db.flush()


def _check(db, nurse_id, request_id):
    quota.assert_daily_quota(db, nurse_id=nurse_id, group_id=G, year=Y, month=M, request_id=request_id)


def _user(nurse_id):
    return UserSchema(
        nurse_id=nurse_id, account_id=f"acc_{nurse_id}", office_id="OFF001", group_id=G, name=nurse_id,
        mb_part="", office_name="", mb_part_name="", gw_useYN="Y", qpis_useYN="N",
        official_title_name=None,
    )


# ── 제출 검사 ──────────────────────────────────────────────────────────────


def test_team_default_blocks_same_team_only(env):
    _team_limit(env, 1, 1)
    _request(env, "N002", {14: V})
    rid = _request(env, "N005", {14: V}, submitted=False)
    with pytest.raises(PreferenceQuotaError) as exc:
        _check(env, "N005", rid)
    (item,) = exc.value.detail["dates"]
    assert item == {"date": "2026-11-14", "basis": "team", "team_id": 1, "team_name": "A팀",
                    "limit": 1, "others": 1}
    assert exc.value.code == "DAILY_QUOTA_EXCEEDED"  # 모바일 오류 처리는 대문자 코드만 살린다
    assert "14일은 A팀" in str(exc.value)
    # B팀은 팀 한도가 없다.
    _check(env, "N003", _request(env, "N003", {14: V}, submitted=False))


def test_date_override_beats_team_default(env):
    _team_limit(env, 1, 1)
    _team_limit(env, 1, 2, day=14)
    _request(env, "N002", {14: V, 15: V})
    _check(env, "N005", _request(env, "N005", {14: V}, submitted=False))
    with pytest.raises(PreferenceQuotaError):
        _check(env, "N005", _request(env, "N005", {15: V}, submitted=False, request_id=2))


def test_ward_limit_applies_across_teams(env):
    """팀 한도가 없는 팀은 병동 기준 — 병동 전원(다른 팀 포함)을 센다."""
    _ward_limit(env, 14, 1)
    _request(env, "N002", {14: V})
    with pytest.raises(PreferenceQuotaError) as exc:
        _check(env, "N003", _request(env, "N003", {14: V}, submitted=False))
    assert [i["basis"] for i in exc.value.detail["dates"]] == ["ward"]
    assert "병동 전체" in str(exc.value)


def test_unassigned_nurse_gets_ward_limit_only(env):
    env.query(Nurse).filter(Nurse.nurse_id == "N006").update({Nurse.team_id: None})
    _team_limit(env, 2, 0)
    _check(env, "N006", _request(env, "N006", {14: V}, submitted=False))
    with pytest.raises(PreferenceQuotaError):  # 한도 0 = 첫 신청부터 불가
        _check(env, "N003", _request(env, "N003", {14: V}, submitted=False))


def test_only_leave_codes_of_latest_submission_take_seats(env):
    _team_limit(env, 1, 1)
    _request(env, "N002", {14: D, 15: GONGGA})                       # 근무·공가는 자리 안 잡음
    _request(env, "N001", {16: V}, submitted=False)                    # 임시저장은 자리 안 잡음
    _request(env, "N005", {17: V}, submitted_at=datetime(2026, 10, 1))  # 옛 제출본
    _request(env, "N005", {}, request_id=2, submitted_at=datetime(2026, 10, 2))  # 최신 제출본(17일 없음)
    rid = _request(env, "N002", {14: V, 15: V, 16: V, 17: V}, submitted=False, request_id=2)
    _check(env, "N002", rid)


def test_own_work_code_is_not_checked(env):
    _team_limit(env, 1, 0)
    _check(env, "N005", _request(env, "N005", {14: D}, submitted=False))


# ── 제출 경로 ──────────────────────────────────────────────────────────────


def test_wanted_entries_submit_rejected(env):
    _team_limit(env, 1, 1)
    _request(env, "N002", {14: V})
    body = PreferenceData(year=Y, month=M, wanted_entries=[
        {"date": "2026-11-14", "shift_id": V, "intent": "wanted"}])
    with pytest.raises(PreferenceQuotaError):
        save_wanted_entries_service(body, _user("N005"), env, is_draft=False)


def test_wanted_entries_submit_ok_returns_daily_quota(env):
    _team_limit(env, 2, 1)
    body = PreferenceData(year=Y, month=M, wanted_entries=[
        {"date": "2026-11-14", "shift_id": V, "intent": "wanted"}])
    result = save_wanted_entries_service(body, _user("N003"), env, is_draft=False)
    assert result["is_submitted"] is True
    cell = result["daily_quota"]["dates"]["2026-11-14"]
    assert cell == {"basis": "team", "limit": 1, "taken": 1, "remaining": 0, "mine": True, "available": True}


def test_legacy_submit_of_saved_draft_rejected(env):
    """구형 경로 — 임시저장해 둔 뒤 {year, month} 만 보내 제출(모바일 흐름). 저장된 draft 칸을 센다."""
    _ward_limit(env, 14, 1)
    _request(env, "N003", {14: V})
    _request(env, "N005", {14: V}, submitted=False)
    with pytest.raises(PreferenceQuotaError):
        submit_preferences_service(PreferenceData(year=Y, month=M), _user("N005"), env, is_draft=False)


def test_reused_draft_from_other_ward_takes_seat_here(env):
    """다른 병동에서 만든 같은 달 draft 를 재사용해 제출해도 이 병동 자리를 잡는다 — 병동을 안 맞추면 셈에서 빠진다(11-04 리뷰)."""
    _ward_limit(env, 14, 1)
    env.add(WantedRequest(nurse_id="N005", request_id=1, month=MONTH, group_id="GRP_OLD", request="",
                          is_submitted=False, created_at=datetime(2026, 10, 1)))   # 옮기기 전 병동의 draft
    env.flush()
    body = PreferenceData(year=Y, month=M, wanted_entries=[{"date": "2026-11-14", "shift_id": V, "intent": "wanted"}])
    save_wanted_entries_service(body, _user("N005"), env, is_draft=False)
    with pytest.raises(PreferenceQuotaError):
        save_wanted_entries_service(body, _user("N003"), env, is_draft=False)


def test_draft_save_is_not_checked(env):
    _team_limit(env, 1, 0)
    body = PreferenceData(year=Y, month=M, wanted_entries=[
        {"date": "2026-11-14", "shift_id": V, "intent": "wanted"}])
    result = save_wanted_entries_service(body, _user("N005"), env, is_draft=True)
    assert result["is_submitted"] is False


# ── 작성 화면 조회 ─────────────────────────────────────────────────────────


def test_daily_quota_view(env):
    """날짜마다 나에게 적용되는 기준 하나의 숫자만 — 팀 한도가 있으면 팀, 없으면 병동."""
    _team_limit(env, 1, 2)
    _ward_limit(env, 14, 3)
    _request(env, "N002", {14: V})
    _request(env, "N005", {14: V})
    a = quota.daily_quota_view(env, nurse_id="N005", group_id=G, year=Y, month=M)
    assert a["rule"] == "team_first"
    assert V in a["counted_shift_ids"] and GONGGA not in a["counted_shift_ids"] and D not in a["counted_shift_ids"]
    assert a["team"] == {"team_id": 1, "team_name": "A팀"}
    assert len(a["dates"]) == 30  # 팀 기본값은 한 달 전체
    # 14일은 병동 한도(3)도 있지만 A팀은 팀 한도가 있어 팀 기준만 — 병동 수는 싣지 않는다.
    assert a["dates"]["2026-11-14"] == {"basis": "team", "limit": 2, "taken": 2, "remaining": 0,
                                         "mine": True, "available": True}
    assert a["dates"]["2026-11-15"]["available"] is True
    b = quota.daily_quota_view(env, nurse_id="N003", group_id=G, year=Y, month=M)
    assert b["team"]["team_id"] == 2
    assert list(b["dates"]) == ["2026-11-14"]  # B팀은 팀 한도 없음 → 병동 한도 날짜만
    assert b["dates"]["2026-11-14"] == {"basis": "ward", "limit": 3, "taken": 2, "remaining": 1,
                                         "mine": False, "available": True}


def test_daily_quota_view_full_and_lowered_limit(env):
    """available = 지금 넣어 제출하면 통과하는지 — 한도를 낮춰 넘친 날은 내 자리가 있어도 False."""
    _team_limit(env, 1, 1)
    _request(env, "N002", {14: V})
    _request(env, "N005", {14: V})  # 한도를 낮추기 전에 들어간 제출(유지된다)
    cell = quota.daily_quota_view(env, nurse_id="N005", group_id=G, year=Y, month=M)["dates"]["2026-11-14"]
    assert cell == {"basis": "team", "limit": 1, "taken": 2, "remaining": 0, "mine": True, "available": False}
    other = quota.daily_quota_view(env, nurse_id="N001", group_id=G, year=Y, month=M)["dates"]["2026-11-14"]
    assert (other["remaining"], other["mine"], other["available"]) == (0, False, False)


def test_daily_quota_view_without_limits(env):
    view = quota.daily_quota_view(env, nurse_id="N005", group_id=G, year=Y, month=M)
    assert (view["rule"], view["team"], view["dates"]) == ("team_first", None, {})


# ── 팀 한도 설정 ───────────────────────────────────────────────────────────


@pytest.mark.parametrize("defaults, dates, message", [
    ([{"team_id": 9, "max_requests": 1}], [], "활성 팀이 아닙니다"),
    ([], [{"target_date": date(2026, 12, 1), "team_id": 1, "max_requests": 1}], "속하지 않는 날짜"),
    ([{"team_id": 1, "max_requests": 1}, {"team_id": 1, "max_requests": 2}], [], "중복"),
])
def test_replace_team_limits_rejects_bad_input(env, defaults, dates, message):
    with pytest.raises(ValueError, match=message):
        quota.replace_team_limits(env, G, Y, M, defaults=defaults, dates=dates)


def test_replace_team_limits_saves_and_warns(env):
    _request(env, "N002", {14: V})
    _request(env, "N005", {14: V})
    view = quota.replace_team_limits(
        env, G, Y, M,
        defaults=[{"team_id": 1, "max_requests": 1}],
        dates=[{"target_date": date(2026, 11, 20), "team_id": 2, "max_requests": 0}],
    )
    assert [(t["team_id"], t["member_count"], t["default_limit"]) for t in view["teams"]] == [
        (1, 3, 1), (2, 3, None)]
    assert view["unassigned_count"] == 0
    assert view["dates"] == [{"target_date": "2026-11-20", "team_id": 2, "max_requests": 0}]
    assert view["submitted_counts"]["2026-11-14"] == {"total": 2, "by_team": {"1": 2}}
    assert view["warnings"] == [{"kind": "over_limit", "date": "2026-11-14", "team_id": 1,
                                 "team_name": "A팀", "limit": 1, "submitted": 2}]
    cleared = quota.replace_team_limits(env, G, Y, M, defaults=[], dates=[])
    assert cleared["dates"] == [] and all(t["default_limit"] is None for t in cleared["teams"])


def test_missing_storage_skips_team_limits(env, monkeypatch):
    _team_limit(env, 1, 0)
    monkeypatch.setattr(quota, "team_limit_storage_ready", lambda db: False)
    _check(env, "N005", _request(env, "N005", {14: V}, submitted=False))
    with pytest.raises(quota.TeamLimitUnavailableError):
        quota.replace_team_limits(env, G, Y, M, defaults=[], dates=[])


# ── 팀 우선 규칙(11-04 결정) ───────────────────────────────────────────────


def test_team_limit_replaces_ward_limit_for_its_members(env):
    """팀 한도가 있는 팀원에게는 그날 병동 한도가 걸리지 않는다. 다른 팀은 병동 기준(팀원 포함 병동 전원)."""
    _ward_limit(env, 14, 1)
    _team_limit(env, 1, 2)
    _request(env, "N002", {14: V})
    _check(env, "N005", _request(env, "N005", {14: V}, submitted=False))  # 병동 1명 찼지만 A팀 기준 1 < 2
    env.query(WantedRequest).filter(WantedRequest.nurse_id == "N005").update(
        {WantedRequest.is_submitted: True, WantedRequest.submitted_at: datetime(2026, 10, 21)})
    with pytest.raises(PreferenceQuotaError) as exc:
        _check(env, "N003", _request(env, "N003", {14: V}, submitted=False))
    assert exc.value.detail["dates"] == [{"date": "2026-11-14", "basis": "ward", "limit": 1, "others": 2}]


def test_team_zero_closes_even_with_ward_room(env):
    _ward_limit(env, 14, 5)
    _team_limit(env, 1, 0)
    with pytest.raises(PreferenceQuotaError):
        _check(env, "N005", _request(env, "N005", {14: V}, submitted=False))


def test_ward_zero_closes_only_ward_basis(env):
    _ward_limit(env, 14, 0)
    _team_limit(env, 1, 1)
    _check(env, "N005", _request(env, "N005", {14: V}, submitted=False))  # A팀 기준
    with pytest.raises(PreferenceQuotaError):
        _check(env, "N003", _request(env, "N003", {14: V}, submitted=False))  # B팀 = 병동 기준 0


def test_unassigned_follows_ward_limit(env):
    env.query(Nurse).filter(Nurse.nurse_id == "N006").update({Nurse.team_id: None})
    _ward_limit(env, 14, 1)
    _team_limit(env, 2, 5)
    _request(env, "N002", {14: V})
    with pytest.raises(PreferenceQuotaError) as exc:
        _check(env, "N006", _request(env, "N006", {14: V}, submitted=False))
    assert exc.value.detail["dates"][0]["basis"] == "ward"


# ── 팀 판정·셈 대상(근무자관리 규칙) ─────────────────────────────────────────


def test_inactive_nurse_takes_no_seat(env):
    _team_limit(env, 1, 1)
    _request(env, "N002", {14: V})
    env.query(Nurse).filter(Nurse.nurse_id == "N002").update({Nurse.active: 0})
    _check(env, "N005", _request(env, "N005", {14: V}, submitted=False))


def test_mid_month_joiner_counts_for_first_team(env):
    """1일을 덮는 구간이 없으면 그 달 첫 구간의 팀 — 근무자관리·생성기와 같은 팀으로 센다."""
    env.add(NurseTeamPeriod(nurse_id="N006", group_id=G, valid_from=date(Y, M, 10), team_id=1, source="edited"))
    env.flush()
    _team_limit(env, 1, 1)
    _request(env, "N006", {14: V})
    with pytest.raises(PreferenceQuotaError):
        _check(env, "N005", _request(env, "N005", {14: V}, submitted=False))


def test_inactive_team_members_are_unassigned(env):
    _team_limit(env, 2, 0)
    env.query(Team).filter(Team.group_id == G, Team.team_id == 2).update({Team.active: 0})
    _check(env, "N003", _request(env, "N003", {14: V}, submitted=False))  # B팀 비활성 → 기타 → 팀 한도 밖


# ── 설정 조회(팀 OFF 개수 모달) ─────────────────────────────────────────────


def test_team_limits_view_members_effective_and_warnings(env):
    env.query(Nurse).filter(Nurse.nurse_id == "N006").update({Nurse.team_id: None})
    _ward_limit(env, 14, 2)
    _team_limit(env, 1, 2)
    _team_limit(env, 2, 1)
    _request(env, "N002", {14: V})
    _request(env, "N003", {14: V})
    view = quota.get_team_limits(env, G, Y, M)
    assert view["rule"] == "team_first" and V in view["counted_shift_ids"]
    assert [(m["nurse_id"], m["team_id"]) for m in view["members"]] == [
        ("N001", 1), ("N002", 1), ("N003", 2), ("N004", 2), ("N005", 1), ("N006", None)]
    assert view["unassigned_count"] == 1
    assert view["ward_limits"] == {"2026-11-14": 2}
    assert len(view["effective"]) == 30  # 팀 기본값이 있어 한 달 전체
    assert view["effective"]["2026-11-14"] == {
        "ward": {"limit": 2, "taken": 2},
        "teams": [{"team_id": 1, "basis": "team", "limit": 2, "taken": 1},
                  {"team_id": 2, "basis": "team", "limit": 1, "taken": 1}],
        "unassigned": {"basis": "ward", "limit": 2, "taken": 2},
    }
    assert view["effective"]["2026-11-15"]["unassigned"] == {"basis": None, "limit": None, "taken": 0}
    # 최대 인원 = min(병동 2, 기타 1) + min(A 2, 3명) + min(B 1, 2명) = 4 > 2
    assert view["warnings"] == [{"kind": "ward_limit_may_overflow", "date": "2026-11-14", "ward_limit": 2,
                                 "max_total": 4, "team_ids": [1, 2]}]


def test_ward_overflow_warning_counts_ward_basis_members_first(env):
    """팀 한도 합이 병동 한도 이하여도, 병동 기준 인원(한도 없는 팀·기타)이 먼저 채우면 병동 한도를 넘는다(11-04 리뷰)."""
    _ward_limit(env, 14, 2)
    _team_limit(env, 1, 1)                                 # A팀만 한도 1 · B팀(3명)은 병동 기준
    view = quota.get_team_limits(env, G, Y, M)
    # 최대 인원 = min(병동 2, B팀 3명) + min(A 1, 3명) = 3 > 2 — 팀 한도 합(1)만 보면 놓친다
    assert view["warnings"] == [{"kind": "ward_limit_may_overflow", "date": "2026-11-14", "ward_limit": 2,
                                 "max_total": 3, "team_ids": [1]}]


def test_team_limits_view_hides_inactive_team_rows(env):
    """비활성 팀의 날짜 예외는 조회에 싣지 않는다 — 받은 그대로 되보낸 저장(통째 교체)이 400 이 되지 않게(11-04 리뷰)."""
    _team_limit(env, 2, 0, day=20)
    env.query(Team).filter(Team.group_id == G, Team.team_id == 2).update({Team.active: 0})
    view = quota.get_team_limits(env, G, Y, M)
    assert view["dates"] == []
    saved = quota.replace_team_limits(env, G, Y, M, defaults=[], dates=view["dates"])
    assert saved["dates"] == []


# ── 정합성 결함(11-04 검토) ────────────────────────────────────────────────


def test_retract_drops_every_submission_of_the_month(env):
    """철회는 그 달 제출본을 모두 내린다 — 옛 AIDE 편집이 남긴 이전 제출본이 되살아나 자리를 잡지 않게."""
    _team_limit(env, 1, 1)
    _request(env, "N002", {14: V}, submitted_at=datetime(2026, 10, 1))
    _request(env, "N002", {15: V}, request_id=2, submitted_at=datetime(2026, 10, 2))
    retract_submission_service(PreferenceSubmit(year=Y, month=M), _user("N002"), env)
    still = env.query(WantedRequest).filter(
        WantedRequest.nurse_id == "N002", WantedRequest.month == MONTH,
        WantedRequest.is_submitted == True,  # noqa: E712
    ).all()
    assert still == []
    _check(env, "N005", _request(env, "N005", {14: V}, submitted=False))  # 14일 자리가 비었다


def test_aide_edit_retires_previous_submission(env):
    """AIDE 날짜 추가는 새 제출본을 만들며 이전 제출본을 내린다(제출본은 간호사·달마다 1건)."""
    _request(env, "N002", {14: V})
    result = wanted_tools.add_wanted_by_date(env, "N002", G, Y, M, "2026-11-15", V, preview_only=False)
    assert result["status"] == "submitted"
    rows = {r.request_id: r.is_submitted for r in env.query(WantedRequest).filter(WantedRequest.nurse_id == "N002")}
    assert rows == {1: False, 2: True}


def test_aide_self_write_follows_quota_and_deadline(env):
    """간호사 본인 AIDE 쓰기는 화면 제출과 같은 일자 한도·마감을 거친다. 수간호사(규칙 밖)는 통과.

    ★ 한도에 걸리면 그 초안만 savepoint 로 되돌린다 — 세션 rollback 은 같은 AIDE 턴의 감사 기록까지 날린다
      (여기선 시드까지 사라져 아래 단계가 깨진다).
    """
    _team_limit(env, 1, 1)
    _request(env, "N002", {14: V})
    _request(env, "N005", {20: V})
    over = wanted_tools.add_wanted_by_date(env, "N005", G, Y, M, "2026-11-14", V,
                                           preview_only=False, self_rules=True)
    assert over["code"] == "DAILY_QUOTA_EXCEEDED" and over["detail"]["dates"][0]["basis"] == "team"
    assert {r.request_id for r in env.query(WantedRequest).filter(WantedRequest.nurse_id == "N005")} == {1}
    ok = wanted_tools.add_wanted_by_date(env, "N005", G, Y, M, "2026-11-14", V,
                                         preview_only=False, self_rules=False)
    assert ok["status"] == "submitted"
    env.query(Wanted).filter(Wanted.group_id == G).update({Wanted.status: "closed"})
    closed = wanted_tools.cancel_wanted_request(env, "N005", G, Y, M, self_rules=True)
    assert "마감" in closed["error"]


def test_aide_self_cancel_retires_other_ward_submission_too(env):
    """간호사 본인 AIDE 철회는 화면 철회처럼 병동을 보지 않는다 — 옮기기 전 병동 제출본이 남으면 화면 저장이
    '이미 제출'로 계속 막힌다. 수간호사 철회는 이 병동 것만 내린다(11-04 검증)."""
    for nurse_id in ("N002", "N005"):
        _request(env, nurse_id, {14: V})
        env.add(WantedRequest(nurse_id=nurse_id, request_id=2, month=MONTH, group_id="GRP_OLD", request="",
                              is_submitted=True, created_at=datetime(2026, 10, 2), submitted_at=datetime(2026, 10, 2)))
    env.flush()
    wanted_tools.cancel_wanted_request(env, "N002", G, Y, M, self_rules=True)
    wanted_tools.cancel_wanted_request(env, "N005", G, Y, M, self_rules=False)

    def _submitted(nurse_id):
        return {r.group_id for r in env.query(WantedRequest).filter(
            WantedRequest.nurse_id == nurse_id, WantedRequest.month == MONTH, WantedRequest.is_submitted == True)}  # noqa: E712

    assert _submitted("N002") == set()
    assert _submitted("N005") == {"GRP_OLD"}


def test_retract_waits_on_lock_with_limit(env, monkeypatch):
    """화면 철회도 원티드 행 잠금에 대기 상한을 건다 — 제출·임시저장·AIDE 와 같은 행을 잡는다(11-04 검증)."""
    used: list = []
    real = quota._lock_wait_limit

    @contextmanager
    def _spy(db, timeout_ms, *args, **kwargs):
        used.append(timeout_ms)
        with real(db, timeout_ms, *args, **kwargs):
            yield

    monkeypatch.setattr(quota, "_lock_wait_limit", _spy)
    _request(env, "N002", {14: V})
    retract_submission_service(PreferenceSubmit(year=Y, month=M), _user("N002"), env)
    assert used == [quota._LOCK_TIMEOUT_MS]


@pytest.mark.parametrize("self_rules", [True, False])
def test_aide_edit_copies_submission_committed_while_waiting(env, monkeypatch, self_rules):
    """잠금을 기다리는 사이 커밋된 편집을 기준으로 복사한다 — 잠금 전에 읽으면 그 편집을 덮는다(11-04 Codex).

    수간호사 편집(self_rules=False)도 같은 병동·월 잠금에 줄을 선다.
    """
    _request(env, "N002", {14: V})
    real_lock = quota.lock_wanted_month
    state = {"other_done": False}

    def _lock_then_other_edit(db, group_id, year, month):
        real_lock(db, group_id, year, month)
        if not state["other_done"]:
            state["other_done"] = True        # 기다리는 사이 다른 편집(15일 추가)이 먼저 커밋된다
            wanted_tools.add_wanted_by_date(db, "N002", G, Y, M, "2026-11-15", V, preview_only=False)

    monkeypatch.setattr(quota, "lock_wanted_month", _lock_then_other_edit)
    result = wanted_tools.add_wanted_by_date(env, "N002", G, Y, M, "2026-11-16", V,
                                             preview_only=False, self_rules=self_rules)
    assert result["status"] == "submitted"
    latest = env.query(WantedRequest).filter(
        WantedRequest.nurse_id == "N002", WantedRequest.month == MONTH, WantedRequest.is_submitted == True,  # noqa: E712
    ).one()
    cells = sorted((r.shift_date.month, r.shift_date.day) for r in env.query(NurseShiftRequest).filter(
        NurseShiftRequest.nurse_id == "N002", NurseShiftRequest.request_id == latest.request_id))
    # 시드의 4월 제출본도 request_id 1 이다 — 이 달 칸만 복사해야 한다(다른 달 같은 번호 칸이 딸려 오지 않게)
    assert cells == [(11, 14), (11, 15), (11, 16)]


def test_aide_date_outside_request_month_is_rejected(env):
    """요청 연월 밖 날짜는 쓰기 전에 거절한다 — 아니면 다른 달 칸이 이 달 마감·한도 검사를 비켜 저장된다(11-04 Codex)."""
    _request(env, "N002", {14: V})
    calls = [
        lambda: wanted_tools.add_wanted_by_date(env, "N002", G, Y, M, "2026-12-14", V,
                                                preview_only=False, self_rules=True),
        lambda: wanted_tools.modify_wanted_by_date(env, "N002", G, Y, M, "2026-12-14", V,
                                                   preview_only=False, self_rules=True),
        lambda: wanted_tools.delete_wanted_by_date(env, "N002", G, Y, M, "2026-12-14",
                                                   preview_only=False, self_rules=True),
    ]
    for call in calls:
        assert "2026년 11월 날짜가 아닙니다" in call()["error"]
    assert {r.request_id for r in env.query(WantedRequest).filter(
        WantedRequest.nurse_id == "N002", WantedRequest.month == MONTH)} == {1}


def test_late_draft_save_cannot_change_request_submitted_meanwhile(env, monkeypatch):
    """임시저장도 병동·월 잠금 뒤에 확인한다 — 기다리는 사이 같은 draft 가 제출되면 그 제출본 칸을 바꾸지 않는다(11-04 Codex)."""
    _ward_limit(env, 14, 0)                                    # 14일 마감 — 한도 검사 없이 제출본에 들어가면 안 된다
    rid = _request(env, "N003", {13: V}, submitted=False)      # 임시저장해 둔 draft(13일)
    real_lock = quota.lock_wanted_month
    state = {"submitted": False}

    def _lock_then_submit(db, group_id, year, month):
        real_lock(db, group_id, year, month)
        if not state["submitted"]:
            state["submitted"] = True                          # 기다리는 사이 같은 draft 가 제출·커밋된다
            submit_preferences_service(PreferenceData(year=Y, month=M), _user("N003"), db, is_draft=False)

    monkeypatch.setattr(quota, "lock_wanted_month", _lock_then_submit)
    late = PreferenceData(year=Y, month=M, wanted_entries=[
        {"date": "2026-11-14", "shift_id": V, "intent": "wanted"}])
    with pytest.raises(PreferenceConflictError, match="이미 제출"):
        save_wanted_entries_service(late, _user("N003"), env, is_draft=True)
    days = sorted(r.shift_date.day for r in env.query(NurseShiftRequest).filter(
        NurseShiftRequest.nurse_id == "N003", NurseShiftRequest.request_id == rid,
        NurseShiftRequest.shift_date >= date(Y, M, 1)))
    assert days == [13]


def _april_rows(db, nurse_id):
    """시드 2026-04 제출본(request_id 1)의 신청 칸·짝 신청."""
    shifts = sorted((r.shift_date, r.shift) for r in db.query(NurseShiftRequest).filter(
        NurseShiftRequest.nurse_id == nurse_id, NurseShiftRequest.request_id == 1,
        NurseShiftRequest.shift_date < date(2026, 5, 1)))
    pairs = sorted(p.target_id for p in db.query(NursePairRequest).filter(
        NursePairRequest.nurse_id == nurse_id, NursePairRequest.month == "2026-04"))
    return shifts, pairs


def _accept_string_dates(db, monkeypatch):
    """구형 경로는 신청 날짜를 문자열로 넣는다 — pymssql 은 받고 테스트 SQLite 는 date 만 받아 바꿔 넣는다."""
    real_add = db.add

    def _add(obj, *args, **kwargs):
        if isinstance(obj, NurseShiftRequest) and isinstance(obj.shift_date, str):
            obj.shift_date = date.fromisoformat(obj.shift_date)
        return real_add(obj, *args, **kwargs)

    monkeypatch.setattr(db, "add", _add)


@pytest.mark.parametrize("is_draft", [True, False])
def test_legacy_data_outside_request_month_is_rejected(env, monkeypatch, is_draft):
    """구형 data 의 다른 달 날짜는 쓰기 전에 거절한다(422) — 다른 달 같은 번호 제출본에 붙어 그 달 마감·한도를 비켜 간다(11-04 Codex)."""
    _accept_string_dates(env, monkeypatch)
    april = _april_rows(env, "N001")                   # 시드 4월 제출본(request_id 1) — 11월 새 draft 도 request_id 1
    data = {"shift": None, "2026-04-20": V}            # 평평한 모양 + 11월 요청에 4월 날짜
    with pytest.raises(PreferenceValidationError, match="속하지 않는 날짜"):
        submit_preferences_service(PreferenceData(year=Y, month=M, data=data), _user("N001"), env, is_draft=is_draft)
    assert _april_rows(env, "N001") == april


def test_legacy_submit_keeps_other_months_with_same_request_id(env, monkeypatch):
    """구형 data 제출은 이 달 칸·짝만 지운다 — request_id 는 간호사·월마다 1부터라 다른 달에도 같은 번호가 있다."""
    _accept_string_dates(env, monkeypatch)
    april = _april_rows(env, "N001")
    assert april[0] and april[1]                       # 칸 3 · 짝 2
    _request(env, "N001", {14: V}, submitted=False)    # 11월 임시저장도 request_id 1
    data = {"shift": {V: {"15": 1}}}                   # 구형 중첩 모양(이 모양이어야 칸을 지우고 다시 쓴다)
    submit_preferences_service(PreferenceData(year=Y, month=M, data=data), _user("N001"), env, is_draft=False)
    assert [(r.shift_date, r.shift) for r in env.query(NurseShiftRequest).filter(
        NurseShiftRequest.nurse_id == "N001", NurseShiftRequest.shift_date >= date(Y, M, 1))] == [(date(Y, M, 15), V)]
    assert _april_rows(env, "N001") == april


# ── 잠금 대기 상한(MSSQL) ─────────────────────────────────────────────────


class _FakeMssql:
    """`_lock_wait_limit` 이 보내는 문장만 기록하는 가짜 세션."""

    def __init__(self):
        self.sent = []
        bind = type("Bind", (), {"dialect": type("Dialect", (), {"name": "mssql"})()})()
        self.get_bind = lambda: bind

    def execute(self, stmt, *args, **kwargs):
        self.sent.append(str(stmt))


def test_lock_wait_limit_maps_timeout_and_restores_setting():
    fake = _FakeMssql()
    timeout = OperationalError("SELECT", {}, Exception(1222, b"Lock request time out period exceeded."))
    with pytest.raises(PreferenceConflictError, match="잠시 후 다시"), quota._lock_wait_limit(fake, 10000):
        raise timeout
    assert fake.sent == ["SET LOCK_TIMEOUT 10000", "SET LOCK_TIMEOUT -1"]
    other = OperationalError("SELECT", {}, Exception(1205, b"deadlock"))
    with pytest.raises(OperationalError), quota._lock_wait_limit(fake, 10000):
        raise other
    assert fake.sent[-1] == "SET LOCK_TIMEOUT -1"


# ── 전체 원티드 조회 일괄화(11-04 최적화) ─────────────────────────────────


def test_all_preferences_batch_keeps_each_nurses_latest_request(env, monkeypatch):
    """칸을 한 번에 읽어도 간호사마다 자기 최신 제출본 칸만 — request_id 는 간호사마다 따로 매긴다."""
    import services.preferences_service as ps

    monkeypatch.setattr(ps, "_resolve_all_preferences_scope", lambda *a, **k: (G, True))
    _request(env, "N002", {14: V}, request_id=1, submitted_at=datetime(2026, 10, 1))
    _request(env, "N002", {15: V}, request_id=2, submitted_at=datetime(2026, 10, 2))  # 최신
    _request(env, "N003", {16: V}, request_id=2)  # 같은 번호, 다른 간호사
    rows = {r["nurse_id"]: r["data"]["shift"] for r in ps.get_all_preferences_service(Y, M, _user("N001"), env)}
    assert set(rows["N002"][V]) == {"15"}
    assert set(rows["N003"][V]) == {"16"}
