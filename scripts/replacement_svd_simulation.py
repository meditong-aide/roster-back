from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime
import calendar
import importlib
from typing import Any

import numpy as np
from numpy.typing import NDArray
from sqlalchemy.orm import Session

db_client2 = importlib.import_module("db.client2")
db_models = importlib.import_module("db.models")

SessionLocal = getattr(db_client2, "SessionLocal")
IssuedRosterSnapshot = getattr(db_models, "IssuedRosterSnapshot")
Nurse = getattr(db_models, "Nurse")
NursePairRequest = getattr(db_models, "NursePairRequest")
NurseShiftRequest = getattr(db_models, "NurseShiftRequest")
WantedRequest = getattr(db_models, "WantedRequest")


OFF_CODES = {"-", "O", "OFF", "주"}
WORK_CODES = {"D", "E", "N", "M"}


@dataclass
class SlotScore:
    nurse_id: str
    name: str
    final: float
    base: float
    sim: float
    latent: float
    pref: float
    pair: float
    fairness_penalty: float
    fatigue_penalty: float


@dataclass
class AggScore:
    name: str = ""
    scores: list[float] = field(default_factory=list)
    slot_count: int = 0


def _to_date(value: Any) -> date | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value).date()
        except Exception:
            return None
    return None


def _cosine(a: NDArray[np.float64], b: NDArray[np.float64]) -> float:
    an = np.linalg.norm(a)
    bn = np.linalg.norm(b)
    if an <= 1e-12 or bn <= 1e-12:
        return 0.0
    return float(np.dot(a, b) / (an * bn))


def _rescale01(values: dict[str, float]) -> dict[str, float]:
    if not values:
        return {}
    arr = np.array(list(values.values()), dtype=float)
    lo = float(arr.min())
    hi = float(arr.max())
    if hi - lo < 1e-12:
        return {k: 0.5 for k in values}
    return {k: (v - lo) / (hi - lo) for k, v in values.items()}


def _latest_issued_snapshot(db: Session) -> Any:
    return (
        db.query(IssuedRosterSnapshot)
        .filter(IssuedRosterSnapshot.is_active_issued == True)
        .order_by(IssuedRosterSnapshot.created_at.desc())
        .first()
    )


def _choose_resigned_nurse(roster_nurses: list[dict[str, Any]], effective_day: int) -> tuple[str, list[tuple[int, str]]]:
    target = None
    target_slots: list[tuple[int, str]] = []
    best_count = -1
    for nurse_row in roster_nurses:
        nurse_id = str(nurse_row.get("nurse_id"))
        schedule = nurse_row.get("schedule") or []
        slots = []
        for day_idx, raw_code in enumerate(schedule):
            day = day_idx + 1
            code = str(raw_code).upper()
            if day <= effective_day:
                continue
            if code in WORK_CODES:
                slots.append((day, code))
        if len(slots) > best_count:
            best_count = len(slots)
            target = nurse_id
            target_slots = slots
    if target is None:
        raise RuntimeError("퇴사 시뮬레이션 대상 간호사를 선택할 수 없습니다.")
    return target, target_slots


def _load_latest_wanted_request_ids(db: Session, nurse_ids: list[str], month_key: str) -> dict[str, int]:
    mapping: dict[str, int] = {}
    for nurse_id in nurse_ids:
        row = (
            db.query(WantedRequest)
            .filter(WantedRequest.nurse_id == nurse_id, WantedRequest.month == month_key)
            .order_by(WantedRequest.request_id.desc())
            .first()
        )
        if row is not None:
            mapping[nurse_id] = int(row.request_id)
    return mapping


