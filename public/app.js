// ══════════════════════════════════════════════════════════════════
//  THEME
// ══════════════════════════════════════════════════════════════════
const THEME_KEY = "ab_theme";
const DEFAULT_THEME = "deep-dev-dark";

function applyTheme(theme) {
  document.documentElement.setAttribute("data-theme", theme);
  localStorage.setItem(THEME_KEY, theme);
  document.querySelectorAll(".theme-btn").forEach(btn => {
    btn.classList.toggle("active", btn.dataset.themeVal === theme);
  });
}

document.querySelectorAll(".theme-btn").forEach(btn => {
  btn.addEventListener("click", () => applyTheme(btn.dataset.themeVal));
});

applyTheme(localStorage.getItem(THEME_KEY) ?? DEFAULT_THEME);

// ══════════════════════════════════════════════════════════════════
//  IDENTITY  (name + token, set once at auth time)
// ══════════════════════════════════════════════════════════════════
const NAME_KEY  = "ab_name";
const TOKEN_KEY = "ab_token";
const DM_KEY    = "ab_dms";

let myName = sessionStorage.getItem(NAME_KEY) ?? "";

const getToken   = () => sessionStorage.getItem(TOKEN_KEY) ?? "";
const setToken   = t  => sessionStorage.setItem(TOKEN_KEY, t);
const clearToken = () => sessionStorage.removeItem(TOKEN_KEY);

function setIdentity(name, token) {
  myName = name;
  sessionStorage.setItem(NAME_KEY, name);
  setToken(token);
  renderIdentity();
}

function renderIdentity() {
  const topbarId = document.getElementById("topbar-identity");
  const sidebarId = document.getElementById("my-identity-sidebar");
  if (topbarId)  topbarId.textContent  = myName ? `@${myName}` : "";
  if (sidebarId && myName) {
    sidebarId.innerHTML = `<span class="my-identity-dot"></span>@${myName}`;
  }
}

// ══════════════════════════════════════════════════════════════════
//  AUTH MODAL
// ══════════════════════════════════════════════════════════════════
const overlay     = document.getElementById("auth-overlay");
const nameInput   = document.getElementById("name-input");
const tokenInput  = document.getElementById("token-input");
const tokenSubmit = document.getElementById("token-submit");

function showAuthModal() {
  return new Promise(resolve => {
    overlay.classList.remove("hidden");
    nameInput.value  = myName || "";
    tokenInput.value = "";
    requestAnimationFrame(() => (myName ? tokenInput : nameInput).focus());

    const submit = () => {
      const name  = nameInput.value.trim();
      const token = tokenInput.value.trim();
      if (!name) { nameInput.focus(); return; }
      overlay.classList.add("hidden");
      setIdentity(name, token);
      resolve();
    };

    tokenSubmit.onclick = submit;
    const onKey = e => { if (e.key === "Enter") submit(); };
    nameInput.addEventListener("keydown",  onKey);
    tokenInput.addEventListener("keydown", onKey);
  });
}

async function ensureAuth() {
  if (!myName) await showAuthModal();
}

// ══════════════════════════════════════════════════════════════════
//  HTTP
// ══════════════════════════════════════════════════════════════════
async function api(path, opts = {}) {
  const token = getToken();
  const headers = {
    ...(opts.headers ?? {}),
    ...(token ? { Authorization: `Bearer ${token}` } : {}),
  };
  const res = await fetch(path, { ...opts, headers });
  if (res.status === 401) {
    clearToken();
    await showAuthModal();
    return api(path, opts);
  }
  if (!res.ok) throw new Error(`${res.status} ${await res.text().catch(() => "")}`);
  return res.json();
}

