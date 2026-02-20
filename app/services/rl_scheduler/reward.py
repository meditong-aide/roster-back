from __future__ import annotations

from services.ai_voucher_metrics import VIOLATION_WEIGHTS


class LNSRewardFn:
    def __init__(self, alpha: float = 0.1):
        self.alpha = float(alpha)

    def _c_hard(self, roster_system) -> float:
        counts: dict[str, int] = {}
        for v in roster_system._find_violations():
            t = str(v.get("type", ""))
            counts[t] = counts.get(t, 0) + 1
        return float(sum(VIOLATION_WEIGHTS.get(t, 1) * c for t, c in counts.items()))

    def score(self, roster_system) -> float:
        c_hard = self._c_hard(roster_system)
        return float(pow(2.718281828, -self.alpha * c_hard))

    def reward(self, before: float, after: float, *, ok: bool, improved: bool | None = None) -> float:
        delta = float(after - before)
        if not ok:
            delta -= 0.1
        if improved is True:
            delta += 0.05
        if improved is False:
            delta -= 0.02
        if delta > 1.0:
            return 1.0
        if delta < -1.0:
            return -1.0
        return float(delta)
