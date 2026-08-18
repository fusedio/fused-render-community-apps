"use strict";
// AI triage board (the Manage view). Loaded by mail.html AFTER its main
// script, so every helper there ($, py, svg, state, caches...) already
// exists; everything here is invoked at event/draw time, never at parse.

// Triage board (its markup is rebuilt wholesale, so clicks delegate from the
// list pane, which is never replaced).
$("#listpane").addEventListener("click", e => {
  if (!e.target.closest("#mgboard")) return;
  if (e.target.closest(".mg-redo")) { runManage(true); return; }
  if (e.target.closest("#mgsettings")) { mg.settings = !mg.settings; paintManage(); return; }
  if (e.target.closest("#mgadd-blank")) {
    mgAddCategory("New category");
    return;
  }
  if (e.target.closest("#mgadd-ai")) {
    if (!aiOn || aiBroken) { toast("Turn on AI features to draft a category."); return; }
    // One tiny question — the name — then the AI drafts the description.
    const { ov, close } = modal(`
      <div class="modal" style="width:380px">
        <h3>Categorize with AI</h3>
        <p style="color:var(--fg-muted);margin:0 0 10px;font-size:12px">Name a category and AI drafts its triage rule — or leave it blank and AI suggests one from your inbox.</p>
        <input id="mgadd-name" spellcheck="false" placeholder="Category name — or leave blank for AI suggestion"
          style="width:100%;padding:9px 11px;border:1.5px solid rgba(128,128,128,.55);border-radius:8px;background:var(--bg)">
        <div class="actions">
          <button class="btn-secondary" id="mgadd-cancel">Cancel</button>
          <button class="btn-primary pill" id="mgadd-go">Draft</button>
        </div>
      </div>`);
    const inp = $("#mgadd-name", ov);
    $("#mgadd-go", ov).focus();   // Enter = draft/suggest right away; Tab back to name it
    const go = () => {
      const name = inp.value.trim();
      close();
      if (!name) { mgSuggestCategory(); return; }   // blank = suggest from the inbox
      // The card shows up at once, disabled, and fills in when the draft lands.
      mg.editBusy = true;
      const id = mgAddCategory(name);
      mgDraftDescription(id);
    };
    inp.addEventListener("keydown", ev => { if (ev.key === "Enter") go(); });
    $("#mgadd-cancel", ov).addEventListener("click", close);
    $("#mgadd-go", ov).addEventListener("click", go);
    return;
  }
  const sv = e.target.closest(".mgs-save");
  if (sv) {
    if (sv.disabled) return;
    const cat = mgCat(sv.dataset.cat), row = sv.closest(".mgs-row");
    if (cat && row) {   // edits live in the card's fields until this commit
      const name = $(".mgs-name", row).value.trim();
      const desc = $(".mgs-desc", row).value.trim();
      if (name) cat.name = name;
      if (desc !== (cat.prompt || "")) {
        cat.prompt = desc;
        try { localStorage.removeItem(MG_CACHE); } catch (err) {}   // old verdicts are stale
      }
      cat.dest = $(".mgs-dest", row).value;
      saveMgConfig();
      toast("Saved");
    }
    mg.editCat = null; paintManage(); return;
  }
  const dis = e.target.closest(".mgs-discard");
  if (dis) {
    if (mg.editNew) {   // discarding a never-saved category removes it
      mgConfig.categories = mgCats().filter(x => x.id !== dis.dataset.cat);
      saveMgConfig();
    }
    mg.editCat = null; paintManage(); return;
  }
  const ai = e.target.closest(".mgs-ai");
  if (ai) {
    const cat = mgCat(ai.dataset.cat), row = ai.closest(".mgs-row");
    if (!cat || !row) return;
    if (!aiOn || aiBroken) { toast("Turn on AI features to write descriptions."); return; }
    ai.disabled = true;   // the dimmed sparkle is the busy state
    const name = $(".mgs-name", row).value.trim() || cat.name;
    const cur = $(".mgs-desc", row).value.trim();
    (async () => {
      try {
        const text = await mgDescribe(cat.id, name, cur);
        // Into the card only — Save is what commits it.
        const ta = document.querySelector(`.mgs-desc[data-cat="${CSS.escape(cat.id)}"]`);
        if (ta && text) { ta.value = text; mgArmSave(); mgSizeDesc(); }
      } catch (err) {
        if (err.type === "ai_unavailable") aiBroken = true;
        toast(aiFailText(err));
      }
      const b = document.querySelector(`.mgs-ai[data-cat="${CSS.escape(cat.id)}"]`);
      if (b) b.disabled = false;
    })();
    return;
  }
  const card = e.target.closest(".mgs-row");
  if (card && !card.classList.contains("editing")) {   // click a card to open it
    if (e.target.closest(".mgs-dest")) return;   // the mini dropdown, not an open request
    mg.editCat = card.dataset.cat;
    mg.editNew = false;
    paintManage(); return;
  }
  const del = e.target.closest(".mgs-del");
  if (del) {
    const c = mgCat(del.dataset.cat);
    if (!c) return;
    const { ov, close } = modal(`
      <div class="modal" style="width:380px">
        <h3>Remove ${esc(c.name)}?</h3>
        <p style="color:var(--fg-muted);margin:0 0 8px">
          Its mail reclassifies into the remaining categories on the next run.
        </p>
        <div class="actions">
          <button class="btn-secondary" id="mgdel-cancel">Cancel</button>
          <button class="btn-primary pill btn-danger-solid" id="mgdel-ok">Remove</button>
        </div>
      </div>`);
    $("#mgdel-cancel", ov).addEventListener("click", close);
    $("#mgdel-ok", ov).addEventListener("click", () => {
      close();
      mgConfig.categories = mgCats().filter(x => x.id !== c.id);
      if (mg.editCat === c.id) mg.editCat = null;
      try { localStorage.removeItem(MG_CACHE); } catch (err) {}
      saveMgConfig(); paintManage();
      toast(`Removed ${c.name} — its mail reclassifies next run`);
    });
    return;
  }
  const tab = e.target.closest(".mg-tab");
  if (tab) { mg.tab = tab.dataset.id; mg.tabPicked = true; paintManage(); mgAutoPeek(); mgPrefetch(); return; }
  if (e.target.closest("#mgact")) { mgConfirm(); return; }
  if (e.target.closest("#mgkeep")) { mgKeep(); return; }
  if (e.target.closest("#mgtriage")) { mgTriage(); return; }
  if (e.target.closest("#mgmove")) { mgMoveMenu(); return; }
  if (e.target.closest("#mgmovemenu")) return;   // the menu handles its own clicks
  if (e.target.id === "mgall") {
    const rows = mgTabRows();
    if (e.target.checked) rows.forEach(t => mg.unchecked.delete(t.id));
    else rows.forEach(t => mg.unchecked.add(t.id));
    paintManage(); return;
  }
  const cb = e.target.closest(".mg-cb");
  if (cb) {   // checkbox first — its row would otherwise open the thread
    if (cb.checked) mg.unchecked.delete(cb.dataset.id);
    else mg.unchecked.add(cb.dataset.id);
    paintManage(); return;
  }
  // The same per-row triage actions as the inbox's hover strip.
  const mact = e.target.closest(".mg-row [data-act]");
  if (mact) {
    const id = mact.closest(".mg-row").dataset.id;
    const kind = mact.dataset.act;
    if (kind === "archive") return mgArchive(id);
    if (kind === "trash") return mgTrash(id);
    if (kind === "unread") return mgSetUnread(id, true, "Marked unread");
    if (kind === "read") return mgSetUnread(id, false, "Marked read");
    return;
  }
  // Clicking a row previews it and marks it read (auto-preview on a tab
  // switch stays read-only). A click on the checkbox's gutter is forwarded by
  // the label to the input and already handled in the .mg-cb branch above.
  if (e.target.closest(".mg-cbwrap") && !e.target.closest(".mg-cb")) return;
  const mrow = e.target.closest(".mg-row");
  if (mrow) {
    const t = mg.threads.find(x => x.id === mrow.dataset.id);
    if (t && t.unread) {   // optimistic, silent — same as opening from the inbox
      t.unread = false;
      const row = state.threads.find(x => x.id === t.id);   // keep the inbox list in step
      if (row) row.unread = false;
      paintManage();
      modOp(t.id, [], ["UNREAD"])
        .then(r => { if (r && r.error) throw new Error(r.error); })
        .catch(err => toast("Couldn't mark read — " + err.message));
    }
    if (mrow.dataset.id === mgPeekId) return;   // already showing
    clearTimeout(mgPeekTimer);
    mgPeekId = mrow.dataset.id;
    mgMarkPeek();
    mgPreview(mgPeekId);
    return;
  }
  // Nothing actionable was hit — a deliberate click on the board's empty
  // space hands the reading pane back to the catch-up landing.
  if (!P.thread && !e.target.closest("input, select, textarea, button, label")) mgPeekReset();
});

