import { promises as fs } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { nanoid } from "nanoid";
import type { Agent, A2AMessage, Room, Task, TopologyGraph, PendingRequest } from "./types.js";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const DATA_PATH = path.resolve(__dirname, "../../data/state.json");

interface PersistedState {
  agents: Agent[];
  rooms: Room[];
  tasks: Task[];
  threads: Record<string, A2AMessage[]>;
}

export class AgentBridgeStore {
  private agents = new Map<string, Agent>();
  private rooms = new Map<string, Room>();
  private tasks = new Map<string, Task>();
  private threads = new Map<string, A2AMessage[]>();
  /** inbox: agentId → messages where receiver === agentId */
  private inbox = new Map<string, A2AMessage[]>();
  /** pending requests awaiting a correlated response */
  private pendingRequests = new Map<string, PendingRequest>();
  private persistTimer: NodeJS.Timeout | null = null;

  async load(): Promise<void> {
    try {
      const raw = await fs.readFile(DATA_PATH, "utf-8");
      let parsed: PersistedState;
      try {
        parsed = JSON.parse(raw) as PersistedState;
      } catch {
        console.warn("[AgentBridgeStore] state.json is corrupt — starting fresh. Backing up to state.json.bak");
        await fs.writeFile(DATA_PATH + ".bak", raw).catch(() => {});
        return;
      }
      parsed.agents?.forEach((agent) => this.agents.set(agent.id, agent));
      parsed.rooms?.forEach((room) => this.rooms.set(room.id, room));
      parsed.tasks?.forEach((task) => this.tasks.set(task.id, task));
      Object.entries(parsed.threads ?? {}).forEach(([id, messages]) => {
        this.threads.set(id, messages);
        // rebuild inbox index from persisted messages
        messages.forEach((msg) => {
          if (msg.receiver) {
            const agentInbox = this.inbox.get(msg.receiver) ?? [];
            agentInbox.push(msg);
            this.inbox.set(msg.receiver, agentInbox);
          }
        });
      });
    } catch (error) {
      if ((error as NodeJS.ErrnoException).code !== "ENOENT") {
        throw error;
      }
    }
  }

  private schedulePersist() {
    if (this.persistTimer) {
      clearTimeout(this.persistTimer);
    }
    this.persistTimer = setTimeout(() => void this.persist(), 250);
  }

  private async persist() {
    const state: PersistedState = {
      agents: [...this.agents.values()],
      rooms: [...this.rooms.values()],
      tasks: [...this.tasks.values()],
      threads: Object.fromEntries(this.threads.entries()),
    };
    const dir = path.dirname(DATA_PATH);
    const tmp = DATA_PATH + ".tmp";
    await fs.mkdir(dir, { recursive: true });
    await fs.writeFile(tmp, JSON.stringify(state, null, 2));
    await fs.rename(tmp, DATA_PATH); // atomic on same filesystem
  }

  listAgents(): Agent[] {
    return [...this.agents.values()];
  }

  getAgent(id: string): Agent | undefined {
    return this.agents.get(id);
  }

  upsertAgent(input: Omit<Agent, "lastSeen"> & { lastSeen?: number }): Agent {
    const now = Date.now();
    const agent: Agent = {
      ...input,
      lastSeen: input.lastSeen ?? now,
    };
    this.agents.set(agent.id, agent);
    this.schedulePersist();
    return agent;
  }

  updateAgentStatus(id: string, status: Agent["status"], working_on?: string) {
    const agent = this.agents.get(id);
    if (!agent) return;
    agent.status = status;
    agent.lastSeen = Date.now();
    if (working_on !== undefined) {
      agent.working_on = working_on;
    }
    this.schedulePersist();
  }

  listRooms(): Room[] {
    return [...this.rooms.values()];
  }

  getRoom(id: string): Room | undefined {
    return this.rooms.get(id);
  }

  /** Returns undefined if room already exists (caller should 409). */
  createRoom(name: string, agents: string[] = []): Room | undefined {
    const id = name.toLowerCase().replace(/\s+/g, "-");
    if (this.rooms.has(id)) {
      return undefined;
    }
    const room: Room = {
      id,
      name,
      agents: [...new Set(agents)],
      activeTasks: 0,
      createdAt: Date.now(),
    };
    this.rooms.set(room.id, room);
    this.schedulePersist();
    return room;
  }

  addAgentToRoom(roomId: string, agentId: string): Room | undefined {
    const room = this.rooms.get(roomId);
    if (!room) return undefined;
    if (!room.agents.includes(agentId)) {
      room.agents.push(agentId);
      this.schedulePersist();
    }
    return room;
  }

