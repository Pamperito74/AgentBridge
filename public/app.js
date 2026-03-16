// ── Auth ──────────────────────────────────────────────────────────────────
const TOKEN_KEY = "ab_token";
const SENDER_KEY = "ab_sender";

const getToken  = () => sessionStorage.getItem(TOKEN_KEY) ?? "";
const setToken  = (t) => sessionStorage.setItem(TOKEN_KEY, t);
const clearToken = () => sessionStorage.removeItem(TOKEN_KEY);

function showAuthModal() {
  return new Promise((resolve) => {
    overlay.classList.remove("hidden");
    tokenInput.value = "";
    requestAnimationFrame(() => tokenInput.focus());
    const submit = () => {
      const val = tokenInput.value.trim();
      if (!val) return;
      setToken(val);
      overlay.classList.add("hidden");
      resolve(val);
    };
    tokenSubmit.onclick = submit;
    tokenInput.onkeydown = (e) => { if (e.key === "Enter") submit(); };
  });
}

async function ensureToken() {
  if (!getToken()) await showAuthModal();
}

// ── HTTP ──────────────────────────────────────────────────────────────────
const BASE = "";

async function api(path, opts = {}) {
  const token = getToken();
  const headers = {
    ...(opts.headers ?? {}),
    ...(token ? { Authorization: `Bearer ${token}` } : {}),
  };
  const res = await fetch(BASE + path, { ...opts, headers });
  if (res.status === 401) {
    clearToken();
    await ensureToken();
    return api(path, opts);
  }
  if (!res.ok) throw new Error(`${res.status} ${await res.text().catch(() => "")}`);
  return res.json();
}

const post = (path, body) => api(path, {
  method: "POST",
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify(body),
});

const patch = (path, body) => api(path, {
  method: "PATCH",
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify(body),
});

// ── State ─────────────────────────────────────────────────────────────────
let rooms = [];
let agents = [];
let tasks = [];
let messages = [];
let activeRoomId = null;
let lastMsgId = null;

// ── DOM ───────────────────────────────────────────────────────────────────
const overlay      = document.getElementById("auth-overlay");
const tokenInput   = document.getElementById("token-input");
const tokenSubmit  = document.getElementById("token-submit");
const connDot      = document.getElementById("connection-dot");
const agentCount   = document.getElementById("agent-count");
const roomCount    = document.getElementById("room-count");
const agentsList   = document.getElementById("agents-list");
const roomsList    = document.getElementById("rooms-list");
const roomTabs     = document.getElementById("room-tabs");
const messagesEl   = document.getElementById("messages");
const composeSender  = document.getElementById("compose-sender");
const composeInput   = document.getElementById("compose-input");
const composeSend    = document.getElementById("compose-send");
const todoEl         = document.getElementById("tasks-todo");
const inprogressEl   = document.getElementById("tasks-inprogress");
const doneEl         = document.getElementById("tasks-done");
const refreshBtn     = document.getElementById("refresh-btn");
const newRoomBtn     = document.getElementById("new-room-btn");
const newTaskBtn     = document.getElementById("new-task-btn");
const tokenBtn       = document.getElementById("token-btn");

// ── Helpers ───────────────────────────────────────────────────────────────
function relTime(ts) {
  const s = Math.floor((Date.now() - ts) / 1000);
  if (s < 60) return `${s}s ago`;
  if (s < 3600) return `${Math.floor(s/60)}m ago`;
  return `${Math.floor(s/3600)}h ago`;
}

function initials(name) {
  return name.split(/[-_]/).map(p => p[0]).join("").slice(0,2).toUpperCase();
}

