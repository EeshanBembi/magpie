/* Magpie web UI — vanilla JS, no build step.
   Talks to the FastAPI in magpie/server.py on 127.0.0.1:7777. */
"use strict";

(() => {
  // ------------------------------------------------------------------
  // tiny helpers
  // ------------------------------------------------------------------

  const $ = (id) => document.getElementById(id);

  const esc = (value) => {
    let out = String(value ?? "");
    if (!/[&<>"']/.test(out)) return out;
    out = out.split("&").join("\u0026amp;");
    out = out.split("<").join("\u0026lt;");
    out = out.split(">").join("\u0026gt;");
    out = out.split('"').join("\u0026quot;");
    out = out.split("'").join("\u0026#39;");
    return out;
  };

  const dayKey = (ts) => {
    const d = new Date(ts * 1000);
    const p = (n) => String(n).padStart(2, "0");
    return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`;
  };

  const todayKey = () => dayKey(Date.now() / 1000);

  const clock = (ts) =>
    new Date(ts * 1000).toLocaleTimeString([], {
      hour: "numeric",
      minute: "2-digit",
    });

  const fullTime = (ts) =>
    new Date(ts * 1000).toLocaleString([], {
      weekday: "short",
      month: "short",
      day: "numeric",
      hour: "numeric",
      minute: "2-digit",
    });

  const dateLabel = (day) => {
    const [y, m, d] = String(day).split("-").map(Number);
    if (!y || !m || !d) return String(day);
    return new Date(y, m - 1, d).toLocaleDateString([], {
      weekday: "long",
      month: "long",
      day: "numeric",
    });
  };

  const fmtDur = (sec) => {
    sec = Math.max(0, Math.round(Number(sec) || 0));
    if (sec < 60) return `${sec}s`;
    const m = Math.floor(sec / 60);
    if (m < 60) return `${m}m`;
    const h = Math.floor(m / 60);
    return `${h}h ${m % 60}m`;
  };

  const clockTimer = (sec) => {
    sec = Math.max(0, Math.floor(Number(sec) || 0));
    const h = Math.floor(sec / 3600);
    const m = Math.floor((sec % 3600) / 60);
    const s = sec % 60;
    const p = (n) => String(n).padStart(2, "0");
    return h ? `${h}:${p(m)}:${p(s)}` : `${p(m)}:${p(s)}`;
  };

  const ago = (ts) => {
    if (!ts) return "";
    const diff = Date.now() / 1000 - Number(ts);
    if (diff < 45) return "just now";
    if (diff < 3600) return `${Math.max(1, Math.floor(diff / 60))}m ago`;
    if (diff < 86400) return `${Math.floor(diff / 3600)}h ago`;
    return new Date(ts * 1000).toLocaleDateString([], { month: "short", day: "numeric" });
  };

  const relDay = (day) => {
    if (day === todayKey()) return "Today";
    const y = new Date();
    y.setDate(y.getDate() - 1);
    const yk = dayKey(y.getTime() / 1000);
    if (day === yk) return "Yesterday";
    return dateLabel(day);
  };

  const eventClock = (value) => {
    if (!value) return "";
    const d = new Date(value);
    if (Number.isNaN(d.getTime())) return "";
    return d.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
  };

  const eventRange = (start, end) => {
    const from = eventClock(start);
    const to = eventClock(end);
    if (from && to) return `${from}–${to}`;
    return from || to || "";
  };

  const calendarListHTML = (events) => `
    <div class="calendar-list">${(events || []).map((ev) => `
      <div class="calendar-item">
        <span class="calendar-time">${esc(eventRange(ev.start, ev.end))}</span>
        <span class="calendar-main">
          <span class="calendar-title">${esc(ev.title || "(untitled event)")}</span>
          ${(ev.location || ev.calendar) ? `<span class="calendar-meta">${esc([ev.location, ev.calendar].filter(Boolean).join(" · "))}</span>` : ""}
        </span>
      </div>`).join("")}</div>`;

  // deterministic hue per app name
  const hueFor = (name) => {
    let h = 0;
    const str = String(name || "?");
    for (let i = 0; i < str.length; i++) h = (h * 31 + str.charCodeAt(i)) % 360;
    return h;
  };

  const debounce = (fn, ms) => {
    let t;
    return (...args) => {
      clearTimeout(t);
      t = setTimeout(() => fn(...args), ms);
    };
  };

  const svgIcon = (name) => {
    const map = {
      spark: '<path d="M12 3l2.2 5.6L20 11l-5.8 2.4L12 19l-2.2-5.6L4 11l5.8-2.4z"/>',
      check: '<path d="M4.5 12.5l4.5 4.5 10-10"/>',
      bell: '<path d="M6 9a6 6 0 0 1 12 0c0 5 2 6 2 6H4s2-1 2-6z"/><path d="M10.5 19.5a1.8 1.8 0 0 0 3 0"/>',
      trash: '<path d="M4 6.5h16M9.5 6.5v-2h5v2M6.5 6.5l1 14h9l1-14M10 10.5v6M14 10.5v6"/>',
      dl: '<path d="M12 4v11M7 11.5l5 5 5-5M5 20h14"/>',
      x: '<path d="M6 6l12 12M18 6L6 18"/>',
      calendar: '<rect x="3.5" y="5" width="17" height="15.5" rx="2.5"/><path d="M3.5 10h17M8 3v4M16 3v4"/>',
      mail: '<rect x="3" y="5.5" width="18" height="13" rx="2.5"/><path d="M3.5 7l8.5 6 8.5-6"/>',
      refresh: '<path d="M20 12a8 8 0 1 1-2.34-5.66"/><path d="M20 3.5V8h-4.5"/>',
      mic: '<rect x="9" y="2.5" width="6" height="11" rx="3"/><path d="M5.5 11.5a6.5 6.5 0 0 0 13 0M12 18v3.5"/>',
      copy: '<rect x="8.5" y="8.5" width="12" height="12" rx="2.5"/><path d="M5.5 15.5h-1a1 1 0 0 1-1-1v-10a1 1 0 0 1 1-1h10a1 1 0 0 1 1 1v1"/>',
      search: '<circle cx="11" cy="11" r="7"/><path d="M20.5 20.5 16 16"/>',
      warn: '<path d="M12 3.5 21.5 20h-19z"/><path d="M12 9.5v5M12 17.5v.5"/>',
      stop: '<rect x="7" y="7" width="10" height="10" rx="2.5"/>',
      camera: '<rect x="3" y="6.5" width="18" height="13" rx="3"/><circle cx="12" cy="13" r="3.5"/><path d="M8.5 6.5 10 3.5h4l1.5 3"/>',
    };
    return `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${map[name] || map.spark}</svg>`;
  };

  // ------------------------------------------------------------------
  // API layer
  // ------------------------------------------------------------------

  const api = async (path, options = {}) => {
    const opts = { headers: {}, ...options };
    if (opts.body !== undefined && typeof opts.body !== "string") {
      opts.body = JSON.stringify(opts.body);
      opts.headers["Content-Type"] = "application/json";
    }
    let res;
    try {
      res = await fetch(path, opts);
    } catch (err) {
      if (err && err.name === "AbortError") {
        const abort = new Error("Request timed out");
        abort.name = "AbortError";
        throw abort;
      }
      throw new Error("Could not reach the Magpie server. Is it running?");
    }
    if (!res.ok) {
      let detail = res.statusText || `Request failed (${res.status})`;
      try {
        const data = await res.json();
        if (data && data.detail) detail = typeof data.detail === "string" ? data.detail : JSON.stringify(data.detail);
      } catch (_) { /* not JSON */ }
      const err = new Error(detail);
      err.status = res.status;
      throw err;
    }
    if (res.status === 204) return null;
    const text = await res.text();
    if (!text) return null;
    try {
      return JSON.parse(text);
    } catch (_) {
      return text;
    }
  };

  // ------------------------------------------------------------------
  // toasts
  // ------------------------------------------------------------------

  const toast = (message, { kind = "info", title = "", ms = 5000 } = {}) => {
    const root = $("toastRoot");
    if (!root) return;
    const el = document.createElement("div");
    el.className = `toast${kind === "error" ? " toast-error" : kind === "success" ? " toast-success" : ""}`;
    el.innerHTML = `
      <div class="toast-body">
        ${title ? `<div class="toast-title">${esc(title)}</div>` : ""}
        <div class="toast-msg">${esc(message)}</div>
      </div>
      <button class="toast-x" type="button" aria-label="Dismiss">✕</button>`;
    const remove = () => {
      if (!el.isConnected) return;
      el.classList.add("leaving");
      setTimeout(() => el.remove(), 200);
    };
    el.querySelector(".toast-x").addEventListener("click", remove);
    root.appendChild(el);
    if (ms > 0) setTimeout(remove, ms);
  };

  const toastError = (err, title = "Something went wrong") =>
    toast(err && err.message ? err.message : String(err), { kind: "error", title });

  // ------------------------------------------------------------------
  // markdown (escape first, then a small safe subset)
  // ------------------------------------------------------------------

  const renderMarkdown = (src) => {
    let text = String(src ?? "").replace(/\r\n/g, "\n");
    text = esc(text);

    const blocks = [];
    text = text.replace(/```([\s\S]*?)```/g, (_, code) => {
      blocks.push(`<pre><code>${code.replace(/\n$/, "")}</code></pre>`);
      return `\u0001${blocks.length - 1}\u0002`;
    });

    const inline = (s) => {
      const spans = [];
      s = s.replace(/`([^`]+)`/g, (_, c) => {
        spans.push(`<code>${c}</code>`);
        return `\u0003${spans.length - 1}\u0004`;
      });
      s = s
        .replace(/\*\*([^*]+)\*\*/g, "\u0005strong\u0006$1\u0005/strong\u0006")
        .replace(/(^|[\s(])\*([^*\n]+)\*/g, "$1\u0005em\u0006$2\u0005/em\u0006")
        .replace(/(^|[\s(])_([^_\n]+)_/g, "$1\u0005em\u0006$2\u0005/em\u0006")
        .replace(/\[([^\]]+)\]\((https?:\/\/[^)\s]+)\)/g, '\u0005a href="$2" target="_blank" rel="noopener noreferrer"\u0006$1\u0005/a\u0006')
        .replace(/\[(\d{1,3})\]/g, '\u0005a class="cite-inline" data-cite="$1"\u0006$1\u0005/a\u0006');
      s = s
        .replaceAll("\u0005strong\u0006", "<strong>")
        .replaceAll("\u0005/strong\u0006", "</strong>")
        .replaceAll("\u0005em\u0006", "<em>")
        .replaceAll("\u0005/em\u0006", "</em>")
        .replace(/\u0005a ([^\u0005\u0006]+)\u0006/g, "<a $1>")
        .replaceAll("\u0005/a\u0006", "</a>");
      s = s.replace(/\u0003(\d+)\u0004/g, (_, n) => spans[Number(n)] ?? "");
      return s;
    };

    const lines = text.split("\n");
    let html = "";
    let list = null;
    let para = [];
    const flushPara = () => {
      if (para.length) {
        html += `<p>${inline(para.join(" "))}</p>`;
        para = [];
      }
    };
    const closeList = () => {
      if (list) {
        html += `</${list}>`;
        list = null;
      }
    };

    for (const rawLine of lines) {
      const line = rawLine.trimEnd();
      const fence = line.match(/^\u0001(\d+)\u0002$/);
      if (fence) {
        flushPara();
        closeList();
        html += blocks[Number(fence[1])] ?? "";
        continue;
      }
      const h = line.match(/^(#{1,4})\s+(.*)$/);
      if (h) {
        flushPara();
        closeList();
        const level = Math.min(4, h[1].length);
        html += `<h${level}>${inline(h[2])}</h${level}>`;
        continue;
      }
      if (/^\s*(?:---|\*\*\*)\s*$/.test(line)) {
        flushPara();
        closeList();
        html += "<hr>";
        continue;
      }
      const ul = line.match(/^\s*[-*•]\s+(.*)$/);
      const ol = line.match(/^\s*\d+[.)]\s+(.*)$/);
      if (ul || ol) {
        flushPara();
        const want = ul ? "ul" : "ol";
        if (list !== want) {
          closeList();
          html += `<${want}>`;
          list = want;
        }
        html += `<li>${inline((ul ? ul[1] : ol[1]).trim())}</li>`;
        continue;
      }
      if (/^\s*$/.test(line)) {
        flushPara();
        closeList();
        continue;
      }
      para.push(line);
    }
    flushPara();
    closeList();

    html = html.replace(/\u0001(\d+)\u0002/g, (_, n) => blocks[Number(n)] ?? "");
    return html;
  };

  // ------------------------------------------------------------------
  // state
  // ------------------------------------------------------------------

  const state = {
    route: "ask",
    chatId: null,
    chatLabel: "",
    sending: false,
    settings: null,
    loopsFilter: "open",
    meetingActiveId: null,
    timelineDay: null,
    journalDay: null,
    loopsDay: null,
    searchState: { q: "", app: "", start: "", end: "", ran: false },
    capturedIds: new Set(),
  };

  const views = {
    ask: askView,
    search: searchView,
    timeline: timelineView,
    journal: journalView,
    loops: loopsView,
    meetings: meetingsView,
    settings: settingsView,
  };

  // ------------------------------------------------------------------
  // view plumbing
  // ------------------------------------------------------------------

  const VIEW_TITLES = {
    ask: "Ask",
    search: "Search",
    timeline: "Timeline",
    journal: "Journal",
    loops: "Loops",
    meetings: "Meetings",
    settings: "Settings",
  };

  let activeViewCleanup = null;

  const setView = (name) => {
    state.route = name;
    document.body.dataset.view = name;
    for (const el of document.querySelectorAll("[data-route]")) {
      el.classList.toggle("active", el.dataset.route === name);
    }
    const title = $("viewTitle");
    if (title) title.textContent = VIEW_TITLES[name] || "Magpie";
    const main = $("view");
    if (activeViewCleanup) {
      try { activeViewCleanup(); } catch (_) { /* noop */ }
      activeViewCleanup = null;
    }
    main.innerHTML = "";
    const render = views[name] || views.ask;
    render(main);
    main.focus({ preventScroll: true });
    main.scrollTop = 0;
  };

  const skeletonCards = (n = 3, extra = "") =>
    `<div class="skel-stack ${extra}">${Array.from({ length: n }, () =>
      `<div class="card"><div class="skeleton skel-line w40"></div><div class="skeleton skel-line w90"></div><div class="skeleton skel-line w70"></div></div>`).join("")}</div>`;

  const emptyState = (icon, title, line, action) => `
    <div class="empty">
      ${svgIcon(icon)}
      <div class="empty-title">${esc(title)}</div>
      <div class="empty-line">${esc(line)}</div>
      ${action ? `<button class="btn ${action.accent ? "btn-accent" : ""}" id="${action.id}" type="button">${esc(action.label)}</button>` : ""}
    </div>`;

  const errorBox = (err, retryId = "retryView") => `
    <div class="error-box">
      <span>${esc(err && err.message ? err.message : String(err))}</span>
      <button class="btn btn-small" id="${retryId}" type="button">Retry</button>
    </div>`;

  const wireRetry = (main, fn) => {
    const btn = $("retryView");
    if (btn) btn.addEventListener("click", () => { main.innerHTML = ""; fn(); });
  };

  const appBadge = (app) =>
    `<span class="app-badge" style="--h:${hueFor(app)}">${esc((app || "?").trim().charAt(0).toUpperCase() || "?")}</span>`;

  const dayPicker = (id, value) => `
    <input class="input" type="date" id="${id}" value="${esc(value)}" aria-label="Pick a day">`;

  const wireDayPicker = (id, onChange) => {
    const el = $(id);
    if (el) el.addEventListener("change", () => onChange(el.value));
  };

  // ------------------------------------------------------------------
  // modal
  // ------------------------------------------------------------------

  let modalOnClose = null;
  function openModal({ title, sub, body, foot = "", width = 760 }) {
    const root = $("modalRoot");
    modalOnClose = null;
    const openSheet = document.querySelector(".chat-menu");
    if (openSheet) openSheet.remove();
    root.innerHTML = `
      <div class="modal-backdrop" id="modalBackdrop">
        <div class="modal" role="dialog" aria-modal="true" style="max-width:${width}px">
          <div class="modal-head">
            <div class="modal-titles">
              <div class="modal-title">${title}</div>
              ${sub ? `<div class="modal-sub">${sub}</div>` : ""}
            </div>
            <button class="modal-x" id="modalX" type="button" aria-label="Close">${svgIcon("x")}</button>
          </div>
          <div class="modal-body" id="modalBody">${body}</div>
          ${foot ? `<div class="modal-foot">${foot}</div>` : ""}
        </div>
      </div>`;
    $("modalX").addEventListener("click", closeModal);
    $("modalBackdrop").addEventListener("mousedown", (e) => {
      if (e.target === e.currentTarget) closeModal();
    });
    const first = $("modalBody").querySelector("input, textarea, button, select");
    if (first) first.focus();
  }

  function closeModal() {
    $("modalRoot").innerHTML = "";
    if (modalOnClose) { const fn = modalOnClose; modalOnClose = null; fn(); }
  }

  // Inline date/time popover used by "→ Calendar" on open loops.
  let calendarPop = null;
  let calendarPopCleanup = null;

  function closeCalendarPopover() {
    if (calendarPop) calendarPop.remove();
    calendarPop = null;
    if (calendarPopCleanup) {
      calendarPopCleanup();
      calendarPopCleanup = null;
    }
  }

  function defaultEventSlot() {
    const p = (n) => String(n).padStart(2, "0");
    const start = new Date();
    start.setSeconds(0, 0);
    start.setMinutes(start.getMinutes() < 30 ? 30 : 60);
    const end = new Date(start.getTime() + 30 * 60000);
    const day = `${start.getFullYear()}-${p(start.getMonth() + 1)}-${p(start.getDate())}`;
    return {
      date: day,
      start: `${p(start.getHours())}:${p(start.getMinutes())}`,
      end: `${p(end.getHours())}:${p(end.getMinutes())}`,
    };
  }

  function openCalendarPopover(anchor, { title = "", onSave }) {
    closeCalendarPopover();
    const slot = defaultEventSlot();
    const pop = document.createElement("div");
    pop.className = "calendar-pop";
    pop.setAttribute("role", "dialog");
    pop.innerHTML = `
      <div class="calendar-pop-head">
        <strong>Add to Calendar</strong>
        <button class="calendar-pop-x" type="button" data-close aria-label="Close">${svgIcon("x")}</button>
      </div>
      <p class="calendar-pop-title">${esc(title || "New event")}</p>
      <div class="calendar-pop-grid">
        <label>Date<input class="input" type="date" data-date value="${slot.date}"></label>
        <label>Start<input class="input" type="time" data-start value="${slot.start}"></label>
        <label>End<input class="input" type="time" data-end value="${slot.end}"></label>
      </div>
      <div class="calendar-pop-actions">
        <button class="btn btn-small" type="button" data-close>Cancel</button>
        <button class="btn btn-small btn-accent" type="button" data-save>${svgIcon("calendar")} Add event</button>
      </div>`;
    document.body.appendChild(pop);
    calendarPop = pop;

    const rect = anchor ? anchor.getBoundingClientRect() : null;
    const width = 300;
    const left = rect
      ? Math.min(Math.max(8, rect.left), window.innerWidth - width - 8)
      : Math.max(8, window.innerWidth / 2 - width / 2);
    let top = rect ? rect.bottom + 8 : 140;
    const height = pop.offsetHeight || 210;
    if (top + height > window.innerHeight - 8) {
      top = Math.max(8, (rect ? rect.top : 140) - height - 8);
    }
    pop.style.left = `${left}px`;
    pop.style.top = `${top}px`;

    const onPointer = (event) => {
      if (pop.contains(event.target) || (anchor && anchor.contains(event.target))) return;
      closeCalendarPopover();
    };
    const onKey = (event) => {
      if (event.key === "Escape") closeCalendarPopover();
    };
    document.addEventListener("mousedown", onPointer, true);
    document.addEventListener("keydown", onKey);
    calendarPopCleanup = () => {
      document.removeEventListener("mousedown", onPointer, true);
      document.removeEventListener("keydown", onKey);
    };

    for (const btn of pop.querySelectorAll("[data-close]")) {
      btn.addEventListener("click", closeCalendarPopover);
    }
    pop.querySelector("[data-save]").addEventListener("click", async (event) => {
      const save = event.currentTarget;
      const date = pop.querySelector("[data-date]").value;
      const startTime = pop.querySelector("[data-start]").value;
      const endTime = pop.querySelector("[data-end]").value;
      if (!date || !startTime || !endTime) {
        toast("Pick a date, start and end time", { kind: "error" });
        return;
      }
      const start = `${date}T${startTime}:00`;
      const end = `${date}T${endTime}:00`;
      if (new Date(end) <= new Date(start)) {
        toast("End must be after start", { kind: "error" });
        return;
      }
      save.disabled = true;
      try {
        await onSave({ start, end });
        closeCalendarPopover();
      } catch (err) {
        toastError(err, "Calendar failed");
        save.disabled = false;
      }
    });
  }

  // capture detail modal (screenshot + text + meta)
  async function openCaptureModal(id) {
    openModal({ title: "Capture", sub: "Loading…", body: skeletonCards(1) });
    try {
      const cap = await api(`/api/captures/${id}`);
      const hasImage = Boolean(cap.image_path);
      openModal({
        title: esc(cap.app || "Unknown app"),
        sub: `${esc(fullTime(cap.ts))}${cap.window_title ? " — " + esc(cap.window_title) : ""}`,
        body: `
          ${hasImage ? `<img class="capture-shot" src="/api/captures/${cap.id}/image" alt="Screenshot">` : `<div class="empty-line" style="text-align:center;padding:12px">No screenshot stored for this capture.</div>`}
          <div class="capture-text">${esc(cap.text || "(no text was captured)")}</div>
          <div class="modal-meta">
            <span>App: ${esc(cap.app || "—")}</span>
            ${cap.bundle_id ? `<span>Bundle: ${esc(cap.bundle_id)}</span>` : ""}
            ${cap.width && cap.height ? `<span>Size: ${cap.width}×${cap.height}</span>` : ""}
          </div>`,
      });
    } catch (err) {
      openModal({ title: "Capture", body: errorBox(err, "retryCapture") });
      $("retryCapture").addEventListener("click", () => openCaptureModal(id));
    }
  }

  // ------------------------------------------------------------------
  // status / header
  // ------------------------------------------------------------------

  const bannerDismissKey = () => `magpie-banner-${dayKey(Date.now() / 1000)}`;

  async function refreshStatus() {
    let s;
    try {
      s = await api("/api/status");
    } catch (err) {
      const pill = $("statusPill");
      pill.className = "status-pill is-error";
      $("statusText").textContent = "Offline";
      const pause = $("pauseBtn");
      pause.hidden = true;
      return;
    }
    const pill = $("statusPill");
    const text = $("statusText");
    const pauseBtn = $("pauseBtn");
    const paused = Boolean(s.paused);
    const permission = s.permission && s.permission.screen_recording;
    const problem = Boolean(s.permission_problem) || permission === false;

    pill.className = "status-pill";
    if (problem) {
      pill.classList.add("is-error");
      text.textContent = "Needs permission";
      pill.title = "Screen Recording permission is missing. Grant it in System Settings, then resume.";
    } else if (paused) {
      pill.classList.add("is-paused");
      text.textContent = "Paused";
    } else {
      pill.classList.add("is-recording");
      text.textContent = "Recording";
    }

    pauseBtn.hidden = false;
    $("pauseLabel").textContent = paused ? "Resume" : "Pause";
    $("pauseIcon").innerHTML = paused
      ? '<path d="M7 4.5v15l13-7.5z"/>'
      : '<path d="M9 5.5v13M15 5.5v13"/>';

    // meeting banner
    const banner = $("meetingBanner");
    const dismissed = sessionStorage.getItem(bannerDismissKey()) === meetingKey(s);
    if (s.meeting_detected && !dismissed) {
      banner.hidden = false;
    } else {
      banner.hidden = true;
    }
  }

  const meetingKey = (s) => String(s.meeting_detected ? "m1" : "m0");

  async function togglePause() {
    const s = await api("/api/status");
    await api(s.paused ? "/api/resume" : "/api/pause", { method: "POST" });
    await refreshStatus();
  }

  async function startMeetingFromBanner() {
    try {
      await api("/api/meetings/start", { method: "POST", body: { title: "" } });
      toast("Recording started", { kind: "success", title: "Meeting" });
      sessionStorage.setItem(bannerDismissKey(), meetingKey({ meeting_detected: true }));
      $("meetingBanner").hidden = true;
      refreshStatus();
    } catch (err) {
      toastError(err, "Could not start recording");
    }
  }

  function wireHeader() {
    $("pauseBtn").addEventListener("click", () => togglePause().catch(toastError));
    $("bannerRecordBtn").addEventListener("click", startMeetingFromBanner);
    $("bannerDismissBtn").addEventListener("click", () => {
      sessionStorage.setItem(bannerDismissKey(), meetingKey({ meeting_detected: true }));
      $("meetingBanner").hidden = true;
    });
  }

  // ------------------------------------------------------------------
  // Ask view
  // ------------------------------------------------------------------

  const SUGGESTED = [
    "What was I working on this morning?",
    "Summarize yesterday",
    "What did I promise to do today?",
  ];

  function askView(main) {
    main.innerHTML = `
      <div class="ask">
        <div class="ask-top">
          <span class="chat-label" id="chatLabel">${esc(state.chatLabel || "New conversation")}</span>
          <button class="btn btn-ghost btn-small" id="newChatBtn" type="button">${svgIcon("spark")} New</button>
          <button class="btn btn-ghost btn-small" id="chatsBtn" type="button">History</button>
        </div>
        <div class="ask-scroll" id="askScroll">
          <div class="ask-col" id="askCol"></div>
        </div>
        <div class="ask-composer-wrap">
          <div class="composer">
            <textarea id="composerInput" rows="1" placeholder="Ask Magpie anything about your day…" aria-label="Message"></textarea>
            <button class="composer-send" id="sendBtn" type="button" aria-label="Send">${svgIcon("spark")}</button>
          </div>
          <div class="ask-hint">Magpie only answers from what it saw on your screen. Cite chips open the source.</div>
        </div>
      </div>`;

    const col = $("askCol");
    const scroll = $("askScroll");
    const input = $("composerInput");
    const sendBtn = $("sendBtn");

    const atBottom = () => scroll.scrollHeight - scroll.scrollTop - scroll.clientHeight < 80;
    const scrollToBottom = () => { scroll.scrollTop = scroll.scrollHeight; };

    const renderChips = () => {
      if (col.children.length) return;
      col.innerHTML = `
        <div class="ask-hero">
          <div class="empty-title" style="font-size:20px">What did you get up to?</div>
          <div class="empty-line">Magpie answers from your own screen memory — nothing leaves your Mac.</div>
          <div class="suggest-chips">${SUGGESTED.map((t) => `<button class="chip" type="button" data-prompt="${esc(t)}">${esc(t)}</button>`).join("")}</div>
        </div>`;
      for (const chip of col.querySelectorAll("[data-prompt]")) {
        chip.addEventListener("click", () => { input.value = chip.dataset.prompt; send(); });
      }
    };

    const addUser = (text) => {
      const el = document.createElement("div");
      el.className = "msg-user";
      el.textContent = text;
      return el;
    };

    const addAssistant = () => {
      const wrap = document.createElement("div");
      wrap.className = "msg-assistant";
      wrap.innerHTML = `<div class="msg-meta"><span class="who">Magpie</span></div><div class="md"><span class="caret"></span></div><div class="cite-row" hidden></div>`;
      return wrap;
    };

    const citationsHTML = (cits) => {
      if (!cits || !cits.length) return "";
      return cits
        .map((c, i) => {
          const n = i + 1;
          const app = c.app ? String(c.app) : "Capture";
          const time = c.ts ? clock(c.ts) : "";
          return `<button class="cite-chip" type="button" data-cap="${c.id}" title="${esc(app + (time ? " · " + time : ""))}"><span class="n">[${n}]</span>${esc(app)}${time ? " · " + esc(time) : ""}</button>`;
        })
        .join("");
    };

    const citeTooltip = (c) => {
      const app = c && c.app ? String(c.app) : "Capture";
      const time = c && c.ts ? clock(c.ts) : "";
      return time ? `${app} · ${time}` : app;
    };

    // Event delegation on the conversation column (survives markdown
    // re-renders mid-stream): cite chips AND inline [n] superscripts both
    // open the source capture modal.
    col.addEventListener("click", (e) => {
      const chip = e.target.closest(".cite-row [data-cap]");
      if (chip) {
        openCaptureModal(chip.dataset.cap);
        return;
      }
      const inline = e.target.closest("a.cite-inline");
      if (inline) {
        e.preventDefault();
        const n = Number(inline.dataset.cite);
        const bubble = inline.closest(".msg-assistant");
        let cit = ((bubble && bubble.__citations) || [])[n - 1];
        if (!cit || cit.id == null) {
          // fall back to the visible source chips below the answer
          const scope = bubble || col;
          const byIndex = scope.querySelectorAll(".cite-row .cite-chip[data-cap]")[n - 1];
          if (byIndex) openCaptureModal(byIndex.dataset.cap);
          return;
        }
        openCaptureModal(cit.id);
      }
    });

    const wireCites = (scope, cits) => {
      if (!scope) return;
      scope.__citations = cits || [];
      for (const a of scope.querySelectorAll(".md a.cite-inline")) {
        const cit = (cits || [])[Number(a.dataset.cite) - 1];
        a.title = cit ? citeTooltip(cit) : "";
        if (cit) a.tabIndex = 0;
      }
    };

    async function send() {
      const text = input.value.trim();
      if (!text || state.sending) return;
      input.value = "";
      autoGrow();
      sendBtn.disabled = true;
      state.sending = true;
      const hero = col.querySelector(".ask-hero");
      if (hero) hero.remove();
      col.appendChild(addUser(text));
      const bubble = addAssistant();
      col.appendChild(bubble);
      scrollToBottom();

      let acc = "";
      const mdEl = bubble.querySelector(".md");
      const citeRow = bubble.querySelector(".cite-row");
      let citations = [];

      try {
        const res = await fetch("/api/chat", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ chat_id: state.chatId, message: text }),
        });
        if (!res.ok || !res.body) {
          let detail = res.statusText || "Chat failed";
          try {
            const j = await res.json();
            if (j && j.detail) detail = j.detail;
          } catch (_) {}
          throw new Error(detail);
        }
        const reader = res.body.getReader();
        const decoder = new TextDecoder();
        let buf = "";
        let done = false;
        while (!done) {
          const { value, done: streamDone } = await reader.read();
          if (streamDone) break;
          buf += decoder.decode(value, { stream: true });
          let idx;
          while ((idx = buf.indexOf("\n\n")) >= 0) {
            const frame = buf.slice(0, idx);
            buf = buf.slice(idx + 2);
            if (!frame.startsWith("data:")) continue;
            let evt;
            try {
              evt = JSON.parse(frame.slice(5).trim());
            } catch (_) {
              continue;
            }
            if (evt.error) throw new Error(evt.error);
            if (evt.delta) {
              acc += evt.delta;
              const wasBottom = atBottom();
              mdEl.innerHTML = renderMarkdown(acc) + '<span class="caret"></span>';
              if (wasBottom) scrollToBottom();
            }
            if (evt.done) {
              done = true;
              citations = evt.citations || [];
              state.chatId = evt.chat_id ?? state.chatId;
            }
          }
        }
        mdEl.innerHTML = renderMarkdown(acc) || '<span class="empty-line" style="color:var(--text-3)">…the model returned nothing. Check your provider in Settings.</span>';
        citeRow.hidden = !citations.length;
        citeRow.innerHTML = citationsHTML(citations);
        wireCites(bubble, citations);
        const label = $("chatLabel");
        if (label && !state.chatLabel) {
          state.chatLabel = text.length > 60 ? text.slice(0, 57) + "…" : text;
          label.textContent = state.chatLabel;
        }
        if (/open in mail|draft (an? )?(email|reply)|subject:/i.test(acc)) {
          const actions = document.createElement("div");
          actions.className = "draft-actions";
          actions.innerHTML = `
            <button class="btn btn-small" type="button" id="draftMailBtn">${svgIcon("mail")} Open in Mail</button>
            <button class="btn btn-small" type="button" id="draftCopyBtn">${svgIcon("copy")} Copy</button>`;
          bubble.appendChild(actions);
          actions.querySelector("#draftMailBtn").addEventListener("click", () =>
            sendEmailDraft({ subject: text, body: acc }).catch((err) => toastError(err, "Mail draft failed"))
          );
          actions.querySelector("#draftCopyBtn").addEventListener("click", () => {
            navigator.clipboard.writeText(acc).then(
              () => toast("Copied", { kind: "success" }),
              () => toast("Could not copy", { kind: "error" })
            );
          });
        }
      } catch (err) {
        toastError(err, "Ask failed");
        let msg = "";
        if (acc) {
          msg = renderMarkdown(acc);
        } else {
          msg = `<span class="fine">The model did not return anything. Check your provider in Settings.</span>`;
        }
        mdEl.innerHTML = msg + `<div class="error-box" style="box-shadow:none"><span>${esc(err.message || String(err))}</span></div>`;
        if (state.chatId) {
          wireCites(bubble, []);
        }
      } finally {
        state.sending = false;
        sendBtn.disabled = false;
        input.focus();
        if (atBottom()) scrollToBottom();
      }
    }

    async function loadChat(id) {
      try {
        const data = await api(`/api/chats/${id}`);
        state.chatId = id;
        state.chatLabel = data.chat && data.chat.title ? data.chat.title : "";
        $("chatLabel").textContent = state.chatLabel;
        col.innerHTML = "";
        closeSheet();
        for (const m of data.messages || []) {
          if (m.role === "user") {
            col.appendChild(addUser(m.content));
          } else {
            const bubble = addAssistant();
            const mdEl = bubble.querySelector(".md");
            mdEl.innerHTML = renderMarkdown(m.content);
            col.appendChild(bubble);
            const cits = m.citations || [];
            const row = bubble.querySelector(".cite-row");
            row.hidden = !cits.length;
            row.innerHTML = citationsHTML(cits);
            wireCites(bubble, cits);
          }
        }
        scrollToBottom();
      } catch (err) {
        toastError(err, "Could not load chat");
      }
    }

    const newChat = () => {
      state.chatId = null;
      state.chatLabel = "";
      $("chatLabel").textContent = "New conversation";
      col.innerHTML = "";
      closeSheet();
      renderChips();
    };

    let sheetOpen = false;
    const closeSheet = () => {
      const sheet = document.querySelector(".chat-menu");
      if (sheet) {
        sheet.remove();
        document.removeEventListener("click", onDocClick, true);
      }
      sheetOpen = false;
    };
    const onDocClick = (e) => {
      if (sheetOpen && !e.target.closest(".chat-menu") && e.target.id !== "chatsBtn") closeSheet();
    };

    $("chatsBtn").addEventListener("click", async () => {
      if (sheetOpen) { closeSheet(); return; }
      let chats = [];
      try {
        const data = await api("/api/chats");
        chats = data.chats || [];
      } catch (err) {
        toastError(err, "Could not load chats");
        return;
      }
      const sheet = document.createElement("div");
      sheet.className = "chat-menu";
      sheet.innerHTML = chats.length
        ? chats
            .map(
              (c) => `
          <button class="chat-menu-item" type="button" data-chat="${c.id}">
            <div class="t">${esc(c.title || "Untitled")}</div>
            <div class="d">${esc(ago(c.created_ts))}</div>
          </button>`
            )
            .join("")
        : `<div class="chat-menu-item"><div class="t" style="color:var(--text-3)">No conversations yet</div></div>`;
      document.querySelector(".ask-top").appendChild(sheet);
      sheetOpen = true;
      document.addEventListener("click", onDocClick, true);
      for (const item of sheet.querySelectorAll("[data-chat]")) {
        item.addEventListener("click", () => loadChat(Number(item.dataset.chat)));
      }
    });

    $("newChatBtn").addEventListener("click", newChat);
    sendBtn.addEventListener("click", send);

    input.addEventListener("keydown", (e) => {
      if (e.key === "Enter" && !e.shiftKey) {
        e.preventDefault();
        send();
      }
    });

    const autoGrow = () => {
      input.style.height = "auto";
      input.style.height = Math.min(160, input.scrollHeight) + "px";
    };
    input.addEventListener("input", autoGrow);

    activeViewCleanup = () => { closeSheet(); };
    renderChips();
    if (state.chatId) loadChat(state.chatId);
    input.focus();
  }

  // ------------------------------------------------------------------
  // Search view
  // ------------------------------------------------------------------

  function searchView(main) {
    const s = state.searchState;
    main.innerHTML = `
      <div class="search-bar">
        <div class="search-input-wrap">
          ${svgIcon("search")}
          <input class="input" id="searchInput" placeholder="Search everything you’ve seen… ( / )" value="${esc(s.q)}">
        </div>
        <input class="input" id="searchApp" placeholder="App filter" value="${esc(s.app)}">
        <input class="input" type="date" id="searchStart" value="${esc(s.start)}" aria-label="From">
        <input class="input" type="date" id="searchEnd" value="${esc(s.end)}" aria-label="To">
        <button class="btn btn-accent" id="searchGo" type="button">Search</button>
      </div>
      <div id="searchResults"></div>`;

    const results = $("searchResults");
    const runSearch = async () => {
      const q = $("searchInput").value.trim();
      const app = $("searchApp").value.trim();
      const start = $("searchStart").value;
      const end = $("searchEnd").value;
      if (!q && !app && !start && !end) {
        results.innerHTML = emptyState("search", "Search your screen memory", "Type words you remember seeing — Magpie finds the moment, with a screenshot.");
        return;
      }
      state.searchState = { q, app, start, end, ran: true };
      results.innerHTML = skeletonCards(4);
      try {
        const params = new URLSearchParams();
        if (q) params.set("q", q);
        if (app) params.set("app", app);
        if (start) params.set("start", start);
        if (end) params.set("end", end);
        params.set("limit", "50");
        const data = await api("/api/search?" + params.toString());
        if (!data.results || !data.results.length) {
          results.innerHTML = emptyState("search", "No matches", "Try fewer or different words, or widen the date range.");
          return;
        }
        results.innerHTML = `<div class="result-count">${data.count} result${data.count === 1 ? "" : "s"}</div>
          <div class="result-list">${data.results.map(resultCard).join("")}</div>`;
        for (const card of results.querySelectorAll("[data-cap]")) {
          card.addEventListener("click", () => openCaptureModal(card.dataset.cap));
        }
      } catch (err) {
        results.innerHTML = errorBox(err);
        wireRetry(results, runSearch);
      }
    };

    function resultCard(r) {
      return `
      <button class="result-card" type="button" data-cap="${r.id}">
        ${appBadge(r.app)}
        <div class="result-main">
          <div class="result-top">
            <span class="result-app">${esc(r.app || "Unknown")}</span>
            <span class="result-time">${esc(fullTime(r.ts))}</span>
            ${Number(r.similar_count) > 1 ? `<span class="result-similar">+${Number(r.similar_count) - 1} similar</span>` : ""}
          </div>
          ${r.window_title ? `<div class="result-title" title="${esc(r.window_title)}">${esc(r.window_title)}</div>` : ""}
          <div class="result-snippet">${formatSnippet(r.snippet)}</div>
        </div>
        ${r.image_path ? `<img class="result-thumb" loading="lazy" src="/api/captures/${r.id}/image" alt="">` : ""}
      </button>`;
    }

    const formatSnippet = (snip) => {
      let text = String(snip || "");
      text = esc(text);
      text = text.split("\u0026lt;mark\u0026gt;").join("<mark>");
      text = text.split("\u0026lt;/mark\u0026gt;").join("</mark>");
      return text;
    };

    $("searchGo").addEventListener("click", runSearch);
    $("searchInput").addEventListener("keydown", (e) => {
      if (e.key === "Enter") runSearch();
    });
    ["searchApp", "searchStart", "searchEnd"].forEach((id) =>
      $(id).addEventListener("change", runSearch)
    );

    if (state.searchState.ran) runSearch();
    else results.innerHTML = emptyState("search", "Search your screen memory", "Type words you remember seeing — Magpie finds the moment, with a screenshot.");
  }

  // ------------------------------------------------------------------
  // Timeline view
  // ------------------------------------------------------------------

  function timelineView(main) {
    const day = state.timelineDay || todayKey();
    let loadSeq = state.timelineSeq || 0;
    state.timelineSeq = ++loadSeq;
    main.innerHTML = `
      <div class="view-head">
        ${dayPicker("tlDay", day)}
        <span class="view-head-label" id="tlDayLabel">${esc(relDay(day))}</span>
        <div class="spacer"></div>
        <button class="btn" id="tlPrev" type="button" aria-label="Previous day">‹</button>
        <button class="btn" id="tlToday" type="button">Today</button>
        <button class="btn" id="tlNext" type="button" aria-label="Next day">›</button>
      </div>
      <div id="tlBody">${skeletonCards(2)}</div>`;

    const body = $("tlBody");

    const load = async () => {
      const token = ++loadSeq;
      try {
        const data = await api(`/api/timeline?day=${encodeURIComponent(day)}`);
        if (token !== loadSeq || !document.getElementById("tlBody")) return;
        if (!(data.activities || []).length) {
          body.innerHTML = emptyState(
            "camera",
            "Nothing captured this day",
            "Capture runs every few seconds while you use your Mac.",
            { id: "tlCaptureNow", label: "Capture now", accent: true }
          );
          const capNow = $("tlCaptureNow");
          if (capNow) capNow.addEventListener("click", async () => {
            capNow.disabled = true;
            try {
              await api("/api/capture-now", { method: "POST" });
            } catch (err) {
              toastError(err, "Capture failed");
            }
            capNow.disabled = false;
            load();
          });
          return;
        }
        renderTimeline({ ...data, activities: data.activities, app_usage: data.app_usage || [] });
      } catch (err) {
        if (token !== loadSeq || !document.getElementById("tlBody")) return;
        body.innerHTML = errorBox(err);
        wireRetry(body, load);
      }
    };

    function renderTimeline(data) {
      const acts = data.activities || [];
      const usage = data.app_usage || [];
      const start = new Date(day + "T00:00:00").getTime() / 1000;
      const end = start + 86400;
      const nowTs = Date.now() / 1000;
      const isToday = day === todayKey();

      const fmtHour = (h) => (h === 0 ? "12am" : h < 12 ? h + "am" : h === 12 ? "12pm" : h < 24 ? (h - 12) + "pm" : "");
      const hours = Array.from({ length: 25 }, (_, i) =>
        `<span>${i % 3 === 0 ? fmtHour(i) : ""}</span>`
      ).join("");

      const blocks = acts
        .map((a) => {
          const s = Math.max(a.clipped_start_ts, start);
          const e = Math.min(a.clipped_end_ts, end);
          if (e <= s) return "";
          const left = ((s - start) / 86400) * 100;
          const width = Math.max(0.15, ((e - s) / 86400) * 100);
          const app = a.app || "Unknown";
          return `<div class="tl-block" style="left:${left}%;width:${width}%;--h:${hueFor(app)}" data-tip="${esc(a.id)}"></div>`;
        })
        .join("");

      const nowLine = isToday && nowTs > start && nowTs < end
        ? `<div class="tl-now" style="left:${((nowTs - start) / 86400) * 100}%"></div>`
        : "";

      body.innerHTML = `
        <div class="card">
          <div class="tl-track-wrap">
            <div class="tl-track">${blocks}${nowLine}</div>
          </div>
          <div class="tl-hours">${hours}</div>
        </div>
        <div class="tl-cols">
          <div class="card">
            <div class="card-title">Activity</div>
            <div class="card-sub">${acts.length} session${acts.length === 1 ? "" : "s"} · ${esc(dateLabel(day))}</div>
            <div class="act-list">${acts.map(actRow).join("")}</div>
          </div>
          <div class="card">
            <div class="card-title">App usage</div>
            <div class="card-sub">Time in each app${isToday ? " so far" : ""}</div>
            ${usage.map(usageRow).join("") || `<p class="hint" style="color:var(--text-3)">No usage recorded.</p>`}
          </div>
        </div>`;

      function actRow(a) {
        const app = a.app || "Unknown";
        return `
        <div class="act-row" data-act="${a.id}">
          <span class="act-dot" style="--h:${hueFor(app)}"></span>
          <span class="act-app">${esc(app)}</span>
          <span class="act-title" title="${esc(a.window_title || "")}">${esc(a.window_title || "")}</span>
          <span class="act-time">${clock(a.clipped_start_ts)} – ${clock(a.clipped_end_ts)} · ${fmtDur(a.duration)}</span>
        </div>`;
      }

      function usageRow(u) {
        const max = Math.max(1, ...usage.map((row) => Math.max(0, Number(row.seconds) || 0)));
        const seconds = Math.max(0, Number(u.seconds) || 0);
        const pct = max > 0 ? Math.max(2, Math.round((seconds / max) * 100)) : 0;
        return `
        <div class="usage-row">
          <span class="usage-app" title="${esc(u.app)}">${esc(u.app || "Unknown")}</span>
          <span class="usage-bar-wrap"><span class="usage-bar" style="width:${pct}%;--h:${hueFor(u.app || "")}"></span></span>
          <span class="usage-time">${fmtDur(seconds)}</span>
        </div>`;
      }

      // hover tooltip
      const track = body.querySelector(".tl-track");
      const tip = document.createElement("div");
      tip.className = "tl-tooltip";
      tip.hidden = true;
      document.body.appendChild(tip);
      const showTip = (block) => {
        const act = acts.find((a) => String(a.id) === block.dataset.tip);
        if (!act) return;
        tip.hidden = false;
        tip.innerHTML = `
          <div class="tt-app">${esc(act.app || "Unknown")}</div>
          ${act.window_title ? `<div class="tt-title">${esc(act.window_title)}</div>` : ""}
          <div class="tt-time">${clock(act.clipped_start_ts)} – ${clock(act.clipped_end_ts)} · ${fmtDur(act.duration)}</div>`;
        const r = block.getBoundingClientRect();
        const tw = tip.offsetWidth;
        let x = r.left + r.width / 2 - tw / 2;
        x = Math.max(8, Math.min(x, window.innerWidth - tw - 8));
        tip.style.left = x + "px";
        tip.style.top = Math.max(8, r.top - tip.offsetHeight - 8) + "px";
      };
      for (const block of body.querySelectorAll(".tl-block")) {
        block.addEventListener("mouseenter", () => showTip(block));
        block.addEventListener("mouseleave", () => { tip.hidden = true; });
      }
      activeViewCleanup = () => tip.remove();
    }

    $("tlPrev").addEventListener("click", () => { shiftDay(-1); });
    $("tlNext").addEventListener("click", () => { shiftDay(1); });
    $("tlToday").addEventListener("click", () => { state.timelineDay = null; setView("timeline"); });
    wireDayPicker("tlDay", (v) => {
      if (!v) return;
      state.timelineDay = v;
      setView("timeline");
    });

    function shiftDay(delta) {
      const [y, m, d] = (state.timelineDay || todayKey()).split("-").map(Number);
      const dt = new Date(y, m - 1, d + delta);
      state.timelineDay = `${dt.getFullYear()}-${String(dt.getMonth() + 1).padStart(2, "0")}-${String(dt.getDate()).padStart(2, "0")}`;
      setView("timeline");
    }

    load();
  }

  // ------------------------------------------------------------------
  // Journal view
  // ------------------------------------------------------------------

  function journalView(main) {
    const day = state.journalDay || todayKey();
    main.innerHTML = `
      <div class="view-head">
        ${dayPicker("jrDay", day)}
        <span class="view-head-label">${esc(relDay(day))}</span>
        <div class="spacer"></div>
        <button class="btn" id="jrCopy" type="button" title="Copy as Markdown">${svgIcon("copy")} Copy</button>
        <button class="btn btn-accent" id="jrRegen" type="button">${svgIcon("refresh")} Regenerate</button>
      </div>
      <div id="jrBody">${skeletonCards(2)}</div>`;

    currentJournal = null;
    const body = $("jrBody");

    const load = async (regenerate = false) => {
      body.innerHTML = skeletonCards(2);
      const regen = $("jrRegen");
      regen.disabled = true;
      let slowTimer = null;
      const controller = new AbortController();
      const timeoutId = setTimeout(() => controller.abort(), 90000);
      slowTimer = setTimeout(() => {
        if (body.querySelector(".skel-stack")) {
          body.innerHTML = `
            <div class="card journal-gen-card">
              <div class="journal-gen">
                <span class="spinner"></span>
                <div>
                  <strong>Writing your journal…</strong>
                  <p class="hint" style="margin:2px 0 0">Magpie is reading the day’s activity — this can take a minute.</p>
                </div>
              </div>
            </div>`;
        }
      }, 6000);
      try {
        const url = regenerate ? "/api/journal/regenerate?day=" : "/api/journal?day=";
        const data = await api(url + encodeURIComponent(day), {
          ...(regenerate ? { method: "POST" } : {}),
          signal: controller.signal,
        });
        if (!(data.calendar || []).length) {
          const calendar = await api(
            "/api/calendar?day=" + encodeURIComponent(day)
          ).catch(() => null);
          if (calendar && Array.isArray(calendar.events)) {
            data.calendar = calendar.events;
          }
        }
        currentJournal = data;
        const meta = data.cached ? "saved" : "fresh";
        body.innerHTML = `
          <div class="journal-meta">
            ${data.summary ? `${meta} · ${fmtModel(data.model)}` : ""}
          </div>
          ${data.calendar && data.calendar.length ? `<div class="card calendar-card"><div class="card-title">On your calendar</div>${calendarListHTML(data.calendar)}</div>` : ""}
          ${data.calendar_error ? `<p class="hint calendar-error">${esc(data.calendar_error)}</p>` : ""}
          <div class="card">
            ${data.summary ? `<div class="md" id="jrMd">${renderMarkdown(data.summary)}</div>` : emptyState("spark", "No journal yet", "Magpie writes a journal at the end of each day, or you can ask for one now.", { id: "jrGenerate", label: "Generate now", accent: true })}
          </div>
          ${data.summary ? `<div class="journal-notion"><button class="btn btn-small" id="jrNotion" type="button">Send to Notion</button></div>` : ""}`;
        const gen = $("jrGenerate");
        if (gen) gen.addEventListener("click", () => load(true));
        const notion = $("jrNotion");
        if (notion) notion.addEventListener("click", async () => {
          notion.disabled = true;
          try {
            await api("/api/actions/notion", {
              method: "POST",
              body: { title: `Magpie journal — ${day}`, content: data.summary },
            });
            toast("Sent to Notion", { kind: "success", title: "Journal" });
          } catch (err) {
            toastError(err, "Notion");
          } finally {
            notion.disabled = false;
          }
        });
      } catch (err) {
        if (err && err.name === "AbortError") {
          err = new Error("The journal request timed out — the model may still be busy. Try again in a moment.");
        }
        if (err.status === 400 && /no (activity|data)/i.test(err.message || "")) {
          body.innerHTML = emptyState("spark", "Nothing to journal", "There wasn’t enough activity on this day to write a journal.");
        } else {
          body.innerHTML = errorBox(err);
          wireRetry(body, () => load(regenerate));
        }
      } finally {
        clearTimeout(slowTimer);
        clearTimeout(timeoutId);
        regen.disabled = false;
      }
    };

    const fmtModel = (m) => {
      if (!m) return "";
      const name = String(m).split(":")[0];
      return name.charAt(0).toUpperCase() + name.slice(1);
    };

    $("jrRegen").addEventListener("click", () => load(true));
    $("jrCopy").addEventListener("click", () => {
      const text = currentJournal && currentJournal.summary;
      if (!text) {
        toast("No journal to copy yet", { kind: "error" });
        return;
      }
      navigator.clipboard.writeText(`# Journal — ${day}\n\n${text}`).then(
        () => toast("Journal copied as Markdown", { kind: "success" }),
        () => toast("Could not copy", { kind: "error" })
      );
    });

    wireDayPicker("jrDay", (v) => {
      if (!v) return;
      state.journalDay = v;
      setView("journal");
    });
    load();
  }

  let currentJournal = null;

  // ------------------------------------------------------------------
  // Loops view
  // ------------------------------------------------------------------

  function loopsView(main) {
    main.innerHTML = `
      <div class="view-head">
        <div class="filter-chips" id="loopsFilterChips">
          <button class="filter-chip ${state.loopsFilter === "open" ? "active" : ""}" data-f="open" type="button">Open</button>
          <button class="filter-chip ${state.loopsFilter === "done" ? "active" : ""}" data-f="done" type="button">Done</button>
          <button class="filter-chip ${state.loopsFilter === "dismissed" ? "active" : ""}" data-f="dismissed" type="button">Dismissed</button>
        </div>
        <div class="spacer"></div>
        ${dayPicker("loopsDay", state.loopsDay || "")}
        <button class="btn" id="loopsFind" type="button" title="Scan today’s captures for follow-ups">${svgIcon("spark")} Find loops</button>
      </div>
      <div id="loopsBody">${skeletonCards(3)}</div>`;

    const body = $("loopsBody");

    const load = async () => {
      body.innerHTML = skeletonCards(3);
      try {
        const params = new URLSearchParams({ status: state.loopsFilter });
        if (state.loopsDay) params.set("day", state.loopsDay);
        const data = await api("/api/loops?" + params.toString());
        if (!data.loops.length) {
          const lines = {
            open: ["No open loops", "Screen prompts, promises and to-dos Magpie noticed will appear here."],
            done: ["Nothing done yet", "Loops you tick off will collect here."],
            dismissed: ["Nothing dismissed", "Loops you dismiss will collect here."],
          }[state.loopsFilter];
          body.innerHTML = emptyState("check", lines[0], lines[1], { id: "loopsFindEmpty", label: "Scan today’s screen", accent: true });
          const fe = $("loopsFindEmpty");
          if (fe) fe.addEventListener("click", findLoops);
          return;
        }
        body.innerHTML = `<div class="loop-list">${data.loops.map(loopRow).join("")}</div>`;
        for (const row of body.querySelectorAll("[data-loop]")) {
          const id = row.dataset.loop;
          row.querySelector(".loop-check").addEventListener("click", () => setLoop(id, "done"));
          row.querySelector("[data-dismiss]").addEventListener("click", () => setLoop(id, "dismissed"));
          row.querySelector("[data-reminder]").addEventListener("click", () => toReminder(id));
          row.querySelector("[data-calendar]").addEventListener("click", () => toCalendar(id));
          row.querySelector("[data-email]").addEventListener("click", () => toEmail(id));
        }
      } catch (err) {
        body.innerHTML = errorBox(err);
        wireRetry(body, load);
      }
    };

    function loopRow(loop) {
      return `
      <div class="loop-row" data-loop="${loop.id}">
        <button class="loop-check" type="button" aria-label="Mark done">${svgIcon("check")}</button>
        <span class="loop-text">${esc(loop.text)}</span>
        <span class="loop-day">${esc(relDay(loop.day))}</span>
        <span class="loop-actions">
          <button class="btn btn-small" data-reminder type="button" title="Create a reminder">${svgIcon("bell")} → Reminder</button>
          <button class="btn btn-small" data-calendar type="button" title="Add to calendar">${svgIcon("calendar")} → Calendar</button>
          <button class="btn btn-small" data-email type="button" title="Draft an email (never sends)">${svgIcon("mail")} Draft email</button>
          <button class="btn btn-ghost btn-small" data-dismiss type="button" aria-label="Dismiss">${svgIcon("x")}</button>
        </span>
      </div>`;
    }

    const setLoop = async (id, status) => {
      try {
        await api(`/api/loops/${id}`, { method: "POST", body: { status } });
        if (status === "done") toast("Marked done", { kind: "success" });
        load();
      } catch (err) {
        toastError(err, "Could not update loop");
      }
    };

    const toReminder = async (id) => {
      const loop = findLoop(id);
      try {
        await api("/api/actions/reminder", {
          method: "POST",
          body: { title: loop.text },
        });
        toast("Reminder created", { kind: "success", title: "Reminders" });
      } catch (err) {
        toastError(err, "Reminder failed");
      }
    };

    const toCalendar = (id) => {
      const loop = findLoop(id);
      const row = document.querySelector(`[data-loop="${id}"]`);
      const anchor = row ? row.querySelector("[data-calendar]") : null;
      openCalendarPopover(anchor, {
        title: loop.text,
        onSave: async ({ start, end }) => {
          await api("/api/actions/event", {
            method: "POST",
            body: { title: loop.text, start, end },
          });
          toast("Event added to your calendar", { kind: "success", title: "Calendar" });
        },
      });
    };

    const toEmail = (id) => {
      const loop = findLoop(id);
      promptEmailDraft(loop.text, loop.text).catch((err) => {
        if (err && err.message !== "cancelled") toastError(err, "Mail draft failed");
      });
    };

    const findLoop = (id) => {
      // refetch the single loop among the loaded list
      const row = document.querySelector(`[data-loop="${id}"]`);
      return {
        id,
        text: row ? row.querySelector(".loop-text").textContent : "",
      };
    };

    const findLoops = async () => {
      const day = state.loopsDay || todayKey();
      body.innerHTML = `<div class="card"><div style="display:flex;gap:10px;align-items:center"><span class="spinner"></span> Reading today’s screen for follow-ups…</div></div>`;
      try {
        const data = await api("/api/loops/extract?day=" + encodeURIComponent(day), { method: "POST" });
        if (!data.count) {
          toast("No new follow-ups found today", { title: "Loops" });
        } else {
          toast(`Found ${data.count} new loop${data.count === 1 ? "" : "s"}`, { kind: "success", title: "Loops" });
          state.loopsFilter = "open";
        }
      } catch (err) {
        toastError(err, "Could not scan today");
      }
      setView("loops");
    };

    $("loopsFind").addEventListener("click", findLoops);
    for (const chip of $("loopsFilterChips").querySelectorAll("[data-f]")) {
      chip.addEventListener("click", () => {
        state.loopsFilter = chip.dataset.f;
        setView("loops");
      });
    }
    wireDayPicker("loopsDay", (v) => {
      state.loopsDay = v || null;
      load();
    });

    activeViewCleanup = () => closeCalendarPopover();
    load();
  }

  async function sendEmailDraft({ to = "", subject = "", body = "" }) {
    try {
      await api("/api/actions/email", { method: "POST", body: { to, subject, body } });
      toast("Mail draft opened — it was not sent", { kind: "success", title: "Mail" });
    } catch (err) {
      toastError(err, "Mail draft failed");
    }
  }

  function promptEmailDraft(subject, body) {
    return new Promise((resolve, reject) => {
      openModal({
        title: "Draft email",
        sub: "Opens a visible draft in Mail.app. Magpie never sends.",
        width: 560,
        body: `
          <div class="field"><label for="emTo">To</label><input class="input" id="emTo" placeholder="someone@example.com" value=""></div>
          <div class="field"><label for="emSubject">Subject</label><input class="input" id="emSubject" value="${esc(subject || "")}"></div>
          <div class="field"><label for="emBody">Body</label><textarea class="input" id="emBody" rows="7">${esc(body || "")}</textarea></div>`,
        foot: `<button class="btn" id="emCancel" type="button">Cancel</button>
               <button class="btn btn-accent" id="emSend" type="button">${svgIcon("mail")} Open draft</button>`,
      });
      $("emCancel").addEventListener("click", () => { closeModal(); reject(new Error("cancelled")); });
      $("emSend").addEventListener("click", async () => {
        const btn = $("emSend");
        btn.disabled = true;
        try {
          await api("/api/actions/email", {
            method: "POST",
            body: {
              to: $("emTo").value.trim(),
              subject: $("emSubject").value,
              body: $("emBody").value,
            },
          });
          toast("Mail draft opened — it was not sent", { kind: "success", title: "Mail" });
          closeModal();
          resolve();
        } catch (err) {
          toastError(err, "Mail draft failed");
          btn.disabled = false;
          reject(err);
        }
      });
    });
  }

  // ------------------------------------------------------------------
  // Meetings view
  // ------------------------------------------------------------------

  function renderTranscript(text) {
    const lines = String(text || "").split(/\r?\n/).filter((line) => line.trim());
    if (!lines.length) {
      return `<div class="transcript-view">No transcript.</div>`;
    }
    const pattern = /^\[(\d{2}:\d{2}:\d{2})\]\s+(Me|Them):\s*(.*)$/;
    const bubbles = lines.map((line) => {
      const match = line.match(pattern);
      if (!match) {
        return `<div class="bubble-row is-them"><div class="bubble them"><span class="bubble-text">${esc(line)}</span></div></div>`;
      }
      const [, time, speaker, body] = match;
      const mine = speaker === "Me";
      return `
        <div class="bubble-row ${mine ? "is-me" : "is-them"}">
          <div class="bubble ${mine ? "me" : "them"}">
            <div class="bubble-meta"><span>${speaker}</span><time>${time}</time></div>
            <div class="bubble-text">${esc(body)}</div>
          </div>
        </div>`;
    });
    return `<div class="transcript-thread">${bubbles.join("")}</div>`;
  }

  function meetingsView(main) {
    main.innerHTML = `
      <div class="meetings-layout">
        <div class="recorder-card card" id="recCard">
          <button class="rec-btn" id="recBtn" type="button" aria-label="Start recording"><span class="rec-dot"></span></button>
          <div class="rec-timer" id="recTimer">00:00</div>
          <div class="rec-state" id="recState">Ready to record</div>
          <div class="rec-row">
            <input class="input" id="recTitle" placeholder="Meeting title (optional)" style="max-width:280px">
          </div>
        </div>
        <div class="meeting-list-wrap">
          <div class="view-head"><div class="card-title" style="margin:0">Recent meetings</div><div class="spacer"></div></div>
          <div id="meetList">${skeletonCards(3)}</div>
        </div>
      </div>`;

    let timerInterval = null;
    let helperRecording = false;
    const modeLabel = () => (helperRecording ? "System + mic" : "mic audio only");

    const setRecordingUI = (startTs, id) => {
      const btn = $("recBtn");
      btn.classList.add("recording");
      btn.setAttribute("aria-label", "Stop recording");
      $("recState").textContent = `Recording — ${helperRecording ? "System + mic" : "mic only"}`;
      const tick = () => {
        const seconds = Date.now() / 1000 - startTs;
        $("recTimer").textContent = clockTimer(seconds);
      };
      tick();
      clearInterval(timerInterval);
      timerInterval = setInterval(tick, 1000);
      state.meetingActiveId = id;
    };

    const setIdleUI = () => {
      clearInterval(timerInterval);
      timerInterval = null;
      const btn = $("recBtn");
      btn.classList.remove("recording");
      btn.setAttribute("aria-label", "Start recording");
      $("recTimer").textContent = "00:00";
      $("recState").textContent = `Ready to record — ${modeLabel()}`;
      state.meetingActiveId = null;
    };

    const refreshRecState = async () => {
      try {
        const [status, meetings] = await Promise.all([
          api("/api/status"),
          api("/api/meetings"),
        ]);
        helperRecording = Boolean(status.helper_recording);
        const active = (meetings.meetings || []).find((m) => m.status === "recording");
        if (active) setRecordingUI(active.start_ts, active.id);
        else setIdleUI();
      } catch (_) {
        setIdleUI();
      }
    };

    $("recBtn").addEventListener("click", async () => {
      const state_ = state.meetingActiveId ? "stop" : "start";
      if (state_ === "start") {
        try {
          const meeting = await api("/api/meetings/start", {
            method: "POST",
            body: { title: $("recTitle").value },
          });
          if (meeting.status === "error") {
            toastError({ message: meeting.error || "Recording failed to start" }, "Meeting");
            return;
          }
          setRecordingUI(meeting.start_ts, meeting.id);
          toast("Recording…", { kind: "success", title: "Meeting" });
        } catch (err) {
          toastError(err, "Could not start recording");
        }
      } else {
        const id = state.meetingActiveId;
        const btn = $("recBtn");
        btn.disabled = true;
        $("recState").innerHTML = '<span class="spinner"></span> Transcribing — this can take a minute…';
        try {
          await api(`/api/meetings/${id}/stop`, { method: "POST" });
          toast("Meeting transcribed and noted", { kind: "success", title: "Meeting" });
        } catch (err) {
          toastError(err, "Could not stop meeting");
        }
        btn.disabled = false;
        await refreshRecState();
        loadList();
      }
    });

    const loadList = async () => {
      const list = $("meetList");
      try {
        const data = await api("/api/meetings");
        const rows = data.meetings || [];
        if (!rows.length) {
          list.innerHTML = emptyState("mic", "No meetings yet", "Hit record and Magpie takes notes for you — transcript, summary, actions.");
          return;
        }
        list.innerHTML = `<div class="result-list">${rows.map(meetingRow).join("")}</div>`;
        for (const row of list.querySelectorAll("[data-meeting]")) {
          row.addEventListener("click", () => openMeeting(row.dataset.meeting));
        }
      } catch (err) {
        list.innerHTML = errorBox(err);
        wireRetry(list, loadList);
      }
    };

    function meetingRow(m) {
      const badge = { recording: "recording", transcribing: "transcribing", done: "done", error: "error" }[m.status] || "";
      return `
      <button class="meeting-row" type="button" data-meeting="${m.id}">
        <span class="meeting-ic">${svgIcon("mic")}</span>
        <span class="meeting-main">
          <span class="meeting-title">${esc(m.title || "Untitled meeting")}</span>
          <span class="meeting-sub">${esc(fullTime(m.start_ts))}${m.end_ts ? " · " + fmtDur(m.end_ts - m.start_ts) : ""}</span>
        </span>
        ${m.error ? `<span class="badge error" title="${esc(m.error)}">error</span>` : `<span class="badge ${badge}">${m.status}</span>`}
      </button>`;
    }

    async function openMeeting(id) {
      openModal({ title: "Meeting", sub: "Loading…", body: skeletonCards(1) });
      try {
        const m = await api(`/api/meetings/${id}`);
        openModal({
          title: esc(m.title || "Untitled meeting"),
          sub: `${esc(fullTime(m.start_ts))}${m.end_ts ? " · " + fmtDur(m.end_ts - m.start_ts) : ""}`,
          width: 820,
          body: `
            ${m.error ? `<div class="error-box"><span>${esc(m.error)}</span></div>` : ""}
            ${m.notes_md || m.transcript ? `
              <div class="tab-btns">
                <button class="tab-btn active" data-tab="notes" type="button">Notes</button>
                <button class="tab-btn" data-tab="transcript" type="button">Transcript</button>
              </div>
              <div data-pane="notes" class="md">${m.notes_md ? renderMarkdown(m.notes_md) : "<p style='color:var(--text-3)'>No notes yet.</p>"}</div>
              <div data-pane="transcript" hidden>${renderTranscript(m.transcript)}</div>` :
              `<div class="empty"><div class="empty-title">Nothing recorded yet</div><div class="empty-line">This meeting has no transcript or notes.</div></div>`}`,
          foot: `
            <button class="btn" id="mtCopy" type="button">${svgIcon("copy")} Copy notes</button>
            <button class="btn btn-small" id="mtNotion" type="button">Send to Notion</button>`,
        });
        $("mtCopy").addEventListener("click", () => {
          navigator.clipboard.writeText(`# ${m.title || "Meeting"}\n\n${m.notes_md || ""}`).then(
            () => toast("Notes copied as Markdown", { kind: "success" }),
            () => toast("Could not copy", { kind: "error" })
          );
        });
        $("mtNotion").addEventListener("click", async () => {
          try {
            await api("/api/actions/notion", {
              method: "POST",
              body: { title: m.title || `Meeting ${m.id}`, content: m.notes_md || m.transcript || "" },
            });
            toast("Sent to Notion", { kind: "success", title: "Meeting notes" });
          } catch (err) {
            toastError(err, "Notion");
          }
        });
        for (const tab of document.querySelectorAll(".modal [data-tab]")) {
          tab.addEventListener("click", () => {
            document.querySelectorAll(".modal [data-tab]").forEach((t) => t.classList.toggle("active", t === tab));
            document.querySelectorAll(".modal [data-pane]").forEach((p) => {
              p.hidden = p.dataset.pane !== tab.dataset.tab;
            });
          });
        }
      } catch (err) {
        openModal({ title: "Meeting", body: errorBox(err, "retryMeeting") });
        $("retryMeeting").addEventListener("click", () => openMeeting(id));
      }
    }

    refreshRecState();
    loadList();
    activeViewCleanup = () => {
      if (timerInterval) clearInterval(timerInterval);
    };
  }

  // ------------------------------------------------------------------
  // Settings view
  // ------------------------------------------------------------------

  function settingsView(main) {
    main.innerHTML = `<div class="settings-grid">${skeletonCards(3)}</div>`;

    const load = async () => {
      try {
        const cfg = await api("/api/settings");
        state.settings = cfg;
        renderSettings(cfg);
      } catch (err) {
        main.innerHTML = errorBox(err);
        wireRetry(main, load);
      }
    };

    function renderSettings(cfg) {
      const prov = cfg.provider || "ollama";
      const profiles = cfg.providers || {};
      const prof = profiles[prov] || {};
      const keyState = prof.api_key && typeof prof.api_key === "object" ? prof.api_key.set : Boolean(prof.api_key);
      const interval = cfg.interval_seconds ?? 15;
      const idle = cfg.idle_pause_seconds ?? 120;
      const keep = !!cfg.keep_screenshots;
      const retention = cfg.retention_days ?? 30;
      const excluded = Array.isArray(cfg.excluded_apps) ? cfg.excluded_apps : [];
      const mic = cfg.mic_device ?? ":0";
      const notionTokenState = cfg.notion_token && typeof cfg.notion_token === "object" ? cfg.notion_token.set : Boolean(cfg.notion_token);
      const notionParent = cfg.notion_parent_page_id || "";
      const remoteEnabled = Boolean(cfg.remote_access);

      main.innerHTML = `
      <div class="settings-grid">
        <div class="card">
          <div class="card-title">Capture</div>
          <div class="card-sub">How often Magpie looks at your screen and what it keeps.</div>
          <div class="form-row">
            <div class="field"><label for="setInterval">Capture interval (seconds)</label>
              <input class="input" type="number" id="setInterval" min="5" max="600" value="${interval}"></div>
            <div class="field"><label for="setIdle">Pause capture after idle (seconds)</label>
              <input class="input" type="number" id="setIdle" min="0" max="3600" value="${idle}"></div>
            <div class="field"><label for="setRetention">Keep history for (days)</label>
              <input class="input" type="number" id="setRetention" min="1" max="3650" value="${retention}"></div>
          </div>
          <label class="switch"><input type="checkbox" id="setKeep" ${keep ? "checked" : ""}><span class="track"></span><span class="switch-label">Keep screenshots</span></label>
          <p class="hint" style="color:var(--text-3);font-size:12.5px;margin-top:6px">Turning this off keeps the extracted text but deletes stored images.</p>
          <div class="field" style="margin-top:16px"><label>Excluded apps</label>
            <div class="chip-edit" id="excludedChips">${excluded.map(chipTag).join("")}</div>
            <div class="chip-add">
              <input class="input" id="excludedInput" placeholder="Add an app name, e.g. Messages">
              <button class="btn" id="excludedAdd" type="button">Add</button>
            </div>
            <p class="hint">Captures in these apps are never stored.</p>
          </div>
        </div>

        <div class="card">
          <div class="card-title">Model provider</div>
          <div class="card-sub">Ask, journals and meeting notes are answered by this model.</div>
          <div class="form-row">
            <div class="field"><label for="setProvider">Provider</label>
              <select class="input" id="setProvider">
                ${["ollama", "openai", "anthropic"].map((p) => `<option value="${p}" ${prov === p ? "selected" : ""}>${p.charAt(0).toUpperCase() + p.slice(1)}${p === "ollama" ? " (local, default)" : ""}</option>`).join("")}
              </select></div>
            <div class="field"><label for="setModel">Model</label>
              <input class="input" id="setModel" value="${esc(prof.model || "")}"></div>
            <div class="field"><label for="setBase">Base URL</label>
              <input class="input" id="setBase" value="${esc(prof.base_url || "")}" placeholder="http://localhost:11434"></div>
            <div class="field"><label for="setKey">API key ${keyState ? '<span class="badge done">set</span>' : ""}</label>
              <input class="input" id="setKey" type="password" placeholder="${keyState ? "Key is set — type to replace" : "Required for OpenAI / Anthropic"}" autocomplete="off"></div>
          </div>
          <div class="field"><label for="setMic">Microphone device (ffmpeg)</label>
            <input class="input" id="setMic" value="${esc(mic)}" style="width:160px" placeholder=":0"></div>
          <button class="btn btn-accent" id="settingsSave" type="button">Save settings</button>
        </div>

        <div class="card">
          <div class="card-title">Notion</div>
          <div class="card-sub">Send journals and meeting notes to a page under your parent page.</div>
          <div class="field"><label for="setNotionToken">Integration token ${notionTokenState ? '<span class="badge done">set</span>' : ""}</label>
            <input class="input" id="setNotionToken" type="password" placeholder="${notionTokenState ? "Token is set — type to replace" : "secret_…"}" autocomplete="off"></div>
          <div class="field"><label for="setNotionParent">Parent page id</label>
            <input class="input" id="setNotionParent" value="${esc(notionParent)}" placeholder="32-character page id"></div>
          <p class="hint">Create an integration at notion.so/my-integrations and share the parent page with it.</p>
        </div>

        <div class="card">
          <div class="card-title">Phone access</div>
          <div class="card-sub">Open Magpie from your phone on the same Wi-Fi, or over Tailscale.</div>
          <label class="switch"><input type="checkbox" id="setRemote" ${remoteEnabled ? "checked" : ""}><span class="track"></span><span class="switch-label">Remote access</span></label>
          <p class="hint" style="color:var(--text-3);font-size:12.5px;margin-top:6px">Restart Magpie after changing this so the server can also bind 0.0.0.0. Non-local requests then need a paired device.</p>
          <div id="remoteBox" class="remote-box"><div class="remote-off">Loading phone access…</div></div>
        </div>

        <div class="card">
          <div class="card-title">Screen Recording permission</div>
          <div id="permBox">${permHTML(null)}</div>
        </div>

        <div class="card danger-zone">
          <div class="card-title">Your data</div>
          <div class="card-sub">Everything lives in ${esc("~/.magpie")} on this Mac only.</div>
          <div class="form-row">
            <button class="btn btn-danger" id="delHour" type="button">Delete last hour</button>
            <button class="btn btn-danger" id="delToday" type="button">Delete today</button>
          </div>
          <label class="field" style="margin-top:14px"><span style="font-size:13px;font-weight:550;color:var(--text-2)">Delete a date range</span></label>
          <div class="delete-range">
            <input class="input" type="date" id="delStart" aria-label="From">
            <span style="color:var(--text-3)">→</span>
            <input class="input" type="date" id="delEnd" aria-label="To">
            <button class="btn btn-danger" id="delRange" type="button">Delete range</button>
          </div>
          <div class="form-row" style="margin-top:14px">
            <a class="btn" href="/api/export" download="magpie-export.json">${svgIcon("dl")} Export everything as JSON</a>
          </div>
        </div>
      </div>`;

      function chipTag(name) {
        return `<span class="chip-tag">${esc(name)}<button type="button" data-chip="${esc(name)}" aria-label="Remove ${esc(name)}">${svgIcon("x")}</button></span>`;
      }

      function readChips() {
        return Array.from(document.querySelectorAll("#excludedChips [data-chip]")).map(
          (b) => b.dataset.chip
        );
      }

      document.querySelectorAll("#excludedChips [data-chip]").forEach((b) =>
        b.addEventListener("click", () => {
          b.closest(".chip-tag").remove();
        })
      );
      $("excludedAdd").addEventListener("click", () => {
        const input = $("excludedInput");
        const name = input.value.trim();
        if (!name) return;
        if (readChips().some((c) => c.toLowerCase() === name.toLowerCase())) {
          input.value = "";
          return;
        }
        document.getElementById("excludedChips").insertAdjacentHTML("beforeend", chipTag(name));
        const added = document.querySelector(`#excludedChips [data-chip="${CSS.escape(name)}"]`);
        added.addEventListener("click", () => added.closest(".chip-tag").remove());
        input.value = "";
      });
      $("excludedInput").addEventListener("keydown", (e) => {
        if (e.key === "Enter") { e.preventDefault(); $("excludedAdd").click(); }
      });

      $("settingsSave").addEventListener("click", async () => {
        const btn = $("settingsSave");
        btn.disabled = true;
        const newProvider = $("setProvider").value;
        const body = {
          interval_seconds: Number($("setInterval").value) || interval,
          idle_pause_seconds: Number($("setIdle").value) || idle,
          retention_days: Number($("setRetention").value) || retention,
          keep_screenshots: $("setKeep").checked,
          mic_device: $("setMic").value.trim() || ":0",
          excluded_apps: readChips(),
          provider: newProvider,
        };
        const model = $("setModel").value.trim();
        const base = $("setBase").value.trim();
        const key = $("setKey").value.trim();
        const notionToken = $("setNotionToken").value.trim();
        body.notion_parent_page_id = $("setNotionParent").value.trim();
        if (notionToken) body.notion_token = notionToken;
        body.providers = {};
        for (const p of ["ollama", "openai", "anthropic"]) {
          const existing = (profiles[p] && typeof profiles[p] === "object" ? profiles[p] : {}) || {};
          const clone = { ...existing };
          if (typeof clone.api_key === "object") delete clone.api_key; // never send the mask
          body.providers[p] = clone;
        }
        body.providers[newProvider].model = model;
        if (newProvider !== "anthropic" && base) body.providers[newProvider].base_url = base;
        if (key) body.providers[newProvider].api_key = key;
        try {
          const saved = await api("/api/settings", { method: "PUT", body });
          state.settings = saved;
          toast("Settings saved", { kind: "success" });
          renderSettings(saved);
        } catch (err) {
          toastError(err, "Could not save settings");
          btn.disabled = false;
        }
      });

      const remoteBox = $("remoteBox");

      const renderRemoteState = (remote) => {
        if (!remoteBox || !document.body.contains(remoteBox)) return;
        if (!remote || !remote.enabled) {
          remoteBox.innerHTML = `<div class="remote-off">Remote access is off. Turn it on above and restart Magpie to reach it from your phone.</div>`;
          return;
        }
        const urls = (remote && remote.urls) || [];
        const url = urls[0] || "";
        remoteBox.innerHTML = `
          <div class="remote-grid">
            <div class="remote-info">
              <div class="remote-label">LAN address</div>
              <div class="remote-url">${url ? `<a href="${esc(url)}" target="_blank" rel="noopener">${esc(url)}</a>` : "No LAN address detected — connect to Wi-Fi or use Tailscale."}</div>
              <div class="remote-label">Pairing code</div>
              <div class="remote-code">${esc(remote.pair_code || "—")}</div>
              <p class="hint">On your phone open the address above and enter this code, or scan the QR code.</p>
              <button class="btn btn-small" id="remoteRotate" type="button">${svgIcon("refresh")} New token &amp; code</button>
            </div>
            <div class="remote-qr">${remote.qr_svg || `<div class="remote-off">Restart Magpie to show the QR code.</div>`}</div>
          </div>`;
        const rotate = $("remoteRotate");
        if (rotate) rotate.addEventListener("click", async () => {
          rotate.disabled = true;
          try {
            const next = await api("/api/remote/rotate", { method: "POST" });
            renderRemoteState(next);
            toast("New token and pairing code generated", { kind: "success", title: "Phone access" });
          } catch (err) {
            toastError(err, "Could not rotate the token");
            rotate.disabled = false;
          }
        });
      };

      const loadRemote = async () => {
        try {
          renderRemoteState(await api("/api/remote"));
        } catch (err) {
          if (remoteBox && document.body.contains(remoteBox)) {
            remoteBox.innerHTML = `<div class="remote-off">${esc(err.message || String(err))}</div>`;
          }
        }
      };

      $("setRemote").addEventListener("change", async (event) => {
        const input = event.target;
        const enabled = input.checked;
        input.disabled = true;
        try {
          const saved = await api("/api/settings", {
            method: "PUT",
            body: { remote_access: enabled },
          });
          state.settings = saved;
          toast(
            enabled
              ? "Phone access enabled — restart Magpie to apply"
              : "Phone access turned off",
            { kind: "success", title: "Phone access" }
          );
          await loadRemote();
        } catch (err) {
          input.checked = !enabled;
          toastError(err, "Could not change phone access");
        } finally {
          input.disabled = false;
        }
      });

      loadRemote();

      const deleteRangeReq = async (start, end, label) => {
        if (!start || !end) {
          toast("Pick both dates", { kind: "error" });
          return;
        }
        if (!window.confirm(`Delete captured data ${label}? This cannot be undone.`)) return;
        try {
          const params = new URLSearchParams({ start, end });
          const res = await api("/api/data?" + params.toString(), { method: "DELETE" });
          const d = res.deleted || {};
          toast(`Deleted ${d.captures || 0} captures, ${d.activities || 0} activity blocks`, { kind: "success", title: "Data" });
        } catch (err) {
          toastError(err, "Delete failed");
        }
      };

      $("delHour").addEventListener("click", () => {
        const end = new Date();
        const start = new Date(end.getTime() - 3600 * 1000);
        deleteRangeReq(isoLocal(start), isoLocal(end), "from the last hour");
      });
      $("delToday").addEventListener("click", () => {
        deleteRangeReq(todayKey() + "T00:00:00", todayKey() + "T23:59:59", "today");
      });
      $("delRange").addEventListener("click", () => {
        deleteRangeReq($("delStart").value + "T00:00:00", $("delEnd").value + "T23:59:59", "in this range");
      });

      function isoLocal(d) {
        const p = (n) => String(n).padStart(2, "0");
        return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}T${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
      }

      // permission status card
      (async () => {
        const box = $("permBox");
        try {
          const s = await api("/api/status");
          const granted = s.permission ? s.permission.screen_recording : null;
          const problem = granted === false || Boolean(s.permission_problem);
          box.innerHTML = permHTML(granted, problem);
        } catch (_) {
          box.innerHTML = permHTML(null, false, "Could not check permission status.");
        }
      })();

      function permHTML(granted, problem, note) {
        if (granted === true && !problem) {
          return `<div class="perm-ok">${svgIcon("check")}<div><strong>Screen Recording is granted.</strong>
            <p class="perm-fix">Magpie can read your screen text and store screenshots.</p></div></div>`;
        }
        if (granted === false || problem) {
          return `<div class="perm-bad">${svgIcon("warn")}<div><strong>Screen Recording permission is missing.</strong>
            <p class="perm-fix">Magpie can’t capture anything without it. To fix:</p>
            <p class="perm-fix">1. Open <code>System Settings → Privacy & Security → Screen Recording</code>.<br>
            2. Find the app you launched Magpie from (Terminal, iTerm, or Magpie itself) and turn it on.<br>
            3. Quit and relaunch that app — macOS requires a restart after granting.</p>
            <p class="perm-fix">Until then Magpie will keep retrying and stay paused on captures.</p></div></div>`;
        }
        return `<div class="perm-bad">${svgIcon("warn")}<div><strong>Status unknown.</strong>
          <p class="perm-fix">${esc(note || "The permission helper could not be reached.")} Run <code>magpie doctor</code> to check.</p></div></div>`;
      }
    }

    load();
  }

  // ------------------------------------------------------------------
  // keyboard shortcuts
  // ------------------------------------------------------------------

  function wireShortcuts() {
    // True when the event target (or something between it and the document)
    // is a text entry point — typing must reach it, so shortcuts stay idle.
    const isTextEntry = (e) => {
      for (let el = e.target; el && el.nodeType === 1; el = el.parentElement) {
        const tag = el.tagName;
        if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT") return true;
        if (el.isContentEditable) return true;
      }
      return false;
    };

    // Focus an input as soon as the view that owns it has rendered.
    // location.hash assignment fires hashchange asynchronously, so keep
    // retrying across several frames (rAF + ticks) until the input exists.
    const focusWhenReady = (id) => {
      let tries = 0;
      const attempt = () => {
        const input = $(id);
        if (input && document.body.contains(input)) {
          input.focus({ preventScroll: false });
          return;
        }
        if (++tries < 40) {
          if (tries % 3 === 0) setTimeout(attempt, 50);
          else if (typeof requestAnimationFrame === "function") requestAnimationFrame(attempt);
          else setTimeout(attempt, 30);
        }
      };
      attempt();
    };

    const goTo = (route, inputId) => {
      if (state.route !== route) location.hash = `#/${route}`;
      focusWhenReady(inputId);
    };

    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape") {
        if ($("modalRoot").firstChild) { closeModal(); return; }
        const sheet = document.querySelector(".chat-menu");
        if (sheet) { sheet.remove(); return; }
        return;
      }
      if ((e.metaKey || e.ctrlKey) && !e.altKey && !e.shiftKey && typeof e.key === "string" && e.key.toLowerCase() === "k") {
        e.preventDefault();
        goTo("ask", "composerInput");
        return;
      }
      if (e.key !== "/" || e.metaKey || e.ctrlKey || e.altKey || e.shiftKey) return;
      if (isTextEntry(e)) return;
      e.preventDefault();
      goTo("search", "searchInput");
    });
  }

  // ------------------------------------------------------------------
  // router
  // ------------------------------------------------------------------

  const ROUTES = ["ask", "search", "timeline", "journal", "loops", "meetings", "settings"];

  const routeFromHash = () => {
    const hash = location.hash.replace(/^#\/?/, "");
    const name = hash.split("/")[0];
    return ROUTES.includes(name) ? name : "ask";
  };

  window.addEventListener("hashchange", () => setView(routeFromHash()));

  // ------------------------------------------------------------------
  // boot
  // ------------------------------------------------------------------

  const seenNotifications = new Set();

  async function pollNotifications() {
    try {
      const data = await api("/api/notifications");
      for (const n of data.notifications || []) {
        if (seenNotifications.has(n.id)) continue;
        seenNotifications.add(n.id);
        if (n.kind === "meeting_detected") continue;
        if (n.kind === "loop" && Date.now() / 1000 - Number(n.created_ts || 0) > 900) continue;
        toast(n.body || n.title, { title: n.kind === "loop" ? "Open loops" : n.title, ms: 7000 });
      }
    } catch (_) { /* offline — the status pill already shows it */ }
  }

  function boot() {
    wireHeader();
    wireShortcuts();
    setView(routeFromHash());
    refreshStatus();
    setInterval(refreshStatus, 10000);
    pollNotifications();
    setInterval(pollNotifications, 45000);
    if ("serviceWorker" in navigator && /^https?:$/.test(location.protocol)) {
      navigator.serviceWorker.register("/sw.js", { scope: "/" }).catch(() => { /* offline cache is best-effort */ });
    }
  }

  document.addEventListener("DOMContentLoaded", boot);
})();
