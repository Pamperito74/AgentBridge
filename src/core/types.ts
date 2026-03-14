export type AgentStatus = "online" | "offline" | "busy";

export interface Agent {
  id: string;
  name: string;
  role: string;
  status: AgentStatus;
  meta?: Record<string, unknown>;
  lastSeen: number;
}

export interface Room {
  id: string;
  name: string;
  agents: string[];
  activeTasks: number;
  createdAt: number;
}

export type MessageType =
  | "discussion"
  | "task_request"
  | "task_result"
  | "event"
  | "tool_call";

export interface A2AMessage {
  protocol: "A2A/1.0";
  sender: string;
  receiver?: string;
  room_id?: string;
  thread_id: string;
  message_type: MessageType;
  content: string;
  timestamp: number;
}

export type TaskStatus = "todo" | "in_progress" | "done" | "blocked";

export interface Task {
  id: string;
  title: string;
  description?: string;
  status: TaskStatus;
  assignedTo?: string;
  roomId?: string;
  createdAt: number;
  updatedAt: number;
}

export interface TopologyNode {
  id: string;
  type: "agent" | "room";
}

export interface TopologyEdge {
  from: string;
  to: string;
}

export interface TopologyGraph {
  nodes: TopologyNode[];
  edges: TopologyEdge[];
}