// Row-click preview state. The preview stays put until another row is clicked
// (or the board's empty space, which hands the pane back to the landing).
let mgPeekReq = 0, mgPeekId = null, mgPeekTimer = null;
async function mgPreview(id) {
  const rid = ++mgPeekReq;
  const hit = threadCache.get(threadKey(id));
  if (hit) { state.thread = hit; renderMgPreview(hit); return; }
  // Not prefetched — a centered spinner makes the fetch unmistakable.
  $("#threadpane").innerHTML = `<div class="pv-loading"><span class="ring" aria-label="Loading"></span></div>`;
  const acct = P.account;
  let res;
  try { res = await py({ op: "get_thread", account: acct, thread: id, mark_read: false }); }
  catch (e) { if (rid === mgPeekReq && P.manage && !P.thread) renderLanding(); return; }
  // Cache even if the user moved on — the next click must not refetch.
  if (!res.error) putThread(`${acct}|${res.id}`, res);
  if (rid !== mgPeekReq || !P.manage || P.thread) return;
  if (res.error) { renderLanding(); return; }   // never leave the spinner stranded
  state.thread = res;
  renderMgPreview(res);
}

// The preview is deliberately spare: one-line title, the AI summary, then just
// the LAST message — none of the thread view's toolbar/reply machinery.
function renderMgPreview(t) {
  const m = t.messages[t.messages.length - 1];
  const who = fromName(m.from), addr = addrOf(m.from);
  const body = m.body_html
    ? `<iframe sandbox="allow-same-origin" data-pv-html></iframe>`
    : esc(m.body_text);
  $("#threadpane").innerHTML = `
    <div id="mgpreview">
      <div class="pv-scroll">
        <div class="msg-head">
          <span class="avatar" style="--h:${hueOf(addr || who)}" aria-hidden="true">${esc(initialOf(who))}</span>
          <span class="mwho">
            <span class="mfrom">${esc(who)}</span>
            ${addr ? `<span class="maddr">${esc(addr)}</span>` : ""}
          </span>
          <span class="mpeek"></span>
          <span class="mdate">${fmtDate(m.date)}</span>
        </div>
        <div class="pv-title">${esc(t.subject)}</div>
        <div class="msg-body">${body}</div>
      </div>
      ${summaryHtml()}
    </div>`;
  const f = document.querySelector("iframe[data-pv-html]");
  if (f) hydrateMailFrame(f, m.body_html);
  if (aiOn && !aiBroken) runSummary(false);
}
// Warm every board thread in the background — visible tab's rows first —
// so click previews land instantly.
function mgPrefetch() {
  const tabIds = new Set(mgTabRows().map(t => t.id));
  warmThreads([...mgTabRows(), ...mg.threads.filter(t => !tabIds.has(t.id))]);
}
function mgPeekReset() {
  clearTimeout(mgPeekTimer);
  ++mgPeekReq;
  if (mgPeekId === null) return;
  mgPeekId = null;
  mgMarkPeek();
  if (P.manage && !P.thread) renderLanding();
}
// Keep the .peeking highlight in step with mgPeekId without a full repaint.
function mgMarkPeek() {
  document.querySelectorAll(".mg-row.peeking").forEach(r => r.classList.remove("peeking"));
  if (mgPeekId !== null) {
    const row = document.querySelector(`.mg-row[data-id="${CSS.escape(mgPeekId)}"]`);
    if (row) row.classList.add("peeking");
  }
}
// On a tab switch the first row previews itself, so the reading pane is never
// blank while the user decides what to click.
function mgAutoPeek() {
  if (!P.manage || P.thread) return;
  const row = document.querySelector("#mglist .mg-row");
  if (!row) { mgPeekReset(); return; }   // empty tab: back to the landing
  if (row.dataset.id === mgPeekId) return;
  clearTimeout(mgPeekTimer);
  mgPeekId = row.dataset.id;
  mgMarkPeek();
  mgPreview(mgPeekId);
}

