"""하드락 정책 SSOT — 설계서 EI-7(dev-4·dev-4e 흡수).

`hard_lock.py` 가 CLAUDE.md 하드락 표 #1~#8 판정 4종을 낸다. 숫자·키·규칙표는
`services.engine_contract.settings`(SettingSpec.hard_lock) · `services.engine_contract.rules`
(RuleSpec.hard_lock_rule_nos) · `services.engine_contract.nurse_attrs`(NurseAttrSpec.hard_lock_*)
에서만 읽는다 — 이 패키지에 숫자를 다시 적지 않는다(정책 두 벌 금지, R3-06).
"""
