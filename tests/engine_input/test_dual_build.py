"""EI-9c 이중 실행 — GoldenStop 뒤 같은 연결의 새 세션에서 오프라인 합본을 짓고, 프로덕션이 첫
시도에 쓴 '옛 조립'과 대조한다(설계서 EI9_SPEC.md §2.5).

구성
  하네스 `after_stop` 갈고리(harness.py, EI-9c)를 골든 12종(`scenario_defs.SCENARIOS`) + 사다리
  4종(`scenario_ladder.LADDER_SCENARIOS`) + precheck 차단 1종(`test_presolve_block_characterization
  .PRECHECK_BLOCK_SCENARIO`) + S5-SP(`test_fixed_cut_characterization._s5_sp`, 확정 원티드 없이
  제출 원티드 경로로만 들어오는 특수요청 사례) + S5-MAX·S5-FW(같은 모듈의 9-0 특성화 변형 —
  일자별 max 되올림 · 확정 원티드 덮기 경로, §0 지시 D) = 20개 시나리오 전부에 단다. S9·S9-F 의
  night_block 경로는 이 12종 실행 안에서 이미 나온다 — §2.0 완료기준의 '12+4+3'에 D 가 더한
  S5-MAX·S5-FW 2종을 보탠 모집합이다(presolve 3 중 S9·S9-F 는 재시드가 아니라 재사용).

프렐류드(`PreludeState`)는 **기존 L0/L0'/L1 캡처를 그대로 쓴다** — capture.py 가 이미
`run_runtime_precheck`·`generate_roster_cp_sat` 의 bound argument 를 canonical 형태(원문 dict,
`_sa_*` 제거)로 기록해 둔다. 새 스파이는 **딱 한 자리**뿐이다 — `read_fixed_cut_inputs`/
`compute_fixed_cut_demand`(9-0, rcs 모듈 최상위 이름)의 `fixed_nurses`·`fixed_special_requests`
는 엔진 입력(L1)에 실리지 않아(고정근무자는 엔진 밖으로 분리된다) 캡처할 자리가 거기뿐이다.
`generate_roster_cp_sat` 자체를 또 감싸지 않는다 — 감싸면 ladder/post 모드(`EngineRecorder`)가
쓰는 `inspect.signature(original)` 이 깨져(바깥에서 보면 `*args, **kwargs` 꼴이 되어 bound
argument 이름이 사라진다) 그 4개 사다리 시나리오의 L1 캡처 자체가 망가진다.

대조가 **독립적인(비동어반복) 재검증**인 곳 — 진짜 값어치가 있는 자리
  - `grade`: 빌더는 `_fetch_grade_config_dict` 를 **새 세션에서 다시** 부른다(L0 은 프로덕션
    자신의 호출 결과). 같은 DB 상태를 두 세션이 각자 읽어 같은 값을 내는지 확인한다.
  - `team.team_min_by_team`/`team_handoff_policy_by_team`: `first_attempt_config=None` 으로
    불러 빌더가 `build_team_min_by_team` 을 **새로 재계산**하게 한다(L1 의 값은 `_run_cp_sat_basic`
    자신이 주입한 것 — 독립 재계산과 대조한다. `first_attempt_config` 를 L1 값 그대로 넘기면
    '이미 있으면 안 덮는다' 분기가 걸려 비교가 동어반복이 된다 — 일부러 `None` 을 쓴다).
  - `demand.post`: 빌더가 `read_fixed_cut_inputs`→`compute_fixed_cut_demand` 를 **새 세션에서
    다시** 돌린다. S5·S1-LT·S9-F·S5-SP(고정근무자 경로) 가 실제로 차감을 겪는 시나리오다 —
    `test_dual_build_demand_cut_scenarios_are_not_vacuous` 가 이 넷에서 실제로 차감 스파이가
    불렸는지(비교가 헛돌지 않는지) 따로 확인한다.
  - 단계 A getter 순서(등급→주말휴무→팀→근무코드): `build_precheck_stage` 를 §1.2 프로덕션
    순서로 불러 새 세션에 붙인 `QueryLog` 문장열이 그 네 표만, 그 순서로만 나오는지 본다
    (test_builder.py 의 shim 기반 검증을 **골든 DB** 위에서 재확인하는 것 — 자리가 다르다).
  - `codebook.code2main`/`id_to_main`: 빌더는 **캐시된** `stage_a.shift_lookup` 위에서
    `_build_code_to_main_map`/`_build_shift_normalizer` 를 다시 돌린다(새 조회는 0 이지만
    계산은 독립). 나이트 블록(L0', `detect_night_forced_oversupply` 호출 인자)의 값과 대조한다.
  - `roster.nurses`: 빌더가 `stage_a.weekend_ids`(캐시) 로 `is_weekend_off` 를 다시 주입하고
    `staffing_first` 규칙을 다시 적용한 명단이 L1 `nurses_data`(프로덕션 자신이 지은 값)와 같은가.
  - `settings_eff`: 빌더가 지은 `config_data`(차감 뒤 demand·team·shift_definitions 병합)로
    `CPSATBasicEngine.create_config_from_db` 를 **다시** 불러 L2(`settings_eff`, 프로덕션이
    첫 시도 안에서 같은 함수를 부른 결과)와 같은가. S3(모든 nullable NULL)은 프로덕션도
    TypeError 라 L2 가 비어 있다 — 빌더도 같은 이유로 비어야 '같다'(`_EXPECTED_LOAD_REPORT_
    ERROR_KEYS["S3"]`).

대조가 **패스스루(동어반복) 확인**인 곳 — 값어치는 '빌더가 베끼다가 잃어버리지 않았다'
  - `settings_raw`·`cells.combined_fixed_cells`·`preferences` — 전부 `PreludeState` 가 그대로
    들고 온 값이라 L0/L1 과 같은 건 당연하다. 그래도 라운드트립 버그(빠뜨림·형 변환 오류)는 잡는다.

조건 ② 음성 대조(S6)
  `flush_pending_transfers`(assignment_service.py — 읽기만 한다, 고치지 않는다)가 발효시키는
  병동이동(X02, 과거 start_date)의 `NurseAssignment.status` 는 생성 **앞**(시드 직후)엔 'active',
  생성 **뒤**(함수 자신의 docstring — "status를 completed로 변경")엔 'completed' 다. 생성 앞에서
  뜬 스냅샷과 `after_stop`(조건 ②, 생성 뒤)에서 뜬 스냅샷을 대조해 둘이 다르다는 것을 고정한다 —
  '프로덕션 앞'에서 오프라인 입력을 지었다면 이 변화를 놓친다는 것의 직접 증거다.

운영 소비 0 — 이 파일은 `app/` 을 전혀 고치지 않는다(읽기만). `harness.py` 에 `after_stop` 갈고리
하나만 새로 썼고 기본값은 `None` 이라, 이 파일이 없어도 다른 골든 테스트는 바이트 그대로다
(재확인: `test_scenarios.py`·`test_ladder.py`·`test_presolve_block_characterization.py`·
`test_capture.py`·`test_fixed_cut*.py`·`test_expected_diff.py` 전부 재실행해 동일 통과 확인 —
STATUS_9abcd.md 참고). 빌더(`build_engine_input`)는 읽기 전용·commit 0 이고, 이 파일이 여는
세션도 전부 하네스의 바깥 트랜잭션 안에 있어 끝나면 롤백된다(흔적 0).
"""
from __future__ import annotations

import copy
import dataclasses
import functools
from collections.abc import Callable
from typing import Any

import pytest
from sqlalchemy.orm import Session

from .backend_check import assert_sqlite_backends
from .capture import Capture, QueryLog, _bound_arguments
from .golden import build_record, mismatch_report, read_golden
from .harness import CapturedRun, Scenario, run_with_output
from .scenario_defs import SCENARIOS
from .scenario_ladder import LADDER_SCENARIOS
from .test_fixed_cut_characterization import _s5_fw, _s5_max, _s5_sp
from .test_presolve_block_characterization import PRECHECK_BLOCK_SCENARIO
from .ward import GROUP_ID

