# Magpie — a private, local-first memory assistant for macOS

Magpie watches what is on your screen, remembers it, and lets you search it, chat
with it, get daily journals, track open loops, and take meeting notes.

Magpie is built around four principles:

1. **Privacy** – 100% local. Screen text, screenshots, audio and the database never
   leave the machine unless the user picks a cloud LLM. Default LLM is local Ollama.
2. **Control** – excluded apps, pause/resume, secret redaction, retention limits,
   delete-a-time-range, export everything as JSON.
3. **Open** – plain SQLite data, a CLI, and a free MCP server.
4. **Any model** – Ollama, any OpenAI-compatible API, or Anthropic.

## Target environment

- macOS 26, Apple Silicon. Python 3.14 (`python3`), `uv` is installed. Swift 6.2.
- Ollama at http://localhost:11434 with model `llama3.2:1b` installed.
- `ffmpeg` and `mlx_whisper` (CLI) are installed for meeting audio.

## Repository layout

```
helper/                  Swift package, executable `magpie-helper`
  Package.swift
  Sources/MagpieHelper/main.swift
magpie/                  Python package
  __init__.py
  config.py              load/save ~/.magpie/config.json (MAGPIE_HOME env overrides ~/.magpie)
  db.py                  SQLite schema + queries (FTS5)
  helper.py              runs magpie-helper subprocess, parses JSON
  capture.py             capture loop
  redact.py              secret redaction
  timeparse.py           natural-language time ranges
  llm.py                 provider abstraction
  rag.py                 retrieval + prompt building + answer with citations
  journal.py             daily summary + open loops extraction
  meetings.py            meeting recording/transcription/notes
  server.py              FastAPI app (API + static UI)
  mcp_server.py          MCP stdio server
  cli.py                 `magpie` CLI entry point
  static/index.html, app.js, styles.css   the web UI (no build step)
tests/                   pytest
pyproject.toml           package `magpie`, script `magpie = magpie.cli:main`
README.md
```

## 1. Swift helper (`magpie-helper`)

Build with `swift build -c release --package-path helper`. Prints JSON to stdout,
errors as `{"error": "..."}` with non-zero exit. Subcommands:

- `ocr <image-path>` → `{"text": "...", "lines": [{"text","confidence","x","y","w","h"}]}`
  using Vision `VNRecognizeTextRequest`, `.accurate`, language correction on.
  Lines sorted top-to-bottom, left-to-right. `text` = lines joined with `\n`.
- `frontmost` → `{"app": "Safari", "bundle_id": "com.apple.Safari", "pid": 123,
  "window_title": "..."}`. App via `NSWorkspace.shared.frontmostApplication`.
  Window title via `CGWindowListCopyWindowInfo` (on-screen windows, layer 0, owner pid
  matches); empty string if unavailable.
- `idle` → `{"seconds": 12.3}` via
  `CGEventSource.secondsSinceLastEventType(.combinedSessionState, eventType: .null)`
  (use `CGEventType(rawValue: ~0)!` for "any input").
- `permissions` → `{"screen_recording": true|false}` via `CGPreflightScreenCaptureAccess()`.

## 2. Storage (`db.py`)

Data dir `~/.magpie/` (or `$MAGPIE_HOME`): `magpie.db`, `shots/YYYY-MM-DD/<id>.jpg`,
`audio/`. Use `sqlite3` with WAL. Tables:

- `captures(id INTEGER PK, ts REAL, app TEXT, bundle_id TEXT, window_title TEXT,
  text TEXT, text_hash TEXT, image_path TEXT NULL, width INT, height INT)`
- `captures_fts` FTS5 virtual table (content='captures', columns text, app,
  window_title, tokenize 'porter unicode61') kept in sync with triggers.
- `activities(id PK, app, bundle_id, window_title, start_ts, end_ts, capture_count)` —
  consecutive captures with the same app+window_title extend the current activity
  if the gap is < 5 minutes; otherwise a new one starts.
- `journals(day TEXT PK, summary TEXT, created_ts REAL, model TEXT)`
- `loops(id PK, text TEXT, source_capture_id INT NULL, day TEXT, status TEXT
  'open'|'done'|'dismissed', created_ts REAL)` with dedupe on lower(text)+day.
- `meetings(id PK, title, start_ts, end_ts, audio_path, transcript TEXT,
  notes_md TEXT, status 'recording'|'transcribing'|'done'|'error', error TEXT)`
- `chats(id PK, created_ts, title)`, `messages(id PK, chat_id, role, content,
  citations_json, ts)`

Functions needed (at least): `insert_capture`, `search(query, start_ts=None,
end_ts=None, app=None, limit=20)` returning rows with bm25 rank and a `snippet()`
highlight; FTS query must be sanitised so user input with quotes/operators never
raises (quote each token, join with OR for recall, prefix-match last token);
`recent(limit)`, `captures_between(start,end)`, `timeline(day)` (activities for a
day), `app_usage(day)` (seconds per app), `delete_range(start,end)` (also deletes
image files), `prune(retention_days)`, `export_all()`, `stats()`.