def _build_preference_tensor(
    db,
    nurse_ids: list[str],
    latest_request_ids: dict[str, int],
    year: int,
    month: int,
    shift_types: list[str],
) -> NDArray[np.float64]:
    n = len(nurse_ids)
    d = calendar.monthrange(year, month)[1]
    s = len(shift_types)
    tensor = np.zeros((n, d, s), dtype=float)
    nurse_index = {nid: idx for idx, nid in enumerate(nurse_ids)}
    shift_index = {code: idx for idx, code in enumerate(shift_types)}

    req_pairs = list(latest_request_ids.items())
    for nurse_id, request_id in req_pairs:
        rows = (
            db.query(NurseShiftRequest)
            .filter(
                NurseShiftRequest.nurse_id == nurse_id,
                NurseShiftRequest.request_id == request_id,
            )
            .all()
        )
        n_idx = nurse_index[nurse_id]
        for r in rows:
            shift_code = str(r.shift or "").upper()
            if shift_code not in shift_index:
                continue
            shift_date = _to_date(r.shift_date)
            if shift_date is None:
                continue
            if shift_date.year != year or shift_date.month != month:
                continue
            day_idx = shift_date.day - 1
            score = float(r.score or 0.0)
            tensor[n_idx, day_idx, shift_index[shift_code]] = score
    return tensor


def _load_pair_map(db: Session, month_key: str, nurse_ids: list[str]) -> dict[tuple[str, str], float]:
    pair_map: dict[tuple[str, str], float] = {}
    rows = (
        db.query(NursePairRequest)
        .filter(NursePairRequest.month == month_key, NursePairRequest.nurse_id.in_(nurse_ids))
        .all()
    )
    for r in rows:
        k = (str(r.nurse_id), str(r.target_id))
        pair_map[k] = float(r.score or 0.0)
    return pair_map


def _slot_candidates(
    roster_schedule_map: dict[str, list[str]],
    nurses_by_id: dict[str, Any],
    resigned_nurse_id: str,
    day: int,
    shift_code: str,
    effective_date: date,
) -> list[str]:
    candidates: list[str] = []
    day_idx = day - 1
    for nurse_id, schedule in roster_schedule_map.items():
        if nurse_id == resigned_nurse_id:
            continue
        n = nurses_by_id.get(nurse_id)
        if n is None:
            continue
        if int(getattr(n, "active", 1) or 1) == 0:
            continue
        join_date = _to_date(getattr(n, "joining_date", None))
        resign_date = _to_date(getattr(n, "resignation_date", None))
        slot_date = date(effective_date.year, effective_date.month, day)
        if join_date and slot_date < join_date:
            continue
        if resign_date and slot_date > resign_date:
            continue
        assigned = str(schedule[day_idx]).upper() if day_idx < len(schedule) else "-"
        if assigned not in OFF_CODES:
            continue
        night_raw = getattr(n, "is_night_nurse", 0)
        if isinstance(night_raw, list):
            night_capable = len(night_raw) > 0
        else:
            try:
                night_capable = int(night_raw or 0) != 0
            except Exception:
                night_capable = bool(night_raw)
        if shift_code == "N" and not night_capable:
            continue
        candidates.append(nurse_id)
    return candidates


