"""The mind graph: agents as a living, force-directed network.

The panel dashboard (`render.py`) answers "is anything wrong". This answers
"what is out there and how is it wired" — the question you actually have when
you open a tab to look at a fleet of agents.

Force-directed layout, canvas, animated. The physics is the same idea as
vis-network or Cytoscape (repulsion between every pair, springs along edges,
gravity to centre) written compactly instead of imported, because both of those
are 400-600KB and need a CDN — and this page has to render with the network
cable pulled, opened as a local file. That constraint is the same one the rest
of the harness enforces, and it is why the whole thing is one file.

Everything untrusted is escaped for the renderer it lands in. Node labels are
agent-authored and reach both HTML and a JSON blob inside a `<script>`, which
is where ADR 0028's defect lives one layer up.
"""

from __future__ import annotations

import html
from dataclasses import dataclass, field
from typing import Any

from aef.dash.contract import PanelState
from aef.dash.render import json_for_script

# Node kinds get different shapes and physics weights. Keeping them distinct is
# the same call `render.py` makes about the two graphs: an agent node and a
# lifecycle state are different things and merging them reads as one graph with
# a confusing topology.
KIND_AGENT = "agent"
KIND_LIFECYCLE = "lifecycle"
KIND_REPO = "repo"


@dataclass(frozen=True)
class MindNode:
    id: str
    label: str
    kind: str = KIND_AGENT
    state: str = PanelState.UNKNOWN.value
    detail: str = ""
    weight: float = 1.0
    hitl: bool = False
    # Remembered layout. None means NEW — the page relaxes it in visibly
    # rather than dropping it at the origin, because the frame in which a node
    # first appears is the most informative one in the product.
    x: float | None = None
    y: float | None = None
    is_new: bool = False
    runs: int = 0

    def __post_init__(self) -> None:
        # ADR 0107's rule, applied to a pixel. In a graph the temptation is
        # stronger than in a panel, because an un-instrumented node still draws
        # as a perfectly nice circle — there is no empty space to notice.
        # Reproduced: MindNode(runs=0, state="healthy") rendered a full,
        # confident dot for a node nothing has ever executed.
        if self.runs == 0 and self.state != PanelState.UNKNOWN.value:
            raise ValueError(
                f"node {self.id!r} reports state={self.state!r} with runs=0. A node nothing "
                f"has executed has no health to report — it is UNKNOWN. A healthy-looking "
                f"circle over no data is the failure this whole design exists to prevent, "
                f"and on a graph it is invisible because nothing looks missing."
            )
        if self.weight < 0:
            raise ValueError(f"node {self.id!r} has negative weight {self.weight}")


@dataclass(frozen=True)
class MindEdge:
    """Two quantities, deliberately not collapsed into one.

    `traversals` is cumulative and never decays — it is drawn as THICKNESS and
    answers "how much has this path ever been used". `liveness` is a recency in
    [0, 1] drawn as BRIGHTNESS, and is **None when the path has never fired**.

    That separation is the whole point: an abandoned path is thick and dim, a
    path never taken is thin and dashed. One decayed scalar would render both
    as the same faint line, which is the failure this design exists to avoid.
    """

    source: str
    target: str
    label: str = ""
    active: bool = False
    traversals: int = 0
    liveness: float | None = None

    def __post_init__(self) -> None:
        # Reproduced: traversals=-5 reaches the canvas as Math.log1p(-5) = NaN,
        # lineWidth becomes NaN, and the edge SILENTLY DISAPPEARS. A graph that
        # drops a connection without saying so is worse than one that refuses
        # to draw, because the missing edge is indistinguishable from an
        # absent relationship.
        if self.traversals < 0:
            raise ValueError(
                f"edge {self.source}->{self.target} has {self.traversals} traversals. A "
                f"negative count reaches the canvas as NaN line width and the edge vanishes "
                f"without a message."
            )
        if self.liveness is not None and not 0.0 <= self.liveness <= 1.0:
            raise ValueError(
                f"edge {self.source}->{self.target} has liveness {self.liveness}, outside "
                f"[0, 1]. Alpha clamps silently, so an out-of-range value renders as a "
                f"perfectly ordinary line that means nothing."
            )
        # The invariant the whole encoding rests on: never-traversed is None,
        # not zero. If a caller passes 0.0 for an edge that never fired it
        # would draw as merely stale, which is the one confusion this design
        # exists to prevent.
        if self.traversals == 0 and self.liveness is not None:
            raise ValueError(
                f"edge {self.source}->{self.target} has 0 traversals but liveness="
                f"{self.liveness}. Never-fired must be None — a 0.0 renders as 'abandoned', "
                f"and abandoned is a measurement while never-fired is an absence."
            )


