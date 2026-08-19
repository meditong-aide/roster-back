"""수면OFF 누적 회차 백필 — 2026-07 부터 시작.

★ seq_at_end / pending_sleep 은 건드리지 않는다(엑셀 연번 기준으로 이미 백필됨).
  sleep_off_count / sleep_off_seq 만 **DB 확정본의 '수면' 코드**로 채운다 —
  엑셀 파서보다 정확하고, 부여 사실은 근무표가 진실이다.

★ 누적 기준은 데이터가 있는 **2026-07** 부터다. 그 이전 이력은 0 에서 시작한다.
"""
import sys
sys.path.insert(0, "app")
from db.client2 import SessionLocal
from db.models import NurseNightCycle
from sqlalchemy import text

MONTHS = [(2026, 7), (2026, 8)]
db = SessionLocal()

# 그룹별 수면OFF 코드 (없으면 그 그룹은 0)
sleep_code = {str(r[0]): str(r[1]) for r in db.execute(text(
    "SELECT group_id, shift_id FROM shifts WHERE sleep_off_target = 1")).fetchall()}

carry: dict[tuple[str, str], int] = {}   # (nurse_id, group_id) -> 누적
touched = 0
for y, m in MONTHS:
    rows = db.execute(text("""
        SELECT e.nurse_id, sc.group_id, e.shift_id
        FROM schedule_entries e
        JOIN schedules sc ON sc.schedule_id = e.schedule_id
        JOIN groups g ON g.group_id COLLATE DATABASE_DEFAULT = sc.group_id COLLATE DATABASE_DEFAULT
        WHERE g.office_id='102243' AND sc.year=:y AND sc.month=:m"""), {"y": y, "m": m}).fetchall()
    cnt: dict[tuple[str, str], int] = {}
    for nid, gid, code in rows:
        k = (str(nid), str(gid))
        cnt.setdefault(k, 0)
        if sleep_code.get(str(gid)) and str(code or "").strip() == sleep_code[str(gid)]:
            cnt[k] += 1
    for k, c in cnt.items():
        carry[k] = carry.get(k, 0) + c
        row = (db.query(NurseNightCycle)
                 .filter(NurseNightCycle.nurse_id == k[0], NurseNightCycle.group_id == k[1],
                         NurseNightCycle.year == y, NurseNightCycle.month == m).first())
        if row is None:
            continue          # 앵커 행이 없는 사람은 건너뛴다(연번 미상)
        row.sleep_off_count = c
        row.sleep_off_seq = carry[k]
        touched += 1
    print(f"  {y}-{m:02d}: 대상 {len(cnt)}명 · 반영 {touched}행 누적")
db.commit()

tot = db.query(NurseNightCycle).filter(NurseNightCycle.sleep_off_seq != None).count()  # noqa: E711
print(f"\n채워진 행 {tot} · 누적>0 "
      f"{db.query(NurseNightCycle).filter(NurseNightCycle.sleep_off_seq > 0).count()}")
db.close()
