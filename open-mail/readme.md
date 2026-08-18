# Mail — Gmail inbox as a fused-render view

A working multi-account mailbox: thread list, read, compose/send/reply, archive/trash/star/unread,
search (full Gmail query syntax), labels, attachments.

The UI is deliberately Gmail-shaped: a top app bar with a rounded search pill, a left nav whose
Compose is a filled pill and whose active label is a rounded chip, list rows that swap the date
for archive/trash/mark-unread on hover, and a compose window that pops up bottom-right instead of
dimming the page. It ships **light and dark themes** (CSS custom properties, toggled from the app
bar, remembered in `localStorage`, applied before first paint so there is no flash) with every
small-text pairing checked against WCAG AA. And when nothing is selected, the reading pane is a
**catch-up briefing** — the AI digest of everything unread runs by itself on landing, one clickable
bullet per thread, cached against the exact unread set so revisiting it is free. With nothing
connected yet you get a **welcome card** instead of an empty mailbox: one sentence and a Connect
Gmail button. It stays usable down to ~440px wide — the sidebar folds into an icon rail at 900px
and the layout drops to one pane at a time below 620px.

## Open it

```
http://127.0.0.1:<port>/embed/<abs path to this dir>/mail.html
```

**Engine requirement: builtin (local).** Start the server with `FUSED_RENDER_ENGINE=builtin`
and have `google-api-python-client` + `google-auth-oauthlib` installed in the server venv.
Why: on this machine the run venvs contain the real fused SDK, and its `@fused.udf` executes
on the **remote** Fused env (server_rt2 lambda — `HOME=/tmp`, no local filesystem, no
subprocess). A mail client owns local token files and spawns a local OAuth browser flow, so
`mail.py` deliberately ships a **bare `main()`** (no decorator): the builtin engine's child
runs it locally; the fused engine would return `null` for it.

Opening the app with no `?account` lands on your first connected account, and on the welcome card
if there is none.

### The demo mailbox is test plumbing

`mail.py` still ships a `demo` provider — fixture threads with triage state persisted in
`~/.fused-mail/demo_state.json` (delete that file to reset it) — because both e2e suites are
built on it and it lets every mutating flow be exercised without touching real mail. It is
**invisible in the UI**: no sidebar row, no default. Reach it with `?account=demo`, which is a
permanent backdoor. `?onboarding=1` is the matching test hook for the welcome card; it pretends
nothing is connected without touching `~/.fused-mail`.

## Sharing this project with someone else

Each person connects **their own Gmail with their own credentials**. Nothing is shared between
installs: tokens are minted to that user, stored on their machine (`~/.fused-mail/tokens/`), and
mail never leaves their laptop — there is no server in the middle.

The only question is which OAuth client signs their consent request:

| Option | Their setup | Isolation |
|---|---|---|
| **App password (IMAP)** — recommended for sharing | paste 16 chars, no GCP at all | total: no shared client, no shared quota |
| **Their own OAuth client** | the ~10 min GCP walkthrough below, once | total; their project, their quota |
| Bundled OAuth client (drop a `credentials.json` next to `mail.py`) | zero | **not isolated** — see below |

**Why the bundled-client option is deliberately not shipped here.** It looks tempting (recipients
get zero setup), and their *mail* would still be private to them. But the OAuth client stays
coupled to whoever owns it:

- the consent screen shows the **owner's** app name and support email — recipients contact them;
- every recipient's API calls draw on the **owner's** project quota;
- deleting the client, or a suspension from someone abusing that client ID, breaks **every** install at once;
- unverified apps are capped at 100 users, and lifting that for `gmail.modify` (a restricted scope)
  requires a paid CASA security assessment.

`mail.py` still prefers `~/.fused-mail/credentials.json` and falls back to a `credentials.json`
sitting next to the script — so bundling one remains possible for a trusted internal group. It is
just not the default, and the file is gitignored.

## Reality check: there is no true 1-click Gmail

Every route ends at a Google consent screen. The only variable is **who owns the OAuth app**:

