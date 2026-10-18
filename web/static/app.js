// Decision Brain page. Safety rules: model and graph text is only ever set with textContent; links are built
// as elements whose href comes from the response's own `sources` list and must be http(s).
"use strict";

const $ = (id) => document.getElementById(id);
const state = { graph: null, frozen: false, replayMode: false, lastQuestion: null, lastAsOf: null,
                questionId: null, seen: new Set(), cy: null, es: null, lastPing: 0, kep: null,
                busy: false, view: "answer", unread: 0, mode: "workspace" };
window.decisionBrain = state;   // read-only handle for the browser check (scripts/ui_check.cjs)
window.DB = { ask: (question) => { setMode("workspace"); ask(question, ""); } };   // used by the Atlas
const RELEASES = ["v1.37", "v1.36", "v1.35", "v1.34", "v1.33", "v1.32", "v1.31", "v1.30", "v1.29", "v1.28",
                  "v1.27", "v1.26", "v1.25", "v1.24", "v1.23", "v1.22", "v1.21", "v1.20"];
const REDUCED_MOTION = window.matchMedia("(prefers-reduced-motion: reduce)");
const NARROW = window.matchMedia("(max-width: 860px)");
const SVG_NS = "http://www.w3.org/2000/svg";

// ---------- small helpers ----------
function el(tag, text, cls) {
  const e = document.createElement(tag);
  if (text !== undefined && text !== null) e.textContent = String(text);
  if (cls) e.className = cls;
  return e;
}
function icon(name) {
  const svg = document.createElementNS(SVG_NS, "svg");
  svg.setAttribute("class", "icon");
  svg.setAttribute("aria-hidden", "true");
  const use = document.createElementNS(SVG_NS, "use");
  use.setAttribute("href", `#i-${name}`);
  svg.appendChild(use);
  return svg;
}
function chip(text, kind) { return el("span", text, kind ? `chip ${kind}` : "chip"); }
function safeUrl(u) {
  try { const url = new URL(u); return (url.protocol === "https:" || url.protocol === "http:") ? url.href : null; }
  catch { return null; }
}
function link(u, text) {
  const href = safeUrl(u);
  if (!href) return el("span", text);
  const a = el("a", text); a.href = href; a.target = "_blank"; a.rel = "noopener noreferrer"; return a;
}
function shortUrl(u) { return String(u || "").replace(/^https?:\/\/(www\.)?github\.com\//, ""); }
async function api(path, opts = {}) {
  const r = await fetch(path, { headers: { "Content-Type": "application/json" }, ...opts });
  const body = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(body.detail || `HTTP ${r.status}`);
  return body;
}
function fmtTime(iso) { return iso ? new Date(iso).toLocaleString() : "?"; }
function relTime(iso) {
  if (!iso) return "time unknown";
  const s = (Date.now() - new Date(iso)) / 1000;
  if (Number.isNaN(s)) return "time unknown";
  if (s < 0 || s > 7 * 86400) return new Date(iso).toLocaleDateString(undefined, { day: "numeric", month: "short", year: "numeric" });
  if (s < 45) return "just now";
  if (s < 3600) return `${Math.round(s / 60)} min ago`;
  if (s < 86400) return `${Math.round(s / 3600)} h ago`;
  return `${Math.round(s / 86400)} d ago`;
}
function announce(msg) {   // polite screen-reader announcement for async work
  const a = $("announcer");
  a.textContent = "";
  setTimeout(() => { a.textContent = msg; }, 50);
}

// ---------- walkthrough (brain_demo only); steps only advance on real events ----------
const STEP_HINTS = {
  ask: "Asking the graph agent…",
  inspect: "Next: open “How I got this”, then press “Replay next real change” in the Live feed.",
  change: "A real change was applied. Answers it touches are flagged outdated.",
  outdated: "Next: press Re-ask to answer again from the updated graph.",
  reask: "Next: turn on “Compare with vector search” and ask a question.",
  compare: "Both methods answered. Compare the evidence each one used.",
};
const FAILED_HINT = "The request failed. Retry, or ask another question.";
function step(name) {
  const items = [...$("scenario").querySelectorAll("li")];
  const idx = items.findIndex((li) => li.dataset.step === name);
  items.forEach((li, i) => {
    li.classList.toggle("active", i === idx);
    li.classList.toggle("done", i < idx);
    if (i === idx) li.setAttribute("aria-current", "step"); else li.removeAttribute("aria-current");
  });
  if (STEP_HINTS[name]) $("scenariohint").textContent = STEP_HINTS[name];
}

// ---------- answer rendering ----------
// "text [2] more" -> text nodes + <a class=cite> for valid [n]; "- item" lines become list items.
function renderText(container, answer, sources) {
  const lines = String(answer || "").split("\n");
  let list = null;
  for (const raw of lines) {
    const line = raw.trimEnd();
    if (!line.trim()) { list = null; continue; }
    const bullet = /^\s*[-*•]\s+/.test(line);
    let target;
    if (bullet) {
      if (!list) { list = el("ul"); container.appendChild(list); }
      target = el("li"); list.appendChild(target);
    } else { list = null; target = el("p"); container.appendChild(target); }
    const content = bullet ? line.replace(/^\s*[-*•]\s+/, "") : line;
    const parts = content.split(/(\[\d+\])/);
    for (const part of parts) {
      const m = part.match(/^\[(\d+)\]$/);
      if (m && sources[Number(m[1]) - 1] && safeUrl(sources[Number(m[1]) - 1])) {
        const a = link(sources[Number(m[1]) - 1], part); a.className = "cite";
        a.title = shortUrl(sources[Number(m[1]) - 1]);
        target.appendChild(a);
      } else {
        target.appendChild(document.createTextNode(part.replace(/\*+/g, "")));   // drop markdown emphasis marks
      }
    }
  }
}

const GUARD_LABEL = {   // the citation guard's own status values (src/agent/loop.py, src/baseline.py)
  ok: ["Citations checked", "ok"], retried: ["Citations checked after retry", "ok"],
  repaired: ["Bad citations removed", "warn"], fallback: ["Fallback answer", "warn"],
  "report-only": ["Citations not enforced", ""],
};
function cardHead(title, kind, chips) {
  const head = el("div", null, "cardhead");
  const h = el("h3");
  h.append(el("span", null, `modedot ${kind}`), title);
  const c = el("div", null, "chips");
  chips.forEach((x) => c.appendChild(x));
  head.append(h, c);
  return head;
}
function errorBox(message) {
  const box = el("div", null, "errbox");
  box.append(icon("alert"), el("p", message));
  if (state.lastQuestion) {
    const b = el("button", "Retry", "btn small"); b.type = "button";
    b.onclick = () => ask(state.lastQuestion, state.lastAsOf);
    box.appendChild(b);
  }
  return box;
}
function loadingCard(card, title, kind, text) {
  card.hidden = false;
  card.classList.remove("is-outdated");
  card.replaceChildren(cardHead(title, kind, []), el("p", text, "loading"),
                       el("div", null, "skel w80"), el("div", null, "skel w60"));
}

function renderCard(card, title, kind, res) {
  card.classList.remove("is-outdated");
  if (!res || res.status === "skipped") { card.hidden = true; card.replaceChildren(); return; }
  card.hidden = false;
  if (res.status === "error") {
    card.replaceChildren(cardHead(title, kind, [chip("Failed", "danger")]), errorBox(`This mode failed: ${res.error}`));
    return;
  }
  const chips = [];
  const [glabel, gkind] = GUARD_LABEL[res.guard] || [`Guard: ${res.guard}`, ""];
  if (res.guard) chips.push(chip(glabel, gkind));
  if (res.guard_problems && res.guard_problems.length) chips.push(chip(`${res.guard_problems.length} citation issue${res.guard_problems.length > 1 ? "s" : ""}`, "warn"));
  if (res.as_of_requested) chips.push(chip(`As of ${res.as_of_requested}`));
  if (res.replayed) chips.push(chip("Recorded answer"));
  card.replaceChildren(cardHead(title, kind, chips));

  const text = el("div", null, "text");
  renderText(text, res.answer, res.sources || []);
  if (!text.childNodes.length) text.appendChild(el("p", "The answer was empty.", "muted"));
  card.appendChild(text);

  const cited = res.citations || [];
  if (cited.length) {
    card.appendChild(el("div", `Sources (${cited.length})`, "srchead"));
    const ol = el("ol", null, "sources");
    for (const c of [...cited].sort((a, b) => a.n - b.n)) {   // only cited sources, keeping their [n] numbers
      const li = el("li");
      li.value = c.n;
      li.appendChild(link(c.url, shortUrl(c.url)));
      ol.appendChild(li);
    }
    card.appendChild(ol);
  } else {
    card.appendChild(el("p", "No sources cited.", "muted small"));
  }

  const meta = el("dl", null, "meta");
  const kv = (k, v) => { const d = el("div"); d.append(el("dt", k), el("dd", v)); meta.appendChild(d); };
  if (res.as_of_requested) {   // requested vs actually applied cutoff, only for historical questions
    kv("as of requested", res.as_of_requested);
    kv("applied", (res.as_of_applied || []).join("; ") || "none (no historical tool used)");
  }
  if (res.data_as_of && (res.data_as_of.last_change || res.data_as_of.snapshot))
    kv("graph current to", res.data_as_of.last_change || res.data_as_of.snapshot);
  if (res.tool_calls) kv("tools", res.tool_calls.map((c) => c.name).join(", ") || "none");
  kv("cost", `$${(res.cost_usd || 0).toFixed(4)}`);
  kv("time", `${res.seconds ?? "?"} s`);
  if (res.replayed) kv("recorded", res.asked_at || "?");
  card.appendChild(meta);
}

function renderEvidence(g, b) {
  const box = $("evidence");
  const show = (g && g.status === "ok") || (b && b.status === "ok");
  box.hidden = !show;
  if (!show) return;
  const cols = [["Graph agent", g]];
  if (b) cols.push(["Vector search only", b]);   // b is null when comparison was not requested
  box.replaceChildren(el("h3", "Evidence used"),
    el("p", b ? "Why the answers can differ: the evidence each method retrieved." : "What the graph agent retrieved for this answer."));
  const ok = (res) => res && res.status === "ok";
  const evs = cols.map(([, res]) => (ok(res) ? res.evidence || [] : null));
  const types = [...new Set(evs.flatMap((ev) => (ev || []).map((e) => e.type)))].sort();
  const table = el("table", null, "evtable");
  const head = el("tr");
  head.appendChild(el("th", "", ""));
  cols.forEach(([name]) => { const th = el("th", name); th.scope = "col"; head.appendChild(th); });
  table.appendChild(el("thead")).appendChild(head);
  const body = el("tbody");
  const row = (label, fn) => {
    const tr = el("tr");
    const th = el("th", label); th.scope = "row"; tr.appendChild(th);
    cols.forEach(([, res], i) => {
      if (!evs[i]) { tr.appendChild(el("td", res && res.status === "error" ? "failed" : "not run", "na")); return; }
      tr.appendChild(el("td", fn(evs[i])));
    });
    body.appendChild(tr);
  };
  row("Items", (ev) => ev.length);
  types.forEach((t) => row(t, (ev) => ev.filter((e) => e.type === t).length));
  row("Date range", (ev) => {
    const dated = ev.filter((e) => e.date).map((e) => e.date).sort();
    return dated.length ? `${dated[0]} … ${dated[dated.length - 1]}` : "no dates";
  });
  table.appendChild(body);
  box.appendChild(table);
}

function renderTrace(g, b) {
  const box = $("trace");
  box.replaceChildren();
  let n = 0;
  for (const [name, res] of [["Graph agent", g], ["Vector baseline", b]]) {
    if (!res || res.status !== "ok") continue;
    box.appendChild(el("h4", name));
    const calls = res.tool_calls || [];
    if (calls.length) {
      const ul = el("ul", null, "calls");
      calls.forEach((c) => {
        const li = el("li");
        li.append(el("code", c.name), el("span", JSON.stringify(c.args), "args"),
                  chip(c.found ? "found" : "nothing found", c.found ? "ok" : ""), el("span", `${c.ms} ms`, "muted small"));
        ul.appendChild(li);
      });
      box.appendChild(ul);
    }
    (res.cypher_trace || []).forEach((q) => {
      n++;
      const block = el("div", null, "q");
      const head = el("div", null, "qhead");
      head.append(el("b", q.tool), el("span", `${q.rows} rows`), el("span", `${q.ms} ms`));
      block.append(head, el("div", `params ${JSON.stringify(q.params)}`, "params"), el("pre", q.cypher));
      box.appendChild(block);
    });
  }
  if (!box.childNodes.length) box.appendChild(el("p", "No tool calls or queries ran for this answer.", "muted small"));
  else if (!n) box.appendChild(el("p", "No Cypher queries were recorded.", "muted small"));
  $("tracecount").textContent = n ? `(${n} queries)` : "";
}

// ---------- graph panel ----------
const TYPE_COLOR = { Person: "#af52de", Team: "#30b0c7", Subproject: "#30b0c7", CodeArea: "#636366",
                     Component: "#636366", FeatureGate: "#636366" };
const NOT_ADOPTED = ["withdrawn", "rejected", "replaced", "deferred"];
function kepColor(status) {
  if (status === "implemented") return "#34c759";
  if (NOT_ADOPTED.includes(status)) return "#ff3b30";
  return "#ff9f0a";
}
function cssVar(name) { return getComputedStyle(document.documentElement).getPropertyValue(name).trim(); }
// Cytoscape cannot read CSS variables, so the stylesheet is rebuilt from them on every theme change.
function buildCyStyle() {
  const v = (name, fallback) => cssVar(name) || fallback;
  const label = v("--g-label", "#27272a"), labelBg = v("--g-label-bg", "#ffffff");
  return [
    { selector: "node", style: { "background-color": "data(color)", label: "data(label)", "font-size": 10,
        "font-family": "-apple-system, BlinkMacSystemFont, sans-serif", "text-wrap": "ellipsis", "text-max-width": 120,
        "text-valign": "top", "text-margin-y": -4, width: 16, height: 16, color: label,
        "text-background-color": labelBg, "text-background-opacity": 0.85, "text-background-padding": 2,
        "text-background-shape": "roundrectangle", "border-width": 2, "border-color": v("--g-node-border", "#fff") } },
    { selector: "node.kep", style: { width: 28, height: 28, "font-weight": 600, "font-size": 11 } },
    { selector: "edge", style: { width: 1.2, "line-color": v("--g-edge", "#b9b9c3"), "target-arrow-color": v("--g-edge", "#b9b9c3"),
        "target-arrow-shape": "triangle", "arrow-scale": 0.8, "curve-style": "bezier", label: "data(label)", "font-size": 8,
        "font-family": "ui-monospace, Menlo, monospace", color: v("--g-edge-label", "#63636e"), "text-rotation": "autorotate",
        "text-background-color": labelBg, "text-background-opacity": 1, "text-background-padding": 2 } },
    { selector: "edge.past", style: { "line-style": "dashed" } },
    { selector: "node.hover", style: { "border-color": v("--g-select", "#4f46e5") } },
    { selector: ".faded", style: { opacity: 0.22 } },
    { selector: "edge.hl", style: { width: 2, "line-color": v("--g-edge-strong", "#52525b"),
        "target-arrow-color": v("--g-edge-strong", "#52525b"), color: label } },
    { selector: "node.flash", style: { "border-width": 7, "border-color": v("--g-flash", "#f59e0b") } },
    { selector: "node:selected", style: { "border-width": 3, "border-color": v("--g-select", "#4f46e5") } },
  ];
}
function initCy() {
  state.cy = cytoscape({ container: $("cy"), minZoom: 0.2, maxZoom: 4, style: buildCyStyle() });
  state.cy.on("tap", "node", (e) => focusNode(e.target));
  state.cy.on("tap", (e) => { if (e.target === state.cy) clearFocus(); });
  state.cy.on("mouseover", "node", (e) => e.target.addClass("hover"));
  state.cy.on("mouseout", "node", (e) => e.target.removeClass("hover"));
}
function zoomBy(factor) {
  const cy = state.cy;
  cy.zoom({ level: cy.zoom() * factor, renderedPosition: { x: cy.width() / 2, y: cy.height() / 2 } });
}
// Fit the path, but never blow a small path up past 1.5x (it leaves room to zoom in).
function fitGraph() {
  const cy = state.cy;
  if (!cy.elements().length) return;
  cy.fit(undefined, 24);
  if (cy.zoom() > 1.5) { cy.zoom(1.5); cy.center(); }
}
// Full screen for the graph panel; where the Fullscreen API is missing (iOS Safari) the panel fills the window.
function graphMaxed() { return document.fullscreenElement === $("panel-graph") || $("panel-graph").classList.contains("is-max"); }
function syncGraphMax() {
  const on = graphMaxed(), b = $("graphmax");
  b.setAttribute("aria-pressed", on ? "true" : "false");
  b.setAttribute("aria-label", on ? "Exit full screen" : "Full screen");
  b.title = on ? "Exit full screen (Esc)" : "Full screen";
  b.querySelector("use").setAttribute("href", on ? "#i-collapse" : "#i-expand");
  requestAnimationFrame(() => { state.cy.resize(); fitGraph(); });
}
async function toggleGraphMax() {
  const panel = $("panel-graph");
  if (document.fullscreenElement) await document.exitFullscreen();
  else if (panel.classList.contains("is-max")) panel.classList.remove("is-max");
  else if (panel.requestFullscreen && document.fullscreenEnabled) {
    try { await panel.requestFullscreen(); } catch { panel.classList.add("is-max"); }
  } else panel.classList.add("is-max");
  syncGraphMax();
}
function focusNode(node) {
  if (!node || node.empty()) return;
  const cy = state.cy;
  cy.elements().removeClass("hl").addClass("faded");
  node.closedNeighborhood().removeClass("faded");
  node.connectedEdges().addClass("hl");
  cy.nodes().unselect();
  node.select();
  showNode(node.data());
}
function clearFocus() {
  state.cy.elements().removeClass("faded hl");
  state.cy.nodes().unselect();
  $("nodeinfo").hidden = true;
}
function nodeText(d) {
  if (d.type === "KEP" && d.name && !d.name.startsWith(d.key)) return `${d.key} · ${d.name}`;
  return d.name || d.key || d.type;
}
function renderPath(path) {
  const nodes = (path && path.nodes) || [];
  const edges = (path && path.edges) || [];
  const ids = new Set(nodes.map((n) => String(n.id)));
  const els = nodes.map((n) => ({
    data: { id: String(n.id), key: n.key, type: n.label,
            label: n.label === "KEP" ? n.key : n.label === "Decision" ? "explanation (LLM)" : (n.name || n.key),
            status: n.status, url: n.url, name: n.name,
            color: n.label === "KEP" ? kepColor(n.status) : (TYPE_COLOR[n.label] || "#aeaeb2") },
    classes: n.label === "KEP" ? "kep" : "" }));
  const shownEdges = edges.filter((e) => ids.has(String(e.src)) && ids.has(String(e.dst)));
  shownEdges.forEach((e) => els.push({
    data: { id: "e" + e.id, source: String(e.src), target: String(e.dst), label: e.type, props: e.props },
    classes: (e.props && (e.props.valid_to || e.props.former)) ? "past" : "" }));
  $("nodeinfo").hidden = true;
  state.cy.elements().remove();
  state.cy.add(els);
  const layout = state.cy.layout({ name: "cose", animate: !REDUCED_MOTION.matches, animationDuration: 600, nodeRepulsion: 9000,
                                   idealEdgeLength: 70, fit: true, padding: 24 });
  layout.one("layoutstop", fitGraph);
  layout.run();
  $("pathinfo").textContent = nodes.length ? `${nodes.length} nodes · ${shownEdges.length} edges` : "(no graph path for this answer)";
  $("cyempty").textContent = "No graph path for this answer.";
  $("cyempty").hidden = nodes.length > 0;
  renderPathText(nodes, shownEdges);
  const kep = nodes.find((n) => n.label === "KEP");
  state.kep = kep ? kep.key : null;
}
// The same path as a list: a keyboard and screen-reader route to what the canvas shows.
function renderPathText(nodes, edges) {
  const list = $("pathlist");
  list.replaceChildren();
  $("pathtext").hidden = !nodes.length;
  const byId = new Map(state.cy.nodes().map((n) => [n.id(), n.data()]));
  state.cy.nodes().forEach((n) => {
    const li = el("li");
    const b = el("button", nodeText(n.data())); b.type = "button";
    b.onclick = () => { focusNode(n); state.cy.animate({ center: { eles: n } }, { duration: REDUCED_MOTION.matches ? 0 : 200 }); };
    li.append(b, el("span", ` ${n.data("type")}${n.data("status") ? `, ${n.data("status")}` : ""}`, "muted"));
    list.appendChild(li);
  });
  edges.forEach((e) => {
    const s = byId.get(String(e.src)), t = byId.get(String(e.dst));
    const past = e.props && (e.props.valid_to || e.props.former);
    const li = el("li");
    li.append(nodeText(s), el("span", ` —${e.type}→ `, "rel"), nodeText(t));
    if (past) li.appendChild(el("span", " (past)", "muted"));
    list.appendChild(li);
  });
}
function showNode(d) {
  const box = $("nodeinfo");
  box.hidden = false;
  const head = el("div", null, "nihead");
  const close = el("button", null, "iconbtn"); close.type = "button";
  close.setAttribute("aria-label", "Close node details"); close.title = "Close";
  close.appendChild(icon("close"));
  close.onclick = clearFocus;
  head.append(el("strong", d.name || d.key || d.type || "Node"), close);
  box.replaceChildren(head);
  const dl = el("dl");
  const row = (k, v) => { const dd = el("dd"); if (v instanceof Node) dd.appendChild(v); else dd.textContent = v; dl.append(el("dt", k), dd); };
  if (d.type) row("Type", d.type);
  if (d.key && d.key !== d.name) row("Key", d.key);
  if (d.status) row("Status", d.status);
  if (d.url) row("Link", link(d.url, shortUrl(d.url)));
  box.appendChild(dl);
  if (!d.status && !d.url) box.appendChild(el("p", "No further details for this node.", "muted small"));
}
function flash(keys) {
  if (!state.cy) return;
  state.cy.nodes().forEach((n) => {
    if (keys.includes(n.data("key"))) {
      n.addClass("flash");
      setTimeout(() => n.removeClass("flash"), 2500);
    }
  });
}

// ---------- ask ----------
function setBusy(on) {
  state.busy = on;
  const b = $("askbtn");
  b.disabled = on;
  b.setAttribute("aria-busy", on ? "true" : "false");
  $("asklabel").textContent = on ? "Asking…" : "Ask";
  $("reask").disabled = on;
  $("examples").querySelectorAll("button").forEach((x) => { x.disabled = on; });
}
async function ask(question, asOf, isReask = false) {
  question = (question || "").trim();
  if (state.busy) return;   // one request at a time (Enter and the button share this path)
  if (!question) {
    $("askerr").hidden = false;
    $("question").setAttribute("aria-invalid", "true");
    $("question").focus();
    return;
  }
  $("askerr").hidden = true;
  $("question").removeAttribute("aria-invalid");
  // keep what was shown before a re-ask, so the old and new answers can be compared
  const previous = isReask ? $("graphcard").querySelector(".text") : null;
  if (previous) $("prevtext").replaceChildren(...previous.cloneNode(true).childNodes);
  $("prevanswer").hidden = true;
  $("prevanswer").open = false;

  state.lastQuestion = question; state.lastAsOf = asOf || null;
  $("question").value = question;
  const compare = $("compare").checked;
  setBusy(true);
  $("outdated").hidden = true;
  $("answersempty").hidden = true;
  $("answers").classList.toggle("two", compare);
  loadingCard($("graphcard"), "Graph agent", "graph", "Choosing graph tools…");
  $("basecard").hidden = !compare;
  if (compare) loadingCard($("basecard"), "Vector search only", "vector", "Retrieving passages…");
  step(isReask ? "reask" : "ask");
  $("exbox").open = false;
  announce(isReask ? "Asking again on the updated graph…" : "Asking…");
  try {
    const res = await api("/api/ask", { method: "POST", body: JSON.stringify({
      question, graph: state.graph, mode: compare ? "both" : "graph", as_of: asOf || null }) });
    state.questionId = res.graph && res.graph.question_id;
    renderCard($("graphcard"), "Graph agent", "graph", res.graph);
    renderCard($("basecard"), "Vector search only", "vector", res.baseline);
    renderEvidence(res.graph, compare ? (res.baseline || { status: "error" }) : null);
    renderTrace(res.graph, res.baseline);
    $("drawer").hidden = false;
    renderPath(res.graph && res.graph.status === "ok" ? res.graph.path : null);
    $("actions").hidden = !(state.kep && !state.frozen);
    $("actkep").textContent = state.kep || "";
    $("actout").replaceChildren();
    if (previous && res.graph && res.graph.status === "ok") $("prevanswer").hidden = false;
    if (res.graph && res.graph.status === "ok") {
      step(isReask ? "reask" : compare ? "compare" : "inspect");
      announce("Answer ready.");
    } else {
      $("scenariohint").textContent = FAILED_HINT;
      announce("The graph agent failed. See the error.");
    }
  } catch (err) {
    renderCard($("graphcard"), "Graph agent", "graph", { status: "error", error: err.message });
    $("graphcard").querySelector(".errbox p").textContent = `Request failed: ${err.message}`;
    if (compare) $("basecard").hidden = true;
    $("scenariohint").textContent = FAILED_HINT;
    announce(`Request failed: ${err.message}`);
  } finally {
    setBusy(false);
  }
}

// ---------- live feed ----------
function markOutdated(summary) {
  $("outdatedtext").textContent = `This answer is outdated: ${summary}`;
  $("outdated").hidden = false;
  const card = $("graphcard");
  if (!card.hidden && !card.classList.contains("is-outdated")) {
    card.classList.add("is-outdated");
    const chips = card.querySelector(".chips");
    if (chips) chips.prepend(chip("Outdated", "warn"));
  }
  step("outdated");
  announce("The current answer is outdated. Re-ask to update it.");
}
function addFeedRow(n) {
  if (n.event_id) { if (state.seen.has(n.event_id)) return; state.seen.add(n.event_id); }
  if (window.Atlas) window.Atlas.onNotice(n);
  const li = el("li", null, "new");
  if (n.type === "rationale_updated") {
    li.appendChild(el("span", "llm", "src"));
    li.appendChild(el("span", `Explanation of ${n.kep} re-checked (${n.verified ? "quotes verified" : "some quotes failed"})`));
  } else {
    li.appendChild(el("span", n.source || "event", "src"));
    li.appendChild(n.url ? link(n.url, n.summary) : el("span", n.summary));
    const when = el("div", null, "when");
    const t = el("time", `happened ${relTime(n.occurred_at)}`);
    if (n.occurred_at) { t.dateTime = n.occurred_at; t.title = fmtTime(n.occurred_at); }
    when.appendChild(t);
    if (n.lag_ms !== null && n.lag_ms !== undefined) when.append(` · applied ${(n.lag_ms / 1000).toFixed(2)} s after receipt`);
    li.appendChild(when);
  }
  $("feed").prepend(li);
  while ($("feed").children.length > 60) $("feed").lastChild.remove();
  if (NARROW.matches && state.view !== "live") {
    state.unread++;
    $("livebadge").textContent = String(state.unread);
    $("livebadge").hidden = false;
  }
  flash(n.touched || []);
  if (n.type === "event") step("change");
  if ((n.outdated_questions || []).includes(state.questionId)) markOutdated(n.summary);
}
function connectFeed() {
  if (state.es) state.es.close();
  state.lastPing = Date.now();
  const es = new EventSource(`/api/stream?graph_name=${encodeURIComponent(state.graph)}`);
  state.es = es;
  const live = (ok, text) => { $("feeddot").className = "dot " + (ok ? "on" : "off"); $("feedstate").textContent = text; };
  es.onopen = () => live(true, "connected");
  es.onerror = () => live(false, "reconnecting…");          // EventSource retries by itself
  es.addEventListener("ping", () => { state.lastPing = Date.now(); live(true, "connected"); });
  es.addEventListener("notice", (e) => {
    state.lastPing = Date.now();
    try { addFeedRow(JSON.parse(e.data)); } catch { /* ignore malformed notice */ }
  });
}
setInterval(() => {   // watchdog: the server pings every 15 s; a silent stream is reconnected
  if (state.graph && Date.now() - state.lastPing > 40000) connectFeed();
}, 10000);

// ---------- freshness ----------
function setFresh(kind, text, title) {
  $("fresh").className = kind ? `fresh ${kind}` : "fresh";
  $("freshtext").textContent = text;
  $("fresh").title = title || text;
}
async function refreshHealth() {
  try {
    const h = await api(`/api/health?graph_name=${encodeURIComponent(state.graph)}`);
    state.frozen = h.frozen;
    if (h.replay_mode) $("replaybanner").hidden = false;
    if (h.public_mode && !h.replay_mode) {
      $("replaybanner").textContent = `Public demo: example questions on the Frozen graph use recorded answers; up to ${h.live_per_ip_hour} live questions per hour. The graph, Cypher and live feed are always real.`;
      $("replaybanner").hidden = false;
    }
    const parts = [];
    const day = (iso) => iso ? new Date(iso).toLocaleDateString(undefined, { day: "numeric", month: "short", year: "numeric" }) : "?";
    let kind = "";
    if (h.frozen) parts.push(`Frozen snapshot, ${day(h.snapshot_cutoff)}`);
    else if (h.last_change) {
      kind = "ok";
      parts.push(`Last change ${new Date(h.last_change).toLocaleString(undefined, { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" })}`);
      if (h.lag_ms !== null && h.lag_ms !== undefined) parts.push(`applied in ${(h.lag_ms / 1000).toFixed(2)} s`);
    } else parts.push(`Snapshot ${day(h.snapshot_cutoff)}, waiting for changes`);
    parts.push(`${h.nodes.toLocaleString()} nodes`);
    const detail = [h.last_change && `last change ${fmtTime(h.last_change)}`, h.snapshot_cutoff && `snapshot ${fmtTime(h.snapshot_cutoff)}`,
                    h.now && `checked ${fmtTime(h.now)}`].filter(Boolean).join(" · ");
    setFresh(kind, parts.join(" · "), detail);
    $("replaynext").hidden = state.graph !== "brain_demo";
    $("democtl").hidden = state.graph !== "brain_demo";
    $("scenario").hidden = state.graph !== "brain_demo";
  } catch (err) { setFresh("danger", "Freshness unavailable", `Health check failed: ${err.message}`); }
}

// ---------- actions ----------
async function runAction(name, args, button) {
  const out = $("actout");
  const title = name === "draft_decision_brief" ? `Decision brief · ${args.kep}` : `Suggested reviewers · ${args.kep_or_path}`;
  const buttons = [$("actbrief"), $("actreviewers")];
  buttons.forEach((b) => { b.disabled = true; });
  button.setAttribute("aria-busy", "true");
  out.replaceChildren(el("p", "Working…", "loading"));
  try {
    const r = await api(`/api/act/${name}?graph_name=${encodeURIComponent(state.graph)}`, { method: "POST", body: JSON.stringify(args) });
    let text = "";
    if (name === "draft_decision_brief") text = r.data.markdown || "No brief.";
    else if (name === "recommend_reviewers") text = (r.data.reviewers || [])
      .map((p) => `@${p.login} (${p.score}): ${p.reasons.join("; ")}`).join("\n") || "No reviewers found.";
    out.replaceChildren(el("h4", title), el("div", text));
    announce(`${title} ready.`);
  } catch (err) {
    out.replaceChildren(el("h4", title), el("div", `Action failed: ${err.message}`, "err"));
    announce(`Action failed: ${err.message}`);
  } finally {
    buttons.forEach((b) => { b.disabled = false; });
    button.removeAttribute("aria-busy");
  }
}

// ---------- appearance, about, tabs ----------
function savedTheme() {
  try { const t = localStorage.getItem("db.theme"); return t === "light" || t === "dark" ? t : "system"; }
  catch { return "system"; }
}
function setTheme(t) {
  const root = document.documentElement;
  root.classList.add("theming");   // switch colours at once instead of animating every control
  requestAnimationFrame(() => requestAnimationFrame(() => root.classList.remove("theming")));
  if (t === "light" || t === "dark") document.documentElement.setAttribute("data-theme", t);
  else document.documentElement.removeAttribute("data-theme");
  try { localStorage.setItem("db.theme", t); } catch { /* storage may be unavailable */ }
  restyleGraph();
}
function restyleGraph() {
  if (state.cy) state.cy.style(buildCyStyle());
  if (window.Atlas) window.Atlas.restyle();
}

// Workspace (ask, path, live feed) or Atlas (the whole graph, web/static/atlas.js). #atlas in the URL opens the Atlas.
function setMode(mode) {
  mode = mode === "atlas" && window.Atlas ? "atlas" : "workspace";
  state.mode = mode;
  document.body.dataset.mode = mode;
  document.querySelectorAll("#viewpick input").forEach((r) => { r.checked = r.value === mode; });
  const url = location.pathname + location.search + (mode === "atlas" ? "#atlas" : "");
  if (location.href !== new URL(url, location.href).href) history.replaceState(null, "", url);
  if (mode === "atlas") window.Atlas.show(state.graph);
  else {
    if (window.Atlas) window.Atlas.hide();
    if (state.cy) requestAnimationFrame(() => state.cy.resize());
  }
}

function setAbout(open) {
  $("about").hidden = !open;
  $("abouttoggle").setAttribute("aria-expanded", open ? "true" : "false");
  try { localStorage.setItem("db.about", open ? "open" : "closed"); } catch { /* storage may be unavailable */ }
}

function setView(view) {
  state.view = view;
  $("main").dataset.view = view;
  document.querySelectorAll(".tabs [role=tab]").forEach((t) => {
    const on = t.dataset.view === view;
    t.setAttribute("aria-selected", on ? "true" : "false");
    t.tabIndex = on ? 0 : -1;
  });
  if (view === "live") { state.unread = 0; $("livebadge").hidden = true; }
  if (view === "graph" && state.cy) requestAnimationFrame(() => { state.cy.resize(); fitGraph(); });
}
function initTabs() {
  const tabs = [...document.querySelectorAll(".tabs [role=tab]")];
  tabs.forEach((t, i) => {
    t.onclick = () => setView(t.dataset.view);
    t.onkeydown = (e) => {
      const d = e.key === "ArrowRight" ? 1 : e.key === "ArrowLeft" ? -1 : 0;
      if (!d) return;
      e.preventDefault();
      const next = tabs[(i + d + tabs.length) % tabs.length];
      next.focus();
      setView(next.dataset.view);
    };
  });
  NARROW.addEventListener("change", () => { if (state.cy) requestAnimationFrame(() => state.cy.resize()); });
}

// ---------- setup ----------
async function init() {
  let aboutPref = null;
  try { aboutPref = localStorage.getItem("db.about"); } catch { /* ignore */ }
  setAbout(aboutPref !== "closed");
  $("aboutclose").onclick = () => setAbout(false);
  $("abouttoggle").onclick = () => setAbout($("about").hidden);

  const theme = savedTheme();
  document.querySelectorAll("#themepick input").forEach((r) => {
    r.checked = r.value === theme;
    r.onchange = () => setTheme(r.value);
  });
  window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => {
    if (!document.documentElement.hasAttribute("data-theme")) restyleGraph();   // only System follows the OS
  });

  initTabs();
  initCy();
  $("zoomin").onclick = () => zoomBy(1.25);
  $("zoomout").onclick = () => zoomBy(0.8);
  $("zoomfit").onclick = fitGraph;
  $("graphmax").onclick = toggleGraphMax;
  document.addEventListener("fullscreenchange", syncGraphMax);
  document.addEventListener("keydown", (e) => {   // Esc closes the fallback full-screen panel (the real one handles Esc itself)
    if (e.key === "Escape" && $("panel-graph").classList.contains("is-max")) { $("panel-graph").classList.remove("is-max"); syncGraphMax(); }
  });

  const h = await api("/api/health").catch(() => ({ graphs: ["brain_demo"] }));
  const pick = $("graph");
  const labels = { brain_live: "Live: follows Kubernetes", brain_demo: "Demo: sandbox webhook", brain_eval: "Frozen: 15 Sep 2026" };
  (h.graphs || []).forEach((g) => { const o = el("option", labels[g] || g); o.value = g; pick.appendChild(o); });
  state.graph = (h.replay_mode || h.public_mode) ? "brain_eval"
    : ((h.graphs || []).includes("brain_demo") ? "brain_demo" : h.graphs[0]);
  pick.value = state.graph;
  pick.onchange = () => {
    state.graph = pick.value; state.seen.clear(); $("feed").replaceChildren(); connectFeed(); refreshHealth();
    if (window.Atlas) window.Atlas.reload(state.graph);
  };
  document.querySelectorAll("#viewpick input").forEach((r) => { r.onchange = () => { if (r.checked) setMode(r.value); }; });
  window.addEventListener("hashchange", () => setMode(location.hash === "#atlas" ? "atlas" : "workspace"));
  setMode(location.hash === "#atlas" ? "atlas" : "workspace");
  const asof = $("asof");
  [["", "now"], ...RELEASES.map((r) => [r, r])].forEach(([v, t]) => { const o = el("option", t); o.value = v; asof.appendChild(o); });
  try {
    const ex = await api("/api/examples");
    const picks = ["G1", "B1", "F1", "E1", "C1", "A1", "H2", "I1", "D1"];
    picks.map((id) => ex.find((e) => e.id === id)).filter(Boolean).forEach((e) => {
      const b = el("button", e.question); b.type = "button"; b.onclick = () => ask(e.question, $("asof").value);
      $("examples").appendChild(b);
    });
  } catch { /* examples are optional */ }
  if (!$("examples").children.length) $("exbox").hidden = true;
  $("askform").onsubmit = (e) => { e.preventDefault(); ask($("question").value, $("asof").value); };
  $("question").addEventListener("keydown", (e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); ask($("question").value, $("asof").value); } });
  $("question").addEventListener("input", () => { if (!$("askerr").hidden) { $("askerr").hidden = true; $("question").removeAttribute("aria-invalid"); } });
  $("reask").onclick = () => ask(state.lastQuestion, state.lastAsOf, true);
  $("replaynext").onclick = async () => {
    const b = $("replaynext");
    b.disabled = true; b.setAttribute("aria-busy", "true");
    $("replayout").textContent = "Publishing the next real change…";
    try {
      const r = await api(`/api/demo/replay-next?graph_name=${encodeURIComponent(state.graph)}`, { method: "POST" });
      $("replayout").textContent = r.published ? `Replayed real change: ${r.published.kep} ${r.published.from} → ${r.published.to} (${r.remaining} left)` : r.note;
    } catch (err) { $("replayout").textContent = `Replay failed: ${err.message}`; }
    finally { b.disabled = false; b.removeAttribute("aria-busy"); }
  };
  $("actbrief").onclick = () => state.kep && runAction("draft_decision_brief", { kep: state.kep }, $("actbrief"));
  $("actreviewers").onclick = () => state.kep && runAction("recommend_reviewers", { kep_or_path: state.kep }, $("actreviewers"));
  connectFeed();
  refreshHealth();
  setInterval(refreshHealth, 5000);
}
init();
