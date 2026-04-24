// ─────────────────────────────────────────────────────────────────
//  State
// ─────────────────────────────────────────────────────────────────
const SYSTEM_THREAD_ID = "system";
const DRAFT_THREAD_ID = "__draft__";
const state = {
  ws: null,
  threads: new Map(),          // thread_id → summary
  archivedThreads: new Map(),  // thread_id → summary
  selectedId: SYSTEM_THREAD_ID,
  threadDetail: new Map(),     // thread_id → full thread object
  logs: [],                    // all received log entries
  showArchived: false,
  authOk: false,
  activeTab: "logs",           // currently visible right-pane tab (only Logs remains)
  detailsCollapsed: true,      // right pane starts collapsed on desktop
  systemMessages: [],          // ephemeral Q&A on the System pseudo-thread
};

// ─────────────────────────────────────────────────────────────────
//  WebSocket
// ─────────────────────────────────────────────────────────────────
function connect() {
  state.ws = new WebSocket("ws://127.0.0.1:18080");
  state.ws.onopen = () => {
    state.ws.send(JSON.stringify({type: "list_threads"}));
    state.ws.send(JSON.stringify({type: "get_logs"}));
  };
  state.ws.onmessage = (e) => handleMessage(JSON.parse(e.data));
  state.ws.onclose = () => {
    setAuth(false, "Disconnected — retrying…");
    setTimeout(connect, 1500);
  };
}

function send(msg) {
  if (state.ws && state.ws.readyState === 1) {
    state.ws.send(JSON.stringify(msg));
  }
}

function handleMessage(m) {
  switch (m.type) {
    case "auth_status":     setAuth(m.ok, m.user); break;
    case "service_status":  renderServiceStatus(m.services || {}); break;
    case "signin_status":   alert(m.message); break;
    case "threads_list":    ingestThreadList(m.threads); break;
    case "thread_created":
      upsertThread(m.thread);
      // If the user is in draft mode waiting for this server round-trip,
      // auto-select the newly-created thread and seed the cached detail
      // with the user message we already rendered.
      if (state.selectedId === DRAFT_THREAD_ID && state.pendingDraftInput) {
        const input = state.pendingDraftInput;
        state.pendingDraftInput = null;
        state.threadDetail.set(m.thread.id, {
          id: m.thread.id,
          messages: [{role: "user", content: input}],
          progress_log: [],
        });
        selectThread(m.thread.id);
      }
      break;
    case "thread_updated":  upsertThread(m.thread); break;
    case "thread_started":  markThreadStatus(m.thread_id, "running"); break;
    case "thread_progress": onThreadProgress(m); break;
    case "thread_completed":onThreadCompleted(m); break;
    case "thread_error":    onThreadError(m); break;
    case "thread_cancelled":onThreadCancelled(m); break;
    case "thread_archived": removeThread(m.thread_id); break;
    case "thread_unarchived": upsertThread(m.thread); break;
    case "thread_deleted":  removeThread(m.thread_id); break;
    case "thread_detail":   loadThreadDetail(m.thread); break;
    case "archived_threads_list": ingestArchivedList(m.threads); break;
    case "log_entry":       appendLog(m.entry); break;
    case "log_history":     state.logs = m.entries || []; renderLogs(); break;
    case "system_query_started":  onSystemQueryStarted(m); break;
    case "system_query_progress": onSystemQueryProgress(m); break;
    case "system_query_complete": onSystemQueryComplete(m); break;
    case "system_query_error":    onSystemQueryError(m); break;
    case "remote_message":  appendSystemNotice("Remote msg from " + m.sender + ": " + m.text); break;
    case "skills_list":     _loadedSkills = Array.isArray(m.skills) ? m.skills : []; renderSkillsModal(); break;
    case "config_data":     onConfigData(m.config || {}); break;
    case "config_saved":    onConfigSaved(m); break;
    case "validate_speakers_started":  onSpeakersValidating(m); break;
    case "speakers_validated":         onSpeakersValidated(m); break;
  }
}

// ─────────────────────────────────────────────────────────────────
//  Auth
// ─────────────────────────────────────────────────────────────────
function setAuth(ok, label) {
  state.authOk = ok;
  document.getElementById("authDot").className = "dot " + (ok ? "ok" : "bad");
  // Label from server is typically "Name <email>". Show just the name in
  // the threads-pane footer; surface the full label (incl. email) on hover.
  const raw = label || (ok ? "Authenticated" : "Not signed in");
  const m = /^\s*(.*?)\s*<([^>]+)>\s*$/.exec(raw);
  const name = m ? m[1] : raw;
  const tip  = m ? `${m[1]} <${m[2]}>` : raw;
  const txt = document.getElementById("authText");
  txt.textContent = name;
  const footer = document.getElementById("threadsFooter");
  if (footer) footer.title = tip;
  document.getElementById("signinBtn").style.display = ok ? "none" : "";
}

function signin() {
  send({type: "signin"});
}

// ─────────────────────────────────────────────────────────────────
//  Service connectivity pills (MicrosoftIQ group + Redis/Teams)
// ─────────────────────────────────────────────────────────────────
const SVC_PILL_IDS = {
  workiq:       "svcWorkiq",
  foundryiq:    "svcFoundryiq",
  fabric_agent: "svcFabricAgent",
  redis_teams:  "svcRedisTeams",
};

function renderServiceStatus(services) {
  for (const [key, elId] of Object.entries(SVC_PILL_IDS)) {
    const el = document.getElementById(elId);
    if (!el) continue;
    const svc = services[key] || { status: "unknown", detail: "" };
    // Remove any old status class; keep "svc-pill".
    el.classList.remove("ok", "down", "unconfigured", "unknown");
    el.classList.add(svc.status || "unknown");
    const friendly = friendlyServiceLabel(key, svc);
    el.title = friendly;
  }
}

function friendlyServiceLabel(key, svc) {
  const names = {
    workiq: "WorkIQ",
    foundryiq: "FoundryIQ",
    fabric_agent: "FabricIQ (Fabric Data Agent)",
    redis_teams: "Redis \u2194 Teams relay",
  };
  const stateLabel = {
    ok: "connected",
    down: "unavailable",
    unconfigured: "not configured",
    unknown: "status unknown",
  }[svc.status] || svc.status;
  const detail = svc.detail ? ` \u2014 ${svc.detail}` : "";
  const when = svc.checked_at
    ? ` (checked ${new Date(svc.checked_at * 1000).toLocaleTimeString()})`
    : "";
  return `${names[key] || key}: ${stateLabel}${detail}${when}`;
}

// ─────────────────────────────────────────────────────────────────
//  Thread list rendering
// ─────────────────────────────────────────────────────────────────
function ingestThreadList(list) {
  state.threads.clear();
  for (const t of list) state.threads.set(t.id, t);
  renderThreadList();
}

function ingestArchivedList(list) {
  state.archivedThreads.clear();
  for (const t of list) state.archivedThreads.set(t.id, t);
  renderThreadList();
}

function upsertThread(t) {
  state.threads.set(t.id, t);
  if (state.showArchived && t.status !== "archived") {
    state.archivedThreads.delete(t.id);
  }
  renderThreadList();
  if (state.selectedId === t.id) renderSelectedHeader();
}

function removeThread(id) {
  state.threads.delete(id);
  state.archivedThreads.delete(id);
  if (state.selectedId === id) selectThread(SYSTEM_THREAD_ID);
  renderThreadList();
}

function markThreadStatus(id, status) {
  const t = state.threads.get(id);
  if (t) { t.status = status; upsertThread(t); }
}

function toggleArchivedFilter() {
  state.showArchived = !state.showArchived;
  const btn = document.getElementById("archToggle");
  if (btn) {
    const lbl = btn.querySelector(".label");
    if (lbl) lbl.textContent = state.showArchived ? "Hide archived" : "Show archived";
    btn.classList.toggle("active", state.showArchived);
  }
  if (state.showArchived) send({type: "list_archived_threads"});
  renderThreadList();
}

function renderThreadList() {
  const el = document.getElementById("threadList");
  el.innerHTML = "";

  // Always-pinned System thread on top
  el.appendChild(threadItemEl({
    id: SYSTEM_THREAD_ID,
    title: "System (cross-task queries)",
    status: "system",
    skill_name: null,
    correlation_tag: "#system",
  }, true));

  const showRunning = document.getElementById("filterRunning").checked;
  const showDone = document.getElementById("filterDone").checked;

  const list = Array.from(state.threads.values())
    .filter(t => {
      // "Running" covers anything in-flight (running / active / awaiting user input).
      if (["running", "active", "awaiting_user"].includes(t.status)) return showRunning;
      // "Completed" covers terminal states (completed / failed).
      if (["completed", "failed"].includes(t.status)) return showDone;
      return true;
    })
    .sort((a, b) => (b.created_at || 0) - (a.created_at || 0));

  for (const t of list) el.appendChild(threadItemEl(t, false));

  if (state.showArchived) {
    const arch = Array.from(state.archivedThreads.values())
      .sort((a, b) => (b.created_at || 0) - (a.created_at || 0));
    if (arch.length) {
      const h = document.createElement("div");
      h.style.cssText = "padding:6px 10px;color:var(--muted);font-size:11px;border-top:1px solid var(--border);";
      h.textContent = "Archived";
      el.appendChild(h);
      for (const t of arch) el.appendChild(threadItemEl(t, false));
    }
  }
}

function threadItemEl(t, isSystem) {
  const d = document.createElement("div");
  d.className = "thread-item" + (state.selectedId === t.id ? " active" : "") + (isSystem ? " system" : "");
  d.onclick = () => selectThread(t.id);

  const row1 = document.createElement("div");
  row1.className = "row1";
  const dot = document.createElement("span");
  dot.className = "status-dot " + (t.status || "");
  row1.appendChild(dot);
  const title = document.createElement("span");
  title.className = "title";
  title.textContent = t.title || "(untitled)";
  row1.appendChild(title);
  if (!isSystem && t.created_at) {
    const time = document.createElement("span");
    time.className = "time";
    time.textContent = formatRelativeTime(t.created_at);
    time.title = new Date(t.created_at * 1000).toLocaleString();
    row1.appendChild(time);
  }
  if (!isSystem) {
    const del = document.createElement("button");
    del.className = "thread-item-del";
    del.title = "Delete task";
    del.setAttribute("aria-label", "Delete task");
    del.innerHTML = '<svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="3 6 5 6 21 6"/><path d="M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6"/><path d="M10 11v6"/><path d="M14 11v6"/><path d="M9 6V4a1 1 0 0 1 1-1h4a1 1 0 0 1 1 1v2"/></svg>';
    del.onclick = (ev) => { ev.stopPropagation(); deleteThread(t.id); };
    row1.appendChild(del);
  }
  d.appendChild(row1);

  const meta = document.createElement("div");
  meta.className = "meta";
  const parts = [];
  if (t.correlation_tag || t.id) parts.push(t.correlation_tag || ("#thread-" + t.id));
  if (t.skill_name) parts.push(t.skill_name);
  if (t.status && !isSystem) parts.push(t.status);
  meta.textContent = parts.join(" · ");
  d.appendChild(meta);
  return d;
}

// Compact relative time for thread list rows. Input is unix seconds.
function formatRelativeTime(unixSec) {
  if (!unixSec) return "";
  const now = Date.now() / 1000;
  const diff = Math.max(0, now - unixSec);
  if (diff < 60)        return "just now";
  if (diff < 3600)      return Math.floor(diff / 60) + "m";
  if (diff < 86400)     return Math.floor(diff / 3600) + "h";
  if (diff < 7 * 86400) return Math.floor(diff / 86400) + "d";
  // Older: show short date.
  const d = new Date(unixSec * 1000);
  return d.toLocaleDateString(undefined, {month: "short", day: "numeric"});
}

// ─────────────────────────────────────────────────────────────────
//  Selection + chat rendering
// ─────────────────────────────────────────────────────────────────
function selectThread(id) {
  state.selectedId = id;
  renderThreadList();
  renderSelectedHeader();
  renderChatBody();
  // Defer right-pane rendering: clear stale content and render ONLY the
  // currently active tab. Other tabs render lazily on click so they never
  // delay the main chat body.
  clearDetailPanels();
  renderActiveTab();
  if (id !== SYSTEM_THREAD_ID) {
    send({type: "get_thread", thread_id: id});
  }
  // On narrow viewports the threads pane is an overlay — close it after
  // selection so the user lands on the chat without an extra tap.
  if (typeof _isXNarrow === "function" && _isXNarrow()) {
    document.querySelector("aside.threads")?.classList.remove("open");
    if (typeof _syncBackdrop === "function") _syncBackdrop();
  }
}

