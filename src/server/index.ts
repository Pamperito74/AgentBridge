import http from "node:http";
import path from "node:path";
import { fileURLToPath } from "node:url";
import express from "express";
import cors from "cors";
import { WebSocketServer } from "ws";
import { nanoid } from "nanoid";
import { AgentBridgeStore } from "../core/store.js";
import { AgentRouter } from "../core/router.js";
import { executeTool, listTools } from "../core/tools.js";
import { paperclipAdapter } from "../core/paperclipAdapter.js";
import type { A2AMessage, Task } from "../core/types.js";
import { logger } from "./logger.js";
import {
  registerAgentSchema,
  createRoomSchema,
  roomMessageSchema,
  createTaskSchema,
  planGoalSchema,
  executeToolSchema,
  a2aMessageSchema,
} from "./validation.js";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const PORT = Number(process.env.PORT ?? 4000);

// Validate required env vars at startup
const AGENTBRIDGE_TOKEN = process.env.AGENTBRIDGE_TOKEN;
if (!AGENTBRIDGE_TOKEN) {
  logger.fatal("AGENTBRIDGE_TOKEN environment variable is not set. Exiting.");
  process.exit(1);
}

const store = new AgentBridgeStore();
const router = new AgentRouter();

await store.load();

const app = express();
app.use(cors());
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
  const { sender, content, thread_id } = parsed.data;
  const message: A2AMessage = {
    protocol: "A2A/1.0",
    sender,
    room_id: room.id,
    thread_id: thread_id ?? `room-${room.id}`,
    message_type: "discussion",
    content,
    timestamp: Date.now(),
  };
  store.addMessage(message);
  room.agents.forEach((agentId) => router.sendToAgent(agentId, message));
  logger.info({ roomId: room.id, sender }, "room message sent");
  res.json(message);
});

app.get("/api/threads/:id", (req, res) => {
  res.json(store.getContext(req.params.id));
});

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

app.get("/api/tasks", (req, res) => {
  const roomId = req.query.room_id as string | undefined;
  res.json(store.listTasks(roomId));
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
    status: "todo",
  });
  logger.info({ taskId: task.id }, "task created");
  res.json(task);
});

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

app.get("/api/topology", (_req, res) => {
  res.json(store.getTopology());
});

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

const server = http.createServer(app);
const wss = new WebSocketServer({ server, path: "/a2a" });

wss.on("connection", (socket, req) => {
  const params = new URL(req.url ?? "", "http://localhost").searchParams;
  const agentId = params.get("agent_id");
  const token = params.get("token");

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

    const message: A2AMessage = {
      ...parsed.data,
      thread_id: parsed.data.thread_id ?? nanoid(),
      timestamp: parsed.data.timestamp ?? Date.now(),
    };

    store.addMessage(message);

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
        room.agents.forEach((agent) => router.sendToAgent(agent, message));
        return;
      }
    }

    router.broadcast(message);
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
