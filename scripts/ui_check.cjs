// Browser check of the demo flow at 1366x768 (Playwright, headless Chromium), then the theme switch and the layout at
// 1440/1024/768/390 px in light and dark. Saves screenshots to docs/img/.
// Needs: the web app on :8000, the brain_demo worker running, brain_demo freshly reset (scripts/reset_demo.sh).
// Run: NODE_PATH=<dir with node_modules/playwright> node scripts/ui_check.cjs
const { chromium } = require("playwright");
const path = require("path");
const OUT = path.join(__dirname, "..", "docs", "img");
const URL = process.env.UI_URL || "http://127.0.0.1:8000/";
const check = (ok, msg) => { console.log(`${ok ? "PASS" : "FAIL"} ${msg}`); if (!ok) process.exitCode = 1; };

(async () => {
  const browser = await chromium.launch();
  const page = await browser.newPage({ viewport: { width: 1366, height: 768 } });
  const errors = [];
  page.on("pageerror", (e) => errors.push(e.message));
  page.on("console", (m) => { if (m.type() === "error") errors.push(`console: ${m.text()}`); });
  await page.goto(URL);
  await page.waitForFunction(() => window.decisionBrain && window.decisionBrain.graph === "brain_demo");
  await page.waitForSelector("#feeddot.on", { timeout: 20000 });
  check(true, "page loaded on the Demo graph, live feed connected");
  check(await page.isVisible("#about"), "About section shown on first visit");
  await page.waitForTimeout(600);
  await page.screenshot({ path: path.join(OUT, "ui-home.png") });
  await page.click("#aboutclose");
  check(!(await page.isVisible("#about")), "About section closes with Got it");

  // 1. ask
  await page.fill("#question", "Is KEP-2699 still planned?");
  await page.click("#askbtn");
  await page.waitForSelector("#graphcard .meta", { timeout: 120000 });
  const answer1 = await page.textContent("#graphcard .text");
  check(/implementable/i.test(answer1), `answer before the change says implementable: "${answer1.slice(0, 90)}…"`);
  check(!(await page.isVisible("#outdated")), "outdated banner is hidden before any change");
  const hrefs = await page.$$eval("#graphcard a.cite", (as) => as.map((a) => a.href));
  check(hrefs.length > 0 && hrefs.every((h) => h.startsWith("https://")), `${hrefs.length} citation links, all https`);
  const nodes = await page.evaluate(() => window.decisionBrain.cy.nodes().length);
  check(nodes > 0, `graph path rendered: ${nodes} nodes`);
  await page.click("#drawer summary");
  const queries = await page.$$eval("#trace pre", (p) => p.length);
  check(queries > 0, `Cypher drawer lists ${queries} queries`);
  await page.click("#pathtext summary");
  await page.click("#pathlist button");
  check(await page.isVisible("#nodeinfo dl"), "node inspector opens from the path-as-text list");
  await page.waitForTimeout(1200);
  await page.screenshot({ path: path.join(OUT, "ui-answer.png") });
  await page.click("#drawer summary");

  // 2. a real change arrives (replayed): feed row, node flash, outdated banner
  await page.click("#replaynext");
  await page.waitForSelector("#feed li", { timeout: 30000 });
  const row = await page.textContent("#feed li");
  check(/KEP-2699 status: implementable → withdrawn/.test(row), `feed shows: "${row.slice(0, 80)}"`);
  await page.waitForSelector("#outdated:not([hidden])", { timeout: 30000 });
  check(true, "answer-outdated banner appeared for the current question");
  await page.screenshot({ path: path.join(OUT, "ui-outdated.png") });

  // 3. re-ask: banner clears, answer now says withdrawn
  await page.click("#reask");
  await page.waitForFunction(() => document.querySelector("#outdated").hidden, null, { timeout: 120000 });
  await page.waitForSelector("#graphcard .meta", { timeout: 120000 });
  const answer2 = await page.textContent("#graphcard .text");
  check(/withdrawn/i.test(answer2), `answer after re-ask says withdrawn: "${answer2.slice(0, 90)}…"`);
  check(!(await page.isVisible("#outdated")), "banner cleared after re-ask");
  const kepColor = await page.evaluate(() => window.decisionBrain.cy.nodes().filter((n) => n.data("key") === "KEP-2699").data("color"));
  check(kepColor === "#ff3b30", `KEP-2699 node is red (not adopted) after re-ask: ${kepColor}`);
  await page.waitForTimeout(1200);
  await page.screenshot({ path: path.join(OUT, "ui-reask.png") });

  // 4. compare with vector search on a made-up topic
  await page.click("label.toggle");   // the switch: users click the label, the checkbox itself is visually hidden
  check(await page.isChecked("#compare"), "compare switch turns on");
  await page.fill("#question", "Why was blockchain-based pod billing rejected?");
  await page.click("#askbtn");
  await page.waitForSelector("#basecard .meta", { timeout: 120000 });
  await page.waitForSelector("#graphcard .meta", { timeout: 120000 });
  const g = await page.textContent("#graphcard .text"), b = await page.textContent("#basecard .text");
  check(/not in the (knowledge )?graph|is(n't| not) in the|has no |no (kep|record|such|decision|match)|can't find|cannot find|could not find|nowhere|not found|does(n't| not) exist/i.test(g),
        `graph answer on the lure: "${g.slice(0, 120)}…"`);
  console.log(`INFO baseline answer on the lure: "${b.slice(0, 100)}…"`);
  check(await page.isVisible("#evidence"), "evidence comparison panel shown");
  await page.waitForTimeout(800);
  await page.screenshot({ path: path.join(OUT, "ui-compare.png") });

  // 5. trace drawer is reachable in the answer column
  check(await page.isVisible("#drawer summary"), "“How I got this” sits under the answer");

  // 6. appearance: explicit dark restyles the page and the graph, survives a reload, System clears it
  const labelColor = () => page.evaluate(() => window.decisionBrain.cy.style().json().find((r) => r.selector === "node").style.color);
  const lightLabel = await labelColor();
  await page.click("#themepick label[title='Dark']");
  check(await page.getAttribute("html", "data-theme") === "dark", "dark theme applied");
  const darkLabel = await labelColor();
  check(darkLabel !== lightLabel, `graph labels restyled for dark: ${lightLabel} → ${darkLabel}`);
  await page.screenshot({ path: path.join(OUT, "ui-dark.png") });
  await page.reload();
  await page.waitForFunction(() => window.decisionBrain && window.decisionBrain.graph);
  check(await page.getAttribute("html", "data-theme") === "dark" && await page.isChecked("#themepick input[value=dark]"),
        "dark theme persists across reload");
  await page.click("#themepick label[title='Follow system']");
  check(await page.getAttribute("html", "data-theme") === null, "System removes the explicit theme");

  // 7. layout at four widths, both schemes: no horizontal scroll; tabs replace the columns on phones
  for (const scheme of ["light", "dark"]) {
    await page.emulateMedia({ colorScheme: scheme });
    for (const [w, h] of [[1440, 900], [1024, 768], [768, 1024], [390, 844]]) {
      await page.setViewportSize({ width: w, height: h });
      await page.waitForTimeout(250);
      const over = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
      check(over <= 0, `${scheme} ${w}px: no horizontal overflow (${over})`);
      await page.screenshot({ path: path.join(OUT, `ui-${scheme}-${w}.png`) });
    }
  }
  await page.emulateMedia({ colorScheme: "light" });
  check(await page.isVisible(".tabs") && !(await page.isVisible("#panel-graph")), "390px: tab bar shown, one panel at a time");
  await page.click("#tab-graph");
  await page.waitForTimeout(300);
  const cyW = await page.evaluate(() => window.decisionBrain.cy.width());
  check(await page.isVisible("#panel-graph") && !(await page.isVisible("#panel-answer")) && cyW > 0, `390px: Graph tab shows the graph (${cyW}px wide)`);
  await page.screenshot({ path: path.join(OUT, "ui-390-graph.png") });

  check(errors.length === 0, `no page or console errors${errors.length ? ": " + errors.join(" | ") : ""}`);
  await browser.close();
})().catch((e) => { console.error("FAIL", e.message); process.exit(1); });
