// Decision Brain page. Safety rules: model and graph text is only ever set with textContent; links are built
// as elements whose href comes from the response's own `sources` list and must be http(s).
"use strict";

const $ = (id) => document.getElementById(id);
const state = { graph: null, frozen: false, replayMode: false, lastQuestion: null, lastAsOf: null,
                questionId: null, seen: new Set(), cy: null, es: null, lastPing: 0, kep: null };
window.decisionBrain = state;   // read-only handle for the browser check (scripts/ui_check.cjs)
const RELEASES = ["v1.37", "v1.36", "v1.35", "v1.34", "v1.33", "v1.32", "v1.31", "v1.30", "v1.29", "v1.28",
                  "v1.27", "v1.26", "v1.25", "v1.24", "v1.23", "v1.22", "v1.21", "v1.20"];

// ---------- small helpers ----------
function el(tag, text, cls) {
  const e = document.createElement(tag);
  if (text !== undefined && text !== null) e.textContent = String(text);
  if (cls) e.className = cls;
  return e;
}
function safeUrl(u) {
  try { const url = new URL(u); return (url.protocol === "https:" || url.protocol === "http:") ? url.href : null; }
  catch { return null; }
}
function link(u, text) {
  const href = safeUrl(u);
  if (!href) return el("span", text);
  const a = el("a", text); a.href = href; a.target = "_blank"; a.rel = "noopener noreferrer"; return a;
}
async function api(path, opts = {}) {
  const r = await fetch(path, { headers: { "Content-Type": "application/json" }, ...opts });
  const body = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(body.detail || `HTTP ${r.status}`);
  return body;
}
function fmtTime(iso) { return iso ? new Date(iso).toLocaleString() : "?"; }
function secs(a, b) { return a && b ? ((new Date(b) - new Date(a)) / 1000).toFixed(1) : null; }
function step(name) {
  const items = [...$("scenario").querySelectorAll("li")];
  const idx = items.findIndex((li) => li.dataset.step === name);
  items.forEach((li, i) => { li.classList.toggle("active", i === idx); li.classList.toggle("done", i < idx); });
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
        const a = link(sources[Number(m[1]) - 1], part); a.className = "cite"; target.appendChild(a);
      } else {
        target.appendChild(document.createTextNode(part.replace(/\*+/g, "")));   // drop markdown emphasis marks
      }
    }
  }
}

