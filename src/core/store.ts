import { promises as fs } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { nanoid } from "nanoid";
import type { Agent, A2AMessage, Room, Task, TopologyGraph } from "./types.js";

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
  private persistTimer: NodeJS.Timeout | null = null;

  async load(): Promise<void> {
    try {
      const raw = await fs.readFile(DATA_PATH, "utf-8");
      const parsed = JSON.parse(raw) as PersistedState;
      parsed.agents.forEach((agent) => this.agents.set(agent.id, agent));
      parsed.rooms.forEach((room) => this.rooms.set(room.id, room));
      parsed.tasks.forEach((task) => this.tasks.set(task.id, task));
      Object.entries(parsed.threads).forEach(([id, messages]) => {
        this.threads.set(id, messages);
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
    await fs.mkdir(path.dirname(DATA_PATH), { recursive: true });
    await fs.writeFile(DATA_PATH, JSON.stringify(state, null, 2));
  }

  listAgents(): Agent[] {
    return [...this.agents.values()];
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

  updateAgentStatus(id: string, status: Agent["status"]) {
    const agent = this.agents.get(id);
    if (!agent) return;
    agent.status = status;
    agent.lastSeen = Date.now();
    this.schedulePersist();
  }

  listRooms(): Room[] {
    return [...this.rooms.values()];
  }

  getRoom(id: string): Room | undefined {
    return this.rooms.get(id);
  }

  createRoom(name: string, agents: string[] = []): Room {
    const room: Room = {
      id: name.toLowerCase().replace(/\s+/g, "-"),
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

  addMessage(message: A2AMessage): A2AMessage {
    const thread = this.threads.get(message.thread_id) ?? [];
    thread.push(message);
    this.threads.set(message.thread_id, thread);
    this.schedulePersist();
    return message;
  }

  getThread(threadId: string): A2AMessage[] {
    return this.threads.get(threadId) ?? [];
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

  updateTask(id: string, patch: Partial<Task>): Task | undefined {
    const task = this.tasks.get(id);
    if (!task) return undefined;
    const updated: Task = {
      ...task,
      ...patch,
      updatedAt: Date.now(),
    };
    this.tasks.set(id, updated);
    this.schedulePersist();
    return updated;
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
