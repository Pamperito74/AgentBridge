// ── Auth ──────────────────────────────────────────────────────────────────

const TOKEN_KEY = "agentbridge_token";

function getToken() {
  return sessionStorage.getItem(TOKEN_KEY) ?? "";
}

function setToken(t) {
  sessionStorage.setItem(TOKEN_KEY, t);
}

function clearToken() {
  sessionStorage.removeItem(TOKEN_KEY);
}

function showAuthModal() {
  return new Promise((resolve) => {
    const overlay = document.createElement("div");
    overlay.id = "auth-overlay";
    overlay.style.cssText =
      "position:fixed;inset:0;background:rgba(0,0,0,0.75);display:flex;align-items:center;justify-content:center;z-index:9999";

    const dialog = document.createElement("div");
    dialog.style.cssText =
      "background:#1a1d24;border:1px solid #3c424f;border-radius:12px;padding:2rem;min-width:360px;display:flex;flex-direction:column;gap:1rem";

    dialog.innerHTML = `
      <h2 style="margin:0;font-family:'Fraunces',serif;color:#f7f3ea">AgentBridge Auth</h2>
      <p style="margin:0;color:#a0a6b0;font-size:0.875rem">Enter your AGENTBRIDGE_TOKEN to connect.</p>
      <input id="token-input" type="password" placeholder="Bearer token…"
        style="padding:0.75rem 1rem;border-radius:8px;border:1px solid #3c424f;background:#0c0e12;color:#f7f3ea;font-size:0.875rem;outline:none" />
      <button id="token-submit"
        style="padding:0.75rem 1rem;border-radius:8px;border:none;background:#f6b73c;color:#0c0e12;font-weight:700;cursor:pointer;font-size:0.875rem">
        Connect
      </button>
    `;

    overlay.appendChild(dialog);
    document.body.appendChild(overlay);

    const input = dialog.querySelector("#token-input");
    const btn = dialog.querySelector("#token-submit");

    const submit = () => {
      const val = input.value.trim();
      if (!val) return;
      setToken(val);
      overlay.remove();
      resolve(val);
    };

    btn.addEventListener("click", submit);
    input.addEventListener("keydown", (e) => { if (e.key === "Enter") submit(); });
    requestAnimationFrame(() => input.focus());
  });
}

async function ensureToken() {
  let token = getToken();
  if (!token) {
    token = await showAuthModal();
  }
  return token;
}

// ── HTTP helper ───────────────────────────────────────────────────────────

async function fetchJSON(path, options = {}) {
  const token = getToken();
  const headers = {
    ...(options.headers ?? {}),
    ...(token ? { Authorization: `Bearer ${token}` } : {}),
  };
  const res = await fetch(path, { ...options, headers });
  if (res.status === 401) {
    clearToken();
    setStatus("Auth error — re-enter token");
    await ensureToken();
    return fetchJSON(path, options); // retry once
  }
  if (!res.ok) {
    throw new Error(`Request failed: ${res.status} ${await res.text().catch(() => "")}`);
  }
  return res.json();
}

// ── DOM refs ──────────────────────────────────────────────────────────────

const statusPill = document.getElementById("status-pill");
const agentCount = document.getElementById("agent-count");
const roomCount = document.getElementById("room-count");
const topologyEl = document.getElementById("topology");
const roomsEl = document.getElementById("rooms");
const toolsEl = document.getElementById("tools");
const messagesEl = document.getElementById("messages");
const roomSelect = document.getElementById("room-select");

const refreshBtn = document.getElementById("refresh");
const createRoomBtn = document.getElementById("create-room");
const runToolBtn = document.getElementById("run-tool");
const sendMessageBtn = document.getElementById("send-message");

// ── Render helpers ────────────────────────────────────────────────────────

function setStatus(text) {
  statusPill.textContent = text;
}

function relativeTime(ts) {
  const diff = Math.floor((Date.now() - ts) / 1000);
  if (diff < 60) return `${diff}s ago`;
  if (diff < 3600) return `${Math.floor(diff / 60)}m ago`;
  return `${Math.floor(diff / 3600)}h ago`;
}