// New category, opened for editing. Discard removes it again (editNew).
function mgAddCategory(name) {
  let i = 1; while (mgCat("custom" + i)) i++;
  const id = "custom" + i;
  // Newest first — the fresh card opens right under the New buttons.
  mgCats().unshift({ id, name, prompt: "", dest: "KEEP" });
  mg.editCat = id;
  mg.editNew = true;
  saveMgConfig(); paintManage();
  return id;
}
// One-sentence classifier description for a category, distinct from the rest.
async function mgDescribe(catId, name, cur) {
  const others = mgCats().filter(c => c.id !== catId)
    .map(c => `- ${c.name}: ${c.prompt || "(no description)"}`).join("\n");
  const res = await fused.ai(
    `Category name: ${name}\nCurrent description: ${cur || "(none)"}\nThe OTHER categories are:\n${others}`,
    { systemPrompt: "You write classifier instructions for an email triage system. Reply with ONE concise sentence (max 25 words) describing exactly which emails belong in the given category, clearly distinct from the other categories. Reply with the sentence only — no quotes, no preamble.", effort: "low" });
  return res.text.trim().replace(/^["']+|["']+$/g, "");
}
// Drafts the description for a freshly created category into its (busy,
// disabled) card. The draft lands in memory only — Save persists it, and
// Discard removes the whole category again.
async function mgDraftDescription(id) {
  const cat = mgCat(id);
  if (!cat) { mg.editBusy = false; return; }
  try {
    const text = await mgDescribe(id, cat.name, "");
    if (text) cat.prompt = text;
  } catch (err) {
    if (err.type === "ai_unavailable") aiBroken = true;
    toast(aiFailText(err));
  }
  mg.editBusy = false;
  if (mg.editCat === id && mg.settings) { paintManage(); mgArmSave(); }
}
// AI reads a sample of the inbox and proposes ONE category (name, description,
// destination) that the existing set misses. The card appears immediately,
// disabled while the model thinks, and fills in when it answers.
async function mgSuggestCategory() {
  if (!aiOn || aiBroken) { toast("Turn on AI features first."); return; }
  const threads = (mg.threads.length ? mg.threads : state.threads).slice(0, 40);
  if (!threads.length) { toast("No mail to learn from yet."); return; }
  mg.editBusy = true;
  const id = mgAddCategory("Suggesting…");
  const cats = mgCats().filter(c => c.id !== id)
    .map(c => `- ${c.name}: ${c.prompt || "(no description)"}`).join("\n");
  const ctx = threads.map(t =>
    `From ${fromName(t.from)} <${addrOf(t.from)}> — "${t.subject}": ${t.snippet}`).join("\n");
  const cat = mgCat(id);
  try {
    const res = await fused.ai(
      `Existing categories:\n${cats}\n\nRecent inbox mail:\n${ctx}`,
      { systemPrompt: "You design email triage categories. Propose ONE new category that captures a clear cluster of the mail below that the existing categories miss. Reply in EXACTLY this format, nothing else:\nNAME: <2-3 word name>\nDESC: <one sentence, max 25 words, describing which emails belong here>\nDEST: <one of TRASH, SPAM, ARCHIVE, KEEP>", effort: "low" });
    const name = /NAME:\s*(.+)/i.exec(res.text);
    const desc = /DESC:\s*(.+)/i.exec(res.text);
    const dest = /DEST:\s*(TRASH|SPAM|ARCHIVE|KEEP)/i.exec(res.text);
    if (cat && name && desc) {
      cat.name = name[1].trim();
      cat.prompt = desc[1].trim();
      if (dest) cat.dest = dest[1].toUpperCase();
    } else {
      toast("No usable suggestion came back — try again.");
    }
  } catch (err) {
    if (err.type === "ai_unavailable") aiBroken = true;
    toast(aiFailText(err));
  }
  mg.editBusy = false;
  if (mg.editCat === id && mg.settings) { paintManage(); mgArmSave(); }
}
// Shrink-wrap every destination pill to its selected label — a fixed width
// left "Trash" swimming in empty space while long label names clipped.
function mgSizeDests() {
  const probe = document.createElement("span");
  probe.style.cssText = "position:absolute;visibility:hidden;white-space:nowrap";
  document.body.appendChild(probe);
  document.querySelectorAll(".mgs-dest").forEach(s => {
    probe.style.font = getComputedStyle(s).font;   // measure with the select's real font
    probe.textContent = s.options[s.selectedIndex] ? s.options[s.selectedIndex].text : "";
    // padding + native arrow need ~44px on top of the text itself
    const w = Math.ceil(probe.getBoundingClientRect().width) + 44;
    s.style.width = Math.max(70, Math.min(180, w)) + "px";
  });
  probe.remove();
}
// Nothing commits until the card's Save — typing or picking a destination just
// arms it. (input covers text fields; change covers the select.)
function mgArmSave() {
  const b = document.querySelector(".mgs-row.editing .mgs-save");
  if (b) b.disabled = false;
}
// The open card's description hugs its content — it grows as you type.
function mgSizeDesc() {
  const ta = document.querySelector(".mgs-row.editing .mgs-desc");
  if (!ta) return;
  ta.style.height = "auto";
  ta.style.height = (ta.scrollHeight + 2) + "px";
}
$("#listpane").addEventListener("input", e => {
  if (!e.target.closest(".mgs-row.editing")) return;
  mgArmSave();
  if (e.target.classList.contains("mgs-desc")) mgSizeDesc();
});
$("#listpane").addEventListener("change", e => {
  const row = e.target.closest(".mgs-row");
  if (!row) return;
  if (row.classList.contains("editing")) {
    mgArmSave();
    if (e.target.classList.contains("mgs-dest")) mgSizeDests();
    return;
  }
  // The mini dropdown on a closed card commits immediately, like it used to.
  if (e.target.classList.contains("mgs-dest")) {
    const cat = mgCat(e.target.dataset.cat);
    if (!cat) return;
    cat.dest = e.target.value;
    saveMgConfig();
    paintManage();
    toast(`${cat.name} → ${mgDestName(cat.dest)} — saved`);
  }
});

// ---------- manage (batch triage board) ----------
// Inbox threads are pre-sorted: learned sender rules first (free), then an
// RSVP heuristic, then one fused.ai call for the rest. Nothing moves until a
// tab's confirm button; each confirm teaches a sender → bucket rule.
const MG_CACHE = "fused.mail.manage.v1";    // "threadId:date" -> bucket (AI verdicts)
const MG_RULES = "fused.mail.manage.rules"; // sender address -> bucket (learned)
// Categories are CONFIG (~/.fused-mail/manage_config.json, via mail.py): each
// carries a classifier prompt and a dest (TRASH/SPAM/ARCHIVE/KEEP/label id).
let mgConfig = null, mgConfigPath = "";
const mgCats = () => (mgConfig && mgConfig.categories) || [];
const mgCat = id => mgCats().find(c => c.id === id);
const mgKeepId = () => { const k = mgCats().find(c => (c.dest || "KEEP") === "KEEP"); return k ? k.id : "keep"; };
// bg=true for the speculative warm at boot — it must never sit in front of
// anything the user is waiting on.
let mgConfigReq = null;
async function loadMgConfig(bg) {
  if (mgConfig) return;
  // Share one in-flight request: the boot warm and a board opened right after
  // it would otherwise both fetch, and the second would wait on the first.
  if (!mgConfigReq) {
    mgConfigReq = py({ op: "manage_config" }, bg ? { bg: true } : undefined)
      .then(res => { mgConfig = res.config; mgConfigPath = res.path || ""; })
      .finally(() => { mgConfigReq = null; });
  }
  await mgConfigReq;
}
async function saveMgConfig() {
  try {
    const res = await py({ op: "save_manage_config", config: mgConfig });
    if (res && res.error) throw new Error(res.error);
  } catch (e) { toast("Couldn't save manage config — " + e.message); }
}
function mgPrompt() {
  return "You sort a busy founder's inbox into these buckets:\n" +
    mgCats().map(c => `${c.id} — ${c.prompt || c.name}`).join("\n") +
    "\nYou get numbered emails (sender, subject, snippet). Reply with ONE line per number, " +
    `exactly 'n: bucket_id' (e.g. '3: ${mgCats()[1] ? mgCats()[1].id : "promo"}'), nothing else. ` +
    `When unsure, use ${mgKeepId()}.`;
}
// One classifier call: numbered emails in, {n: categoryId} out (unknown ids
// and unparseable lines are simply absent — callers default to Keep).
async function mgClassify(threads) {
  const ctx = threads.map((t, i) =>
    `[${i + 1}] From ${fromName(t.from)} <${addrOf(t.from)}> — "${t.subject}": ${t.snippet}`).join("\n");
  const res = await fused.ai("Inbox emails:\n" + ctx, { systemPrompt: mgPrompt(), effort: "low" });
  const ids = new Set(mgCats().map(c => c.id));
  const map = {};
  for (const line of res.text.split("\n")) {
    const m = /^\s*\[?(\d+)\]?\s*[:\-]\s*([\w-]+)\b/.exec(line.trim());
    if (m && ids.has(m[2].toLowerCase())) map[parseInt(m[1], 10)] = m[2].toLowerCase();
  }
  return map;
}
// Calendar RSVP responses are 100% predictable — never worth a model call.
// They feed whichever category the config marks with `auto: "rsvp"`.
const MG_RSVP = /^(accepted|declined|tentative|canceled event|cancelled event|updated invitation)\s*:/i;
// Where a category's batch goes, and how the UI talks about it.
function mgDestName(dest) {
  if (dest === "TRASH") return "Trash";
  if (dest === "SPAM") return "Spam";
  if (dest === "ARCHIVE") return "Archive";
  if (dest === "KEEP") return "Keep in inbox";
  const l = state.labels.find(x => x.id === dest);
  return l ? labelName(l) : dest;
}
const mgVerb = dest => dest === "TRASH" ? "Trash" : dest === "SPAM" ? "Spam"
  : dest === "ARCHIVE" ? "Archive" : "Move";
const mgDone = dest => dest === "TRASH" ? "Moved to trash" : dest === "SPAM" ? "Marked as spam"
  : dest === "ARCHIVE" ? "Archived" : "Moved to " + mgDestName(dest);
// Tabs are DESTINATIONS: threads pool under their category's dest, one confirm
// clears the tab. Order: label moves, then Archive, Trash, Spam, Keep last.
const mgDestOf = catId => { const c = mgCat(catId); return c ? (c.dest || "KEEP") : "KEEP"; };
const MG_DEST_RANK = { ARCHIVE: 1, TRASH: 2, SPAM: 3, KEEP: 4 };
function mgDests() {
  const seen = [];
  mgCats().forEach(c => { const d = c.dest || "KEEP"; if (!seen.includes(d)) seen.push(d); });
  return seen.sort((a, b) => (MG_DEST_RANK[a] || 0) - (MG_DEST_RANK[b] || 0));
}
let mgRun = 0;
let mg = { status: "idle", tab: "TRASH", threads: [], assign: {}, src: {},
           unchecked: new Set(), error: "", busy: false, settings: false, tabPicked: false,
           editCat: null,     // id of the ONE category currently open for editing
           editBusy: false }; // AI is drafting into that card — fields disabled

function mgRules() { return readStore(MG_RULES); }
function mgLearn(addr, cat) {
  if (!addr) return;
  const r = mgRules();
  r[addr.toLowerCase()] = cat;
  try { localStorage.setItem(MG_RULES, JSON.stringify(r)); } catch (e) {}
}
function mgCachePut(entries) {
  const c = readStore(MG_CACHE);
  Object.assign(c, entries);
  // Generous cap: evicting a verdict sends that (old, unchanged) thread back
  // to the model on the next run, which reads as "it re-triaged everything".
  const keys = Object.keys(c);
  while (keys.length > 2000) delete c[keys.shift()];
  try { localStorage.setItem(MG_CACHE, JSON.stringify(c)); } catch (e) {}
}

// Sort what can be sorted for free — learned sender rules, the RSVP
// heuristic, then cached AI verdicts — and report whatever is left over.
// Pure bookkeeping over data already in hand: no awaits, no model call, so
// both the board and the background pass can run it instantly.
function mgSortKnown(threads, force) {
  const ids = new Set(mgCats().map(c => c.id));
  const rsvpCat = mgCats().find(c => c.auto === "rsvp");
  const rules = mgRules(), cached = force ? {} : readStore(MG_CACHE);
  // Rules/cache written under category ids that no longer exist (renamed or
  // deleted in the config file) are ignored — those threads reclassify.
  const alias = { trash: ids.has("cold") ? "cold" : null };   // pre-config bucket name
  const valid = c => ids.has(c) ? c : (alias[c] || null);
  const assign = {}, src = {}, unknown = [];
  for (const t of threads) {
    const addr = addrOf(t.from).toLowerCase();
    const byRule = valid(rules[addr]), byCache = valid(cached[t.id + ":" + t.date]);
    if (byRule) { assign[t.id] = byRule; src[t.id] = "rule"; }
    else if (rsvpCat && MG_RSVP.test(t.subject)) { assign[t.id] = rsvpCat.id; src[t.id] = "rule"; }
    else if (byCache) { assign[t.id] = byCache; src[t.id] = "ai"; }
    else unknown.push(t);
  }
  return { assign, src, unknown };
}
// Classify `unknown` and write the verdicts to the cache, so the next open —
// board or background — gets them for free.
async function mgClassifyAndCache(unknown, assign, src) {
  const keep = mgKeepId();
  const map = await mgClassify(unknown);
  const fresh = {};
  unknown.forEach((t, i) => {
    assign[t.id] = map[i + 1] || keep; src[t.id] = "ai";
    fresh[t.id + ":" + t.date] = assign[t.id];
  });
  mgCachePut(fresh);
}

// Triage the inbox WITHOUT opening the board: called after every inbox load,
// so by the time the AI triage entry is clicked every verdict is already
// cached and the board paints instantly. Only the model call costs anything,
// and only for senders no rule or cached verdict covers.
let mgPrewarming = false;
async function prewarmTriage() {
  if (mgPrewarming || !aiOn || aiBroken || onboardingNow) return;
  if (P.manage) return;              // the board is open — runManage owns this
  if (!state.threads.length) return;
  mgPrewarming = true;
  try {
    await loadMgConfig();            // cheap local file read, cached in memory after
    if (P.manage) return;
    const threads = state.threads.slice();
    const { assign, src, unknown } = mgSortKnown(threads, false);
    if (!unknown.length) return;     // everything already known — nothing to spend
    await mgClassifyAndCache(unknown, assign, src);
  } catch (err) {
    if (err && err.type === "ai_unavailable") aiBroken = true;
    // Silent by design: this is speculative work the user never asked for.
  } finally { mgPrewarming = false; }
}

async function runManage(force) {
  const rid = ++mgRun;
  mg.status = "loading"; mg.error = ""; mg.tabPicked = false;
  // Paint from the threads already in hand before awaiting anything — with a
  // warm config and cached verdicts the board is populated on the first frame
  // instead of showing "Reading the inbox" through two round trips. Keyed on
  // the list being CACHED rather than non-empty, so inbox zero lands straight
  // on "nothing to manage" instead of spinning at an answer we already have.
  if (mgConfig && P.label === "INBOX" && !P.q && !force && listCache.has(listKey("INBOX", ""))) {
    mg.threads = state.threads.slice();
    const pre = mgSortKnown(mg.threads, false);
    mg.assign = pre.assign; mg.src = pre.src;
    if (!pre.unknown.length) mg.status = "ready";
  }
  paintManage();
  let threads;
  try {
    if (force) mgConfig = null;   // Retry/regenerate also picks up hand-edits to the file
    await loadMgConfig();
    threads = (P.label === "INBOX" && !P.q && state.threads.length)
      ? state.threads.slice()
      : ((await py({ op: "list_threads", account: P.account, label: "INBOX", q: "" })).threads || []);
  } catch (e) {
    if (rid !== mgRun) return;
    mg.status = "error"; mg.error = e.message; paintManage(); return;
  }
  if (rid !== mgRun) return;
  mg.threads = threads;
  const keep = mgKeepId();
  const dests = mgDests();
  if (!dests.includes(mg.tab)) mg.tab = dests[0] || "KEEP";
  const { assign, src, unknown } = mgSortKnown(threads, force);
  mg.assign = assign; mg.src = src;
  mgPrefetch();   // start warming bodies NOW — not after the model finishes
  if (unknown.length && aiOn && !aiBroken) {
    paintManage();   // show what rules already sorted while the model thinks
    try {
      await mgClassifyAndCache(unknown, assign, src);
      if (rid !== mgRun) return;
    } catch (err) {
      if (rid !== mgRun) return;
      if (err.type === "ai_unavailable") aiBroken = true;
      unknown.forEach(t => { assign[t.id] = keep; src[t.id] = "none"; });
      mg.error = aiFailText(err);
    }
  } else {
    unknown.forEach(t => { assign[t.id] = keep; src[t.id] = "none"; });
    if (unknown.length && !aiOn) mg.error = "Turn on AI features to sort unrecognized senders.";
  }
  if (rid !== mgRun) return;
  mg.status = "ready"; mg.unchecked = new Set();
  // Open on the first tab that actually has mail, unless the user already
  // picked one — an empty default tab hides the board's content.
  if (!mg.tabPicked) {
    const counts = {};
    mg.threads.forEach(t => { const d = mgDestOf(assign[t.id]); counts[d] = (counts[d] || 0) + 1; });
    const first = mgDests().find(d => counts[d]);
    if (first) mg.tab = first;
  }
  paintManage();
  mgPrefetch();   // warm the visible tab so click previews land instantly
}

function mgTabRows() {
  return mg.threads.filter(t => mgDestOf(mg.assign[t.id]) === mg.tab);
}
function mgSelected() {
  return mgTabRows().filter(t => !mg.unchecked.has(t.id));
}
function paintManage() {
  const tabs = $("#mgtabs");
  if (!tabs) return;
  const sub = $("#managehead .mg-sub"), list = $("#mglist"), foot = $("#mgfoot");
  if (mg.status === "loading" && !mg.threads.length) {
    sub.textContent = "";
    tabs.innerHTML = ""; foot.innerHTML = "";
    list.innerHTML = `<div class="mg-spin"><span class="dots">Reading the inbox</span></div>`;
    return;
  }
  if (mg.status === "error") {
    sub.textContent = "";
    tabs.innerHTML = ""; foot.innerHTML = "";
    list.innerHTML = `<div class="mg-empty">${esc(mg.error)}<div style="margin-top:12px">` +
      `<button class="dg-redo mg-redo">Retry</button></div></div>`;
    return;
  }
  const cats = mgCats();
  if (!cats.length) return;
  const gear = $("#mgsettings");
  if (gear) {
    gear.classList.toggle("active", mg.settings);
    // A gear on the board, a back arrow inside settings.
    gear.innerHTML = svg(mg.settings ? "back" : "gear", 17);
    gear.title = mg.settings ? "Back to the board" : "Map categories to destinations";
    gear.setAttribute("aria-label", gear.title);
  }
  // Settings mode: the list area becomes the category → destination mapper.
  if (mg.settings) {
    sub.textContent = "";
    tabs.innerHTML = "";
    foot.innerHTML = "";
    const opt = (d, cur) => `<option value="${esc(d)}" ${cur === d ? "selected" : ""}>${esc(mgDestName(d))}</option>`;
    const destSel = (c, dis) =>
      `<select class="mgs-dest" data-cat="${esc(c.id)}" aria-label="Destination for ${esc(c.name)}" ${dis || ""}>` +
      ["TRASH", "SPAM", "ARCHIVE", "KEEP"].map(d => opt(d, c.dest || "KEEP")).join("") +
      userLabels().map(l => opt(l.id, c.dest)).join("") + `</select>`;
    // Category CARDS: click one to expand it for editing (one at a time).
    // Edits stay local to the card until Save — which only lights up once
    // something actually changed.
    list.innerHTML = `<div id="mgprefs">
      <div id="mgadd">
        <button id="mgadd-blank">+ New<span class="mgadd-long">&nbsp;category</span></button>
        <button id="mgadd-ai">${svg("sparkle", 13)} Categorize<span class="mgadd-long">&nbsp;with AI</span></button>
      </div>` + cats.map(c => {
      const editing = mg.editCat === c.id;
      if (!editing) return `
      <div class="mgs-row" data-cat="${esc(c.id)}" role="button" tabindex="0"
        aria-label="Edit ${esc(c.name)}">
        <span class="mgs-namev">${esc(c.name)}</span>
        <span class="mgs-pencil" aria-hidden="true">${svg("pencil", 13)}</span>
        ${destSel(c)}
      </div>`;
      const busy = mg.editBusy;   // AI is drafting into this card — hands off until it lands
      const dis = busy ? "disabled" : "";
      return `
      <div class="mgs-row editing" data-cat="${esc(c.id)}">
        <div class="mgs-head">
          <input class="mgs-name" data-cat="${esc(c.id)}" value="${esc(c.name)}" spellcheck="false" aria-label="Category name" ${dis}>
          ${destSel(c, dis)}
        </div>
        <div class="mgs-labrow">
          <span class="mgs-lab">AI triage description</span>
          <button class="mgs-ai" data-cat="${esc(c.id)}" title="Write with AI" aria-label="Write description with AI" ${dis}>${svg("sparkle", 14)}</button>
          ${busy ? `<span class="mgs-busy"><span class="dots">Drafting</span></span>` : ""}
        </div>
        <textarea class="mgs-desc" data-cat="${esc(c.id)}" rows="3" spellcheck="false"
          placeholder="Describe what belongs here — the AI triages with this"
          aria-label="AI triage description for ${esc(c.name)}" ${dis}>${esc(c.prompt || "")}</textarea>
        <div class="mgs-tools">
          ${cats.length > 1 ? `<button class="mgs-del" data-cat="${esc(c.id)}" title="Remove category" aria-label="Remove ${esc(c.name)}" ${dis}>${svg("trash", 14)}</button>` : ""}
          <button class="mgs-discard" data-cat="${esc(c.id)}" ${dis}>Discard</button>
          <button class="mgs-save" data-cat="${esc(c.id)}" disabled>Save</button>
        </div>
      </div>`;
    }).join("") +
      `</div>`;
    mgSizeDests();
    mgSizeDesc();
    // Tight on space? Both add-buttons drop to their short labels together.
    const add = $("#mgadd");
    if (add && [...add.children].some(b => b.scrollWidth > b.clientWidth)) add.classList.add("tight");
    return;
  }
  const keep = mgKeepId();
  const byCat = {};
  cats.forEach(c => byCat[c.id] = []);
  mg.threads.forEach(t => (byCat[mg.assign[t.id]] || byCat[keep] || []).push(t));
  const busySort = mg.status === "loading";
  sub.textContent = mg.error || "";
  // One tab per DESTINATION — its count is the pool of every category mapped there.
  const dests = mgDests();
  if (!dests.includes(mg.tab)) mg.tab = dests[0] || "KEEP";
  const destCount = d => cats.reduce((n, c) => n + ((c.dest || "KEEP") === d ? byCat[c.id].length : 0), 0);
  tabs.innerHTML = dests.map(d => {
    const n = destCount(d);
    return `<button class="mg-tab ${d === mg.tab ? "active" : ""}" data-id="${esc(d)}" data-tip="${esc(mgDestName(d))}">` +
      `<span class="mg-tname">${esc(mgDestName(d))}</span>` +
      (n ? `<span class="mg-n">${n}</span>` : "") + `</button>`;
  }).join("");
  // Tight on space? Swap the system tabs' text for their icon, one at a time
  // from the RIGHT end of the row (tab order is Archive, Trash, Spam, Keep),
  // until everything fits without scrolling.
  const SYS_TAB_ICONS = { KEEP: "inbox", ARCHIVE: "archive", SPAM: "report", TRASH: "trash" };
  for (const d of ["KEEP", "SPAM", "TRASH", "ARCHIVE"]) {
    if (tabs.scrollWidth <= tabs.clientWidth) break;
    const t = tabs.querySelector(`.mg-tab[data-id="${d}"] .mg-tname`);
    if (t) t.outerHTML = svg(SYS_TAB_ICONS[d], 15);
  }
  tabs.classList.toggle("scrollable", tabs.scrollWidth > tabs.clientWidth);
  const activeTab = tabs.querySelector(".active");
  if (activeTab) activeTab.scrollIntoView({ inline: "nearest", block: "nearest" });
  const tabCats = cats.filter(c => (c.dest || "KEEP") === mg.tab);
  const rows = tabCats.flatMap(c => byCat[c.id]);
  const actionable = mg.tab !== "KEEP";
  const scroll = list.scrollTop;
  const rowHtml = t => `
      <div class="mg-row ${t.unread ? "unread" : ""} ${t.id === mgPeekId ? "peeking" : ""}" data-id="${esc(t.id)}">
        <label class="mg-cbwrap"><input type="checkbox" class="mg-cb" data-id="${esc(t.id)}"
          ${mg.unchecked.has(t.id) ? "" : "checked"} aria-label="Include ${esc(t.subject)}"></label>
        <div class="mg-main">
          <div class="mg-top">
            <span class="mg-from">${esc(fromName(t.from))}</span>
            ${mg.src[t.id] === "rule" ? `<span class="mg-rule" title="Sorted by a rule you taught it">rule</span>` : ""}
            <span class="mg-date">${fmtDate(t.date)}</span>
          </div>
          <div class="mg-subj">${esc(t.subject)}</div>
        </div>
        ${actsHtml(t)}
      </div>`;
  if (!mg.threads.length) {
    list.innerHTML = `<div class="mg-empty">Inbox zero — nothing to manage.</div>`;
  } else if (!rows.length) {
    list.innerHTML = `<div class="mg-empty">${busySort ? `<span class="dots">Sorting</span>` : "Nothing in this bucket."}</div>`;
  } else {
    // Rows group by category inside the tab so a mixed pool stays reviewable.
    list.innerHTML = tabCats.map(c => byCat[c.id].length
      ? `<div class="mg-group">${esc(c.name)}<span class="mg-n">${byCat[c.id].length}</span></div>` +
        byCat[c.id].map(rowHtml).join("")
      : "").join("") + (busySort ? `<div class="mg-spin"><span class="dots">Sorting the rest</span></div>` : "");
  }
  list.scrollTop = scroll;
  if (rows.length) {
    const n = mgSelected().length;
    const selectAll = `<label class="mg-all"><input type="checkbox" id="mgall" ${n === rows.length ? "checked" : ""}> <span class="mg-all-txt">Select all&nbsp;</span>(${n})</label>`;
    foot.innerHTML = actionable ? `
      ${selectAll}
      <button id="mgkeep" class="btn-ghost pill" ${n ? "" : "disabled"}>Keep</button>
      <button id="mgact" class="btn-primary pill ${mg.tab === "TRASH" || mg.tab === "SPAM" ? "btn-danger-solid" : ""}"
        ${n ? "" : "disabled"}>${
          esc(["TRASH", "SPAM", "ARCHIVE"].includes(mg.tab) ? mgVerb(mg.tab) : `Move to ${mgDestName(mg.tab)}`)}</button>` : `
      ${selectAll}
      <button id="mgmove" class="btn-ghost pill" title="Move to" aria-label="Move to" aria-haspopup="menu"
        ${n ? "" : "disabled"}>${svg("folderin", 15)}<span class="mgmv-txt">Move to ▾</span></button>
      <button id="mgtriage" class="btn-primary pill" style="display:inline-flex;align-items:center;gap:6px" ${n && !mg.busy ? "" : "disabled"}>${svg("sparkle", 14)}${mg.busy ? "Triaging…" : "Triage"}</button>`;
  } else {
    foot.innerHTML = "";
  }
  // Tight on space? The Move-to button drops its text and keeps the icon.
  foot.classList.remove("tight");
  if ($("#mgmove") && foot.scrollWidth > foot.clientWidth) foot.classList.add("tight");
}

function mgConfirm() { mgMoveTo(mg.tab, true); }
// Move the selected threads to `dest`. `learn` teaches the sender rules only
// when the move confirms the AI's own sorting (the tab's confirm button) —
// a hand-picked "Move to" destination says nothing about the category.
function mgMoveTo(dest, learn) {
  if (!dest || dest === "KEEP") return;
  const picked = mgSelected();
  if (!picked.length) return;
  // Optimistic: rows leave the board immediately and the Gmail calls run in
  // the background (the py queue serializes them), so the user moves straight
  // to the next tab instead of watching a spinner.
  const gone = new Set(picked.map(t => t.id));
  picked.forEach(t => { if (learn) mgLearn(addrOf(t.from), mg.assign[t.id]); delete mg.assign[t.id]; });
  mg.threads = mg.threads.filter(t => !gone.has(t.id));
  mg.unchecked = new Set();
  toast(`${mgDone(dest)} ${picked.length}`);
  mgAdvanceTab();   // an emptied tab hands off to the next one with mail
  paintManage();
  (async () => {
    let ok = 0, fail = "";
    for (const t of picked) {
      try {
        const r = dest === "TRASH" ? await py({ op: "trash", account: P.account, thread: t.id })
          // ARCHIVE just drops INBOX; SPAM or a label id adds it and leaves the inbox
          : await modOp(t.id, dest === "ARCHIVE" ? [] : [dest], ["INBOX"]);
        if (r && r.error) throw new Error(r.error);
        ok++;
      } catch (e) { fail = e.message; }
    }
    invalidateCaches();
    const failed = picked.length - ok;
    if (failed) toast(`${mgDone(dest)} ${ok} — ${failed} failed (${fail})`);
    if (P.label === "INBOX" && !P.q) loadThreads(false);
  })();
}

// "Move to" on the Keep tab: opens ABOVE its button — Archive, Trash, Spam,
// then every user label.
function mgMoveMenu() {
  openMoveMenu("mgmovemenu", $("#mgfoot"),
    [{ id: "ARCHIVE", name: "Archive" }, { id: "TRASH", name: "Trash" }, { id: "SPAM", name: "Spam" },
     ...userLabels().map(l => ({ id: l.id, name: labelName(l) }))],
    id => mgMoveTo(id, false));
}

// "Keep" on any tab: the selected threads stay in the inbox — they jump to the
// Keep tab, and each sender gets a keep rule so next time they land there.
function mgKeep() {
  const picked = mgSelected();
  if (!picked.length) return;
  const keep = mgKeepId();
  const fresh = {};
  picked.forEach(t => {
    mg.assign[t.id] = keep; mg.src[t.id] = "rule";
    mgLearn(addrOf(t.from), keep);
    fresh[t.id + ":" + t.date] = keep;
  });
  mgCachePut(fresh);
  mg.unchecked = new Set();
  toast(`Kept ${picked.length} in the inbox`);
  mgAdvanceTab();
  paintManage();
}
// When the current tab runs dry, jump to the next destination that still has
// mail — no point staring at "Nothing in this bucket."
function mgAdvanceTab() {
  const counts = {};
  mg.threads.forEach(t => { const d = mgDestOf(mg.assign[t.id]); counts[d] = (counts[d] || 0) + 1; });
  if (counts[mg.tab]) return;
  const next = mgDests().find(d => counts[d]);
  if (next) mg.tab = next;
}

// "Triage" on the Keep tab: re-run the model on the selected threads. Sending
// one here means its current sorting was wrong, so its sender rule is dropped
// and the fresh AI verdict replaces the cached one.
async function mgTriage() {
  const picked = mgSelected();
  if (!picked.length || mg.busy) return;
  if (!aiOn || aiBroken) { toast("Turn on AI features to triage."); return; }
  mg.busy = true; paintManage();
  const keep = mgKeepId();
  const r = mgRules();
  picked.forEach(t => delete r[addrOf(t.from).toLowerCase()]);
  try { localStorage.setItem(MG_RULES, JSON.stringify(r)); } catch (e) {}
  try {
    const map = await mgClassify(picked);
    const fresh = {};
    let moved = 0;
    picked.forEach((t, i) => {
      const cat = map[i + 1] || keep;
      if (mgDestOf(cat) !== "KEEP") moved++;
      mg.assign[t.id] = cat; mg.src[t.id] = "ai";
      fresh[t.id + ":" + t.date] = cat;
    });
    mgCachePut(fresh);
    toast(moved ? `Triaged — ${moved} re-sorted out of Keep` : "Triaged — the model kept them all");
  } catch (err) {
    if (err.type === "ai_unavailable") aiBroken = true;
    toast(aiFailText(err));
  }
  mg.busy = false;
  mg.unchecked = new Set();
  paintManage();
}

function renderManage() {
  // The board owns the MIDDLE pane (where the thread list normally lives);
  // the reading pane stays free for the click preview / catch-up landing.
  $("#list").innerHTML = `
  <div id="mgboard">
    <div class="mg-hd">
      <span class="mg-title">AI triage</span>
      <button id="mgsettings" title="Map categories to destinations" aria-label="Map categories to destinations"></button>
    </div>
    <div id="managehead">
      <div id="mgtabs"></div>
      <p class="mg-sub"></p>
    </div>
    <div id="mglist"></div>
    <div id="mgfoot"></div>
  </div>`;
  paintManage();
  if (mg.status !== "loading") runManage(false);
}


// The triage board's copies of the inbox row actions. Same optimistic shape:
// the row leaves the board (and the inbox list) NOW, the server call runs
// behind it, and a failure or Undo puts it back in both places.
function mgRemoveOptimistic(tid) {
  const idx = mg.threads.findIndex(t => t.id === tid);
  const removed = idx >= 0 ? mg.threads.splice(idx, 1)[0] : null;
  const inbox = removeRowOptimistic(tid);
  if (mgPeekId === tid) mgPeekReset();
  paintManage();
  return { idx, removed, inbox };
}
function mgRestoreRow({ idx, removed, inbox }) {
  if (removed) {
    mg.threads.splice(Math.min(idx, mg.threads.length), 0, removed);
    paintManage();
  }
  restoreRow(inbox);
}
const mgArchive = tid => undoableMove(tid, "Archived", "Couldn't archive",
  () => modOp(tid, [], ["INBOX"]), () => modOp(tid, ["INBOX"], []),
  mgRemoveOptimistic, mgRestoreRow, mgRestoreRow);
const mgTrash = tid => undoableMove(tid, "Moved to trash", "Couldn't move to trash",
  () => py({ op: "trash", account: P.account, thread: tid }), () => modOp(tid, ["INBOX"], ["TRASH"]),
  mgRemoveOptimistic, mgRestoreRow, mgRestoreRow);
function mgSetUnread(tid, unread, msg) {
  const t = mg.threads.find(x => x.id === tid);
  const was = t ? t.unread : null;
  if (t) t.unread = unread;
  const row = state.threads.find(x => x.id === tid);   // keep the inbox list in step
  if (row) row.unread = unread;
  paintManage();
  toast(msg);
  modOp(tid, unread ? ["UNREAD"] : [], unread ? [] : ["UNREAD"])
    .then(r => { if (r && r.error) throw new Error(r.error); })
    .catch(err => {
      if (t && was !== null) { t.unread = was; if (row) row.unread = was; paintManage(); }
      toast("Couldn't update — " + err.message);
    });
}
