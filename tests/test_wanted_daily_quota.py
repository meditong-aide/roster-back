"""원티드 일자별 신청 한도(병동 전체 + 팀별) — 제출 검사·작성 화면 조회·팀 한도 설정.

검증 대상(기획 `.omc/plans/wanted_team_daily_limit_plan.md` 4장 판정 규칙):
- 팀 기본값 / 날짜 예외 우선 / 병동 전체 한도 함께 적용 / 팀 미배정자는 병동 한도만
- 세는 대상 = 휴가·휴무 계열 '희망'의 **최신 제출본**만(임시저장·철회·근무코드·공가 제외)
- 제출 경로(wanted_entries · 구형 data) 둘 다 409 로 막고 다른 팀은 통과
- 작성 화면 조회(daily_quota) · 설정 저장 검증·초과 경고 · 저장소 없으면 팀 한도 건너뜀

시드 팀: A팀(1) = N001·N002·N005 / B팀(2) = N003·N004·N006 (팀 구간 없음 → nurses.team_id 폴백).
"""
from datetime import date, datetime

import pytest
import services.wanted_daily_quota_service as quota
from db.models import (
    Nurse,
    NurseShiftRequest,
    Shift,
    Wanted,
    WantedConfig,
    WantedRequest,
    WantedTeamLimit,
)
from schemas.auth_schema import User as UserSchema
from schemas.roster_schema import PreferenceData
from services.preferences_service import (
    PreferenceQuotaError,
    save_wanted_entries_service,
    submit_preferences_service,
)

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
    # 저장소 확인 캐시가 다른 테스트로 새지 않게(이 모듈 밖에서는 표가 없다).
    monkeypatch.setattr(quota, "_TEAM_LIMIT_READY", False)
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
    assert item == {"date": "2026-11-14", "scope": "team", "team_id": 1, "team_name": "A팀",
                    "limit": 1, "used": 1}
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
    _ward_limit(env, 14, 1)
    _request(env, "N002", {14: V})
    with pytest.raises(PreferenceQuotaError) as exc:
        _check(env, "N003", _request(env, "N003", {14: V}, submitted=False))
    assert [i["scope"] for i in exc.value.detail["dates"]] == ["ward"]
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
    assert (cell["team_limit"], cell["team_used"], cell["mine"]) == (1, 1, True)


def test_legacy_submit_of_saved_draft_rejected(env):
    """구형 경로 — 임시저장해 둔 뒤 {year, month} 만 보내 제출(모바일 흐름). 저장된 draft 칸을 센다."""
    _ward_limit(env, 14, 1)
    _request(env, "N003", {14: V})
    _request(env, "N005", {14: V}, submitted=False)
    with pytest.raises(PreferenceQuotaError):
        submit_preferences_service(PreferenceData(year=Y, month=M), _user("N005"), env, is_draft=False)


def test_draft_save_is_not_checked(env):
    _team_limit(env, 1, 0)
    body = PreferenceData(year=Y, month=M, wanted_entries=[
        {"date": "2026-11-14", "shift_id": V, "intent": "wanted"}])
    result = save_wanted_entries_service(body, _user("N005"), env, is_draft=True)
    assert result["is_submitted"] is False


# ── 작성 화면 조회 ─────────────────────────────────────────────────────────


def test_daily_quota_view(env):
    _team_limit(env, 1, 2)
    _ward_limit(env, 14, 3)
    _request(env, "N002", {14: V})
    _request(env, "N005", {14: V})
    a = quota.daily_quota_view(env, nurse_id="N005", group_id=G, year=Y, month=M)
    assert a["team"] == {"team_id": 1, "team_name": "A팀"}
    assert len(a["dates"]) == 30  # 팀 기본값은 한 달 전체
    assert a["dates"]["2026-11-14"] == {"ward_limit": 3, "ward_used": 2, "team_limit": 2,
                                         "team_used": 2, "mine": True}
    b = quota.daily_quota_view(env, nurse_id="N003", group_id=G, year=Y, month=M)
    assert b["team_limit_enabled"] is True and b["team"]["team_id"] == 2
    assert list(b["dates"]) == ["2026-11-14"]  # B팀은 팀 한도 없음 → 병동 한도 날짜만
    assert b["dates"]["2026-11-14"]["team_used"] == 0 and b["dates"]["2026-11-14"]["mine"] is False


def test_daily_quota_view_without_limits(env):
    view = quota.daily_quota_view(env, nurse_id="N005", group_id=G, year=Y, month=M)
    assert view == {"team_limit_enabled": False, "team": None, "dates": {}}


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
    assert view["warnings"] == [{"date": "2026-11-14", "team_id": 1, "team_name": "A팀",
                                 "limit": 1, "submitted": 2}]
    cleared = quota.replace_team_limits(env, G, Y, M, defaults=[], dates=[])
    assert cleared["dates"] == [] and all(t["default_limit"] is None for t in cleared["teams"])


def test_missing_storage_skips_team_limits(env, monkeypatch):
    _team_limit(env, 1, 0)
    monkeypatch.setattr(quota, "team_limit_storage_ready", lambda db: False)
    _check(env, "N005", _request(env, "N005", {14: V}, submitted=False))
    with pytest.raises(quota.TeamLimitUnavailableError):
        quota.replace_team_limits(env, G, Y, M, defaults=[], dates=[])
