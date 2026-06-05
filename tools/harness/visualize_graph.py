from __future__ import annotations

import argparse
import json
from pathlib import Path


HTML_TEMPLATE = """<!doctype html>
<html lang="ko">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width,initial-scale=1" />
  <title>Harness Graph Viewer</title>
  <script src="https://unpkg.com/cytoscape@3.28.1/dist/cytoscape.min.js"></script>
  <style>
    body { margin:0; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; background:#0b1020; color:#e8ecf4; }
    #topbar { padding:12px 16px; border-bottom:1px solid #29304a; display:flex; gap:12px; align-items:center; flex-wrap:wrap; }
    #cy { width:100vw; height:calc(100vh - 82px); }
    .chip { padding:6px 10px; border-radius:999px; border:1px solid #3a4568; background:#121a33; font-size:12px; }
    .legend { display:flex; gap:8px; flex-wrap:wrap; }
    button { background:#1f2a4d; color:#e8ecf4; border:1px solid #3a4568; border-radius:8px; padding:7px 10px; cursor:pointer; }
    button:hover { background:#27345d; }
    .hint { opacity:.8; font-size:12px; }
  </style>
</head>
<body>
  <div id="topbar">
    <div class="chip"><b>Run</b> __RUN_ID__</div>
    <div class="chip"><b>Group</b> __GROUP_ID__</div>
    <div class="chip"><b>Month</b> __YEAR__-__MONTH__</div>
    <div class="chip"><b>Strategy</b> __STRATEGY__</div>
    <button id="fitAll">전체 보기</button>
    <button id="showFails">실패 체인만</button>
    <button id="showWanted">Wanted 체인만</button>
    <button id="showAll">전체 노드</button>
    <button id="toggleVirtual">연결 보강 ON</button>
    <span class="hint">노드 클릭: 연결 하이라이트 / 휠: 확대</span>
  </div>
  <div id="cy"></div>

  <script>
    const graph = __GRAPH_JSON__;

    const palette = {
      RunNode: '#7dd3fc',
      GroupNode: '#c4b5fd',
      MonthNode: '#93c5fd',
      RuleNode: '#a7f3d0',
      MetricNode: '#f9a8d4',
      ViolationNode: '#fca5a5',
      CoverageMinNode: '#fde68a',
      TeamMinNode: '#fdba74',
      GradeMinNode: '#fcd34d',
      GradeMaxNode: '#fbbf24',
      OffWindowNode: '#86efac',
      CarryoverTransitionNode: '#67e8f9',
      PrecepteeSyncNode: '#d8b4fe',
      WantedSubmissionNode: '#60a5fa',
      WantedApplyNode: '#34d399',
      ConstraintNode: '#9ca3af',
      default: '#d1d5db'
    };

    const failRuleIds = new Set((graph.violations || []).map(v => v.rule_id));
    const failViolationIds = new Set((graph.violations || []).map(v => v.violation_id));

    const elements = [];
    const virtualElements = [];
    for (const n of (graph.nodes || [])) {
      const isFailRule = n.type === 'RuleNode' && failRuleIds.has(n.attrs?.rule_id);
      const isFailViolation = n.type === 'ViolationNode' && failViolationIds.has(n.id);
      elements.push({
        data: {
          id: n.id,
          label: n.id,
          type: n.type,
          short: n.id.length > 42 ? (n.id.slice(0, 39) + '...') : n.id,
          color: palette[n.type] || palette.default,
          isFailRule,
          isFailViolation,
          attrs: JSON.stringify(n.attrs || {})
        }
      });
    }
    for (const e of (graph.edges || [])) {
      elements.push({ data: { id: `${e.type}|${e.from}|${e.to}`, source: e.from, target: e.to, type: e.type } });
    }

    // Visualization-only virtual edges to keep the graph connected around run node.
    // These do NOT change graph_export.json semantics.
    const runNode = (graph.nodes || []).find(n => n.type === 'RunNode');
    const runNodeId = runNode ? runNode.id : null;
    if (runNodeId) {
      for (const n of (graph.nodes || [])) {
        if (n.type === 'MetricNode') {
          virtualElements.push({
            data: {
              id: `VIRTUAL_IN_RUN|${runNodeId}|${n.id}`,
              source: runNodeId,
              target: n.id,
              type: 'VIRTUAL_IN_RUN',
              virtual: true
            },
            classes: 'virtual-edge'
          });
        }
      }
    }

    const cy = cytoscape({
      container: document.getElementById('cy'),
      elements,
      style: [
        {
          selector: 'node',
          style: {
            'background-color': 'data(color)',
            'label': 'data(short)',
            'font-size': 10,
            'text-wrap': 'wrap',
            'text-max-width': 140,
            'color': '#0b1020',
            'border-width': 1,
            'border-color': '#0b1020',
            'width': 28,
            'height': 28
          }
        },
        {
          selector: 'node[isFailRule = 1], node[isFailViolation = 1]',
          style: {
            'border-width': 4,
            'border-color': '#ef4444',
            'width': 34,
            'height': 34
          }
        },
        {
          selector: 'edge',
          style: {
            'curve-style': 'bezier',
            'target-arrow-shape': 'triangle',
            'line-color': '#60709c',
            'target-arrow-color': '#60709c',
            'label': 'data(type)',
            'font-size': 8,
            'color': '#e8ecf4',
            'text-background-color': '#0b1020',
            'text-background-opacity': 0.7,
            'text-background-padding': 2,
            'width': 1.7
          }
        },
        {
          selector: 'edge.virtual-edge',
          style: {
            'line-style': 'dashed',
            'line-color': '#475569',
            'target-arrow-color': '#475569',
            'target-arrow-shape': 'none',
            'label': '',
            'width': 1,
            'opacity': 0.5
          }
        },
        { selector: '.dim', style: { 'opacity': 0.15 } },
        { selector: '.focus', style: { 'opacity': 1 } }
      ],
      layout: { name: 'cose', animate: false, nodeRepulsion: 8000, idealEdgeLength: 110, gravity: 0.4 }
    });

    let virtualOn = false;
    function applyLayout() {
      cy.layout({ name: 'cose', animate: false, nodeRepulsion: 8000, idealEdgeLength: 110, gravity: 0.4 }).run();
      cy.fit(cy.elements(':visible'), 40);
    }

    function setVirtualEdges(enabled) {
      const btn = document.getElementById('toggleVirtual');
      virtualOn = enabled;
      if (enabled) {
        cy.add(virtualElements);
        btn.textContent = '연결 보강 OFF';
      } else {
        cy.$('edge.virtual-edge').remove();
        btn.textContent = '연결 보강 ON';
      }
      applyLayout();
    }

    cy.on('tap', 'node', (evt) => {
      const n = evt.target;
      cy.elements().addClass('dim').removeClass('focus');
      const hood = n.closedNeighborhood();
      hood.addClass('focus').removeClass('dim');
      n.style('label', `${n.data('short')}\n${n.data('type')}`);
    });

    cy.on('tap', (evt) => {
      if (evt.target === cy) {
        cy.elements().removeClass('dim focus');
      }
    });

    document.getElementById('fitAll').onclick = () => cy.fit(cy.elements(), 40);

    document.getElementById('showFails').onclick = () => {
      const keep = new Set();
      for (const v of (graph.violations || [])) {
        for (const id of (v.node_ids || [])) keep.add(id);
        keep.add(v.violation_id);
      }
      cy.nodes().forEach(n => n.style('display', keep.has(n.id()) ? 'element' : 'none'));
      cy.edges().forEach(e => {
        const visible = e.source().style('display') !== 'none' && e.target().style('display') !== 'none';
        e.style('display', visible ? 'element' : 'none');
      });
      cy.fit(cy.elements(':visible'), 40);
    };

    document.getElementById('showWanted').onclick = () => {
      const keep = new Set();
      const wantedNodes = (graph.nodes || []).filter(n => n.type === 'WantedSubmissionNode' || n.type === 'WantedApplyNode');
      for (const n of wantedNodes) keep.add(n.id);

      // wanted node와 직접 연결된 엣지/노드 유지
      for (const e of (graph.edges || [])) {
        if (keep.has(e.from) || keep.has(e.to)) {
          keep.add(e.from);
          keep.add(e.to);
        }
      }

      // wanted owner_family 위반(E_WANTED_APPLY) 관련 violation 체인도 포함
      for (const v of (graph.violations || [])) {
        if (v.rule_id === 'E_WANTED_APPLY') {
          keep.add(v.violation_id);
          for (const id of (v.node_ids || [])) keep.add(id);
        }
      }

      cy.nodes().forEach(n => n.style('display', keep.has(n.id()) ? 'element' : 'none'));
      cy.edges().forEach(e => {
        const visible = e.source().style('display') !== 'none' && e.target().style('display') !== 'none';
        e.style('display', visible ? 'element' : 'none');
      });
      cy.fit(cy.elements(':visible'), 40);
    };

    document.getElementById('showAll').onclick = () => {
      cy.elements().style('display', 'element');
      cy.fit(cy.elements(), 40);
    };

    document.getElementById('toggleVirtual').onclick = () => {
      setVirtualEdges(!virtualOn);
    };

    // Default ON for intuitive connected graph view.
    setVirtualEdges(true);
  </script>
</body>
</html>
"""


