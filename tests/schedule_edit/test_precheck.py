"""바꾸기 전 산술 점검(11-03 · 사용자 '무조건 바꾸지 말고 위반이 셈으로 나오면 먼저 알리고 동의') — 반드시 어기게 되는
것만 짚는다(전월 근무 꼬리까지 · 틀린 경고 없음)."""

from __future__ import annotations

from services.schedule_edit.context import FIXED_WANTED
from services.schedule_edit.precheck import apply_findings, finding_from_json, finding_to_json, precheck
from services.schedule_edit.requests import CountTarget, CoverTarget, SetCell
from services.schedule_edit.rules import build_rules
from services.schedule_edit.solver import propose_edits
from tests.schedule_edit.test_solver import BEFORE_MONTH, make_ctx

LOOSE = {"not_one_night": False}


def _check(ctx, *requests, allow=None):
    return precheck(ctx, build_rules(ctx), list(requests), open_from=0, allow=allow)


def _tight():
    """A 는 3·4일이 바꿀 수 없는 OFF 라 1일을 OFF 로 하면 OFF 수(2개)를 반드시 넘는다."""
    ctx = make_ctx({"A": "DDOO"}, off_days=2, config=LOOSE)
    ctx.frozen[("A", 2)] = ctx.frozen[("A", 3)] = "주휴·특수 휴무"
    return ctx


def test_a_request_with_room_has_nothing_to_ask():
    ctx = make_ctx({"A": "DDOO", "B": "OODD"}, cov_min={"D": 1}, config=LOOSE)
    out = _check(ctx, SetCell("A", 1, "O"))
    assert out.blocked == [] and out.findings == []           # 빠진 D 는 B 가 메울 수 있다 — 묻지 않는다


def test_unavoidable_setting_is_asked_with_the_amount_to_pass():
    out = _check(_tight(), SetCell("A", 0, "O"))
    assert out.blocked == []
    assert [(f.kind, f.text) for f in out.findings] == [("setting", "A OFF 수 상한보다 1개 많게")]
    assert out.findings[0].allow == {("OFF수", "A", None, None, "상한"): 1}
    # 이미 그만큼 허용했으면(고른 레버 등) 다시 묻지 않는다
    assert _check(_tight(), SetCell("A", 0, "O"), allow=out.findings[0].allow).findings == []


def test_previous_month_tail_is_part_of_the_window():
    # 전월 말 N N 뒤 1일 D — 이번 달만 보면 깨끗하지만 N→D 전환 금지에 걸린다(하드락이라 묻지 않고 막는다)
    ctx = make_ctx({"A": "OODD", "B": "DDOO"}, config=LOOSE)
    ctx.tail = {"A": ["O", "N", "N"], "B": ["O", "O", "O"]}
    out = _check(ctx, SetCell("A", 0, "D"))
    assert out.findings == [] and "전환금지" in out.families
    assert any("N→D" in b and "전월 말일 N" in b for b in out.blocked), out.blocked


def test_consecutive_work_across_the_month_boundary_names_the_fixed_days():
    ctx = make_ctx({"A": "OOOOO", "B": "DDDOO"}, config=LOOSE)
    ctx.tail = {"A": ["D", "D", "D"], "B": ["O", "O", "O"]}
    out = _check(ctx, SetCell("A", 0, "D"), SetCell("A", 1, "D"), SetCell("A", 2, "D"))
    assert out.blocked == ["A 1·2·3일 요청대로 바꾸면 최대 연속 근무일 수에 걸립니다(바꿀 수 없는 칸: 전월 말일 2일 전 D, 전월 말일 1일 전 D, "
                           "전월 말일 D)."]


def test_count_target_against_personal_limit_and_monthly_night_cap():
    ctx = make_ctx({"A": "NNOONNOO", "B": "OONNOONN"},
                   config={**LOOSE, "max_nig_per_month": 5, "two_offs_after_two_nig": False})
    ctx.monthly_limits["A"] = {"n_max": 4}
    out = _check(ctx, CountTarget("A", frozenset({"N"}), 5, 0, 7))
    assert [(f.kind, f.text) for f in out.findings] == [("setting", "A 월 N 상한보다 1회 많게")]
    assert out.findings[0].allow == {("개인한도", "A", None, "n", "상한"): 1}
    blocked = _check(ctx, CountTarget("A", frozenset({"N"}), 6, 0, 7)).blocked
    assert blocked == ["A — 요청대로면 이 달 N이 6회 이상이라 월 최대 야간 근무 수(5회)를 넘습니다."]


def test_cover_request_below_minimum_is_asked_with_counts():
    ctx = make_ctx({"A": "DD", "B": "DD", "C": "OO"}, cov_min={"D": 2}, config=LOOSE)
    out = _check(ctx, CoverTarget(0, "D", 1))
    assert [(f.kind, f.text) for f in out.findings] == [("cover", "1일 D 2명 → 1명 — 최소 근무 인원(2명)보다 1명 적음")]
    assert out.findings[0].allow == {}                        # 그날 인원 요청은 풀이가 이미 봐준다 — 동의만 받는다


def test_direct_request_on_wanted_cells_is_asked_first():
    ctx = make_ctx({"A": "OD", "B": "DO"}, config=LOOSE)
    ctx.wishes[("A", 0)] = {"O"}
    ctx.frozen[("B", 0)] = FIXED_WANTED
    out = _check(ctx, SetCell("A", 0, "D"), SetCell("B", 0, "O"))
    assert [(f.kind, f.text) for f in out.findings] == [
        ("wanted", "A 1일 O — 원티드로 받은 칸이라 바꾸면 그 원티드를 지키지 못함"),
        ("wanted", "B 1일 D — 확정 원티드로 받은 칸이라 바꾸면 그 원티드를 지키지 못함")]


def test_apply_findings_carry_the_same_ids_as_what_was_confirmed():
    ctx, request = _tight(), [SetCell("A", 0, "O")]
    asked = _check(ctx, *request).findings
    result = propose_edits(ctx, request, allow=asked[0].allow, today=BEFORE_MONTH)
    assert result.status == "ok", result.reasons
    found = apply_findings(ctx, build_rules(ctx), result.candidates[0], request)
    assert asked[0].id in {f.id for f in found}               # 앞에서 동의했으면 적용 전에 다시 묻지 않는다
    assert finding_from_json(finding_to_json(asked[0])) == asked[0]
