import { test } from "node:test";
import assert from "node:assert/strict";
import {
  registerAgentSchema,
  createRoomSchema,
  roomMessageSchema,
  createTaskSchema,
  a2aMessageSchema,
} from "../src/server/validation.js";

test("registerAgentSchema: accepts valid agent", () => {
  const result = registerAgentSchema.safeParse({
    id: "agent-1",
    name: "CTO Agent",
    role: "cto",
  });
  assert.ok(result.success);
  assert.equal(result.data?.status, undefined);
});

test("registerAgentSchema: rejects missing required fields", () => {
  const result = registerAgentSchema.safeParse({ id: "a" });
  assert.ok(!result.success);
});

test("registerAgentSchema: rejects invalid status", () => {
  const result = registerAgentSchema.safeParse({
    id: "a",
    name: "Agent",
    role: "role",
    status: "unknown",
  });
  assert.ok(!result.success);
});

test("createRoomSchema: accepts valid room", () => {
  const result = createRoomSchema.safeParse({ name: "Engineering", agents: ["cto"] });
  assert.ok(result.success);
  assert.equal(result.data?.name, "Engineering");
});

test("createRoomSchema: requires name", () => {
  const result = createRoomSchema.safeParse({ agents: [] });
  assert.ok(!result.success);
});

test("roomMessageSchema: accepts valid message", () => {
  const result = roomMessageSchema.safeParse({
    sender: "cto_agent",
    content: "Deploy now",
  });
  assert.ok(result.success);
});

test("roomMessageSchema: rejects empty content", () => {
  const result = roomMessageSchema.safeParse({ sender: "a", content: "" });
  assert.ok(!result.success);
});

test("createTaskSchema: accepts valid task", () => {
  const result = createTaskSchema.safeParse({ title: "Ship v1" });
  assert.ok(result.success);
});

test("createTaskSchema: rejects empty title", () => {
  const result = createTaskSchema.safeParse({ title: "" });
  assert.ok(!result.success);
});

test("a2aMessageSchema: accepts valid message", () => {
  const result = a2aMessageSchema.safeParse({
    protocol: "A2A/1.0",
    sender: "agent-a",
    thread_id: "thread-1",
    message_type: "discussion",
    content: "Hello",
  });
  assert.ok(result.success);
});

test("a2aMessageSchema: rejects wrong protocol", () => {
  const result = a2aMessageSchema.safeParse({
    protocol: "HTTP/1.1",
    sender: "agent-a",
    thread_id: "t1",
    message_type: "discussion",
    content: "x",
  });
  assert.ok(!result.success);
});

test("a2aMessageSchema: rejects invalid message_type", () => {
  const result = a2aMessageSchema.safeParse({
    protocol: "A2A/1.0",
    sender: "agent-a",
    thread_id: "t1",
    message_type: "unknown_type",
    content: "x",
  });
  assert.ok(!result.success);
});
