"""모델 지문 골든 — 설계서 §7.1 보강(EI-1c, 10-09 리뷰 실험 · 10-13 실측 반영).

갈래 B(골든 모델 지문) 전용 테스트. `tests/engine_contract/` 는 다른 갈래가 고치는 중이라
이 파일은 건드리지 않는다(참조도 안 한다).

무엇을 보는가
  lex 변형(S1·S1-D·S2·S4·S5·S6·S7·S8) — `fallback_lex._solve_traced` 의 첫 호출(이 파일의
  모든 `solver.Solve`/`SolveWithSolutionCallback` 은 이 함수를 거친다 — 전수 확인됨)에서 모델을
  가로채 지문을 낸다. phase 가 `"stage1["` 로 시작해야 정상이다(기본 경로의 실제 첫 Solve).
  primary 변형(S1-P) — `cp_sat_basic._build_full_model` 의 첫 호출에서 지문을 낸다.
  두 경로 모두 원본 `solver.Solve` 는 불리지 않는다(solve 0 — `safety.solve_calls == 0` 로 본다).

기존 골든은 손대지 않는다
  `golden/*.json` 은 이 파일이 **절대 쓰지 않는다** — 실행 전후 sha256 를 대조해 확인한다
  (`test_existing_golden_untouched`). 지문 전용 골든은 별도 디렉터리 `golden_model/*.json` 다.

★ 실측 결정론 한계 — 2026-10-13 발견 → 2026-10-14 EI-1c 마무리로 하네스 쪽 해소(꼭 읽고
  아래 테스트 구성을 이해할 것)
  2026-10-13 당시: CP-SAT 모델의 **완전** 지문(어느 (간호사,일자) 쌍이 `transition_ban`·
  `pattern_eod`·`isolated_*` 류 패턴을 받는가까지)이 PYTHONHASHSEED 가 다르면 달라졌다 —
  대상 9개(lex 8 + primary 1) 전부에서 재현됐다. 원인은 `cp_sat_basic.py` 의
  `_normalize_requirements` 가 근무 요구치 키를 **set** 으로 모아 `shift_types`(= 변수 이름
  속 근무 색인)의 순서가 `PYTHONHASHSEED` 에 흔들리는 것이다(EI-16 결함 큐 '근무 순서
  결정론' — 실측·조사 `shift_order/REPORT.md`). 이 결함의 **수정 자체**는 앱 로직이라 이 작업
  범위(tests/) 밖이고(EI-16 이 나중에 고친다), 그동안 하네스 쪽에서 지문이 흔들리지 않게
  `model_canon.model_fingerprint` 에 그 실행이 실제로 쓴 `shift_types` 를 넘겨 변수 이름 속
  근무 색인을 **근무 코드 알파벳순**으로 다시 매기게 했다(`model_fingerprint.py` 의
  `_install_shift_types_capture` → `holder.shift_types` → `model_fingerprint(model,
  holder.shift_types)`). 그 결과 대상 9개 전부에서 **완전 지문(digest)까지 시드와 무관**해졌다
  — 아래 `test_subprocess_hashseed_digest_stable` 이 PYTHONHASHSEED 0·1·12345 세 하위
  프로세스로 이를 직접 확인한다(전에는 '요약 + 이름표별 호출 수'만 보고 완전 지문 불일치는
  알려진 한계로 적바림만 했는데, 그 우회는 더 필요 없어 걷어냈다).
    - 같은 프로세스 2회(`test_two_runs_identical_same_process`) — PYTHONHASHSEED 는 프로세스
      시작 때 한 번 정해지므로 같은 프로세스 안에서는 항상 같다(실측 확인: 같은 프로세스에서
      3번 반복해도 digest 동일). 그래서 여기서는 **완전** 지문까지 단언한다.
    - PYTHONHASHSEED 가 다른 하위 프로세스 3회(`test_subprocess_hashseed_digest_stable`) —
      0·1·12345 세 값으로 **완전 지문(digest 포함) 전체 레코드**가 같음을 단언한다(재표기가
      실제로 EI-16 결함을 하네스에서 흡수했는지 보는 핵심 테스트).
    - 골든 비교(`test_golden_model`) — 완전 지문이 재현 가능해야 쓸모가 있으므로, **항상
      PYTHONHASHSEED=0 으로 고정한 하위 프로세스**로 지문을 낸다(위에서 시드 무관함을
      확인했으니 이 고정값 자체는 '우회'가 아니라, golden_model/*.json 에 적을 값 하나를
      고르는 보통의 결정이다 — 아무 값이나 같아야 하므로 0 을 그대로 쓴다). 실측: 같은
      seed=0 하위 프로세스를 따로 여러 번 실행해도 digest 가 완전히 같다 — 그래서
      UPDATE_GOLDEN_MODEL 없이 몇 번을 다시 돌려도(이 테스트 파일을 돈 pytest 프로세스 자신의
      hash seed 와 무관하게) 어긋나지 않는다.

UPDATE_GOLDEN_MODEL=1 로 `golden_model/*.json` 을 (다시) 쓴다(골든이 없으면 먼저 이렇게 만든다) — 기존 `golden/`(L0~L3) 의 `UPDATE_GOLDEN` 과 **다른 플래그**다(2026-10-13 보강, 아래
`UPDATE_ENV` 참고).

★ ortools 판 기준 — 2026-10-14(9.12.4544 → 9.15.6755 전환, `uv.lock` 고정판 = 운영 Lambda 판)
  `model.Proto()` 가 9.13+ 부터 pybind 래퍼(`SerializeToString`·`WhichOneof`·`HasField` 없음)로
  바뀌어 `model_canon.py`·`capture.py` 가 그대로 쓰면 지문 채취 자체가 예외로 죽는다(이 파일의
  지문이 전부 `None`이 돼 `test_stage_matches_variant` 의 `fingerprint is not None` 단언이
  먼저 잡는다). `tests/engine_input/proto_compat.py`(`model_to_pb`) 로 판과 무관하게 표준
  `cp_model_pb2.CpModelProto` 로 바꿔 읽도록 고쳤다 — **운영 판(9.15.6755)을 지금부터의
  기준으로 삼는다.** `golden_model/*.json` 9개는 9.12 시절에 떴지만, 9.15 로 재검증한 결과
  바이트 단위로 그대로 재현됐다(표현 차이 없음 — `UPDATE_GOLDEN_MODEL` 재생성 불필요, 실측
  근거는 `golden_model/README.md`)."""

