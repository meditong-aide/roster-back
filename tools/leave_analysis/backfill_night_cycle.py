"""8월 앵커 백필 — night_cycle_seed.json → nurse_night_cycle (2026-08 스냅샷)."""
import sys, json
sys.path.insert(0, "app")
from db.client2 import SessionLocal
from db.models import NurseNightCycle

Y, M = 2026, 8
db = SessionLocal()
seed = json.load(open("tools/leave_analysis/night_cycle_seed.json", encoding="utf-8"))
before = db.query(NurseNightCycle).count()

ins = upd = 0
for s in seed:
    row = (db.query(NurseNightCycle)
             .filter(NurseNightCycle.nurse_id == s["nurse_id"],
                     NurseNightCycle.group_id == s["gid"],
                     NurseNightCycle.year == Y, NurseNightCycle.month == M)
             .first())
    if row is None:
        db.add(NurseNightCycle(
            nurse_id=s["nurse_id"], group_id=s["gid"], year=Y, month=M,
            seq_at_end=s["seq_at_end"], pending_sleep=int(s["pending"] or 0)))
        ins += 1
    else:
        row.seq_at_end = s["seq_at_end"]
        row.pending_sleep = int(s["pending"] or 0)
        upd += 1
db.commit()

after = db.query(NurseNightCycle).count()
pend = db.query(NurseNightCycle).filter(NurseNightCycle.pending_sleep > 0).count()
seq15 = db.query(NurseNightCycle).filter(NurseNightCycle.seq_at_end >= 15).count()
print(f"백필: INSERT {ins} · UPDATE {upd} · 행수 {before} → {after}")
print(f"검증: pending>0 {pend}명 · seq_at_end>=15 {seq15}명 · seed {len(seed)}명")
assert after == len(seed), f"행수 불일치 {after} != {len(seed)}"
print("OK")
db.close()
