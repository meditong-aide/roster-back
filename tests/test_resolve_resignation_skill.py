"""resolve-resignation 스킬 grounding 테스트.

이름→id, 근무표 해석, 퇴사일 파싱 후 부분 재생성 '준비된 액션'을 반환하는지 검증.
(실제 솔버 실행은 상위 레이어 몫 — generate-schedule 의 _sqs_dispatch_required 패턴.)
"""
from __future__ import annotations

from datetime import date

from db.models import Nurse, Schedule
from agents_v2.skills.resolve_resignation import resolve_resignation, _parse_cutoff
from agents_v2.skills.registry import run_skill


def _seed(db):
    db.add_all([
        Nurse(nurse_id="n1", account_id="n1", name="김민지", group_id="g1", active=1, sequence=1),
        Nurse(nurse_id="n2", account_id="n2", name="이영희", group_id="g1", active=1, sequence=2),
        Nurse(nurse_id="n3", account_id="n3", name="박서준", group_id="g1", active=1, sequence=3),
        Schedule(schedule_id="schedAAAAAAA", group_id="g1", year=2026, month=3,
                 version=1, dropped=False),
    ])
    db.flush()


def test_parse_cutoff_forms():
    assert _parse_cutoff("2026-03-16", 2026, 3) == date(2026, 3, 16)
    assert _parse_cutoff("3월 16일", 2026, 3) == date(2026, 3, 16)
    assert _parse_cutoff("16", 2026, 3) == date(2026, 3, 16)
    assert _parse_cutoff("", 2026, 3) is None
    assert _parse_cutoff("헛소리", 2026, 3) is None


def test_grounds_and_prepares_action(db):
    _seed(db)
    out = resolve_resignation(db, {
        "group_id": "g1", "year": 2026, "month": 3,
        "resigned_nurse": "김민지", "cutoff_date": "2026-03-16",
    })
    assert out["_partial_resolve_required"] is True
    assert out["resigned_nurse_id"] == "n1"
    assert out["resigned_nurse_name"] == "김민지"
    assert out["cutoff_date"] == "2026-03-16"
    assert out["schedule_id"] == "schedAAAAAAA"
    assert out["_partial_resolve_params"]["resigned_nurse_id"] == "n1"


def test_replacement_preceptor_grounding(db):
    _seed(db)
    out = resolve_resignation(db, {
        "group_id": "g1", "year": 2026, "month": 3,
        "resigned_nurse": "이영희", "cutoff_date": "3월 16일",
        "replacement_preceptor": "박서준",
    })
    assert out["replacement_preceptor_id"] == "n3"
    assert out["replacement_preceptor_name"] == "박서준"
    assert out["cutoff_date"] == "2026-03-16"


def test_nurse_not_found_returns_error(db):
    _seed(db)
    out = resolve_resignation(db, {
        "group_id": "g1", "year": 2026, "month": 3,
        "resigned_nurse": "없는사람", "cutoff_date": "2026-03-16",
    })
    assert "error" in out and "_partial_resolve_required" not in out


def test_registered_and_dispatchable(db):
    _seed(db)
    out = run_skill(db, "resolve-resignation", {
        "group_id": "g1", "year": 2026, "month": 3,
        "resigned_nurse": "김민지", "cutoff_date": "2026-03-16",
    })
    assert out["resigned_nurse_id"] == "n1"
