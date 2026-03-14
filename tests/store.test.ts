import { test } from "node:test";
import assert from "node:assert/strict";
import { AgentBridgeStore } from "../src/core/store.js";

test("store creates rooms and tasks", () => {
  const store = new AgentBridgeStore();
  const room = store.createRoom("Engineering", ["cto_agent"]);
  assert.equal(room.id, "engineering");

  const task = store.createTask({
    title: "Ship A2A routing",
    status: "todo",
    roomId: room.id,
  });
  assert.ok(task.id);
  assert.equal(store.listTasks(room.id).length, 1);
});

test("store records thread messages", () => {
  const store = new AgentBridgeStore();
  store.addMessage({
    protocol: "A2A/1.0",
    sender: "cto_agent",
    receiver: "devops_agent",
    thread_id: "thread_1",
    message_type: "discussion",
    content: "Deploy with blue-green",
    timestamp: Date.now(),
  });

  const context = store.getContext("thread_1");
  assert.equal(context.messages.length, 1);
});