// ── Render: agents ────────────────────────────────────────────────────────
function renderAgents() {
  agentCount.textContent = agents.length;
  agentsList.innerHTML = "";
  if (!agents.length) {
    agentsList.innerHTML = '<div style="color:var(--muted);font-size:0.75rem;padding:4px 8px">No agents online</div>';
    return;
  }
  agents.forEach(a => {
    const el = document.createElement("div");
    el.className = "agent-item";
    el.innerHTML = `
      <div class="agent-dot ${a.status}"></div>
      <div class="agent-info">
        <div class="agent-name">${a.name}</div>
        <div class="agent-role">${a.role}${a.status !== "online" ? ` · ${a.status}` : ""}</div>
        ${a.working_on ? `<div class="agent-working">▶ ${a.working_on}</div>` : ""}
      </div>
    `;
    agentsList.appendChild(el);
  });
}

// ── Render: rooms (sidebar + tabs) ────────────────────────────────────────
function renderRooms() {
  roomCount.textContent = rooms.length;

  roomsList.innerHTML = "";
  roomTabs.innerHTML = "";

  rooms.forEach(r => {
    const item = document.createElement("div");
    item.className = "room-item" + (r.id === activeRoomId ? " active" : "");
    item.textContent = r.name;
    item.onclick = () => switchRoom(r.id);
    roomsList.appendChild(item);

    const tab = document.createElement("div");
    tab.className = "room-tab" + (r.id === activeRoomId ? " active" : "");
    tab.textContent = `# ${r.name}`;
    tab.dataset.roomId = r.id;
    tab.onclick = () => switchRoom(r.id);
    roomTabs.appendChild(tab);
  });
}

function switchRoom(roomId) {
  activeRoomId = roomId;
  lastMsgId = null;
  messages = [];
  renderRooms();
  renderMessages();
  loadMessages();
}

// ── Render: messages ──────────────────────────────────────────────────────
function renderMessages() {
  messagesEl.innerHTML = "";
  if (!messages.length) return;

  let lastSender = null;
  messages.forEach(m => {
    const grouped = m.sender === lastSender;
    lastSender = m.sender;

    const el = document.createElement("div");
    el.className = "msg";

    const dest = m.receiver ? `→ ${m.receiver}` : "";
    const typeLabel = m.message_type !== "discussion"
      ? `<div class="msg-type ${m.message_type}">${m.message_type.replace("_", " ")}</div>`
      : "";

    if (grouped) {
      el.innerHTML = `
        <div class="msg-avatar" style="visibility:hidden">${initials(m.sender)}</div>
        <div class="msg-body">
          <div class="msg-content">${escHtml(m.content)}</div>
          ${typeLabel}
        </div>
        <div class="msg-time">${relTime(m.timestamp)}</div>
      `;
    } else {
      el.innerHTML = `
        <div class="msg-avatar">${initials(m.sender)}</div>
        <div class="msg-body">
          <div class="msg-meta">
            <span class="msg-sender">${escHtml(m.sender)}</span>
            ${dest ? `<span class="msg-dest">${escHtml(dest)}</span>` : ""}
            <span class="msg-time">${relTime(m.timestamp)}</span>
          </div>
          <div class="msg-content">${escHtml(m.content)}</div>
          ${typeLabel}
        </div>
      `;
    }
    messagesEl.appendChild(el);
  });
  messagesEl.scrollTop = messagesEl.scrollHeight;
}

