# Manual test plan — AI features (summary + draft reply)

**Open:** http://127.0.0.1:8865/embed/Users/akshilthumar/Desktop/fused/fused-render-worktrees/mail-inbox/examples/mail/mail.html

**Reset the demo inbox first (optional):** `rm ~/.fused-mail/demo_state.json`

Automated suites already pass — `node examples/mail/tests/e2e-mailbox.js` (22/22) and
`node examples/mail/tests/e2e-ai.js` (38/38). This plan covers the judgement calls a
script can't make: does the summary actually help, does the draft sound like you,
does the thing look like a mail client.

> **The demo mailbox is invisible now.** It is still in `mail.py` because both suites are
> built on it, but it never renders: no sidebar row, no highlight, and opening the app with
> no `?account` lands on your first connected account. The suites reach it through
> `?account=demo`, which is a permanent backdoor — see §8.

---

## 1. It stays off until you say so

| Step | Expect |
|---|---|
| Load the page, look bottom-left | "AI features" with the switch **off** |
| Open any thread | No summary box, no "Draft reply" button — plain mailbox |
| Click the AI toggle | A dialog appears **before** anything turns on |
| Read it | Says Claude Code CLI is the credential, and that open threads are sent to Anthropic |
| Click Cancel | Still off. Nothing changed |
| Click the toggle → Turn on | Switch goes lime, label reads "AI features on" |

## 2. Summary

| Step | Expect |
|---|---|
| Open "Q3 planning doc — comments by Friday" | Summary box at the top, "Reading the thread…" animating |
| Wait | Two bullets + an "Action:" line, within a few seconds |
| Judge it | Does it tell you what you'd need to know without reading the thread? |
| Click another thread, then come back | Summary appears **instantly** — cached, no second call |
| Click **Redo** | Regenerates from scratch |
| Open "Weekly metrics digest" | Summary should have no "Action:" line — nothing is being asked of you |

## 3. Suggested reply (replaces the Draft button)

| Step | Expect |
|---|---|
| Open a thread where someone else spoke last | A "Suggested reply" ghost panel streams in above the reply box, by itself |
| Press **Tab** (or click the panel) | The suggestion fills the reply box, panel hides, cursor lands in the box |
| Type your own text first, then press Tab | Nothing — Tab never overwrites what you wrote |
| Open a thread where YOU sent the last message | Suggestion is a **follow-up nudge** instead of a reply |
| Reopen a thread | Suggestion is instant (cached until someone replies) |
| Read the suggestion | Reply body only — no "Here's a draft", no fake signature |

## 3b. Landing briefing (was: the unread digest strip)

The digest is no longer a panel you open above the list. With nothing selected, it **is**
the reading pane, and it runs by itself.

| Step | Expect |
|---|---|
| Load the app with AI on and no thread open | "While you were away" + "N unread conversations", bullets streaming in — **no click needed** |
| Read it | Max 5 bullets, important first, routine mail grouped; no [n] numbers visible — grouped bullets show an "N mails" badge |
| Click a bullet | Jumps straight to that thread; the briefing yields the pane |
| Press **Esc** (or ✕) to close the thread | Briefing is back, instantly — cached against the exact unread set, no second model call |
| Click **✦** in the app bar while a thread is open | Closes the thread and shows the briefing |
| Click **Regenerate** | Fresh model call; otherwise cached until the unread set changes |
| Mark everything read, then ✦ | "Nothing unread — you're caught up." under an "All caught up" headline |
| Turn AI off | Plain "Select a conversation" plus one quiet line offering the briefing — no modal, no nag |
| Open a thread mid-stream, then close it again | The briefing picks up where it was; a landing re-render never restarts an in-flight stream |

## 4. Real mail (account `akshil@fused.io`)

| Step | Expect |
|---|---|
| Switch to your real account, open a long thread | Summary handles it; only the last 8 messages are sent, bodies trimmed |
| Open an HTML newsletter | Summary still works — HTML is stripped before sending |
| Draft a reply to a real thread | Tone matches the thread. **Nothing sends until you click Send** |

## 5. Turning it off

| Step | Expect |
|---|---|
| Toggle AI off | Summary box and Draft button vanish immediately |
| Reload the page | Stays off. (Same for on — the setting persists) |

---

## What to report back

- **Summary quality** — useful, or generic padding? Wrong on any thread?
- **Draft quality** — would you send it after a small edit, or is it unusable?
- **Speed** — does streaming make it feel fast enough?
- Anything that errored, and what the message said.

## Known limits (not bugs)

- **Local only.** `fused.ai` runs the `claude` CLI on this machine, so this page can never be
  exported or hosted — a deliberate choice (option A).
- **No `claude` CLI = no AI.** You'd see "AI unavailable"; the mailbox itself is unaffected.
- Summaries cache in `localStorage`, keyed by thread + last message — a new reply invalidates it.
- Only the newest 8 messages, 1500 characters each, are sent per thread.
- Cost rides your Claude Code subscription, not an API key.

## 6. Makeover additions (2026-08-03)