#: 빌더가 재조회(§2.5 '독립 재검증')하는 수요 리스트 2키 → `DemandSnapshot` 필드 이름. 플랫
#: `daily_shift_requirements` 는 9-0 차감이 건드리지 않아(by_day·max_by_day 만 바꾼다, §3.1)
#: 비교에서 뺐다.
_DEMAND_LIST_KEYS = {
    "daily_shift_requirements_by_day": "by_day",
    "daily_shift_requirements_max_by_day": "max_by_day",
}
#: 단계 A getter 가 쓰는 표 4종(순서 그대로) — `build_precheck_stage` 를 이 순서로 부른다(§1.2).
_PRECHECK_TABLES = ("roster_grade_config", "nurse_weekendoff_period", "teams", "shifts")
#: 고정근무자 차감 경로를 실제로 겪는 시나리오(§2.5 지시 — '비교가 헛돌지 않는지'의 대상).
#: S5-MAX(일자별 max 되올림)·S5-FW(확정 원티드 덮기) 는 둘 다 `s5_ward()`(F01 고정근무 D) 위의
#: 변형이라 같은 차감 경로를 겪는다(§0 지시 D).
_FIXED_CUT_SCENARIOS = ("S5", "S1-LT", "S9-F", "S5-SP", "S5-MAX", "S5-FW")
#: 시나리오별로 `load_report.errors` 에 남아도 되는 키(리뷰 지적 수정분 — 기본은 전부 빈 집합,
#: 즉 `errors == {}` 를 기대한다). 구조적으로 피할 수 없는 경우만 여기 명시한다 — 조용히
#: 늘어나지 않도록 `test_dual_build_load_report_has_no_unexpected_errors` 가 '명시 안 된 키가
#: 남으면' 실패로, '명시했는데 안 남으면'(고쳐져서 더 이상 안 나는 경우)도 실패로 잡는다.
#: S3(O-15 추가분) — `scenario_defs.py` 가 "모든 nullable 컬럼 NULL(BR-1) — 현행 TypeError 경로"
#: 라고 적은 바로 그 시나리오다. `shift_priority=None` 이면 `create_config_from_db` 의
#: `max(0.05, config_data.get('shift_priority', 0.7))` 가 `None > float` 비교에서 TypeError —
#: 프로덕션 L2 에도 `settings_eff` 가 없다(§2.5 대조표 '같음(S3 은 TypeError …)'). 빌더도 같은
#: 함수를 불러 같은 예외를 내고 `_safe_call` 이 `errors["settings_eff"]` 에 담는다 — 둘 다
#: 비어 있다는 뜻이라 `_compare_settings_eff` 는 이 경우를 불일치로 보지 않는다.
_EXPECTED_LOAD_REPORT_ERROR_KEYS: dict[str, frozenset[str]] = {"S3": frozenset({"settings_eff"})}


# ── 프렐류드 캡처 — 기존 L0/L1 재사용 + 고정근무 차감 전용 스파이 1곳 ──────────────


@dataclasses.dataclass
class _FixedCutCapture:
    """`read_fixed_cut_inputs`/`compute_fixed_cut_demand` 호출 인자 원문(9-0, rcs 최상위 이름).

    엔진 입력(L1)에는 고정근무자가 실리지 않으므로(엔진 밖으로 분리) 이 둘을 캡처하는 것이
    `fixed_nurses`·`fixed_special_requests` 를 얻는 유일한 자리다.
    """

    fixed_nurses: list[dict[str, Any]] = dataclasses.field(default_factory=list)
    fixed_special_requests: list[Any] = dataclasses.field(default_factory=list)
    called: bool = False


def _strip_sa(obj: Any) -> dict[str, Any]:
    """ORM 인스턴스 → `vars()` 사본에서 `_sa_*` 를 뺀 dict(PreludeState 의 '__dict__ 사본' 규칙)."""
    return {k: v for k, v in vars(obj).items() if not str(k).startswith("_sa_")}


def _fixed_cut_capture_patch(capture: _FixedCutCapture):
    """`rcs.read_fixed_cut_inputs`/`compute_fixed_cut_demand` 를 감싸 인자를 복사한다(원본은 그대로 호출).

    두 이름 다 rcs 모듈 최상위(9-0 이 둔 이름)일 뿐 `generate_roster_cp_sat`(ladder/post 모드의
    `EngineRecorder` 가 `inspect.signature` 로 들여다보는 바로 그 이름)은 건드리지 않는다 —
    그래서 사다리 시나리오(S1-L·S1-LT·S1-LB)에서도 안전하다.
    """

    def patch(mp: pytest.MonkeyPatch) -> None:
        import services.roster_create_service as rcs

        original_inputs = rcs.read_fixed_cut_inputs
        original_demand = rcs.compute_fixed_cut_demand

        def spy_inputs(db, fixed_nurses, **kwargs):
            if not capture.called:
                capture.fixed_nurses = [_strip_sa(n) for n in fixed_nurses]
            return original_inputs(db, fixed_nurses, **kwargs)

        def spy_demand(inputs, special_requests, demand_config, **kwargs):
            if not capture.called:
                capture.fixed_special_requests = list(special_requests)
                capture.called = True
            return original_demand(inputs, special_requests, demand_config, **kwargs)

        mp.setattr(rcs, "read_fixed_cut_inputs", spy_inputs)
        mp.setattr(rcs, "compute_fixed_cut_demand", spy_demand)

    return patch


@dataclasses.dataclass
class _FirstCallCapture:
    """`_run_cp_sat_basic` **첫** 호출의 `nurses_in_group`·`config_override` 사본(ORM `__dict__`,
    날짜는 진짜 `date`/`datetime`) — 첫 시도에 닿는 경로(L1 있는 15종) 전용(리뷰 지적 수정분,
    §2.5). `config_override`(M-H, 리뷰 지적 — rcs :6794 `config_override=config_dict`)는
    production 모드 이중 실행(`_compare_production_mode_matches_offline`)의 `first_attempt_
    config` 로 쓴다 — 운영이 **실제로** 그 자리에서 넘기는 바로 그 config 사본이다.
    """

    nurses_in_group: list[dict[str, Any]] | None = None
    config_override: dict[str, Any] | None = None
    called: bool = False


def _first_call_capture_patch(capture: _FirstCallCapture):
    """`rcs._run_cp_sat_basic` 첫 호출에서만 `nurses_in_group`·`config_override` 를 사본으로 뜬다.

    §2.5 '캡처 순간에 사본을 뜬다'가 가리키는 바로 그 지점이다 — 이전엔 L0/L1 의 canonical
    사본(날짜가 ISO 문자열)을 재사용해 `_active_range_in_month` 의 `join.date()` 가
    `AttributeError` 로 깨졌고(18/18 재현, 리뷰 지적), `_safe_call` 이 이를 삼켜 `stage_b.
    allowed`·`post.health_leave` 가 조용히 비었다. `_run_cp_sat_basic` 은 rcs 모듈 전역
    이름(bare call)이라 바로 스파이할 수 있고, `generate_roster_cp_sat` 과 달리 `EngineRecorder`
    가 이 이름의 시그니처를 들여다보지 않으므로 사다리/post 모드에서도 안전하다. 재시도(두 번째
    호출부터)는 뜨지 않는다 — '첫 시도' 값만 쓴다(§2.4-2 `first_attempt_config` 와 같은 뜻).
    """

    def patch(mp: pytest.MonkeyPatch) -> None:
        import services.roster_create_service as rcs

        original = rcs._run_cp_sat_basic

        @functools.wraps(original)
        def spy(db, current_user, nurses_in_group, *args, **kwargs):
            if not capture.called:
                capture.nurses_in_group = [_strip_sa(n) for n in nurses_in_group]
                override = kwargs.get("config_override")
                capture.config_override = dict(override) if isinstance(override, dict) else None
                capture.called = True
            return original(db, current_user, nurses_in_group, *args, **kwargs)

        mp.setattr(rcs, "_run_cp_sat_basic", spy)

    return patch


@dataclasses.dataclass
class _PrecheckNursesCapture:
    """`run_runtime_precheck` 의 `nurses_dict`(canonical **이전**, 좁은 ontology 프로필 —
    `_ONTOLOGY_NURSE_FIELDS`, `joining_date`·`resignation_date` 포함) 사본 — 첫 시도에 닿지
    못하는 경로(night_block·precheck_block, `_run_cp_sat_basic` 이 아예 안 불린다) 전용
    폴백이다(리뷰 지적 수정분).
    """

    nurses_dict: list[dict[str, Any]] | None = None