function escHtml(s) {
  return String(s)
    .replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;")
    .replace(/"/g,"&quot;");
}

// ── Render: tasks ─────────────────────────────────────────────────────────
function renderTasks() {
  const todo       = tasks.filter(t => t.status === "todo");
  const inprogress = tasks.filter(t => t.status === "in_progress");
  const done       = tasks.filter(t => t.status === "done" || t.status === "blocked");

  [
    [todoEl,       todo],
    [inprogressEl, inprogress],
    [doneEl,       done],
  ].forEach(([el, list]) => {
    el.innerHTML = "";
    if (!list.length) {
      el.innerHTML = '<div class="task-empty">—</div>';
      return;
    }
    list.forEach(t => {
      const card = document.createElement("div");
      card.className = "task-card";
      card.innerHTML = `
        <div class="task-card-title">${escHtml(t.title)}</div>
        <div class="task-card-meta">${t.assignedTo ? `@${t.assignedTo}` : "unassigned"} · TASK-${t.id.slice(0,6)}</div>
      `;
      card.onclick = () => showTaskActions(t);
      el.appendChild(card);
    });
  });
}

function showTaskActions(task) {
  const next = task.status === "todo" ? "in_progress"
    : task.status === "in_progress" ? "done"
    : null;
  if (!next) return;
  const label = next === "in_progress" ? "Start" : "Mark done";
  if (!confirm(`${label}: "${task.title}"?`)) return;
  patch(`/api/tasks/${task.id}`, { status: next })
    .then(() => loadAll())
    .catch(err => alert(err.message));
}

// ── Connection status ─────────────────────────────────────────────────────
function setOnline(ok) {
  connDot.className = "status-dot " + (ok ? "online" : "offline");
  connDot.title = ok ? "Connected" : "Offline";
}

// ── Data loaders ──────────────────────────────────────────────────────────
async function loadMessages() {
  if (!activeRoomId) return;
  const threadId = `room-${activeRoomId}`;
  const ctx = await api(`/api/threads/${threadId}`);
  messages = ctx.messages ?? [];
  if (messages.length) lastMsgId = messages[messages.length - 1].id;
  renderMessages();
}

async function loadAll() {
  try {
    [agents, rooms, tasks] = await Promise.all([
      api("/api/agents"),
      api("/api/rooms"),
      api("/api/tasks"),
    ]);
    setOnline(true);

    if (!activeRoomId && rooms.length) activeRoomId = rooms[0].id;

    renderAgents();
    renderRooms();
    renderTasks();
    await loadMessages();
  } catch {
    setOnline(false);
  }
}

// ── Poll for new messages every 5s ────────────────────────────────────────
async function pollMessages() {
  if (!activeRoomId) return;
  try {
    const threadId = `room-${activeRoomId}`;
    const ctx = await api(`/api/threads/${threadId}`);
    const newMsgs = ctx.messages ?? [];
    if (newMsgs.length !== messages.length) {
      messages = newMsgs;
      renderMessages();
    }
  } catch { /* silent */ }
}

// ── Send message ──────────────────────────────────────────────────────────
async function sendMessage() {
  const content = composeInput.value.trim();
  const sender  = composeSender.value.trim() || "daniel";
  if (!content || !activeRoomId) return;

  composeInput.value = "";
  composeInput.disabled = true;

  try {
    const msg = await post(`/api/rooms/${activeRoomId}/message`, { sender, content });
    messages.push(msg);
    renderMessages();
    sessionStorage.setItem(SENDER_KEY, sender);
  } catch (err) {
    alert(err.message);
    composeInput.value = content;
  } finally {
    composeInput.disabled = false;
    composeInput.focus();
  }
}

// ── Event listeners ───────────────────────────────────────────────────────
composeSend.addEventListener("click", sendMessage);
composeInput.addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) {
    e.preventDefault();
    sendMessage();
  }
});

refreshBtn.addEventListener("click", loadAll);

tokenBtn.addEventListener("click", async () => {
  clearToken();
  await ensureToken();
  loadAll();
});

newRoomBtn.addEventListener("click", async () => {
  const name = prompt("Room name?");
  if (!name) return;
  try {
    await post("/api/rooms", { name });
    await loadAll();
  } catch (err) {
    alert(err.message);
  }
});

newTaskBtn.addEventListener("click", async () => {
  const title = prompt("Task title?");
  if (!title) return;
  const assignedTo = prompt("Assign to agent? (leave blank for unassigned)") || undefined;
  try {
    await post("/api/tasks", { title, assignedTo });
    await loadAll();
  } catch (err) {
    alert(err.message);
  }
});

// ── Boot ──────────────────────────────────────────────────────────────────
(async () => {
  await ensureToken();

  // restore sender from last session
  const savedSender = sessionStorage.getItem(SENDER_KEY);
  if (savedSender) composeSender.value = savedSender;

  await loadAll();

  // poll every 5s for messages, every 15s for agents+tasks
  setInterval(pollMessages, 5_000);
  setInterval(loadAll, 15_000);
})();
