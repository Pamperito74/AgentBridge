import http from "node:http";
import path from "node:path";
import { fileURLToPath } from "node:url";
import express from "express";
import cors from "cors";
import { WebSocketServer } from "ws";
import { nanoid } from "nanoid";
import { store } from "../core/store.js";
import { AgentRouter } from "../core/router.js";
import { executeTool, listTools } from "../core/tools.js";
import { paperclipAdapter } from "../core/paperclipAdapter.js";
import type { A2AMessage } from "../core/types.js";
import { logger } from "./logger.js";
import {
  registerAgentSchema,
  patchAgentStatusSchema,
  createRoomSchema,
  roomMessageSchema,
  createTaskSchema,
  patchTaskSchema,
  planGoalSchema,
  executeToolSchema,
  a2aMessageSchema,
  requestMessageSchema,
  respondMessageSchema,
  ackSchema,
} from "./validation.js";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const PORT = Number(process.env.PORT ?? 4000);

// Validate required env vars at startup
const AGENTBRIDGE_TOKEN = process.env.AGENTBRIDGE_TOKEN;
if (!AGENTBRIDGE_TOKEN) {
  logger.fatal("AGENTBRIDGE_TOKEN environment variable is not set. Exiting.");
  process.exit(1);
}

const router = new AgentRouter();

await store.load();

const app = express();
app.use(cors({ origin: process.env.ALLOWED_ORIGINS?.split(",") ?? "*" }));
app.use(express.json({ limit: "1mb" }));
app.use(express.static(path.resolve(__dirname, "../../public")));

// Auth middleware — all /api/* routes require Bearer token
function requireAuth(req: express.Request, res: express.Response, next: express.NextFunction): void {
  const auth = req.headers.authorization;
  if (!auth || !auth.startsWith("Bearer ")) {
    res.status(401).json({ error: "Unauthorized: missing Bearer token" });
    return;
  }
  const token = auth.slice(7);
  if (token !== AGENTBRIDGE_TOKEN) {
    res.status(401).json({ error: "Unauthorized: invalid token" });
    return;
  }
  next();
}

app.use("/api", requireAuth);

app.get("/health", (_req, res) => {
  res.json({ ok: true, time: new Date().toISOString() });
});

// ── Agents ────────────────────────────────────────────────────────────────

app.get("/api/agents", (_req, res) => {
  res.json(store.listAgents());
});

app.post("/api/agents/register", (req, res) => {
  const parsed = registerAgentSchema.safeParse(req.body);
  if (!parsed.success) {
    res.status(400).json({ error: parsed.error.flatten() });
    return;
  }
  const body = parsed.data;
  const agent = store.upsertAgent({
    id: body.id,
    name: body.name,
    role: body.role,
    status: body.status ?? "online",
    meta: body.meta ?? {},
  });
  logger.info({ agentId: agent.id }, "agent registered");
  res.json(agent);
});

app.get("/api/agents/:id", (req, res) => {
  const agent = store.getAgent(req.params.id);
  if (!agent) {
    res.status(404).json({ error: `Agent '${req.params.id}' not found` });
    return;
  }
  res.json(agent);
});

app.patch("/api/agents/:id/status", (req, res) => {
  const agent = store.getAgent(req.params.id);
  if (!agent) {
    res.status(404).json({ error: `Agent '${req.params.id}' not found` });
    return;
  }
  const parsed = patchAgentStatusSchema.safeParse(req.body);
  if (!parsed.success) {
    res.status(400).json({ error: parsed.error.flatten() });
    return;
  }
  const { status, working_on } = parsed.data;
  store.updateAgentStatus(req.params.id, status ?? agent.status, working_on);
  logger.info({ agentId: req.params.id, status, working_on }, "agent status updated");
  res.json(store.getAgent(req.params.id));
});

// Inbox: messages addressed specifically to this agent
app.get("/api/agents/:id/inbox", (req, res) => {
  const agent = store.getAgent(req.params.id);
  if (!agent) {
    res.status(404).json({ error: `Agent '${req.params.id}' not found` });
    return;
  }
  const unreadOnly = req.query.unread === "true";
  let msgs = store.getInbox(req.params.id);
  if (unreadOnly) {
    msgs = msgs.filter((m) => !m.read);
  }
  res.json({ agentId: req.params.id, unread_count: msgs.filter((m) => !m.read).length, messages: msgs });
});

// Acknowledge (mark read) a specific inbox message
app.post("/api/agents/:id/inbox/ack", (req, res) => {
  const parsed = ackSchema.safeParse(req.body);
  if (!parsed.success) {
    res.status(400).json({ error: parsed.error.flatten() });
    return;
  }
  const ok = store.markInboxRead(req.params.id, parsed.data.messageId);
  if (!ok) {
    res.status(404).json({ error: "Message not found in this agent's inbox" });
    return;
  }
  res.json({ ok: true });
});

