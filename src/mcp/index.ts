import { Server } from "@modelcontextprotocol/sdk/server/index.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import {
  CallToolRequestSchema,
  ListToolsRequestSchema,
} from "@modelcontextprotocol/sdk/types.js";
import { store } from "../core/store.js";
import { executeTool, listTools } from "../core/tools.js";

// store is the singleton shared with the REST API — no separate load() needed
// when running embedded. When running standalone (stdio only), we still need
// to load persisted state.
await store.load();

const server = new Server(
  {
    name: "agentbridge-mcp",
    version: "0.2.0",
  },
  {
    capabilities: {
      tools: {},
    },
  }
);

server.setRequestHandler(ListToolsRequestSchema, async () => {
  const tools = [
    {
      name: "register",
      description: "Register this agent with AgentBridge",
      inputSchema: {
        type: "object",
        properties: {
          id: { type: "string" },
          name: { type: "string" },
          role: { type: "string" },
          working_on: { type: "string" },
        },
        required: ["id", "name", "role"],
      },
    },
    {
      name: "list_agents",
      description: "List agents known to AgentBridge",
      inputSchema: { type: "object", properties: {} },
    },
    {
      name: "get_agent",
      description: "Get a specific agent by ID",
      inputSchema: {
        type: "object",
        properties: { id: { type: "string" } },
        required: ["id"],
      },
    },
    {
      name: "update_status",
      description: "Update this agent's status and working_on field",
      inputSchema: {
        type: "object",
        properties: {
          id: { type: "string" },
          status: { type: "string", enum: ["online", "busy", "offline"] },
          working_on: { type: "string" },
        },
        required: ["id"],
      },
    },
    {
      name: "send",
      description: "Send a message to a room or specific agent",
      inputSchema: {
        type: "object",
        properties: {
          sender: { type: "string" },
          content: { type: "string" },
          room_id: { type: "string" },
          receiver: { type: "string" },
          thread_id: { type: "string" },
          message_type: { type: "string" },
        },
        required: ["sender", "content"],
      },
    },
    {
      name: "read",
      description: "Read messages, optionally filtered by thread or since a message id",
      inputSchema: {
        type: "object",
        properties: {
          thread_id: { type: "string" },
          since_id: { type: "string" },
          limit: { type: "number" },
        },
      },
    },
    {
      name: "get_inbox",
      description: "Get messages addressed to a specific agent",
      inputSchema: {
        type: "object",
        properties: {
          agent_id: { type: "string" },
          unread_only: { type: "boolean" },
        },
        required: ["agent_id"],
      },
    },
    {
      name: "ack",
      description: "Mark an inbox message as read",
      inputSchema: {
        type: "object",
        properties: {
          agent_id: { type: "string" },
          message_id: { type: "string" },
        },
        required: ["agent_id", "message_id"],
      },
    },
    {
      name: "request_agent",
      description: "Send a correlated request to an agent and wait for response",
      inputSchema: {
        type: "object",
        properties: {
          sender: { type: "string" },
          recipient: { type: "string" },
          content: { type: "string" },
          timeout_ms: { type: "number" },
        },
        required: ["sender", "recipient", "content"],
      },
    },
    {
      name: "respond",
      description: "Respond to a pending request by correlationId",
      inputSchema: {
        type: "object",
        properties: {
          request_id: { type: "string" },
          sender: { type: "string" },
          content: { type: "string" },
        },
        required: ["request_id", "sender", "content"],
      },
    },
    {
      name: "list_rooms",
      description: "List AgentBridge rooms",
      inputSchema: { type: "object", properties: {} },
    },
    {
      name: "create_room",
      description: "Create a new room (returns error if name already taken)",
      inputSchema: {
        type: "object",
        properties: {
          name: { type: "string" },
          agents: { type: "array", items: { type: "string" } },
        },
        required: ["name"],
      },
    },
    {
      name: "join_room",
      description: "Join an existing room",
      inputSchema: {
        type: "object",
        properties: {
          room_id: { type: "string" },
          agent_id: { type: "string" },
        },
        required: ["room_id", "agent_id"],
      },
    },
    {
      name: "get_context",
      description: "Get thread context",
      inputSchema: {
        type: "object",
        properties: { thread_id: { type: "string" } },
        required: ["thread_id"],
      },
    },
    {
      name: "list_tasks",
      description: "List tasks, optionally filtered by room, assignee, or status",
      inputSchema: {
        type: "object",
        properties: {
          room_id: { type: "string" },
          assigned_to: { type: "string" },
          status: { type: "string" },
        },
      },
    },
    {
      name: "create_task",
      description: "Create a new task",
      inputSchema: {
        type: "object",
        properties: {
          title: { type: "string" },
          description: { type: "string" },
          assigned_to: { type: "string" },
          room_id: { type: "string" },
        },
        required: ["title"],
      },
    },
    {
      name: "update_task",
      description: "Update a task's status, assignee, or result",
      inputSchema: {
        type: "object",
        properties: {
          id: { type: "string" },
          status: { type: "string", enum: ["todo", "in_progress", "done", "blocked"] },
          assigned_to: { type: "string" },
          result: { type: "string" },
        },
        required: ["id"],
      },
    },
    {
      name: "heartbeat",
      description: "Send a heartbeat to keep agent status online",
      inputSchema: {
        type: "object",
        properties: {
          id: { type: "string" },
          working_on: { type: "string" },
        },
        required: ["id"],
      },
    },
    {
      name: "deregister",
      description: "Mark this agent as offline",
      inputSchema: {
        type: "object",
        properties: { id: { type: "string" } },
        required: ["id"],
      },
    },
    {
      name: "agents",
      description: "Alias for list_agents",
      inputSchema: { type: "object", properties: {} },
    },
    {
      name: "threads",
      description: "List all known thread IDs",
      inputSchema: { type: "object", properties: {} },
    },
    {
      name: "get_topology",
      description: "Get the current agent/room topology graph",
      inputSchema: { type: "object", properties: {} },
    },
    {
      name: "execute_tool",
      description: "Execute a registered AgentBridge tool",
      inputSchema: {
        type: "object",
        properties: {
          tool: { type: "string" },
          args: { type: "object" },
        },
        required: ["tool"],
      },
    },
    ...listTools().map((tool) => ({
      name: tool.name,
      description: tool.description,
      inputSchema: { type: "object" },
    })),
  ];

  return { tools };
});