def _precheck_nurses_capture_patch(capture: _PrecheckNursesCapture):
    """`run_runtime_precheck` 를 감싸 호출자가 넘긴 **원본** `nurses_dict` 를 복사한다.

    `scenario.patches` 는 `Capture.install` **보다 먼저** 걸린다(harness.py `run_production`
    의 적용 순서 — `install_fixed_clock` 뒤 `for patch in scenario.patches` 가 먼저, `capture.
    install` 은 `_run_in_transaction` 안에서 나중에 돈다). 그래서 이 패치가 캡처하는 `original`
    은 아직 `Capture._spy_precheck` 가 감싸기 전의 진짜 함수이고, 이 스파이가 받는 `nurses_dict`
    는 canonical 을 거치지 않은 원본 값이다. `functools.wraps(original)` 로 감싸 `inspect.
    signature` 가 `__wrapped__` 를 따라가게 한다 — 안 하면 `Capture._bound_arguments` 가 이
    스파이의 `(*, nurses_dict, **kwargs)` 모양을 실제 시그니처로 오인해 L0 캡처가 깨진다.
    """

    def patch(mp: pytest.MonkeyPatch) -> None:
        import services.precheck as precheck

        original = precheck.run_runtime_precheck

        @functools.wraps(original)
        def spy(*, nurses_dict, **kwargs):
            if capture.nurses_dict is None:
                capture.nurses_dict = [dict(n) for n in nurses_dict]
            return original(nurses_dict=nurses_dict, **kwargs)

        mp.setattr(precheck, "run_runtime_precheck", spy)

    return patch


#: 고정근무 차감(`apply_fixed_cut_demand`)이 `config_dict` 에 대입하는 키 둘.
_CUT_DEMAND_KEYS = ("daily_shift_requirements_by_day", "daily_shift_requirements_max_by_day")
#: 단계 A 입구 뒤 precheck 앞(EI-10g2·g4)에서 운영이 바꾸는 `config_dict` 키 — L0 는 바뀐 뒤 값이라 프렐류드는 입구 값으로 되돌린다.
#:   수요 두 키(고정근무 차감 — 10g2) · `initial_constraints`(precheck 사본에 단계 B 병합본을 싣는다 — 10g4).
_PRELUDE_RESTORE_KEYS = _CUT_DEMAND_KEYS + ("initial_constraints",)


@dataclasses.dataclass
class _CombinedCellsCapture:
    """`stage_a_at_precheck` **첫** 호출의 `combined_fixed_cells` 사본(엔진 형식 원형 칸).

    EI-10d 전에는 L0(`run_runtime_precheck` 의 `fixed_cells`)가 원형 칸 그대로여서 `_build_prelude` 가 L0 에서
    칸을 꺼내 썼다. 10d 부터 L0 는 precheck 가 읽는 번역본(`{nurse_id, day_index, shift}`)이라 원형 칸을 단계 A
    입구에서 따로 뜬다 — 운영이 단계 A 에 넘기는 바로 그 칸이다(precheck 번역·엔진 호출보다 앞이라 아직 안 바뀌었다).
    """

    cells: list[dict[str, Any]] | None = None
    #: 같은 호출의 `config_dict` 중 `_PRELUDE_RESTORE_KEYS` 의 깊은 사본 — **차감 전·단계 B 병합 전** 값이다(EI-10g2: 차감이 precheck 앞으로
    #: 옮겨져 L0 는 차감 뒤가 됐다 — 오프라인 합본이 차감을 한 번 더 하지 않게 프렐류드는 이 키들을 이 값으로 되돌린다). 키가 없었으면 없다.
    pre_cut_demand: dict[str, Any] | None = None


def _combined_cells_capture_patch(capture: _CombinedCellsCapture):
    """`rcs.stage_a_at_precheck` 를 감싸 첫 호출의 `combined_fixed_cells` 를 복사한다(원본은 그대로 호출).

    rcs 가 `from services.engine_input.views import stage_a_at_precheck` 로 모듈 전역에 묶어 둔 이름이라
    `rcs` 속성을 바꾸면 운영 호출부(rcs:6496)가 이 스파이를 지난다. 재시도(둘째 호출부터)는 뜨지 않는다.
    """

    def patch(mp: pytest.MonkeyPatch) -> None:
        import services.roster_create_service as rcs

        original = rcs.stage_a_at_precheck

        @functools.wraps(original)
        def spy(db, **kwargs):
            if capture.cells is None:
                capture.cells = [dict(cell) for cell in kwargs.get("combined_fixed_cells") or []]
                cfg = kwargs.get("config_dict") or {}
                capture.pre_cut_demand = {k: copy.deepcopy(cfg[k]) for k in _PRELUDE_RESTORE_KEYS if k in cfg}
            return original(db, **kwargs)

        mp.setattr(rcs, "stage_a_at_precheck", spy)

    return patch


@dataclasses.dataclass
class _SpecialRequestsCapture:
    """`_collect_nurses_and_preferences`(rcs)가 돌려주는 **전체** `special_shift_requests`
    사본(O-11) — 소속+인바운드 전원 몫이다. `fixed_special_requests`(고정근무자만 거른
    부분집합, `_FixedCutCapture`)로는 얻을 수 없다 — `_post_health_leave` 가 `existing_
    requests` 로 받는 근사 오차의 원천이었다(§0 지시 E).
    """

    special_shift_requests: list[dict[str, Any]] = dataclasses.field(default_factory=list)
    called: bool = False


def _special_requests_capture_patch(capture: _SpecialRequestsCapture):
    """`rcs._collect_nurses_and_preferences` 를 감싸 반환값 3번째 원소(특수요청 전체)를 복사한다.

    이 이름은 `generate_roster_cp_sat`/`_run_cp_sat_basic` 과 무관해 `inspect.signature` 영향이
    없다(`_FixedCutCapture`·`_FirstCallCapture`와 같은 근거 — 사다리/post 모드에서도 안전).
    """

    def patch(mp: pytest.MonkeyPatch) -> None:
        import services.roster_create_service as rcs

        original = rcs._collect_nurses_and_preferences

        @functools.wraps(original)
        def spy(db, req, current_user):
            result = original(db, req, current_user)
            if not capture.called:
                _nurses, _prefs, special_shift_requests, _map = result
                capture.special_shift_requests = [dict(r) for r in special_shift_requests]
                capture.called = True
            return result

        mp.setattr(rcs, "_collect_nurses_and_preferences", spy)

    return patch


@dataclasses.dataclass
class _ShiftManageCapture:
    """`rcs._build_code_to_main_map` **첫** 호출의 `shift_manage_data` 사본(M-D, 리뷰 지적).

    이 함수는 L1 경로(`_run_cp_sat_basic` :2995)뿐 아니라 night_block 경로(`build_presolve_
    view` → 이 함수, presolve_block.py:457-459, 지연 import 라 패치가 그대로 먹힌다)에서도
    불린다 — `_build_prelude` 가 `shift_manage_data` 를 L1 에서만 가져오던 한계(S9·S9-F 에서
    codebook 대조가 늘 비는 원인)를 고친다. L1 이 있는 15종은 기존처럼 L1 값을 그대로 쓴다
    (이 캡처는 L1 없는 경로의 폴백일 뿐이다 — 기존 통과분의 비교 대상을 바꾸지 않는다).
    """

    shift_manage_data: list[dict] | None = None
    called: bool = False


def _shift_manage_capture_patch(capture: _ShiftManageCapture):
    """`rcs._build_code_to_main_map` 첫 호출의 `shift_manage_data` 를 복사한다(원본은 그대로 호출)."""

    def patch(mp: pytest.MonkeyPatch) -> None:
        import services.roster_create_service as rcs

        original = rcs._build_code_to_main_map

        def spy(shift_manage_data):
            if not capture.called:
                capture.shift_manage_data = [dict(r) for r in (shift_manage_data or [])]
                capture.called = True
            return original(shift_manage_data)

        mp.setattr(rcs, "_build_code_to_main_map", spy)

    return patch


def _with_patches(scenario: Scenario, *extra) -> Scenario:
    """기존 시나리오를 바꾸지 않고 패치만 덧붙인 사본(`_with_patches` 선례, test_presolve_block_characterization.py)."""
    return dataclasses.replace(scenario, patches=scenario.patches + tuple(extra))