const post  = (path, body) => api(path, { method: "POST",  headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
const patch = (path, body) => api(path, { method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });

// ══════════════════════════════════════════════════════════════════
//  STATE
// ══════════════════════════════════════════════════════════════════
let rooms         = [];
let agents        = [];
let tasks         = [];
let messages      = [];
let activeRoomId  = null;
let activeDmAgent = null;   // agent name when in DM mode

// Unread counts: convId → count (in-memory, cleared on switch)
const unread = {};

// ══════════════════════════════════════════════════════════════════
//  DOM
// ══════════════════════════════════════════════════════════════════
const connPill      = document.getElementById("conn-pill");
const connDot       = document.getElementById("connection-dot");
const connLabel     = document.getElementById("conn-label");
const agentCount    = document.getElementById("agent-count");
const roomCount     = document.getElementById("room-count");
const agentsList    = document.getElementById("agents-list");
const roomsList     = document.getElementById("rooms-list");
const dmsList       = document.getElementById("dms-list");
const roomTabs      = document.getElementById("room-tabs");
const feedHeaderRight = document.getElementById("feed-header-right");
const messagesEl    = document.getElementById("messages");
const typingEl      = document.getElementById("typing-indicator");
const composeInput  = document.getElementById("compose-input");
const composeSend   = document.getElementById("compose-send");
const mentionPopup  = document.getElementById("mention-popup");
const slashPopup    = document.getElementById("slash-popup");
const todoEl        = document.getElementById("tasks-todo");
const inprogressEl  = document.getElementById("tasks-inprogress");
const doneEl        = document.getElementById("tasks-done");
const refreshBtn    = document.getElementById("refresh-btn");
const newRoomBtn    = document.getElementById("new-room-btn");
const newDmBtn      = document.getElementById("new-dm-btn");
const newTaskBtn    = document.getElementById("new-task-btn");
const tokenBtn      = document.getElementById("token-btn");
const taskPopover   = document.getElementById("task-popover");
const taskPopTitle  = document.getElementById("task-popover-title");
const taskPopCancel = document.getElementById("task-popover-cancel");
const taskPopConfirm= document.getElementById("task-popover-confirm");
const switcherOverlay = document.getElementById("switcher-overlay");
const switcherInput   = document.getElementById("switcher-input");
const switcherList    = document.getElementById("switcher-list");
const switcherBtn     = document.getElementById("switcher-btn");

// ══════════════════════════════════════════════════════════════════
//  HELPERS
// ══════════════════════════════════════════════════════════════════
function relTime(ts) {
  const s = Math.floor((Date.now() - ts) / 1000);
  if (s < 60)   return `${s}s ago`;
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  return `${Math.floor(s / 3600)}h ago`;
}

function initials(name) {
  return name.split(/[-_]/).map(p => p[0]).join("").slice(0, 2).toUpperCase();
}

// Hash agent name → one of 8 accent colours
const AVATAR_COLORS = [
  "#6366f1", "#06b6d4", "#22c55e", "#f59e0b",
  "#ef4444", "#8b5cf6", "#ec4899", "#14b8a6",
];
function avatarColor(name) {
  let h = 5381;
  for (let i = 0; i < name.length; i++) h = ((h << 5) + h) ^ name.charCodeAt(i);
  return AVATAR_COLORS[Math.abs(h) % AVATAR_COLORS.length];
}

function dmRoomName(a, b) {
  return "dm-" + [a, b].sort().join("~");
}

function getDmPartner(roomName) {
  // roomName = "dm-nameA~nameB"
  const inner = roomName.slice(3); // "nameA~nameB"
  const idx   = inner.indexOf("~");
  if (idx === -1) return null;
  const a = inner.slice(0, idx);
  const b = inner.slice(idx + 1);
  return a === myName ? b : a;
}

function isDmRoom(room) {
  return room.name.startsWith("dm-") && room.name.includes("~");
}

function currentConvId() {
  if (activeDmAgent) {
    const dm = rooms.find(r => r.name === dmRoomName(myName, activeDmAgent));
    return dm ? `room-${dm.id}` : null;
  }
  return activeRoomId ? `room-${activeRoomId}` : null;
}

// ══════════════════════════════════════════════════════════════════
//  UNREAD TRACKING
// ══════════════════════════════════════════════════════════════════
function getLastRead(convId) {
  return parseInt(localStorage.getItem(`ab_read_${convId}`) ?? "0", 10);
}

function markRead(convId) {
  if (!convId) return;
  localStorage.setItem(`ab_read_${convId}`, Date.now().toString());
  delete unread[convId];
}

function addUnread(convId, count = 1) {
  if (!convId) return;
  unread[convId] = (unread[convId] ?? 0) + count;
}

function unreadBadge(count) {
  if (!count) return "";
  return `<span class="badge badge--unread">${count > 9 ? "9+" : count}</span>`;
}

// ══════════════════════════════════════════════════════════════════
//  CONNECTION STATUS
// ══════════════════════════════════════════════════════════════════
function setOnline(ok) {
  connDot.className = "conn-dot " + (ok ? "online" : "offline");
  connLabel.textContent = ok ? "Connected" : "Offline";
  connPill.className = "conn-pill " + (ok ? "online" : "offline");
}

// ══════════════════════════════════════════════════════════════════
//  RENDER: AGENTS
// ══════════════════════════════════════════════════════════════════
function renderAgents() {
  agentCount.textContent = agents.length;
  agentsList.innerHTML = "";
  if (!agents.length) {
    agentsList.innerHTML = '<div class="empty-state">No agents online</div>';
    return;
  }
  agents.forEach(a => {
    const el = document.createElement("div");
    el.className = "agent-item";
    el.title     = `Click to DM ${a.name}`;
    const color  = avatarColor(a.name);
    el.innerHTML = `
      <div class="agent-dot ${a.status}"></div>
      <div class="agent-info">
        <div class="agent-name">${escText(a.name)}</div>
        <div class="agent-role">${escText(a.role || "")}${a.status !== "online" ? ` · ${a.status}` : ""}</div>
        ${a.working_on ? `<div class="agent-working">▶ ${escText(a.working_on)}</div>` : ""}
      </div>
      <div class="agent-dm-hint">DM</div>
    `;
    el.onclick = () => openDm(a.name);
    agentsList.appendChild(el);
  });
}

// ══════════════════════════════════════════════════════════════════
//  RENDER: ROOMS (sidebar + tabs)
// ══════════════════════════════════════════════════════════════════
function renderRooms() {
  const publicRooms = rooms.filter(r => !isDmRoom(r));
  roomCount.textContent = publicRooms.length;

  roomsList.innerHTML = "";
  roomTabs.innerHTML  = "";

  publicRooms.forEach(r => {
    const convId = `room-${r.id}`;
    const isActive = activeRoomId === r.id && !activeDmAgent;
    const cnt = unread[convId] ?? 0;

    // sidebar item
    const item = document.createElement("div");
    item.className = "room-item" + (isActive ? " active" : "");
    item.innerHTML = `<span class="room-name">${escText(r.name)}</span>${unreadBadge(cnt)}`;
    item.onclick   = () => switchRoom(r.id);
    roomsList.appendChild(item);

    // feed tab
    const tab = document.createElement("div");
    tab.className  = "room-tab" + (isActive ? " active" : "");
    tab.textContent = `# ${r.name}`;
    tab.dataset.roomId = r.id;
    tab.onclick    = () => switchRoom(r.id);
    roomTabs.appendChild(tab);
  });
}

// ══════════════════════════════════════════════════════════════════
//  RENDER: DMs
// ══════════════════════════════════════════════════════════════════
function renderDms() {
  const dmRooms = rooms.filter(isDmRoom);
  dmsList.innerHTML = "";

  if (!dmRooms.length) {
    dmsList.innerHTML = '<div class="empty-state">No direct messages</div>';
    return;
  }

  dmRooms.forEach(room => {
    const partner = getDmPartner(room.name);
    if (!partner) return;
    const agent   = agents.find(a => a.name === partner);
    const convId  = `room-${room.id}`;
    const isActive = activeRoomId === room.id && activeDmAgent === partner;
    const cnt     = unread[convId] ?? 0;

    const el = document.createElement("div");
    el.className = "room-item dm-item" + (isActive ? " active" : "");
    el.innerHTML = `
      <span class="dm-status ${agent?.status ?? "offline"}"></span>
      <span class="dm-name">${escText(partner)}</span>
      ${unreadBadge(cnt)}
    `;
    el.onclick = () => { activeDmAgent = partner; switchRoom(room.id); };
    dmsList.appendChild(el);
  });
}

// ══════════════════════════════════════════════════════════════════
//  SWITCH ROOM / OPEN DM
// ══════════════════════════════════════════════════════════════════
function switchRoom(roomId) {
  const prevConvId = currentConvId();
  activeRoomId = roomId;
  if (!activeDmAgent) {
    // only clear DM state when explicitly switching to a non-DM room
  }
  messages = [];
  markRead(currentConvId());
  renderRooms();
  renderDms();
  renderMessages();
  updateFeedHeader();
  loadCurrentConversation();
}

async function openDm(agentName) {
  if (!myName || agentName === myName) return;
  const roomName = dmRoomName(myName, agentName);

  // find or create the DM room
  let room = rooms.find(r => r.name === roomName);
  if (!room) {
    try {
      room = await post("/api/rooms", { name: roomName });
      rooms.push(room);
    } catch {
      // might already exist — reload
      await loadAll();
      room = rooms.find(r => r.name === roomName);
    }
  }
  if (!room) return;

  activeDmAgent = agentName;
  switchRoom(room.id);
}

function updateFeedHeader() {
  if (activeDmAgent) {
    const agent = agents.find(a => a.name === activeDmAgent);
    feedHeaderRight.textContent = activeDmAgent + (agent?.status ? ` · ${agent.status}` : "");
  } else {
    feedHeaderRight.textContent = "";
  }
}

// ══════════════════════════════════════════════════════════════════
//  RENDER: MESSAGES  (XSS-safe mention tokenizer)
// ══════════════════════════════════════════════════════════════════
function escText(s) {
  const el = document.createElement("span");
  el.textContent = String(s);
  return el.innerHTML;
}

function renderMessageContent(content) {
  // Split on @mention tokens — safe: never uses innerHTML for content
  const parts = String(content).split(/(@\S+)/g);
  const wrap  = document.createElement("div");
  wrap.className = "msg-content";

  let mentionedMe = false;
  let hasBroadcast = false;

  parts.forEach(part => {
    if (/^@\S+$/.test(part)) {
      const name       = part.slice(1);
      const isBroadcast = name === "everyone";
      const isMe       = name === myName;
      if (isMe)        mentionedMe   = true;
      if (isBroadcast) hasBroadcast  = true;

      const span = document.createElement("span");
      span.className   = `msg-mention ${isBroadcast ? "msg-mention--broadcast" : "msg-mention--targeted"}`;
      span.dataset.agent = name;
      span.textContent = part;
      wrap.appendChild(span);
    } else {
      wrap.appendChild(document.createTextNode(part));
    }
  });

  return { el: wrap, mentionedMe, hasBroadcast };
}

function renderMessages() {
  messagesEl.innerHTML = "";
  if (!messages.length) {
    messagesEl.innerHTML = '<div class="empty-state" style="text-align:center;padding:32px 0">No messages yet</div>';
    return;
  }

  let lastSender = null;
  let lastDate   = null;

  messages.forEach(m => {
    // Day divider
    const msgDate = new Date(m.timestamp).toDateString();
    if (msgDate !== lastDate) {
      lastDate = msgDate;
      const div = document.createElement("div");
      div.className   = "msg-day-divider";
      div.textContent = msgDate === new Date().toDateString() ? "Today" : msgDate;
      messagesEl.appendChild(div);
      lastSender = null; // reset grouping across day dividers
    }

    const grouped = m.sender === lastSender;
    lastSender = m.sender;

    const el    = document.createElement("div");
    el.className = "msg";

    const { el: contentEl, mentionedMe, hasBroadcast } = renderMessageContent(m.content);
    if (mentionedMe)  el.classList.add("msg--mentioned-me");
    if (hasBroadcast) el.classList.add("msg--mentioned-broadcast");

    const color  = avatarColor(m.sender);
    const typeLabel = m.message_type && m.message_type !== "discussion"
      ? `<div class="msg-type ${m.message_type}">${m.message_type.replace(/_/g, " ")}</div>` : "";

    if (grouped) {
      const avatarPlaceholder = document.createElement("div");
      avatarPlaceholder.className = "msg-avatar";
      avatarPlaceholder.style.visibility = "hidden";
      avatarPlaceholder.textContent = initials(m.sender);

      const body = document.createElement("div");
      body.className = "msg-body";
      body.appendChild(contentEl);
      if (typeLabel) body.insertAdjacentHTML("beforeend", typeLabel);

      const timeEl = document.createElement("div");
      timeEl.className   = "msg-time";
      timeEl.textContent = relTime(m.timestamp);

      el.appendChild(avatarPlaceholder);
      el.appendChild(body);
      el.appendChild(timeEl);
    } else {
      const avatarEl = document.createElement("div");
      avatarEl.className   = "msg-avatar";
      avatarEl.textContent = initials(m.sender);
      avatarEl.style.background   = color + "22";
      avatarEl.style.borderColor  = color + "55";
      avatarEl.style.color        = color;

      const destText = m.receiver ? `→ ${m.receiver}` : "";
      const meta = document.createElement("div");
      meta.className = "msg-meta";
      meta.innerHTML = `
        <span class="msg-sender" style="color:${color}">${escText(m.sender)}</span>
        ${destText ? `<span class="msg-dest">${escText(destText)}</span>` : ""}
        <span class="msg-time">${relTime(m.timestamp)}</span>
      `;

      const body = document.createElement("div");
      body.className = "msg-body";
      body.appendChild(meta);
      body.appendChild(contentEl);
      if (typeLabel) body.insertAdjacentHTML("beforeend", typeLabel);

      el.appendChild(avatarEl);
      el.appendChild(body);
    }

    messagesEl.appendChild(el);
  });

  messagesEl.scrollTop = messagesEl.scrollHeight;
}

// ══════════════════════════════════════════════════════════════════
//  RENDER: TASKS  (inline popover, no confirm())
// ══════════════════════════════════════════════════════════════════
let pendingTask = null;

function renderTasks() {
  const todo       = tasks.filter(t => t.status === "todo");
  const inprogress = tasks.filter(t => t.status === "in_progress");
  const done       = tasks.filter(t => t.status === "done" || t.status === "blocked");

  [[todoEl, todo], [inprogressEl, inprogress], [doneEl, done]].forEach(([el, list]) => {
    el.innerHTML = "";
    if (!list.length) { el.innerHTML = '<div class="task-empty">—</div>'; return; }
    list.forEach(t => {
      const card = document.createElement("div");
      card.className = "task-card";
      card.innerHTML = `
        <div class="task-card-title">${escText(t.title)}</div>
        <div class="task-card-meta">${t.assignedTo ? `@${t.assignedTo}` : "unassigned"} · TASK-${t.id.slice(0, 6)}</div>
      `;
      card.onclick = e => showTaskPopover(t, card, e);
      el.appendChild(card);
    });
  });
}

function showTaskPopover(task, cardEl, e) {
  e.stopPropagation();
  const next = task.status === "todo" ? "in_progress"
    : task.status === "in_progress" ? "done" : null;
  if (!next) return;

  pendingTask = { task, next };
  taskPopTitle.textContent   = `"${task.title}"`;
  taskPopConfirm.textContent = next === "in_progress" ? "▶ Start" : "✓ Done";

  // Position below card
  const rect = cardEl.getBoundingClientRect();
  const popH = 90;
  const top  = rect.bottom + 6 + popH > window.innerHeight
    ? rect.top - popH - 6 : rect.bottom + 6;
  taskPopover.style.top  = `${top}px`;
  taskPopover.style.left = `${Math.min(rect.left, window.innerWidth - 270)}px`;
  taskPopover.classList.remove("hidden");
}

taskPopCancel.addEventListener("click", () => {
  taskPopover.classList.add("hidden");
  pendingTask = null;
});
taskPopConfirm.addEventListener("click", () => {
  if (!pendingTask) return;
  const { task, next } = pendingTask;
  taskPopover.classList.add("hidden");
  pendingTask = null;
  patch(`/api/tasks/${task.id}`, { status: next })
    .then(() => loadAll())
    .catch(() => {});
});
document.addEventListener("click", e => {
  if (!taskPopover.classList.contains("hidden") && !taskPopover.contains(e.target)) {
    taskPopover.classList.add("hidden");
    pendingTask = null;
  }
});

// ══════════════════════════════════════════════════════════════════
//  @MENTION POPUP
// ══════════════════════════════════════════════════════════════════
let mentionState = { active: false, query: "", idx: 0 };

function getMentionItems(query) {
  const q    = query.toLowerCase();
  const all  = [
    { name: "everyone", role: "Broadcast all",  status: "broadcast", special: true },
    ...agents.map(a => ({ name: a.name, role: a.role || "", status: a.status, special: false })),
  ];
  return q ? all.filter(i => i.name.toLowerCase().startsWith(q)) : all;
}

function openMentionPopup(query) {
  mentionState = { active: true, query, idx: 0 };
  renderMentionPopup();
}

function closeMentionPopup() {
  mentionState.active = false;
  mentionPopup.classList.add("hidden");
}

function renderMentionPopup() {
  const items = getMentionItems(mentionState.query);
  if (!items.length) { closeMentionPopup(); return; }

  mentionPopup.innerHTML = "";
  mentionPopup.classList.remove("hidden");

  items.forEach((item, i) => {
    const el = document.createElement("div");
    el.className   = "mention-popup-item" + (i === mentionState.idx ? " selected" : "") + (item.special ? " mention-everyone" : "");
    el.setAttribute("role", "option");
    el.setAttribute("aria-selected", String(i === mentionState.idx));

    const dot  = document.createElement("span");
    dot.className = `mention-popup-dot ${item.status}`;

    const name = document.createElement("span");
    name.className   = "mention-name";
    name.textContent = `@${item.name}`;

    const role = document.createElement("span");
    role.className   = "mention-role";
    role.textContent = item.role;

    el.appendChild(dot);
    el.appendChild(name);
    el.appendChild(role);

    el.addEventListener("mousedown", ev => { ev.preventDefault(); insertMention(item.name); });
    mentionPopup.appendChild(el);
  });
}

function insertMention(name) {
  const ta     = composeInput;
  const val    = ta.value;
  const cursor = ta.selectionStart;
  const before = val.substring(0, cursor);
  const atIdx  = before.lastIndexOf("@");
  if (atIdx === -1) return;
  const after   = val.substring(cursor);
  ta.value      = before.substring(0, atIdx) + `@${name} ` + after;
  const pos     = atIdx + name.length + 2;
  ta.setSelectionRange(pos, pos);
  closeMentionPopup();
  ta.focus();
  autoResize(ta);
}

// ══════════════════════════════════════════════════════════════════
//  SLASH COMMAND POPUP
// ══════════════════════════════════════════════════════════════════
const SLASH_COMMANDS = [
  { cmd: "/task",   desc: "Create a task",        usage: "/task <title>" },
  { cmd: "/assign", desc: "Assign task to agent", usage: "/assign TASK-id @agent" },
  { cmd: "/done",   desc: "Mark task done",       usage: "/done TASK-id" },
  { cmd: "/dm",     desc: "Open direct message",  usage: "/dm @agent" },
];

let slashState = { active: false, query: "", idx: 0 };

function getSlashItems(query) {
  const q = query.toLowerCase();
  return SLASH_COMMANDS.filter(c => c.cmd.slice(1).startsWith(q));
}

function openSlashPopup(query) {
  slashState = { active: true, query, idx: 0 };
  renderSlashPopup();
}

function closeSlashPopup() {
  slashState.active = false;
  slashPopup.classList.add("hidden");
}

function renderSlashPopup() {
  const items = getSlashItems(slashState.query);
  if (!items.length) { closeSlashPopup(); return; }

  slashPopup.innerHTML = "";
  slashPopup.classList.remove("hidden");

  items.forEach((item, i) => {
    const el = document.createElement("div");
    el.className = "slash-item" + (i === slashState.idx ? " selected" : "");

    const cmd  = document.createElement("span");
    cmd.className   = "slash-cmd";
    cmd.textContent = item.cmd;

    const desc = document.createElement("span");
    desc.className   = "slash-desc";
    desc.textContent = item.usage;

    el.appendChild(cmd);
    el.appendChild(desc);
    el.addEventListener("mousedown", ev => {
      ev.preventDefault();
      composeInput.value = item.cmd + " ";
      composeInput.focus();
      closeSlashPopup();
    });
    slashPopup.appendChild(el);
  });
}

// ══════════════════════════════════════════════════════════════════
//  AUTO-RESIZE TEXTAREA
// ══════════════════════════════════════════════════════════════════
function autoResize(ta) {
  ta.style.height = "auto";
  ta.style.height = Math.min(ta.scrollHeight, 120) + "px";
}

// ══════════════════════════════════════════════════════════════════
//  COMPOSE: input event handler
// ══════════════════════════════════════════════════════════════════
composeInput.addEventListener("input", () => {
  autoResize(composeInput);

  const val    = composeInput.value;
  const cursor = composeInput.selectionStart;
  const before = val.substring(0, cursor);

  // @mention detection: find @ not followed by space in current word
  const mentionMatch = before.match(/@(\w*)$/);
  if (mentionMatch) {
    openMentionPopup(mentionMatch[1]);
    closeSlashPopup();
    return;
  }
  closeMentionPopup();

  // Slash command: only at line start
  const lineStart  = before.lastIndexOf("\n") + 1;
  const currentLine = before.substring(lineStart);
  const slashMatch  = currentLine.match(/^\/(\w*)$/);
  if (slashMatch) {
    openSlashPopup(slashMatch[1]);
    return;
  }
  closeSlashPopup();
});

composeInput.addEventListener("keydown", e => {
  // Mention popup navigation
  if (mentionState.active) {
    const items = getMentionItems(mentionState.query);
    if (e.key === "ArrowDown") {
      e.preventDefault();
      mentionState.idx = (mentionState.idx + 1) % items.length;
      renderMentionPopup();
      return;
    }
    if (e.key === "ArrowUp") {
      e.preventDefault();
      mentionState.idx = (mentionState.idx - 1 + items.length) % items.length;
      renderMentionPopup();
      return;
    }
    if (e.key === "Enter" || e.key === "Tab") {
      e.preventDefault();
      if (items[mentionState.idx]) insertMention(items[mentionState.idx].name);
      return;
    }
    if (e.key === "Escape") { e.preventDefault(); closeMentionPopup(); return; }
  }

  // Slash popup navigation
  if (slashState.active) {
    const items = getSlashItems(slashState.query);
    if (e.key === "ArrowDown") {
      e.preventDefault();
      slashState.idx = (slashState.idx + 1) % items.length;
      renderSlashPopup();
      return;
    }
    if (e.key === "ArrowUp") {
      e.preventDefault();
      slashState.idx = (slashState.idx - 1 + items.length) % items.length;
      renderSlashPopup();
      return;
    }
    if (e.key === "Enter" || e.key === "Tab") {
      e.preventDefault();
      const item = items[slashState.idx];
      if (item) { composeInput.value = item.cmd + " "; closeSlashPopup(); }
      return;
    }
    if (e.key === "Escape") { e.preventDefault(); closeSlashPopup(); return; }
  }

  // Send on Enter (no Shift)
  if (e.key === "Enter" && !e.shiftKey) {
    e.preventDefault();
    sendMessage();
  }
});

// ══════════════════════════════════════════════════════════════════
//  SLASH COMMAND HANDLER
// ══════════════════════════════════════════════════════════════════
async function handleSlashCommand(raw) {
  const parts = raw.trim().split(/\s+/);
  const cmd   = parts[0].toLowerCase();

  if (cmd === "/task") {
    const title = parts.slice(1).join(" ");
    if (!title) { showToast("Usage: /task <title>"); return; }
    const task = await post("/api/tasks", { title, assignedTo: undefined });
    showToast(`Task created: TASK-${task.id.slice(0, 6)} — ${task.title}`);
    await loadAll();
    return;
  }

  if (cmd === "/assign") {
    // /assign TASK-abc123 @agent
    const taskFragment = parts[1];
    const agentMention = parts[2];
    if (!taskFragment || !agentMention) { showToast("Usage: /assign TASK-id @agent"); return; }
    const agentName = agentMention.replace(/^@/, "");
    const shortId   = taskFragment.replace(/^TASK-/i, "");
    const found     = tasks.find(t => t.id.startsWith(shortId));
    if (!found) { showToast(`Task not found: ${taskFragment}`); return; }
    await patch(`/api/tasks/${found.id}`, { assigned_to: agentName });
    showToast(`TASK-${found.id.slice(0, 6)} → @${agentName}`);
    await loadAll();
    return;
  }

  if (cmd === "/done") {
    const taskFragment = parts[1];
    if (!taskFragment) { showToast("Usage: /done TASK-id"); return; }
    const shortId = taskFragment.replace(/^TASK-/i, "");
    const found   = tasks.find(t => t.id.startsWith(shortId));
    if (!found) { showToast(`Task not found: ${taskFragment}`); return; }
    await patch(`/api/tasks/${found.id}`, { status: "done" });
    showToast(`TASK-${found.id.slice(0, 6)} → done ✓`);
    await loadAll();
    return;
  }

  if (cmd === "/dm") {
    const target = (parts[1] ?? "").replace(/^@/, "");
    if (!target) { showToast("Usage: /dm @agentname"); return; }
    await openDm(target);
    return;
  }

  showToast(`Unknown command: ${cmd}`);
}

// ══════════════════════════════════════════════════════════════════
//  TOAST (lightweight, no browser alert())
// ══════════════════════════════════════════════════════════════════
function showToast(msg) {
  let toast = document.getElementById("ab-toast");
  if (!toast) {
    toast = document.createElement("div");
    toast.id = "ab-toast";
    Object.assign(toast.style, {
      position: "fixed", bottom: "20px", left: "50%", transform: "translateX(-50%)",
      background: "var(--bg-elevated)", color: "var(--text-1)",
      border: "1px solid var(--border-strong)", borderRadius: "var(--radius-lg)",
      padding: "8px 16px", fontSize: "var(--text-sm)", zIndex: "9999",
      boxShadow: "var(--shadow-md)", fontFamily: "var(--font-ui)",
      transition: "opacity 200ms", maxWidth: "360px", textAlign: "center",
    });
    document.body.appendChild(toast);
  }
  toast.textContent = msg;
  toast.style.opacity = "1";
  clearTimeout(toast._t);
  toast._t = setTimeout(() => { toast.style.opacity = "0"; }, 2800);
}

// ══════════════════════════════════════════════════════════════════
//  SEND MESSAGE
// ══════════════════════════════════════════════════════════════════
async function sendMessage() {
  const content = composeInput.value.trim();
  if (!content) return;

  const convId = currentConvId();
  if (!convId && !activeRoomId) { showToast("Select a room or DM first"); return; }

  // Slash commands
  if (content.startsWith("/")) {
    composeInput.value = "";
    autoResize(composeInput);
    try { await handleSlashCommand(content); } catch (err) { showToast(err.message); }
    return;
  }

  // Determine receiver
  let receiver = null;
  if (activeDmAgent) {
    receiver = activeDmAgent;
  } else {
    // extract first @name that isn't @everyone
    const m = content.match(/@(\S+)/);
    if (m && m[1] !== "everyone") receiver = m[1];
  }

  const roomId = activeRoomId;
  composeInput.value = "";
  autoResize(composeInput);
  composeInput.disabled = true;

  try {
    const payload = { sender: myName, content };
    if (receiver) payload.receiver = receiver;
    const msg = await post(`/api/rooms/${roomId}/message`, payload);
    messages.push(msg);
    renderMessages();
  } catch (err) {
    showToast(err.message);
    composeInput.value = content;
    autoResize(composeInput);
  } finally {
    composeInput.disabled = false;
    composeInput.focus();
  }
}

// ══════════════════════════════════════════════════════════════════
//  DATA LOADERS
// ══════════════════════════════════════════════════════════════════
async function loadCurrentConversation() {
  if (!activeRoomId) return;
  const threadId = `room-${activeRoomId}`;
  try {
    const ctx  = await api(`/api/threads/${threadId}`);
    messages   = ctx.messages ?? [];
    renderMessages();
  } catch { /* silent */ }
}

async function pollMessages() {
  if (!activeRoomId) return;
  try {
    const threadId = `room-${activeRoomId}`;
    const ctx      = await api(`/api/threads/${threadId}`);
    const newMsgs  = ctx.messages ?? [];
    if (newMsgs.length !== messages.length) {
      // count new messages not from me and mark unread for other convs
      const newOnes = newMsgs.slice(messages.length);
      const convId  = currentConvId();
      messages = newMsgs;
      renderMessages();
      markRead(convId);
    }
  } catch { /* silent */ }
}

async function loadAll() {
  try {
    [agents, rooms, tasks] = await Promise.all([
      api("/api/agents"),
      api("/api/rooms"),
      api("/api/tasks"),
    ]);
    setOnline(true);

    // Auto-select first public room if nothing active
    if (!activeRoomId && !activeDmAgent) {
      const first = rooms.find(r => !isDmRoom(r));
      if (first) { activeRoomId = first.id; markRead(`room-${first.id}`); }
    }

    renderAgents();
    renderRooms();
    renderDms();
    renderTasks();
    renderIdentity();
    updateFeedHeader();
    await loadCurrentConversation();
  } catch {
    setOnline(false);
  }
}

// ══════════════════════════════════════════════════════════════════
//  CMD+K SWITCHER
// ══════════════════════════════════════════════════════════════════
let switcherSelectedIdx = 0;

function openSwitcher() {
  switcherOverlay.classList.remove("hidden");
  switcherInput.value = "";
  switcherSelectedIdx = 0;
  renderSwitcherList("");
  requestAnimationFrame(() => switcherInput.focus());
}

function closeSwitcher() {
  switcherOverlay.classList.add("hidden");
}

function getSwitcherItems(query) {
  const q    = query.toLowerCase();
  const publicRooms = rooms.filter(r => !isDmRoom(r));
  const dmRoomsList = rooms.filter(isDmRoom);

  return [
    ...publicRooms.map(r => ({ type: "room", id: r.id, label: r.name, icon: "#" })),
    ...dmRoomsList.map(r => {
      const partner = getDmPartner(r.name);
      return { type: "dm", id: r.id, name: partner, label: partner ?? r.name, icon: "@" };
    }),
    ...agents.filter(a => a.name !== myName).map(a => ({
      type: "dm-new", name: a.name, label: a.name, icon: "✉", role: a.role,
    })),
  ].filter(i => !q || i.label.toLowerCase().includes(q));
}

function renderSwitcherList(query) {
  const items = getSwitcherItems(query);
  switcherList.innerHTML = "";

  if (!items.length) {
    switcherList.innerHTML = '<div class="switcher-empty">No matches</div>';
    return;
  }

  items.forEach((item, i) => {
    const el = document.createElement("div");
    el.className = "switcher-item" + (i === switcherSelectedIdx ? " selected" : "");

    const icon = document.createElement("span");
    icon.className   = "switcher-item-icon";
    icon.textContent = item.icon;

    el.appendChild(icon);
    el.appendChild(document.createTextNode(item.label));

    el.onclick = () => {
      closeSwitcher();
      if (item.type === "room")   { activeDmAgent = null; switchRoom(item.id); }
      else if (item.type === "dm") { activeDmAgent = item.name; switchRoom(item.id); }
      else if (item.type === "dm-new") { openDm(item.name); }
    };
    switcherList.appendChild(el);
  });
}

switcherInput.addEventListener("input", () => {
  switcherSelectedIdx = 0;
  renderSwitcherList(switcherInput.value);
});

switcherInput.addEventListener("keydown", e => {
  const items = getSwitcherItems(switcherInput.value);
  if (e.key === "ArrowDown") {
    e.preventDefault();
    switcherSelectedIdx = Math.min(switcherSelectedIdx + 1, items.length - 1);
    renderSwitcherList(switcherInput.value);
  } else if (e.key === "ArrowUp") {
    e.preventDefault();
    switcherSelectedIdx = Math.max(switcherSelectedIdx - 1, 0);
    renderSwitcherList(switcherInput.value);
  } else if (e.key === "Enter") {
    const el = switcherList.querySelector(".switcher-item.selected");
    if (el) el.click();
  } else if (e.key === "Escape") {
    closeSwitcher();
  }
});

switcherOverlay.addEventListener("click", e => {
  if (e.target === switcherOverlay) closeSwitcher();
});

// ══════════════════════════════════════════════════════════════════
//  EVENT LISTENERS
// ══════════════════════════════════════════════════════════════════
composeSend.addEventListener("click", sendMessage);

refreshBtn.addEventListener("click", loadAll);

tokenBtn.addEventListener("click", async () => {
  clearToken();
  sessionStorage.removeItem(NAME_KEY);
  myName = "";
  await showAuthModal();
  loadAll();
});

newRoomBtn.addEventListener("click", async () => {
  const name = prompt("Room name?");
  if (!name) return;
  try { await post("/api/rooms", { name }); await loadAll(); }
  catch (err) { showToast(err.message); }
});

newDmBtn.addEventListener("click", async () => {
  const name = prompt("Agent name to DM?");
  if (!name) return;
  await openDm(name.replace(/^@/, ""));
});

newTaskBtn.addEventListener("click", async () => {
  const title = prompt("Task title?");
  if (!title) return;
  try { await post("/api/tasks", { title }); await loadAll(); }
  catch (err) { showToast(err.message); }
});

switcherBtn.addEventListener("click", openSwitcher);

// Global keyboard shortcuts
document.addEventListener("keydown", e => {
  // Cmd/Ctrl+K → switcher
  if ((e.metaKey || e.ctrlKey) && e.key === "k") {
    e.preventDefault();
    openSwitcher();
    return;
  }
  // Escape → close popups
  if (e.key === "Escape") {
    closeMentionPopup();
    closeSlashPopup();
    closeSwitcher();
    taskPopover.classList.add("hidden");
    pendingTask = null;
  }
});

// ══════════════════════════════════════════════════════════════════
//  BOOT
// ══════════════════════════════════════════════════════════════════
(async () => {
  await ensureAuth();
  await loadAll();
  renderIdentity();

  // Poll messages every 4s, full refresh every 15s
  setInterval(pollMessages, 4_000);
  setInterval(loadAll, 15_000);
})();
