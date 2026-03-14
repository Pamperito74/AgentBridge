import { Server } from "@modelcontextprotocol/sdk/server/index.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import {
  CallToolRequestSchema,
  ListToolsRequestSchema,
} from "@modelcontextprotocol/sdk/types.js";
import { AgentBridgeStore } from "../core/store.js";
import { executeTool, listTools } from "../core/tools.js";

const store = new AgentBridgeStore();
await store.load();

const server = new Server(
  {
    name: "agentbridge-mcp",
    version: "0.1.0",
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
      name: "list_agents",
      description: "List agents known to AgentBridge",
      inputSchema: { type: "object", properties: {} },
    },
    {
      name: "list_rooms",
      description: "List AgentBridge rooms",
      inputSchema: { type: "object", properties: {} },
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

  if (name === "list_agents") {
    return {
      content: [{ type: "text", text: JSON.stringify(store.listAgents(), null, 2) }],
    };
  }

  if (name === "list_rooms") {
    return {
      content: [{ type: "text", text: JSON.stringify(store.listRooms(), null, 2) }],
    };
  }

  if (name === "get_context") {
    const threadId = (args as { thread_id?: string })?.thread_id;
    if (!threadId) {
      throw new Error("thread_id is required");
    }
    return {
      content: [{ type: "text", text: JSON.stringify(store.getContext(threadId), null, 2) }],
    };
  }

  if (name === "execute_tool") {
    const { tool, args: toolArgs } = (args as { tool?: string; args?: Record<string, unknown> }) ?? {};
    if (!tool) {
      throw new Error("tool is required");
    }
    const result = await executeTool(tool, toolArgs ?? {});
    return {
      content: [{ type: "text", text: JSON.stringify(result, null, 2) }],
    };
  }

  const result = await executeTool(name, (args as Record<string, unknown>) ?? {});
  return {
    content: [{ type: "text", text: JSON.stringify(result, null, 2) }],
  };
});

const transport = new StdioServerTransport();
await server.connect(transport);