// Deregister (go offline)
app.delete("/api/agents/:id", (req, res) => {
  const agent = store.getAgent(req.params.id);
  if (!agent) {
    res.status(404).json({ error: `Agent '${req.params.id}' not found` });
    return;
  }
  store.updateAgentStatus(req.params.id, "offline", undefined);
  logger.info({ agentId: req.params.id }, "agent deregistered");
  res.json({ ok: true, message: `Agent '${req.params.id}' is now offline` });
});

// ── Rooms ─────────────────────────────────────────────────────────────────

app.get("/api/rooms", (_req, res) => {
  res.json(store.listRooms());
});

app.post("/api/rooms", (req, res) => {
  const parsed = createRoomSchema.safeParse(req.body);
  if (!parsed.success) {
    res.status(400).json({ error: parsed.error.flatten() });
    return;
  }
  const room = store.createRoom(parsed.data.name, parsed.data.agents ?? []);
  if (!room) {
    const id = parsed.data.name.toLowerCase().replace(/\s+/g, "-");
    res.status(409).json({
      error: `Room '${parsed.data.name}' already exists.`,
      hint: `Use POST /api/rooms/${id}/join to join it.`,
    });
    return;
  }
  logger.info({ roomId: room.id }, "room created");
  res.json(room);
});

app.get("/api/rooms/:id", (req, res) => {
  const room = store.getRoom(req.params.id);
  if (!room) {
    res.status(404).json({ error: "room not found" });
    return;
  }
  res.json(room);
});

app.post("/api/rooms/:id/join", (req, res) => {
  const room = store.getRoom(req.params.id);
  if (!room) {
    res.status(404).json({ error: `Room '${req.params.id}' not found` });
    return;
  }
  const agentId = (req.body as { agentId?: string }).agentId;
  if (!agentId || typeof agentId !== "string") {
    res.status(400).json({ error: "agentId is required" });
    return;
  }
  const updated = store.addAgentToRoom(req.params.id, agentId);
  logger.info({ roomId: req.params.id, agentId }, "agent joined room");
  res.json(updated);
});

app.post("/api/rooms/:id/message", (req, res) => {
  const room = store.getRoom(req.params.id);
  if (!room) {
    res.status(404).json({ error: "room not found" });
    return;
  }
  const parsed = roomMessageSchema.safeParse(req.body);
  if (!parsed.success) {
    res.status(400).json({ error: parsed.error.flatten() });
    return;
  }
  const { sender, content, thread_id, receiver } = parsed.data;
  const message = store.addMessage({
    protocol: "A2A/1.0",
    sender,
    receiver,
    room_id: room.id,
    thread_id: thread_id ?? `room-${room.id}`,
    message_type: "discussion",
    content,
    timestamp: Date.now(),
  });

  if (receiver) {
    // targeted message — deliver only to receiver
    const delivered = router.sendToAgent(receiver, message);
    if (!delivered) {
      logger.warn({ roomId: room.id, sender, receiver }, "receiver offline");
    }
  } else {
    // broadcast to room — skip sender to avoid echo
    room.agents
      .filter((agentId) => agentId !== sender)
      .forEach((agentId) => router.sendToAgent(agentId, message));
  }

  logger.info({ roomId: room.id, sender }, "room message sent");
  res.json(message);
});

// ── Threads ───────────────────────────────────────────────────────────────

app.get("/api/threads/:id", (req, res) => {
  res.json(store.getContext(req.params.id));
});

// ── Messages (global, cursor-based) ───────────────────────────────────────

app.get("/api/messages", (req, res) => {
  const sinceId = req.query.since as string | undefined;
  const limit = Math.min(Number(req.query.limit ?? 20), 100);
  const messages = store.getMessages({ sinceId, limit });
  res.json(messages);
});

// ── Request / Response ────────────────────────────────────────────────────

/**
 * Send a correlated request to a specific agent.
 * Returns immediately with { requestId } — caller polls GET /api/messages/request/:requestId.
 */
app.post("/api/messages/request", (req, res) => {
  const parsed = requestMessageSchema.safeParse(req.body);
  if (!parsed.success) {
    res.status(400).json({ error: parsed.error.flatten() });
    return;
  }
  const { sender, recipient, content, timeout_ms } = parsed.data;
  const pending = store.createRequest({
    sender,
    recipient,
    content,
    timeoutMs: timeout_ms ?? 60_000,
  });

  // deliver the request as an inbox message to the recipient
  const message = store.addMessage({
    protocol: "A2A/1.0",
    sender,
    receiver: recipient,
    thread_id: `req-${pending.requestId}`,
    message_type: "task_request",
    content,
    timestamp: Date.now(),
    correlationId: pending.requestId,
  });

  const delivered = router.sendToAgent(recipient, message);
  if (!delivered) {
    logger.warn({ sender, recipient }, "request recipient offline — message stored in inbox");
  }

  logger.info({ requestId: pending.requestId, sender, recipient }, "request created");
  res.json({ requestId: pending.requestId, message });
});

