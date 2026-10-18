// Atlas: the whole graph as an Obsidian-style force view (teams, KEPs, code areas, people from GET /api/atlas).
// Safety rules as in app.js: graph text is only set with textContent, and links must be http(s).
// app.js owns the page and calls Atlas.show(graph) / hide() / reload(graph) / onNotice(notice) / restyle();
// "Ask about this" goes back through window.DB.ask(question).
"use strict";

window.Atlas = (() => {
  const $ = (id) => document.getElementById(id);
  const REDUCED = window.matchMedia("(prefers-reduced-motion: reduce)");
  const NARROW = window.matchMedia("(max-width: 860px)");
  const NOT_ADOPTED = ["withdrawn", "rejected", "replaced", "deferred"];
  const TYPE_NAME = { Team: "Team", KEP: "KEP", CodeArea: "Code area", Person: "Person" };
  const TYPE_ORDER = { Team: 0, KEP: 1, Person: 2, CodeArea: 3 };
  const LINK_DIST = { OWNS: 34, OWNS_CODE: 30, LEADS: 40, PARTICIPATES_IN: 90, AUTHORED: 46, APPROVES: 52,
                      APPROVES_CODE: 52, SUPERSEDES: 40, SEE_ALSO: 60 };
  // links inside a team pull hard and links across teams only a little, so each team forms its own cluster
  const LINK_PULL = { OWNS: 0.5, OWNS_CODE: 0.5, LEADS: 0.4, PARTICIPATES_IN: 0.02, AUTHORED: 0.06, APPROVES: 0.06,
                      APPROVES_CODE: 0.05, SUPERSEDES: 0.15, SEE_ALSO: 0.05 };
  const FONT = "-apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif";
  const PLAY_MS = 20000, SLIDER_MAX = 1000;

  const st = {
    fg: null, graph: null, data: null, loading: null, byId: new Map(), linksOf: new Map(), teamHue: new Map(),
    layers: { Team: true, KEP: true, CodeArea: true, Person: true }, colourBy: "team",
    t: null, tMin: 0, tMax: 0, maxRank: 1, selected: null, hover: null, lastQuery: "", hlNodes: new Set(), hlLinks: new Set(),
    pulses: new Map(), appear: new Map(), playing: false, playFrom: 0, playStart: 0, c: {}, refresh: null,
  };

  // ---------- helpers ----------
  function el(tag, text, cls) {
    const e = document.createElement(tag);
    if (text !== undefined && text !== null) e.textContent = text;
    if (cls) e.className = cls;
    return e;
  }
  function icon(name) {
    const ns = "http://www.w3.org/2000/svg", s = document.createElementNS(ns, "svg"), u = document.createElementNS(ns, "use");
    s.setAttribute("class", "icon"); s.setAttribute("aria-hidden", "true"); u.setAttribute("href", `#i-${name}`);
    s.appendChild(u);
    return s;
  }
  const safeUrl = (u) => /^https?:\/\//i.test(u || "") ? u : null;
  const idOf = (x) => (typeof x === "object" && x ? x.id : x);
  const css = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  const fmtDate = (ts) => new Date(ts * 1000).toLocaleDateString(undefined, { year: "numeric", month: "short" });
  const easeOutBack = (p) => 1 + 2.2 * Math.pow(p - 1, 3) + 1.2 * Math.pow(p - 1, 2);
  function fade(rgba, f) {   // the same colour with its alpha multiplied by f
    const m = /rgba?\(([^)]+)\)/.exec(rgba);
    if (!m) return rgba;
    const [r, g, b, a = "1"] = m[1].split(",").map((x) => x.trim());
    return `rgba(${r},${g},${b},${(parseFloat(a) * f).toFixed(3)})`;
  }
  function setStatus(text) { $("atlasstatus").textContent = text || ""; }

  // ---------- colour ----------
  function readTheme() {
    const c = st.c;
    c.bg = css("--atlas-bg"); c.link = css("--atlas-link"); c.linkHl = css("--atlas-link-hl");
    c.linkFaint = fade(c.link, 0.35); c.label = css("--atlas-label"); c.halo = css("--atlas-halo");
    c.sat = parseFloat(css("--atlas-sat")) || 70; c.light = parseFloat(css("--atlas-light")) || 50;
    c.glow = parseFloat(css("--atlas-glow")) || 0; c.flash = css("--g-flash");
    c.ok = css("--kep-ok"); c.mid = css("--kep-mid"); c.no = css("--kep-no");
    c.person = css("--person"); c.team = css("--team"); c.code = css("--code"); c.other = css("--other");
  }
  function teamColor(team) {
    const h = st.teamHue.get(team);
    return h === undefined ? st.c.other : `hsl(${h} ${st.c.sat}% ${st.c.light}%)`;
  }
  function statusAt(n) {
    if (n.type !== "KEP") return null;
    if (st.t === null) return n.status;
    const h = st.data.history[n.id];
    if (!h || !h.length) return n.status;
    let s = h[0][1];
    for (const [ts, v] of h) { if (ts <= st.t) s = v; else break; }
    return s;
  }
  function statusColor(s) {
    if (s === "implemented") return st.c.ok;
    if (NOT_ADOPTED.includes(s)) return st.c.no;
    return s ? st.c.mid : st.c.other;
  }
  function colorOf(n) {
    if (st.colourBy === "status") {
      if (n.type === "KEP") return statusColor(statusAt(n));
      return n.type === "Person" ? st.c.person : n.type === "Team" ? st.c.team : st.c.code;
    }
    return teamColor(n.team);
  }
  function recolor() {
    if (!st.data) return;
    for (const n of st.data.nodes) n.__c = colorOf(n);
    renderLegend();
    if (st.selected) renderCard(st.selected);
  }
  function renderLegend() {
    const box = $("atlaslegend");
    box.replaceChildren();
    if (st.colourBy === "status") {
      [["Implemented", st.c.ok], ["Proposed", st.c.mid], ["Not adopted", st.c.no]].forEach(([t, col]) => {
        const s = el("span", t, "swatch"); s.style.setProperty("--c", col); box.appendChild(s);
      });
      if (st.t !== null) box.appendChild(el("span", `as of ${fmtDate(st.t)}`));
    } else {
      box.appendChild(el("span", "Each colour is one team: a SIG, working group or committee, with its KEPs, code and people."));
    }
  }

  // ---------- visibility and time ----------
  function visible(n) {
    if (!st.layers[n.type]) return false;
    return st.t === null || n.__born <= st.t;
  }
  const linkVisible = (l) => visible(l.source) && visible(l.target);

  function sliderToTime(v) { return v >= SLIDER_MAX ? null : st.tMin + (v / SLIDER_MAX) * (st.tMax - st.tMin); }
  function setTime(v, animateNew) {
    const prev = st.t, t = sliderToTime(v);
    st.t = t;
    $("atlastime").value = String(v);
    $("atlasdate").textContent = t === null ? "Now" : fmtDate(t);
    if (animateNew && !REDUCED.matches && prev !== null) {
      const now = performance.now(), upTo = t === null ? Infinity : t;
      for (const n of st.data.nodes) if (n.__born > prev && n.__born <= upTo && st.layers[n.type]) st.appear.set(n.id, now);
    }
    if (st.selected && !visible(st.selected)) select(null);
    recolor();
    refreshAccessors();
  }
  function renderTicks() {
    const box = $("atlasticks");
    box.replaceChildren();
    const span = st.tMax - st.tMin;
    const inRange = (st.data.releases || []).filter(([, ts]) => ts > st.tMin && ts < st.tMax);
    const every = Math.max(1, Math.ceil(inRange.length / 8));
    inRange.filter((_, i) => i % every === 0).forEach(([v, ts]) => {
      const s = el("span", v);
      s.style.left = `${((ts - st.tMin) / span) * 100}%`;
      box.appendChild(s);
    });
  }
  function playIcon(on) {
    const b = $("atlasplay");
    b.replaceChildren(icon(on ? "pause" : "play"));
    b.setAttribute("aria-label", on ? "Pause history" : "Play history");
    b.title = on ? "Pause" : "Play history";
  }
  function togglePlay() {
    if (st.playing) { st.playing = false; playIcon(false); return; }
    let v = Number($("atlastime").value);
    if (v >= SLIDER_MAX) { v = 0; setTime(0, false); }
    st.playing = true; st.playFrom = v; st.playStart = performance.now();
    playIcon(true);
    const step = (now) => {
      if (!st.playing) return;
      const next = Math.min(SLIDER_MAX, st.playFrom + ((now - st.playStart) / PLAY_MS) * SLIDER_MAX);
      setTime(Math.round(next), true);
      if (next >= SLIDER_MAX) { st.playing = false; playIcon(false); return; }
      requestAnimationFrame(step);
    };
    requestAnimationFrame(step);
  }

  // ---------- highlight, selection, card ----------
  function setHighlight(n) {
    st.hlNodes.clear(); st.hlLinks.clear();
    if (n) {
      st.hlNodes.add(n.id);
      for (const l of st.linksOf.get(n.id) || []) {
        if (!linkVisible(l)) continue;
        st.hlLinks.add(l);
        st.hlNodes.add(idOf(l.source)); st.hlNodes.add(idOf(l.target));
      }
    }
    refreshAccessors();
  }
  function refreshAccessors() {   // force-graph caches particle counts per link; re-setting the accessor refreshes them
    if (st.fg) st.fg.linkDirectionalParticles(st.fg.linkDirectionalParticles());
  }
  function select(n, fly) {
    st.selected = n || null;
    setHighlight(st.selected);
    renderCard(st.selected);
    if (n && fly) {
      const target = n.type === "Team" ? 1.3 : 2.4;
      st.fg.centerAt(n.x, n.y, 700);
      if (st.fg.zoom() < target) st.fg.zoom(target, 700);
    }
  }
  function question(n) {
    switch (n.type) {
      case "KEP": return NOT_ADOPTED.includes(n.status) ? `Why was ${n.id} ${n.status}?` : `What is ${n.id} and why was it decided?`;
      case "Team": return `Who leads ${n.label} (${n.id}) and which KEPs does it own?`;
      case "CodeArea": return `Who owns the code in ${n.label}?`;
      default: return `What does ${n.id} work on, and which KEPs have they approved?`;
    }
  }
  function renderCard(n) {
    const card = $("atlascard");
    if (!n) { card.hidden = true; card.replaceChildren(); return; }
    card.replaceChildren();
    const head = el("div", null, "nihead");
    head.appendChild(el("span", TYPE_NAME[n.type] || n.type, "kind"));
    const close = el("button", null, "iconbtn");
    close.type = "button"; close.setAttribute("aria-label", "Close details"); close.appendChild(icon("close"));
    close.onclick = () => select(null);
    head.appendChild(close);
    card.appendChild(head);
    card.appendChild(el("h3", n.label));

    const dl = el("dl");
    const row = (k, v) => { if (v === null || v === undefined || v === "") return; dl.appendChild(el("dt", k)); const d = el("dd"); d.append(v); dl.appendChild(d); };
    if (n.type === "KEP") {
      const s = statusAt(n);
      const chip = el("span", s || "unknown", `chip ${s === "implemented" ? "ok" : NOT_ADOPTED.includes(s) ? "danger" : "warn"}`);
      row(st.t === null ? "Status" : `Status, ${fmtDate(st.t)}`, chip);
      if (n.born_ts) row("Created", fmtDate(n.born_ts));
    }
    if (n.type === "Team") row("Kind", n.kind ? n.kind.toUpperCase() : null);
    if (n.type === "Team" && n.retired) row("State", "retired");
    if (n.type === "Person" && n.role) row("Leads", `${(st.byId.get(n.team) || {}).label || n.team} (${n.role.replace("_", " ")})`);
    if (n.type !== "Team" && n.team) row("Team", (st.byId.get(n.team) || {}).label || n.team);
    row("Connections", String(new Set((st.linksOf.get(n.id) || []).map((l) => `${idOf(l.source)}|${idOf(l.target)}`)).size));
    card.appendChild(dl);

    const actions = el("div", null, "row");
    const askBtn = el("button", "Ask about this", "btn primary small");
    askBtn.type = "button";
    askBtn.onclick = () => window.DB && window.DB.ask(question(n));
    actions.appendChild(askBtn);
    const url = safeUrl(n.url);
    if (url) {
      const a = el("a", "Open on GitHub", "btn small");
      a.href = url; a.target = "_blank"; a.rel = "noopener noreferrer";
      actions.appendChild(a);
    }
    card.appendChild(actions);

    const byOther = new Map();   // one row per neighbour, with every relation to it
    for (const l of (st.linksOf.get(n.id) || []).filter(linkVisible)) {
      const other = idOf(l.source) === n.id ? l.target : l.source;
      const o = typeof other === "object" ? other : st.byId.get(other);
      if (!o) continue;
      if (!byOther.has(o.id)) byOther.set(o.id, { o, rels: [] });
      byOther.get(o.id).rels.push(l.type.toLowerCase().replace(/_/g, " "));
    }
    const neigh = [...byOther.values()].sort((a, b) => (TYPE_ORDER[a.o.type] - TYPE_ORDER[b.o.type]) || a.o.label.localeCompare(b.o.label));
    if (neigh.length) {
      card.appendChild(el("h4", `Connected to (${neigh.length})`));
      const ul = el("ul", null, "neigh");
      neigh.slice(0, 60).forEach(({ o, rels }) => {
        const li = el("li");
        li.appendChild(el("span", [...new Set(rels)].join(", "), "rel"));
        const b = el("button", o.label);
        b.type = "button";
        b.onclick = () => select(o, true);
        li.appendChild(b);
        ul.appendChild(li);
      });
      if (neigh.length > 60) ul.appendChild(el("li", `and ${neigh.length - 60} more`, "muted"));
      card.appendChild(ul);
    }
    card.hidden = false;
  }

  // ---------- drawing ----------
  function drawNode(n, ctx, k) {
    const now = performance.now(), c = st.c;
    let r = n.__r;
    const t0 = st.appear.get(n.id);
    if (t0 !== undefined) {
      const p = (now - t0) / 450;
      if (p >= 1) st.appear.delete(n.id); else r *= Math.max(0.05, easeOutBack(p));
    }
    const dim = st.hlNodes.size > 0 && !st.hlNodes.has(n.id);
    const alpha = dim ? 0.12 : 1;
    ctx.globalAlpha = alpha;
    const col = n.__c;
    ctx.beginPath();
    if (n.type === "CodeArea") {
      const d = r * 1.25;
      ctx.moveTo(n.x, n.y - d); ctx.lineTo(n.x + d, n.y); ctx.lineTo(n.x, n.y + d); ctx.lineTo(n.x - d, n.y); ctx.closePath();
      ctx.fillStyle = col; ctx.fill();
    } else if (n.type === "Person") {
      ctx.arc(n.x, n.y, r, 0, 2 * Math.PI);
      ctx.fillStyle = c.bg; ctx.fill();
      ctx.lineWidth = Math.max(r * 0.55, 0.6); ctx.strokeStyle = col; ctx.stroke();
    } else {
      if (n.type === "Team" && !dim && c.glow) { ctx.shadowColor = col; ctx.shadowBlur = c.glow; }
      ctx.arc(n.x, n.y, r, 0, 2 * Math.PI);
      ctx.fillStyle = col; ctx.fill();
      ctx.shadowBlur = 0;
      if (n.type === "Team") {
        ctx.beginPath(); ctx.arc(n.x, n.y, r + 2.5, 0, 2 * Math.PI);
        ctx.globalAlpha = alpha * 0.45; ctx.lineWidth = 1; ctx.strokeStyle = col; ctx.stroke();
        ctx.globalAlpha = alpha;
      }
    }
    if (st.selected && n.id === st.selected.id) {
      ctx.beginPath(); ctx.arc(n.x, n.y, r + 4 / k + 1.5, 0, 2 * Math.PI);
      ctx.lineWidth = 2 / k; ctx.strokeStyle = c.linkHl; ctx.stroke();
    }
    const p0 = st.pulses.get(n.id);
    if (p0 !== undefined) {
      const p = (now - p0) / 1800;
      if (p >= 1) st.pulses.delete(n.id);
      else for (const lag of [0, 0.3]) {
        const q = p - lag;
        if (q <= 0) continue;
        ctx.beginPath(); ctx.arc(n.x, n.y, r + q * 34, 0, 2 * Math.PI);
        ctx.globalAlpha = (1 - q) * 0.9; ctx.lineWidth = 2.2 / k; ctx.strokeStyle = c.flash; ctx.stroke();
      }
      ctx.globalAlpha = alpha;
    }
    const lit = st.hlNodes.has(n.id);
    const focus = n.id === (st.selected || {}).id || n.id === (st.hover || {}).id;
    const showLabel = n.type === "Team" ? k > 0.3 || (k > 0.1 && (n.size || 0) >= 6)
      : focus ? true
      : lit ? (st.hlNodes.size <= 45 ? k > 0.6 : k > 3.2)
      : !dim && (k > (n.type === "CodeArea" ? 5 : 2.8));
    if (showLabel && !dim) {
      const team = n.type === "Team";
      const fs = (team ? 12.5 : 10.5) / k;
      let text = n.type === "KEP" && !lit ? n.id : n.label;
      if (text.length > 48) text = text.slice(0, 47) + "…";
      ctx.font = `${team ? 650 : 500} ${fs}px ${FONT}`;
      ctx.textAlign = "center"; ctx.textBaseline = "top";
      const y = n.y + r + (team ? 4 : 2) / k + (team ? 2.5 : 0);
      ctx.lineJoin = "round"; ctx.lineWidth = 3.2 / k; ctx.strokeStyle = c.halo;
      ctx.strokeText(text, n.x, y);
      ctx.fillStyle = c.label; ctx.fillText(text, n.x, y);
    }
    ctx.globalAlpha = 1;
  }
  // a gentle pull of every node toward its own team's hub (d3 force protocol: f(alpha) + initialize(nodes))
  function clusterForce() {
    let nodes = [];
    const f = (alpha) => {
      for (const n of nodes) {
        const h = n.__hub;
        if (!h || h === n) continue;
        n.vx += (h.x - n.x) * 0.035 * alpha;
        n.vy += (h.y - n.y) * 0.035 * alpha;
      }
    };
    f.initialize = (ns) => { nodes = ns; };
    return f;
  }
  function paintPointer(n, color, ctx) {
    ctx.fillStyle = color;
    ctx.beginPath(); ctx.arc(n.x, n.y, n.__r + 2, 0, 2 * Math.PI); ctx.fill();
  }
  // redraw every frame only while something moves (pulses, pop-ins, playback, particles); otherwise let it idle
  function needsFrames() {
    return st.pulses.size > 0 || st.appear.size > 0 || st.playing || (st.hlLinks.size > 0 && !REDUCED.matches);
  }

  // ---------- data ----------
  function prepare(data) {
    st.byId = new Map(data.nodes.map((n) => [n.id, n]));
    st.linksOf = new Map();
    for (const l of data.links) {
      for (const id of [idOf(l.source), idOf(l.target)]) {
        if (!st.linksOf.has(id)) st.linksOf.set(id, []);
        st.linksOf.get(id).push(l);
      }
    }
    const teams = data.nodes.filter((n) => n.type === "Team").sort((a, b) => a.id.localeCompare(b.id));
    st.teamHue = new Map(teams.map((t, i) => [t.id, Math.round((i * 137.508 + 200) % 360)]));
    st.maxRank = Math.max(1e-9, ...data.nodes.filter((n) => n.type === "Person").map((n) => n.size || 0));
    const firstTs = (n) => n.born_ts ?? ((data.history[n.id] || [])[0] || [])[0] ?? 0;
    for (const n of data.nodes) {
      n.__hub = n.team ? st.byId.get(n.team) : null;
      n.__born = n.type === "KEP" ? firstTs(n) : 0;
      n.__r = n.type === "Team" ? 6 + Math.sqrt(n.size || 0) * 1.7
        : n.type === "KEP" ? 2.2 + Math.sqrt(n.size || 0) * 0.75
        : n.type === "CodeArea" ? 1.6 + Math.sqrt(n.size || 0) * 0.45
        : 1.8 + 5 * Math.sqrt((n.size || 0) / st.maxRank);
    }
    // a person appears with their earliest KEP (people only linked to code are there from the start)
    for (const n of data.nodes) {
      if (n.type !== "Person") continue;
      const kepBorn = (st.linksOf.get(n.id) || []).map((l) => st.byId.get(idOf(idOf(l.source) === n.id ? l.target : l.source)))
        .filter((o) => o && o.type === "KEP").map((o) => o.__born);
      const hasCode = (st.linksOf.get(n.id) || []).some((l) => l.type !== "AUTHORED" && l.type !== "APPROVES");
      n.__born = kepBorn.length && !hasCode ? Math.min(...kepBorn) : 0;
    }
    const kepBorn = data.nodes.filter((n) => n.type === "KEP" && n.__born).map((n) => n.__born);
    st.tMin = kepBorn.length ? Math.min(...kepBorn) - 30 * 86400 : Date.now() / 1000 - 5 * 365 * 86400;
    st.tMax = Math.max(Date.now() / 1000, data.cutoff_ts || 0);
  }
  function seedPositions(data) {   // teams on a ring, everything else near its team: the physics then unfolds clusters
    const teams = data.nodes.filter((n) => n.type === "Team").sort((a, b) => (b.size || 0) - (a.size || 0));
    const R = 70 * Math.sqrt(Math.max(teams.length, 1)) + 120, anchor = new Map();
    teams.forEach((t, i) => {
      const a = (i / teams.length) * 2 * Math.PI;
      t.x = R * Math.cos(a); t.y = R * Math.sin(a);
      anchor.set(t.id, [t.x, t.y]);
    });
    for (const n of data.nodes) {
      if (n.type === "Team") continue;
      const [ax, ay] = anchor.get(n.team) || [0, 0];
      n.x = ax * 0.9 + (Math.random() - 0.5) * 140;
      n.y = ay * 0.9 + (Math.random() - 0.5) * 140;
    }
    return R;
  }
  function counts(data) {
    const c = {};
    for (const n of data.nodes) c[n.type] = (c[n.type] || 0) + 1;
    document.querySelectorAll("#atlas [data-count]").forEach((s) => { s.textContent = c[s.dataset.count] || 0; });
    $("atlascount").textContent = `${data.nodes.length.toLocaleString()} nodes · ${data.links.length.toLocaleString()} links`;
    const list = $("atlaslist");
    list.replaceChildren();
    for (const n of [...data.nodes].sort((a, b) => TYPE_ORDER[a.type] - TYPE_ORDER[b.type])) {
      const o = el("option"); o.value = n.type === "Team" ? `${n.label} (${n.id})` : n.label; list.appendChild(o);
    }
  }

  async function load(graphName) {
    st.graph = graphName;
    const myLoad = (st.loading = Symbol("load"));
    setStatus("Loading the graph…");
    select(null);
    let data;
    try {
      const r = await fetch(`/api/atlas?graph_name=${encodeURIComponent(graphName)}`);
      if (!r.ok) throw new Error(`${r.status} ${r.statusText}`);
      data = await r.json();
    } catch (err) {
      if (myLoad === st.loading) setStatus(`The graph could not be loaded (${err.message}).`);
      return;
    }
    if (myLoad !== st.loading) return;   // a newer load (another graph) started meanwhile
    st.data = data;
    prepare(data);
    const R = seedPositions(data);
    readTheme();
    recolor();
    counts(data);
    renderTicks();
    setTime(Number($("atlastime").value), false);
    st.fg.graphData({ nodes: data.nodes, links: data.links });
    const w = $("atlas").clientWidth, h = $("atlas").clientHeight;
    st.fg.centerAt(0, 0, 0).zoom(Math.min(w, h) / (2 * R + 260), 0);
    setStatus("");
    // once the clusters have formed, frame them (unless the visitor already picked something)
    setTimeout(() => { if (myLoad === st.loading && !st.selected) st.fg.zoomToFit(900, 50, visible); },
               REDUCED.matches ? 50 : 3000);
  }
  // after a live change: take new statuses and new nodes without disturbing the layout
  async function refreshData() {
    if (!st.data || !st.graph) return;
    let fresh;
    try { fresh = await (await fetch(`/api/atlas?graph_name=${encodeURIComponent(st.graph)}`)).json(); } catch { return; }
    if (!fresh || !fresh.nodes) return;
    const keyOf = (l) => `${idOf(l.source)}|${idOf(l.target)}|${l.type}`;
    const known = new Set(st.data.links.map(keyOf));
    const added = [];
    for (const n of fresh.nodes) {
      const old = st.byId.get(n.id);
      if (old) { old.status = n.status; old.born_ts = n.born_ts; old.size = n.size; continue; }
      const team = st.byId.get(n.team);
      n.x = (team ? team.x : 0) + (Math.random() - 0.5) * 40; n.y = (team ? team.y : 0) + (Math.random() - 0.5) * 40;
      st.data.nodes.push(n); added.push(n);
    }
    for (const l of fresh.links) if (!known.has(keyOf(l))) st.data.links.push(l);
    st.data.history = fresh.history;
    prepare(st.data);
    const now = performance.now();
    added.forEach((n) => st.appear.set(n.id, now));
    recolor();
    counts(st.data);
    if (added.length) st.fg.graphData({ nodes: st.data.nodes, links: st.data.links });
    else refreshAccessors();
  }

  // ---------- setup ----------
  function init() {
    readTheme();
    const fg = new ForceGraph($("atlascanvas"));
    st.fg = fg;
    fg.backgroundColor("rgba(0,0,0,0)")
      .nodeId("id")
      .nodeLabel(() => "")
      .nodeVal((n) => n.__r)
      .nodeVisibility(visible)
      .linkVisibility(linkVisible)
      .nodeCanvasObjectMode(() => "replace")
      .nodeCanvasObject(drawNode)
      .nodePointerAreaPaint(paintPointer)
      .linkColor((l) => (st.hlLinks.size ? (st.hlLinks.has(l) ? st.c.linkHl : st.c.linkFaint) : st.c.link))
      .linkWidth((l) => (st.hlLinks.has(l) ? 1.3 : 0.45))
      .linkDirectionalParticles((l) => (!REDUCED.matches && st.hlLinks.has(l) ? 2 : 0))
      .linkDirectionalParticleWidth(2.4)
      .linkDirectionalParticleSpeed(0.006)
      .linkDirectionalParticleColor((l) => (typeof l.source === "object" ? l.source.__c : st.c.linkHl))
      .d3AlphaDecay(0.018)
      .d3VelocityDecay(0.3)
      .cooldownTime(20000)
      .warmupTicks(REDUCED.matches ? 300 : 0)
      .cooldownTicks(REDUCED.matches ? 0 : Infinity)
      .onNodeHover((n) => {
        st.hover = n || null;
        $("atlascanvas").style.cursor = n ? "pointer" : "";
        setHighlight(n || st.selected);
      })
      .onNodeClick((n) => select(n, true))
      .onBackgroundClick(() => select(null));
    fg.d3Force("charge").strength((n) => (n.type === "Team" ? -420 : n.type === "Person" ? -26 : -34)).distanceMax(900);
    fg.d3Force("link").distance((l) => LINK_DIST[l.type] || 50).strength((l) => LINK_PULL[l.type] ?? 0.1);
    fg.d3Force("cluster", clusterForce());
    setInterval(() => { if (st.fg && !$("atlas").hidden) st.fg.autoPauseRedraw(!needsFrames()); }, 200);

    new ResizeObserver(() => {
      const box = $("atlas");
      if (!box.hidden) fg.width(box.clientWidth).height(box.clientHeight);
    }).observe($("atlas"));

    if (NARROW.matches) $("atlasctl").open = false;
    $("atlassearch").addEventListener("change", search);
    $("atlassearch").addEventListener("input", () => { st.lastQuery = ""; });
    $("atlassearch").addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); st.lastQuery = ""; search(); } });
    document.querySelectorAll("#atlas .atlas-layers input").forEach((cb) => {
      cb.onchange = () => {
        st.layers[cb.dataset.type] = cb.checked;
        if (st.selected && !visible(st.selected)) select(null); else setHighlight(st.selected);
      };
    });
    document.querySelectorAll("#atlascolour input").forEach((r) => {
      r.onchange = () => { if (r.checked) { st.colourBy = r.value; recolor(); } };
    });
    $("atlasfit").onclick = () => fg.zoomToFit(700, 50, visible);
    $("atlasshake").onclick = () => {
      for (const n of st.data ? st.data.nodes : []) { n.vx = (n.vx || 0) + (Math.random() - 0.5) * 14; n.vy = (n.vy || 0) + (Math.random() - 0.5) * 14; }
      fg.d3ReheatSimulation();
    };
    $("atlasplay").onclick = togglePlay;
    $("atlastime").max = String(SLIDER_MAX);
    $("atlastime").value = String(SLIDER_MAX);
    $("atlastime").oninput = () => { st.playing = false; playIcon(false); setTime(Number($("atlastime").value), false); };
    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape" && !$("atlas").hidden && st.selected) select(null);
    });
  }
  function search() {
    if (!st.data) return;
    const q = $("atlassearch").value.trim().toLowerCase();
    if (!q || q === st.lastQuery) return;
    st.lastQuery = q;
    const nodes = st.data.nodes;
    const exact = nodes.find((n) => n.id.toLowerCase() === q || n.label.toLowerCase() === q
                                  || `${n.label} (${n.id})`.toLowerCase() === q);
    const n = exact || [...nodes].sort((a, b) => TYPE_ORDER[a.type] - TYPE_ORDER[b.type])
      .find((x) => x.id.toLowerCase().includes(q) || x.label.toLowerCase().includes(q));
    if (!n) { setStatus(`Nothing called “${$("atlassearch").value.trim()}” in this graph.`); setTimeout(() => setStatus(""), 2500); return; }
    if (!st.layers[n.type]) {
      st.layers[n.type] = true;
      const cb = document.querySelector(`#atlas .atlas-layers input[data-type="${n.type}"]`);
      if (cb) cb.checked = true;
    }
    if (!visible(n)) { st.playing = false; playIcon(false); setTime(SLIDER_MAX, false); }
    select(n, true);
  }

  // ---------- public ----------
  async function show(graphName) {
    $("atlas").hidden = false;
    if (typeof ForceGraph === "undefined") { setStatus("The graph library did not load. Check the connection and reload the page."); return; }
    if (!st.fg) init();
    st.fg.width($("atlas").clientWidth).height($("atlas").clientHeight);
    st.fg.resumeAnimation();
    if (st.graph !== graphName || !st.data) await load(graphName);
  }
  function hide() {
    $("atlas").hidden = true;
    st.playing = false;
    if (st.fg) st.fg.pauseAnimation();
  }
  function reload(graphName) {
    if (!$("atlas").hidden && st.fg) load(graphName);
    else st.graph = null;   // load on the next show()
  }
  function onNotice(n) {
    if (!st.data || !st.fg) return;
    const now = performance.now();
    for (const id of n.touched || []) {
      if (!st.byId.has(id)) continue;
      st.pulses.set(id, now);
      if (!REDUCED.matches) (st.linksOf.get(id) || []).filter(linkVisible).forEach((l) => st.fg.emitParticle(l));
    }
    if (n.type === "event") {   // statuses or new KEPs may have changed: fetch again, once per burst
      clearTimeout(st.refresh);
      st.refresh = setTimeout(refreshData, 1500);
    }
  }
  function restyle() {
    readTheme();
    recolor();
    refreshAccessors();
  }
  return { show, hide, reload, onNotice, restyle };
})();
