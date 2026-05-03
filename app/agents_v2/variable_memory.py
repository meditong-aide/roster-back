"""Variable Memory — Routine step 간 파라미터 전달.

Routine paper (Zeng et al., 2025) 핵심 개념:
step 1 결과를 step 2 입력으로 자동 연결하여 LLM의 중간 결과 추출 부담 제거.

사용:
- store(): skill 실행 결과에서 재사용 가능한 키를 자동 추출/저장
- inject(): args에서 $vm.{key} 참조를 실제 값으로 치환
- get(): 특정 키 값 조회
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


# VM에 자동 저장되는 핵심 키 목록
_AUTO_EXTRACT_KEYS = frozenset({
    "schedule_id",
    "nurse_ids",
    "nurse_id",
    "shift_codes",
    "shift_id",
    "wanted_request_ids",
    "entries",
    "entry_id",
    "current_shift",
    "preview",
    "candidates",
    "report",
})


@dataclass
class VariableMemory:
    """Lightweight key-value store for inter-step parameter passing."""

    _store: dict[str, Any] = field(default_factory=dict)

    def store(self, skill_name: str, result: Any) -> None:
        """Extract reusable values from skill result and save to VM."""
        # Store full result under last_{skill_name} for flexible access
        # This works for both dict and list results (e.g. schedule entries)
        self._store[f"last_{skill_name}"] = result

        if isinstance(result, dict):
            # Auto-extract known keys from dict results
            for key in _AUTO_EXTRACT_KEYS:
                if key in result:
                    self._store[key] = result[key]
        elif isinstance(result, list) and result:
            # For list results: store count and extract IDs if uniform structure
            self._store[f"last_{skill_name}_count"] = len(result)
            if isinstance(result[0], dict):
                # Extract common keys from first item for downstream reference
                for key in _AUTO_EXTRACT_KEYS:
                    values = [r[key] for r in result if key in r]
                    if values:
                        self._store[key] = values

    def inject(self, args: dict) -> dict:
        """Replace $vm.{key} references in args with actual values."""
        if not self._store:
            return args

        injected = {}
        for k, v in args.items():
            if isinstance(v, str) and v.startswith("$vm."):
                vm_key = v[4:]
                injected[k] = self._store.get(vm_key, v)
            else:
                injected[k] = v
        return injected

    def get(self, key: str, default: Any = None) -> Any:
        return self._store.get(key, default)

    def clear(self) -> None:
        self._store.clear()

    def to_dict(self) -> dict:
        """Snapshot for debugging/trace."""
        return dict(self._store)
