// AI feature suite — demo account only; needs the `claude` CLI installed.
//   rm -f ~/.fused-mail/demo_state.json && node tests/e2e-ai.js
//
// Needs `bunx playwright install chromium` once. Playwright resolves its own
// browser; PLAYWRIGHT_MODULE / CHROMIUM_PATH / APP_PATH override the defaults.
//
// Covers the AI layer (summary, suggested reply, landing briefing) plus the
// two chrome features that ship with it: the theme toggle and the list's
// hover quick actions. Makes ~4 real model calls, so give it a few minutes.
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || "playwright");
const SHELL = process.env.CHROMIUM_PATH || undefined;
const PORT = process.env.PORT || "8865";
const MAIL = process.env.APP_PATH ||
  require("path").resolve(__dirname, "..", "index.html");
// ?account=demo — the demo mailbox no longer renders in the sidebar, and
// without the param the app would open the first REAL account.
const URL = `http://127.0.0.1:${PORT}/embed${MAIL}?account=demo`;
let pass = 0, fail = 0;
const ok = (n, c) => c ? (pass++, console.log("PASS", n)) : (fail++, console.log("FAIL", n));

async function frame(page) {
  for (let i = 0; i < 600; i++) {
    const f = page.frames().find(fr => fr.url().includes("/render?path="));
    if (f) return f;
    await page.waitForTimeout(100);
  }
  throw new Error("no render frame");
}
// The briefing owns the reading pane whenever no thread is open; it resolves
// itself (cache or model) and flips #digest[data-ready] when it is done.
const digestReady = f => f.waitForFunction(() => {
  const p = document.querySelector("#digest");
  return p && p.dataset.ready === "1";
}, null, { timeout: 150000 });

