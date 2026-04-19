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
  activeTab: "info",           // currently visible right-pane tab
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
    case "skills_list":     break; // informational only
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
  document.getElementById("authText").textContent = label || (ok ? "Authenticated" : "Not signed in");
  document.getElementById("signinBtn").style.display = ok ? "none" : "";
}

function signin() {
  send({type: "signin"});
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
  document.getElementById("archToggle").textContent =
    state.showArchived ? "Hide archived" : "Show archived";
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
  const showAwaiting = document.getElementById("filterAwaiting").checked;
  const showDone = document.getElementById("filterDone").checked;

  const list = Array.from(state.threads.values())
    .filter(t => {
      if (["running", "active"].includes(t.status)) return showRunning;
      if (t.status === "awaiting_user") return showAwaiting;
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
}

function clearDetailPanels() {
  document.getElementById("panel-info").innerHTML = "";
  document.getElementById("panel-progress").innerHTML = "";
  document.getElementById("panel-logs").innerHTML = "";
}

function renderActiveTab() {
  const name = state.activeTab || "info";
  if (name === "info" || name === "progress") {
    // renderDetails fills both Info and Progress panels — cheap enough to
    // render together since metadata is small.
    renderDetails();
  } else if (name === "logs") {
    renderLogs();
  }
}

function renderSelectedHeader() {
  const tag = document.getElementById("chatTag");
  const title = document.getElementById("chatTitle");
  const status = document.getElementById("chatStatus");
  if (state.selectedId === SYSTEM_THREAD_ID) {
    tag.textContent = "#system";
    title.textContent = "System · cross-task queries";
    status.textContent = "ready";
    status.className = "status-label";
    updateComposerLockState();
    return;
  }
  if (state.selectedId === DRAFT_THREAD_ID) {
    tag.textContent = "#new";
    title.textContent = "New task";
    status.textContent = "draft";
    status.className = "status-label";
    updateComposerLockState();
    return;
  }
  const t = state.threads.get(state.selectedId) || state.archivedThreads.get(state.selectedId);
  if (!t) { tag.textContent = ""; title.textContent = "(not found)"; updateComposerLockState(); return; }
  tag.textContent = t.correlation_tag || ("#thread-" + t.id);
  title.textContent = t.title || "(untitled)";
  status.textContent = t.status || "unknown";
  status.className = "status-label " + (t.status || "");
  updateComposerLockState();
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
  }

  wrap.classList.toggle("show-notice", show);
  notice.innerHTML = show ? label : "";
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
  for (const msg of d.messages || []) {
    appendMsg(msg.role, msg.content);
  }
  // If this thread is still working, re-attach the shimmer so the user
  // sees ongoing activity after switching tabs or reopening the app.
  // Only "running" means the executor is actively working — "active" is the
  // default idle status for any non-archived thread.
  const t = state.threads.get(state.selectedId);
  if (t && t.status === "running") {
    const log = d.progress_log || [];
    const last = log.length ? log[log.length - 1] : null;
    const lastMsg = last
      ? (Array.isArray(last) ? last[2] : last.message)
      : "Working…";
    setLiveStatus(lastMsg);
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
  body.appendChild(d);
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

function onThreadProgress(m) {
  if (state.selectedId === m.thread_id) {
    setLiveStatus(m.message);
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
  // Only re-render details when the Info/Progress tab is actually visible —
  // no point rebuilding a hidden panel on every progress tick.
  if (state.selectedId === m.thread_id
      && (state.activeTab === "info" || state.activeTab === "progress")) {
    renderDetails();
  }
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

function loadThreadDetail(thread) {
  // Preserve any progress events we buffered before the full detail arrived.
  const existing = state.threadDetail.get(thread.id);
  if (existing && existing.progress_log && existing.progress_log.length) {
    const serverLog = thread.progress_log || [];
    // Server is authoritative when it has entries; otherwise keep our buffer.
    if (!serverLog.length) thread.progress_log = existing.progress_log;
  }
  state.threadDetail.set(thread.id, thread);
  if (state.selectedId === thread.id) {
    renderChatBody();
    // Only refresh the right pane if the user is currently looking at
    // Info/Progress; Logs pulls from a separate state.logs buffer.
    if (state.activeTab === "info" || state.activeTab === "progress") {
      renderDetails();
    }
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
    if (state.activeTab === "info") renderDetails();
  }
}
function onSystemQueryError(m) {
  state.systemMessages.push({role: "assistant", content: "⚠️  " + m.error});
  if (state.selectedId === SYSTEM_THREAD_ID) {
    clearLiveStatus();
    appendMsg("assistant", "⚠️  " + m.error);
    if (state.activeTab === "info") renderDetails();
  }
}

function clearSystemConversation() {
  state.systemMessages = [];
  if (state.selectedId === SYSTEM_THREAD_ID) {
    renderChatBody();
    renderDetails();
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
}

function sendInput() {
  const box = document.getElementById("inputBox");
  const text = box.value.trim();
  if (!text) return;
  box.value = "";
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
//  Right pane: details / progress / logs
// ─────────────────────────────────────────────────────────────────
function switchTab(name) {
  state.activeTab = name;
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

function renderDetails() {
  const info = document.getElementById("panel-info");
  const prog = document.getElementById("panel-progress");

  if (state.selectedId === SYSTEM_THREAD_ID) {
    info.innerHTML = '<div class="hint" style="margin-bottom:8px;">System thread — ephemeral Q&amp;A. Conversation is not persisted and is lost on app restart.</div>';
    if (state.systemMessages.length) {
      const actions = document.createElement("div");
      actions.className = "actions";
      actions.innerHTML = `<button class="danger" onclick="clearSystemConversation()">Clear conversation</button>`;
      info.appendChild(actions);
    }
    prog.innerHTML = '<div class="empty">—</div>';
    return;
  }

  const t = state.threadDetail.get(state.selectedId)
        || state.threads.get(state.selectedId)
        || state.archivedThreads.get(state.selectedId);
  if (!t) {
    info.innerHTML = '<div class="empty">(not found)</div>';
    prog.innerHTML = '';
    return;
  }

  info.innerHTML = "";
  const dl = document.createElement("dl");
  const add = (k, v) => {
    const dt = document.createElement("dt"); dt.textContent = k; dl.appendChild(dt);
    const dd = document.createElement("dd"); dd.textContent = v == null ? "—" : String(v); dl.appendChild(dd);
  };
  add("Correlation tag", t.correlation_tag || ("#thread-" + t.id));
  add("Status", t.status);
  add("Skill", t.skill_name);
  add("Source", t.source);
  if (t.external_user) add("External user", t.external_user);
  if (t.created_at) add("Created", new Date(t.created_at * 1000).toLocaleString());
  info.appendChild(dl);

  const actions = document.createElement("div");
  actions.className = "actions";
  if (t.status === "archived") {
    actions.innerHTML = `<button onclick="unarchiveThread('${t.id}')">Restore</button>`;
  } else {
    actions.innerHTML = `
      <button onclick="archiveThread('${t.id}')">Archive</button>
      <button class="danger" onclick="deleteThread('${t.id}')">Delete</button>`;
  }
  info.appendChild(actions);

  // Progress panel
  prog.innerHTML = "";
  const steps = t.progress_log || [];
  if (!steps.length) {
    prog.innerHTML = '<div class="empty">No progress events yet.</div>';
  } else {
    for (const step of steps) {
      // Server persists entries as {ts, kind, message, request_id}; older
      // client-side pushes may have used [ts, kind, msg] tuples. Handle both.
      const ts   = Array.isArray(step) ? step[0] : step.ts;
      const kind = Array.isArray(step) ? step[1] : step.kind;
      const msg  = Array.isArray(step) ? step[2] : step.message;
      const row = document.createElement("div");
      row.style.cssText = "font-size:12px;padding:6px 0;border-bottom:1px solid var(--border);";
      const time = new Date((ts || 0) * 1000).toLocaleTimeString();
      // Native tooltip with the full untruncated message on hover.
      row.title = String(msg ?? "");
      const head = document.createElement("div");
      head.style.cssText = "color:var(--muted);margin-bottom:2px;";
      head.textContent = `${time} · ${kind}`;
      const bodyEl = document.createElement("div");
      bodyEl.className = "md";
      // Render markdown so bold, lists, tables, code, and links display
      // properly. The raw original is still available via row.title.
      bodyEl.innerHTML = renderMarkdown(String(msg ?? ""));
      row.appendChild(head);
      row.appendChild(bodyEl);
      prog.appendChild(row);
    }
  }
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
  {label: "RFP Evaluation — Foundry Agent", keys: [
    {key: "FOUNDRY_PROJECT_ENDPOINT", hint: "Foundry project endpoint"},
    {key: "FOUNDRY_AGENT_NAME",       hint: "Agent name, e.g. project-analysis-agent"},
    {key: "FOUNDRY_AUTH_MODE",        hint: "browser | cli"},
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

// Boot
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
  }
});
