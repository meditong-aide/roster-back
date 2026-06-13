"""Layer A / B — agent 입출력 보안 검열.

- input_classifier: agent.run 진입 직전 사용자 발화 분류 (SAFE/SUSPICIOUS/MALICIOUS)
- output_check: 응답 반환 직전 누출 검사 + 마스킹

설계: 결정론적 정규식 fast-path. CLAUDE.md 의 "intent 파싱에 regex 금지" 원칙은
사용자 의도 해석에 한정 — 보안 경계의 알려진 적대적 패턴 매칭에는 regex 가 적합.
"""

from .input_classifier import (
    InputVerdict,
    classify_input,
)
from .output_check import (
    OutputCheckResult,
    check_output,
)

__all__ = [
    "InputVerdict",
    "classify_input",
    "OutputCheckResult",
    "check_output",
]