| Step | Expect |
|---|---|
| Press `j` / `k` with nothing focused | Walks down/up the thread list, opening each |
| Press `e` on an open thread | Archives it; toast shows **Undo**; Undo puts it back |
| Press `c`, `/`, `Esc` | Compose opens; search focuses; modal → thread closes in that order |
| Tab through the list | Rows take focus with a visible ring; Enter opens; star appears on focus |
| Star a thread with the network broken | Star reverts and an error toast names the failure (no silent lie) |
| Narrow the window to ~900px with a thread open | Nothing clips; Send stays visible |
| Type half a reply, toggle AI on | Your text survives the re-render |

## 7. Gmail-style UI: themes and hover quick actions (2026-08-03)

### 7a. Light / dark

| Step | Expect |
|---|---|
| Load the app for the first time | Light theme — unless your OS is set to dark, in which case dark. **No flash of the wrong palette** on load or reload |
| Click the moon/sun in the app bar | Palette switches **instantly** (no cross-fade); the icon and its tooltip swap |
| Reload | The choice sticks (`localStorage` → `fused.mail.theme`) |
| Switch OS appearance after choosing manually | Your explicit choice wins; the OS is only the first-run default |
| In each theme, read the small text | Dates, snippets, meta lines, chips: all ≥ 4.5:1 (the values in `mail.html`'s theme block are computed, not eyeballed) |
| Open an HTML newsletter in dark theme | The message iframe keeps a white body — real newsletters assume white, and that is deliberate |
| Reduce motion in system settings | Compose still pops open, just without the rise |

### 7b. Hover quick actions

| Step | Expect |
|---|---|
| Hover a list row | The date makes way for archive / trash / mark-unread icons — nothing behind them bleeds through |
| Click archive on a row you are **not** reading | It leaves the inbox; the thread you have open stays open; toast offers Undo |
| Click trash | Same, with an Undo that restores INBOX |
| Click the envelope on a read row | Row goes bold-unread; the icon flips to "mark as read" |
| Tab to a row with the keyboard | The actions appear on focus too, and the star is reachable |
| Move the mouse away mid-row | Actions fade out in ~120ms; the date comes back |

### 7c. Layout

| Step | Expect |
|---|---|
| Narrow to ~1000px | Sidebar and list shrink; **Send never leaves the screen**; the brand wordmark drops before anything clips |
| Open compose | Bottom-right popup, no full-screen dim — you can still read and click the mailbox behind it |
| Press Esc with compose open | Compose closes first; a second Esc closes the thread |
| Click outside compose | It stays open (that is the point of a popup) |

## 8. Onboarding, empty states and real-mail polish (2026-08-04)

### 8a. Accounts and onboarding

`?onboarding=1` is a **test-only URL hook**: it makes the app behave as if no account were
connected, which is the only way to exercise the welcome card on a machine that really has
one. It never changes anything under `~/.fused-mail`.

| Step | Expect |
|---|---|
| Open the app with no `?account` | Your first connected account loads — never the demo inbox |
| Look at the sidebar, any state | No "Demo inbox" row ever, even at `?account=demo` (nothing is highlighted then) |
| Open `?account=demo` | The demo mailbox loads normally — the backdoor still works |
| Open `?onboarding=1` | Both mailbox panes are replaced by one centered **Welcome to Mail** card; the sidebar shows only "+ Add account"; search is hidden; the ✦ briefing button is gone |
| Click **Connect Gmail** | The same add-account modal the sidebar opens (app password + "Use OAuth instead") |
| Connect an account for real | The mailbox appears immediately, on the account you just added |
| Disconnect your last account | You land back on the welcome card, not on a demo inbox |
| Both themes | Card, button and the app-password note all read cleanly in light and dark |

### 8b. Empty states

| Step | Expect |
|---|---|
| Open an empty label (e.g. Trash) | Glyph + "No mail here" + one quiet line — not a bare sentence |
| Search for something with no hits | Glyph + "No results" + the query echoed back |

### 8c. Real-mail rendering

| Step | Expect |
|---|---|
| Open any real thread | Every message shows a **real date** — never "1 Jan 1970" |
| Compare a message date with the list row | They agree (both prefer the `Date:` header; the list still *sorts* by arrival time) |
| Look at real list rows | Each has a preview line, HTML stripped, no `@media only screen…` CSS, no tracking URLs |
| Look at a thread header | Label chips (Inbox / Sent / Important / your own labels) actually appear |
| Open a 5+ message thread | Collapsed messages show a one-line preview next to the sender |
| Open a mail with a very long subject | Wraps to two lines and then ellipses — not clipped at one |
| Turn AI on, summarize a newsletter | No raw URLs in the summary or the briefing — links are named ("the Vanta link") |
| Paste a 300-character URL into a reply and read it back | It wraps inside the pane; nothing scrolls sideways |

### 8d. Narrow widths

| Step | Expect |
|---|---|
| Narrow to ~900px | The sidebar becomes an icon rail; list stays ≥ 280px; nothing wraps in a row |
| Narrow to ~600px | One pane at a time — the list, or the thread once you open one |
| At any width down to ~440px | No horizontal scrollbar; the search pill stays usable; **Send** stays on screen |
| Open a thread at ~440px, press ✕ | Back to the list |