def main() -> None:
    p = argparse.ArgumentParser(description="Render harness graph_export.json to interactive HTML")
    p.add_argument("--input", required=True, help="Path to graph_export.json")
    p.add_argument("--output", default="", help="Output HTML path (default: same dir/graph_view.html)")
    args = p.parse_args()

    in_path = Path(args.input).resolve()
    if not in_path.exists():
        raise FileNotFoundError(f"input not found: {in_path}")

    data = json.loads(in_path.read_text(encoding="utf-8"))
    run = data.get("run") or {}

    out_path = Path(args.output).resolve() if args.output else in_path.parent / "graph_view.html"

    html = (
        HTML_TEMPLATE.replace("__RUN_ID__", str(run.get("run_id") or "-"))
        .replace("__GROUP_ID__", str(run.get("group_id") or "-"))
        .replace("__YEAR__", str(run.get("year") or "-"))
        .replace("__MONTH__", f"{int(run.get('month') or 0):02d}" if run.get("month") else "-")
        .replace("__STRATEGY__", str(run.get("strategy") or "-"))
        .replace("__GRAPH_JSON__", json.dumps(data, ensure_ascii=False))
    )
    out_path.write_text(html, encoding="utf-8")
    print(str(out_path))


if __name__ == "__main__":
    main()
