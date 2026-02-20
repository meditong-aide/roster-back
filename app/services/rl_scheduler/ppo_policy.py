from __future__ import annotations

import json
import random
from pathlib import Path

from services.rl_scheduler.action_mapper import LNSActionMapper
from services.rl_scheduler.reward import LNSRewardFn
from services.rl_scheduler.state_builder import build_lns_state


class PPOPolicyAdapter:
    def __init__(self, action_space_size: int, model_path: str | None = None, seed: int | None = None):
        self.action_space_size = int(action_space_size)
        self.rng = random.Random(seed)
        self.priors = [1.0 for _ in range(self.action_space_size)]
        self.model_path = model_path
        self.load_status = "default"
        self.load_error: str | None = None
        if model_path:
            self._load(model_path)

    def _load(self, model_path: str) -> None:
        p = Path(model_path)
        if not p.exists() or not p.is_file():
            self.load_status = "missing"
            self.load_error = "model file not found"
            return
        if p.suffix.lower() != ".json":
            self.load_status = "invalid"
            self.load_error = "model file must be .json"
            return
        try:
            raw = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            self.load_status = "invalid"
            self.load_error = "model json parse failed"
            return
        if not isinstance(raw, dict):
            self.load_status = "invalid"
            self.load_error = "model root must be object"
            return
        fmt = str(raw.get("format_version") or "")
        policy_type = str(raw.get("policy_type") or "")
        model_action_space = raw.get("action_space_size")
        priors = raw.get("action_priors")
        if fmt != "lns_ppo_v1":
            self.load_status = "invalid"
            self.load_error = "unsupported format_version"
            return
        if policy_type != "categorical_priors":
            self.load_status = "invalid"
            self.load_error = "unsupported policy_type"
            return
        if not isinstance(model_action_space, int) or model_action_space != self.action_space_size:
            self.load_status = "invalid"
            self.load_error = "action_space_size mismatch"
            return
        if not isinstance(priors, list):
            self.load_status = "invalid"
            self.load_error = "action_priors must be list"
            return
        vals = []
        for v in priors[: self.action_space_size]:
            try:
                vals.append(max(0.0, float(v)))
            except Exception:
                vals.append(0.0)
        if vals and sum(vals) > 0:
            while len(vals) < self.action_space_size:
                vals.append(1.0)
            self.priors = vals
            self.load_status = "loaded"
            self.load_error = None
            return
        self.load_status = "invalid"
        self.load_error = "action_priors sum must be > 0"

    def act(self, obs_vector: list[float]) -> tuple[int, dict]:
        population = list(range(self.action_space_size))
        action = self.rng.choices(population, weights=self.priors, k=1)[0]
        p = self.priors[action] / sum(self.priors) if sum(self.priors) > 0 else (1.0 / self.action_space_size)
        return int(action), {
            "logprob": 0.0 if p <= 0 else float(__import__("math").log(p)),
            "source": "ppo_adapter",
            "load_status": self.load_status,
            "load_error": self.load_error,
        }

    def update(self, transition: dict) -> None:
        return None


class PPONeighborhoodPolicy:
    def __init__(self, N: int, D: int, seed: int | None = None, model_path: str | None = None):
        self.N = int(N)
        self.D = int(D)
        self.eps = None
        self.mapper = LNSActionMapper()
        self.reward_fn = LNSRewardFn()
        self.adapter = PPOPolicyAdapter(self.mapper.action_space(), model_path=model_path, seed=seed)
        self.last_select_meta: dict = {}
        self.last_reward: float | None = None
        self._pending: dict | None = None

    def select(self, k_n: int = 4, k_d: int = 7, roster_system=None, lns_metrics: dict | None = None):
        if roster_system is None:
            n_sel = random.sample(range(self.N), k=min(k_n, self.N))
            d_sel = random.sample(range(self.D), k=min(k_d, self.D))
            self.last_select_meta = {"operator": "random", "action_id": -1, "source": "fallback"}
            self._pending = None
            return n_sel, d_sel, False

        state = build_lns_state(roster_system, lns_metrics=lns_metrics)
        action_id, act_meta = self.adapter.act(state.vector)
        n_sel, d_sel, map_meta = self.mapper.to_neighborhood(
            roster_system,
            state,
            action_id,
            k_n=k_n,
            k_d=k_d,
        )
        before_score = self.reward_fn.score(roster_system)
        self._pending = {
            "obs": state.vector,
            "action_id": action_id,
            "before_score": before_score,
            "operator": map_meta.get("operator", "random"),
        }
        self.last_select_meta = {
            "action_id": int(action_id),
            "operator": map_meta.get("operator", "random"),
            "source": act_meta.get("source"),
            "logprob": act_meta.get("logprob"),
            "state_dim": len(state.vector),
        }
        return n_sel, d_sel, False

    def update(self, ok: bool, n_sel, d_sel, roster_system=None, improved: bool | None = None):
        if self._pending is None or roster_system is None:
            self.last_reward = None
            return None
        after_score = self.reward_fn.score(roster_system)
        reward = self.reward_fn.reward(
            self._pending["before_score"],
            after_score,
            ok=ok,
            improved=improved,
        )
        self.last_reward = float(reward)
        self.adapter.update(
            {
                "obs": self._pending["obs"],
                "action_id": self._pending["action_id"],
                "reward": reward,
                "ok": bool(ok),
                "improved": improved,
                "before_score": self._pending["before_score"],
                "after_score": after_score,
            }
        )
        self._pending = None
        return None
