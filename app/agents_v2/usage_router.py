"""사용량 대시보드 엔드포인트 — agent_llm_usage 집계 노출.

agents_v2(스케줄링 에이전트)와 원티드 agent 의 LLM 토큰/비용을 한 테이블
(agent_llm_usage)에서 집계한다.

- GET /api/agent/usage           : JSON API (HN/ADM 전용, by=group|nurse|model|purpose)
- GET /api/agent/usage/view      : 서버 렌더 대시보드(HTML). 내부/개발용 — 인증 없이 전체 조회.
  office→group 롤업 + 일자별 누적 + 월별. 데이터를 서버가 직접 렌더(토큰 불필요).
  ⚠️ 비용 데이터 무인증 노출이므로 운영 배포 전 게이트/차단 필요(개발 편의용).
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from agents_v2.usage import usage_by_office, usage_summary, usage_timeseries
from db.client2 import get_db
from routers.auth import get_current_user_from_cookie
from schemas.auth_schema import User

router = APIRouter(prefix="/api/agent/usage", tags=["agent_usage"])


def _resolve_role(user: User) -> str:
    """chat_router._resolve_role 와 동일 의미 (ADM / HN / NURSE)."""
    if getattr(user, "is_master_admin", False):
        return "ADM"
    if getattr(user, "is_head_nurse", False) or (getattr(user, "hn_auth", "") or "").upper() == "HN":
        return "HN"
    return "NURSE"


@router.get("")
def get_usage(
    by: str = Query("group", description="group|nurse|model|purpose"),
    days: Optional[int] = Query(None, ge=1, le=365, description="최근 N일 (미지정=전체)"),
    current_user: User = Depends(get_current_user_from_cookie),
    db: Session = Depends(get_db),
) -> dict:
    """LLM 사용량/비용 집계. HN=자기 병동, ADM=전체."""
    role = _resolve_role(current_user)
    if role not in ("HN", "ADM"):
        raise HTTPException(
            status_code=403,
            detail="사용량 조회는 수간호사(HN) 또는 관리자(ADM) 전용입니다.",
        )

    since = datetime.utcnow() - timedelta(days=days) if days else None
    # HN 은 자기 병동만, ADM 은 전체(group_id=None)
    group_id = None if role == "ADM" else getattr(current_user, "group_id", None)

    try:
        rows = usage_summary(db, by=by, group_id=group_id, since=since)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    total = {
        "calls": sum(r["calls"] for r in rows),
        "input_tokens": sum(r["input_tokens"] for r in rows),
        "output_tokens": sum(r["output_tokens"] for r in rows),
        "cost_usd": round(sum(r["cost_usd"] for r in rows), 6),
    }
    return {
        "by": by,
        "since_days": days,
        "scope": "all" if role == "ADM" else group_id,
        "rows": rows,
        "total": total,
    }


# ── 서버 렌더 대시보드 (Jinja 불필요 · 인증 없이 전체) ────────────────────────
# 데이터를 서버가 직접 조회해 HTML 에 임베드 → 셸이 별도 인증 fetch 안 함.
# office→group 롤업 / 일자별 누적 / 월별. 개발·내부 확인용(운영 게이트 필요).


def build_dashboard_data(db: Session, *, days: Optional[int] = None) -> dict:
    """대시보드가 그릴 전체 데이터(총합 + office롤업 + 일자별 + 월별)."""
    since = datetime.utcnow() - timedelta(days=days) if days else None
    offices = usage_by_office(db, since=since)
    daily = usage_timeseries(db, bucket="day", since=since)
    monthly = usage_timeseries(db, bucket="month", since=since)
    total = {
        "calls": sum(o["calls"] for o in offices),
        "input_tokens": sum(o["input_tokens"] for o in offices),
        "output_tokens": sum(o["output_tokens"] for o in offices),
        "cost_usd": round(sum(o["cost_usd"] for o in offices), 6),
    }
    return {
        "days": days,
        "total": total,
        "offices": offices,
        "daily": daily,
        "monthly": monthly,
    }


def render_dashboard_html(data: dict) -> str:
    """대시보드 데이터를 임베드한 자기완결 HTML 반환(외부 의존성 0)."""
    payload = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
    return _DASH_TEMPLATE.replace("/*__USAGE_DATA__*/null", payload)


@router.get("/view", response_class=HTMLResponse, include_in_schema=False)
def usage_dashboard_view(
    days: Optional[int] = Query(None, ge=1, le=365, description="최근 N일 (미지정=전체)"),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    """서버 렌더 사용량 대시보드(내부/개발용, 무인증 전체 조회)."""
    return HTMLResponse(content=render_dashboard_html(build_dashboard_data(db, days=days)))


_DASH_TEMPLATE = """<!doctype html>
<html lang="ko">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>LLM 사용량 대시보드</title>
<style>
  :root{
    --bg:#f6f6f3; --panel:#ffffff; --ink:#1b1a18; --muted:#73716c;
    --line:#e7e4de; --accent:#2f6f5e; --soft:#e2efe9; --bar:#84c3b0; --line2:#2f6f5e;
    --grid:#eeece7; --warn:#c76b3f;
  }
  @media (prefers-color-scheme:dark){
    :root{ --bg:#16171a; --panel:#1e2023; --ink:#ecebe7; --muted:#9b9995;
      --line:#2f3236; --accent:#74cbb0; --soft:#233530; --bar:#3e7d6c; --line2:#74cbb0;
      --grid:#26292d; --warn:#e0895f; }
  }
  *{box-sizing:border-box}
  body{margin:0;background:var(--bg);color:var(--ink);
    font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","Apple SD Gothic Neo","Malgun Gothic",sans-serif;
    font-size:14px;line-height:1.5;padding:26px 20px 70px}
  .wrap{max-width:1000px;margin:0 auto}
  h1{font-size:20px;font-weight:660;margin:0 0 2px;letter-spacing:-.01em}
  .sub{color:var(--muted);font-size:12.5px;margin-bottom:18px}
  .rangebar{display:flex;gap:6px;margin-bottom:20px;flex-wrap:wrap}
  .rangebar a{font-size:12.5px;padding:6px 12px;border:1px solid var(--line);border-radius:999px;
    color:var(--muted);text-decoration:none;background:var(--panel)}
  .rangebar a.on{background:var(--accent);border-color:var(--accent);color:#fff}
  .kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:12px;margin-bottom:24px}
  .kpi{background:var(--panel);border:1px solid var(--line);border-radius:13px;padding:15px 17px}
  .kpi .k{font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:.05em}
  .kpi .v{font-size:23px;font-weight:650;margin-top:6px;font-variant-numeric:tabular-nums}
  .card{background:var(--panel);border:1px solid var(--line);border-radius:13px;margin-bottom:22px;overflow:hidden}
  .card .hd{display:flex;align-items:center;justify-content:space-between;gap:10px;
    padding:14px 17px;border-bottom:1px solid var(--line)}
  .card .hd h2{font-size:13.5px;font-weight:640;margin:0}
  .card .hd .note{font-size:11.5px;color:var(--muted)}
  .toggle{display:flex;gap:4px}
  .toggle button{font:inherit;font-size:12px;padding:5px 11px;border:1px solid var(--line);
    background:var(--panel);color:var(--muted);border-radius:8px;cursor:pointer}
  .toggle button.on{background:var(--soft);color:var(--accent);border-color:var(--accent)}
  .chartwrap{padding:14px 12px 6px}
  canvas{width:100%;height:230px;display:block}
  .chart-empty{padding:40px;text-align:center;color:var(--muted)}
  .tbl-scroll{overflow-x:auto}
  table{width:100%;border-collapse:collapse;font-variant-numeric:tabular-nums}
  th,td{padding:10px 16px;text-align:right;white-space:nowrap}
  th:first-child,td:first-child{text-align:left}
  thead th{font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:.04em;
    font-weight:600;border-bottom:1px solid var(--line)}
  tbody tr+tr td{border-top:1px solid var(--line)}
  .cost{font-weight:600}
  .off-row{cursor:pointer}
  .off-row:hover{background:var(--soft)}
  .off-row .name{display:flex;align-items:center;gap:9px}
  .caret{display:inline-block;width:9px;color:var(--muted);transition:transform .12s}
  .off-row.open .caret{transform:rotate(90deg)}
  .grp-row td{background:color-mix(in srgb,var(--soft) 40%,transparent);font-size:13px}
  .grp-row td:first-child{padding-left:38px;color:var(--muted)}
  .barwrap{display:inline-block;width:78px;height:6px;background:var(--soft);border-radius:3px;
    overflow:hidden;vertical-align:middle;margin-right:9px}
  .bar{display:block;height:6px;background:var(--bar);border-radius:3px;min-width:2px}
  .msg{padding:34px 16px;text-align:center;color:var(--muted)}
  .foot{margin-top:8px;font-size:11.5px;color:var(--muted)}
  .legend{display:flex;gap:16px;padding:0 17px 12px;font-size:11.5px;color:var(--muted)}
  .legend i{display:inline-block;width:20px;height:0;vertical-align:middle;margin-right:6px}
  .legend .l-bar i{height:9px;background:var(--bar);border-radius:2px}
  .legend .l-line i{border-top:2px solid var(--line2)}
</style>
</head>
<body>
<div class="wrap">
  <h1>LLM 사용량 대시보드</h1>
  <div class="sub">스케줄링 · 원티드 에이전트 통합 · <span id="scope-note">전체(무인증 내부 뷰)</span></div>

  <div class="rangebar" id="rangebar">
    <a data-d="7">최근 7일</a>
    <a data-d="30">최근 30일</a>
    <a data-d="90">최근 90일</a>
    <a data-d="">전체</a>
  </div>

  <div class="kpis">
    <div class="kpi"><div class="k">총 비용 (USD)</div><div class="v cost" id="k-cost">—</div></div>
    <div class="kpi"><div class="k">입력 토큰</div><div class="v" id="k-in">—</div></div>
    <div class="kpi"><div class="k">출력 토큰</div><div class="v" id="k-out">—</div></div>
    <div class="kpi"><div class="k">호출 수</div><div class="v" id="k-calls">—</div></div>
  </div>

  <div class="card">
    <div class="hd">
      <h2>일자별 누적 사용량</h2>
      <div class="toggle" id="metric-toggle">
        <button data-m="cost" class="on">비용</button>
        <button data-m="tokens">토큰</button>
      </div>
    </div>
    <div class="chartwrap"><canvas id="chart"></canvas></div>
    <div class="legend">
      <span class="l-bar"><i></i>일별</span>
      <span class="l-line"><i></i>누적</span>
    </div>
  </div>

  <div class="card">
    <div class="hd"><h2>월별 사용량</h2><span class="note">달 단위 합계</span></div>
    <div class="tbl-scroll">
      <table>
        <thead><tr><th>월</th><th>호출</th><th>입력</th><th>출력</th><th>비용(USD)</th><th>누적 비용</th></tr></thead>
        <tbody id="monthly-rows"></tbody>
      </table>
    </div>
  </div>

  <div class="card">
    <div class="hd"><h2>병원(office) → 병동(group) 롤업</h2><span class="note">행 클릭 시 병동 펼침</span></div>
    <div class="tbl-scroll">
      <table>
        <thead><tr><th>병원 / 병동</th><th>호출</th><th>입력</th><th>출력</th><th>비용(USD)</th></tr></thead>
        <tbody id="office-rows"></tbody>
      </table>
    </div>
  </div>
  <div class="foot" id="foot"></div>
</div>

<script id="usage-data" type="application/json">/*__USAGE_DATA__*/null</script>
<script>
const DATA = JSON.parse(document.getElementById('usage-data').textContent);
const nf = new Intl.NumberFormat('ko-KR');
const cf = n => '$' + Number(n||0).toLocaleString('en-US',{minimumFractionDigits:4,maximumFractionDigits:4});
const el = id => document.getElementById(id);
let METRIC = 'cost';

// ── 기간 링크 활성표시 ──
(function(){
  const cur = DATA.days == null ? '' : String(DATA.days);
  el('rangebar').querySelectorAll('a').forEach(a=>{
    if(a.dataset.d === cur) a.classList.add('on');
    const q = a.dataset.d ? ('?days='+a.dataset.d) : '?';
    a.setAttribute('href', location.pathname + q);
  });
})();

// ── KPI ──
(function(){
  const t = DATA.total||{};
  el('k-cost').textContent = cf(t.cost_usd);
  el('k-in').textContent = nf.format(t.input_tokens||0);
  el('k-out').textContent = nf.format(t.output_tokens||0);
  el('k-calls').textContent = nf.format(t.calls||0);
  const d = DATA.days == null ? '전체 기간' : ('최근 '+DATA.days+'일');
  el('foot').textContent = d + ' · 비용은 모델 단가 기준 추정치 · office_id 는 group→office 매핑으로 롤업';
})();

// ── 월별 표 ──
(function(){
  const rows = DATA.monthly||[];
  el('monthly-rows').innerHTML = rows.length ? rows.map(r=>
    `<tr><td>${r.bucket}</td><td>${nf.format(r.calls)}</td><td>${nf.format(r.input_tokens)}</td>`
    +`<td>${nf.format(r.output_tokens)}</td><td class="cost">${cf(r.cost_usd)}</td>`
    +`<td>${cf(r.cum_cost_usd)}</td></tr>`
  ).join('') : '<tr><td colspan="6" class="msg">기록된 사용량이 없습니다.</td></tr>';
})();

// ── Office → Group 롤업 (아코디언) ──
(function(){
  const offs = DATA.offices||[];
  if(!offs.length){ el('office-rows').innerHTML='<tr><td colspan="5" class="msg">기록된 사용량이 없습니다.</td></tr>'; return; }
  const maxCost = Math.max(...offs.map(o=>o.cost_usd||0), 1e-9);
  let html = '';
  offs.forEach((o,oi)=>{
    const w = Math.max(2, Math.round((o.cost_usd||0)/maxCost*78));
    html += `<tr class="off-row" data-oi="${oi}"><td><span class="name">`
      + `<span class="caret">▸</span><span class="barwrap"><span class="bar" style="width:${w}px"></span></span>`
      + `<strong>${o.office_name||'(병원 미상)'}</strong></span></td>`
      + `<td>${nf.format(o.calls)}</td><td>${nf.format(o.input_tokens)}</td>`
      + `<td>${nf.format(o.output_tokens)}</td><td class="cost">${cf(o.cost_usd)}</td></tr>`;
    (o.groups||[]).forEach(g=>{
      html += `<tr class="grp-row" data-parent="${oi}" hidden><td>${g.group_name||g.group_id||'(미상)'}</td>`
        + `<td>${nf.format(g.calls)}</td><td>${nf.format(g.input_tokens)}</td>`
        + `<td>${nf.format(g.output_tokens)}</td><td class="cost">${cf(g.cost_usd)}</td></tr>`;
    });
  });
  el('office-rows').innerHTML = html;
  el('office-rows').querySelectorAll('.off-row').forEach(row=>{
    row.addEventListener('click',()=>{
      row.classList.toggle('open');
      const open = row.classList.contains('open');
      document.querySelectorAll(`.grp-row[data-parent="${row.dataset.oi}"]`).forEach(g=>g.hidden=!open);
    });
  });
})();

// ── 일자별 누적 라인차트 (canvas, 의존성 0) ──
const canvas = el('chart');
function draw(){
  const daily = DATA.daily||[];
  const ctx = canvas.getContext('2d');
  const dpr = window.devicePixelRatio||1;
  const cssW = canvas.clientWidth||600, cssH = 230;
  canvas.width = cssW*dpr; canvas.height = cssH*dpr;
  ctx.setTransform(dpr,0,0,dpr,0,0);
  ctx.clearRect(0,0,cssW,cssH);
  const cs = getComputedStyle(document.documentElement);
  const C = k => cs.getPropertyValue(k).trim();
  if(!daily.length){ ctx.fillStyle=C('--muted'); ctx.font='13px sans-serif'; ctx.textAlign='center';
    ctx.fillText('기록된 사용량이 없습니다.', cssW/2, cssH/2); return; }

  const padL=54, padR=14, padT=14, padB=26;
  const w = cssW-padL-padR, h = cssH-padT-padB;
  const dayVal = r => METRIC==='cost' ? r.cost_usd : (r.input_tokens+r.output_tokens);
  const cumVal = r => METRIC==='cost' ? r.cum_cost_usd : r.cum_tokens;
  const barMax = Math.max(...daily.map(dayVal), 1e-9);
  const cumMax = Math.max(...daily.map(cumVal), 1e-9);
  const n = daily.length;
  const x = i => padL + (n===1 ? w/2 : w*i/(n-1));
  const bw = Math.max(1, Math.min(16, w/n*0.6));

  // grid
  ctx.strokeStyle=C('--grid'); ctx.lineWidth=1; ctx.fillStyle=C('--muted'); ctx.font='10px sans-serif';
  ctx.textAlign='right'; ctx.textBaseline='middle';
  for(let g=0; g<=4; g++){
    const yy = padT + h*g/4;
    ctx.beginPath(); ctx.moveTo(padL,yy); ctx.lineTo(cssW-padR,yy); ctx.stroke();
    const v = cumMax*(1-g/4);
    ctx.fillText(METRIC==='cost' ? '$'+v.toFixed(v<1?3:2) : Intl.NumberFormat('ko',{notation:'compact'}).format(v), padL-8, yy);
  }
  // daily bars (일별)
  ctx.fillStyle=C('--bar');
  daily.forEach((r,i)=>{ const bh=h*dayVal(r)/barMax; ctx.fillRect(x(i)-bw/2, padT+h-bh, bw, bh); });
  // cumulative line (누적)
  ctx.strokeStyle=C('--line2'); ctx.lineWidth=2; ctx.beginPath();
  daily.forEach((r,i)=>{ const yy=padT+h-h*cumVal(r)/cumMax; i?ctx.lineTo(x(i),yy):ctx.moveTo(x(i),yy); });
  ctx.stroke();
  // endpoint dot
  const last=daily[n-1]; const ey=padT+h-h*cumVal(last)/cumMax;
  ctx.fillStyle=C('--line2'); ctx.beginPath(); ctx.arc(x(n-1),ey,3.2,0,7); ctx.fill();
  // x labels (first / last)
  ctx.fillStyle=C('--muted'); ctx.textBaseline='top'; ctx.textAlign='left';
  ctx.fillText(daily[0].bucket, padL, padT+h+7);
  if(n>1){ ctx.textAlign='right'; ctx.fillText(last.bucket, cssW-padR, padT+h+7); }
}
el('metric-toggle').querySelectorAll('button').forEach(b=>{
  b.addEventListener('click',()=>{
    METRIC=b.dataset.m;
    el('metric-toggle').querySelectorAll('button').forEach(x=>x.classList.toggle('on',x===b));
    draw();
  });
});
window.addEventListener('resize', draw);
draw();
</script>
</body>
</html>"""
