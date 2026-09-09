"""8월 앵커(seq_at_end / pending) 재산출 — 괄호 표기 수정 반영본.

leave_scan.json(8월 레코드) + DB 간호사 매칭 → night_cycle_seed.json
"""
import sys, json, re, collections

def norm_name(v: str) -> str:
    """이름 정규화. `황상은(파트장)` → `황상은` · `이한솔b` ↔ `이한솔B` 흡수."""
    s2 = re.sub(r"\s+", "", re.sub(r"\([^)]*\)", "", str(v or ""))).upper()
    return re.sub(r"[AB]$", "", s2) if len(s2) > 2 else s2
sys.path.insert(0, "app")
from db.client2 import SessionLocal
from sqlalchemy import text

SCAN = "/private/tmp/claude-501/-Users-hjj-Downloads-meditong/073f7752-4e9d-4ef0-b3f6-29a4f022cfa0/scratchpad/rs/leave_scan.json"
recs = [r for r in json.load(open(SCAN, encoding="utf-8")) if r.get("month") == 8]
# 같은 인원이 여러 시트에 있으면 nseq 가 더 많은 쪽 채택(중복 시트 제거)
best = {}
for r in recs:
    k = (r["file"], r["name"])
    if k not in best or len(r.get("nseq") or {}) > len(best[k].get("nseq") or {}):
        best[k] = r
recs = list(best.values())

db = SessionLocal()
# 이름 → (nurse_id, group_id, group_name)  ※ 동명이인은 그룹명 힌트로 가른다
rows = db.execute(text("""
    SELECT n.nurse_id, n.name, n.group_id, g.group_name
    FROM nurses n JOIN groups g ON g.group_id COLLATE DATABASE_DEFAULT = n.group_id COLLATE DATABASE_DEFAULT
    WHERE g.office_id='102243' AND n.active=1""")).fetchall()
by_name = collections.defaultdict(list)
for nid, nm, gid, gname in rows:
    by_name[norm_name(nm)].append((str(nid), str(gid), str(gname)))

# ★ DB 8월 확정본의 간호사별 N 개수 — 정본 판별 + 동명이인 매칭의 레퍼런스.
_dbn = {str(r[0]): int(r[1]) for r in db.execute(text("""
    SELECT e.nurse_id, COUNT(*) FROM schedule_entries e
    JOIN schedules sc ON sc.schedule_id = e.schedule_id
    JOIN groups g ON g.group_id COLLATE DATABASE_DEFAULT = sc.group_id COLLATE DATABASE_DEFAULT
    WHERE g.office_id='102243' AND sc.year=2026 AND sc.month=8 AND e.shift_id = N'N'
    GROUP BY e.nurse_id""")).fetchall()}


def match(rec):
    """엑셀 레코드 → (nurse_id, group_id, group_name).

    ★ 동명이인은 **DB 의 8월 N 개수**로 가른다. 병동명 힌트만으로는 응급실-RN 과
      응급실-AN 을 구분할 수 없어 첫 후보로 쏠린다(실측: 김지영·김민영 오매칭).
    """
    cand = by_name.get(norm_name(rec["name"]), [])
    if not cand:
        return None
    if len(cand) == 1:
        return cand[0]
    want = int(rec.get("n_total") or 0)
    exact = [c for c in cand if _dbn.get(c[0], 0) == want]
    if len(exact) == 1:
        return exact[0]
    ward = str(rec.get("file", "")) + str(rec.get("sheet", ""))
    for nid, gid, gname in cand:
        core = gname.replace("병동", "").replace("-RN", "").replace("-AN", "")
        if core and core in ward:
            return (nid, gid, gname)
    return exact[0] if exact else cand[0]

seed, unmatched = [], []
for r in recs:
    m = match(r)
    if not m:
        unmatched.append(f"{r.get('sheet','')}/{r['name']}"); continue
    nid, gid, gname = m
    nseq = {int(k): int(v) for k, v in (r.get("nseq") or {}).items()}
    n_total = int(r.get("n_total") or 0)
    last_day = max(nseq) if nseq else None
    seq_end = nseq[last_day] if last_day else None
    sleep_days = sorted(int(d) for d in (r.get("수면") or []))
    # pending: 마지막으로 cycle(15) 에 도달한 지점 이후 수면OFF 가 없으면 이월
    hit = [d for d, s in sorted(nseq.items()) if s >= 15]
    pending = 1 if hit and not any(d > hit[-1] for d in sleep_days) else 0
    seed.append({
        "ward": gname, "nurse_id": nid, "gid": gid, "name": r["name"],
        "seq_at_end": seq_end, "last_n_day": last_day, "pending": pending,
        "n_total": n_total, "sleep": sleep_days,
    })

# ★ 같은 사람이 여러 파일(원본/AI본)에 있으므로 nurse_id 기준으로 한 번 더 접는다.
#   연번 정보가 더 많은 쪽을 채택한다.
dedup = {}
for s_ in seed:
    k = s_["nurse_id"]
    want = _dbn.get(k, 0)
    gap = abs((s_["n_total"] or 0) - want)
    prev = dedup.get(k)
    if prev is None:
        dedup[k] = s_
        continue
    prev_gap = abs((prev["n_total"] or 0) - want)
    # DB 와 더 가까운 쪽 · 동률이면 연번 정보가 더 많은 쪽
    if gap < prev_gap or (gap == prev_gap and (s_["seq_at_end"] or 0) > (prev["seq_at_end"] or 0)):
        dedup[k] = s_
seed = list(dedup.values())

out = "tools/leave_analysis/night_cycle_seed.json"
json.dump(seed, open(out, "w", encoding="utf-8"), ensure_ascii=False)
print(f"재산출 {len(seed)}명 → {out}")
print(f"  seq_at_end 보유 {sum(1 for s in seed if s['seq_at_end'])}명 · pending>0 {sum(1 for s in seed if s['pending'])}명")
if unmatched: print(f"  미매칭 {len(unmatched)}: {unmatched[:8]}")
db.close()