function renderRooms(rooms) {
  roomsEl.innerHTML = "";
  roomSelect.innerHTML = '<option value="">— select room —</option>';

  rooms.forEach((room) => {
    const card = document.createElement("div");
    card.className = "card";
    card.innerHTML = `
      <strong>${room.name}</strong>
      <p class="muted">Agents: ${room.agents.length} • Active tasks: ${room.activeTasks}</p>
    `;
    roomsEl.appendChild(card);

    const option = document.createElement("option");
    option.value = room.id;
    option.textContent = room.name;
    roomSelect.appendChild(option);
  });
}

function renderTools(tools) {
  toolsEl.innerHTML = "";
  tools.forEach((tool) => {
    const card = document.createElement("div");
    card.className = "card";
    card.innerHTML = `
      <strong>${tool.name}</strong>
      <p class="muted">${tool.description || ""}</p>
    `;
    toolsEl.appendChild(card);
  });
}

function renderMessages(messages) {
  messagesEl.innerHTML = "";
  if (!messages.length) {
    messagesEl.innerHTML = '<p class="muted" style="padding:1rem">No messages yet.</p>';
    return;
  }
  [...messages].reverse().forEach((msg) => {
    const card = document.createElement("div");
    card.className = "card message";
    const ts = relativeTime(msg.timestamp);
    const dest = msg.receiver ? `→ ${msg.receiver}` : msg.room_id ? `#${msg.room_id}` : "broadcast";
    card.innerHTML = `
      <div style="display:flex;justify-content:space-between;align-items:baseline;gap:1rem">
        <span><strong>${msg.sender}</strong> <span class="muted">${dest}</span></span>
        <small class="muted">${ts}</small>
      </div>
      <p style="margin:0.25rem 0 0">${msg.content}</p>
      <small class="muted" style="font-size:0.7rem">MSG-${(msg.id ?? "").slice(0, 8)} · ${msg.message_type}</small>
    `;
    messagesEl.appendChild(card);
  });
}

function renderAgents(agents, container) {
  if (!container) return;
  container.innerHTML = "";
  agents.forEach((agent) => {
    const dot = agent.status === "online" ? "#4ade80" : agent.status === "busy" ? "#f6b73c" : "#6b7280";
    const ts = relativeTime(agent.lastSeen);
    const card = document.createElement("div");
    card.className = "card";
    card.innerHTML = `
      <div style="display:flex;align-items:center;gap:0.5rem">
        <span style="width:8px;height:8px;border-radius:50%;background:${dot};flex-shrink:0"></span>
        <strong>${agent.name}</strong>
        <span class="muted" style="font-size:0.75rem">${agent.role}</span>
      </div>
      ${agent.working_on ? `<p class="muted" style="margin:0.25rem 0 0;font-size:0.75rem">▶ ${agent.working_on}</p>` : ""}
      <small class="muted">${ts}</small>
    `;
    container.appendChild(card);
  });
}

function renderTopology(graph) {
  const width = 800;
  const height = 480;

  const svg = d3.select(topologyEl);
  svg.selectAll("*").remove();

  const nodes = graph.nodes.map((node) => ({ ...node }));
  const links = graph.edges.map((edge) => ({ source: edge.from, target: edge.to }));

  const simulation = d3
    .forceSimulation(nodes)
    .force("link", d3.forceLink(links).id((d) => d.id).distance(120))
    .force("charge", d3.forceManyBody().strength(-320))
    .force("center", d3.forceCenter(width / 2, height / 2));

  const link = svg
    .append("g")
    .attr("stroke", "#3c424f")
    .attr("stroke-opacity", 0.6)
    .selectAll("line")
    .data(links)
    .join("line")
    .attr("stroke-width", 2);

  const node = svg
    .append("g")
    .selectAll("circle")
    .data(nodes)
    .join("circle")
    .attr("r", (d) => (d.type === "room" ? 18 : 12))
    .attr("fill", (d) => (d.type === "room" ? "#f6b73c" : "#ff7a6b"))
    .attr("stroke", "#0c0e12")
    .attr("stroke-width", 2)
    .call(
      d3
        .drag()
        .on("start", (event, d) => {
          if (!event.active) simulation.alphaTarget(0.3).restart();
          d.fx = d.x;
          d.fy = d.y;
        })
        .on("drag", (event, d) => {
          d.fx = event.x;
          d.fy = event.y;
        })
        .on("end", (event, d) => {
          if (!event.active) simulation.alphaTarget(0);
          d.fx = null;
          d.fy = null;
        })
    );

  const label = svg
    .append("g")
    .selectAll("text")
    .data(nodes)
    .join("text")
    .attr("fill", "#f7f3ea")
    .attr("font-size", 12)
    .attr("font-family", "Space Grotesk")
    .text((d) => d.id);

  simulation.on("tick", () => {
    link
      .attr("x1", (d) => d.source.x)
      .attr("y1", (d) => d.source.y)
      .attr("x2", (d) => d.target.x)
      .attr("y2", (d) => d.target.y);

    node.attr("cx", (d) => d.x).attr("cy", (d) => d.y);
    label.attr("x", (d) => d.x + 14).attr("y", (d) => d.y + 4);
  });
}

