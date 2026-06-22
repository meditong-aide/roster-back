# Live Router Eval Baselines

`tests/agent_qa/live_router_eval.py` 의 실행 결과 영구 보존.

파일명 컨벤션:
```
live_router_eval_<YYYY-MM-DD>_<provider>_<label>.json
```

- `provider`: openai / anthropic / deterministic
- `label`: before / after / 자유 라벨 (예: 'wave3_only', 'prompt_v2')

새 측정 후 추가:
```bash
python tests/agent_qa/live_router_eval.py \
  --provider openai --output tests/agent_qa/baselines/live_router_eval_$(date +%F)_openai_after.json
```

비교는 단순 diff 가능 — 각 JSON 의 `rows[].hit` / `by_tool` / `avg_latency_s`.