def _build_prelude(
    layers: dict, fixed: _FixedCutCapture, first_call: _FirstCallCapture,
    precheck_nurses: _PrecheckNursesCapture, special_requests: _SpecialRequestsCapture,
    shift_manage: _ShiftManageCapture, combined_cells: _CombinedCellsCapture, req: Any, user: Any,
):
    """L0(config_dict)·첫 호출 캡처(간호사, 실제 타입) + 고정근무 차감 스파이 → `PreludeState`.

    `nurses_for_engine` 은 **반드시 실제 타입 사본**에서 가져온다(§2.5 '캡처 순간에 사본을
    뜬다') — L0/L1 의 canonical 사본(날짜가 ISO 문자열)을 재사용하면 `_active_range_in_month`
    의 `join.date()` 가 깨진다(리뷰 지적, 18/18 재현 — `stage_b.allowed`·`post.health_leave`
    가 `_safe_call` 에 조용히 삼켜졌었다). 우선순위:
      1. `_run_cp_sat_basic` 첫 호출(`first_call.nurses_in_group`) — 첫 시도에 닿는 15종.
      2. `run_runtime_precheck` 의 `nurses_dict`(좁은 프로필이지만 실제 타입) — 첫 시도에
         닿지 못하는 경로(night_block·precheck_block, 3종) 전용 폴백.
    둘 다 없으면(엔진 대상 0명) 빈 리스트 — 이 파일의 18종엔 해당 경로가 없다.
    """
    from services.engine_input.model import Ctx, PreludeState

    ctx = Ctx(office_id=user.office_id, group_id=user.group_id, year=req.year, month=req.month,
              config_id=getattr(req, "config_id", None))
    l0 = layers["L0"][0] if layers["L0"] else None
    l1 = layers["L1"][0] if layers["L1"] else None
    # `team_min_by_team` 은 precheck 블록이 **사본**(`precheck_config = dict(config_dict)`)
    # 에만 조건부로 주입한다(rcs :6527-:6559) — '정찰 직전' 원본 config 엔 없었다는 전제로
    # 늘 뺀다. 그래야 `_compare_team` 이 `first_attempt_config=None` 으로 불러 `_build_team_dict`
    # 의 재계산 분기(`"team_min_by_team" not in config`)를 **항상** 타서 독립 재검증이 된다
    # (끼워 넣지 않으면 늘 '이미 있다' 분기로 빠져 동어반복이 된다). 원본 config 자체가
    # override 로 이미 `team_min_by_team` 을 갖고 있던 경우는 이 가정이 깨지지만, 그 경우를
    # 겪는 골든 시나리오가 없다(리뷰 지적 — §6 으로 남김, 구분하려면 precheck **이전** config
    # 스냅샷이 따로 필요하다).
    config_dict = ({k: v for k, v in l0["config_dict"].items() if k != "team_min_by_team"}
                   if l0 else {})
    if combined_cells.pre_cut_demand is not None:
        # (EI-10g2·g4) L0 는 차감 뒤·단계 B 병합 뒤다 — 그 키들만 단계 A 입구 값으로 되돌린다. 키가 없었으면 지운다.
        from services.engine_input.snapshot import canonical

        for key in _PRELUDE_RESTORE_KEYS:
            if key in combined_cells.pre_cut_demand:
                config_dict[key] = canonical(combined_cells.pre_cut_demand[key])
            else:
                config_dict.pop(key, None)
    if first_call.nurses_in_group is not None:
        nurses_for_engine = first_call.nurses_in_group
    elif precheck_nurses.nurses_dict is not None:
        nurses_for_engine = precheck_nurses.nurses_dict
    else:
        nurses_for_engine = []
    return PreludeState(
        ctx=ctx, config_dict=config_dict,
        nurses_for_engine=nurses_for_engine,
        fixed_nurses=fixed.fixed_nurses, fixed_special_requests=fixed.fixed_special_requests,
        combined_fixed_cells=list(combined_cells.cells or []),  # 단계 A 입구 사본 — L0 는 10d 부터 번역본
        # L1 이 있으면 그 값을 그대로 쓴다(기존 15종의 비교 대상 불변). L1 없는 경로(night_block·
        # precheck_block)는 `_ShiftManageCapture`(M-D, 리뷰 지적)로 폴백한다 — 이전엔 `{}` 로
        # 떨어져 S9·S9-F 에서 `codebook`(code2main·id_to_main) 대조가 통째로 빠졌다.
        shift_manage_data=(list(l1["shift_manage_data"]) if l1
                           else list(shift_manage.shift_manage_data or [])),
        preferences=list(l1["prefs_data"]) if l1 else [],
        special_shift_requests=list(special_requests.special_shift_requests))


# ── 대조 ────────────────────────────────────────────────────────────────


def _fmt(value: Any) -> str:
    """보고용 짧은 표기."""
    text = repr(value)
    return text if len(text) <= 200 else text[:197] + "..."


def _compare_grade(ei, layers: dict) -> str | None:
    """독립 재검증 — 빌더가 새 세션에서 다시 부른 등급과 L0(프로덕션 호출)이 같은가."""
    if not layers["L0"]:
        return None
    from services.engine_input.snapshot import canonical

    old = layers["L0"][0].get("grade_config")
    new = canonical(ei.grade) if ei.grade else None
    return None if old == new else f"grade 불일치: {_fmt(old)} != {_fmt(new)}"


def _compare_team(ei, layers: dict) -> str | None:
    """독립 재검증 — `first_attempt_config=None` 으로 새로 재계산한 team 과 L1(프로덕션 주입)이 같은가."""
    if not layers["L1"]:
        return None
    from services.engine_input.snapshot import canonical

    old_cfg = layers["L1"][0]["config_data"]
    mismatches: list[str] = []
    for key in ("team_min_by_team", "team_handoff_policy_by_team"):
        old_value = old_cfg.get(key) or None
        new_raw = ei.team.get(key)
        new_value = (canonical(new_raw) or None) if new_raw else None
        if old_value != new_value:
            mismatches.append(f"team.{key} 불일치: {_fmt(old_value)} != {_fmt(new_value)}")
    return "; ".join(mismatches) or None


def _effective(post, pre, field_name: str):
    """차감 안 됐으면(None) pre 값으로 되돌린다(builder 의 §3.8 규칙과 같다)."""
    value = getattr(post, field_name) if post is not None else None
    return value if value is not None else getattr(pre, field_name)


def _compare_demand_post(ei, layers: dict) -> str | None:
    """독립 재검증 — 빌더가 새 세션에서 다시 차감한 수요와 옛 조립(L1, 없으면 L0')이 같은가."""
    if layers["L1"]:
        old = layers["L1"][0]["config_data"]
    elif layers["L0p"]:
        old = layers["L0p"][0]["config"]
    else:
        return None
    mismatches: list[str] = []
    for key, field_name in _DEMAND_LIST_KEYS.items():
        new_value = _effective(ei.demand.post, ei.demand.pre, field_name)
        old_value = old.get(key)
        if new_value != old_value:
            mismatches.append(f"demand.post.{key} 불일치: {_fmt(old_value)} != {_fmt(new_value)}")
    return "; ".join(mismatches) or None


def _compare_passthrough(ei, prelude) -> str | None:
    """패스스루 라운드트립 — `PreludeState` 가 들고 온 값을 빌더가 그대로 실었는가."""
    mismatches: list[str] = []
    if ei.settings_raw != prelude.config_dict:
        mismatches.append("settings_raw 가 prelude.config_dict 와 다르다(round-trip)")
    if ei.cells.get("combined_fixed_cells") != prelude.combined_fixed_cells:
        mismatches.append("cells.combined_fixed_cells 가 prelude 와 다르다(round-trip)")
    if ei.preferences != prelude.preferences:
        mismatches.append("preferences 가 prelude 와 다르다(round-trip)")
    return "; ".join(mismatches) or None