server.setRequestHandler(CallToolRequestSchema, async (request) => {
  const { name, arguments: args } = request.params;
  const a = (args ?? {}) as Record<string, unknown>;

  const text = (obj: unknown) => JSON.stringify(obj, null, 2);

  switch (name) {
    case "register": {
      const agent = store.upsertAgent({
        id: a.id as string,
        name: a.name as string,
        role: a.role as string,
        status: "online",
        working_on: (a.working_on as string | undefined),
        meta: {},
      });
      return { content: [{ type: "text", text: `Registered as ${agent.name} (${agent.role})` }] };
    }

    case "agents":
    case "list_agents":
      return { content: [{ type: "text", text: text(store.listAgents()) }] };

    case "get_agent": {
      const agent = store.getAgent(a.id as string);
      if (!agent) throw new Error(`Agent '${a.id}' not found`);
      return { content: [{ type: "text", text: text(agent) }] };
    }

    case "update_status":
    case "heartbeat": {
      const id = a.id as string;
      const status = (a.status as "online" | "busy" | "offline" | undefined) ?? "online";
      const working_on = a.working_on as string | undefined;
      store.updateAgentStatus(id, status, working_on);
      return { content: [{ type: "text", text: `${id} [${status}]${working_on ? ` — working on: ${working_on}` : ""}` }] };
    }

    case "deregister": {
      store.updateAgentStatus(a.id as string, "offline");
      return { content: [{ type: "text", text: `${a.id} [offline]` }] };
    }

    case "send": {
      const msg = store.addMessage({
        protocol: "A2A/1.0",
        sender: a.sender as string,
        receiver: a.receiver as string | undefined,
        room_id: a.room_id as string | undefined,
        thread_id: (a.thread_id as string | undefined) ?? `mcp-${Date.now()}`,
        message_type: (a.message_type as "discussion" | "task_request" | "task_result" | "event" | "tool_call" | undefined) ?? "discussion",
        content: a.content as string,
        timestamp: Date.now(),
      });
      return { content: [{ type: "text", text: `Sent: ${msg.content} [MSG-${msg.id.slice(0, 8)}]` }] };
    }

    case "read": {
      if (a.thread_id) {
        const ctx = store.getContext(a.thread_id as string);
        return { content: [{ type: "text", text: text(ctx.messages) }] };
      }
      const msgs = store.getMessages({
        sinceId: a.since_id as string | undefined,
        limit: (a.limit as number | undefined) ?? 20,
      });
      return { content: [{ type: "text", text: msgs.map((m) => `${m.sender}: ${m.content}`).join("\n") || "(no messages)" }] };
    }

    case "get_inbox": {
      const inbox = store.getInbox(a.agent_id as string);
      const filtered = a.unread_only ? inbox.filter((m) => !m.read) : inbox;
      return { content: [{ type: "text", text: text({ unread_count: filtered.filter((m) => !m.read).length, messages: filtered }) }] };
    }

    case "ack": {
      const ok = store.markInboxRead(a.agent_id as string, a.message_id as string);
      return { content: [{ type: "text", text: ok ? "Message marked as read" : "Message not found" }] };
    }

    case "request_agent": {
      const pending = store.createRequest({
        sender: a.sender as string,
        recipient: a.recipient as string,
        content: a.content as string,
        timeoutMs: (a.timeout_ms as number | undefined) ?? 60_000,
      });
      store.addMessage({
        protocol: "A2A/1.0",
        sender: a.sender as string,
        receiver: a.recipient as string,
        thread_id: `req-${pending.requestId}`,
        message_type: "task_request",
        content: a.content as string,
        timestamp: Date.now(),
        correlationId: pending.requestId,
      });
      // poll for response
      const deadline = Date.now() + pending.timeoutMs;
      while (Date.now() < deadline) {
        await new Promise((r) => setTimeout(r, 500));
        const current = store.getRequest(pending.requestId);
        if (current?.resolved && current.response) {
          return { content: [{ type: "text", text: current.response.content }] };
        }
      }
      return { content: [{ type: "text", text: `Request timed out — ${a.recipient} did not respond within ${pending.timeoutMs / 1000}s` }] };
    }

    case "respond": {
      const req = store.getRequest(a.request_id as string);
      if (!req) throw new Error(`Request '${a.request_id}' not found`);
      const responseMessage = store.addMessage({
        protocol: "A2A/1.0",
        sender: a.sender as string,
        receiver: req.sender,
        thread_id: `req-${a.request_id}`,
        message_type: "task_result",
        content: a.content as string,
        timestamp: Date.now(),
        correlationId: a.request_id as string,
      });
      store.resolveRequest(a.request_id as string, responseMessage);
      return { content: [{ type: "text", text: `Responded to REQ-${(a.request_id as string).slice(0, 8)}` }] };
    }

    case "list_rooms":
      return { content: [{ type: "text", text: text(store.listRooms()) }] };

    case "create_room": {
      const room = store.createRoom(a.name as string, (a.agents as string[] | undefined) ?? []);
      if (!room) throw new Error(`Room '${a.name}' already exists. Use join_room to join it.`);
      return { content: [{ type: "text", text: `Room created: ${room.name} (${room.id})` }] };
    }

    case "join_room": {
      const room = store.addAgentToRoom(a.room_id as string, a.agent_id as string);
      if (!room) throw new Error(`Room '${a.room_id}' not found`);
      return { content: [{ type: "text", text: `Joined room: ${room.name}` }] };
    }

    case "get_context": {
      const threadId = a.thread_id as string;
      if (!threadId) throw new Error("thread_id is required");
      return { content: [{ type: "text", text: text(store.getContext(threadId)) }] };
    }

    case "threads": {
      // We don't store thread IDs separately, derive from all messages
      const allMsgs = store.getMessages({ limit: 1000 });
      const threadIds = [...new Set(allMsgs.map((m) => m.thread_id))];
      return { content: [{ type: "text", text: threadIds.map((t) => `#${t}`).join("\n") || "(no threads)" }] };
    }

    case "list_tasks": {
      let tasks = store.listTasks(a.room_id as string | undefined);
      if (a.assigned_to) tasks = tasks.filter((t) => t.assignedTo === (a.assigned_to as string));
      if (a.status) tasks = tasks.filter((t) => t.status === (a.status as string));
      return { content: [{ type: "text", text: text(tasks) }] };
    }

    case "create_task": {
      const task = store.createTask({
        title: a.title as string,
        description: a.description as string | undefined,
        assignedTo: a.assigned_to as string | undefined,
        roomId: a.room_id as string | undefined,
        status: "todo",
      });
      return { content: [{ type: "text", text: `Task created: TASK-${task.id.slice(0, 8)} — ${task.title}` }] };
    }

    case "update_task": {
      const task = store.getTask(a.id as string);
      if (!task) throw new Error(`Task '${a.id}' not found`);
      const updated = store.updateTask(a.id as string, {
        status: a.status as "todo" | "in_progress" | "done" | "blocked" | undefined,
        assignedTo: a.assigned_to as string | undefined,
        result: a.result as string | undefined,
      });
      return { content: [{ type: "text", text: `TASK-${(a.id as string).slice(0, 8)} → [${updated?.status}]` }] };
    }

    case "get_topology":
      return { content: [{ type: "text", text: text(store.getTopology()) }] };

    case "execute_tool": {
      const { tool, args: toolArgs } = a as { tool?: string; args?: Record<string, unknown> };
      if (!tool) throw new Error("tool is required");
      const result = await executeTool(tool, toolArgs ?? {});
      return { content: [{ type: "text", text: text(result) }] };
    }

    default: {
      const result = await executeTool(name, a);
      return { content: [{ type: "text", text: text(result) }] };
    }
  }
});

const transport = new StdioServerTransport();
await server.connect(transport);
