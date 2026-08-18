// Mailbox regression suite — runs entirely against the built-in `demo` account,
// so it needs no credentials and touches no real mail.
//
//   node examples/mail/tests/e2e-mailbox.js            (server on :8865)
//   PORT=9000 node examples/mail/tests/e2e-mailbox.js
//
// Resets the demo inbox first: rm ~/.fused-mail/demo_state.json
const PW = "/Users/akshilthumar/.nvm/versions/node/v22.17.1/lib/node_modules/@playwright/mcp/node_modules/playwright";
const { chromium } = require(PW);
const SHELL = "/Users/akshilthumar/Library/Caches/ms-playwright/chromium_headless_shell-1234/chrome-headless-shell-mac-arm64/chrome-headless-shell";

const PORT = process.env.PORT || "8865";
const MAIL = "/Users/akshilthumar/Desktop/fused/fused-render-worktrees/mail-inbox/examples/mail/mail.html";
const BASE = `http://127.0.0.1:${PORT}/embed${MAIL}`;
// The demo mailbox is invisible chrome now — it never renders in the sidebar,
// and `?account=demo` is the backdoor the suites use to reach the provider.
// Without it the app opens the first REAL connected account, which these tests
// must never touch.
const URL = `${BASE}?account=demo`;

let pass = 0, fail = 0;
const ok = (n, c) => c ? (pass++, console.log("PASS", n)) : (fail++, console.log("FAIL", n));

// The page runs inside the shell's iframe — assertions must target that frame.
async function appFrame(page) {
  for (let i = 0; i < 600; i++) {
    const f = page.frames().find(fr => fr.url().includes("/render?path="));
    if (f) return f;
    await page.waitForTimeout(100);
  }
  throw new Error("render frame never appeared");
}

(async () => {
  const browser = await chromium.launch({ executablePath: SHELL });
  const page = await browser.newPage({ viewport: { width: 1400, height: 860 } });
  page.on("pageerror", e => console.log("[pageerror]", e.message.slice(0, 300)));

  await page.goto(URL);
  let f = await appFrame(page);
  await f.waitForSelector("#addacct", { timeout: 90000 });
  // Demo is a test backend, not a feature: no sidebar row, no highlighted row.
  ok("demo never shows in the sidebar",
     !/demo/i.test(await f.textContent("#accounts")) && (await f.$$(".acct.active")).length === 0);

  await f.waitForSelector(".row", { timeout: 60000 });
  ok("inbox rows = 6", (await f.$$(".row")).length === 6);
  ok("unread rows exist", (await f.$$(".row.unread")).length >= 2);

  await f.click('.row[data-id="demo-t1"]');
  await f.waitForSelector("#threadhead h2", { timeout: 30000 });
  ok("thread subject", (await f.textContent("#threadhead h2")).includes("Q3 planning"));
  ok("2 messages", (await f.$$(".msg")).length === 2);
  ok("first msg collapsed", (await f.$$(".msg.collapsed")).length === 1);
  ok("url has thread param", page.url().includes("thread=demo-t1"));

  await f.waitForFunction(() => !document.querySelector('.row[data-id="demo-t1"]').classList.contains("unread"), null, { timeout: 20000 });
  ok("row unread cleared after open", true);

  await f.fill("#replytext", "Sounds good, will review by Thursday.");
  await f.click("#replysend");
  await f.waitForFunction(() => document.querySelector("#toast").textContent === "Sent", null, { timeout: 30000 });
  ok("reply sent toast", true);

  await f.hover('.row[data-id="demo-t2"]');
  await f.click('.row[data-id="demo-t2"] .star');
  await f.waitForFunction(() => {
    const r = document.querySelector('.row[data-id="demo-t2"] .star');
    return r && r.classList.contains("on");
  }, null, { timeout: 20000 });
  ok("star toggled", true);

  await f.click('.row[data-id="demo-t4"]');
  await f.waitForSelector("#t-archive", { timeout: 30000 });
  await f.click("#t-archive");
  await f.waitForFunction(() => !document.querySelector('.row[data-id="demo-t4"]'), null, { timeout: 30000 });
  ok("archived thread left inbox", true);

  await f.fill("#search", "invoice");
  await f.press("#search", "Enter");
  await f.waitForFunction(() => document.querySelectorAll(".row").length === 1, null, { timeout: 30000 });
  ok("search filters to 1", true);
  await page.waitForFunction(() => location.search.includes("q=invoice"), null, { timeout: 15000 });
  ok("search in url", true);
  await f.press("#search", "Escape");
  await f.waitForFunction(() => document.querySelectorAll(".row").length > 1, null, { timeout: 30000 });

  await f.click('.row[data-id="demo-t2"]');
  await f.waitForSelector(".att", { timeout: 30000 });
  ok("attachment chip", (await f.textContent(".att")).includes("invoice-4821.pdf"));
  ok("html body iframe", (await f.$$("iframe[data-html-idx]")).length === 1);

  await f.click("#composebtn");
  await f.fill("#c-to", "test@example.com");
  await f.fill("#c-subj", "Hello from fused mail");
  await f.fill("#c-body", "This is a test send from the demo account.");
  await f.click("#c-send");
  await f.waitForFunction(() => document.querySelector("#toast").textContent === "Sent", null, { timeout: 30000 });
  ok("compose sent", true);
  await f.click('.lbl[data-id="SENT"]');
  await f.waitForFunction(() => [...document.querySelectorAll(".row .subj")].some(s => s.textContent.includes("Hello from fused mail")), null, { timeout: 30000 });
  ok("sent label shows composed mail", true);

  await page.goto(`${BASE}?account=demo&label=INBOX&thread=demo-t3`);
  f = await appFrame(page);
  await f.waitForSelector("#threadhead h2", { timeout: 60000 });
  ok("refresh restores thread", (await f.textContent("#threadhead h2")).includes("dinner"));

  // ---------- onboarding (no account connected) ----------
  // ?onboarding=1 is a test-only hook: this machine HAS a real account, and
  // faking "none connected" must not move anything under ~/.fused-mail.
  await page.goto(`${BASE}?onboarding=1`);
  f = await appFrame(page);
  await f.waitForSelector("#onboard:not([hidden])", { timeout: 60000 });
  const shell = await f.evaluate(() => ({
    list: document.querySelector("#listpane").hidden,
    thread: document.querySelector("#threadpane").hidden,
    labels: document.querySelector("#labels").hidden,
    compose: document.querySelector("#composebtn").hidden,
    accts: document.querySelectorAll("#accounts .acct").length,
    addacct: !!document.querySelector("#addacct"),
  }));
  ok("onboarding replaces both mailbox panes", shell.list && shell.thread);
  ok("onboarding sidebar offers only Add account",
     shell.labels && shell.compose && shell.accts === 0 && shell.addacct);
  const onbTxt = await f.textContent("#onboard");
  ok("onboarding copy", /Welcome to Mail/.test(onbTxt) && /everything stays on this machine/.test(onbTxt));
  await f.click("#onb-connect");
  await f.waitForSelector("#a-connect", { timeout: 15000 });
  ok("Connect Gmail opens the add-account modal", /app password/i.test(await f.textContent(".modal")));

  await browser.close();
  console.log(`\n${pass} pass / ${fail} fail`);
  process.exit(fail ? 1 : 0);
})().catch(e => { console.error("ERROR", e.message); process.exit(2); });