def _compare_cells_and_carryover(ei, layers: dict) -> str | None:
    """독립 재검증 — 단계 B 가 다시 합친 `cells.initial_constraints`·`carryover`(⑤ 전월 꼬리
    6종 + `off_window_constraints`, O-18)가 L1(프로덕션 자신이 짓고 첫 시도에 주입한
    `config_data`)과 같은가.

    리뷰 지적 — 이전엔 단계 B 가 ④⑤⑥ 을 조회만 하고 버려(`EngineInput.cells`·`carryover` 가
    늘 비어 있었다) 이 부품의 회귀를 9c 가 전혀 못 잡았다. **키 집합 자체도 L1 에서 독립적으로
    뽑는다**(리뷰 지적 HIGH — 이전엔 `builder._CARRYOVER_KEYS` 를 그대로 가져와 썼다. 피검
    코드가 키 하나를 빠뜨리면(`_CARRYOVER_KEYS` 에서 빼면) 비교 대상 키 집합도 같이 빠져
    자기 참조가 된다. N2d·N2e 변이(`prev_month_last_n_k`·`prev_month_last_main` 제거)가 이
    공백을 실측했다 — 둘 다 149 passed 로 살아남았다). L1 의 `config_data` 가 실제로 담은
    `prev_month_*`·`off_window_constraints` 키 **전부**(값이 있든 없든, rcs 가 조건부로만
    대입하므로 '키가 있다'=값이 참이다)를 먼저 집합으로 비교하고, 그다음 값을 비교한다.
    S6(병동이동, X01·N07)이 `off_window_constraints` 가 실제로 채워지는 유일한 골든이다.
    """
    if not layers["L1"]:
        return None
    from services.engine_input.snapshot import canonical

    old_cfg = layers["L1"][0]["config_data"]
    mismatches: list[str] = []
    old_ic = canonical(old_cfg.get("initial_constraints") or {"forced_off": {}, "forbidden": {}})
    new_ic = canonical(ei.cells.get("initial_constraints") or {})
    if old_ic != new_ic:
        mismatches.append(f"cells.initial_constraints 불일치: {_fmt(old_ic)} != {_fmt(new_ic)}")
    expected_keys = {k for k in old_cfg if k.startswith("prev_month_") or k == "off_window_constraints"}
    got_keys = set(ei.carryover)
    if expected_keys != got_keys:
        mismatches.append(f"carryover 키 집합 불일치: {sorted(expected_keys)} != {sorted(got_keys)}")
    for key in expected_keys & got_keys:
        old_value = canonical(old_cfg[key]) if old_cfg.get(key) else None
        new_value = canonical(ei.carryover[key]) if ei.carryover.get(key) else None
        if old_value != new_value:
            mismatches.append(f"carryover.{key} 불일치: {_fmt(old_value)} != {_fmt(new_value)}")
    return "; ".join(mismatches) or None


def _compare_codebook(ei, layers: dict) -> str | None:
    """`code2main`·`id_to_main`·`shift_definitions` 가 나이트 블록(L0')·첫 시도(L1)가 쓴
    값과 같은가(§2.5 대조표).

    `code2main`·`id_to_main` 은 **배선 확인**이다(F — 동어반복 아님 주의로 표현을 고쳤다,
    리뷰 지적 — 운영 함수 `_build_code_to_main_map`/`build_shift_normalizer` 를 그대로 불러
    같은 `shift_manage_data` 를 넣었을 때 같은 결과가 나오는지 볼 뿐, 독립 재계산은 아니다.
    L0'(`detect_night_forced_oversupply` 호출 인자)에 직접 실린 값과 대조한다 — `layers["L0p"]`
    가 있으면 **L1 유무와 무관하게** 비교한다(리뷰 지적 M-D — 이전엔 L1 이 있을 때만 전체를
    비교해, `code2main` 의 입력(`prelude.shift_manage_data`)을 L1 에서만 가져오던 하네스
    한계 때문에 S9·S9-F(L1 없음)에서 이 비교가 통째로 빠져 있었다. `_ShiftManageCapture`
    가 L1 없는 경로도 캡처해 메워, 이제 S9·S9-F 에서도 비교가 돈다).
    `shift_definitions` 는 **독립 재검증**이다 — L1 `config_data`(비어 있으면 생산도 키 자체를
    안 심는다 — `or None` 으로 같이 다룬다) 와 대조하므로 L1 이 있을 때만 비교한다.
    """
    from services.engine_input.snapshot import canonical

    mismatches: list[str] = []
    if layers["L0p"]:
        old_l0p = layers["L0p"][0]
        for key in ("code2main", "id_to_main"):
            old_value = old_l0p.get(key) or None
            new_value = canonical(ei.codebook.get(key)) if ei.codebook.get(key) else None
            if old_value != new_value:
                mismatches.append(f"codebook.{key} 불일치: {_fmt(old_value)} != {_fmt(new_value)}")
    if layers["L1"]:
        old_defs = layers["L1"][0]["config_data"].get("shift_definitions") or None
        new_defs = (canonical(ei.codebook.get("shift_definitions"))
                   if ei.codebook.get("shift_definitions") else None)
        if old_defs != new_defs:
            mismatches.append(f"codebook.shift_definitions 불일치: {_fmt(old_defs)} != {_fmt(new_defs)}")
    return "; ".join(mismatches) or None


def _production_engine_input(session: Session, prelude, first_call: _FirstCallCapture):
    """`config_override` 가 캡처됐을 때(첫 시도 경로, 15종) production 모드 `EngineInput` 을
    짓는다(M-H, 리뷰 지적 — 9c 가 production 모드를 한 번도 돌리지 않던 공백을 메운다).
    """
    if first_call.config_override is None:
        return None
    from services.engine_input.builder import build_engine_input

    return build_engine_input(session, prelude, first_attempt_config=first_call.config_override,
                              mode="production")


def _compare_production_mode_matches_offline(ei, ei_production) -> str | None:
    """독립 재검증 — production 모드(운영이 실제로 넘기는 `config_override` 로)가 오프라인
    합본과 digest 대상 부품에서 같은가(M-H, 리뷰 지적). `post`·`raw_column`·`load_report` 는
    D20 이 '모드마다 다르다'고 정한 자리라 뺀다 — 나머지(demand.post·settings_raw·grade·team·
    codebook·roster·cells·carryover·settings_eff)는 '같은 요청·같은 DB 면 운영=오프라인'
    전제(EI-13a digest 대조의 바탕)가 지금도 성립하는지 본다.
    """
    if ei_production is None:
        return None
    from services.engine_input.snapshot import canonical

    mismatches: list[str] = []
    for field_name in ("settings_raw", "grade", "team", "codebook", "roster", "cells",
                       "carryover", "settings_eff"):
        old = canonical(getattr(ei, field_name))
        new = canonical(getattr(ei_production, field_name))
        if old != new:
            mismatches.append(f"production.{field_name} 불일치: {_fmt(old)} != {_fmt(new)}")
    old_post = canonical(ei.demand.post) if ei.demand.post is not None else None
    new_post = canonical(ei_production.demand.post) if ei_production.demand.post is not None else None
    if old_post != new_post:
        mismatches.append(f"production.demand.post 불일치: {_fmt(old_post)} != {_fmt(new_post)}")
    return "; ".join(mismatches) or None


def _compare_health_leave(ei, layers: dict) -> str | None:
    """독립 재검증 — `post.health_leave.target_ids` 의 엔진 간호사 부분이 L1 `nurses_data`
    의 `health_leave_extra_off` 플래그 집합과 같은가(O-11, 리뷰 지적 MEDIUM).

    L1 이 없는 경로(night_block — S9·S9-F)는 비교하지 않는다(`_compare_roster`·`_compare_
    codebook.shift_definitions` 와 같은 한계 — 이 하네스의 프렐류드가 그 경로에서 좁은
    precheck 대체 프로필을 쓴다, §2.5 '좁힌 것'). S1-L3(ladder, L1 있음)이 일반 간호사
    6명이 실제로 `health_leave_extra_off=True` 인 실사례다.
    """
    if not layers["L1"]:
        return None
    nurses_data = layers["L1"][0]["nurses_data"]
    engine_ids = {str(n.get("nurse_id")) for n in nurses_data}
    old = {str(n.get("nurse_id")) for n in nurses_data if n.get("health_leave_extra_off")}
    target_ids = ei.post.get("health_leave", {}).get("target_ids") or []
    new = {str(t) for t in target_ids} & engine_ids
    if old != new:
        return f"post.health_leave.target_ids 불일치: {_fmt(old)} != {_fmt(new)}"
    return None


def _compare_roster(ei, layers: dict) -> str | None:
    """독립 재검증 — 빌더가 지은 `roster.nurses`(순서·id·`is_weekend_off`)가 L1 `nurses_data`
    와 같은가 + `nurse_id_to_engine_index` 가 L2(`create_nurses_from_db` 가 **실제로 매긴**
    엔진 정렬·인덱스)와 같은가(§2.5 대조표, 리뷰 지적 HIGH — 이전엔 L2 를 전혀 안 봤다).
    L1 이 없는 경로(night_block·precheck_block)는 비교하지 않는다. `nurse_id_to_engine_index`
    는 **L1 이 있어도 L2 가 없는** 경로(ladder/post — `_compare_settings_eff` 와 같은 네
    경로, `create_nurses_from_db` 가 구조적으로 안 불린다)에서는 추가로 건너뛴다.
    """
    if not layers["L1"]:
        return None
    from services.engine_input.snapshot import canonical

    old = canonical(layers["L1"][0]["nurses_data"])
    new = canonical(ei.roster.get("nurses") or [])
    mismatches: list[str] = []
    if old != new:
        mismatches.append(f"roster.nurses 불일치: {_fmt(old)} != {_fmt(new)}")
    l2_nurses = layers["L2"].get("nurses")
    if l2_nurses:
        old_index = {n["db_id"]: n["id"] for n in l2_nurses}
        new_index = {str(k): v for k, v in (ei.roster.get("nurse_id_to_engine_index") or {}).items()}
        if old_index != new_index:
            mismatches.append(
                f"roster.nurse_id_to_engine_index 불일치: {_fmt(old_index)} != {_fmt(new_index)}")
    return "; ".join(mismatches) or None