| Route | Clicks per account | Cost |
|---|---|---|
| Aggregator (Composio / Nylas hosted auth) | ~2 | zero GCP setup, but your mail transits their servers + their API key |
| **Own OAuth client (this app, Superhuman's model)** | **2** | one-time GCP console setup — already done for this project |
| App password (IMAP) | 1 paste | being phased out; blocked on many Workspace domains |

### Gotchas that cost real time here (2026 console)

- **Publish the app to production** once it works. While publishing status is `Testing`, Google
  expires refresh tokens after **7 days** — every account has to re-consent weekly. In production
  (still unverified) the token persists; users just click through a
  "Google hasn't verified this app" → *Advanced* → *Go to … (unsafe)* screen once. Verified here.
- Test users **must** be saved on the Audience page **before** consent *while in Testing mode*, or
  Google returns `403 access_denied — has not completed the Google verification process` even for
  the project owner. The chip field needs a real click + Enter; a scripted/DOM click leaves the list
  empty with no error. (Publishing to production makes the list irrelevant.)
- The scope must also be declared under **Data Access** (`Manually add scopes` →
  `https://www.googleapis.com/auth/gmail.modify` → Add to table → Update → Save).
- Consent must be **completed in a normal Chrome profile**; a fresh Playwright-launched profile
  is refused with "This browser or app may not be secure".

## Connect real Gmail accounts — path A: app password (~1 min, default)

1. [myaccount.google.com/apppasswords](https://myaccount.google.com/apppasswords) (requires 2-step verification on the account).
2. Create an app password, copy the 16 characters.
3. In the app: **+ Add account** → paste email + app password → Connect.

Runs over IMAP/SMTP with Gmail's extensions (X-GM-THRID threads, X-GM-LABELS label ops,
X-GM-RAW full Gmail search syntax), so threads/labels/search/triage all work. Trade-offs vs
the API path: no snippets in the thread list, newest ~100 messages per folder for thread
grouping, no pagination, and Google is slowly phasing app passwords out.

## Connect real Gmail accounts — path B: OAuth + Gmail API (one-time ~10 min, full fidelity)

1. [console.cloud.google.com](https://console.cloud.google.com) → create or pick a project.
2. **APIs & Services → Library** → enable **Gmail API**.
3. **OAuth consent screen** → Internal (Workspace) — or External + add yourself as a test user.
4. **Credentials → Create credentials → OAuth client ID** → application type **Desktop app**.
5. Download the client JSON → save as `~/.fused-mail/credentials.json`.

Then click **+ Add account** in the sidebar. A Google consent window opens in your browser;
approve, and the account appears. Repeat for as many accounts as you want — one token per
account under `~/.fused-mail/tokens/`, all sharing the single OAuth client.

Scope requested: `gmail.modify` (read/send/labels — no delete-forever, no settings).

## Files

| File | Role |
|---|---|
| `mail.html` | UI. State (account/label/q/thread) lives in URL params — refresh-proof. |
| `mail.py` | All data ops behind one bare `main(op=…)` dispatcher (no `@fused.udf` — see engine note above). |
| `add_account.py` | Detached OAuth consent worker (spawned by `op=start_auth`; dodges the 30 s engine timeout). |

## Data layout (`~/.fused-mail/`)

```
credentials.json      OAuth client (you provide, once)
accounts.json         registered accounts
tokens/<email>.json   per-account refresh token
downloads/            attachment downloads
auth_status.json      add-account progress
demo_state.json       demo inbox triage state
```

## Notes / v2 candidates

- Provider field per account — Outlook (Microsoft Graph) adapter slots in later; IMAP adapter (app-password path) already doubles as a template for non-Gmail IMAP hosts.
- True 1-click "Connect Gmail" needs a hosted, verified OAuth client (Fused-owned GCP project) or a third-party aggregator (Nylas/Unipile — mail flows through their servers).
- No local cache: every list/read is a live API call (25 threads per page, one batched round-trip).
- AI layer (triage, drafts, summarize — Superhuman-style) will reuse `mail.py` ops as its tool surface.
