import { nanoid } from "nanoid";

export interface ToolResult {
  id: string;
  tool: string;
  input: Record<string, unknown>;
  output: Record<string, unknown> | string;
  timestamp: number;
}

export type ToolHandler = (args: Record<string, unknown>) => Promise<ToolResult>;

const registry = new Map<string, ToolHandler>();

function register(toolName: string, handler: ToolHandler) {
  registry.set(toolName, handler);
}

register("deploy_service", async (args) => ({
  id: nanoid(),
  tool: "deploy_service",
  input: args,
  output: {
    status: "queued",
    env: args.env ?? "staging",
    message: "Deployment scheduled by AgentBridge mock tool.",
  },
  timestamp: Date.now(),
}));

register("generate_report", async (args) => ({
  id: nanoid(),
  tool: "generate_report",
  input: args,
  output: {
    status: "complete",
    summary: "Mock report generated.",
  },
  timestamp: Date.now(),
}));

export async function executeTool(toolName: string, args: Record<string, unknown>) {
  const handler = registry.get(toolName);
  if (!handler) {
    return {
      error: `Unknown tool: ${toolName}`,
      available: [...registry.keys()],
    };
  }
  return handler(args);
}

export function listTools() {
  return [...registry.keys()].map((name) => ({
    name,
    description: name === "deploy_service"
      ? "Queue a deployment to a target environment"
      : "Generate a mock report",
  }));
}