def _compare_settings_eff(ei, layers: dict, path: str) -> str | None:
    """독립 재검증까지는 아니지만 — 빌더가 지은 `config_data` 로 같은 엔진 함수
    (`CPSATBasicEngine.create_config_from_db`)를 다시 불러 L2(`settings_eff`)와 같은 결과가
    나오는가(§2.5 대조표). **`path == "first_attempt"` 일 때만** 비교한다(실측 — `harness.
    classify_path` 가 L1 이 있어도 `path` 를 그렇게 가르는 경로 4종이 있다: `ladder_
    unrecoverable`(S1-L·S1-LT·S1-LB)·`post_complete`(S1-L3)는 `ladder.py` 가 원본 엔진을
    아예 안 불러 L2 가 구조적으로 늘 비고, `night_block`(S9·S9-F)은 `_run_cp_sat_basic` 전에
    막혀 L1 자체가 없다. S3 은 `create_config_from_db` 의 TypeError 가 생성 래퍼까지 번져
    `path == "error"` 가 된다 — L2 에 `settings_eff` 가 없다(scenario_defs.py 주석 "현행
    TypeError 경로"). 이 네 경로는 비교 기준(L2)이 구조적으로 없거나 분류 자체가 달라
    `first_attempt`(기본 12종 중 S3 를 뺀 나머지)로 좁혀야 비교가 의미 있다. S3 은 빌더도
    같은 이유로 비어야 한다는 것을 `test_dual_build_load_report_has_no_unexpected_errors`
    (`_EXPECTED_LOAD_REPORT_ERROR_KEYS["S3"]`)가 따로 확인한다.
    """
    if path != "first_attempt":
        return None
    from services.engine_input.snapshot import canonical

    old = layers["L2"].get("settings_eff")
    new = canonical(ei.settings_eff) if ei.settings_eff else None
    if old != new:
        return f"settings_eff 불일치: {_fmt(old)} != {_fmt(new)}"
    return None


def _compare_raw(ei, layers: dict) -> str | None:
    """독립 재검증 — 9d `raw_column['weekly_off_settings_activate']`(오프라인 사본)가 L0 의
    `config_dict` 실효값과 같은가(리뷰 지적 — O-7 '교차 테스트로 드리프트를 막는다'가 실제로는
    L0 과 비교하는 테스트가 없어 비어 있었다). `n2n_min_gap`·`fixed_holiday_off_yn` 은 빼는데,
    override 가 있으면 `settings_raw` 실효값과 raw 컬럼이 **의도적으로** 다른 사례(S8 류, §2.3)가
    있어 L0 과의 단순 비교로는 그 차이를 구분할 수 없기 때문이다. `weekly_off_settings_activate`
    는 override 로 덮이지 않아(rcs 에 그런 경로가 없다) 늘 같아야 한다(실측 18종 전부 일치).
    """
    if not layers["L0"]:
        return None
    old = bool(layers["L0"][0]["config_dict"].get("weekly_off_settings_activate"))
    new = bool(ei.load_report.raw_column.get("weekly_off_settings_activate"))
    if old != new:
        return f"raw_column.weekly_off_settings_activate 불일치: {old!r} != {new!r}"
    return None


def _nurse_record_id(record: dict) -> Any:
    """캡처된 간호사 기록(ORM 전체 기록 — `__orm__` 표지 + `vars()`)에서 `nurse_id` 를 뽑는다."""
    return record.get("nurse_id") if isinstance(record, dict) else None


def _compare_precheck_call_offline(
    session: Session, prelude, layers: dict, run_runtime_precheck: Callable, ei: Any = None,
) -> str | None:
    """D1(§4.4) — 오프라인 `to_precheck_input(stage_a_off, stage_a_off.config_dict)` 가
    프로덕션 L0(`run_runtime_precheck` 실제 호출)과 같은가. `stage_a_off` 는 이 파일의
    `prelude`(L0 config 에서 `team_min_by_team` 을 뺀 값, `_build_prelude` 참고)로 새로
    짓는다 — 팀은 getter 가 새 세션에서 다시 계산한다(동어반복이 아니다). `live` 를 넘기지
    않아 간호사 투영은 `_as_nurse_views`(오프라인 사본)를 거친다.

    `run_runtime_precheck` 는 **호출자가 패치 걸리기 전에 미리 떠 둔** 원본을 받는다 —
    이 시점(`after_stop`, 생성 끝난 뒤)엔 `services.precheck.run_runtime_precheck` 가
    `Capture._spy_precheck`(`functools.wraps` 없음, L0 기록용)로 이미 감싸여 있어, 여기서
    다시 `from services.precheck import run_runtime_precheck` 로 뜨면 `inspect.signature` 가
    `(*args, **kwargs)` 꼴만 보여 `_bound_arguments` 가 깨진다(리뷰 포함 안 된 버그 — 실측:
    바인딩 결과가 `{"args": [], "kwargs": {...}}` 로 나와 L0 과 통째로 달라짐)."""
    if not layers["L0"]:
        return None
    from services.engine_input.builder import build_precheck_stage
    from services.engine_input.snapshot import canonical
    from services.engine_input.views import to_precheck_input

    stage_a_off = build_precheck_stage(session, prelude)
    # (EI-10g2) 프로덕션은 고정근무 차감 **뒤** config 를 precheck 에 넘긴다 — 차감이 대입하는 두 키는 L0(차감 뒤)에서 가져온다.
    #   (이 두 키의 차감 값 자체는 `demand.post` 대조가 따로 본다.)
    off_config = dict(prelude.config_dict)
    for key in _CUT_DEMAND_KEYS:
        if key in layers["L0"][0]["config_dict"]:
            off_config[key] = copy.deepcopy(layers["L0"][0]["config_dict"][key])
    # (EI-10g4) 단계 B 제약은 오프라인으로 지은 엔진 입력(`ei`)에서 싣는다 — 프로덕션은 precheck 앞 빌더 결과를 싣는다.
    call = to_precheck_input(stage_a_off, off_config, ei=ei, log=lambda _m: None)
    offline = canonical(_bound_arguments(run_runtime_precheck, (), call.kwargs()))
    old = layers["L0"][0]
    return None if offline == old else f"D1(오프라인 precheck 입력) 불일치: {_fmt(old)} != {_fmt(offline)}"


def _compare_presolve_view_offline(session: Session, prelude, layers: dict) -> str | None:
    """D2(§4.4) — 오프라인 `presolve_view(stage_a_off, <L0p config 6키를 덮은 사본>)` 의
    `code2main`·`id_to_main`·`fixed_cells`·`num_days`·간호사 id 순서가 L0p(탐지기 실제
    호출)와 같은가. L0p 의 `config` 는 `DETECTOR_CONFIG_KEYS` 6키만 담긴 사본이다(capture.py
    `_spy_detector`) — `assemble_presolve_view` 는 `config_dict` 를 그대로 보관만 하고
    `code2main`·`id_to_main`·`fixed_cells`·`num_days` 어디에도 쓰지 않으므로(presolve_block.py)
    이 비교엔 값이 없지만, 명세가 요구한 대로 덮어 짓는다."""
    if not layers["L0p"]:
        return None
    from services.engine_input.builder import build_precheck_stage
    from services.engine_input.views import presolve_view

    stage_a_off = build_precheck_stage(session, prelude)
    merged_config = {**prelude.config_dict, **(layers["L0p"][0].get("config") or {})}
    view = presolve_view(stage_a_off, merged_config)
    old = layers["L0p"][0]
    mismatches: list[str] = []
    if view.code2main != (old.get("code2main") or {}):
        mismatches.append(f"D2 code2main 불일치: {_fmt(old.get('code2main'))} != {_fmt(view.code2main)}")
    if view.id_to_main != (old.get("id_to_main") or {}):
        mismatches.append(f"D2 id_to_main 불일치: {_fmt(old.get('id_to_main'))} != {_fmt(view.id_to_main)}")
    old_fixed_cells = old.get("fixed_cells") or []
    if view.fixed_cells != old_fixed_cells:
        mismatches.append(f"D2 fixed_cells 불일치: {_fmt(old_fixed_cells)} != {_fmt(view.fixed_cells)}")
    if view.num_days != old.get("num_days"):
        mismatches.append(f"D2 num_days 불일치: {old.get('num_days')!r} != {view.num_days!r}")
    old_nurse_ids = [_nurse_record_id(n) for n in (old.get("nurses") or [])]
    new_nurse_ids = [getattr(n, "nurse_id", None) for n in view.nurses]
    if old_nurse_ids != new_nurse_ids:
        mismatches.append(f"D2 간호사 id 순서 불일치: {old_nurse_ids} != {new_nurse_ids}")
    return "; ".join(mismatches) or None


