# golden_model — 모델 지문 골든(EI-1c)

`test_model_fingerprint.py` 가 `model_fingerprint`/`fingerprint_summary` 결과와 대조하는
골든이다(생성: `model_fingerprint.write_golden_model`, `UPDATE_GOLDEN_MODEL=1` 일 때만 덮어쓴다).

> **D29(2026-10-20)** — 이 폴더의 `*.json` 은 커밋하지 않는 로컬 전용이다(README 만 커밋). JSON 이 없으면 골든 비교
> (`test_golden_model`)는 실패하지 않고 D29 사유로 건너뛴다(`golden_gate.require_golden(GOLDEN_MODEL)` · `REQUIRE_GOLDEN=1` 이면 실패).

## ortools 판 기준 — 2026-10-14 확인(9.12.4544 → 9.15.6755 전환)

이 골든 9개(lex 8 + primary 1 `S1-P`)는 **운영 Lambda 가 실제로 쓰는 ortools 판**
(`uv.lock` 고정판)을 기준으로 삼는다 — 로컬 `.venv` 가 9.12.4544 에서 9.15.6755 로 바뀐
뒤 하네스(`model_canon.py`·`capture.py` 의 `model.Proto()` 읽기)를
`tests/engine_input/proto_compat.py` 로 판과 무관하게 고친 다음, **다시 떠보지 않고**
전부 재검증했다 — 9개 전부 digest·summary·rule_counts 가 9.12 때 만든 골든 그대로
일치했다(표현 차이 없음, `UPDATE_GOLDEN_MODEL` 불필요). 즉 이 골든 파일들은 9.12 시절에
떴지만, 9.15 로도 바이트 단위로 재현된다는 사실을 실측으로 확인한 뒤 그대로 둔 것이다.

앞으로 ortools 판이 또 바뀌어 골든이 흔들리면(표현 차이로만 — 모델 자체의 의미 변화가
아님을 먼저 확인한 뒤), 그 판을 새 기준으로 삼아 `UPDATE_GOLDEN_MODEL=1` 로 다시 뜨고
이 문단에 어떤 필드가 왜 바뀌었는지 남긴다.

## 재수신 이력 — 2026-10-20 (origin/dev `4ec454e`)

**다시 쓰지 않았다.** 순수 dev `3ede458` 와 `4ec454e` 에서 같은 하네스로 뜬 모델 지문 9개가 **바이트 동일**했다
(`.omc/aide_docs/v2/engine_input/redev_4ec454e/STATUS_redev_4ec454e.md` §4 — dev 3커밋은 1단계 첫 모델을 바꾸지 않았다.
1bedd4d 는 1단계 **이후** 판정 표시라 이 지문의 대상 밖이다). 비교 모드(플래그 없음)가 이 9개를 그대로 통과해야 한다.
