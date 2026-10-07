"""Milestone 3: one self-contained HTML file.

No CDN, no external stylesheet, no remote font, no fetch. It must render with
the network cable pulled, opened as a local file — the same containment
property the rest of the harness enforces, and it makes the page publishable
unchanged.

Everything untrusted is escaped for the renderer it lands in, and there are
three of them nested here: HTML, Mermaid, and JSON inside a `<script>` block.
ADR 0028 was one untrusted string reaching one renderer; a node id containing
`"]` broke out of its own label. This has more strings and more renderers, so
the escaping is per-renderer and each is tested against the real hostile
string rather than a paraphrase.

**The Mermaid trust boundary, stated rather than assumed.** This module does
not escape Mermaid labels, because it never builds one from an untrusted
string: `graph_mermaid` arrives as finished diagram source from
`Graph.visualize()`, which does its own escaping (ADR 0028), and the loop
diagram is built here from fixed labels and integer counts. Both are then
`html.escape`d into a `<pre>`, so a hostile diagram would be displayed rather
than executed.

A `mermaid_label()` helper was written for this and deleted before it shipped:
it had no caller, and a declared escaper nobody invokes reads as an escaper
that is running (ADR 0092). If a future caller passes node ids straight in,
that helper has to come back WITH its call site, not before it.
"""

from __future__ import annotations

import html
import json
from typing import Any

from aef.dash.contract import PanelState
from aef.dash.export import StatusExport

STATE_GLYPH = {
    PanelState.HEALTHY: "●",
    PanelState.DEGRADED: "▲",
    PanelState.UNKNOWN: "○",
}


def json_for_script(payload: Any) -> str:
    """JSON safe to embed in a `<script>` block.

    `json.dumps` escapes neither `</script>` inside a string value nor the
    U+2028/U+2029 line separators, which are literal newlines to a JavaScript
    parser and terminate the statement. Both are how a string that looked
    escaped becomes executable.
    """
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return (
        encoded.replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("&", "\\u0026")
        .replace(" ", "\\u2028")
        .replace(" ", "\\u2029")
    )


def _panel_card(key: str, body: dict[str, Any]) -> str:
    state = PanelState(body["state"])
    glyph = STATE_GLYPH[state]
    title = html.escape(str(body["title"]))
    watches = html.escape(str(body["watches"]))

    if state is PanelState.UNKNOWN:
        detail = f'<div class="reason">no data: {html.escape(str(body.get("reason", "")))}</div>'
        remedy = str(body.get("remedy", ""))
        if remedy:
            detail += f'<div class="remedy">{html.escape(remedy)}</div>'
    else:
        detail = f'<div class="value">{html.escape(str(body.get("value", "")))}</div>'

    return (
        f'<article class="panel {state.value}">'
        f'<header><span class="glyph">{glyph}</span>'
        f"<h3>{title}</h3></header>"
        f'<p class="watches">{watches}</p>'
        f"{detail}"
        f'<footer class="state">{state.value}</footer>'
        f"</article>"
    )


def render(export: StatusExport) -> str:
    payload = export.to_payload()
    panels = payload["panels"]

    banner_bits: list[str] = []
    facts = payload["facts"]

    # The kill switch, the halt state and the ledger verification go ABOVE the
    # metrics. An operator scanning for "is anything wrong" must not scroll
    # past twelve healthy counters to find the halt.
    if facts.get("kill_switch_engaged"):
        reason = html.escape(str(facts.get("kill_switch_reason", "")))
        banner_bits.append(
            f'<div class="banner halt"><strong>LOOP HALTED</strong>'
            f"<span>{reason or 'no reason recorded'}</span></div>"
        )
    if facts.get("ledger_error"):
        banner_bits.append(
            f'<div class="banner halt"><strong>LEDGER CHAIN FAILED VERIFICATION</strong>'
            f"<span>{html.escape(str(facts['ledger_error']))}</span></div>"
        )
    elif not facts.get("ledger_verified"):
        banner_bits.append(
            '<div class="banner unknown"><strong>LEDGER NOT VERIFIED</strong>'
            "<span>no chain to check — this is an absence, not a pass</span></div>"
        )

    unknown_count = sum(1 for b in panels.values() if b["state"] == PanelState.UNKNOWN.value)
    if unknown_count:
        banner_bits.append(
            f'<div class="banner unknown"><strong>{unknown_count} of {len(panels)} panels '
            f"have no data behind them</strong>"
            f"<span>an empty panel is not a passing one</span></div>"
        )

    cards = "".join(_panel_card(k, b) for k, b in sorted(panels.items()))

    graph_section = ""
    if payload["graph_mermaid"]:
        graph_section = (
            '<section><h2>Agent graph</h2><div class="scroll">'
            f'<pre class="mermaid">{html.escape(payload["graph_mermaid"])}</pre>'
            "</div></section>"
        )

    loop_section = (
        '<section><h2>Proposal lifecycle</h2><div class="scroll">'
        f'<pre class="mermaid">{html.escape(payload["loop_mermaid"])}</pre>'
        "</div></section>"
    )

    fact_rows = "".join(
        f"<tr><th>{html.escape(str(k))}</th><td>{html.escape(str(v))}</td></tr>"
        for k, v in sorted(facts.items())
    )

    worst = PanelState(payload["worst_state"])

    return f"""<title>{html.escape(export.repo_name)} — agent status</title>
<style>{_CSS}</style>
<main>
  <header class="top {worst.value}">
    <h1>{html.escape(export.repo_name)}</h1>
    <p class="sub">{STATE_GLYPH[worst]} {worst.value}
      · generated {html.escape(payload["generated_at"])}
      · schema v{payload["schema_version"]}</p>
  </header>
  {"".join(banner_bits)}
  <section class="grid">{cards}</section>
  {graph_section}
  {loop_section}
  <section><h2>Facts</h2><div class="scroll"><table>{fact_rows}</table></div></section>
  <footer class="foot">Read-only. This page has no controls and makes no requests.</footer>
</main>
<script type="application/json" id="aef-status">{json_for_script(payload)}</script>
"""