@dataclass(frozen=True)
class MindGraph:
    title: str
    nodes: tuple[MindNode, ...] = ()
    edges: tuple[MindEdge, ...] = ()
    caption: str = ""
    legend: tuple[tuple[str, str], ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        ids = [n.id for n in self.nodes]
        duplicates = sorted({i for i in ids if ids.count(i) > 1})
        if duplicates:
            # Reproduced: the JS builds `idx[node.id]`, so a duplicate id keeps
            # only the last one and every edge silently re-points at it. One
            # node vanishes and the graph still looks complete.
            raise ValueError(
                f"duplicate node ids {duplicates}. The layout indexes nodes by id, so a "
                f"duplicate silently discards one and re-points its edges — leaving a graph "
                f"that looks whole and is not."
            )
        known = {n.id for n in self.nodes}
        dangling = [
            (e.source, e.target)
            for e in self.edges
            if e.source not in known or e.target not in known
        ]
        if dangling:
            # An edge to a node that does not exist silently vanishes in most
            # graph libraries, so the picture looks complete and is not. Refuse
            # rather than draw a graph that is quietly missing connections.
            raise ValueError(
                f"edges reference nodes that do not exist: {dangling}. A dangling edge is "
                f"dropped silently by every layout engine, which makes an incomplete graph "
                f"look finished."
            )

    def to_payload(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "caption": self.caption,
            "nodes": [
                {
                    "id": n.id,
                    "label": n.label,
                    "kind": n.kind,
                    "state": n.state,
                    "detail": n.detail,
                    "weight": n.weight,
                    "hitl": n.hitl,
                    "x": n.x,
                    "y": n.y,
                    "isNew": n.is_new,
                    "runs": n.runs,
                }
                for n in self.nodes
            ],
            "edges": [
                {
                    "source": e.source,
                    "target": e.target,
                    "label": e.label,
                    "active": e.active,
                    "traversals": e.traversals,
                    "liveness": e.liveness,
                }
                for e in self.edges
            ],
        }


def render_mind(graphs: list[MindGraph]) -> str:
    """One self-contained page holding one or more mind graphs."""
    payload = [g.to_payload() for g in graphs]
    tabs = "".join(
        f'<button class="tab" data-i="{i}">{html.escape(g.title)}</button>'
        for i, g in enumerate(graphs)
    )
    legend_items = "".join(
        f'<span class="lg"><i class="dot {cls}"></i>{html.escape(text)}</span>'
        for cls, text in (
            ("healthy", "healthy"),
            ("degraded", "degraded"),
            ("unknown", "no data"),
            ("hitl", "human approval gate"),
            ("newnode", "first seen this run"),
        )
    )
    return (
        _PAGE.replace("__CSS__", _CSS)
        .replace("__JS__", _JS)
        .replace("__TABS__", tabs)
        .replace("__LEGEND__", legend_items)
        .replace("__DATA__", json_for_script(payload))
    )


_PAGE = """<title>Agent mind graph</title>
<style>__CSS__</style>
<main>
  <header class="head">
    <div>
      <p class="eyebrow">live topology</p>
      <h1>Agent mind graph</h1>
    </div>
    <div class="tabs">__TABS__</div>
  </header>
  <p class="caption" id="caption"></p>
  <div class="stage">
    <canvas id="c"></canvas>
    <div class="tip" id="tip"></div>
    <div class="legend">__LEGEND__</div>
    <div class="hint">drag to move · hover for detail · double-click to release</div>
  </div>
  <footer>Read-only. No controls, no requests. Generated from recorded state.</footer>
</main>
<script type="application/json" id="mind-data">__DATA__</script>
<script>__JS__</script>
"""

_CSS = """
/* Palette: an instrument, not a website. Ground is a blue-biased ink so the
   phosphor accent sits ON it rather than vibrating against pure black. The
   semantic trio (ok/warn/bad) is deliberately separate from the accent, so
   "this edge is live" never reads as "this node is fine". */
:root{
  --ink:#0a0d12; --panel:#11151d; --line:#1e2531; --grid:#141a24;
  --fg:#e6e9f0; --muted:#798294;
  --accent:#7fd1c1;            /* phosphor: flow, liveness */
  --ok:#57c98b; --warn:#e0a94a; --bad:#e5665f; --unk:#5c6577;
  --mono:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;
  --sans:ui-sans-serif,-apple-system,"Segoe UI",Roboto,sans-serif;
}
@media (prefers-color-scheme: light){
  :root{ --ink:#f6f7f9; --panel:#ffffff; --line:#e2e6ec; --grid:#eef1f5;
         --fg:#141922; --muted:#68717f; --accent:#178c78;
         --ok:#1d9a63; --warn:#b57d10; --bad:#cf4b45; --unk:#98a1b0; }
}
:root[data-theme="dark"]{
  --ink:#0a0d12; --panel:#11151d; --line:#1e2531; --grid:#141a24;
  --fg:#e6e9f0; --muted:#798294; --accent:#7fd1c1;
  --ok:#57c98b; --warn:#e0a94a; --bad:#e5665f; --unk:#5c6577;
}
:root[data-theme="light"]{
  --ink:#f6f7f9; --panel:#ffffff; --line:#e2e6ec; --grid:#eef1f5;
  --fg:#141922; --muted:#68717f; --accent:#178c78;
  --ok:#1d9a63; --warn:#b57d10; --bad:#cf4b45; --unk:#98a1b0;
}
*{box-sizing:border-box}
body{margin:0;background:var(--ink);color:var(--fg);font:15px/1.55 var(--sans);
  -webkit-font-smoothing:antialiased}
main{max-width:1180px;margin:0 auto;padding:2rem 1.25rem 3rem;
  display:flex;flex-direction:column;gap:1rem}
.head{display:flex;flex-wrap:wrap;gap:1rem;align-items:flex-end;
  justify-content:space-between}
.eyebrow{font:500 11px/1 var(--mono);letter-spacing:.16em;text-transform:uppercase;
  color:var(--accent);margin:0 0 .5rem}
h1{font-size:1.6rem;line-height:1.15;margin:0;letter-spacing:-.02em;
  text-wrap:balance;font-weight:620}
/* Segmented control, not pills-in-a-row: these are views of one thing. */
.tabs{display:flex;border:1px solid var(--line);border-radius:9px;overflow:hidden;
  background:var(--panel)}
.tab{background:transparent;border:0;border-right:1px solid var(--line);
  color:var(--muted);padding:.5rem .9rem;font:500 12px/1 var(--mono);
  letter-spacing:.04em;cursor:pointer;transition:color .15s,background .15s}
.tab:last-child{border-right:0}
.tab:hover{color:var(--fg)}
.tab.on{color:var(--ink);background:var(--accent)}
.tab:focus-visible{outline:2px solid var(--accent);outline-offset:-2px}
.caption{color:var(--muted);font-size:.86rem;margin:0;max-width:64ch}
.stage{position:relative;border:1px solid var(--line);border-radius:14px;
  overflow:hidden;background:var(--panel)}
canvas{display:block;width:100%;height:min(68vh,640px);touch-action:none}
.tip{position:absolute;pointer-events:none;opacity:0;transition:opacity .12s;
  background:var(--ink);border:1px solid var(--line);border-radius:10px;
  padding:.55rem .7rem;max-width:270px;box-shadow:0 10px 30px rgba(0,0,0,.45)}
.tip b{display:block;font:600 12px/1.3 var(--mono);letter-spacing:.02em;
  margin-bottom:.25rem}
.tip span{display:block;font-size:.76rem;color:var(--muted)}
.legend{position:absolute;left:1rem;bottom:.9rem;display:flex;gap:1rem;
  flex-wrap:wrap;font:11px/1 var(--mono);letter-spacing:.05em;color:var(--muted)}
.lg{display:flex;align-items:center;gap:.4rem}
.dot{width:9px;height:9px;border-radius:50%;display:inline-block;flex:0 0 auto}
.dot.healthy{background:var(--ok)}
.dot.degraded{background:var(--bad)}
.dot.unknown{background:transparent;border:1.5px dashed var(--unk)}
.dot.hitl{background:transparent;border:2px solid var(--warn)}
.dot.newnode{background:transparent;border:2px solid var(--accent)}
.hint{position:absolute;right:1rem;bottom:.9rem;font:11px/1 var(--mono);
  letter-spacing:.05em;color:var(--muted)}
footer{color:var(--muted);font-size:.78rem;border-top:1px solid var(--line);
  padding-top:1rem}
@media(max-width:680px){.hint{display:none}.legend{gap:.7rem}}
@media(prefers-reduced-motion:reduce){.tab{transition:none}}
"""

_JS = """
(function(){
var DATA = JSON.parse(document.getElementById('mind-data').textContent);
var cv = document.getElementById('c'), cx = cv.getContext('2d');
var tip = document.getElementById('tip'), cap = document.getElementById('caption');
var W=0,H=0,DPR=Math.min(window.devicePixelRatio||1,2);
var nodes=[],edges=[],idx={},hover=null,drag=null,t=0,cur=-1;

function css(v){return getComputedStyle(document.documentElement).getPropertyValue(v).trim();}
function colorFor(n){
  if(n.state==='degraded')return css('--bad');
  if(n.state==='healthy')return css('--ok');
  return css('--unk');
}
function radius(n){return 9+Math.sqrt(Math.max(n.weight,0))*3.2;}

function load(i){
  cur=i; var g=DATA[i];
  cap.textContent=g.caption||'';
  var cxp=W/2||400, cyp=H/2||300;
  nodes=g.nodes.map(function(n,k){
    var a=(k/g.nodes.length)*Math.PI*2;
    // A remembered position is restored and PINNED. Only nodes with no memory
    // relax into place, so the map stays where the reader left it — the
    // published work on user-guided layout is explicit that structural
    // consistency beats optimal aesthetics, because the reader's memory of
    // where things were IS the value.
    var placed = (n.x!==null && n.x!==undefined && n.y!==null && n.y!==undefined);
    return Object.assign({},n,{
      x: placed ? n.x : cxp+Math.cos(a)*150+(k%3)*7,
      y: placed ? n.y : cyp+Math.sin(a)*150+(k%5)*5,
      vx:0, vy:0, pin: placed, born: placed?0:1
    });
  });
  idx={}; nodes.forEach(function(n){idx[n.id]=n;});
  edges=g.edges.map(function(e,i){return {a:idx[e.source],b:idx[e.target],
    label:e.label,active:e.active,ph:((i*0.6180339887)%1)};});
  document.querySelectorAll('.tab').forEach(function(b,j){
    b.classList.toggle('on', j===i);});
}

function size(){
  var r=cv.getBoundingClientRect(); W=r.width; H=r.height;
  cv.width=W*DPR; cv.height=H*DPR; cx.setTransform(DPR,0,0,DPR,0,0);
}

function step(){
  var i,j,n,m,dx,dy,d,f;
  for(i=0;i<nodes.length;i++){
    n=nodes[i];
    for(j=i+1;j<nodes.length;j++){
      m=nodes[j]; dx=m.x-n.x; dy=m.y-n.y; d=Math.sqrt(dx*dx+dy*dy)||0.01;
      f=2600/(d*d); dx/=d; dy/=d;
      n.vx-=dx*f; n.vy-=dy*f; m.vx+=dx*f; m.vy+=dy*f;
    }
  }
  for(i=0;i<edges.length;i++){
    var e=edges[i]; if(!e.a||!e.b)continue;
    dx=e.b.x-e.a.x; dy=e.b.y-e.a.y; d=Math.sqrt(dx*dx+dy*dy)||0.01;
    f=(d-125)*0.012; dx/=d; dy/=d;
    e.a.vx+=dx*f; e.a.vy+=dy*f; e.b.vx-=dx*f; e.b.vy-=dy*f;
  }
  for(i=0;i<nodes.length;i++){
    n=nodes[i];
    n.vx+=(W/2-n.x)*0.0016; n.vy+=(H/2-n.y)*0.0016;
    if(n.pin||n===drag){n.vx=0;n.vy=0;continue;}
    if(n.born){ n.born+=1; if(n.born>190){ n.born=0; n.pin=true; } }
    n.vx*=0.86; n.vy*=0.86;
    n.x+=Math.max(-6,Math.min(6,n.vx)); n.y+=Math.max(-6,Math.min(6,n.vy));
    var r=radius(n)+4;
    n.x=Math.max(r,Math.min(W-r,n.x)); n.y=Math.max(r,Math.min(H-r,n.y));
  }
}

var CALM = window.matchMedia('(prefers-reduced-motion: reduce)').matches;

function grid(){
  var g=css('--grid'); cx.strokeStyle=g; cx.lineWidth=1;
  for(var x=0;x<W;x+=34){cx.beginPath();cx.moveTo(x+.5,0);cx.lineTo(x+.5,H);cx.stroke();}
  for(var y=0;y<H;y+=34){cx.beginPath();cx.moveTo(0,y+.5);cx.lineTo(W,y+.5);cx.stroke();}
}

function draw(){
  if(!CALM) t+=0.016;
  cx.clearRect(0,0,W,H); grid();
  var nb={};
  if(hover){nb[hover.id]=1;edges.forEach(function(e){
    if(e.a===hover)nb[e.b.id]=1; if(e.b===hover)nb[e.a.id]=1;});}

  edges.forEach(function(e){
    if(!e.a||!e.b)return;
    var lit = hover && (e.a===hover||e.b===hover);
    // THICKNESS = cumulative traversals, log-scaled so one very hot path does
    // not flatten every other. Never decays.
    var w = 1 + Math.log1p(e.traversals||0)*0.85;
    // BRIGHTNESS = liveness. null means NEVER traversed: drawn thin and
    // dashed, distinct from an abandoned path which stays THICK and merely
    // dims. Collapsing those two into one faint line is the failure this
    // encoding exists to avoid.
    var never = (e.liveness===null||e.liveness===undefined);
    var live = never ? 0 : e.liveness;
    cx.setLineDash(never?[4,4]:[]);
    cx.strokeStyle = lit ? css('--accent') : css('--line');
    cx.lineWidth = lit ? Math.max(w,1.8) : w;
    cx.globalAlpha = (hover&&!lit) ? 0.2 : (never ? 0.35 : 0.35+live*0.65);
    cx.beginPath(); cx.moveTo(e.a.x,e.a.y); cx.lineTo(e.b.x,e.b.y); cx.stroke();
    cx.setLineDash([]);
    if(e.active && !CALM && !never && live>0.15){
      var p=((t*0.35+e.ph)%1);
      var px=e.a.x+(e.b.x-e.a.x)*p, py=e.a.y+(e.b.y-e.a.y)*p;
      cx.fillStyle=css('--accent'); cx.globalAlpha=(hover&&!lit)?0.3:0.95;
      cx.beginPath(); cx.arc(px,py,2.6,0,6.284); cx.fill();
    }
    cx.globalAlpha=1;
  });

  nodes.forEach(function(n){
    var r=radius(n), col=colorFor(n);
    var dim = hover && !nb[n.id];
    cx.globalAlpha = dim ? 0.25 : 1;
    if(n.state==='healthy' && !CALM){
      var pulse=r+4+Math.sin(t*1.6+n.x*0.02)*2.2;
      cx.beginPath(); cx.arc(n.x,n.y,pulse,0,6.284);
      cx.fillStyle=col; cx.globalAlpha=dim?0.05:0.13; cx.fill();
      cx.globalAlpha=dim?0.25:1;
    }
    cx.beginPath(); cx.arc(n.x,n.y,r,0,6.284);
    cx.fillStyle = n.state==='unknown' ? 'transparent' : col;
    cx.fill();
    cx.lineWidth = n.state==='unknown' ? 1.6 : 0;
    if(n.state==='unknown'){cx.setLineDash([3,3]);cx.strokeStyle=col;cx.stroke();cx.setLineDash([]);}
    if(n.hitl){
      cx.beginPath(); cx.arc(n.x,n.y,r+4.5,0,6.284);
      cx.strokeStyle=css('--warn'); cx.lineWidth=2; cx.stroke();
    }
    // Seen for the first time this generation: an expanding ring. This is the
    // frame worth catching — the graph grew, and something caused that.
    if(n.isNew){
      if(CALM){
        cx.beginPath(); cx.arc(n.x,n.y,r+6,0,6.284);
        cx.strokeStyle=css('--accent'); cx.lineWidth=1.4; cx.stroke();
      } else {
        var ring=(t*0.55)%1;
        cx.beginPath(); cx.arc(n.x,n.y,r+2+ring*18,0,6.284);
        cx.strokeStyle=css('--accent'); cx.lineWidth=1.4;
        cx.globalAlpha=(dim?0.2:1)*(1-ring); cx.stroke();
        cx.globalAlpha=dim?0.25:1;
      }
    }
    cx.fillStyle=css('--fg'); cx.font='500 11px ui-monospace,SFMono-Regular,Menlo,monospace';
    cx.textAlign='center'; cx.textBaseline='top';
    cx.fillText(n.label, n.x, n.y+r+5);
    cx.globalAlpha=1;
  });
}

function loop(){ step(); draw(); requestAnimationFrame(loop); }

function at(x,y){
  for(var i=nodes.length-1;i>=0;i--){
    var n=nodes[i], dx=x-n.x, dy=y-n.y;
    if(dx*dx+dy*dy < Math.pow(radius(n)+6,2)) return n;
  }
  return null;
}
function pos(ev){var r=cv.getBoundingClientRect();
  var s=ev.touches?ev.touches[0]:ev; return {x:s.clientX-r.left,y:s.clientY-r.top};}

cv.addEventListener('mousemove',function(ev){
  var p=pos(ev);
  if(drag){drag.x=p.x;drag.y=p.y;return;}
  var n=at(p.x,p.y); hover=n;
  cv.style.cursor = n?'grab':'default';
  if(n){
    tip.style.opacity=1;
    tip.style.left=Math.min(p.x+14,W-250)+'px'; tip.style.top=(p.y+14)+'px';
    tip.innerHTML='<b></b><span></span>';
    tip.firstChild.textContent=n.label;
    tip.lastChild.textContent=(n.detail||n.kind)+' · '+n.state+(n.hitl?' · HITL gate':'');
  } else { tip.style.opacity=0; }
});
cv.addEventListener('mousedown',function(ev){var p=pos(ev);drag=at(p.x,p.y);
  if(drag){drag.pin=true;cv.style.cursor='grabbing';}});
window.addEventListener('mouseup',function(){drag=null;cv.style.cursor='default';});
cv.addEventListener('dblclick',function(ev){var p=pos(ev);var n=at(p.x,p.y);
  if(n)n.pin=false;});
cv.addEventListener('mouseleave',function(){hover=null;tip.style.opacity=0;});

document.querySelectorAll('.tab').forEach(function(b){
  b.addEventListener('click',function(){load(parseInt(b.dataset.i,10));});
});
window.addEventListener('resize',function(){size();});
size(); load(0); loop();
})();
"""