function renderCard(card, title, res) {
  card.hidden = false;
  card.replaceChildren(el("h3", title));
  if (!res || res.status === "skipped") { card.hidden = true; return; }
  if (res.status === "error") {
    card.appendChild(el("p", `This mode failed: ${res.error}`, "error"));
    return;
  }
  const text = el("div", null, "text");
  renderText(text, res.answer, res.sources || []);
  card.appendChild(text);
  const cited = res.citations || [];
  if (cited.length) {
    const ol = el("ol", null, "sources");
    for (const c of [...cited].sort((a, b) => a.n - b.n)) {   // only cited sources, keeping their [n] numbers
      const li = el("li");
      li.value = c.n;
      li.appendChild(link(c.url, c.url.replace(/^https:\/\/github\.com\//, "")));
      ol.appendChild(li);
    }
    card.appendChild(ol);
  } else {
    card.appendChild(el("p", "No sources cited.", "muted small"));
  }
  const meta = [];
  if (res.as_of_requested) {   // requested vs actually applied cutoff, only for historical questions
    meta.push(`as of requested: ${res.as_of_requested}`);
    meta.push(`applied: ${(res.as_of_applied || []).join("; ") || "none (no historical tool used)"}`);
  }
  if (res.data_as_of && (res.data_as_of.last_change || res.data_as_of.snapshot))
    meta.push(`graph current to ${res.data_as_of.last_change || res.data_as_of.snapshot}`);
  meta.push(`guard: ${res.guard}`);
  if (res.guard_problems && res.guard_problems.length) meta.push(`issues: ${res.guard_problems.length}`);
  if (res.tool_calls) meta.push(`tools: ${res.tool_calls.map((c) => c.name).join(", ") || "none"}`);
  meta.push(`$${(res.cost_usd || 0).toFixed(4)} · ${res.seconds ?? "?"} s`);
  if (res.replayed) meta.push(`replayed (recorded ${res.asked_at || "?"})`);
  card.appendChild(el("div", meta.join(" · "), "meta"));
}

function renderEvidence(g, b) {
  const box = $("evidence");
  const show = (g && g.status === "ok") || (b && b.status === "ok");
  box.hidden = !show;
  if (!show) return;
  box.replaceChildren(el("h4", "Why the answers can differ: the evidence each one used"));
  const cols = el("div", null, "cols");
  for (const [name, res] of [["Graph", g], ["Vector search", b]]) {
    const col = el("div");
    col.appendChild(el("div", name, "flag"));
    if (!res || res.status !== "ok") { col.appendChild(el("div", "not run", "muted")); cols.appendChild(col); continue; }
    const ev = res.evidence || [];
    const statusRows = ev.filter((e) => e.type === "status change").length;
    col.appendChild(el("div", `${ev.length} items · status-history facts: ${statusRows}`, "muted"));
    const counts = {};
    ev.forEach((e) => { counts[e.type] = (counts[e.type] || 0) + 1; });
    const ul = el("ul");
    Object.entries(counts).forEach(([t, n]) => ul.appendChild(el("li", `${t}: ${n}`)));
    const dated = ev.filter((e) => e.date).map((e) => e.date).sort();
    if (dated.length) ul.appendChild(el("li", `dates ${dated[0]} … ${dated[dated.length - 1]}`));
    col.appendChild(ul);
    cols.appendChild(col);
  }
  box.appendChild(cols);
}

function renderTrace(g, b) {
  const box = $("trace");
  box.replaceChildren();
  let n = 0;
  for (const [name, res] of [["Graph agent", g], ["Vector baseline", b]]) {
    if (!res || res.status !== "ok") continue;
    box.appendChild(el("div", name, "call"));
    (res.tool_calls || []).forEach((c) => box.appendChild(el("div", `tool ${c.name}(${JSON.stringify(c.args)}) · found=${c.found} · ${c.ms} ms`, "q-meta")));
    (res.cypher_trace || []).forEach((q) => {
      n++;
      box.appendChild(el("div", `${q.tool} · ${q.rows} rows · ${q.ms} ms · params ${JSON.stringify(q.params)}`, "q-meta"));
      box.appendChild(el("pre", q.cypher));
    });
  }
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
function initCy() {
  state.cy = cytoscape({
    container: $("cy"), wheelSensitivity: 0.3,
    style: [
      { selector: "node", style: { "background-color": "data(color)", label: "data(label)", "font-size": 10,
          "font-family": "-apple-system, BlinkMacSystemFont, sans-serif", "text-wrap": "ellipsis", "text-max-width": 120,
          "text-margin-y": -4, width: 16, height: 16, color: cssVar("--ink") || "#1d1d1f",
          "border-width": 2, "border-color": cssVar("--panel") || "#fff" } },
      { selector: "node.kep", style: { width: 28, height: 28, "font-weight": 600, "font-size": 11 } },
      { selector: "edge", style: { width: 1.2, "line-color": cssVar("--other") || "#aeaeb2",
          "target-arrow-color": cssVar("--other") || "#aeaeb2", "target-arrow-shape": "triangle", "arrow-scale": 0.8,
          "curve-style": "bezier", label: "data(label)", "font-size": 8, color: cssVar("--muted") || "#6e6e73",
          "text-rotation": "autorotate", "text-background-color": cssVar("--soft") || "#f2f2f7", "text-background-opacity": 1,
          "text-background-padding": 2 } },
      { selector: "edge.past", style: { "line-style": "dashed" } },
      { selector: "node.flash", style: { "border-width": 7, "border-color": "#ffd60a" } },
      { selector: "node:selected", style: { "border-width": 3, "border-color": "#0071e3" } },
    ],
  });
  state.cy.on("tap", "node", (e) => showNode(e.target.data()));
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
  edges.filter((e) => ids.has(String(e.src)) && ids.has(String(e.dst))).forEach((e) => els.push({
    data: { id: "e" + e.id, source: String(e.src), target: String(e.dst), label: e.type, props: e.props },
    classes: (e.props && (e.props.valid_to || e.props.former)) ? "past" : "" }));
  state.cy.elements().remove();
  state.cy.add(els);
  state.cy.layout({ name: "cose", animate: true, animationDuration: 600, nodeRepulsion: 9000, idealEdgeLength: 70,
                    fit: true, padding: 20 }).run();
  $("pathinfo").textContent = nodes.length ? `${nodes.length} nodes · ${els.length - nodes.length} edges` : "(no graph path for this answer)";
  const kep = nodes.find((n) => n.label === "KEP");
  state.kep = kep ? kep.key : null;
}
function showNode(d) {
  const box = $("nodeinfo");
  box.hidden = false;
  box.replaceChildren(el("b", `${d.type}: ${d.name || d.key}`));
  if (d.status) box.appendChild(el("div", `status: ${d.status}`));
  if (d.url) { const p = el("div"); p.appendChild(link(d.url, d.url)); box.appendChild(p); }
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
async function ask(question, asOf, isReask = false) {
  question = (question || "").trim();
  if (!question) return;
  state.lastQuestion = question; state.lastAsOf = asOf || null;
  $("question").value = question;
  const compare = $("compare").checked;
  $("askbtn").disabled = true;
  $("outdated").hidden = true;
  $("answers").classList.toggle("two", compare);
  $("graphcard").hidden = false;
  $("graphcard").replaceChildren(el("h3", "Graph agent"), el("p", "Thinking: choosing graph tools…", "loading"));
  $("basecard").hidden = !compare;
  if (compare) $("basecard").replaceChildren(el("h3", "Vector search only"), el("p", "Retrieving passages…", "loading"));
  step(isReask ? "reask" : "ask");
  $("exbox").open = false;
  try {
    const res = await api("/api/ask", { method: "POST", body: JSON.stringify({
      question, graph: state.graph, mode: compare ? "both" : "graph", as_of: asOf || null }) });
    state.questionId = res.graph && res.graph.question_id;
    renderCard($("graphcard"), "Graph agent", res.graph);
    renderCard($("basecard"), "Vector search only", res.baseline);
    renderEvidence(res.graph, compare ? res.baseline : null);
    renderTrace(res.graph, res.baseline);
    renderPath(res.graph && res.graph.status === "ok" ? res.graph.path : null);
    $("actions").hidden = !(state.kep && !state.frozen);
    $("actout").textContent = "";
    step(isReask ? "reask" : compare ? "compare" : "inspect");
  } catch (err) {
    $("graphcard").replaceChildren(el("h3", "Graph agent"), el("p", `Request failed: ${err.message}`, "error"));
  } finally {
    $("askbtn").disabled = false;
  }
}

// ---------- live feed ----------
function addFeedRow(n) {
  if (n.event_id) { if (state.seen.has(n.event_id)) return; state.seen.add(n.event_id); }
  const li = el("li", null, "new");
  if (n.type === "rationale_updated") {
    li.appendChild(el("span", "llm", "src"));
    li.appendChild(el("span", `Explanation of ${n.kep} re-checked (${n.verified ? "quotes verified" : "some quotes failed"})`));
  } else {
    li.appendChild(el("span", n.source || "event", "src"));
    li.appendChild(n.url ? link(n.url, n.summary) : el("span", n.summary));
    const when = [`happened ${fmtTime(n.occurred_at)}`];
    if (n.lag_ms !== null && n.lag_ms !== undefined) when.push(`applied ${(n.lag_ms / 1000).toFixed(2)} s after receipt`);
    li.appendChild(el("div", when.join(" · "), "when"));
  }
  $("feed").prepend(li);
  while ($("feed").children.length > 60) $("feed").lastChild.remove();
  flash(n.touched || []);
  if (n.type === "event") step("change");
  if ((n.outdated_questions || []).includes(state.questionId)) {
    $("outdatedtext").textContent = `This answer is outdated: ${n.summary}`;
    $("outdated").hidden = false;
    step("outdated");
  }
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
    if (h.frozen) parts.push(`Frozen snapshot, ${day(h.snapshot_cutoff)}`);
    else if (h.last_change) {
      parts.push(`Last change ${new Date(h.last_change).toLocaleString(undefined, { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" })}`);
      if (h.lag_ms !== null && h.lag_ms !== undefined) parts.push(`applied in ${(h.lag_ms / 1000).toFixed(2)} s`);
    } else parts.push(`Snapshot ${day(h.snapshot_cutoff)}, waiting for changes`);
    parts.push(`${h.nodes.toLocaleString()} nodes`);
    $("fresh").textContent = parts.join(" · ");
    $("replaynext").hidden = state.graph !== "brain_demo";
    $("scenario").hidden = state.graph !== "brain_demo";
  } catch (err) { $("fresh").textContent = `health unavailable: ${err.message}`; }
}

// ---------- actions ----------
async function runAction(name, args) {
  $("actout").textContent = "Working…";
  try {
    const r = await api(`/api/act/${name}?graph_name=${encodeURIComponent(state.graph)}`, { method: "POST", body: JSON.stringify(args) });
    if (name === "draft_decision_brief") $("actout").textContent = r.data.markdown || "No brief.";
    else if (name === "recommend_reviewers") $("actout").textContent = (r.data.reviewers || [])
      .map((p) => `@${p.login} (${p.score}): ${p.reasons.join("; ")}`).join("\n") || "No reviewers found.";
  } catch (err) { $("actout").textContent = `Action failed: ${err.message}`; }
}

// ---------- setup ----------
function setAbout(open) {
  $("about").hidden = !open;
  try { localStorage.setItem("db.about", open ? "open" : "closed"); } catch { /* storage may be unavailable */ }
}

async function init() {
  let aboutPref = null;
  try { aboutPref = localStorage.getItem("db.about"); } catch { /* ignore */ }
  setAbout(aboutPref !== "closed");
  $("aboutclose").onclick = () => setAbout(false);
  $("abouttoggle").onclick = () => setAbout($("about").hidden);
  initCy();
  const h = await api("/api/health").catch(() => ({ graphs: ["brain_demo"] }));
  const pick = $("graph");
  const labels = { brain_live: "Live: follows Kubernetes", brain_demo: "Demo: sandbox webhook", brain_eval: "Frozen: 15 Sep 2026" };
  (h.graphs || []).forEach((g) => { const o = el("option", labels[g] || g); o.value = g; pick.appendChild(o); });
  state.graph = (h.replay_mode || h.public_mode) ? "brain_eval"
    : ((h.graphs || []).includes("brain_demo") ? "brain_demo" : h.graphs[0]);
  pick.value = state.graph;
  pick.onchange = () => { state.graph = pick.value; state.seen.clear(); $("feed").replaceChildren(); connectFeed(); refreshHealth(); };
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
  $("askform").onsubmit = (e) => { e.preventDefault(); ask($("question").value, $("asof").value); };
  $("question").addEventListener("keydown", (e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); ask($("question").value, $("asof").value); } });
  $("reask").onclick = () => ask(state.lastQuestion, state.lastAsOf, true);
  $("replaynext").onclick = async () => {
    try {
      const r = await api(`/api/demo/replay-next?graph_name=${encodeURIComponent(state.graph)}`, { method: "POST" });
      $("replayout").textContent = r.published ? `Replayed real change: ${r.published.kep} ${r.published.from} → ${r.published.to} (${r.remaining} left)` : r.note;
    } catch (err) { $("replayout").textContent = `Replay failed: ${err.message}`; }
  };
  $("actbrief").onclick = () => state.kep && runAction("draft_decision_brief", { kep: state.kep });
  $("actreviewers").onclick = () => state.kep && runAction("recommend_reviewers", { kep_or_path: state.kep });
  connectFeed();
  refreshHealth();
  setInterval(refreshHealth, 5000);
}
init();
