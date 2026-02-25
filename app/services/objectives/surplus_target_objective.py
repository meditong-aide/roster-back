from __future__ import annotations


def _pick_target_codes(mode: str, work_codes: list[str]) -> list[str]:
    mode = (mode or "").strip().lower()
    if mode == "d_only":
        return [c for c in ["D"] if c in work_codes]
    if mode == "de_balanced":
        return [c for c in ["D", "E"] if c in work_codes]
    return work_codes[:]


def append_surplus_target_direction_terms(
    *,
    m,
    cfg,
    over_vars_by_day: dict,
    obj: list,
    N: int,
    prefix: str,
) -> None:
    if not bool(getattr(cfg, "surplus_target_enable", True)):
        return

    strategy = str(getattr(cfg, "surplus_target_strategy", "huber") or "huber").strip().lower()
    mode = str(getattr(cfg, "surplus_direction_mode", "de_balanced") or "de_balanced").strip().lower()

    w_base = int(getattr(cfg, "surplus_target_weight", 0) or 0)
    if w_base <= 0:
        w_base = int(round(int(getattr(cfg, "oversupply_equalize_weight", 120) or 120) * 1.2))

    w_non_target = max(1, int(round(w_base * 1.3)))
    w_target = max(1, int(round(w_base * 1.0)))
    w_hi = max(1, int(round(w_base * 0.7)))
    band = max(1, int(getattr(cfg, "surplus_target_band", 1) or 1))
    hinge = max(1, int(getattr(cfg, "surplus_target_hinge", 2) or 2))

    for d, code2ov in over_vars_by_day.items():
        work_codes = [code for code in code2ov.keys() if code in cfg.daily_shift_requirements.keys()]
        if not work_codes:
            continue

        target_codes = _pick_target_codes(mode, work_codes)
        if not target_codes:
            target_codes = work_codes[:]

        non_target_codes = [c for c in work_codes if c not in target_codes]
        for code in non_target_codes:
            obj.append(-w_non_target * code2ov[code])

        if len(target_codes) == 1:
            continue

        k = len(target_codes)
        tday = sum(code2ov[c] for c in target_codes)
        for code in target_codes:
            ov = code2ov[code]
            if strategy == "l1":
                diff = m.NewIntVar(0, k * N, f"{prefix}_st_l1_{d}_{code}")
                m.Add(diff >= k * ov - tday)
                m.Add(diff >= tday - k * ov)
                obj.append(-w_target * diff)
            elif strategy == "banded":
                diff = m.NewIntVar(0, k * N, f"{prefix}_st_bd_{d}_{code}")
                m.Add(diff >= k * ov - tday)
                m.Add(diff >= tday - k * ov)
                slack = m.NewIntVar(0, k * N, f"{prefix}_st_bd_slk_{d}_{code}")
                m.Add(slack >= diff - band)
                obj.append(-w_target * slack)
            else:
                diff = m.NewIntVar(0, k * N, f"{prefix}_st_hb_{d}_{code}")
                m.Add(diff >= k * ov - tday)
                m.Add(diff >= tday - k * ov)
                hi = m.NewIntVar(0, k * N, f"{prefix}_st_hb_hi_{d}_{code}")
                m.Add(hi >= diff - hinge)
                obj.append(-w_target * diff)
                obj.append(-w_hi * hi)