from __future__ import annotations

import functools
import hashlib
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from .golden import GOLDEN_DIR, diff_paths
from .golden_gate import GOLDEN_MODEL
from .model_fingerprint import (
    FINGERPRINT_TARGETS,
    build_fingerprint_record,
    golden_model_path,
    read_golden_model,
    run_fingerprint,
    write_golden_model,
)

#: 지문 전용 골든(golden_model/) 재생성 플래그 — 기존 `golden/`(L0~L3) 의 `UPDATE_GOLDEN` 과
#: **다른 이름**이다(2026-10-13 보강). 전에는 같은 이름을 썼는데, `golden.py`·`test_scenarios.py`
#: 도 같은 `UPDATE_GOLDEN` 을 읽어서, 지문 골든만 다시 만들려고 `UPDATE_GOLDEN=1 pytest
#: tests/engine_input` 를 돌리면 기존 `golden/*.json` 도 같이 덮어써졌다 — "기존 골든 불변"
#: 원칙을 사용 실수 하나로 깰 수 있는 구조였다. 재생성은 이 파일 하나로 한정해서 돈다:
#: `UPDATE_GOLDEN_MODEL=1 pytest tests/engine_input/test_model_fingerprint.py::test_golden_model`.
#: 플래그 이름의 정본은 `golden_gate.GOLDEN_MODEL.update_env`(= "UPDATE_GOLDEN_MODEL") 다 — 이 이름은 별칭이다(D29).
UPDATE_ENV = GOLDEN_MODEL.update_env
_SUBPROCESS_SCRIPT = Path(__file__).resolve().parent / "_fp_subprocess_entry.py"
#: 가짜 AWS config/credentials 내용 — 실 키가 아니다(AWS_ACCESS_KEY_ID="testing" 과 짝).
_FAKEAWS_CONFIG = "[profile eitest]\nregion = ap-northeast-2\n"
_FAKEAWS_CREDENTIALS = "[eitest]\naws_access_key_id = testing\naws_secret_access_key = testing\n"


