import { z } from "zod";

export const agentStatusSchema = z.enum(["online", "offline", "busy"]);

export const registerAgentSchema = z.object({
  id: z.string().min(1),
  name: z.string().min(1),
  role: z.string().min(1),
  status: agentStatusSchema.optional(),
  meta: z.record(z.string(), z.unknown()).optional(),
});

export const patchAgentStatusSchema = z.object({
  status: agentStatusSchema.optional(),
  working_on: z.string().optional(),
});

export const createRoomSchema = z.object({
  name: z.string().min(1),
  agents: z.array(z.string()).optional(),
});

export const messageTypeSchema = z.enum([
  "discussion",
  "task_request",
  "task_result",
  "event",
  "tool_call",
]);

export const roomMessageSchema = z.object({
  sender: z.string().min(1),
  content: z.string().min(1),
  thread_id: z.string().optional(),
  receiver: z.string().optional(),
});

export const createTaskSchema = z.object({
  title: z.string().min(1),
  description: z.string().optional(),
  assignedTo: z.string().optional(),
  roomId: z.string().optional(),
  status: z.enum(["todo", "in_progress", "done", "blocked"]).optional(),
});

export const patchTaskSchema = z.object({
  status: z.enum(["todo", "in_progress", "done", "blocked"]).optional(),
  assignedTo: z.string().optional(),
  result: z.string().optional(),
});

export const planGoalSchema = z.object({
  goal: z.string().min(1),
});

export const executeToolSchema = z.object({
  tool: z.string().min(1),
  args: z.record(z.string(), z.unknown()).optional(),
});

export const a2aMessageSchema = z.object({
  protocol: z.literal("A2A/1.0"),
  sender: z.string().min(1),
  receiver: z.string().optional(),
  room_id: z.string().optional(),
  thread_id: z.string().min(1),
  message_type: messageTypeSchema,
  content: z.string().min(1),
  timestamp: z.number().optional(),
  correlationId: z.string().optional(),
});

export const requestMessageSchema = z.object({
  sender: z.string().min(1),
  recipient: z.string().min(1),
  content: z.string().min(1),
  timeout_ms: z.number().min(1000).max(120_000).optional(),
});

export const respondMessageSchema = z.object({
  requestId: z.string().min(1),
  sender: z.string().min(1),
  content: z.string().min(1),
});

export const ackSchema = z.object({
  messageId: z.string().min(1),
});
