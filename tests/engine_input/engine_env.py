"""엔진 실험 env — 이름 목록 · 분류 · 호출 시점 실효값 · 실행 동안 격리(설계서 §3.1·§3.2·§4.4).

이름 목록
  엔진·생성 경로 파일(`services/cp_sat/` · `cp_sat_basic.py` · `roster_create_service.py`)에서
  `getenv("X")` · `environ.get("X")` · `environ["X"]` 꼴의 **문자열 리터럴 이름**을 읽어 모은다.
  설계서 재집계는 67종이다. 변수로 넘기는 이름은 잡히지 않는다 — 전수 확정은 EI-6 몫이다.
기록(빌드 시점 스냅샷 금지 — §3.2·§4.4)
  `env_effective()` 는 **부르는 그 순간**의 os.environ 을 읽는다. 기록기는 엔진 입구가 불릴 때마다
  부른다. 그래서 실행 중 켰다 끄는 토글(`FB_VERIFY_SKIP_STAGE3` · `AIDE_ENABLE_MUS_REGISTRY`)이
  그 시도에만 보인다. 값이 있는 이름만 남긴다(없으면 코드 기본값 — 운영 EC2 `.env` 에도 엔진
  플래그는 0개다, §3.1).
  ★ import 때 읽어 모듈 상수로 굳는 이름은 기록값과 실효값이 다를 수 있다([PY-04]).
분류(§3.1 표)
  런타임 토글·진단(`FB_VERIFY_SKIP_STAGE3` · `AIDE_ENABLE_MUS_REGISTRY` · `AIDE_PROBE_*` ·
  `MUS_DIAG_*` · `UNDIAG_PROBE_DISABLE` · `AIDE_LEX_LOG/TRACE` · `AIDE_DUMP_CASE`)은 기록만 하고
  digest 에서 뺀다. 나머지(엔진 계약·실험 플래그)는 digest 에 넣는다.
격리
  사다리·성공 모드 시나리오는 실행 동안 엔진 env 를 지우고 시나리오가 정한 값만 둔다. 개발자
  셸·`.env` 의 실험 플래그가 골든에 섞이지 않게 하려는 것이다. 끝나면 MonkeyPatch 가 되돌린다.
"""

from __future__ import annotations

import functools
import importlib.util
import os
import re
from pathlib import Path

import pytest

#: 엔진·생성 경로 — `services` 패키지 기준 상대 경로.
ENGINE_ENV_SOURCES = ("cp_sat", "cp_sat_basic.py", "roster_create_service.py")
_ENV_NAME_RE = re.compile(
    r"""(?:getenv|environ\.get|environ\.setdefault|environ\.pop)\(\s*["']([A-Z][A-Z0-9_]+)["']"""
    r"""|environ\[\s*["']([A-Z][A-Z0-9_]+)["']\s*\]""")

#: §3.1 '런타임 토글·진단' — 기록만, digest 제외.
RUNTIME_TOGGLE_NAMES = frozenset({
    "FB_VERIFY_SKIP_STAGE3", "AIDE_ENABLE_MUS_REGISTRY", "UNDIAG_PROBE_DISABLE",
    "AIDE_LEX_LOG", "AIDE_LEX_TRACE", "AIDE_DUMP_CASE",
})
RUNTIME_TOGGLE_PREFIXES = ("AIDE_PROBE_", "MUS_DIAG_")
#: 격리 때 목록 밖에서도 지우는 접두(진단 경로가 읽는 `AIDE_SHADOW_*` 등).
ISOLATE_PREFIXES = ("AIDE_", "MUS_", "UNDIAG_", "FB_VERIFY_")


def _services_dir() -> Path:
    """`services` 패키지 폴더(import 하지 않고 찾는다)."""
    spec = importlib.util.find_spec("services")
    if spec is None or not spec.submodule_search_locations:
        raise RuntimeError("services 패키지를 찾지 못했다")
    return Path(next(iter(spec.submodule_search_locations)))


def _source_files() -> list[Path]:
    """스캔 대상 파일(폴더는 하위 .py 전부, 이름순)."""
    root = _services_dir()
    files: list[Path] = []
    for rel in ENGINE_ENV_SOURCES:
        path = root / rel
        files.extend(sorted(path.rglob("*.py")) if path.is_dir() else [path])
    return files


@functools.lru_cache(maxsize=1)
def engine_env_names() -> tuple[str, ...]:
    """엔진 경로가 문자열 리터럴로 읽는 env 이름(정렬)."""
    names: set[str] = set()
    for path in _source_files():
        for match in _ENV_NAME_RE.finditer(path.read_text(encoding="utf-8")):
            names.add(match.group(1) or match.group(2))
    return tuple(sorted(names))


def is_runtime_toggle(name: str) -> bool:
    """§3.1 런타임 토글·진단인가(기록만, digest 제외)."""
    return name in RUNTIME_TOGGLE_NAMES or name.startswith(RUNTIME_TOGGLE_PREFIXES)


def env_effective(names: tuple[str, ...] | None = None) -> dict[str, str]:
    """지금 이 순간 값이 있는 엔진 env(이름 → 값). 없으면 코드 기본값이라 남기지 않는다."""
    return {name: os.environ[name] for name in (names or engine_env_names()) if name in os.environ}


def isolate_engine_env(mp: pytest.MonkeyPatch, **overrides: str) -> None:
    """실행 동안 엔진 env 를 지우고 overrides 만 둔다(mp 가 끝나면 원상 복구)."""
    doomed = set(engine_env_names()) | {k for k in os.environ if k.startswith(ISOLATE_PREFIXES)}
    for name in sorted(doomed):
        mp.delenv(name, raising=False)
    for name, value in overrides.items():
        mp.setenv(name, value)