@functools.lru_cache(maxsize=1)
def _fakeaws_dir() -> Path:
    """가짜 AWS config/credentials 를 이 테스트 **자신이** 만든다 — 외부 고정 경로
    (`parents[4]/"fakeaws"`)에 기대지 않는다(2026-10-13 보강). 전에는 그 경로가 없으면
    `pytest.skip` 했는데, 이 파일을 저장소(roster-back/tests/engine_input)나 다른 깊이로
    옮기면 그 상대경로가 더 이상 맞지 않아(`parents[4]` 가 실제로는 존재하지 않는 다른
    디렉터리를 가리킨다) 골든 비교·solve 0·stage·hashseed 테스트 37개가 전부 조용히
    SKIP 되고 종료 코드는 0 이었다(설계서 §7.2 '빈 skip 금지' 위반 — 회귀를 놓친다).
    이제는 준비가 안 되면(디렉터리 생성·파일 쓰기 실패) 그대로 예외가 올라가 테스트가
    FAIL 한다 — SKIP 으로 숨지 않는다."""
    d = Path(tempfile.mkdtemp(prefix="aide_fakeaws_"))
    (d / "config").write_text(_FAKEAWS_CONFIG, encoding="utf-8")
    (d / "credentials").write_text(_FAKEAWS_CREDENTIALS, encoding="utf-8")
    return d


_MERGED_ROOT = Path(__file__).resolve().parents[2]
#: 골든 비교용 — 세 값 중 하나를 그대로 쓴다(위 docstring '실측 결정론 한계' — 시드 무관함을
#: 확인했으니 이 값 자체는 더 이상 결정론 '우회'가 아니다).
_PINNED_SEED = "0"
#: 완전 지문(digest) 교차 확인용 — 설계서 §7.1 예시(12345) + 추가 1(EI-1c 마무리, 2026-10-14).
_CROSS_SEEDS = ("1", "12345")

#: 하위 프로세스 전용 env — 작업 지시의 인라인 템플릿과 같다. PYTHONHASHSEED 만 호출마다 더한다.
_ENV_OVERRIDES = {
    "OPENAI_API_KEY": "", "ANTHROPIC_API_KEY": "", "GEMINI_API_KEY": "", "GOOGLE_API_KEY": "",
    "RUN_REAL_LLM": "", "REDIS_URL": "", "LANGFUSE_PUBLIC_KEY": "", "LANGFUSE_SECRET_KEY": "",
    "LANGFUSE_HOST": "", "AIDE_DAG_PLANNING": "",
    "MS_DB_HOST": "127.0.0.1", "MS_DB_PORT": "1", "DB_HOST": "127.0.0.1", "DB_PORT": "1",
    "SQS_QUEUE_URL": "", "AWS_ACCESS_KEY_ID": "testing", "AWS_SECRET_ACCESS_KEY": "testing",
    "AWS_SESSION_TOKEN": "", "AWS_PROFILE": "eitest",
    "AWS_EC2_METADATA_DISABLED": "true", "AWS_DEFAULT_REGION": "ap-northeast-2",
    "ENVIRONMENT": "development", "PYTHONDONTWRITEBYTECODE": "1",
}


def _update_requested() -> bool:
    return os.environ.get(UPDATE_ENV) == "1"


def _existing_golden_hashes() -> dict[str, str]:
    """기존 `golden/*.json` 전부의 sha256(불변 확인용 — 이 파일은 그 디렉터리를 쓰지 않는다)."""
    return {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(GOLDEN_DIR.glob("*.json"))}


def _subprocess_record(variant: str, name: str, hashseed: str) -> dict:
    """하위 프로세스(고정 env + PYTHONHASHSEED)로 지문 기록 JSON 한 줄을 받는다."""
    fakeaws_dir = _fakeaws_dir()
    env = dict(os.environ)
    env.update(_ENV_OVERRIDES)
    env["AWS_CONFIG_FILE"] = str(fakeaws_dir / "config")
    env["AWS_SHARED_CREDENTIALS_FILE"] = str(fakeaws_dir / "credentials")
    env["PYTHONHASHSEED"] = hashseed
    proc = subprocess.run(
        [sys.executable, str(_SUBPROCESS_SCRIPT), variant, name],
        cwd=str(_MERGED_ROOT), env=env, capture_output=True, text=True, timeout=180,
    )
    assert proc.returncode == 0, (
        f"{name}/{variant} seed={hashseed} 하위 프로세스 실패(exit={proc.returncode}):\n"
        f"{proc.stderr[-4000:]}")
    lines = proc.stdout.strip().splitlines()
    assert lines, f"{name}/{variant} seed={hashseed} 표준출력이 비었다\nstderr:\n{proc.stderr[-2000:]}"
    return json.loads(lines[-1])


# ── 골든 비교 — PYTHONHASHSEED 고정 하위 프로세스(재현 가능) ─────────────