/**
 * Respond to a pending request by correlationId.
 */
app.post("/api/messages/respond", (req, res) => {
  const parsed = respondMessageSchema.safeParse(req.body);
  if (!parsed.success) {
    res.status(400).json({ error: parsed.error.flatten() });
    return;
  }
  const { requestId, sender, content } = parsed.data;
  const pending = store.getRequest(requestId);
  if (!pending) {
    res.status(404).json({ error: `Request '${requestId}' not found` });
    return;
  }
  if (pending.resolved) {
    res.status(409).json({ error: `Request '${requestId}' already has a response` });
    return;
  }

  const responseMessage = store.addMessage({
    protocol: "A2A/1.0",
    sender,
    receiver: pending.sender,
    thread_id: `req-${requestId}`,
    message_type: "task_result",
    content,
    timestamp: Date.now(),
    correlationId: requestId,
  });

  store.resolveRequest(requestId, responseMessage);

  // deliver response to original requester via WS if connected
  router.sendToAgent(pending.sender, responseMessage);

  logger.info({ requestId, sender }, "request responded");
  res.json({ ok: true, message: responseMessage });
});

/**
 * Poll for a response to a pending request (long-poll up to timeout).
 */
app.get("/api/messages/request/:requestId", async (req, res) => {
  const pending = store.getRequest(req.params.requestId);
  if (!pending) {
    res.status(404).json({ error: `Request '${req.params.requestId}' not found` });
    return;
  }

  if (pending.resolved && pending.response) {
    res.json({ resolved: true, response: pending.response });
    return;
  }

  const deadline = pending.createdAt + pending.timeoutMs;
  const pollInterval = 500;

  const poll = (): Promise<void> =>
    new Promise((resolve) => {
      const interval = setInterval(() => {
        const current = store.getRequest(req.params.requestId);
        if (!current) {
          clearInterval(interval);
          res.status(404).json({ error: "Request disappeared" });
          resolve();
          return;
        }
        if (current.resolved && current.response) {
          clearInterval(interval);
          res.json({ resolved: true, response: current.response });
          resolve();
          return;
        }
        if (Date.now() >= deadline) {
          clearInterval(interval);
          res.json({ resolved: false, message: "Request timed out — recipient has not responded yet" });
          resolve();
        }
      }, pollInterval);

      req.on("close", () => {
        clearInterval(interval);
        resolve();
      });
    });

  await poll();
});

// ── Tools ─────────────────────────────────────────────────────────────────

app.get("/api/tools", (_req, res) => {
  res.json(listTools());
});

app.post("/api/tools/execute", async (req, res) => {
  const parsed = executeToolSchema.safeParse(req.body);
  if (!parsed.success) {
    res.status(400).json({ error: parsed.error.flatten() });
    return;
  }
  const result = await executeTool(parsed.data.tool, parsed.data.args ?? {});
  res.json(result);
});

// ── Tasks ─────────────────────────────────────────────────────────────────

app.get("/api/tasks", (req, res) => {
  const roomId = req.query.room_id as string | undefined;
  const assignedTo = req.query.assigned_to as string | undefined;
  const status = req.query.status as string | undefined;
  let tasks = store.listTasks(roomId);
  if (assignedTo) tasks = tasks.filter((t) => t.assignedTo === assignedTo);
  if (status) tasks = tasks.filter((t) => t.status === status);
  res.json(tasks);
});

app.post("/api/tasks", (req, res) => {
  const parsed = createTaskSchema.safeParse(req.body);
  if (!parsed.success) {
    res.status(400).json({ error: parsed.error.flatten() });
    return;
  }
  const { title, description, assignedTo, roomId } = parsed.data;
  const task = store.createTask({
    title,
    description,
    assignedTo,
    roomId,
    status: parsed.data.status ?? "todo",
  });
  logger.info({ taskId: task.id }, "task created");
  res.json(task);
});

app.get("/api/tasks/:id", (req, res) => {
  const task = store.getTask(req.params.id);
  if (!task) {
    res.status(404).json({ error: `Task '${req.params.id}' not found` });
    return;
  }
  res.json(task);
});

app.patch("/api/tasks/:id", (req, res) => {
  const task = store.getTask(req.params.id);
  if (!task) {
    res.status(404).json({ error: `Task '${req.params.id}' not found` });
    return;
  }
  const parsed = patchTaskSchema.safeParse(req.body);
  if (!parsed.success) {
    res.status(400).json({ error: parsed.error.flatten() });
    return;
  }
  const updated = store.updateTask(req.params.id, parsed.data);
  logger.info({ taskId: req.params.id, patch: parsed.data }, "task updated");
  res.json(updated);
});