function clearDetailPanels() {
  const logs = document.getElementById("panel-logs");
  if (logs) logs.innerHTML = "";
}

function renderActiveTab() {
  // Only the Logs tab survives — everything else is rendered inline in the
  // main chat (step cards) or via thread-list / chat-header controls.
  renderLogs();
}

function renderSelectedHeader() {
  const tag = document.getElementById("chatTag");
  const title = document.getElementById("chatTitle");
  const status = document.getElementById("chatStatus");
  const crumb = document.getElementById("breadcrumb");
  const archBtn = document.getElementById("chatHeaderArchiveBtn");
  const unarchBtn = document.getElementById("chatHeaderUnarchiveBtn");
  const clearBtn = document.getElementById("chatHeaderClearBtn");
  const setCrumb = (txt) => { if (crumb) crumb.textContent = txt || ""; };
  const showHdrBtn = (el, on) => { if (el) el.style.display = on ? "" : "none"; };
  if (state.selectedId === SYSTEM_THREAD_ID) {
    tag.textContent = "#system";
    title.textContent = "System · cross-task queries";
    status.textContent = "ready";
    status.className = "status-label";
    setCrumb("System");
    showHdrBtn(archBtn, false);
    showHdrBtn(unarchBtn, false);
    showHdrBtn(clearBtn, state.systemMessages.length > 0);
    updateComposerLockState();
    return;
  }
  if (state.selectedId === DRAFT_THREAD_ID) {
    tag.textContent = "#new";
    title.textContent = "New task";
    status.textContent = "draft";
    status.className = "status-label";
    setCrumb("New task");
    showHdrBtn(archBtn, false);
    showHdrBtn(unarchBtn, false);
    showHdrBtn(clearBtn, false);
    updateComposerLockState();
    return;
  }
  const t = state.threads.get(state.selectedId) || state.archivedThreads.get(state.selectedId);
  if (!t) {
    tag.textContent = ""; title.textContent = "(not found)"; setCrumb("");
    showHdrBtn(archBtn, false); showHdrBtn(unarchBtn, false); showHdrBtn(clearBtn, false);
    updateComposerLockState();
    return;
  }
  tag.textContent = t.correlation_tag || ("#thread-" + t.id);
  title.textContent = t.title || "(untitled)";
  status.textContent = t.status || "unknown";
  status.className = "status-label " + (t.status || "");
  setCrumb(t.title || "(untitled)");
  const isArchived = t.status === "archived" || state.archivedThreads.has(t.id);
  showHdrBtn(archBtn, !isArchived);
  showHdrBtn(unarchBtn, isArchived);
  showHdrBtn(clearBtn, false);
  updateComposerLockState();
}

// Archive / unarchive the currently selected thread (chat-header buttons).
function archiveSelectedThread() {
  const id = state.selectedId;
  if (!id || id === SYSTEM_THREAD_ID || id === DRAFT_THREAD_ID) return;
  archiveThread(id);
}
function unarchiveSelectedThread() {
  const id = state.selectedId;
  if (!id || id === SYSTEM_THREAD_ID || id === DRAFT_THREAD_ID) return;
  unarchiveThread(id);
}

// Show a small informational notice when the selected thread is in a
// terminal state (completed / error / rejected). The composer stays
// fully usable — sending a message simply continues the same thread,
// which is useful for follow-up tweaks (e.g. "regenerate that doc").
function updateComposerLockState() {
  const wrap = document.getElementById("chatInput");
  const notice = document.getElementById("composerNotice");
  if (!wrap || !notice) return;

  let show = false;
  let label = "";
  let isRunning = false;
  if (state.selectedId !== SYSTEM_THREAD_ID && state.selectedId !== DRAFT_THREAD_ID) {
    const t = state.threads.get(state.selectedId) || state.archivedThreads.get(state.selectedId);
    const s = t && t.status;
    if (s === "completed" || s === "error" || s === "rejected") {
      show = true;
      const pretty = s === "completed" ? "completed"
                   : s === "rejected" ? "rejected"
                   : "ended with an error";
      label = "This thread is " + pretty
            + ". You can keep typing here to continue or "
            + '<a onclick="newThread()">start a new task</a>.';
    }
    if (s === "running") isRunning = true;
  }

  wrap.classList.toggle("show-notice", show);
  notice.innerHTML = show ? label : "";

  // Swap Send <-> Stop based on whether the agent is actively working.
  const sendBtn = document.getElementById("sendBtn");
  const stopBtn = document.getElementById("stopBtn");
  if (sendBtn && stopBtn) {
    sendBtn.style.display = isRunning ? "none" : "";
    stopBtn.style.display = isRunning ? "" : "none";
  }
}

function cancelCurrentThread() {
  const tid = state.selectedId;
  if (!tid || tid === SYSTEM_THREAD_ID || tid === DRAFT_THREAD_ID) return;
  try {
    state.ws.send(JSON.stringify({ type: "cancel_thread", thread_id: tid }));
  } catch (e) {
    console.warn("cancel send failed", e);
  }
}

function renderChatBody() {
  const body = document.getElementById("chatBody");
  body.innerHTML = "";
  if (state.selectedId === SYSTEM_THREAD_ID) {
    if (!state.systemMessages.length) {
      body.innerHTML = '<div class="empty">System thread — ask cross-cutting status questions like "what\'s running?" or "which tasks are waiting?".</div>';
    } else {
      for (const msg of state.systemMessages) appendMsg(msg.role, msg.content);
      scrollChatToEnd();
    }
    return;
  }
  if (state.selectedId === DRAFT_THREAD_ID) {
    body.innerHTML = '<div class="empty">New task — describe what you\'d like me to do below and press Send.</div>';
    return;
  }
  const d = state.threadDetail.get(state.selectedId);
  if (!d) { body.innerHTML = '<div class="empty">Loading…</div>'; return; }
  // Interleave user/assistant messages with persisted progress cards so
  // the historical view matches what was shown live. Messages don't carry
  // their own timestamps in the cached payload, so we render them in
  // order, with all card-worthy progress entries that arrived BEFORE the
  // final assistant message inserted in chronological order before that
  // message. In practice: user → step cards → assistant.
  const msgs = d.messages || [];
  const cards = (d.progress_log || []).filter(p => {
    const k = Array.isArray(p) ? p[1] : p.kind;
    return PROGRESS_CARD_KINDS.has(k);
  });
  // Find the last user message — cards belong to the turn after it.
  let lastUserIdx = -1;
  for (let i = msgs.length - 1; i >= 0; i--) {
    if (msgs[i].role === "user") { lastUserIdx = i; break; }
  }
  for (let i = 0; i < msgs.length; i++) {
    appendMsg(msgs[i].role, msgs[i].content);
    if (i === lastUserIdx) {
      for (const p of cards) {
        const kind = Array.isArray(p) ? p[1] : p.kind;
        const message = Array.isArray(p) ? p[2] : p.message;
        body.appendChild(buildProgressCard(kind, message));
      }
    }
  }
  // If no user message yet (rare), still render any cards we have.
  if (lastUserIdx === -1 && cards.length) {
    for (const p of cards) {
      const kind = Array.isArray(p) ? p[1] : p.kind;
      const message = Array.isArray(p) ? p[2] : p.message;
      body.appendChild(buildProgressCard(kind, message));
    }
  }
  // If this thread is still working, re-attach the shimmer so the user
  // sees ongoing activity after switching tabs or reopening the app.
  // Only "running" means the executor is actively working — "active" is the
  // default idle status for any non-archived thread.
  const t = state.threads.get(state.selectedId);
  if (t && t.status === "running") {
    const log = d.progress_log || [];
    // Walk backwards looking for the most recent SHORT transient status
    // (step/tool/agent). Fall back to the short label of the latest card.
    let shimmerText = "Working…";
    for (let i = log.length - 1; i >= 0; i--) {
      const e = log[i];
      const kind = Array.isArray(e) ? e[1] : e.kind;
      const msg = Array.isArray(e) ? e[2] : e.message;
      if (!PROGRESS_CARD_KINDS.has(kind)) {
        shimmerText = msg || shimmerText;
        break;
      }
      if (PROGRESS_CARD_KINDS.has(kind)) {
        shimmerText = shortStatusLabel(msg) || shimmerText;
        break;
      }
    }
    setLiveStatus(shimmerText);
  }
  scrollChatToEnd();
}

function appendMsg(role, text) {
  const body = document.getElementById("chatBody");
  if (body.querySelector(".empty")) body.innerHTML = "";
  const d = document.createElement("div");
  d.className = "msg " + role;
  const r = document.createElement("div");
  r.className = "role";
  r.textContent = role;
  d.appendChild(r);
  const t = document.createElement("div");
  if (role === "assistant") {
    t.className = "md";
    t.innerHTML = renderMarkdown(text || "");
  } else {
    t.textContent = text;
  }
  d.appendChild(t);

  // Wrap assistant messages in a row with a monogram avatar so the
  // conversation has a visual anchor on the left. User messages remain
  // bare and right-aligned — the colour + alignment already identify them.
  if (role === "assistant") {
    const row = document.createElement("div");
    row.className = "msg-row assistant";
    const av = document.createElement("div");
    av.className = "avatar avatar-agent";
    av.textContent = "H";
    av.title = "Hub Cowork";
    row.appendChild(av);
    row.appendChild(d);
    body.appendChild(row);
  } else {
    body.appendChild(d);
  }
  scrollChatToEnd();
}

function appendSystemNotice(text) {
  if (state.selectedId !== SYSTEM_THREAD_ID) return;
  const body = document.getElementById("chatBody");
  if (body.querySelector(".empty")) body.innerHTML = "";
  const d = document.createElement("div");
  d.className = "msg progress";
  d.textContent = text;
  body.appendChild(d);
  scrollChatToEnd();
}

// Build a persistent step card for a progress / milestone event and append
// it to the chat body. The shimmer (live-status) sits *after* this card so
// new cards always insert above it.
function appendProgressCard(kind, message) {
  const body = document.getElementById("chatBody");
  if (body.querySelector(".empty")) body.innerHTML = "";
  const card = buildProgressCard(kind, message);
  const live = body.querySelector(".live-status");
  if (live) body.insertBefore(card, live);
  else body.appendChild(card);
  scrollChatToEnd();
}

function buildProgressCard(kind, message) {
  const card = document.createElement("div");
  card.className = "step-card kind-" + (kind || "info");

  const head = document.createElement("div");
  head.className = "step-card-head";
  const dot = document.createElement("span");
  dot.className = "step-card-icon";
  dot.innerHTML = timelineIcon(kind);
  head.appendChild(dot);

  // Try to peel off a leading "**Title**" so the title sits in the header
  // bar and the body just shows the supporting markdown.
  const raw = String(message ?? "");
  const m = raw.match(/^\s*\*\*([^*\n]+)\*\*\s*\n+([\s\S]*)$/);
  let title, rest;
  if (m) { title = m[1].trim(); rest = m[2]; }
  else if (kind === "milestone") { title = raw.trim(); rest = ""; }
  else { title = ""; rest = raw; }

  if (title) {
    const t = document.createElement("span");
    t.className = "step-card-title";
    t.textContent = title;
    head.appendChild(t);
  }
  card.appendChild(head);

  if (rest && rest.trim()) {
    const bodyEl = document.createElement("div");
    bodyEl.className = "step-card-body md";
    bodyEl.innerHTML = renderMarkdown(rest);
    card.appendChild(bodyEl);
  }
  return card;
}

function scrollChatToEnd() {
  const body = document.getElementById("chatBody");
  body.scrollTop = body.scrollHeight;
}

