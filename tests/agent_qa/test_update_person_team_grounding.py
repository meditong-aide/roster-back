"""update-person-attr: team_name → team_id 내부 그라운딩 검증.

2026-05-30 추가. LLM 이 'A팀' 같은 팀 이름 문자열을 그대로 전달해도 스킬이
DB 조회로 team_id 로 매핑한다. 모르는 팀은 needs_clarification.
"""
from __future__ import annotations

import pytest

from agents_v2.skills.update_person_attr import update_person_attr
from db.models import Nurse, Team


@pytest.fixture
def seeded(db):
    db.add_all([
        Team(office_id="of1", group_id="g1", team_id=1, team_name="A팀", active=1),
        Team(office_id="of1", group_id="g1", team_id=2, team_name="B팀", active=1),
        Nurse(
            nurse_id="n1", account_id="acc_n1", name="이유림",
            office_id="of1", group_id="g1",
            team_id=2, grade=2, experience=3, active=1,
        ),
    ])
    db.flush()
    return db


def test_team_name_string_resolves(seeded):
    res = update_person_attr(seeded, {
        "nurse_ids": ["n1"],
        "office_id": "of1",
        "group_id": "g1",
        "field": "team_id",
        "value": "A팀",
    })
    assert "error" not in res, res
    nurse = seeded.query(Nurse).filter(Nurse.nurse_id == "n1").first()
    assert nurse.team_id == 1


def test_int_team_id_still_works(seeded):
    res = update_person_attr(seeded, {
        "nurse_ids": ["n1"],
        "office_id": "of1",
        "group_id": "g1",
        "field": "team_id",
        "value": 1,
    })
    assert "error" not in res, res
    nurse = seeded.query(Nurse).filter(Nurse.nurse_id == "n1").first()
    assert nurse.team_id == 1


def test_digit_string_team_id(seeded):
    res = update_person_attr(seeded, {
        "nurse_ids": ["n1"],
        "office_id": "of1",
        "group_id": "g1",
        "field": "team_id",
        "value": "1",
    })
    assert "error" not in res, res
    nurse = seeded.query(Nurse).filter(Nurse.nurse_id == "n1").first()
    assert nurse.team_id == 1


def test_unknown_team_returns_clarification(seeded):
    res = update_person_attr(seeded, {
        "nurse_ids": ["n1"],
        "office_id": "of1",
        "group_id": "g1",
        "field": "team_id",
        "value": "Z팀",
    })
    assert res.get("needs_clarification") is True
    assert "A팀" in res["options"] and "B팀" in res["options"]
    nurse = seeded.query(Nurse).filter(Nurse.nurse_id == "n1").first()
    assert nurse.team_id == 2


def test_preview_with_team_name(seeded):
    res = update_person_attr(seeded, {
        "nurse_ids": ["n1"],
        "office_id": "of1",
        "group_id": "g1",
        "field": "team_id",
        "value": "A팀",
        "preview_only": True,
    })
    assert res.get("preview") is True
    applied = res["applied_mutations"][0]
    assert applied["field"] == "team_id"
    assert applied["new_value"] == 1


def test_multi_mutation_team_name_with_grade(seeded):
    res = update_person_attr(seeded, {
        "nurse_ids": ["n1"],
        "office_id": "of1",
        "group_id": "g1",
        "mutations": [
            {"field": "team_id", "value": "A팀"},
            {"field": "grade", "value": 3},
        ],
    })
    assert "error" not in res, res
    nurse = seeded.query(Nurse).filter(Nurse.nurse_id == "n1").first()
    assert nurse.team_id == 1
    assert nurse.grade == 3