// ── PM Planner ────────────────────────────────────────────────────────────

app.post("/api/pm/plan", (req, res) => {
  const parsed = planGoalSchema.safeParse(req.body);
  if (!parsed.success) {
    res.status(400).json({ error: parsed.error.flatten() });
    return;
  }
  const { goal } = parsed.data;
  const agents = store.listAgents();
  const plan = [
    { title: `Scope: ${goal}`, role: "cto" },
    { title: `Implement: ${goal}`, role: "backend" },
    { title: `Ship: ${goal}`, role: "devops" },
  ];
  const tasks = plan.map((item) => {
    const match = agents.find((agent) => agent.role.toLowerCase().includes(item.role));
    return store.createTask({
      title: item.title,
      description: `Assigned by Project Manager Agent for goal: ${goal}`,
      assignedTo: match?.id,
      status: "todo",
    });
  });
  res.json({ goal, tasks });
});

// ── Topology ──────────────────────────────────────────────────────────────

app.get("/api/topology", (_req, res) => {
  res.json(store.getTopology());
});

// ── Paperclip adapter ─────────────────────────────────────────────────────

app.get("/api/paperclip/agents", async (_req, res) => {
  try {
    const agents = await paperclipAdapter.fetchAgents();
    res.json(agents);
  } catch (error) {
    res.status(500).json({ error: (error as Error).message });
  }
});

app.get("/api/paperclip/tasks", async (_req, res) => {
  try {
    const tasks = await paperclipAdapter.fetchTasks();
    res.json(tasks);
  } catch (error) {
    res.status(500).json({ error: (error as Error).message });
  }
});

// ── WebSocket (A2A) ───────────────────────────────────────────────────────

const server = http.createServer(app);
const wss = new WebSocketServer({ server, path: "/a2a" });

wss.on("connection", (socket, req) => {
  // Auth: prefer Authorization header (secure), fall back to URL param for backwards compat
  const authHeader = req.headers.authorization;
  const params = new URL(req.url ?? "", "http://localhost").searchParams;
  const token = authHeader?.startsWith("Bearer ") ? authHeader.slice(7) : params.get("token");
  const agentId = params.get("agent_id");

  if (!token || token !== AGENTBRIDGE_TOKEN) {
    logger.warn("WebSocket connection rejected: invalid or missing token");
    socket.close(1008, "Unauthorized: invalid or missing token");
    return;
  }
  if (!agentId) {
    socket.close(1008, "agent_id is required");
    return;
  }
  router.register(agentId, socket);
  store.updateAgentStatus(agentId, "online");
  logger.info({ agentId }, "WebSocket connected");

  socket.on("message", (data) => {
    let rawPayload: unknown;
    try {
      rawPayload = JSON.parse(data.toString());
    } catch {
      socket.send(JSON.stringify({ error: "Invalid JSON" }));
      return;
    }

    const parsed = a2aMessageSchema.safeParse(rawPayload);
    if (!parsed.success) {
      socket.send(JSON.stringify({ error: "Invalid A2A message", details: parsed.error.flatten() }));
      return;
    }

    const message = store.addMessage({
      ...parsed.data,
      thread_id: parsed.data.thread_id ?? nanoid(),
      timestamp: parsed.data.timestamp ?? Date.now(),
    });

    if (message.receiver) {
      const delivered = router.sendToAgent(message.receiver, message);
      if (!delivered) {
        socket.send(JSON.stringify({ warning: "Receiver offline", message }));
      }
      return;
    }

    if (message.room_id) {
      const room = store.getRoom(message.room_id);
      if (room) {
        // echo suppression: skip sender
        room.agents
          .filter((aid) => aid !== agentId)
          .forEach((aid) => router.sendToAgent(aid, message));
        return;
      }
    }

    // broadcast to all except sender
    router.broadcastExcept(agentId, message);
  });

  socket.on("close", () => {
    router.unregister(agentId);
    store.updateAgentStatus(agentId, "offline");
    logger.info({ agentId }, "WebSocket disconnected");
  });
});

// Global error handler
app.use((err: Error, _req: express.Request, res: express.Response, _next: express.NextFunction) => {
  logger.error({ err }, "unhandled server error");
  res.status(500).json({ error: "Internal server error" });
});

server.listen(PORT, () => {
  logger.info({ port: PORT }, "AgentBridge running");
});

// Graceful shutdown
function shutdown() {
  logger.info("shutting down...");
  server.close(() => process.exit(0));
  setTimeout(() => process.exit(1), 10_000);
}
process.on("SIGTERM", shutdown);
process.on("SIGINT", shutdown);