// ─────────────────────────────────────────────────────────────────
//  Live progress
// ─────────────────────────────────────────────────────────────────
// A single shimmering status line replaces the old stream of progress
// bubbles. The full history is still available in the Progress tab.
//
// Progress messages from skills/tools often contain raw markdown (bold
// `**…**`, table pipes `|`, headings `#`, code spans, list markers). On a
// single fleeting line that looks ungainly, so we strip the syntax and
// collapse whitespace before display. The full original text is preserved
// in the `title` attribute (hover tooltip) and in the Progress tab.
function flattenMarkdown(s) {
  if (!s) return "";
  let t = String(s);
  // Code fences and inline code → plain text.
  t = t.replace(/```[\s\S]*?```/g, m => m.replace(/```/g, "").trim());
  t = t.replace(/`([^`]+)`/g, "$1");
  // Bold / italic / strike markers.
  t = t.replace(/\*\*([^*]+)\*\*/g, "$1");
  t = t.replace(/\*([^*]+)\*/g, "$1");
  t = t.replace(/__([^_]+)__/g, "$1");
  t = t.replace(/_([^_]+)_/g, "$1");
  t = t.replace(/~~([^~]+)~~/g, "$1");
  // Markdown links [text](url) → text.
  t = t.replace(/\[([^\]]+)\]\([^)]+\)/g, "$1");
  // Headings, blockquotes, list markers at line starts.
  t = t.replace(/^\s{0,3}#{1,6}\s+/gm, "");
  t = t.replace(/^\s*>\s?/gm, "");
  t = t.replace(/^\s*[-*+]\s+/gm, "• ");
  t = t.replace(/^\s*\d+\.\s+/gm, "");
  // Table pipes → " · ", drop separator rows like |---|---|.
  t = t.replace(/^\s*\|?\s*[:\-\s|]+\|[\s\-:|]*$/gm, "");
  t = t.replace(/\s*\|\s*/g, " · ");
  // Collapse whitespace / newlines into single spaces.
  t = t.replace(/\s+/g, " ").trim();
  return t;
}

function setLiveStatus(text) {
  const body = document.getElementById("chatBody");
  if (body.querySelector(".empty")) body.innerHTML = "";
  let el = body.querySelector(".live-status");
  if (!el) {
    el = document.createElement("div");
    el.className = "live-status";
    const sp = document.createElement("div");
    sp.className = "spinner";
    const tx = document.createElement("div");
    tx.className = "text";
    el.appendChild(sp);
    el.appendChild(tx);
    body.appendChild(el);
  }
  // Keep the live status as the last child so it sits just after any
  // previously-rendered user/assistant bubbles.
  if (el !== body.lastElementChild) body.appendChild(el);
  const raw = text || "Working…";
  el.querySelector(".text").textContent = flattenMarkdown(raw) || "Working…";
  // Full untruncated original on hover.
  el.title = raw;
  scrollChatToEnd();
}

function clearLiveStatus() {
  const body = document.getElementById("chatBody");
  const el = body.querySelector(".live-status");
  if (el) el.remove();
}

// Kinds that carry rich content worth pinning in the main chat as a
// persistent step card. Other kinds (step/tool/agent) are short transient
// status used only to drive the shimmer.
const PROGRESS_CARD_KINDS = new Set(["progress", "milestone"]);

// Best-effort short label for the shimmer when a "progress" event arrives:
// extract the bold "**Step Title**" the log_progress tool prefixes, or
// fall back to the first line.
function shortStatusLabel(text) {
  if (!text) return "";
  const s = String(text);
  const m = s.match(/^\s*\*\*([^*\n]+)\*\*/);
  if (m) return m[1].trim();
  const firstLine = s.split(/\r?\n/, 1)[0];
  return flattenMarkdown(firstLine);
}

function onThreadProgress(m) {
  if (state.selectedId === m.thread_id) {
    if (PROGRESS_CARD_KINDS.has(m.kind)) {
      // Pin the rich content as a step card in the main chat. Update the
      // shimmer to a short label so the user knows the next step is starting.
      appendProgressCard(m.kind, m.message);
      setLiveStatus(shortStatusLabel(m.message) || "Working…");
    } else {
      setLiveStatus(m.message);
    }
  }
  // Append to the progress cache. If the full thread detail hasn't arrived
  // from the server yet (get_thread round-trip still in flight), create a
  // stub entry so events aren't lost — loadThreadDetail() will merge the
  // server payload in when it arrives, preserving any progress we buffered.
  let d = state.threadDetail.get(m.thread_id);
  if (!d) {
    d = { id: m.thread_id, progress_log: [] };
    state.threadDetail.set(m.thread_id, d);
  }
  d.progress_log = d.progress_log || [];
  // Match the shape the server uses when it persists progress entries so
  // that renderDetails reads them the same way whether they came from the
  // live stream or a get_thread round-trip.
  d.progress_log.push({
    ts: Date.now() / 1000,
    kind: m.kind,
    message: m.message,
    request_id: m.request_id,
  });
}

function onThreadCompleted(m) {
  markThreadStatus(m.thread_id, "completed");
  // Always append the assistant reply to the cached detail so it survives
  // tab switches. If the user wasn't looking at this thread when it
  // finished, they'll still see their user message + the reply when they
  // come back (selectThread also re-fetches to get authoritative state).
  const d = state.threadDetail.get(m.thread_id);
  if (d) {
    d.messages = d.messages || [];
    d.messages.push({role: "assistant", content: m.result || ""});
  }
  if (state.selectedId === m.thread_id) {
    clearLiveStatus();
    appendMsg("assistant", m.result || "");
    // Refresh detail to persist message.
    send({type: "get_thread", thread_id: m.thread_id});
  }
}

function onThreadError(m) {
  markThreadStatus(m.thread_id, "failed");
  const errText = "⚠️  Error: " + (m.error || "unknown");
  const d = state.threadDetail.get(m.thread_id);
  if (d) {
    d.messages = d.messages || [];
    d.messages.push({role: "assistant", content: errText});
  }
  if (state.selectedId === m.thread_id) {
    clearLiveStatus();
    appendMsg("assistant", errText);
  }
}

function onThreadCancelled(m) {
  markThreadStatus(m.thread_id, "cancelled");
  const note = "⏹  Stopped by user.";
  const d = state.threadDetail.get(m.thread_id);
  if (d) {
    d.messages = d.messages || [];
    d.messages.push({role: "assistant", content: note});
  }
  if (state.selectedId === m.thread_id) {
    clearLiveStatus();
    appendMsg("assistant", note);
  }
}

function loadThreadDetail(thread) {
  // Preserve any progress events we buffered before the full detail arrived.
  const existing = state.threadDetail.get(thread.id);
  if (existing && existing.progress_log && existing.progress_log.length) {
    const serverLog = thread.progress_log || [];
    // Server is authoritative when it has entries; otherwise keep our buffer.
    if (!serverLog.length) thread.progress_log = existing.progress_log;
  }
  // Merge any persisted per-thread code_log entries into state.logs so the
  // Logs tab shows them for completed threads loaded from disk (the live
  // ring buffer only has entries since the server started).
  if (Array.isArray(thread.code_log) && thread.code_log.length) {
    const seen = new Set(
      state.logs
        .filter(e => e.thread_id === thread.id)
        .map(e => `${e.ts}|${e.msg}`)
    );
    for (const e of thread.code_log) {
      const key = `${e.ts}|${e.msg}`;
      if (!seen.has(key)) {
        state.logs.push({...e, thread_id: thread.id});
        seen.add(key);
      }
    }
    state.logs.sort((a, b) => (a.ts || 0) - (b.ts || 0));
    if (state.logs.length > 2000) state.logs = state.logs.slice(-2000);
  }
  state.threadDetail.set(thread.id, thread);
  if (state.selectedId === thread.id) {
    renderChatBody();
    if (state.activeTab === "logs") renderLogs();
  }
}

// ─────────────────────────────────────────────────────────────────
//  System query (no thread creation)
// ─────────────────────────────────────────────────────────────────
function onSystemQueryStarted(m) {
  if (state.selectedId === SYSTEM_THREAD_ID) {
    setLiveStatus("Working…");
  }
}
function onSystemQueryProgress(m) {
  if (state.selectedId === SYSTEM_THREAD_ID) setLiveStatus(m.message);
}
function onSystemQueryComplete(m) {
  state.systemMessages.push({role: "assistant", content: m.result || ""});
  if (state.selectedId === SYSTEM_THREAD_ID) {
    clearLiveStatus();
    appendMsg("assistant", m.result || "");
  }
}
function onSystemQueryError(m) {
  state.systemMessages.push({role: "assistant", content: "⚠️  " + m.error});
  if (state.selectedId === SYSTEM_THREAD_ID) {
    clearLiveStatus();
    appendMsg("assistant", "⚠️  " + m.error);
  }
}

function clearSystemConversation() {
  state.systemMessages = [];
  if (state.selectedId === SYSTEM_THREAD_ID) {
    renderChatBody();
  }
}

// ─────────────────────────────────────────────────────────────────
//  Input
// ─────────────────────────────────────────────────────────────────
function onInputKey(e) {
  if (e.key === "Enter" && !e.shiftKey) {
    e.preventDefault();
    sendInput();
  }
  // Reflect "has text" on the wrapper so the send button lights up.
  // Deferred to allow the keypress to update the textarea value first.
  setTimeout(updateComposerHasInput, 0);
}

function updateComposerHasInput() {
  const wrap = document.getElementById("chatInput");
  const box = document.getElementById("inputBox");
  if (!wrap || !box) return;
  wrap.classList.toggle("has-input", !!box.value.trim());
  autoResizeInput(box);
}

// Grow the composer textarea to fit its content, capped by the CSS max-height
// (overflow scrolls past the cap). Reset to "auto" first so it can shrink
// again when the user deletes lines. The CSS max-height keeps the chat body
// from being squeezed; the responsive grid is unaffected because the composer
// is laid out by flex inside its own row.
function autoResizeInput(box) {
  if (!box) box = document.getElementById("inputBox");
  if (!box) return;
  box.style.height = "auto";
  // scrollHeight includes padding; cap is enforced by CSS max-height.
  box.style.height = box.scrollHeight + "px";
}

function sendInput() {
  const box = document.getElementById("inputBox");
  const text = box.value.trim();
  if (!text) return;
  box.value = "";
  updateComposerHasInput();
  if (state.selectedId === SYSTEM_THREAD_ID) {
    appendMsg("user", text);
    state.systemMessages.push({role: "user", content: text});
    setLiveStatus("Working…");
    send({type: "system_query", input: text});
    return;
  }
  if (state.selectedId === DRAFT_THREAD_ID) {
    // Draft mode: ask the server to create a real thread. We'll auto-select
    // it when thread_created arrives (see case in handleMessage).
    state.pendingDraftInput = text;
    appendMsg("user", text);
    setLiveStatus("Creating task…");
    send({type: "create_thread", input: text});
    return;
  }
  const t = state.threads.get(state.selectedId);
  if (!t || t.status === "archived") {
    // Archived — send_to_thread will unarchive server-side.
  }
  appendMsg("user", text);
  // Also write to the cached detail so the message survives tab switches
  // without waiting for the server's get_thread round-trip.
  const d = state.threadDetail.get(state.selectedId);
  if (d) {
    d.messages = d.messages || [];
    d.messages.push({role: "user", content: text});
  } else {
    state.threadDetail.set(state.selectedId, {
      id: state.selectedId,
      messages: [{role: "user", content: text}],
      progress_log: [],
    });
  }
  setLiveStatus("Working…");
  send({type: "send_to_thread", thread_id: state.selectedId, input: text});
}

function newThread() {
  // Switch to an in-UI draft. Actual thread creation happens when the
  // user sends their first message from the chat input.
  state.selectedId = DRAFT_THREAD_ID;
  renderThreadList();
  renderSelectedHeader();
  renderChatBody();
  clearDetailPanels();
  renderActiveTab();
  const box = document.getElementById("inputBox");
  if (box) { box.focus(); }
}

// ─────────────────────────────────────────────────────────────────
//  Responsive layout: off-canvas side panes
//  Mirrors the CSS breakpoints in chat_ui.css. The toggle buttons
//  in the topbar are hidden by CSS on wide windows, so these helpers
//  are only reachable on narrow viewports.
// ─────────────────────────────────────────────────────────────────
function _isNarrow() { return window.matchMedia("(max-width: 900px)").matches; }
function _isXNarrow() { return window.matchMedia("(max-width: 700px)").matches; }

function _syncBackdrop() {
  const bd = document.getElementById("paneBackdrop");
  if (!bd) return;
  const anyOpen =
    document.querySelector("aside.threads.open") ||
    document.querySelector("section.details.open");
  bd.classList.toggle("open", !!anyOpen);
}

function toggleThreadsPane() {
  // On narrow viewports the threads pane is an off-canvas overlay — keep
  // the .open toggle. On desktop, slide it out by toggling a class on
  // .app that collapses the threads column to 0.
  if (_isXNarrow()) {
    const el = document.querySelector("aside.threads");
    if (!el) return;
    document.querySelector("section.details")?.classList.remove("open");
    el.classList.toggle("open");
    _syncBackdrop();
    return;
  }
  const app = document.querySelector(".app");
  if (!app) return;
  app.classList.toggle("threads-collapsed");
}

function toggleDetailsPane() {
  const el = document.querySelector("section.details");
  if (!el) return;
  document.querySelector("aside.threads")?.classList.remove("open");
  el.classList.toggle("open");
  _syncBackdrop();
}

function closeMobilePanes() {
  document.querySelector("aside.threads")?.classList.remove("open");
  document.querySelector("section.details")?.classList.remove("open");
  _syncBackdrop();
}

// Auto-close panes on viewport resize back to wide so they don't get
// stuck off-screen when the layout switches back to inline columns.
window.addEventListener("resize", () => {
  if (!_isXNarrow()) document.querySelector("aside.threads")?.classList.remove("open");
  if (!_isNarrow())  document.querySelector("section.details")?.classList.remove("open");
  _syncBackdrop();
});

// ─────────────────────────────────────────────────────────────────
//  Right pane: details / progress / logs
// ─────────────────────────────────────────────────────────────────
// On desktop the right pane is COLLAPSED by default — main chat gets the
// full width. Clicking a tab button expands the pane and shows that tab.
// Clicking the active tab again collapses it.
function setDetailsCollapsed(collapsed) {
  document.querySelector(".app")?.classList.toggle("details-collapsed", collapsed);
  state.detailsCollapsed = collapsed;
}

function switchTab(name) {
  // If clicking the already-active tab, collapse the pane.
  if (state.activeTab === name && !state.detailsCollapsed) {
    setDetailsCollapsed(true);
    return;
  }
  state.activeTab = name;
  setDetailsCollapsed(false);
  for (const b of document.querySelectorAll(".details .tabs button")) {
    b.classList.toggle("active", b.dataset.tab === name);
  }
  for (const p of document.querySelectorAll(".details .panel")) {
    p.classList.toggle("hidden", p.id !== "panel-" + name);
  }
  // Lazily render whichever tab the user just opened — guarantees Logs is
  // fresh when revisiting a thread after viewing another.
  renderActiveTab();
}

// Open the Logs tab from the chat header. If logs are already showing,
// collapse the pane instead.
function openLogsTab() {
  if (!state.detailsCollapsed && state.activeTab === "logs") {
    setDetailsCollapsed(true);
    return;
  }
  switchTab("logs");
}

function renderDetails() {
  // The Info and Progress tabs were removed once their content was
  // surfaced inline (step cards in the chat, status/skill in the chat
  // header and thread list, archive/delete via chat-header + thread-item
  // hover). renderDetails is kept as a no-op so legacy call sites — and
  // any external code paths — don't blow up.
}

// Inline SVG icons for each progress kind. Kept tiny so the timeline dots
// stay 18px. Stroke uses currentColor so kind-* CSS rules can recolor.
function timelineIcon(kind) {
  const ic = {
    progress:  '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="9"/><polyline points="12 7 12 12 15 14"/></svg>',
    step:      '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><polyline points="9 18 15 12 9 6"/></svg>',
    tool:      '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M14.7 6.3a4 4 0 0 0-5.4 5.4L3 18l3 3 6.3-6.3a4 4 0 0 0 5.4-5.4l-2.5 2.5-2.5-2.5z"/></svg>',
    agent:     '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><rect x="4" y="6" width="16" height="12" rx="3"/><circle cx="9" cy="12" r="1.2" fill="currentColor"/><circle cx="15" cy="12" r="1.2" fill="currentColor"/></svg>',
    milestone: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><polyline points="20 6 9 17 4 12"/></svg>',
    error:     '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="9"/><line x1="15" y1="9" x2="9" y2="15"/><line x1="9" y1="9" x2="15" y2="15"/></svg>',
  };
  return ic[kind] || '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="3" fill="currentColor"/></svg>';
}

function renderLogs() {
  const el = document.getElementById("panel-logs");
  el.innerHTML = "";
  const filter = state.selectedId;
  const entries = state.logs.filter(e => {
    if (filter === SYSTEM_THREAD_ID) {
      return !e.thread_id || e.thread_id === SYSTEM_THREAD_ID;
    }
    return e.thread_id === filter;
  });
  if (!entries.length) {
    el.innerHTML = '<div class="empty" style="color:#8b949e">No logs for this task yet.</div>';
    return;
  }
  for (const e of entries.slice(-500)) {
    el.appendChild(logEntryEl(e));
  }
  el.scrollTop = el.scrollHeight;
}

function logEntryEl(e) {
  const d = document.createElement("div");
  d.className = "log-entry";
  const time = typeof e.ts === "number"
    ? new Date(e.ts * 1000).toLocaleTimeString()
    : (e.ts || "");
  d.innerHTML = `<span class="ts">${time}</span><span class="lvl ${e.level}">${e.level}</span>${escapeHtml(e.msg)}`;
  return d;
}

function appendLog(entry) {
  state.logs.push(entry);
  if (state.logs.length > 2000) state.logs.shift();
  // Only mutate the DOM if the Logs tab is actually open AND the entry
  // belongs to the currently selected thread. Otherwise the buffer grows
  // silently and renders on next tab open via renderLogs().
  if (state.activeTab !== "logs") return;
  const filter = state.selectedId;
  const keep = (filter === SYSTEM_THREAD_ID)
    ? (!entry.thread_id || entry.thread_id === SYSTEM_THREAD_ID)
    : entry.thread_id === filter;
  if (keep) {
    const el = document.getElementById("panel-logs");
    if (el.querySelector(".empty")) el.innerHTML = "";
    el.appendChild(logEntryEl(entry));
    el.scrollTop = el.scrollHeight;
  }
}

function archiveThread(id) { send({type: "archive_thread", thread_id: id}); }
function unarchiveThread(id) { send({type: "unarchive_thread", thread_id: id}); }
function deleteThread(id) {
  if (!confirm("Delete this task permanently?")) return;
  send({type: "delete_thread", thread_id: id});
}

function openConfig() {
  setConfigMsg("Loading…", "");
  document.getElementById("settingsModal").classList.add("open");
  // Disable save until data arrives.
  document.getElementById("cfgSaveBtn").disabled = true;
  send({type: "get_config"});
}

function closeSettings() {
  document.getElementById("settingsModal").classList.remove("open");
}

function onSettingsBackdrop(ev) {
  // Click on dimmed area (not the modal content) closes.
  if (ev.target === ev.currentTarget) closeSettings();
}

// Holds the last server-loaded config so we can preserve unknown keys on save.
let _loadedConfig = {};
// Snapshot of currently-active environment values from the server. Used
// to pre-fill the Settings env editor and to compute deltas on save (so
// we only persist values that actually differ from .env / defaults).
let _currentEnv = {};

function onConfigData(cfg) {
  _loadedConfig = cfg || {};
  _currentEnv = (cfg && cfg._env_current) || {};
  document.getElementById("cfgHubName").value = cfg.hub_name || "";
  document.getElementById("cfgStartTime").value = cfg.default_session_start_time || "";
  document.getElementById("cfgOutputFolder").value = cfg.agenda_output_folder || "";
  document.getElementById("cfgTemplatePath").value = cfg.agenda_template_path || "";
  const catalog = Array.isArray(cfg.topic_catalog) ? cfg.topic_catalog : [];
  document.getElementById("cfgTopicCatalog").value =
    JSON.stringify(catalog, null, 2);
  document.getElementById("cfgSaveBtn").disabled = false;
  setConfigMsg("", "");
  validateTopicCatalog();
  // Pool starts empty on load — populated only by user adds.
  _speakerPool = [];
  _speakerEditing = null;
  _topicEditingIdx = -1;
  _addTopicOpen = false;
  // Hydrate speaker validation cache from persisted config so badges
  // survive a restart.
  _speakerState = {};
  const persisted = (cfg && cfg._speaker_validations) || {};
  if (persisted && typeof persisted === "object") {
    for (const [key, entry] of Object.entries(persisted)) {
      if (!entry || typeof entry !== "object") continue;
      _speakerState[key] = {
        input: entry.input || key,
        role: entry.role || "",
        status: entry.status || "pending",
        matches: Array.isArray(entry.matches) ? entry.matches : [],
        picked: Number.isInteger(entry.picked) ? entry.picked : 0,
        validated_at: entry.validated_at || "",
      };
    }
  }
  rebuildSpeakerTable();
  rebuildTopicCards();
  renderEnvEditor();
}

// ─────────────────────────────────────────────────────────────────
//  Environment / secrets editor
// ─────────────────────────────────────────────────────────────────
// Schema for editable env vars. Grouped, with type hints. Anything matching
// the secret patterns below is rendered as a password field with a Show toggle.
const ENV_GROUPS = [
  {label: "Azure OpenAI", keys: [
    {key: "AZURE_OPENAI_ENDPOINT",        hint: "https://<resource>.openai.azure.com/openai/v1"},
    {key: "AZURE_OPENAI_CHAT_MODEL",      hint: "Deployment name, e.g. gpt-5.2"},
    {key: "AZURE_OPENAI_CHAT_MODEL_SMALL",hint: "Smaller/faster deployment, e.g. gpt-5.4-mini"},
    {key: "AZURE_OPENAI_API_VERSION",     hint: "e.g. 2025-03-01-preview"},
  ]},
  {label: "Azure account", keys: [
    {key: "AZURE_TENANT_ID",       hint: "Your Microsoft Entra tenant ID"},
    {key: "AZURE_SUBSCRIPTION_ID", hint: "Subscription selected after az login"},
    {key: "RESOURCE_TENANT_ID",    hint: "Cross-tenant: tenant where Foundry/Fabric live"},
  ]},
  {label: "Email (Azure Communication Services)", keys: [
    {key: "ACS_ENDPOINT",        hint: "https://<resource>.communication.azure.com"},
    {key: "ACS_SENDER_ADDRESS",  hint: "DoNotReply@<your-domain>"},
  ]},
  {label: "WorkIQ", keys: [
    {key: "WORKIQ_PATH", hint: "Full path to workiq CLI (only if not on PATH)"},
  ]},
  {label: "RFP Evaluation — FoundryIQ", keys: [
    {key: "FOUNDRYIQ_ENDPOINT",    hint: "Azure AI Search endpoint"},
    {key: "FOUNDRYIQ_KB_NAME",     hint: "Index name, e.g. rfp-knowledge-store"},
    {key: "FOUNDRYIQ_AUTH_MODE",   hint: "browser | cli"},
    {key: "FOUNDRYIQ_API_VERSION", hint: "e.g. 2025-11-01-preview"},
  ]},
  {label: "RFP Evaluation — Fabric Data Agent", keys: [
    {key: "FABRIC_DATA_AGENT_URL", hint: "Published URL ending in /aiassistant/openai"},
    {key: "FABRIC_AUTH_MODE",      hint: "browser | cli"},
    {key: "FABRIC_API_VERSION",    hint: "e.g. 2024-05-01-preview"},
    {key: "FABRIC_POLL_TIMEOUT",   hint: "Seconds, default 600"},
    {key: "FABRIC_POLL_INTERVAL",  hint: "Seconds, default 3"},
  ]},
  {label: "RFP Evaluation — Output", keys: [
    {key: "RFP_OUTPUT_FOLDER",    hint: "Local folder for generated briefs"},
    {key: "RFP_SHARE_RECIPIENTS", hint: "Semicolon-separated emails"},
  ]},
  {label: "Microsoft Graph (optional)", keys: [
    {key: "GRAPH_TENANT_ID",     hint: ""},
    {key: "GRAPH_CLIENT_ID",     hint: ""},
    {key: "GRAPH_CLIENT_SECRET", hint: "Client secret (treated as password)"},
    {key: "GRAPH_USER_UPN",      hint: ""},
  ]},
  {label: "Redis (optional, for Teams remote tasks)", keys: [
    {key: "AZ_REDIS_CACHE_ENDPOINT",   hint: "<host>.region.redis.azure.net:10000"},
    {key: "REDIS_NAMESPACE",           hint: "Default: hub-cowork"},
    {key: "REDIS_SESSION_TTL_SECONDS", hint: "Default: 86400"},
  ]},
];

function _isSecretKey(k) {
  return /(SECRET|KEY|PASSWORD|TOKEN)$/.test(k);
}

function renderEnvEditor() {
  const slot = document.getElementById("envEditorSlot");
  if (!slot) return;
  const overrides = (_loadedConfig && _loadedConfig._env_overrides) || {};
  const html = ENV_GROUPS.map(group => {
    const rows = group.keys.map(({key, hint}) => {
      const isSecret = _isSecretKey(key);
      // Pre-fill: explicit override wins, else fall back to currently
      // active env value (from .env or shipped defaults). This means
      // the editor reflects what the agent is actually running on.
      const val = overrides[key] != null
        ? String(overrides[key])
        : (_currentEnv[key] != null ? String(_currentEnv[key]) : "");
      const inputId = "env_" + key;
      const inputType = isSecret ? "password" : "text";
      const showBtn = isSecret
        ? `<button type="button" class="secondary" style="padding:4px 8px;font-size:11px;"
                   onclick="toggleEnvVisibility('${inputId}', this)">Show</button>`
        : "";
      const hintHtml = hint
        ? `<div class="hint" style="font-size:11px;color:var(--muted);margin-top:2px;">${escapeHtml(hint)}</div>`
        : "";
      return `
        <div class="form-row" style="margin-bottom:8px;">
          <label for="${inputId}" style="font-family:monospace;font-size:12px;">${key}</label>
          <div style="display:flex;gap:6px;align-items:center;">
            <input type="${inputType}" id="${inputId}" data-env-key="${key}"
                   value="${escapeHtml(val)}" style="flex:1;" autocomplete="off" spellcheck="false" />
            ${showBtn}
          </div>
          ${hintHtml}
        </div>`;
    }).join("");
    return `
      <details style="margin-bottom:8px;">
        <summary style="cursor:pointer;font-weight:600;font-size:13px;padding:4px 0;">
          ${escapeHtml(group.label)}
        </summary>
        <div style="padding:8px 4px 0 12px;">${rows}</div>
      </details>`;
  }).join("");
  slot.innerHTML = html;
}

function toggleEnvVisibility(inputId, btn) {
  const el = document.getElementById(inputId);
  if (!el) return;
  if (el.type === "password") {
    el.type = "text";
    btn.textContent = "Hide";
  } else {
    el.type = "password";
    btn.textContent = "Show";
  }
}

function _collectEnvOverrides() {
  // Only persist a key as an override when its value differs from the
  // currently-active env (defaults + user .env). Keeps the config file
  // small and lets future .env updates flow through unchanged keys.
  const out = {};
  // Preserve any existing overrides for keys not exposed in the editor.
  const existing = (_loadedConfig && _loadedConfig._env_overrides) || {};
  const editorKeys = new Set();
  const inputs = document.querySelectorAll("#envEditorSlot input[data-env-key]");
  inputs.forEach(el => {
    const key = el.getAttribute("data-env-key");
    editorKeys.add(key);
    const val = (el.value || "").trim();
    const current = (_currentEnv[key] != null ? String(_currentEnv[key]) : "");
    if (val && val !== current) out[key] = val;
  });
  // Carry through any existing overrides for env keys not in the editor.
  for (const [k, v] of Object.entries(existing)) {
    if (!editorKeys.has(k) && v != null && v !== "") out[k] = v;
  }
  return out;
}

function restartAgent() {
  if (!confirm("Restart the agent now?\n\nAny in-flight tasks will be paused. " +
               "Unsaved Settings changes will be lost — click Save first if needed.")) {
    return;
  }
  setConfigMsg("Restarting agent…", "");
  document.getElementById("restartBtn").disabled = true;
  send({type: "restart"});
  // Hard-reload the UI shortly so it reconnects to the fresh process.
  setTimeout(() => { window.location.reload(); }, 1500);
}

function onConfigSaved(m) {
  if (m.ok) {
    setConfigMsg("Saved.", "ok");
    setTimeout(() => {
      closeSettings();
      setConfigMsg("", "");
    }, 700);
  } else {
    setConfigMsg("Save failed: " + (m.error || "unknown error"), "error");
    document.getElementById("cfgSaveBtn").disabled = false;
  }
}

function setConfigMsg(text, kind) {
  const el = document.getElementById("cfgMsg");
  el.textContent = text || "";
  el.className = "msg" + (kind ? " " + kind : "");
}

function validateTopicCatalog() {
  const ta = document.getElementById("cfgTopicCatalog");
  const hint = document.getElementById("cfgTopicHint");
  const raw = ta.value.trim();
  if (!raw) {
    ta.classList.remove("invalid");
    hint.textContent = "Empty — catalog will be saved as an empty list.";
    return true;
  }
  try {
    const parsed = JSON.parse(raw);
    if (!Array.isArray(parsed)) throw new Error("must be an array");
    ta.classList.remove("invalid");
    hint.textContent = parsed.length + " topic(s). Valid JSON.";
    return true;
  } catch (e) {
    ta.classList.add("invalid");
    hint.textContent = "Invalid JSON: " + e.message;
    return false;
  }
}

function saveSettings() {
  // Parse topic catalog.
  const raw = document.getElementById("cfgTopicCatalog").value.trim();
  let topicCatalog;
  if (raw === "") {
    topicCatalog = [];
  } else {
    try {
      topicCatalog = JSON.parse(raw);
      if (!Array.isArray(topicCatalog)) throw new Error("must be a JSON array");
    } catch (e) {
      setConfigMsg("Topic catalog: " + e.message, "error");
      return;
    }
  }

  // Preserve any keys we don't edit (forward-compat), overwrite the ones we do.
  const next = Object.assign({}, _loadedConfig, {
    hub_name: document.getElementById("cfgHubName").value.trim(),
    default_session_start_time: document.getElementById("cfgStartTime").value.trim(),
    agenda_output_folder: document.getElementById("cfgOutputFolder").value.trim(),
    agenda_template_path: document.getElementById("cfgTemplatePath").value.trim(),
    topic_catalog: topicCatalog,
    _speaker_validations: _serializeSpeakerValidations(),
    _env_overrides: _collectEnvOverrides(),
  });
  // `speakers_by_topic` is derived server-side — don't round-trip it.
  delete next.speakers_by_topic;

  document.getElementById("cfgSaveBtn").disabled = true;
  setConfigMsg("Saving…", "");
  send({type: "save_config", config: next});
}

// ─────────────────────────────────────────────────────────────────
//  Speaker validator (Settings modal)
// ─────────────────────────────────────────────────────────────────
// _speakerState is indexed by lowercased input name. Each entry:
// { input, status: 'pending'|'matched'|'ambiguous'|'not_found'|'error',
//   matches: [{name, role, upn}], picked: <index into matches> }
let _speakerState = {};
let _speakerValidateReqId = null;
// Pool of names the user added but hasn't assigned to a topic yet.
// Each entry: {name, role}. Persists only within the modal session.
let _speakerPool = [];
// Lowercased name currently being re-searched in the speakers table.
let _speakerEditing = null;

function _serializeSpeakerValidations() {
  // Persist only meaningful entries: drop pending/error and entries with
  // no matches so we don't bloat the config file.
  const out = {};
  for (const [key, st] of Object.entries(_speakerState || {})) {
    if (!st || !st.status) continue;
    if (st.status === "pending") continue;
    out[key] = {
      input: st.input || key,
      role: st.role || "",
      status: st.status,
      matches: Array.isArray(st.matches) ? st.matches : [],
      picked: Number.isInteger(st.picked) ? st.picked : 0,
      validated_at: st.validated_at || "",
    };
  }
  return out;
}

function _persistSpeakerValidations() {
  // Silent save: round-trip the current config plus the latest
  // _speaker_validations map. Doesn't touch user-visible save state.
  if (!_loadedConfig) return;
  const next = Object.assign({}, _loadedConfig, {
    _speaker_validations: _serializeSpeakerValidations(),
  });
  _loadedConfig = next;
  send({type: "save_config", config: next, silent: true});
}

function _collectSpeakersFromCatalog() {
  // Extract a deduped ordered list of speaker names from the topic catalog
  // textarea, then append any pool entries not already present.
  const raw = document.getElementById("cfgTopicCatalog").value.trim();
  const seen = new Set();
  const out = [];
  let parsed = [];
  if (raw) {
    try { parsed = JSON.parse(raw); } catch (e) { parsed = []; }
  }
  if (Array.isArray(parsed)) {
    for (const topic of parsed) {
      if (!topic || !Array.isArray(topic.speakers)) continue;
      for (const sp of topic.speakers) {
        const name = (sp && sp.name || "").trim();
        if (!name) continue;
        const key = name.toLowerCase();
        if (seen.has(key)) continue;
        seen.add(key);
        out.push({name, role: (sp && sp.role || "").trim()});
      }
    }
  }
  for (const sp of _speakerPool) {
    const key = (sp.name || "").toLowerCase();
    if (!key || seen.has(key)) continue;
    seen.add(key);
    out.push({name: sp.name, role: sp.role || ""});
  }
  return out;
}

function rebuildSpeakerTable() {
  const speakers = _collectSpeakersFromCatalog();
  let catalogLen = 0;
  try {
    const parsed = JSON.parse(
      document.getElementById("cfgTopicCatalog").value.trim() || "[]");
    catalogLen = Array.isArray(parsed) ? parsed.length : 0;
  } catch (e) { /* ignore */ }
  document.getElementById("spkSummary").textContent =
    speakers.length + " unique speaker(s) across " + catalogLen + " topic(s)";

  // Preserve any prior validation state for names still in the list.
  const next = {};
  for (const sp of speakers) {
    const key = sp.name.toLowerCase();
    next[key] = _speakerState[key] || {
      input: sp.name, role: sp.role,
      status: "pending", matches: [], picked: 0,
    };
    next[key].role = sp.role;
  }
  _speakerState = next;

  const tbody = document.getElementById("spkTbody");
  if (!speakers.length) {
    tbody.innerHTML = '<tr><td colspan="4" class="hint">' +
      'No speakers yet. Add one below or paste names into the JSON.' +
      '</td></tr>';
    document.getElementById("spkValidateBtn").disabled = true;
    document.getElementById("spkApplyBtn").disabled = true;
    return;
  }

  document.getElementById("spkValidateBtn").disabled = false;

  const rows = speakers.map(sp => {
    const key = sp.name.toLowerCase();
    const st = _speakerState[key];
    return _renderSpeakerRow(sp, st);
  });
  tbody.innerHTML = rows.join("");
  _refreshApplyButton();
}

function _renderSpeakerRow(sp, st) {
  const key = sp.name.toLowerCase();
  const escKey = key.replace(/'/g, "\\'");
  const nameHtml = escapeHtml(sp.name);
  const roleHtml = escapeHtml(sp.role || "");
  let statusHtml = '<span class="status-badge pending">Unvalidated</span>';
  let roleCell = roleHtml || '<span class="hint">—</span>';
  let nameCell = nameHtml;
  let rowClass = "";

  // If user clicked "Re-search" on this row, render an inline editor.
  if (_speakerEditing === key) {
    nameCell =
      '<input type="text" class="row-edit" id="spkEdit-' + escapeHtml(key) +
      '" value="' + nameHtml + '" ' +
      'onkeydown="if(event.key===\'Enter\'){event.preventDefault();' +
      'commitSpeakerReSearch(\'' + escKey + '\');}' +
      'else if(event.key===\'Escape\'){cancelSpeakerReSearch();}" />';
    roleCell = '<span class="hint">Type a corrected name and press Enter</span>';
    statusHtml = '<span class="status-badge pending">Editing</span>';
  } else if (st.status === "matched") {
    const m = st.matches[0] || {};
    statusHtml = '<span class="status-badge matched">✓ Matched</span>';
    const resolvedName = m.name || sp.name;
    const resolvedRole = m.role || "";
    const nameDiff = resolvedName.toLowerCase() !== sp.name.toLowerCase();
    const roleDiff = resolvedRole && resolvedRole !== (sp.role || "");
    roleCell =
      '<div>' + escapeHtml(resolvedRole || sp.role || "(no title on file)") + '</div>' +
      (nameDiff || roleDiff
        ? '<div class="resolved">WorkIQ: ' +
          escapeHtml(resolvedName) +
          (resolvedRole ? ' — ' + escapeHtml(resolvedRole) : '') +
          '</div>'
        : '');
  } else if (st.status === "ambiguous") {
    rowClass = "row-ambiguous";
    statusHtml = '<span class="status-badge ambiguous">? Multiple</span>';
    const opts = st.matches.map((m, i) =>
      '<option value="' + i + '"' + (i === (st.picked || 0) ? ' selected' : '') + '>' +
      escapeHtml(m.name) + (m.role ? ' — ' + escapeHtml(m.role) : '') +
      (m.upn ? ' (' + escapeHtml(m.upn) + ')' : '') +
      '</option>'
    ).join("");
    roleCell =
      '<select class="ambig-pick" onchange="pickAmbiguous(\'' + escKey +
      '\', this.value)">' + opts + '</select>';
  } else if (st.status === "not_found") {
    rowClass = "row-notfound";
    statusHtml = '<span class="status-badge notfound">✗ No match</span>';
    roleCell = '<span class="hint">No one found — fix the spelling via Re-search</span>';
  } else if (st.status === "error") {
    statusHtml = '<span class="status-badge error">Error</span>';
    roleCell = '<span class="hint">' + escapeHtml(st.error || "Lookup failed") + '</span>';
  }

  // Action column: Re-search + Remove.
  const actions =
    '<button class="row-act" title="Re-search this name" ' +
    'onclick="startSpeakerReSearch(\'' + escKey + '\')">↻</button>' +
    '<button class="row-act" title="Remove from all topics" ' +
    'onclick="removeSpeakerEverywhere(\'' + escKey + '\')">✕</button>';

  return '<tr class="' + rowClass + '">' +
    '<td>' + nameCell + '</td>' +
    '<td>' + roleCell + '</td>' +
    '<td class="status">' + statusHtml + '</td>' +
    '<td class="status">' + actions + '</td>' +
    '</tr>';
}

function pickAmbiguous(key, idx) {
  const st = _speakerState[key];
  if (!st) return;
  st.picked = parseInt(idx, 10) || 0;
  _refreshApplyButton();
}

function _refreshApplyButton() {
  const any = Object.values(_speakerState).some(
    s => s.status === "matched" || s.status === "ambiguous");
  document.getElementById("spkApplyBtn").disabled = !any;
}

function validateAllSpeakers() {
  const speakers = _collectSpeakersFromCatalog();
  if (!speakers.length) return;
  for (const sp of speakers) {
    const key = sp.name.toLowerCase();
    _speakerState[key] = {
      input: sp.name, role: sp.role,
      status: "pending", matches: [], picked: 0,
    };
  }
  rebuildSpeakerTable();

  document.getElementById("spkValidateBtn").disabled = true;
  document.getElementById("spkApplyBtn").disabled = true;
  _speakerValidateReqId = "s" + Date.now().toString(36);
  document.getElementById("spkStatus").textContent =
    "Validating " + speakers.length + " name(s)…";
  send({
    type: "validate_speakers",
    request_id: _speakerValidateReqId,
    names: speakers.map(s => s.name),
  });
}

function _validateSingleSpeaker(name) {
  // Used by re-search and add-new flows. Sends one name to the server.
  _speakerValidateReqId = "s" + Date.now().toString(36);
  document.getElementById("spkStatus").textContent =
    "Validating \"" + name + "\"…";
  send({
    type: "validate_speakers",
    request_id: _speakerValidateReqId,
    names: [name],
  });
}

function onSpeakersValidating(m) {
  if (m.request_id && m.request_id !== _speakerValidateReqId) return;
  document.getElementById("spkStatus").textContent =
    "Validating " + (m.count || 0) + " name(s)…";
}

function onSpeakersValidated(m) {
  if (m.request_id && m.request_id !== _speakerValidateReqId) return;
  document.getElementById("spkValidateBtn").disabled = false;

  const results = Array.isArray(m.results) ? m.results : [];
  let matched = 0, ambiguous = 0, notFound = 0, errored = 0;

  for (const r of results) {
    const key = (r.input || "").toLowerCase();
    if (!_speakerState[key]) {
      // Could be a re-search/add result for a name not yet in catalog.
      // Inject it into state and pool so the row appears.
      _speakerState[key] = {
        input: r.input, role: "",
        status: "pending", matches: [], picked: 0,
      };
      const exists = _speakerPool.some(
        sp => (sp.name || "").toLowerCase() === key);
      if (!exists) _speakerPool.push({name: r.input, role: ""});
    }
    _speakerState[key].status = r.status || "error";
    _speakerState[key].matches = Array.isArray(r.matches) ? r.matches : [];
    _speakerState[key].picked = 0;
    _speakerState[key].validated_at = new Date().toISOString();
    if (r.error) _speakerState[key].error = r.error;
    if (r.status === "matched") matched++;
    else if (r.status === "ambiguous") ambiguous++;
    else if (r.status === "not_found") notFound++;
    else errored++;
  }

  const parts = [];
  if (matched)   parts.push(matched + " matched");
  if (ambiguous) parts.push(ambiguous + " ambiguous");
  if (notFound)  parts.push(notFound + " not found");
  if (errored)   parts.push(errored + " error(s)");
  document.getElementById("spkStatus").textContent =
    parts.length ? parts.join(" · ") : "No results";

  if (m.error) {
    document.getElementById("spkStatus").textContent =
      "Error: " + m.error;
  }

  rebuildSpeakerTable();
  rebuildTopicCards();  // refresh the dropdown options
  // Persist validation results silently so badges survive a restart.
  _persistSpeakerValidations();
}

function startSpeakerReSearch(key) {
  _speakerEditing = key;
  rebuildSpeakerTable();
  // Focus the new input.
  setTimeout(() => {
    const el = document.getElementById("spkEdit-" + key);
    if (el) { el.focus(); el.select(); }
  }, 0);
}

function cancelSpeakerReSearch() {
  _speakerEditing = null;
  rebuildSpeakerTable();
}

function commitSpeakerReSearch(oldKey) {
  const input = document.getElementById("spkEdit-" + oldKey);
  if (!input) return;
  const newName = (input.value || "").trim();
  _speakerEditing = null;
  if (!newName) { rebuildSpeakerTable(); return; }
  const newKey = newName.toLowerCase();

  // Rename in the catalog JSON: every occurrence of oldKey → newName.
  _renameSpeakerInCatalog(oldKey, newName);
  // Rename in the pool too.
  _speakerPool = _speakerPool.map(sp =>
    (sp.name || "").toLowerCase() === oldKey
      ? {name: newName, role: sp.role || ""}
      : sp);
  // Drop old state, prep new pending entry.
  delete _speakerState[oldKey];
  _speakerState[newKey] = {
    input: newName, role: "",
    status: "pending", matches: [], picked: 0,
  };

  rebuildSpeakerTable();
  rebuildTopicCards();
  _validateSingleSpeaker(newName);
}

function removeSpeakerEverywhere(key) {
  if (!confirm("Remove this speaker from all topics?")) return;
  _removeSpeakerFromCatalog(key);
  _speakerPool = _speakerPool.filter(
    sp => (sp.name || "").toLowerCase() !== key);
  delete _speakerState[key];
  rebuildSpeakerTable();
  rebuildTopicCards();
  _persistSpeakerValidations();
}

function addSpeakerToPool() {
  const inp = document.getElementById("spkAddInput");
  const name = (inp.value || "").trim();
  if (!name) return;
  const key = name.toLowerCase();
  // Already known? Just trigger a re-validate.
  if (!_speakerState[key]) {
    _speakerPool.push({name, role: ""});
    _speakerState[key] = {
      input: name, role: "",
      status: "pending", matches: [], picked: 0,
    };
  }
  inp.value = "";
  rebuildSpeakerTable();
  rebuildTopicCards();
  _validateSingleSpeaker(name);
}

function applyResolvedSpeakers() {
  // Walk the topic catalog JSON, replace each speaker whose name matches an
  // entry in _speakerState (matched/ambiguous) with the resolved name+role.
  const raw = document.getElementById("cfgTopicCatalog").value.trim();
  let parsed;
  try { parsed = JSON.parse(raw); }
  catch (e) {
    setConfigMsg("Cannot apply: topic catalog JSON is invalid.", "error");
    return;
  }
  if (!Array.isArray(parsed)) return;

  let updated = 0;
  for (const topic of parsed) {
    if (!topic || !Array.isArray(topic.speakers)) continue;
    topic.speakers = topic.speakers.map(sp => {
      const nm = (sp && sp.name || "").trim();
      if (!nm) return sp;
      const st = _speakerState[nm.toLowerCase()];
      if (!st) return sp;
      let m = null;
      if (st.status === "matched") m = st.matches[0];
      else if (st.status === "ambiguous") m = st.matches[st.picked || 0];
      if (m && (m.name !== sp.name || (m.role || "") !== (sp.role || ""))) {
        updated++;
        return {name: m.name, role: m.role || sp.role || ""};
      }
      return sp;
    });
  }

  // Also fold in any pool entries that resolved to a real person — promote
  // them so they're not lost on save (but leave them unassigned to topics).
  for (const sp of _speakerPool) {
    const key = (sp.name || "").toLowerCase();
    const st = _speakerState[key];
    if (!st) continue;
    let m = null;
    if (st.status === "matched") m = st.matches[0];
    else if (st.status === "ambiguous") m = st.matches[st.picked || 0];
    if (m) {
      sp.name = m.name;
      sp.role = m.role || sp.role || "";
    }
  }

  document.getElementById("cfgTopicCatalog").value =
    JSON.stringify(parsed, null, 2);
  validateTopicCatalog();
  rebuildSpeakerTable();
  rebuildTopicCards();
  setConfigMsg(
    updated ? ("Applied " + updated + " resolved role(s). Review and Save.")
            : "No changes — resolved names already match.",
    updated ? "ok" : "");
}

// ─── Catalog mutators (operate on the JSON textarea) ─────────────
function _readCatalog() {
  const raw = document.getElementById("cfgTopicCatalog").value.trim();
  if (!raw) return [];
  try {
    const p = JSON.parse(raw);
    return Array.isArray(p) ? p : [];
  } catch (e) { return []; }
}

function _writeCatalog(catalog) {
  document.getElementById("cfgTopicCatalog").value =
    JSON.stringify(catalog, null, 2);
  validateTopicCatalog();
}

function _renameSpeakerInCatalog(oldKey, newName) {
  const cat = _readCatalog();
  for (const topic of cat) {
    if (!topic || !Array.isArray(topic.speakers)) continue;
    for (const sp of topic.speakers) {
      if ((sp && sp.name || "").toLowerCase() === oldKey) {
        sp.name = newName;
        sp.role = "";  // role is now stale; will be filled by Apply.
      }
    }
  }
  _writeCatalog(cat);
}

function _removeSpeakerFromCatalog(key) {
  const cat = _readCatalog();
  for (const topic of cat) {
    if (!topic || !Array.isArray(topic.speakers)) continue;
    topic.speakers = topic.speakers.filter(
      sp => (sp && sp.name || "").toLowerCase() !== key);
  }
  _writeCatalog(cat);
}

// ─────────────────────────────────────────────────────────────────
//  Topic editor (cards with chip-based speaker assignment)
// ─────────────────────────────────────────────────────────────────
// Index of the topic currently in inline-edit mode (-1 = none).
let _topicEditingIdx = -1;
// Whether the "Add topic" form is visible.
let _addTopicOpen = false;

function rebuildTopicCards() {
  const cat = _readCatalog();
  const container = document.getElementById("topicCardList");
  document.getElementById("topicEditorSummary").textContent =
    cat.length + " topic(s)";

  // Render the add-topic form slot.
  const slot = document.getElementById("addTopicSlot");
  if (slot) {
    slot.innerHTML = _addTopicOpen ? _renderAddTopicForm() : "";
  }

  if (!cat.length) {
    container.innerHTML = '<div class="hint">' +
      'No topics yet. Click <strong>+ Add topic</strong> above to create one.' +
      '</div>';
    return;
  }

  // Pool of unique names (catalog + pool) for the dropdown.
  const allSpeakers = _collectSpeakersFromCatalog();
  container.innerHTML = cat.map((t, idx) =>
    _renderTopicCard(t, idx, allSpeakers)).join("");
}

function _resolvedDisplay(name) {
  // If the speaker is matched in WorkIQ, show the canonical name+role
  // alongside the chip label.
  const st = _speakerState[(name || "").toLowerCase()];
  if (!st) return {label: name, sub: "", title: name};
  if (st.status === "matched" && st.matches[0]) {
    const m = st.matches[0];
    return {
      label: m.name,
      sub: m.role || "",
      title: m.name + (m.role ? " — " + m.role : ""),
    };
  }
  if (st.status === "not_found") {
    return {label: name + " ⚠", sub: "",
            title: "WorkIQ found no match for this name"};
  }
  return {label: name, sub: "", title: name};
}

function _resolveSpeaker(name) {
  // Resolve a name to its best {name, role} pair, preferring the WorkIQ
  // match when available, then catalog/pool role, then blank.
  const key = (name || "").toLowerCase();
  const st = _speakerState[key];
  if (st) {
    if (st.status === "matched" && st.matches[0]) {
      const m = st.matches[0];
      return {name: m.name || name, role: m.role || ""};
    }
    if (st.status === "ambiguous" && st.matches[st.picked || 0]) {
      const m = st.matches[st.picked || 0];
      return {name: m.name || name, role: m.role || ""};
    }
    if (st.role) return {name, role: st.role};
  }
  // Fall back to the role recorded in the catalog/pool for this name.
  const all = _collectSpeakersFromCatalog();
  const hit = all.find(s => s.name.toLowerCase() === key);
  if (hit && hit.role) return {name, role: hit.role};
  return {name, role: ""};
}

function _renderTopicCard(topic, idx, allSpeakers) {
  // Inline edit mode for title/category/description.
  if (_topicEditingIdx === idx) {
    return _renderTopicEditCard(topic, idx);
  }

  const cat = escapeHtml(topic.topic_category || "");
  const title = escapeHtml(topic.topic || "(untitled topic)");
  const desc = escapeHtml(topic.description || "");
  const speakers = Array.isArray(topic.speakers) ? topic.speakers : [];
  const assigned = new Set(
    speakers.map(s => (s && s.name || "").toLowerCase()).filter(Boolean));

  const chips = speakers.length ? speakers.map(sp => {
    const nm = (sp && sp.name || "").trim();
    if (!nm) return "";
    const disp = _resolvedDisplay(nm);
    // Prefer the role that is actually saved on this topic; fall back to
    // the WorkIQ-resolved role if the catalog entry has none.
    const shownRole = (sp.role || "").trim() || disp.sub;
    const sub = shownRole
      ? '<span class="chip-sub"> — ' + escapeHtml(shownRole) + '</span>'
      : '';
    return '<span class="chip" title="' + escapeHtml(disp.title) + '">' +
      escapeHtml(disp.label) + sub +
      '<button class="x" title="Remove from this topic" ' +
      'onclick="removeSpeakerFromTopic(' + idx + ', \'' +
      nm.toLowerCase().replace(/'/g, "\\'") + '\')">×</button>' +
      '</span>';
  }).join("") : '<span class="chip-empty">No speakers assigned</span>';

  // Picker dropdown: only speakers in the pool not already assigned.
  // We resolve each option through _resolveSpeaker so the role label and
  // the value embedded for selection both reflect any WorkIQ match.
  const opts = allSpeakers
    .filter(s => !assigned.has(s.name.toLowerCase()))
    .map(s => {
      const r = _resolveSpeaker(s.name);
      const role = r.role || s.role || "";
      // Encode {name, role} in the option value so addSpeakerToTopic
      // doesn't need to re-lookup state (which can miss after Apply).
      const payload = JSON.stringify({name: s.name, role}).replace(/"/g, "&quot;");
      return '<option value="' + payload + '">' +
        escapeHtml(s.name) +
        (role ? ' — ' + escapeHtml(role) : '') +
        '</option>';
    }).join("");

  const pickerId = "tpkr-" + idx;
  const pickerHtml = opts
    ? '<select id="' + pickerId + '"><option value="">— add a speaker —</option>' +
      opts + '</select>' +
      '<button class="secondary" onclick="addSpeakerToTopic(' + idx +
      ', document.getElementById(\'' + pickerId + '\').value)">Add</button>'
    : '<span class="hint">All known speakers are already assigned. ' +
      'Add a new one in the Speakers section above.</span>';

  return '<div class="topic-card">' +
    '<div class="topic-head">' +
      '<div class="topic-title">' + title + '</div>' +
      '<div class="head-acts">' +
        '<button class="row-act" title="Edit title / category / description" ' +
        'onclick="startEditTopic(' + idx + ')">✎</button>' +
        '<button class="row-act" title="Remove this topic" ' +
        'onclick="removeTopic(' + idx + ')">✕</button>' +
      '</div>' +
    '</div>' +
    (cat ? '<div class="topic-cat">' + cat + '</div>' : '') +
    (desc ? '<div class="topic-desc">' + desc + '</div>' : '') +
    '<div class="chips">' + chips + '</div>' +
    '<div class="picker-row">' + pickerHtml + '</div>' +
    '</div>';
}

function _renderTopicEditCard(topic, idx) {
  const title = escapeHtml(topic.topic || "");
  const cat = escapeHtml(topic.topic_category || "");
  const desc = escapeHtml(topic.description || "");
  return '<div class="topic-card">' +
    '<div class="topic-edit">' +
      '<input type="text" id="te-title-' + idx + '" placeholder="Topic title" value="' + title + '" />' +
      '<input type="text" id="te-cat-' + idx + '" placeholder="Topic category / tags" value="' + cat + '" />' +
      '<textarea id="te-desc-' + idx + '" placeholder="Description / abstract">' + desc + '</textarea>' +
      '<div class="row">' +
        '<button class="secondary" onclick="cancelEditTopic()">Cancel</button>' +
        '<button class="primary" onclick="saveEditTopic(' + idx + ')">Save</button>' +
      '</div>' +
    '</div>' +
    '</div>';
}

function startEditTopic(idx) {
  _topicEditingIdx = idx;
  rebuildTopicCards();
  setTimeout(() => {
    const el = document.getElementById("te-title-" + idx);
    if (el) { el.focus(); el.select(); }
  }, 0);
}

function cancelEditTopic() {
  _topicEditingIdx = -1;
  rebuildTopicCards();
}

function saveEditTopic(idx) {
  const titleEl = document.getElementById("te-title-" + idx);
  const catEl = document.getElementById("te-cat-" + idx);
  const descEl = document.getElementById("te-desc-" + idx);
  if (!titleEl) return;
  const title = (titleEl.value || "").trim();
  if (!title) { titleEl.focus(); return; }
  const cat = _readCatalog();
  if (idx < 0 || idx >= cat.length) return;
  cat[idx].topic = title;
  cat[idx].topic_category = (catEl.value || "").trim();
  cat[idx].description = (descEl.value || "").trim();
  _writeCatalog(cat);
  _topicEditingIdx = -1;
  rebuildTopicCards();
}

function showAddTopicForm() {
  _addTopicOpen = true;
  rebuildTopicCards();
  setTimeout(() => {
    const el = document.getElementById("nt-title");
    if (el) el.focus();
  }, 0);
}

function _renderAddTopicForm() {
  return '<div class="add-topic-form">' +
    '<input type="text" id="nt-title" placeholder="Topic title (required)" />' +
    '<input type="text" id="nt-cat" placeholder="Topic category / tags" />' +
    '<textarea id="nt-desc" placeholder="Description / abstract"></textarea>' +
    '<div class="row">' +
      '<button class="secondary" onclick="cancelAddTopic()">Cancel</button>' +
      '<button class="primary" onclick="commitAddTopic()">Add topic</button>' +
    '</div>' +
    '</div>';
}

function cancelAddTopic() {
  _addTopicOpen = false;
  rebuildTopicCards();
}

function commitAddTopic() {
  const titleEl = document.getElementById("nt-title");
  const catEl = document.getElementById("nt-cat");
  const descEl = document.getElementById("nt-desc");
  if (!titleEl) return;
  const title = (titleEl.value || "").trim();
  if (!title) { titleEl.focus(); return; }
  const cat = _readCatalog();
  cat.push({
    topic: title,
    topic_category: (catEl.value || "").trim(),
    description: (descEl.value || "").trim(),
    speakers: [],
  });
  _writeCatalog(cat);
  _addTopicOpen = false;
  rebuildTopicCards();
  rebuildSpeakerTable();
}

function addSpeakerToTopic(topicIdx, raw) {
  // raw is either a JSON-encoded {name, role} payload from the dropdown,
  // or a bare name string (defensive fallback).
  if (!raw) return;
  let name = "", role = "";
  try {
    const obj = JSON.parse(raw);
    name = (obj && obj.name || "").trim();
    role = (obj && obj.role || "").trim();
  } catch (e) {
    name = String(raw).trim();
  }
  if (!name) return;
  // If we didn't get a role from the payload, resolve it now.
  if (!role) {
    const r = _resolveSpeaker(name);
    name = r.name || name;
    role = r.role || "";
  }
  const cat = _readCatalog();
  if (topicIdx < 0 || topicIdx >= cat.length) return;
  const topic = cat[topicIdx];
  if (!Array.isArray(topic.speakers)) topic.speakers = [];
  const exists = topic.speakers.some(
    s => (s && s.name || "").toLowerCase() === name.toLowerCase());
  if (exists) return;
  topic.speakers.push({name, role});
  _writeCatalog(cat);
  rebuildTopicCards();
  rebuildSpeakerTable();
}

function removeSpeakerFromTopic(topicIdx, key) {
  const cat = _readCatalog();
  if (topicIdx < 0 || topicIdx >= cat.length) return;
  const topic = cat[topicIdx];
  if (!Array.isArray(topic.speakers)) return;
  topic.speakers = topic.speakers.filter(
    s => (s && s.name || "").toLowerCase() !== key);
  _writeCatalog(cat);
  rebuildTopicCards();
  rebuildSpeakerTable();
}

function removeTopic(idx) {
  const cat = _readCatalog();
  if (idx < 0 || idx >= cat.length) return;
  if (!confirm("Remove this topic from the catalog?")) return;
  cat.splice(idx, 1);
  _writeCatalog(cat);
  rebuildTopicCards();
  rebuildSpeakerTable();
}


function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, c => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

// ─────────────────────────────────────────────────────────────────
//  Minimal, safe Markdown renderer
//  Supports: fenced code, GFM tables, headings, blockquotes, ordered
//  and unordered lists, horizontal rules, inline code, bold, italic,
//  strikethrough, links, and auto-paragraphs. All input is HTML-escaped
//  before any markup is injected, so output cannot carry raw HTML.
// ─────────────────────────────────────────────────────────────────
function renderMarkdown(src) {
  if (!src) return "";
  src = String(src).replace(/\r\n/g, "\n").replace(/\r/g, "\n");

  // Strip internal control-flow markers that may have leaked into older
  // persisted messages. New messages already have these stripped server-side
  // (see agent_core.py); this is a defensive cleanup for historical threads.
  src = src.replace(/\[AWAITING_CONFIRMATION\]/g, "")
           .replace(/\[STOP_CHAIN\]/g, "")
           .replace(/\n{3,}/g, "\n\n")
           .trim();

  // 1. Extract fenced code blocks first so their contents are not processed.
  const codeBlocks = [];
  src = src.replace(/```([a-zA-Z0-9_-]*)\n([\s\S]*?)```/g, (_, lang, code) => {
    const idx = codeBlocks.length;
    codeBlocks.push({ lang: lang || "", code: code.replace(/\n$/, "") });
    return `\u0000CODEBLOCK${idx}\u0000`;
  });

  const lines = src.split("\n");
  const out = [];
  let i = 0;

  const flushPara = (buf) => {
    if (!buf.length) return;
    const text = buf.join("\n").trim();
    buf.length = 0;
    if (text) out.push("<p>" + renderInline(text) + "</p>");
  };

  let para = [];

  while (i < lines.length) {
    const line = lines[i];

    // Code-block placeholder on its own line.
    const phMatch = /^\u0000CODEBLOCK(\d+)\u0000\s*$/.exec(line);
    if (phMatch) {
      flushPara(para);
      const blk = codeBlocks[Number(phMatch[1])];
      out.push("<pre><code>" + escapeHtml(blk.code) + "</code></pre>");
      i++;
      continue;
    }

    // Blank line ends a paragraph.
    if (/^\s*$/.test(line)) {
      flushPara(para);
      i++;
      continue;
    }

    // Horizontal rule.
    if (/^\s*(?:---|\*\*\*|___)\s*$/.test(line)) {
      flushPara(para);
      out.push("<hr/>");
      i++;
      continue;
    }

    // ATX heading.
    const h = /^(#{1,6})\s+(.*?)\s*#*\s*$/.exec(line);
    if (h) {
      flushPara(para);
      const level = h[1].length;
      out.push(`<h${level}>${renderInline(h[2])}</h${level}>`);
      i++;
      continue;
    }

    // GFM table: header line, delimiter line, then zero+ row lines.
    if (/\|/.test(line) && i + 1 < lines.length &&
        /^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)+\|?\s*$/.test(lines[i + 1])) {
      flushPara(para);
      const headers = splitTableRow(line);
      const aligns = splitTableRow(lines[i + 1]).map(parseAlign);
      i += 2;
      const rows = [];
      while (i < lines.length && /\|/.test(lines[i]) && !/^\s*$/.test(lines[i])) {
        rows.push(splitTableRow(lines[i]));
        i++;
      }
      let html = "<table><thead><tr>";
      headers.forEach((h, idx) => {
        const a = aligns[idx] ? ` style="text-align:${aligns[idx]}"` : "";
        html += `<th${a}>${renderInline(h)}</th>`;
      });
      html += "</tr></thead><tbody>";
      for (const row of rows) {
        html += "<tr>";
        for (let idx = 0; idx < headers.length; idx++) {
          const a = aligns[idx] ? ` style="text-align:${aligns[idx]}"` : "";
          html += `<td${a}>${renderInline(row[idx] || "")}</td>`;
        }
        html += "</tr>";
      }
      html += "</tbody></table>";
      out.push(html);
      continue;
    }

    // Blockquote.
    if (/^\s*>/.test(line)) {
      flushPara(para);
      const buf = [];
      while (i < lines.length && /^\s*>/.test(lines[i])) {
        buf.push(lines[i].replace(/^\s*>\s?/, ""));
        i++;
      }
      out.push("<blockquote>" + renderMarkdown(buf.join("\n")) + "</blockquote>");
      continue;
    }

    // Unordered list.
    if (/^\s*[-*+]\s+/.test(line)) {
      flushPara(para);
      const items = [];
      while (i < lines.length && /^\s*[-*+]\s+/.test(lines[i])) {
        let item = lines[i].replace(/^\s*[-*+]\s+/, "");
        i++;
        while (i < lines.length && /^\s{2,}\S/.test(lines[i])) {
          item += "\n" + lines[i].replace(/^\s{2}/, "");
          i++;
        }
        items.push(item);
      }
      out.push("<ul>" + items.map(it => "<li>" + renderInline(it) + "</li>").join("") + "</ul>");
      continue;
    }

    // Ordered list.
    if (/^\s*\d+[.)]\s+/.test(line)) {
      flushPara(para);
      const items = [];
      while (i < lines.length && /^\s*\d+[.)]\s+/.test(lines[i])) {
        let item = lines[i].replace(/^\s*\d+[.)]\s+/, "");
        i++;
        while (i < lines.length && /^\s{2,}\S/.test(lines[i])) {
          item += "\n" + lines[i].replace(/^\s{2}/, "");
          i++;
        }
        items.push(item);
      }
      out.push("<ol>" + items.map(it => "<li>" + renderInline(it) + "</li>").join("") + "</ol>");
      continue;
    }

    // Default: paragraph line.
    para.push(line);
    i++;
  }
  flushPara(para);

  let html = out.join("\n");
  // Restore any code-block placeholders that survived inside paragraphs.
  html = html.replace(/\u0000CODEBLOCK(\d+)\u0000/g, (_, n) => {
    const blk = codeBlocks[Number(n)];
    return "<pre><code>" + escapeHtml(blk.code) + "</code></pre>";
  });
  return html;
}

function splitTableRow(line) {
  let s = line.trim();
  if (s.startsWith("|")) s = s.slice(1);
  if (s.endsWith("|")) s = s.slice(0, -1);
  // Split on '|' not preceded by backslash.
  return s.split(/(?<!\\)\|/).map(c => c.trim().replace(/\\\|/g, "|"));
}

function parseAlign(cell) {
  const c = cell.trim();
  const left = c.startsWith(":");
  const right = c.endsWith(":");
  if (left && right) return "center";
  if (right) return "right";
  if (left) return "left";
  return "";
}

function renderInline(text) {
  // Escape first, then apply inline markup using unique sentinels so that
  // e.g. URL contents don't get re-processed.
  let s = escapeHtml(text);

  // Inline code — protect content from further processing.
  const inlineCode = [];
  s = s.replace(/`([^`\n]+)`/g, (_, code) => {
    inlineCode.push(code);
    return `\u0001${inlineCode.length - 1}\u0001`;
  });

  // Links: [text](url)
  s = s.replace(/\[([^\]]+)\]\(([^)\s]+)(?:\s+"([^"]*)")?\)/g, (_, label, url, title) => {
    // Allow web/mail/in-page anchors, plus local file:/// URLs (which we
    // intercept globally in a click handler and route through the agent's
    // open_file WS message — pywebview won't navigate file:// itself).
    let safeUrl = "#";
    if (/^(https?:|mailto:|#|\/)/i.test(url)) {
      safeUrl = url;
    } else if (/^file:/i.test(url)) {
      safeUrl = url;
    }
    const t = title ? ` title="${title}"` : "";
    const isFile = /^file:/i.test(safeUrl);
    const cls = isFile ? ' class="file-link"' : "";
    const target = isFile ? "" : ' target="_blank" rel="noopener noreferrer"';
    return `<a href="${safeUrl}"${target}${cls}${t}>${label}</a>`;
  });

  // Bold + italic.
  s = s.replace(/\*\*\*([^*\n]+)\*\*\*/g, "<strong><em>$1</em></strong>");
  s = s.replace(/\*\*([^*\n]+)\*\*/g, "<strong>$1</strong>");
  s = s.replace(/(^|[\s(])\*([^*\n]+)\*(?=[\s).,;:!?]|$)/g, "$1<em>$2</em>");
  s = s.replace(/__([^_\n]+)__/g, "<strong>$1</strong>");
  s = s.replace(/(^|[\s(])_([^_\n]+)_(?=[\s).,;:!?]|$)/g, "$1<em>$2</em>");

  // Strikethrough.
  s = s.replace(/~~([^~\n]+)~~/g, "<del>$1</del>");

  // Restore inline code.
  s = s.replace(/\u0001(\d+)\u0001/g, (_, n) => "<code>" + inlineCode[Number(n)] + "</code>");

  return s;
}

// ─────────────────────────────────────────────────────────────────
//  Top-bar app menu (kebab) — Settings / Restart / About
// ─────────────────────────────────────────────────────────────────
function toggleAppMenu(ev) {
  if (ev) ev.stopPropagation();
  const menu = document.getElementById("appMenu");
  const btn = document.getElementById("appMenuBtn");
  if (!menu) return;
  const open = menu.classList.toggle("open");
  if (btn) btn.setAttribute("aria-expanded", open ? "true" : "false");
}

function closeAppMenu() {
  const menu = document.getElementById("appMenu");
  const btn = document.getElementById("appMenuBtn");
  if (menu) menu.classList.remove("open");
  if (btn) btn.setAttribute("aria-expanded", "false");
}

// Click anywhere outside the menu to dismiss it.
document.addEventListener("click", (ev) => {
  const wrap = document.querySelector(".app-menu");
  if (wrap && !wrap.contains(ev.target)) closeAppMenu();
});
document.addEventListener("keydown", (ev) => {
  if (ev.key === "Escape") closeAppMenu();
});

function showAbout() {
  alert(
    "Hub Cowork\n\n" +
    "Single-process Windows desktop agent for the Hub workflow.\n" +
    "Routes user messages to skills, runs tools, and bridges Teams via Redis.\n\n" +
    "WebSocket: ws://127.0.0.1:18080\n" +
    "Data: ~/.hub-cowork/"
  );
}

// ─────────────────────────────────────────────────────────────────
//  Skills modal — list of skills loaded by the agent, grouped by folder
// ─────────────────────────────────────────────────────────────────
let _loadedSkills = [];

function openSkillsModal() {
  const m = document.getElementById("skillsModal");
  if (!m) return;
  renderSkillsModal();
  m.classList.add("open");
}

function closeSkillsModal() {
  const m = document.getElementById("skillsModal");
  if (m) m.classList.remove("open");
}

function onSkillsBackdrop(ev) {
  if (ev && ev.target && ev.target.id === "skillsModal") closeSkillsModal();
}

function _prettyGroupName(g) {
  if (!g) return "General";
  return g.replace(/[_-]+/g, " ")
          .replace(/\b\w/g, (c) => c.toUpperCase());
}

function _esc(s) {
  return String(s == null ? "" : s)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function renderSkillsModal() {
  const body = document.getElementById("skillsBody");
  if (!body) return;
  const skills = _loadedSkills || [];
  if (!skills.length) {
    body.innerHTML = '<div class="skills-empty">No skills loaded yet.</div>';
    return;
  }
  // Group by `group` (parent folder); top-level skills go under "General".
  const groups = new Map();
  for (const s of skills) {
    const key = s.group || "";
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(s);
  }
  // Sort: top-level ("General") first, then alphabetical groups.
  const ordered = [...groups.keys()].sort((a, b) => {
    if (a === "" && b !== "") return -1;
    if (b === "" && a !== "") return 1;
    return a.localeCompare(b);
  });

  const html = ordered.map((key) => {
    const items = groups.get(key).slice().sort((a, b) => {
      // Internal/chained skills last; otherwise alphabetical.
      if (!!a.internal !== !!b.internal) return a.internal ? 1 : -1;
      return a.name.localeCompare(b.name);
    });
    const title = _prettyGroupName(key);
    const skillsHtml = items.map((s) => {
      // Strip the [INTERNAL...] prefix from displayed description.
      const desc = String(s.description || "").replace(/^\[INTERNAL[^\]]*\]\s*/i, "").trim();
      const badges = [];
      if (s.model) badges.push(`<span class="badge model-${_esc(s.model)}">${_esc(s.model)}</span>`);
      if (s.internal) badges.push('<span class="badge internal">internal</span>');
      if (s.next_skill) badges.push(`<span class="badge chain">→ ${_esc(s.next_skill)}</span>`);
      const tools = (s.tools || []).map(t => `<code class="tool">${_esc(t)}</code>`).join(" ");
      return `
        <div class="skill-card${s.internal ? ' internal' : ''}">
          <div class="skill-row">
            <span class="skill-name">${_esc(s.name)}</span>
            <span class="skill-badges">${badges.join(" ")}</span>
          </div>
          <div class="skill-desc">${_esc(desc) || '<i>No description.</i>'}</div>
          ${tools ? `<div class="skill-tools">${tools}</div>` : ""}
        </div>
      `;
    }).join("");
    return `
      <section class="skill-group">
        <h3 class="skill-group-title">${_esc(title)} <span class="skill-group-count">${items.length}</span></h3>
        <div class="skill-group-body">${skillsHtml}</div>
      </section>
    `;
  }).join("");

  body.innerHTML = html;
}

// Boot
setDetailsCollapsed(true);
connect();

// Intercept clicks on file:// links anywhere in the UI and ask the agent
// to open the file with the OS handler. pywebview won't navigate to
// file:// URLs and even if it did the embedded webview can't launch
// Word/Excel/etc. The agent runs locally so it can just os.startfile.
document.addEventListener("click", (e) => {
  const a = e.target.closest && e.target.closest('a[href^="file:"]');
  if (!a) return;
  e.preventDefault();
  let url = a.getAttribute("href") || "";
  // file:///C:/Users/... → C:/Users/...
  let path = url.replace(/^file:\/{2,3}/i, "");
  try { path = decodeURIComponent(path); } catch (_) { /* ignore */ }
  send({type: "open_file", path: path});
});

// Close settings modal on Escape.
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") {
    const m = document.getElementById("settingsModal");
    if (m && m.classList.contains("open")) closeSettings();
    const sk = document.getElementById("skillsModal");
    if (sk && sk.classList.contains("open")) closeSkillsModal();
  }
});