@pytest.fixture(scope="module")
def pinned_records():
    """대상 전부를 `PYTHONHASHSEED=0` 고정 하위 프로세스로 한 번씩 돌린다(기존 골든 불변 확인 포함)."""
    before = _existing_golden_hashes()
    records = {name: _subprocess_record(variant, name, _PINNED_SEED) for variant, name in FINGERPRINT_TARGETS}
    after = _existing_golden_hashes()
    return {"records": records, "before": before, "after": after}


def test_existing_golden_untouched(pinned_records):
    """`golden/*.json` 은 이 파일이 손대지 않는다 — sha256 이 실행 전후 같다."""
    assert pinned_records["after"] == pinned_records["before"]


@pytest.mark.parametrize("variant,name", FINGERPRINT_TARGETS)
def test_solve_zero(variant, name, pinned_records):
    """지문을 낸 시나리오는 실제 `CpSolver.Solve` 를 한 번도 부르지 않는다(solve 0)."""
    record = pinned_records["records"][name]
    assert record["safety"]["solve_calls"] == 0, record["safety"]
    assert record["safety"]["separate_sessions"] == 0, record["safety"]


@pytest.mark.parametrize("variant,name", FINGERPRINT_TARGETS)
def test_stage_matches_variant(variant, name, pinned_records):
    """lex 변형은 stage1 에서, primary 변형은 primary_build 에서 멈춘다.

    다른 Solve 가 먼저 불리면(`probe_calls` 가 비지 않음) 실패로 본다 — 실측상 지금 코드
    경로는 비지 않는다(설계서 §7.1 '그 앞 Solve' 처리 — 없으면 그냥 첫 Solve).
    """
    record = pinned_records["records"][name]
    assert record["probe_calls"] == [], record["probe_calls"]
    expected_stage = "stage1" if variant == "lex" else "primary_build"
    assert record["stage"] == expected_stage, record
    assert record["outcome"] == "golden_stop", record
    assert record["fingerprint"] is not None, record


@pytest.mark.parametrize("variant,name", FINGERPRINT_TARGETS)
def test_golden_model(variant, name, pinned_records):
    """지문 골든 비교(`UPDATE_GOLDEN_MODEL=1` 이면 다시 쓴다) — `golden/` 이 아니라 `golden_model/`.

    `PYTHONHASHSEED=0` 으로 고정한 하위 프로세스 기록이라 재현 가능하다(실측: 같은 seed 하위
    프로세스를 여러 번 따로 실행해도 완전 지문 digest 가 같다) — `UPDATE_GOLDEN_MODEL` 없이 몇 번을
    다시 돌려도 이 파일을 돌리는 pytest 프로세스 자신의 hash seed 와 무관하게 어긋나지 않는다.
    """
    record = pinned_records["records"][name]
    if _update_requested():
        write_golden_model(name, record)
        return
    golden = read_golden_model(name)
    assert golden is not None, f"지문 골든 없음: {golden_model_path(name)} — UPDATE_GOLDEN_MODEL=1 로 만든다"
    lines = diff_paths(golden, record)
    assert lines == [], "\n".join(lines[:40])


# ── 결정론 ───────────────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def inprocess_pairs(engine, engine_input_tables, guard_log):
    """대상 전부를 **이** 프로세스에서 두 번씩 돌린다(완전 지문 결정론 + 외부 접촉 확인용)."""
    before = len(guard_log)
    pairs = {}
    for variant, name in FINGERPRINT_TARGETS:
        runs = []
        for _ in range(2):
            result, holder = run_fingerprint(engine, name, variant)
            runs.append(build_fingerprint_record(name, variant, result, holder))
        pairs[name] = runs
    return {"pairs": pairs, "blocked": list(guard_log[before:])}


def test_no_external_contact(inprocess_pairs):
    """지문 하네스(이 프로세스 경로)는 바깥(실 DB·AWS·외부망)에 닿지 않는다 — package 가드
    (`conftest.py`)가 막은 시도가 0. 하위 프로세스 경로는 같은 `run_production` 호출을 그대로
    타고 같은 가짜 sqlite/db.client2 패치 아래서 돌므로 같은 보장을 구조적으로 물려받는다
    (기존 `golden/` 시나리오 테스트가 이미 같은 호출 경로로 이 보장을 확인했다)."""
    assert inprocess_pairs["blocked"] == []


