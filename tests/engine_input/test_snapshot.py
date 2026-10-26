"""`services.engine_input.snapshot` 단위 — EI-9a(EI9_SPEC.md §2.2, 설계서 §2.4·§7.3).

digest 제외 목록(메타 6 · post · load_report · ctx.job_id·runtime · 토글 env)과 포함 부품
(`settings_raw` 비메타 칸·`demand.post`·`grade`·`solver_params.time_limit`)을 각각 하나씩
바꿔 digest 가 '같다/다르다'를 단언한다 — 손 목록이 아니라 레지스트리에서 동적으로 비교한다
(§2.2 완료 기준). canonical 재수출 동일성과 app 쪽 import 0(AST)도 여기서 본다.
"""
from __future__ import annotations

import ast
import copy
import dataclasses
from pathlib import Path

import services.engine_input.snapshot as snapshot_module
from services.engine_contract.env import ENV_BY_NAME, digest_names
from services.engine_contract.settings import META_KEYS
from services.engine_input.model import (
    Ctx,
    DemandPart,
    DemandSnapshot,
    EngineInput,
    LoadReport,
)
from services.engine_input.snapshot import digest, digest_view

import engine_input.canonical as tests_canonical  # noqa: F401 (경로 존재 확인용 — pytest 가 tests/ 를 루트로 보므로 최상위 `engine_input` 이다)

APP_ROOT = Path(__file__).resolve().parents[2] / "app"


def _sample_ei(**overrides) -> EngineInput:
    base = dict(
        ctx=Ctx(office_id="102560", group_id="G1", year=2026, month=11, days=30,
                config_id=7, job_id="job-1", runtime="local"),
        settings_raw={
            "max_nig_per_month": 15,
            "last_generate_status": "success",
            "config_memo": "9월 마감본",
        },
        settings_eff={"max_nig_per_month": 15},
        codebook={"shift_definitions": {"D": {"id": 1}}},
        demand=DemandPart(
            pre=DemandSnapshot(base={"D": 4, "E": 3, "N": 2}),
            post=DemandSnapshot(base={"D": 3, "E": 3, "N": 2}),
        ),
        roster={"nurse_ids": ["N1", "N2"]},
        cells={"fixed": {}},
        carryover={"n_tail": {}},
        grade={"N1": 1},
        team={"min_by_team": {}},
        preferences={"N1": {}},
        post={"health_leave": {"targets": ["N1"]}},
        solver_params={"time_limit": 60},
        env_effective={"AIDE_MAX_CONSEC_WORK": "6"},
        load_report=LoadReport(errors={}, fixed_cut={"applied": False}),
    )
    base.update(overrides)
    return EngineInput(**base)


# ── 제외 칸 — 하나를 바꿔도 digest 가 같다 ─────────────────────────────────


def test_digest_same_when_meta_key_changes():
    """메타 칸 사례(설계서 §7.3) — `last_generate_status` 만 다른 두 ei 의 digest 가 같다."""
    a = _sample_ei()
    b = _sample_ei(settings_raw={**a.settings_raw, "last_generate_status": "failed"})
    assert digest(a) == digest(b)


@dataclasses.dataclass(frozen=True)
class _Case:
    name: str
    mutate: object  # Callable[[EngineInput], EngineInput]


def _mutate_ctx_job_id(ei):
    return dataclasses.replace(ei, ctx=dataclasses.replace(ei.ctx, job_id="job-2"))


def _mutate_ctx_runtime(ei):
    return dataclasses.replace(ei, ctx=dataclasses.replace(ei.ctx, runtime="ec2"))


def _mutate_post(ei):
    return dataclasses.replace(ei, post={"health_leave": {"targets": ["N1", "N2"]}})


def _mutate_load_report(ei):
    return dataclasses.replace(ei, load_report=LoadReport(errors={"fixed_cut": "boom"}))


def _mutate_each_meta_key(ei):
    changed = dict(ei.settings_raw)
    for key in META_KEYS():
        changed[key] = f"바뀐값-{key}"
    return dataclasses.replace(ei, settings_raw=changed)


def _mutate_runtime_toggle_env(ei):
    toggle_names = [name for name, spec in ENV_BY_NAME.items() if not spec.digest]
    assert toggle_names, "런타임 토글 env 가 레지스트리에 있어야 이 테스트가 뭔가를 본다"
    changed = dict(ei.env_effective)
    changed[toggle_names[0]] = "바뀐값"
    return dataclasses.replace(ei, env_effective=changed)


EXCLUDED_CASES = (
    _Case("ctx.job_id", _mutate_ctx_job_id),
    _Case("ctx.runtime", _mutate_ctx_runtime),
    _Case("post(후처리 기록)", _mutate_post),
    _Case("load_report", _mutate_load_report),
    _Case("settings_raw 메타 6칸", _mutate_each_meta_key),
    _Case("env_effective 런타임 토글", _mutate_runtime_toggle_env),
)


def test_digest_unchanged_for_each_excluded_field():
    base = _sample_ei()
    base_digest = digest(base)
    for case in EXCLUDED_CASES:
        mutated = case.mutate(base)
        assert digest(mutated) == base_digest, f"제외 칸인데 digest 가 바뀜: {case.name}"