_CSS = """
:root {
  --bg: #fbfbfa; --fg: #1a1a19; --muted: #6b6b68; --card: #ffffff;
  --line: #e3e3e0; --ok: #2f7d4f; --bad: #b0342c; --unk: #8a6d1f;
}
@media (prefers-color-scheme: dark) {
  :root { --bg:#161615; --fg:#eeeeec; --muted:#9a9a96; --card:#1f1f1e;
          --line:#33332f; --ok:#6fbf8b; --bad:#e8776d; --unk:#d4b25a; }
}
:root[data-theme="dark"] {
  --bg:#161615; --fg:#eeeeec; --muted:#9a9a96; --card:#1f1f1e;
  --line:#33332f; --ok:#6fbf8b; --bad:#e8776d; --unk:#d4b25a;
}
:root[data-theme="light"] {
  --bg:#fbfbfa; --fg:#1a1a19; --muted:#6b6b68; --card:#ffffff;
  --line:#e3e3e0; --ok:#2f7d4f; --bad:#b0342c; --unk:#8a6d1f;
}
body { background: var(--bg); color: var(--fg);
  font: 15px/1.55 ui-sans-serif, -apple-system, "Segoe UI", sans-serif;
  margin: 0; padding: 2rem 1.25rem; }
main { max-width: 1100px; margin: 0 auto; }
h1 { font-size: 1.5rem; margin: 0; letter-spacing: -0.01em; }
h2 { font-size: 1rem; text-transform: uppercase; letter-spacing: 0.08em;
  color: var(--muted); margin: 2.5rem 0 0.75rem; font-weight: 600; }
h3 { font-size: 0.95rem; margin: 0; font-weight: 600; }
.top { border-bottom: 1px solid var(--line); padding-bottom: 1rem; }
.sub { color: var(--muted); margin: 0.35rem 0 0; font-size: 0.85rem; }
.top.healthy .sub { color: var(--ok); }
.top.degraded .sub { color: var(--bad); }
.top.unknown .sub { color: var(--unk); }
.banner { margin-top: 1rem; padding: 0.8rem 1rem; border-radius: 8px;
  display: flex; flex-direction: column; gap: 0.2rem; font-size: 0.9rem; }
.banner strong { letter-spacing: 0.04em; }
.banner span { color: var(--muted); }
.banner.halt { background: color-mix(in srgb, var(--bad) 14%, transparent);
  border: 1px solid var(--bad); }
.banner.unknown { background: color-mix(in srgb, var(--unk) 14%, transparent);
  border: 1px solid var(--unk); }
.grid { display: grid; gap: 0.75rem; margin-top: 1.5rem;
  grid-template-columns: repeat(auto-fill, minmax(230px, 1fr)); }
.panel { background: var(--card); border: 1px solid var(--line);
  border-radius: 10px; padding: 0.9rem; display: flex; flex-direction: column;
  gap: 0.4rem; }
.panel header { display: flex; align-items: center; gap: 0.5rem; }
.glyph { font-size: 0.9rem; }
.panel.healthy .glyph { color: var(--ok); }
.panel.degraded .glyph { color: var(--bad); }
.panel.unknown .glyph { color: var(--unk); }
.panel.unknown { border-style: dashed; }
.watches { color: var(--muted); font-size: 0.8rem; margin: 0; }
.value { font-size: 1.5rem; font-weight: 600; font-variant-numeric: tabular-nums; }
.reason { font-family: ui-monospace, monospace; font-size: 0.8rem; color: var(--unk); }
.remedy { font-size: 0.78rem; color: var(--muted); }
.state { font-size: 0.7rem; text-transform: uppercase; letter-spacing: 0.08em;
  color: var(--muted); margin-top: auto; }
.scroll { overflow-x: auto; }
table { border-collapse: collapse; font-size: 0.85rem; min-width: 320px; }
th, td { text-align: left; padding: 0.35rem 1rem 0.35rem 0;
  border-bottom: 1px solid var(--line); font-weight: 400; }
th { color: var(--muted); font-family: ui-monospace, monospace; }
td { font-variant-numeric: tabular-nums; }
pre.mermaid { background: var(--card); border: 1px solid var(--line);
  border-radius: 10px; padding: 1rem; font-size: 0.82rem; margin: 0; }
.foot { margin-top: 3rem; padding-top: 1rem; border-top: 1px solid var(--line);
  color: var(--muted); font-size: 0.8rem; }
img { max-width: 100%; }
"""