## 3. Capture loop (`capture.py`)

Every `interval_seconds` (default 15):
1. Skip if paused, or idle > `idle_pause_seconds` (default 120).
2. `frontmost`. Skip if bundle_id or app name is in `excluded_apps`
   (default: 1Password, Keychain Access, Bitwarden, LastPass, Dashlane, Messages
   optional off) or window title matches private-browsing patterns
   (`Private Browsing`, `Incognito`, `InPrivate`).
3. `screencapture -x -C -t jpg <tmp>` of the main display. If it fails or the image
   is empty/black → record a permission problem in status, continue.
4. Downscale to max 1600px wide, OCR with helper.
5. Redact secrets (see below). Compute `text_hash` (sha1 of normalised text). If the
   text is ≥ 90% similar (difflib quick ratio on normalised text) to the previous
   capture of the same app+window, skip storing (dedupe) but extend the activity.
6. Store capture; if `keep_screenshots` (default true) save JPEG quality 60 under shots/.
7. Every hour: prune by `retention_days` (default 30).

Must be runnable as a background thread inside the server AND headless via
`magpie capture --once` (single capture, prints JSON) for testing. The screenshot
source and helper must be injectable so tests can feed a fake image.

## 4. Redaction (`redact.py`)

Replace with `[REDACTED:<kind>]`: credit card numbers (13–19 digits, spaces/dashes
allowed, Luhn-valid only), AWS keys `AKIA[0-9A-Z]{16}`, OpenAI/Anthropic keys
`sk-[A-Za-z0-9_-]{20,}` / `sk-ant-...`, GitHub tokens `gh[pousr]_[A-Za-z0-9]{36,}`,
Slack `xox[baprs]-...`, JWTs, private key blocks, US SSN `\d{3}-\d{2}-\d{4}`, and lines
of the form `password: xxx` / `password=xxx`. Return `(text, counts_by_kind)`.

## 5. Time parsing (`timeparse.py`)

`parse_range(question, now) -> (start_ts, end_ts) | None` for: today, yesterday,
this morning (06–12), this afternoon (12–18), tonight/this evening, last night,
last hour / last N hours / last N minutes, this week, last week, on Monday..Sunday
(most recent past), N days ago. Also strip matched phrases for retrieval.

## 6. LLM (`llm.py`)

