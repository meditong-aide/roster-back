"""EI-13a-2 시험 — 첫 시도 직전 빌더 호출 · 시도마다 재조회 제거(설계 ei13_15/A/SPEC.md §3).

골든 시나리오 20종(`test_dual_build.dual_build_results` 재사용)에서
  - 프로덕션이 첫 시도 직전에 만든 엔진 입력의 digest == 같은 요청의 오프라인 합본 digest,
  - 차단 경로(S9·S9-F·precheck 차단)도 빌더를 한 번 돈다(EI-10g3 — precheck 앞으로 옮겨 차단 전에 돈다 · 조회 +6/+7),
  - `AttemptSource` 는 평범한 값만 담고 시도가 값을 고쳐도 다음 시도에 안 보인다(BR-6),
  - 빌더 DB 부품이 실패하면 `AttemptSource` 가 없고 지금 경로(재조회)로 간다(A7).
쿼리 수·순서열 변화는 `expected_diff.EI13_STAGES`(골든 대조)·`test_ladder.test_attempts_do_not_reread_db` 가 본다.
"""
from __future__ import annotations

import pytest

from .test_dual_build import ALL_DUAL_BUILD_SCENARIOS, PRECHECK_BLOCK_SCENARIO, dual_build_results  # noqa: F401
from .test_mssql_shim import shim_db  # noqa: F401 — 픽스처 재사용
from .test_solve_stage import _builder, _make_prelude, _rcs, _seed
from .ward import GROUP_ID, OFFICE_ID, base_ward

#: 단계 B 를 짓기 전에 멈추는 차단 경로 — 빌더를 부르지 않는다.
BLOCKED = ("S9", "S9-F", PRECHECK_BLOCK_SCENARIO.name)
REACHED = [n for n in ALL_DUAL_BUILD_SCENARIOS if n not in BLOCKED]


def _digest_parts(ei) -> dict:
    from services.engine_input.snapshot import digest_view

    return digest_view(ei)


@pytest.mark.parametrize("name", REACHED)
def test_first_attempt_digest_equals_offline_digest(name, dual_build_results):  # noqa: F811
    """설계 핵심 단언 — 첫 시도 직전 프로덕션 엔진 입력의 digest 가 오프라인 합본의 digest 와 같다."""
    from services.engine_input.snapshot import digest

    result = dual_build_results[name]
    assert result.first_attempt_build is not None, f"{name}: 첫 시도 직전 빌더 호출이 없다"
    prod_ei, _src = result.first_attempt_build
    offline = result.offline_engine_input
    if digest(prod_ei) != digest(offline):
        a, b = _digest_parts(prod_ei), _digest_parts(offline)
        diff = [k for k in sorted(set(a) | set(b)) if a.get(k) != b.get(k)]
        raise AssertionError(f"{name}({result.path}): digest 불일치 — 다른 부품 {diff}")


@pytest.mark.parametrize("name", BLOCKED)
def test_blocked_paths_call_the_builder_once_before_the_block(name, dual_build_results):  # noqa: F811
    """EI-10g3 — 단계 B 빌더가 precheck 앞으로 와서 **차단 경로도 빌더를 한 번 돈다**(옛: 차단 경로는 0회). 쿼리 증가분은
    `test_ei10g_query_paths.py` 의 경로별 기대표가 정확히 고정한다(precheck 차단 +7 · 나이트 차단 +6)."""
    built = dual_build_results[name].first_attempt_build
    assert built is not None, f"{name}: 차단 경로에서도 빌더가 돌아야 한다(precheck 앞)"
    ei, _src = built
    assert ei is not None


@pytest.mark.parametrize("name", REACHED)
def test_first_attempt_builds_attempt_source_without_errors(name, dual_build_results):  # noqa: F811
    """DB 부품 오류가 없는 골든에서는 `AttemptSource` 가 만들어진다(S3 의 `settings_eff` 는 계산 기록이라 막지 않는다)."""
    prod_ei, src = dual_build_results[name].first_attempt_build
    assert src is not None, (name, prod_ei.load_report.errors)
    assert prod_ei.load_report.raw_column in (None, {})  # production 모드 — raw·post 를 읽지 않는다
    assert prod_ei.post == {}


def _attempt_source_fixture(shim_db):  # noqa: F811
    from db.models import Nurse

    builder = _builder()
    ward = base_ward(4)
    _seed(shim_db, ward)
    prelude = _make_prelude(ward)
    stage_a = builder.build_precheck_stage(shim_db, prelude)
    config = dict(prelude.config_dict)
    ei, src = builder.build_engine_input_at_first_attempt(
        shim_db, stage_a, preferences=[], first_attempt_config=config)
    nurses = (shim_db.query(Nurse).filter(Nurse.group_id == GROUP_ID, Nurse.role == "RN")
              .order_by(Nurse.sequence).all())
    return ward, prelude, ei, src, nurses, config


def test_attempt_source_holds_plain_values_and_matches_only_its_roster(shim_db):  # noqa: F811
    ward, _prelude, ei, src, nurses, _config = _attempt_source_fixture(shim_db)
    assert src is not None and not ei.load_report.errors
    assert src.matches(nurses)
    assert not src.matches(nurses[:-1])          # 다른 명단이면 쓰지 않는다 — 주말휴무·금지 원티드는 명단에 따라 다르다
    assert not src.matches(list(reversed(nurses)))
    blob = repr((src.team_rows, src.shift_defs, src.allowed_reads, src.cross_month_reads, src.banned, src.grade))
    assert "_sa_instance_state" not in blob and "db.models" not in blob
    assert [d["shift_id"] for d in src.shift_defs] == [s["shift_id"] for s in ward.shifts]


