"""임옥희 N=0 재현 — n_range 목적함수 무력화 확인.

★ 고치기 전에 기준값을 잡는다. 검증 셋(판정자 지정):
   ① n_range 값이 7 미만  ② 임옥희 N 이 4 안팎  ③ 하드락 8종 여전히 0
   + 다른 range 패스 비회귀  + 제외 인원 수 로깅
"""
import contextlib, io, os, re, sys
sys.path.insert(0, "app")
from sqlalchemy import text            # noqa: E402
from db.client2 import SessionLocal    # noqa: E402
from schemas.roster_schema import RosterRequest  # noqa: E402
import services.roster_create_service as RCS     # noqa: E402
import importlib.util as _iu           # noqa: E402

_sp = _iu.spec_from_file_location("va", "tools/harness/verify_all.py")
va = _iu.module_from_spec(_sp); _sp.loader.exec_module(va)

GID = os.getenv("G", "10259891d93c")
Y, M = int(os.getenv("Y", "2026")), int(os.getenv("M", "10"))

db = SessionLocal()
try:
    user = va.make_user(db, GID)
    # ★ raw SQL 금지 — `allowed_shifts` 의 실제 컬럼명은 **`is_night_nurse`** 다
    #   (models.py:86 `name="is_night_nurse", key="allowed_shifts"`). ORM 이 매핑한다.
    from db.models import Nurse
    rows = [(n.nurse_id, n.name, getattr(n, "allowed_shifts", None),
             getattr(n, "fixed_shift", None), getattr(n, "is_weekend_off", None))
            for n in db.query(Nurse).filter(Nurse.group_id == GID).all()]
finally:
    db.close()
print(f"간호사 {len(rows)}명")

buf, db2 = io.StringIO(), SessionLocal()
try:
    with contextlib.redirect_stdout(buf):
        RCS.generate_roster_service(RosterRequest(year=Y, month=M, group_id=GID), user, db2)
finally:
    db2.close()
log = buf.getvalue()
pathlib_out = __import__("pathlib").Path("tools/harness/runs/repro_last.log")
pathlib_out.write_text(log, encoding="utf-8")

# lex 4-pass(n_range) 동결값
for m in re.finditer(r"lex(\d+):(\w+).*?OPTIMAL.*?value=(-?\d+)", log):
    print(f"  lex{m.group(1)}:{m.group(2):<10} = {m.group(3)}")
for pat, label in ((r"최소 커버리지 부족: (-?\d+), 과잉: (-?\d+)", "stage1"),
                   (r"폴백 완료: 커버리지부족=(-?\d+), 안전위반합=(-?\d+)", "최종")):
    mm = list(re.finditer(pat, log))
    if mm:
        print(f"  {label}: {mm[-1].groups()}")
# 간호사별 N 수
db3 = SessionLocal()
try:
    sid = db3.execute(text("SELECT TOP 1 schedule_id FROM schedules WHERE group_id=:g "
                           "AND year=:y AND month=:m AND dropped=0 ORDER BY created_at DESC"),
                      {"g": GID, "y": Y, "m": M}).scalar()
    # ★ 테이블명은 `schedule_entries` 다(models.py:502). `schedule_cells` 는 없다.
    # ★ `schedule_entries` 는 `shift_code` 가 아니라 **`shift_id`** 다(models.py:506).
    cells = db3.execute(text("SELECT nurse_id, shift_id FROM schedule_entries "
                             "WHERE schedule_id=:s"), {"s": sid}).fetchall()
finally:
    db3.close()
from collections import defaultdict
nc = defaultdict(lambda: defaultdict(int))
for nid, code in cells:
    nc[str(nid)][str(code)] += 1
name_of = {str(r[0]): r[1] for r in rows}
ns = [(name_of.get(k, k), v.get("N", 0)) for k, v in nc.items()]
ns.sort(key=lambda x: x[1])
print(f"\n  N 배정 (schedule_id={sid}, {len(ns)}명)")
for nm, c in ns:
    mark = " ★" if c == 0 else ""
    print(f"    {nm:<10} N={c}{mark}")
vals = [c for _, c in ns]
print(f"  → N range = {max(vals) - min(vals)} (max {max(vals)} - min {min(vals)})")