def main():
    db = SessionLocal()
    try:
        snap = _latest_issued_snapshot(db)
        if snap is None:
            raise RuntimeError("활성 발행 스냅샷이 없습니다.")

        roster_json = snap.roster_json or {}
        roster_nurses = roster_json.get("nurses") or []
        year = int(roster_json.get("year") or snap.year)
        month = int(roster_json.get("month") or snap.month)
        days_in_month = int(roster_json.get("days_in_month") or calendar.monthrange(year, month)[1])
        if not roster_nurses:
            raise RuntimeError("스냅샷에 roster.nurses 데이터가 없습니다.")

        effective_day = max(2, days_in_month // 2)
        effective_date = date(year, month, effective_day)

        resigned_nurse_id, resigned_slots = _choose_resigned_nurse(roster_nurses, effective_day)
        if not resigned_slots:
            raise RuntimeError("선택된 퇴사 간호사에게 중반 이후 근무 슬롯이 없습니다.")

        roster_schedule_map = {
            str(row.get("nurse_id")): [str(x).upper() for x in (row.get("schedule") or [])]
            for row in roster_nurses
        }
        nurse_ids = list(roster_schedule_map.keys())
        nurses = db.query(Nurse).filter(Nurse.nurse_id.in_(nurse_ids)).all()
        nurses_by_id = {str(n.nurse_id): n for n in nurses}

        shift_types = ["D", "E", "N", "O"]
        month_key = f"{year:04d}-{month:02d}"
        latest_request_ids = _load_latest_wanted_request_ids(db, nurse_ids, month_key)
        pref_tensor = _build_preference_tensor(
            db=db,
            nurse_ids=nurse_ids,
            latest_request_ids=latest_request_ids,
            year=year,
            month=month,
            shift_types=shift_types,
        )
        pair_map = _load_pair_map(db, month_key, nurse_ids)

        roster_tensor = np.zeros_like(pref_tensor)
        for n_idx, nurse_id in enumerate(nurse_ids):
            schedule = roster_schedule_map.get(nurse_id, [])
            for day_idx, code in enumerate(schedule[:days_in_month]):
                c = str(code).upper()
                if c in OFF_CODES:
                    c = "O"
                if c in shift_types:
                    roster_tensor[n_idx, day_idx, shift_types.index(c)] = 1.0

        shift_affinity: dict[str, dict[str, float]] = {}
        for n_idx, nurse_id in enumerate(nurse_ids):
            counts = {k: 0.0 for k in shift_types}
            schedule = roster_schedule_map.get(nurse_id, [])
            for code in schedule:
                c = str(code).upper()
                if c in OFF_CODES:
                    c = "O"
                if c in counts:
                    counts[c] += 1.0
            total = sum(counts.values())
            if total <= 0:
                shift_affinity[nurse_id] = {k: 0.0 for k in shift_types}
            else:
                shift_affinity[nurse_id] = {k: counts[k] / total for k in shift_types}

        n = len(nurse_ids)
        d = days_in_month
        s = len(shift_types)
        feature_tensor = pref_tensor + 0.7 * roster_tensor
        flat_pref = feature_tensor.reshape(n, d * s)
        flat_centered = flat_pref - flat_pref.mean(axis=1, keepdims=True)
        max_rank = max(1, min(8, n - 1, d * s - 1))
        u, sigma, vt = np.linalg.svd(flat_centered, full_matrices=False)
        u_k = u[:, :max_rank]
        sigma_k = sigma[:max_rank]
        vt_k = vt[:max_rank, :]
        nurse_emb = u_k * sigma_k
        slot_emb = vt_k.T

        nurse_idx = {nid: idx for idx, nid in enumerate(nurse_ids)}
        shift_idx = {code: idx for idx, code in enumerate(shift_types)}
        resigned_idx = nurse_idx[resigned_nurse_id]

        aggregate: defaultdict[str, AggScore] = defaultdict(AggScore)

        print("=== Replacement Recommendation Simulation (Base + SVD) ===")
        print(f"snapshot_id={snap.snapshot_id}, schedule_id={snap.schedule_id}, ym={year}-{month:02d}")
        print(f"simulated_resignation nurse_id={resigned_nurse_id}, effective_day={effective_day}")
        print(f"affected_work_slots={len(resigned_slots)}")

        for slot_order, (day, shift_code) in enumerate(resigned_slots[:10], start=1):
            if shift_code not in shift_idx:
                continue
            cands = _slot_candidates(
                roster_schedule_map=roster_schedule_map,
                nurses_by_id=nurses_by_id,
                resigned_nurse_id=resigned_nurse_id,
                day=day,
                shift_code=shift_code,
                effective_date=effective_date,
            )
            if not cands:
                print(f"slot#{slot_order} day={day} shift={shift_code}: eligible=0")
                continue

            pref_raw: dict[str, float] = {}
            pair_raw: dict[str, float] = {}
            fair_pen_raw: dict[str, float] = {}
            fatigue_pen_raw: dict[str, float] = {}
            sim_raw: dict[str, float] = {}
            latent_raw: dict[str, float] = {}

            day_idx = day - 1
            slot_j = day_idx * s + shift_idx[shift_code]

            for cand_id in cands:
                c_idx = nurse_idx[cand_id]
                wanted_pref = float(pref_tensor[c_idx, day_idx, shift_idx[shift_code]])
                if wanted_pref > 0:
                    pref_raw[cand_id] = wanted_pref
                else:
                    pref_raw[cand_id] = float(shift_affinity.get(cand_id, {}).get(shift_code, 0.0))

                p1 = pair_map.get((resigned_nurse_id, cand_id), 0.0)
                p2 = pair_map.get((cand_id, resigned_nurse_id), 0.0)
                pair_raw[cand_id] = (p1 + p2) / 2.0

                schedule = roster_schedule_map[cand_id]
                same_shift_load = sum(
                    1
                    for dd in range(effective_day - 1, min(len(schedule), days_in_month))
                    if schedule[dd] == shift_code
                )
                fair_pen_raw[cand_id] = float(same_shift_load)

                fatigue = 0.0
                prev_code = schedule[day_idx - 1] if day_idx - 1 >= 0 else "-"
                if prev_code == "N" and shift_code in {"D", "E"}:
                    fatigue += 1.0
                if prev_code == shift_code and shift_code == "N":
                    fatigue += 0.8
                fatigue_pen_raw[cand_id] = fatigue

                sim_raw[cand_id] = _cosine(nurse_emb[c_idx], nurse_emb[resigned_idx])
                latent_raw[cand_id] = float(np.dot(nurse_emb[c_idx], slot_emb[slot_j]))

            pref_n = _rescale01(pref_raw)
            pair_n = _rescale01(pair_raw)
            fair_pen_n = _rescale01(fair_pen_raw)
            fatigue_pen_n = _rescale01(fatigue_pen_raw)
            sim_n = _rescale01(sim_raw)
            latent_n = _rescale01(latent_raw)

            rows: list[SlotScore] = []
            for cand_id in cands:
                base = (
                    0.70 * pref_n[cand_id]
                    + 0.30 * pair_n[cand_id]
                    - 0.25 * fair_pen_n[cand_id]
                    - 0.15 * fatigue_pen_n[cand_id]
                )
                final = 0.55 * base + 0.25 * sim_n[cand_id] + 0.20 * latent_n[cand_id]
                nurse_obj = nurses_by_id.get(cand_id)
                rows.append(
                    SlotScore(
                        nurse_id=cand_id,
                        name=getattr(nurse_obj, "name", cand_id),
                        final=final,
                        base=base,
                        sim=sim_n[cand_id],
                        latent=latent_n[cand_id],
                        pref=pref_n[cand_id],
                        pair=pair_n[cand_id],
                        fairness_penalty=fair_pen_n[cand_id],
                        fatigue_penalty=fatigue_pen_n[cand_id],
                    )
                )

            rows.sort(key=lambda x: x.final, reverse=True)
            top3 = rows[:3]
            print(f"slot#{slot_order} day={day:02d} shift={shift_code} eligible={len(cands)}")
            for r in top3:
                print(
                    "  - "
                    f"{r.name}({r.nurse_id}) final={r.final:.3f} "
                    f"[base={r.base:.3f}, sim={r.sim:.3f}, latent={r.latent:.3f}, "
                    f"pref={r.pref:.3f}, pair={r.pair:.3f}, "
                    f"fair_pen={r.fairness_penalty:.3f}, fatigue_pen={r.fatigue_penalty:.3f}]"
                )
                aggregate[r.nurse_id].scores.append(r.final)
                aggregate[r.nurse_id].slot_count += 1
                aggregate[r.nurse_id].name = r.name

        summary = []
        for nurse_id, info in aggregate.items():
            avg = float(np.mean(info.scores)) if info.scores else 0.0
            summary.append((nurse_id, info.name, avg, info.slot_count))
        summary.sort(key=lambda x: (x[2], x[3]), reverse=True)

        print("\n=== Aggregate Top Candidates (across first 10 affected slots) ===")
        for nurse_id, name, avg, cnt in summary[:10]:
            print(f"- {name}({nurse_id}) avg_final={avg:.3f}, top3_covered_slots={cnt}")

    finally:
        db.close()


if __name__ == "__main__":
    main()
