import { AgentBridgeStore } from "../src/core/store.js";

const store = new AgentBridgeStore();
await store.load();

store.upsertAgent({ id: "ceo_agent", name: "CEO", role: "Executive", status: "online" });
store.upsertAgent({ id: "cto_agent", name: "CTO", role: "Engineering", status: "online" });
store.upsertAgent({ id: "devops_agent", name: "DevOps", role: "Infrastructure", status: "online" });
store.upsertAgent({ id: "security_agent", name: "Security", role: "Security", status: "online" });

const room = store.createRoom("Engineering Room", ["cto_agent", "devops_agent", "security_agent"]);
store.addMessage({
  protocol: "A2A/1.0",
  sender: "cto_agent",
  room_id: room.id,
  thread_id: `room-${room.id}`,
  message_type: "discussion",
  content: "Kickoff: align on blue-green deployment.",
  timestamp: Date.now(),
});

console.log("Seed complete.");
