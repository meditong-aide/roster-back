"""골든 문지기 — 골든이 없는 체크아웃에서는 골든 대조를 D29 사유로 건너뛰고, 일부만 있으면 실패한다(설계서 §7.1 D29).

왜 있는가
  골든 JSON 은 커밋하지 않는다(로컬 전용 · `.gitignore` 의 `tests/engine_input/golden*/**/*.json` · D29=① 2026-10-20).
  새로 받은 체크아웃에는 `golden/`·`golden_presolve/`·`golden_base/dev4ec454e/` 가 폴더째 없고 `golden_model/`·
  `golden_base/` 에는 README.md 만 있다. 그 체크아웃에서 골든 대조 테스트가 '파일 없음'으로 빨개지지 않게 하되,
  이 브랜치 로컬에서는 골든 하나가 빠지거나 남는 것을 조용히 넘기지 않는다.

판정(`require_golden`) — 디렉터리 **바로 아래** `*.json` 수 n 으로만 본다(하위 폴더는 세지 않는다)
  n == 0                        → `REQUIRE_GOLDEN=1` 이면 실패, 아니면 D29 사유로 건너뜀(갱신 플래그가 켜져 있어도 같다 —
                                  읽는 쪽은 대조할 것이 없다. 쓰는 테스트는 먼저 쓰고 읽으므로 그때는 이미 n ≥ 1 이다)
  n != expected · 갱신 중 아님  → 실패(일부만 있거나 남으면 건너뛰지 않는다 — 파일 단위 건너뛰기 금지)
  그 밖                          → 그대로 돌아간다
  건너뛰기는 디렉터리 **전체**가 비었을 때만 한다. 파일 단위로 건너뛰면 파일 하나가 빠지거나 재생성 중 지워져도 조용히
  지나가서 `golden.py` 의 '파일이 없으면 실패'와 Δ 대조의 전제가 함께 깨진다. 건너뛸 때는 사유가 늘 `SKIP_PREFIX` 로
  시작하고(§7.2 '빈 skip 금지'), `tests/engine_input/conftest.py` 의 `pytest_terminal_summary` 가 그 수를 한 줄로 집계한다.
  그 conftest 는 engine_input 을 함께 돌릴 때만 올라온다 — `tests/engine_contract` 만 돌리면 집계 줄이 없으니 사유는 `-rs`
  로 본다.

쓰는 법
  골든 파일을 여는 함수의 **첫 줄**에서 그 디렉터리의 세트로 부른다(`golden.read_golden` · `model_fingerprint.read_golden_model`
  등) — 그 함수를 거치는 테스트는 따로 고치지 않아도 같은 규칙을 따른다. 문지기를 거치는 함수는 모듈 import 때 부르지 않는다
  (골든이 없으면 `allow_module_level` 없는 skip 이라 수집 오류다). 매개변수 목록은 코드가 정의한 이름에서 뽑고, 골든 데이터는
  처음 쓰는 테스트에서 읽는다(예: `test_precheck_vs_engine_stage1._template` — `functools.cache`). `allow_module_level=True`
  는 남겨 두지만 테스트 모듈은 쓰지 않는다 — 비었을 때만 모듈 전체를 건너뛸 뿐 개수가 어긋나거나 `REQUIRE_GOLDEN=1` 이면 수집
  오류이고, pytest 는 수집 오류에서 세션 전체를 멈춘다(Interrupted). `test_golden_gate.py` 가 사용 0곳을 단언하고, 어느 함수가
  골든을 읽으면서 문지기를 안 거치는지(첫 줄·세트·import 때 호출 포함)도 그 파일의 정적 감사가 잡는다.

경로·플래그 이름은 **여기 한 곳에만** 둔다. `golden.GOLDEN_DIR`·`model_fingerprint.GOLDEN_MODEL_DIR` 같은 옛 상수는 이 값의
별칭이다(import·grep 이 그대로 통한다). 재수신 때 고치는 '골든 기준 상수'는 `GOLDEN_BASE`(폴더 이름·개수)와
`test_presolve_block_characterization.FROZEN_SHA256` 이다.

이 모듈은 표준 라이브러리와 pytest 만 쓴다(다른 테스트 모듈을 import 하지 않는다 — 순환 방지).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent


@dataclass(frozen=True)
class GoldenSet:
    """골든 디렉터리 하나 — 이름 · 경로 · 이 브랜치 로컬의 JSON 수 · 이 디렉터리를 (다시) 쓰는 플래그."""

    key: str  # 사유·메시지에 쓰는 이름 — tests/engine_input/<key>/
    path: Path
    expected: int  # 이 브랜치 로컬의 JSON 수(D29 파일 수 단언)
    update_env: str | None  # 이 디렉터리를 (다시) 쓰는 플래그. 동결 사본은 None


GOLDEN = GoldenSet("golden", HERE / "golden", 16, "UPDATE_GOLDEN")
GOLDEN_PRESOLVE = GoldenSet("golden_presolve", HERE / "golden_presolve", 3, "UPDATE_GOLDEN_PRESOLVE")
GOLDEN_MODEL = GoldenSet("golden_model", HERE / "golden_model", 9, "UPDATE_GOLDEN_MODEL")
GOLDEN_BASE = GoldenSet("golden_base/dev4ec454e", HERE / "golden_base" / "dev4ec454e", 16, None)
ALL_GOLDEN_SETS = (GOLDEN, GOLDEN_PRESOLVE, GOLDEN_MODEL, GOLDEN_BASE)
REQUIRE_ENV = "REQUIRE_GOLDEN"
SKIP_PREFIX = "D29 골든 없음"


def _flag_on(name: str | None) -> bool:
    """플래그가 켜졌는가 — 값이 정확히 `"1"` 일 때만(`"0"`·빈 값·다른 값은 꺼짐). `golden.update_requested()` 와 같은 비교."""
    return name is not None and os.environ.get(name) == "1"


def _where(gs: GoldenSet) -> str:
    return f"tests/engine_input/{gs.key}/"


def json_count(gs: GoldenSet) -> int:
    """`gs.path` 바로 아래의 `*.json` 파일 수(폴더가 없으면 0). 하위 폴더는 세지 않는다."""
    if not gs.path.is_dir():
        return 0
    return sum(1 for path in gs.path.glob("*.json") if path.is_file())


def skip_reason(gs: GoldenSet) -> str:
    """골든이 없어 건너뛸 때의 사유 — 늘 `SKIP_PREFIX` 로 시작한다(집계·grep 이 이 접두어를 본다)."""
    return (
        f"{SKIP_PREFIX}: {_where(gs)} 에 *.json 이 하나도 없다 — 골든은 커밋하지 않는 로컬 전용(D29)이라 "
        f"이 체크아웃에서는 골든 대조를 건너뛴다. 실패로 바꾸려면 {REQUIRE_ENV}=1"
    )


def require_golden(gs: GoldenSet, *, allow_module_level: bool = False) -> None:
    """골든 디렉터리 `gs` 를 읽기 전에 부른다 — 비었으면 건너뜀(또는 실패), 수가 기준과 다르면 실패(모듈 docstring 표)."""
    n = json_count(gs)
    updating = _flag_on(gs.update_env)
    if n == 0:
        if _flag_on(REQUIRE_ENV):
            pytest.fail(f"{skip_reason(gs)} — {REQUIRE_ENV}=1 이라 실패", pytrace=False)
        pytest.skip(skip_reason(gs), allow_module_level=allow_module_level)
    if n != gs.expected and not updating:
        pytest.fail(
            f"D29 골든 수 불일치: {_where(gs)} 의 *.json 이 {n}개다(기준 {gs.expected}개). 일부만 있거나 남으면 건너뛰지 않고 "
            f"실패한다(파일 하나가 빠지거나 재생성 중 지워진 것을 조용히 넘기지 않는다). 골든을 다시 갖춰 두거나, 시나리오를 더하거나 "
            f"뺐다면 같은 변경에서 golden_gate 의 개수({gs.key} = {gs.expected})를 고친다.",
            pytrace=False,
        )