@pytest.mark.parametrize("variant,name", FINGERPRINT_TARGETS)
def test_two_runs_identical_same_process(variant, name, inprocess_pairs):
    """같은 프로세스에서 두 번 돌린 지문이 **완전히** 같다(설계서 §7.1 결정론 — 같은 커밋 2회).

    PYTHONHASHSEED 는 프로세스 시작 때 한 번 정해지므로 같은 프로세스 안에서는 늘 같다 —
    그래서 여기서는 완전 지문(digest)까지 단언한다.
    """
    rec1, rec2 = inprocess_pairs["pairs"][name]
    lines = diff_paths(rec1, rec2)
    assert lines == [], "\n".join(lines[:40])


def test_s1p_env_isolated_but_skip_primary_preserved(pinned_records):
    """env 격리 patch 순서 회귀(2026-10-13) — 격리는 `base.patches` **앞**에 와야 한다.
    뒤에 두면 S1-P 의 `skip_primary_off`(SKIP_PRIMARY=0) 가 지워져 primary 대신 lex 경로로
    샌다. S1 은 완전히 격리돼 빈 env 여야 한다(개발자 셸 플래그가 안 섞인다)."""
    s1p = pinned_records["records"]["S1-P"]
    assert s1p["env"] == {"SKIP_PRIMARY": "0"}
    assert s1p["stage"] == "primary_build"
    s1 = pinned_records["records"]["S1"]
    assert s1["env"] == {}


@pytest.mark.parametrize("variant,name", FINGERPRINT_TARGETS)
@pytest.mark.parametrize("seed", _CROSS_SEEDS)
def test_subprocess_hashseed_digest_stable(variant, name, seed, pinned_records):
    """`PYTHONHASHSEED` 0(기준) vs 1·12345 하위 프로세스 — **완전 지문(digest 포함) 전체
    레코드**가 같아야 한다(EI-1c 마무리, 2026-10-14 — model_canon 의 근무 색인 재표기가 EI-16
    '근무 순서 결정론' 결함을 하네스에서 흡수했는지 보는 핵심 테스트).

    2026-10-13 당시엔 완전 지문이 대상 9개 전부에서 시드마다 달라(모듈 docstring '실측
    결정론 한계'), 요약·이름표 호출 수만 비교하고 digest 불일치는 '알려진 한계'로 적바림만
    했다 — 그 우회는 이제 필요 없어 걷어냈다. 시드가 달라도 전체 레코드가 완전히 같아야
    한다(`fingerprint`·`digest` 포함 — `safety`·`env`·`rule_counts`·`summary` 는 당연히
    같았으니 덤이다)."""
    rec0 = pinned_records["records"][name]  # seed=0 — pinned_records 와 같은 subprocess 재사용
    rec1 = _subprocess_record(variant, name, seed)
    lines = diff_paths(rec0, rec1)
    assert lines == [], f"seed=0 vs seed={seed}:\n" + "\n".join(lines[:40])


#: 하드락 8종 중 대상 9개 조합 안에서 **실측으로 이미 켜지는** 이름표(model_fingerprint.py
#: 모듈 docstring '★ 사각지대' 참고) — 조용히 다시 비면 알아채도록 고정한다. `enforce_4o`
#: 는 뺐다 — 그건 "아직 못 넓힌 사각지대"로 알려진 상태라 여기 넣으면 늘 실패한다.
_HARD_LOCK_IDS_COVERED = {"not_one_night": "S7", "recovery_2n2off": "S7", "mid_transition": "S4"}


def test_known_hard_lock_coverage_does_not_silently_regress(pinned_records):
    """하드락 #5(2N→2OFF)·#7(1N 금지)·#8(MID) 이름표가 대상 9개 중 최소 하나에서는 계속
    불려야 한다 — 시나리오를 고치다 실수로 그 분기를 다시 안 타게 만들면 이 테스트가 먼저
    깨진다(조용히 커버리지가 좁아지는 것을 막는다, HIGH 리뷰 지적의 보수적 완화판 — 전체
    해소(`enforce_4o` 포함 4종 전부)는 새 시나리오/가로채기 지점이 필요해 이번 범위 밖이다)."""
    missing = []
    for rule_id in _HARD_LOCK_IDS_COVERED:
        total = sum(pinned_records["records"][name]["rule_counts"].get(rule_id, 0)
                    for _variant, name in FINGERPRINT_TARGETS)
        if total <= 0:
            missing.append(rule_id)
    assert missing == [], f"하드락 이름표가 더 이상 대상 9개 어디서도 안 불린다: {missing}"
