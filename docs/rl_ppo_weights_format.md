# RL PPO Weights Format

`LNS_PPO_MODEL_PATH`는 아래 JSON 포맷만 허용한다.

```json
{
  "format_version": "lns_ppo_v1",
  "policy_type": "categorical_priors",
  "action_space_size": 4,
  "action_priors": [1.0, 2.0, 1.5, 0.8]
}
```

- `format_version`: 현재 `lns_ppo_v1`만 허용
- `policy_type`: 현재 `categorical_priors`만 허용
- `action_space_size`: 코드의 action space와 정확히 일치해야 함
- `action_priors`: 길이 `action_space_size`인 0 이상 수치 배열, 합계는 0보다 커야 함

유효하지 않으면 PPO 어댑터는 기본 priors(균등)로 fallback한다.