def _getter_order(session: Session, prelude) -> list[str]:
    """등급→주말휴무→팀→근무코드 순으로 불러, 그 네 표만 그 순서로 나오는지 쿼리열을 남긴다(§1.2)."""
    from services.engine_input.builder import build_precheck_stage

    wanted = set(_PRECHECK_TABLES)
    log = QueryLog()
    log.attach(session.connection())
    log.armed = True
    try:
        stage = build_precheck_stage(session, prelude)
        stage.grade.get()
        stage.weekend_ids.get()
        stage.team_rows.get()
        stage.shift_lookup.get()
    finally:
        log.detach()
    order: list[str] = []
    for stmt in log.statements:
        _verb, _, tables = stmt.partition(":")
        for table in tables.split("+"):
            if table in wanted:
                order.append(table)
    return order


# ── 실행 ────────────────────────────────────────────────────────────────


@dataclasses.dataclass
class DualBuildResult:
    """한 시나리오의 이중 실행 결과 — 대조 결과 + getter 순서 + (골든이 있으면) 캡처런."""

    scenario: str
    path: str
    mismatches: list[str]
    getter_order: list[str]
    fixed_cut_spied: bool
    #: `_collect_nurses_and_preferences` 스파이가 특수요청을 1건 이상 캡처했는가(O-11,
    #: §0 지시 E) — '캡처가 조용히 빈 채로 넘어가지 않았는가' 를 보는 용도.
    special_requests_count: int
    #: 오프라인 합본의 `load_report.errors` — 비어 있어야 한다(리뷰 지적, §2.4-2 '무raise' 와는
    #: 별개로 '부품이 조용히 실패하지 않았는가'를 본다). 시나리오별 허용 예외는
    #: `_EXPECTED_LOAD_REPORT_ERROR_KEYS` 에 명시한다.
    load_report_errors: dict[str, str]
    #: `config_override` 가 캡처돼 production 모드 이중 실행이 실제로 돌았는가(M-H, 리뷰
    #: 지적 — '캡처가 조용히 빈 채로 넘어가 비교가 헛돌지 않았는가' 를 보는 용도).
    production_mode_compared: bool
    captured_run: CapturedRun
    #: EI-13a-2 — 프로덕션이 첫 시도 직전에 부른 빌더의 `(EngineInput, AttemptSource | None)`(없으면 None) 과
    #: 그 시나리오의 오프라인 합본·프렐류드 — `test_ei13a_first_attempt_source.py` 가 digest 를 대조한다.
    first_attempt_build: tuple | None = None
    offline_engine_input: Any = None
    prelude: Any = None


def _run_dual_build(engine, scenario: Scenario) -> DualBuildResult:
    """`after_stop` 을 달아 시나리오를 1회 돌리고, 오프라인 합본을 옛 조립과 대조한다."""
    # D1 용 원본 — 시나리오 패치가 걸리기 **전**에 떠 둔다(위 `_compare_precheck_call_offline`
    # docstring 참고 — `after_stop` 시점엔 이미 L0 기록 스파이로 감싸여 시그니처가 깨진다).
    import services.precheck as _precheck_module
    pristine_run_runtime_precheck = _precheck_module.run_runtime_precheck
    fixed_capture = _FixedCutCapture()
    first_call_capture = _FirstCallCapture()
    precheck_nurses_capture = _PrecheckNursesCapture()
    special_requests_capture = _SpecialRequestsCapture()
    shift_manage_capture = _ShiftManageCapture()
    combined_cells_capture = _CombinedCellsCapture()
    patched = _with_patches(
        scenario, _fixed_cut_capture_patch(fixed_capture),
        _first_call_capture_patch(first_call_capture),
        _precheck_nurses_capture_patch(precheck_nurses_capture),
        _special_requests_capture_patch(special_requests_capture),
        _shift_manage_capture_patch(shift_manage_capture),
        _combined_cells_capture_patch(combined_cells_capture),
    )
    outcome: dict[str, Any] = {}

    def after_stop(session: Session, req: Any, user: Any, _kwargs: dict, capture: Capture) -> None:
        from services.engine_input.builder import build_engine_input
        from services.engine_input.raw import read_raw_setting

        prelude = _build_prelude(
            capture.layers, fixed_capture, first_call_capture, precheck_nurses_capture,
            special_requests_capture, shift_manage_capture, combined_cells_capture, req, user)
        outcome["prelude"] = prelude
        outcome["getter_order"] = _getter_order(session, prelude)
        outcome["engine_input"] = build_engine_input(
            session, prelude, first_attempt_config=None, raw_reader=read_raw_setting,
            advanced_inference=bool(getattr(req, "advanced_inference", False)))
        outcome["d1_mismatch"] = _compare_precheck_call_offline(
            session, prelude, capture.layers, pristine_run_runtime_precheck, outcome["engine_input"])
        outcome["d2_mismatch"] = _compare_presolve_view_offline(session, prelude, capture.layers)
        outcome["engine_input_production"] = _production_engine_input(
            session, prelude, first_call_capture)
        outcome["first_attempt_build"] = capture.built_engine_inputs[0] if capture.built_engine_inputs else None

    run = run_with_output(engine, patched, after_stop=after_stop)
    assert "engine_input" in outcome, f"{scenario.name}: after_stop 미도달"
    ei = outcome["engine_input"]
    layers = run.result.layers
    mismatches = [m for m in (
        _compare_grade(ei, layers), _compare_team(ei, layers),
        _compare_demand_post(ei, layers), _compare_passthrough(ei, outcome["prelude"]),
        _compare_cells_and_carryover(ei, layers), _compare_raw(ei, layers),
        _compare_codebook(ei, layers), _compare_roster(ei, layers),
        _compare_settings_eff(ei, layers, run.result.path), _compare_health_leave(ei, layers),
        _compare_production_mode_matches_offline(ei, outcome.get("engine_input_production")),
        outcome.get("d1_mismatch"), outcome.get("d2_mismatch"),
    ) if m]
    return DualBuildResult(
        scenario=scenario.name, path=run.result.path, mismatches=mismatches,
        getter_order=outcome["getter_order"], fixed_cut_spied=fixed_capture.called,
        special_requests_count=len(special_requests_capture.special_shift_requests),
        load_report_errors=dict(ei.load_report.errors),
        production_mode_compared=outcome.get("engine_input_production") is not None,
        captured_run=run, first_attempt_build=outcome.get("first_attempt_build"),
        offline_engine_input=ei, prelude=outcome["prelude"])


ALL_DUAL_BUILD_SCENARIOS: dict[str, Scenario] = {
    **SCENARIOS, **LADDER_SCENARIOS,
    PRECHECK_BLOCK_SCENARIO.name: PRECHECK_BLOCK_SCENARIO,
    "S5-SP": _s5_sp(),
    "S5-MAX": _s5_max(),
    "S5-FW": _s5_fw(),
}


@pytest.fixture(scope="module")
def dual_build_results(engine, engine_input_tables) -> dict[str, DualBuildResult]:
    """시나리오 20종 전부를 `after_stop` 과 함께 1회씩 돌린다(§2.0 '12+4+3' + S5-SP + S5-MAX·S5-FW).

    LADDER_SCENARIOS 는 이미 `ladder_env`/`post_env` 패치(`redirect_live_graph` 포함)를 갖고
    있다 — `ladder.LIVE_GRAPH_DIRS` 는 test_ladder.py 와 공유하는 **프로세스 전역** 리스트라,
    여기서 드레인하지 않으면 `test_ladder.py::test_no_trace` 의 '흔적 0' 셈이 깨진다(같은
    pytest 세션에서 두 파일이 함께 돌 때만 보이는 간섭 — 재확인: 두 파일을 함께 돌려 통과
    확인했다). 반환값은 쓰지 않는다 — 이 파일의 관심사가 아니다.
    """
    from .ladder import drain_live_graph_dirs

    assert_sqlite_backends(engine)
    results = {name: _run_dual_build(engine, scenario)
               for name, scenario in ALL_DUAL_BUILD_SCENARIOS.items()}
    drain_live_graph_dirs()
    return results