  addMessage(message: Omit<A2AMessage, "id"> & { id?: string }): A2AMessage {
    const msg: A2AMessage = {
      ...message,
      id: message.id ?? nanoid(),
    };
    const thread = this.threads.get(msg.thread_id) ?? [];
    thread.push(msg);
    this.threads.set(msg.thread_id, thread);

    // maintain inbox index for targeted messages
    if (msg.receiver) {
      const agentInbox = this.inbox.get(msg.receiver) ?? [];
      agentInbox.push(msg);
      this.inbox.set(msg.receiver, agentInbox);
    }

    this.schedulePersist();
    return msg;
  }

  getThread(threadId: string): A2AMessage[] {
    return this.threads.get(threadId) ?? [];
  }

  /** Returns all messages across all threads, optionally since a given message id, up to limit. */
  getMessages(opts: { sinceId?: string; limit?: number } = {}): A2AMessage[] {
    const all: A2AMessage[] = [];
    for (const msgs of this.threads.values()) {
      all.push(...msgs);
    }
    all.sort((a, b) => a.timestamp - b.timestamp);

    if (opts.sinceId) {
      const idx = all.findIndex((m) => m.id === opts.sinceId);
      if (idx !== -1) {
        all.splice(0, idx + 1);
      }
    }

    if (opts.limit) {
      return all.slice(0, opts.limit);
    }
    return all;
  }

  getInbox(agentId: string): A2AMessage[] {
    return this.inbox.get(agentId) ?? [];
  }

  markInboxRead(agentId: string, messageId: string): boolean {
    const msgs = this.inbox.get(agentId);
    if (!msgs) return false;
    const msg = msgs.find((m) => m.id === messageId);
    if (!msg) return false;
    msg.read = true;
    this.schedulePersist();
    return true;
  }

  createTask(input: Omit<Task, "id" | "createdAt" | "updatedAt">): Task {
    const task: Task = {
      id: nanoid(),
      createdAt: Date.now(),
      updatedAt: Date.now(),
      ...input,
    };
    this.tasks.set(task.id, task);
    if (task.roomId) {
      const room = this.rooms.get(task.roomId);
      if (room) {
        room.activeTasks += 1;
      }
    }
    this.schedulePersist();
    return task;
  }

  listTasks(roomId?: string): Task[] {
    const tasks = [...this.tasks.values()];
    if (!roomId) return tasks;
    return tasks.filter((task) => task.roomId === roomId);
  }

  getTask(id: string): Task | undefined {
    return this.tasks.get(id);
  }

  updateTask(id: string, patch: Partial<Task>): Task | undefined {
    const task = this.tasks.get(id);
    if (!task) return undefined;

    const prevStatus = task.status;
    const updated: Task = {
      ...task,
      ...patch,
      updatedAt: Date.now(),
    };
    this.tasks.set(id, updated);

    // decrement room activeTasks when task transitions to a terminal state
    const terminal = new Set<Task["status"]>(["done", "blocked"]);
    const wasActive = !terminal.has(prevStatus);
    const isNowTerminal = terminal.has(updated.status);
    if (wasActive && isNowTerminal && updated.roomId) {
      const room = this.rooms.get(updated.roomId);
      if (room && room.activeTasks > 0) {
        room.activeTasks -= 1;
      }
    }

    this.schedulePersist();
    return updated;
  }

  // ── Request / Response correlation ──────────────────────────────────────

  createRequest(input: Omit<PendingRequest, "requestId" | "createdAt" | "resolved">): PendingRequest {
    const req: PendingRequest = {
      ...input,
      requestId: nanoid(),
      createdAt: Date.now(),
      resolved: false,
    };
    this.pendingRequests.set(req.requestId, req);
    return req;
  }

  getRequest(requestId: string): PendingRequest | undefined {
    return this.pendingRequests.get(requestId);
  }

  resolveRequest(requestId: string, response: A2AMessage): PendingRequest | undefined {
    const req = this.pendingRequests.get(requestId);
    if (!req || req.resolved) return undefined;
    req.response = response;
    req.resolved = true;
    return req;
  }

  getContext(threadId: string) {
    const messages = this.getThread(threadId);
    const participants = new Set<string>();
    messages.forEach((msg) => {
      participants.add(msg.sender);
      if (msg.receiver) participants.add(msg.receiver);
    });
    return {
      thread_id: threadId,
      participants: [...participants],
      messages,
    };
  }

  getTopology(): TopologyGraph {
    const nodes: TopologyGraph["nodes"] = this.listAgents().map((agent) => ({
      id: agent.id,
      type: "agent" as const,
    }));
    this.listRooms().forEach((room) => {
      nodes.push({ id: room.id, type: "room" as const });
    });

    const edges = this.listRooms().flatMap((room) =>
      room.agents.map((agentId) => ({
        from: room.id,
        to: agentId,
      }))
    );

    return { nodes, edges };
  }
}

// Singleton — shared between REST API and MCP server
export const store = new AgentBridgeStore();