(async () => {
  const browser = await chromium.launch(SHELL ? { executablePath: SHELL } : {});
  const page = await browser.newPage({ viewport: { width: 1400, height: 900 } });
  page.on("pageerror", e => console.log("[pageerror]", e.message.slice(0, 200)));
  await page.goto(URL);
  let f = await frame(page);
  await f.waitForSelector("#aitoggle", { timeout: 90000 });

  // ---------- AI off by default ----------
  ok("toggle present, off by default", !(await f.$eval("#aitoggle", e => e.classList.contains("on"))));
  // .land-hint only exists once renderLanding() has run — #placeholder alone is
  // also in the pre-boot markup, so waiting on it would race the first draw.
  await f.waitForSelector(".land-hint", { timeout: 30000 });
  ok("no briefing on landing while AI off", (await f.$$("#digest")).length === 0);
  ok("landing invites AI without nagging",
     /Turn on AI features for a catch-up briefing here/.test(await f.textContent("#landing")));

  await f.click('.row[data-id="demo-t1"]');
  await f.waitForSelector("#threadhead h2", { timeout: 30000 });
  ok("no summary panel while AI off", (await f.$$("#summary")).length === 0);
  ok("no suggestion panel while AI off", (await f.$$("#suggest")).length === 0);
  ok("digest icon hidden while AI off", await f.$eval("#digestbtn", e => e.hidden));

  // ---------- consent ----------
  await f.click("#aitoggle");
  await f.waitForSelector("#ai-ok", { timeout: 10000 });
  const modalTxt = await f.textContent(".modal");
  ok("consent names Claude Code CLI", /Claude Code CLI/.test(modalTxt));
  ok("consent discloses sending to Anthropic", /sent to Anthropic/.test(modalTxt));
  await f.click("#ai-ok");
  ok("toggle on after consent", await f.$eval("#aitoggle", e => e.classList.contains("on")));

  // ---------- summary ----------
  await f.waitForSelector("#summary", { timeout: 15000 });
  await f.waitForFunction(() => {
    const b = document.querySelector("#summary .sum-body");
    return b && b.textContent.trim().length > 20 && !b.querySelector(".dots");
  }, null, { timeout: 120000 });
  const sum = await f.textContent("#summary .sum-body");
  ok("summary generated", sum.trim().length > 20);
  ok("summary not an error", !(await f.$eval("#summary", e => e.classList.contains("err"))));
  console.log("   SUMMARY:", sum.trim().replace(/\n/g, " | ").slice(0, 170));
  ok("mentions thread content (Q3/GPU/budget)", /Q3|GPU|budget|Friday/i.test(sum));

  // cache: reopen thread → instant, no dots
  await f.click('.row[data-id="demo-t2"]');
  await f.waitForSelector("#threadhead h2", { timeout: 30000 });
  await f.click('.row[data-id="demo-t1"]');
  await f.waitForSelector("#summary .sum-body", { timeout: 30000 });
  await page.waitForTimeout(700);
  const cached = await f.textContent("#summary .sum-body");
  ok("cached summary returns instantly", cached.trim() === sum.trim());

  // ---------- suggested reply ----------
  await f.waitForFunction(() => {
    const b = document.querySelector("#suggest");
    return b && !b.hidden && b.dataset.ready === "1"
        && b.querySelector(".sug-body").textContent.trim().length > 30;
  }, null, { timeout: 120000 });
  const sug = (await f.textContent("#suggest .sug-body")).trim();
  ok("suggested reply auto-generates", sug.length > 30);
  console.log("   SUGGESTION:", sug.replace(/\n/g, " | ").slice(0, 170));
  ok("suggestion has no preamble", !/^(here('| i)s|sure|certainly|draft:)/i.test(sug));

  await f.press("body", "Tab");
  const taVal = (await f.$eval("#replytext", e => e.value)).trim();
  ok("Tab inserts suggestion into reply box", taVal === sug);
  ok("suggestion hides after accept", await f.$eval("#suggest", e => e.hidden));

  // ---------- landing briefing (auto, no click) ----------
  // Closing the thread returns to the landing state; the briefing must run by
  // itself — nothing below clicks a "generate" affordance.
  await f.click("#t-close");
  await f.waitForSelector("#digest", { timeout: 20000 });
  await digestReady(f);
  const digestTxt = await f.textContent("#digest .dg-body");
  console.log("   DIGEST:", digestTxt.trim().replace(/\n/g, " | ").slice(0, 200));
  ok("briefing runs automatically on landing", digestTxt.trim().length > 20);
  ok("briefing headline", (await f.textContent(".land-title")).includes("While you were away"));
  ok("briefing counts unread", /\d+ unread conversation/.test(await f.textContent(".land-sub")));
  ok("digest icon visible with AI on", await f.$eval("#digestbtn", e => !e.hidden));

  let items = await f.$$("#digest .dg-item");
  ok("digest has clickable thread items", items.length >= 1);
  const targetId = await items[0].getAttribute("data-id");
  await items[0].click();
  await f.waitForSelector("#threadhead h2", { timeout: 30000 });
  await page.waitForFunction(id => location.search.includes("thread=" + id), targetId, { timeout: 15000 });
  ok("digest item opens its thread", true);
  ok("briefing yields the pane to the thread", (await f.$$("#digest")).length === 0);

  // ✦ in the app bar closes the open thread and shows the briefing again
  await f.click("#digestbtn");
  await f.waitForSelector("#digest", { timeout: 20000 });
  await digestReady(f);
  ok("digest button returns to the briefing", (await f.textContent("#digest .dg-body")).trim().length > 20);

  // Regenerate: dataset.ready drops while it re-runs, then comes back
  await f.click("#digest .dg-redo");
  await f.waitForFunction(() => document.querySelector("#digest").dataset.ready === "", null, { timeout: 15000 });
  await digestReady(f);
  ok("Redo regenerates the briefing", (await f.textContent("#digest .dg-body")).trim().length > 20);

  // ---------- theme ----------
  const theme0 = await f.evaluate(() => document.documentElement.getAttribute("data-theme"));
  ok("theme attribute set before paint", theme0 === "light" || theme0 === "dark");
  await f.click("#themebtn");
  const theme1 = await f.evaluate(() => document.documentElement.getAttribute("data-theme"));
  ok("theme toggle flips data-theme", theme1 !== theme0 && (theme1 === "light" || theme1 === "dark"));
  ok("theme choice persisted", (await f.evaluate(() => localStorage.getItem("fused.mail.theme"))) === theme1);

  // ---------- persistence across reload ----------
  await page.reload();
  f = await frame(page);
  await f.waitForSelector("#aitoggle", { timeout: 60000 });
  ok("theme survives reload",
     (await f.evaluate(() => document.documentElement.getAttribute("data-theme"))) === theme1);
  ok("AI stays on after reload", await f.$eval("#aitoggle", e => e.classList.contains("on")));

  // ---------- hover quick actions ----------
  await f.waitForSelector(".row", { timeout: 60000 });
  const archiveId = await f.$eval(".row", r => r.dataset.id);
  await f.hover(`.row[data-id="${archiveId}"]`);
  await f.click(`.row[data-id="${archiveId}"] [data-act="archive"]`);
  // every triage rebuilds the list through a "Loading…" state, so "the row is
  // gone" is only meaningful once rows are back on screen.
  await f.waitForFunction(id => document.querySelectorAll(".row").length > 0
      && !document.querySelector(`.row[data-id="${id}"]`), archiveId, { timeout: 30000 });
  ok("hover quick action archives from the list", true);

  // read ⇄ unread, both directions, from the same hover strip
  const flipId = await f.$eval(".row", r => r.dataset.id);
  const wasUnread = await f.$eval(`.row[data-id="${flipId}"]`, r => r.classList.contains("unread"));
  await f.hover(`.row[data-id="${flipId}"]`);
  await f.click(`.row[data-id="${flipId}"] [data-act="${wasUnread ? "read" : "unread"}"]`);
  await f.waitForFunction(([i, want]) => {
    const r = document.querySelector(`.row[data-id="${i}"]`);
    return r && r.classList.contains("unread") === want;
  }, [flipId, !wasUnread], { timeout: 30000 });
  ok("hover quick action flips read/unread", true);

  // mark everything read with the quick action, then the briefing must say so
  const unreadIds = await f.$$eval(".row.unread", rs => rs.map(r => r.dataset.id));
  ok("quick actions offer mark-as-read on unread rows", unreadIds.length >= 1);
  for (const id of unreadIds) {
    await f.hover(`.row[data-id="${id}"]`);
    await f.click(`.row[data-id="${id}"] [data-act="read"]`);
    await f.waitForFunction(i => {
      if (!document.querySelectorAll(".row").length) return false;
      const r = document.querySelector(`.row[data-id="${i}"]`);
      return !r || !r.classList.contains("unread");
    }, id, { timeout: 30000 });
  }
  await f.click("#digestbtn");
  await f.waitForFunction(() => {
    const b = document.querySelector("#digest .dg-body");
    return b && b.textContent.includes("Nothing unread — you're caught up.");
  }, null, { timeout: 60000 });
  ok("caught-up state when nothing is unread", true);
  ok("caught-up headline", (await f.textContent(".land-title")).includes("All caught up"));

  // ---------- turning it off ----------
  await f.click("#aitoggle");
  ok("briefing gone when AI off", (await f.$$("#digest")).length === 0);
  // whichever threads survived the triage above — demo-t3 may have been archived
  const anyId = await f.$eval(".row", r => r.dataset.id);
  await f.click(`.row[data-id="${anyId}"]`);
  await f.waitForSelector("#threadhead h2", { timeout: 30000 });
  ok("summary gone when AI off", (await f.$$("#summary")).length === 0);

  await page.screenshot({ path: "/tmp/ai-final.png" });
  await browser.close();
  console.log(`\n${pass} pass / ${fail} fail`);
  process.exit(fail ? 1 : 0);
})().catch(e => { console.error("ERROR", e.message); process.exit(2); });
