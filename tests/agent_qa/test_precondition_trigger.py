"""축③ 선행조건 트리거 — 확정 근무표 상태가시성 + bulk_mutation 트리거 인코딩 검증.

- query_schedule(schedule scope)이 확정본(IssuedRoster)일 때 roster_source 를 노출하는지
- bulk_mutation description 에 확정-근무표 선행조건 트리거가 인코딩됐는지
(라이브 A/B로 트리거가 실제 clarify를 유발함은 별도 확인 완료: baseline 0/3 → trigger 3/3)
"""

from __future__ import annotations

from agents_v2.skills.descriptions import SKILL_TOOLS
from agents_v2.skills.query_schedule import _query_schedule_entries
from db.models import IssuedRoster


def test_bulk_mutation_encodes_finalized_precondition():
    d = next(t for t in SKILL_TOOLS if t["name"] == "bulk_mutation")["description"]
    assert "선행조건(확정 근무표)" in d
    # int-0b: 확정본 칸은 에이전트가 고치지 않는다 → 근무표 만들기 화면에서 마감 철회 후 수정으로 안내
    assert "마감 철회" in d
    assert "직접 수정할까요" not in d
    assert "확정된 근무표의 시프트 셀" not in d  # 앞부분 정의가 뒤의 금지와 모순되지 않게
    assert "[새 버전으로 저장]" not in d  # 화면에 없는 버튼 이름을 안내하지 않는다


def test_draft_roster_returns_plain_list(db, seed_data):
    # 시드엔 IssuedRoster 없음 → 작업본(draft) → 기존 list 그대로(무변경)
    res = _query_schedule_entries(db, "GRP001", 2026, 4, {"nurse_ids": ["N001"]})
    assert isinstance(res, list)


def test_finalized_roster_surfaces_roster_source(db, seed_data):
    # 확정본(IssuedRoster) 발행 → query_schedule 이 roster_source 를 노출해야 트리거가 볼 수 있음
    db.add(IssuedRoster(
        seq_no=1, version=1, office_id="OFF001", group_id="GRP001",
        nurse_id="N001", schedule_id="SCH202604V1", is_active=True,
    ))
    db.flush()

    res = _query_schedule_entries(db, "GRP001", 2026, 4, {"nurse_ids": ["N001"]})
    assert isinstance(res, dict)
    assert "확정본" in res.get("roster_source", "")
    assert isinstance(res.get("entries"), list) and res["entries"]
