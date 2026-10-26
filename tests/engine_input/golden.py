"""골든 JSON — 기록 형식 · 읽기/쓰기 · 어긋난 자리(층·키) 보고(설계서 §7.1 '골든 재생성 규칙').

규칙
  - 파일: `tests/engine_input/golden/<시나리오>.json`(키 정렬·들여쓰기 1·끝 줄바꿈).
  - **`UPDATE_GOLDEN=1` 일 때만 쓴다.** 아니면 비교만 한다(파일이 없으면 실패).
  - 비교가 틀리면 어긋난 자리를 `$.layers.L1[0].config_data.n2n_min_gap: 3 → 0` 처럼 보인다.
기록하는 칸
  outcome·path·error·http_detail(차단 payload)·digest·쿼리(수·순서열·지점 표지)·안전장치 횟수·
  삼킨 실패 줄(앱이 예외를 삼키고 찍은 줄)·WARNING 이상 로그·층 전체(L0·L0'·L1·L2·P, 사다리·성공
  모드면 attempts·L3 — ladder.py).
  digest 는 §2.4 제외 칸(settings_raw 메타 6칸 · L3 · 런타임 토글 env)과 시도 기록의 쿼리 위치
  (`attempts[*].queries_before`)를 뺀 층의 sha256 이다.
  층 원본은 그대로 둔다.
D29(2026-10-20) — 골든 JSON 은 커밋하지 않는 로컬 전용이다. `read_golden` 은 첫 줄에서 문지기(`golden_gate.require_golden`)
  를 거친다: 디렉터리가 통째로 비었으면 D29 사유로 건너뛰고(`REQUIRE_GOLDEN=1` 이면 실패), 일부만 있으면 실패한다.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

from .canonical import canonical
from .golden_gate import GOLDEN, require_golden

#: 경로·플래그 이름의 정본은 `golden_gate.GOLDEN` 이다 — 아래 둘은 import·grep 이 그대로 통하게 남긴 별칭이다.
GOLDEN_DIR = GOLDEN.path
UPDATE_ENV = GOLDEN.update_env
_MAX_DIFF_LINES = 40


def update_requested() -> bool:
    """골든을 다시 쓸 차례인가(UPDATE_GOLDEN=1)."""
    return os.environ.get(UPDATE_ENV) == "1"


def golden_path(name: str) -> Path:
    """시나리오 이름 → 골든 파일 경로."""
    return GOLDEN_DIR / f"{name}.json"


#: 실행마다 달라지는 표기 — 메모리 주소 · 초안 근무표 id(uuid4 앞 12자리, roster_create_service.py:8315).
_VOLATILE = ((re.compile(r"0x[0-9a-fA-F]+"), "0x…"), (re.compile(r"\b[0-9a-f]{12}\b"), "<schedule_id>"))


def _mask_volatile(line: str) -> str:
    """실행마다 달라지는 표기를 가린다."""
    for pattern, repl in _VOLATILE:
        line = pattern.sub(repl, line)
    return line


def swallowed_failure_lines(stdout: str) -> list[str]:
    """앱이 예외를 삼키고 남긴 줄('실패' 포함 · 'error:' 로 시작) — 조용한 기본값 추락의 흔적."""
    return [_mask_volatile(line) for line in stdout.splitlines()
            if "실패" in line or line.startswith("error:")]


def build_record(scenario, run) -> dict:
    """한 번의 실행(CapturedRun)을 골든 기록으로 만든다."""
    result = run.result
    return canonical({
        "scenario": result.scenario,
        "description": scenario.description,
        "outcome": result.outcome,
        "path": result.path,
        "error": result.error,
        "http_detail": result.http_detail,
        "digest": result.digest,
        "safety": {"solve_calls": result.solve_calls,
                   "separate_sessions": result.separate_sessions,
                   "l2_calls_outside_l1": result.l2_calls_outside_l1},
        "queries": result.queries,
        "swallowed_failures": swallowed_failure_lines(run.stdout),
        "logs": run.logs,
        "layers": result.layers,
    })


def write_golden(name: str, record: dict) -> Path:
    """골든 파일을 쓴다(UPDATE_GOLDEN=1 일 때만 부른다)."""
    GOLDEN_DIR.mkdir(parents=True, exist_ok=True)
    path = golden_path(name)
    text = json.dumps(record, ensure_ascii=False, sort_keys=True, indent=1)
    path.write_text(text + "\n", encoding="utf-8")
    return path


def read_golden(name: str) -> dict | None:
    """골든 파일을 읽는다. 없으면 None.

    첫 줄 문지기(D29): `golden/` 이 통째로 비었으면 건너뜀(`REQUIRE_GOLDEN=1` 이면 실패), 수가 기준(16)과 다르면 실패다.
    `GOLDEN` 은 부를 때마다 모듈 전역에서 읽는다(`test_golden_gate` 가 빈 세트로 바꿔 끼워 연결을 확인한다).
    """
    require_golden(GOLDEN)
    path = golden_path(name)
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


# ── 어긋난 자리 ─────────────────────────────────────────────────────────


def _short(value) -> str:
    """보고용 짧은 표기."""
    text = json.dumps(value, ensure_ascii=False, sort_keys=True)
    return text if len(text) <= 80 else text[:77] + "..."


def _diff_dict(expected: dict, actual: dict, path: str, out: list[str]) -> None:
    """dict 두 개의 키별 차이."""
    for key in sorted(set(expected) | set(actual)):
        sub = f"{path}.{key}"
        if key not in actual:
            out.append(f"{sub}: 사라짐 (골든 {_short(expected[key])})")
        elif key not in expected:
            out.append(f"{sub}: 새로 생김 ({_short(actual[key])})")
        else:
            diff_paths(expected[key], actual[key], sub, out)


def _diff_list(expected: list, actual: list, path: str, out: list[str]) -> None:
    """list 두 개의 칸별 차이(길이가 다르면 그것도 적는다)."""
    if len(expected) != len(actual):
        out.append(f"{path}: 길이 {len(expected)} → {len(actual)}")
    for i, (exp, act) in enumerate(zip(expected, actual, strict=False)):
        diff_paths(exp, act, f"{path}[{i}]", out)


def diff_paths(expected, actual, path: str = "$", out: list[str] | None = None) -> list[str]:
    """두 canonical 값의 어긋난 자리를 `경로: 골든 → 지금` 줄로 모은다."""
    out = [] if out is None else out
    if isinstance(expected, dict) and isinstance(actual, dict):
        _diff_dict(expected, actual, path, out)
    elif isinstance(expected, list) and isinstance(actual, list):
        _diff_list(expected, actual, path, out)
    elif expected != actual:
        out.append(f"{path}: {_short(expected)} → {_short(actual)}")
    return out


def mismatch_report(name: str, expected: dict, actual: dict) -> str | None:
    """골든과 다르면 보고문(층·키 경로 목록), 같으면 None."""
    lines = diff_paths(expected, actual)
    if not lines:
        return None
    head = f"골든 {name} 과 다르다 — {len(lines)}곳 (재생성은 UPDATE_GOLDEN=1)"
    shown = lines[:_MAX_DIFF_LINES]
    more = [f"... 외 {len(lines) - len(shown)}곳"] if len(lines) > len(shown) else []
    return "\n".join([head, *shown, *more])
