import json


def normalize_allowed_shift_codes(raw, use_mid: bool = False) -> set[str]:
    if raw is None:
        return set()
    if isinstance(raw, str):
        txt = raw.strip()
        if not txt:
            return set()
        parsed = None
        if txt.startswith("[") and txt.endswith("]"):
            try:
                parsed = json.loads(txt)
            except Exception:
                parsed = None
        if parsed is None:
            raw = [p.strip() for p in txt.split(",") if p.strip()]
        else:
            raw = parsed
    if isinstance(raw, (tuple, set)):
        raw = list(raw)
    if not isinstance(raw, list):
        return set()

    valid = {"D", "E", "N"}
    if bool(use_mid):
        valid.add("M")
    out: set[str] = set()
    for x in raw:
        code = str(x).strip().upper()
        if code in valid:
            out.add(code)
    return out


def is_n_only_profile(raw, use_mid: bool = False) -> bool:
    return normalize_allowed_shift_codes(raw, use_mid=use_mid) == {"N"}


def is_code_blocked_by_profile(raw, code: str, use_mid: bool = False) -> bool:
    allowed = normalize_allowed_shift_codes(raw, use_mid=use_mid)
    if not allowed:
        return False
    return str(code).strip().upper() not in allowed