`get_provider(config)` returns an object with `chat(messages, stream=False)` →
str or iterator of str chunks. Providers via `httpx`:
- `ollama` (base_url default http://localhost:11434, model default `llama3.2:1b`) using /api/chat.
- `openai` (OpenAI-compatible: base_url, api_key, model) using /v1/chat/completions.
- `anthropic` (api_key, model) using /v1/messages with `anthropic-version: 2023-06-01`.
- `fake` for tests: echoes a deterministic answer that includes `[1]`.
Clear error message if the provider is unreachable (shown in UI, not a stack trace).

## 7. Chat with memory (`rag.py`)

`answer(question, chat_history, config, db) -> {"answer", "citations": [capture rows]}`
and a streaming variant. Steps: parse time range; extract keywords (drop stopwords);
FTS search within range (limit 12); if no keyword hits but a time range exists, use
evenly sampled captures from the range plus that range's activity timeline;
always include a compact "what the user was doing" timeline for the range.
Truncate each capture text to ~1200 chars, total context ~12k chars.
System prompt: you are Magpie, answer only from the provided screen memories,
cite sources as [n], say plainly when memory doesn't contain the answer, include
times in local time. Drafting requests ("draft a reply to...") are allowed using
memories as context.

## 8. Journal + open loops (`journal.py`)

- `generate_journal(day)` – build per-app time usage + activity list + sampled
  captures → LLM → markdown with sections "Summary", "What you worked on",
  "People & conversations", "Open loops". Cached in `journals`; regenerate on request.
- `extract_loops(day)` – LLM returns a JSON list of follow-ups/todos/promises seen on
  screen (e.g. "Reply to Sam about the contract"). Parse robustly (find the JSON
  array in the output; fall back to bullet lines). Insert into `loops`.
- Auto-run for the previous day the first time the server starts each day (background).

## 9. Meetings (`meetings.py`)

- Start: `ffmpeg -f avfoundation -i ":<mic index>" -ac 1 -ar 16000 <audio>.wav`
  subprocess (default mic `:0`; config `mic_device`). Stop: send `q` to stdin / SIGINT.
- Then transcribe with `mlx_whisper <wav> --model mlx-community/whisper-large-v3-turbo
  --output-format txt --output-dir <dir>` (model configurable; fall back to
  `whisper-cli` if mlx_whisper missing; if none, status error with clear message).
- Notes: LLM turns transcript + screen captures during the meeting window into
  markdown: Summary, Key points, Decisions, Action items (owner if known).
- Auto-detect suggestion: if frontmost app is zoom.us, Microsoft Teams, Webex, FaceTime
  or a browser window title contains "Meet -" / "Google Meet", status exposes
  `meeting_detected: true` so the UI can prompt "Record this meeting?".
- Note: mic only (system audio needs a loopback device like BlackHole; document it).

## 10. HTTP API (`server.py`, FastAPI, 127.0.0.1:7777 only)

- `GET /api/status` – capturing/paused, last capture ts, counts, permission state,
  provider + reachable, meeting_detected.
- `POST /api/pause`, `POST /api/resume`, `POST /api/capture-now`
- `GET /api/search?q=&start=&end=&app=` · `GET /api/captures/{id}` · `GET /api/captures/{id}/image`
- `GET /api/timeline?day=YYYY-MM-DD` (activities + app usage)
- `POST /api/chat` `{chat_id?, message}` → SSE stream of `{"delta"}` events then a
  final `{"done": true, "citations": [...], "chat_id"}`. `GET /api/chats`, `GET /api/chats/{id}`
- `GET /api/journal?day=` · `POST /api/journal/regenerate?day=`
- `GET /api/loops?status=open` · `POST /api/loops/{id}` `{status}` · `POST /api/loops/extract?day=`
- `POST /api/meetings/start`, `POST /api/meetings/{id}/stop`, `GET /api/meetings`, `GET /api/meetings/{id}`
- `GET/PUT /api/settings` · `DELETE /api/data?start=&end=` · `GET /api/export`
- `/` serves the UI from `magpie/static`.

## 11. Web UI (`magpie/static/`, vanilla JS, no build, no CDN)

Polished, calm, modern design; light and dark mode (prefers-color-scheme); left
sidebar nav; works at narrow widths. Views:
- **Ask** – chat with streaming responses, markdown rendering (write a tiny safe
  renderer: escape HTML first; support headings, bold, italics, code, lists, links),
  citation chips `[n]` that open the source capture (screenshot + text + time + app).
  Suggested prompts: "What was I working on this morning?", "Summarize yesterday",
  "What did I promise to do today?". Chat history list.
- **Search** – query box, app filter, date range; result cards with app icon letter,
  time, window title, highlighted snippet, thumbnail; click → detail modal.
- **Timeline** – day picker; horizontal hour bar with colored blocks per app;
  list of activities with durations; app usage bars.
- **Journal** – day picker, rendered journal, regenerate button.
- **Loops** – open loops list with done/dismiss, "find loops for today" button.
- **Meetings** – record/stop button with timer, list of meetings, notes + transcript.
- **Settings** – provider (ollama/openai/anthropic) + model + base URL + key
  (key never returned in GET; show "set"), interval, idle pause, keep screenshots,
  retention days, excluded apps (editable chips), mic device, pause toggle,
  delete last hour / today / range, export JSON, permission status with how-to-fix
  text for Screen Recording.
- Header: capture status pill (Recording / Paused / Needs permission) with pause button,
  and a "Record this meeting?" banner when `meeting_detected`.

## 12. MCP server (`mcp_server.py`)

`magpie mcp` runs a stdio MCP server (use the `mcp` Python SDK, FastMCP) with tools:
`search_memory(query, start?, end?, app?, limit?)`, `get_timeline(day)`,
`get_journal(day)`, `list_open_loops()`, `ask_memory(question)`. Reads the same DB.

## 13. CLI (`cli.py`)

`magpie serve [--port 7777] [--no-capture]`, `magpie capture --once`,
`magpie search <q>`, `magpie ask <question>`, `magpie status`, `magpie pause|resume`
(via HTTP to running server), `magpie mcp`, `magpie doctor` (checks helper built,
screen recording permission, Ollama reachable, ffmpeg, mlx_whisper; prints fixes),
`magpie export > file.json`.

## 14. Tests (pytest, must all pass, no network, no real screen)

Use a temp `MAGPIE_HOME`. Cover: db insert/search incl. hostile FTS input
(`"`, `AND (`, `*`), activity merging, delete_range, prune; redaction (valid vs
invalid Luhn, each key kind); timeparse cases; capture pipeline with fake
screenshot + fake helper incl. excluded app, private window, dedupe; rag with
fake provider returns citations; journal loop JSON parsing (clean, wrapped in prose,
bullets fallback); API endpoints via FastAPI TestClient (status, search, chat SSE,
settings key masking, delete). Plus one optional real test marked `macos` that
renders text into a PNG with Pillow and checks `magpie-helper ocr` reads it.

## 15. README.md

What it is, what you get (feature list), install (uv venv,
`swift build`, grant Screen Recording to the terminal), run, MCP config snippet for
Claude Desktop / opencode, privacy model, limitations.

## Quality bar

- Everything works end to end on this machine. No placeholders, no TODO stubs.
- Errors are handled and shown helpfully; capture loop never crashes the server.
- Keep dependencies small: fastapi, uvicorn, httpx, pillow, mcp, pytest.

## 16. Native app and integrations (phase 4) — must ship, not optional

### 16a. Native Mac app (`app/`, SwiftUI, Swift package executable `Magpie`)
- Assembled into `dist/Magpie.app` by `scripts/build_app.sh` (swift build -c release,
  create bundle dirs, Info.plist with `LSUIElement=true`, bundle id `ai.magpie.app`,
  `NSMicrophoneUsageDescription`, `NSCalendarsFullAccessUsageDescription`,
  `NSRemindersFullAccessUsageDescription`, `NSAppleEventsUsageDescription`; copy
  magpie-helper into Contents/Resources; ad-hoc codesign `codesign -s - --deep`).
- On launch it starts the Python backend (`<repo>/.venv/bin/magpie serve`, path from
  `MAGPIE_REPO` env or a path baked in at build time) unless already running on 7777,
  and stops it on quit.
- `MenuBarExtra` (window style): status dot (Recording/Paused/Needs permission),
  today's capture count and top 3 apps, Pause/Resume, Record meeting / Stop,
  "Open Magpie", "Quick Ask (⌥Space)", Settings, Quit.
- Main window: `WKWebView` loading http://127.0.0.1:7777 with transparent titlebar,
  min size 900x600; shows a native "Starting Magpie…" view until /api/status is up.
- Quick Ask panel: global hotkey ⌥Space (Carbon `RegisterEventHotKey`), floating
  `NSPanel` (non-activating, centered, rounded, vibrancy `NSVisualEffectView`), text
  field → streams answer from POST /api/chat, Esc closes, Enter in empty field opens
  main window on that chat.
- Native notifications (`UNUserNotificationCenter`): poll /api/notifications every
  60s and post them (new open loops due today, meeting detected → "Record this
  meeting?" with action button that starts recording, journal ready).

### 16b. System + mic audio for meetings (no BlackHole)
- Helper subcommand `record-audio <out-dir>`: ScreenCaptureKit `SCStream` with
  `capturesAudio = true`, `excludesCurrentProcessAudio = true`, sample rate 16000 mono,
  plus `captureMicrophone = true` (macOS 15+) writing `system.wav` and `mic.wav`
  (AVAssetWriter or AVAudioFile). Runs until SIGINT/SIGTERM or stdin closes, then
  finalises files and prints `{"system": path, "mic": path}`.
- meetings.py uses the helper first; falls back to ffmpeg mic-only. Mix with
  `ffmpeg -i system.wav -i mic.wav -filter_complex amix=inputs=2 -ac 1 -ar 16000 mix.wav`.
  Transcribe each track separately to label speakers "Me" (mic) vs "Them" (system),
  merging segments by timestamp (use mlx_whisper `--output-format json`).

### 16c. Phone access (PWA, private)
- Setting `remote_access` (default off). When on, server also listens on 0.0.0.0 and
  every non-localhost request needs `Authorization: Bearer <token>` or cookie set by
  `/pair?code=<6-digit>`; pairing code shown in Settings with a QR code (generate the
  QR as SVG in pure Python — implement a minimal QR encoder or use `segno` dependency).
- `manifest.webmanifest`, service worker for app-shell caching, icons, mobile layout
  with bottom tab bar (Ask, Search, Loops, Meetings). Works over LAN or Tailscale.

### 16d. Actions & integrations (local first)
- Helper subcommands using EventKit: `calendar-events <start-iso> <end-iso>` → JSON
  events; `add-reminder <title> [--due iso] [--notes text]`; `add-event <title> <start>
  <end> [--notes]`. Handle permission requests (`requestFullAccessToReminders/Events`).
- Meetings auto-titled from the calendar event overlapping now; Journal includes the
  day's calendar.
- Draft email: `osascript` to create a visible Mail.app outgoing message (to, subject,
  body) — never sends automatically.
- Notion: optional `notion_token` + `notion_parent_page_id` settings; "Send to Notion"
  on journals and meeting notes creates a page via the Notion API.
- Copy as Markdown everywhere.
- UI: each open loop has "→ Reminder", "→ Calendar", "Draft email"; chat answers that
  look like drafts get "Open in Mail" / "Copy".
- API: `POST /api/actions/reminder|event|email|notion`, `GET /api/calendar?day=`,
  `GET /api/notifications`.
- MCP adds tools: `create_reminder`, `create_calendar_event`, `draft_email`,
  `get_calendar`.
