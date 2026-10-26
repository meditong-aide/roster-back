"""CP-SAT 제약 이름표 래퍼(설계서 §2.3 · EI-3).

엔진이 거는 제약마다 RULES(`services.engine_contract.rules`)의 id 를 붙인다.
`m.Add(expr)` 를 `hard_add(m, "rule_id", expr)` 로 바꿔 쓰면 같은 Constraint 가 돌아오고
`.OnlyEnforceIf(...)` 연쇄도 그대로 된다 — 모델은 한 비트도 바뀌지 않는다.

  - hard_*: 해를 실제로 제한하는 제약(하드락 포함). id 는 RULES 의 HARD 계열.
  - aux_*: 보조 변수 정의·목적식용 식. tag 는 RULES 의 AUX.
  - *_call: `Add` 가 아닌 제약 메서드(AddExactlyOne·AddMinEquality 등)를 이름으로 부른다.

런타임 검증·기록은 하지 않는다(동작·속도 불변). id 가 RULES 에 있는지, 이름표 없는 제약이
남았는지는 정적 감사(tests/engine_contract/test_rule_audit.py — A1)가 본다.
"""

from __future__ import annotations


def hard_add(m, rule_id, expr):
    """하드 제약 `m.Add(expr)` — rule_id 는 이름표일 뿐 호출에 쓰지 않는다."""
    return m.Add(expr)


def aux_add(m, tag, expr):
    """보조 식 `m.Add(expr)` — tag 는 이름표일 뿐 호출에 쓰지 않는다."""
    return m.Add(expr)


def hard_call(m, rule_id, method, *args, **kwargs):
    """하드 제약 `m.<method>(*args, **kwargs)`."""
    return getattr(m, method)(*args, **kwargs)


def aux_call(m, tag, method, *args, **kwargs):
    """보조 식 `m.<method>(*args, **kwargs)`."""
    return getattr(m, method)(*args, **kwargs)