# ── 포함 부품 — 하나를 바꾸면 digest 가 다르다 ─────────────────────────────


def _mutate_settings_raw_non_meta(ei):
    return dataclasses.replace(ei, settings_raw={**ei.settings_raw, "max_nig_per_month": 14})


def _mutate_demand_post(ei):
    new_post = dataclasses.replace(ei.demand.post, base={**ei.demand.post.base, "D": 1})
    return dataclasses.replace(ei, demand=dataclasses.replace(ei.demand, post=new_post))


def _mutate_grade(ei):
    return dataclasses.replace(ei, grade={**ei.grade, "N1": 2})


def _mutate_solver_params_time_limit(ei):
    return dataclasses.replace(ei, solver_params={**ei.solver_params, "time_limit": 180})


INCLUDED_CASES = (
    _Case("settings_raw 비메타 칸", _mutate_settings_raw_non_meta),
    _Case("demand.post 한 칸", _mutate_demand_post),
    _Case("grade 한 칸", _mutate_grade),
    _Case("solver_params.time_limit", _mutate_solver_params_time_limit),
)


def test_digest_changes_for_each_included_field():
    base = _sample_ei()
    base_digest = digest(base)
    for case in INCLUDED_CASES:
        mutated = case.mutate(base)
        assert digest(mutated) != base_digest, f"포함 부품인데 digest 가 안 바뀜: {case.name}"


# ── 손 목록 금지 — 동적 비교 ────────────────────────────────────────────────


def test_exclusion_lists_are_derived_from_registry_not_hand_written():
    """§2.2 '동적 비교 — 손 목록 금지'. snapshot 의 제외 판정이 지금 레지스트리 값과
    1:1 로 맞는지 전수 비교한다(새 META 키·새 RUNTIME_TOGGLE env 가 추가돼도 깨지지 않아야 함)."""
    meta_keys = META_KEYS()
    assert meta_keys, "레지스트리에 META 키가 있어야 이 테스트가 뭔가를 본다"
    assert snapshot_module._registry_meta_keys() == frozenset(meta_keys)

    names = digest_names()
    assert names, "digest 대상 env 가 있어야 이 테스트가 뭔가를 본다"
    for name, spec in ENV_BY_NAME.items():
        expected_excluded = not spec.digest
        assert snapshot_module._is_digest_excluded_env(name) is expected_excluded, name
    assert snapshot_module._is_digest_excluded_env("NOT_A_REGISTERED_ENV_NAME") is False


def test_digest_view_does_not_mutate_original():
    ei = _sample_ei()
    before = copy.deepcopy(snapshot_module.canonical(ei))
    digest_view(ei)
    after = snapshot_module.canonical(ei)
    assert before == after


# ── canonical 재수출 동일성(O-6) ────────────────────────────────────────────


def test_tests_canonical_reexports_same_objects_as_app_snapshot():
    """`tests/engine_input/canonical.py` 는 순수 핵심을 재수출만 한다 — 같은 함수 객체여야
    '두 벌'이 아니다(EI9_SPEC.md §6 O-6)."""
    from engine_input import canonical as tc

    for name in ("canonical", "canonical_json", "sha256_digest", "CanonicalError",
                 "_scalar", "_mapping", "_value", "_object", "_orm", "_orm_identity",
                 "_is_record_object", "_json_text", "_key_text"):
        assert getattr(tc, name) is getattr(snapshot_module, name), name


def test_canonical_json_matches_between_app_and_tests_module():
    """재수출이 아니라 동치 재구현이었다면 이 값이 달라질 수 있다 — 참조 동일성(위 테스트)과
    별개로 산출물도 같은지 한 번 더 본다."""
    from engine_input import canonical as tc

    ei = _sample_ei()
    assert tc.canonical_json(ei) == snapshot_module.canonical_json(ei)


# ── app 안 import 0 (호출처 0, EI9_SPEC.md §2.2 완료 기준) ─────────────────


def _iter_app_py_files():
    for path in APP_ROOT.rglob("*.py"):
        # engine_input 패키지 자신은 제외 — model.py/snapshot.py 가 서로 import 하는 것은 허용.
        if "engine_input" in path.relative_to(APP_ROOT).parts:
            continue
        yield path


def _imports_forbidden_module(tree: ast.Module) -> str | None:
    forbidden = {"services.engine_input.model", "services.engine_input.snapshot"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name in forbidden:
                    return alias.name
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if module in forbidden:
                return module
            if module in ("services.engine_input", "..engine_input", ".engine_input"):
                for alias in node.names:
                    if alias.name in ("model", "snapshot"):
                        return f"{module}.{alias.name}"
    return None


def test_no_app_code_imports_model_or_snapshot_yet():
    """9a 는 '호출처 0' 상태다 — engine_input 패키지 밖 그 누구도 아직 이 두 모듈을 안 쓴다."""
    offenders = []
    for path in _iter_app_py_files():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        hit = _imports_forbidden_module(tree)
        if hit:
            offenders.append(f"{path.relative_to(APP_ROOT)}: {hit}")
    assert offenders == [], offenders
