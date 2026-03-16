import type { WebSocket } from "ws";
import type { A2AMessage } from "./types.js";

export class AgentRouter {
  private sockets = new Map<string, WebSocket>();

  register(agentId: string, socket: WebSocket) {
    this.sockets.set(agentId, socket);
  }

  unregister(agentId: string) {
    this.sockets.delete(agentId);
  }

  sendToAgent(agentId: string, message: A2AMessage) {
    const socket = this.sockets.get(agentId);
    if (!socket || socket.readyState !== socket.OPEN) {
      return false;
    }
    socket.send(JSON.stringify(message));
    return true;
  }

  broadcast(message: A2AMessage) {
    for (const socket of this.sockets.values()) {
      if (socket.readyState === socket.OPEN) {
        socket.send(JSON.stringify(message));
      }
    }
  }

  /** Broadcast to all agents except the sender (echo suppression). */
  broadcastExcept(excludeAgentId: string, message: A2AMessage) {
    for (const [agentId, socket] of this.sockets.entries()) {
      if (agentId !== excludeAgentId && socket.readyState === socket.OPEN) {
        socket.send(JSON.stringify(message));
      }
    }
  }
}