# ── 테스트 ──────────────────────────────────────────────────────────────


def test_dual_build_covers_all_golden_scenarios(dual_build_results):
    """12(골든) + 4(사다리) + 1(precheck 차단) + 1(S5-SP) + 2(S5-MAX·S5-FW, §0 지시 D) = 20 전부."""
    assert set(dual_build_results) == set(ALL_DUAL_BUILD_SCENARIOS)
    assert len(dual_build_results) == 20


@pytest.mark.parametrize("name", list(ALL_DUAL_BUILD_SCENARIOS))
def test_dual_build_matches_old_assembly(name, dual_build_results):
    """오프라인 합본의 grade·team·demand.post·패스스루 부품이 옛 조립(L0/L0'/L1)과 같다(§2.5 대조표)."""
    result = dual_build_results[name]
    assert result.mismatches == [], f"{name}({result.path}): " + " | ".join(result.mismatches)


@pytest.mark.parametrize("name", list(ALL_DUAL_BUILD_SCENARIOS))
def test_dual_build_getter_order_matches_production(name, dual_build_results):
    """단계 A getter 를 §1.2 순서(등급→주말휴무→팀→근무코드)로 부르면 그 네 표만 그 순서로 나온다."""
    result = dual_build_results[name]
    assert result.getter_order == list(_PRECHECK_TABLES), (name, result.getter_order)


@pytest.mark.parametrize("name", list(ALL_DUAL_BUILD_SCENARIOS))
def test_dual_build_load_report_has_no_unexpected_errors(name, dual_build_results):
    """오프라인 합본의 `load_report.errors` 가 조용히 쌓이지 않는다(리뷰 지적 수정분).

    `_build_prelude` 가 canonical 사본(날짜가 ISO 문자열)을 재사용하던 때는 18/18 시나리오
    전부에서 `stage_b.allowed`·`post.health_leave` 가 `AttributeError: 'str' object has no
    attribute 'date'` 로 채워졌는데, `_safe_call` 이 삼키고 어떤 테스트도 `errors` 를 보지
    않아 드러나지 않았다. 지금은 `_EXPECTED_LOAD_REPORT_ERROR_KEYS` 에 없는 키가 하나라도
    남으면 실패하고, 명시했는데 더 이상 안 남아도(고쳐졌는데 표가 안 줄었으면) 실패한다 —
    허용 목록이 조용히 안 쓰이는 채로 남는 것을 막는다.
    """
    result = dual_build_results[name]
    expected = _EXPECTED_LOAD_REPORT_ERROR_KEYS.get(name, frozenset())
    got = set(result.load_report_errors)
    assert got == set(expected), (
        f"{name}: errors={result.load_report_errors!r} expected_keys={sorted(expected)!r}")


def test_dual_build_demand_cut_scenarios_are_not_vacuous(dual_build_results):
    """S5·S1-LT·S9-F·S5-SP·S5-MAX·S5-FW 는 실제로 고정근무 차감 스파이가 불렸다 —
    demand.post 비교가 헛돌지 않는다(§0 지시 D — S5-MAX·S5-FW 를 추가분으로 더했다)."""
    for name in _FIXED_CUT_SCENARIOS:
        assert dual_build_results[name].fixed_cut_spied is True, name
    for name in ("S1", "S2", "S3", "S4", "S6", "S7", "S8", "S1-L", "S1-LB", "S1-L3",
                 PRECHECK_BLOCK_SCENARIO.name):
        assert dual_build_results[name].fixed_cut_spied is False, name


def test_dual_build_production_mode_comparison_is_not_vacuous(dual_build_results):
    """production 모드 이중 실행(M-H, 리뷰 지적)이 실제로 돈 시나리오가 있다 — 첫 시도에
    닿는 17종(night_block 3종 — S9·S9-F·precheck_block — 제외)은 `config_override` 가
    캡처돼 비교가 돈다."""
    skipped = {"S9", "S9-F", PRECHECK_BLOCK_SCENARIO.name}
    for name, result in dual_build_results.items():
        assert result.production_mode_compared == (name not in skipped), name


def test_dual_build_special_shift_requests_capture_is_not_vacuous(dual_build_results):
    """S5-SP 는 `_collect_nurses_and_preferences` 스파이가 실제로 특수요청을 캡처한다(O-11).

    F01(제출 원티드 '연', 12/9)이 전체 그룹 조회에도 잡혀야 한다 — 0건이면 `_post_health_leave`
    가 쓰는 `prelude.special_shift_requests` 가 늘 빈 채였다는 뜻이라 O-11 수정이 무의미해진다.
    """
    assert dual_build_results["S5-SP"].special_requests_count >= 1


def test_dual_build_off_window_constraints_s6_is_not_vacuous(dual_build_results):
    """S6(병동이동)은 `off_window_constraints` 가 실제로 채워지는 유일한 골든이다(O-18).

    비교(`_compare_cells_and_carryover`)가 '양쪽 다 빈 dict라서 우연히 같다'는 헛돌이가
    아님을 직접 본다 — ⑤ 단독 재조회는 `{}` 인데(STATUS_9abcd.md 실측), `prelude.config_dict`
    (mid-month 가 precheck 앞에서 이미 심어 둔 값) 폴백 덕에 carryover 가 채워진다.
    """
    result = dual_build_results["S6"]
    assert result.mismatches == [], result.mismatches
    carryover = result.captured_run.result.layers["L1"][0]["config_data"].get("off_window_constraints")
    assert carryover, "S6 L1 의 off_window_constraints 가 비어 있다 — 비교가 헛돈다"
    assert set(carryover) == {"X01", "N07"}


@pytest.mark.parametrize("name", list(SCENARIOS))
def test_dual_build_patched_run_matches_existing_golden(name, dual_build_results):
    """`after_stop`+스파이를 단 실행도 기존 golden/<name>.json 과 바이트 같다(기존 골든 결과 무변경)."""
    result = dual_build_results[name]
    golden = read_golden(name)
    assert golden is not None, f"골든 없음: {name}"
    record = build_record(SCENARIOS[name], result.captured_run)
    report = mismatch_report(name, golden, record)
    assert report is None, report


def test_dual_build_before_production_differs_for_s6_transfer(engine, engine_input_tables):
    """조건 ② 음성 대조 — 병동이동 발효(`flush_pending_transfers`, assignment_service.py, 읽기만
    함)가 바꾸는 `NurseAssignment.status` 를 생성 **앞**(시드 직후)과 생성 **뒤**(`after_stop`,
    조건 ②)에 각각 스냅샷해 다르다는 것을 고정한다. 오프라인 합본을 '프로덕션 앞'에서 지었다면
    이 변화를 놓친다 — 9c 가 `after_stop` 을 GoldenStop **뒤**에 두는 이유(§2.5)의 직접 증거다.
    """
    from db.models import NurseAssignment

    assert_sqlite_backends(engine)
    scenario = SCENARIOS["S6"]

    def _snapshot(session: Session) -> dict[str, str]:
        rows = (session.query(NurseAssignment)
                .filter(NurseAssignment.reason == "병동이동",
                        NurseAssignment.target_group_id == GROUP_ID)
                .all())
        return {row.nurse_id: row.status for row in rows}

    # 생성 앞(시드 직후) — 독립 트랜잭션, 끝나면 롤백해 흔적을 남기지 않는다.
    # pysqlite 는 DML 전까지 BEGIN 을 내지 않는다(harness.py `_OPEN_TRANSACTION_SQL` 과 같은
    # 이유) — 이 빈 DELETE 가 없으면 DDL(ALTER TABLE)이 트랜잭션 밖에서 돌아 롤백으로 안 지워진다.
    conn = engine.connect()
    outer = conn.begin()
    try:
        conn.exec_driver_sql("DELETE FROM roster_config WHERE 1 = 0")
        for ddl in scenario.ddl:
            conn.exec_driver_sql(ddl)
        session = Session(bind=conn, join_transaction_mode="create_savepoint")
        try:
            scenario.seed(session)
            session.commit()
            before = _snapshot(session)
        finally:
            session.close()
    finally:
        outer.rollback()
        conn.close()
    assert before == {"X02": "active"}, before

    # 생성 뒤(after_stop, 조건 ②)
    after: dict[str, str] = {}

    def after_stop(session: Session, _req: Any, _user: Any, _kwargs: dict, _capture: Capture) -> None:
        after.update(_snapshot(session))

    run_with_output(engine, scenario, after_stop=after_stop)
    assert after == {"X02": "completed"}, after
    assert after != before