// ── Agents panel ──────────────────────────────────────────────────────────

let agentsContainer = null;

function ensureAgentsPanel() {
  if (agentsContainer) return;
  const grid = document.querySelector(".grid");
  if (!grid) return;
  const panel = document.createElement("article");
  panel.className = "panel";
  panel.innerHTML = `
    <div class="panel-header">
      <h2>Agents</h2>
      <button id="clear-token" title="Re-enter auth token" style="font-size:0.75rem;padding:0.25rem 0.5rem">🔑 Token</button>
    </div>
    <div id="agents-list"></div>
  `;
  grid.appendChild(panel);
  agentsContainer = panel.querySelector("#agents-list");
  panel.querySelector("#clear-token").addEventListener("click", async () => {
    clearToken();
    await ensureToken();
    await refresh();
  });
}

// ── Refresh ───────────────────────────────────────────────────────────────

async function refresh() {
  try {
    setStatus("Online");
    const [agents, rooms, tools, topology] = await Promise.all([
      fetchJSON("/api/agents"),
      fetchJSON("/api/rooms"),
      fetchJSON("/api/tools"),
      fetchJSON("/api/topology"),
    ]);

    agentCount.textContent = agents.length;
    roomCount.textContent = rooms.length;
    renderRooms(rooms);
    renderTools(tools);
    renderTopology(topology);
    ensureAgentsPanel();
    renderAgents(agents, agentsContainer);

    if (rooms[0]) {
      await loadMessages(rooms[0].id);
    }
  } catch (error) {
    setStatus("Offline");
    console.error(error);
  }
}

async function loadMessages(roomId) {
  if (!roomId) return;
  const threadId = `room-${roomId}`;
  try {
    const context = await fetchJSON(`/api/threads/${threadId}`);
    renderMessages(context.messages || []);
  } catch {
    renderMessages([]);
  }
}

// ── Event listeners ───────────────────────────────────────────────────────

refreshBtn.addEventListener("click", refresh);

createRoomBtn.addEventListener("click", async () => {
  const name = prompt("Room name?");
  if (!name) return;
  try {
    await fetchJSON("/api/rooms", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name }),
    });
  } catch (err) {
    alert(err.message);
    return;
  }
  await refresh();
});

runToolBtn.addEventListener("click", async () => {
  const tool = prompt("Tool name?", "deploy_service");
  if (!tool) return;
  try {
    const result = await fetchJSON("/api/tools/execute", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ tool, args: { env: "staging" } }),
    });
    alert(JSON.stringify(result, null, 2));
  } catch (err) {
    alert(err.message);
  }
});

roomSelect.addEventListener("change", async (event) => {
  await loadMessages(event.target.value);
});

sendMessageBtn.addEventListener("click", async () => {
  const roomId = roomSelect.value;
  if (!roomId) {
    alert("Select a room first");
    return;
  }
  const content = prompt("Message?");
  if (!content) return;
  const sender = prompt("Your agent ID?", "cto_agent") || "cto_agent";
  try {
    await fetchJSON(`/api/rooms/${roomId}/message`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ sender, content }),
    });
    await loadMessages(roomId);
  } catch (err) {
    alert(err.message);
  }
});

// ── Boot ──────────────────────────────────────────────────────────────────

(async () => {
  await ensureToken();
  await refresh();
  // auto-refresh every 30s
  setInterval(refresh, 30_000);
})();