def test_attempts_do_not_see_each_others_mutations(shim_db, monkeypatch):  # noqa: F811
    """BR-6 — 시도가 엔진 입구에서 DB 부품 값을 고쳐도 같은 `attempt_src` 로 부른 다음 시도에 안 보인다."""
    rcs = _rcs()
    ward, prelude, _ei, src, nurses, config = _attempt_source_fixture(shim_db)
    ctx = prelude.ctx
    seen: list[dict] = []

    def _engine(nurses_dict, prefs, config_dict, year, month, shift_manage, **kwargs):
        grade = kwargs.get("grade_config")
        seen.append({
            "shift_ids": [d["shift_id"] for d in config_dict["shift_definitions"]],
            "ic": repr(config_dict["initial_constraints"]),
            "grade": repr(grade),
            "team": repr(config_dict.get("team_min_by_team")),
            "attempts": sum(1 for _ in seen),
        })
        config_dict["shift_definitions"][0]["shift_id"] = "ZZ"
        config_dict["initial_constraints"].setdefault("forbidden", {})["N01"] = {0: ["D"]}
        if isinstance(grade, dict):
            grade["constraints_json"] = {"mutated": True}
        if isinstance(config_dict.get("team_min_by_team"), dict):
            config_dict["team_min_by_team"]["mutated"] = {"D": 9}
        raise RuntimeError("stop-after-engine-entry")

    monkeypatch.setattr(rcs, "generate_roster_cp_sat", _engine)
    for _ in range(2):
        with pytest.raises(RuntimeError, match="stop-after-engine-entry"):
            rcs._run_cp_sat_basic(
                shim_db, ctx, nurses, [], None, ctx, prelude.shift_manage_data,
                config_override=dict(config), attempt_src=src)
    assert len(seen) == 2
    assert seen[0]["shift_ids"] == seen[1]["shift_ids"] and "ZZ" not in seen[1]["shift_ids"]
    assert seen[0]["ic"] == seen[1]["ic"]
    assert seen[0]["grade"] == seen[1]["grade"]
    assert seen[0]["team"] == seen[1]["team"]
    # 공유 원본도 그대로다
    assert src.shift_defs[0]["shift_id"] != "ZZ" and "mutated" not in repr(src.grade) + repr(src.banned)


def test_attempt_source_makes_run_cp_sat_basic_query_free_for_the_seven_rereads(shim_db, engine, monkeypatch):  # noqa: F811
    """재조회 0 — `attempt_src` 를 주면 ①~⑦ 표(주말휴무·팀·근무코드·허용근무·전월·금지원티드·등급)를 하나도 읽지 않는다."""
    from .capture import QueryLog

    rcs = _rcs()
    _ward, prelude, _ei, src, nurses, config = _attempt_source_fixture(shim_db)
    monkeypatch.setattr(rcs, "generate_roster_cp_sat",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("stop")))
    ctx = prelude.ctx

    def _tables(attempt_src):
        log = QueryLog()
        log.attach(engine)
        log.armed = True
        try:
            with pytest.raises(RuntimeError, match="stop"):
                rcs._run_cp_sat_basic(shim_db, ctx, nurses, [], None, ctx, prelude.shift_manage_data,
                                      config_override=dict(config), attempt_src=attempt_src)
        finally:
            log.detach()
        return log.statements

    with_src, without_src = _tables(src), _tables(None)
    assert with_src == []
    assert len(without_src) >= 7


def test_builder_part_failure_yields_no_attempt_source(shim_db, monkeypatch):  # noqa: F811
    """A7 — DB 부품(⑤)이 실패하면 `AttemptSource` 가 없다 → 호출처는 지금 경로(재조회)로 같은 자리에서 같은 오류를 겪는다."""
    builder = _builder()

    def _boom(*a, **k):
        raise RuntimeError("cross-boom")

    monkeypatch.setattr(builder, "_query_cross_month_constraints", _boom)
    ward = base_ward(2)
    _seed(shim_db, ward)
    prelude = _make_prelude(ward)
    stage_a = builder.build_precheck_stage(shim_db, prelude)
    ei, src = builder.build_engine_input_at_first_attempt(
        shim_db, stage_a, preferences=[], first_attempt_config=dict(prelude.config_dict))
    assert src is None
    assert "stage_b.cross_month" in ei.load_report.errors


def test_builder_never_raises_even_if_stage_a_is_broken(shim_db):  # noqa: F811
    builder = _builder()
    ward = base_ward(1)
    _seed(shim_db, ward)
    stage_a = builder.build_precheck_stage(shim_db, _make_prelude(ward))
    stage_a.nurses_for_engine = None          # 조립 전체가 터지는 입력
    ei, src = builder.build_engine_input_at_first_attempt(
        shim_db, stage_a, preferences=[], first_attempt_config={})
    assert src is None and ei.load_report.errors
    assert OFFICE_ID  # (import 사용)
