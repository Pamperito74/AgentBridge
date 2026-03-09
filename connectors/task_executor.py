#!/usr/bin/env python3
"""
AgentBridge Task Executor Connector
====================================
Registers as an agent on AgentBridge, polls for task requests,
executes them locally, and posts results back to the sender.

Supported task types
--------------------
  shell       — run an arbitrary shell command
  git_commit  — git add -A && git commit -m "..."
  git_pr      — gh pr create --title "..." --body "..."
  aider       — run aider with a prompt against specified files
  test        — run a test command (npm test, pytest, etc.)

Usage
-----
  python connectors/task_executor.py

  # Optional env vars
  AGENTBRIDGE_URL=http://localhost:7890   (default)
  EXECUTOR_NAME=task-executor            (default)
  EXECUTOR_POLL_INTERVAL=3               (seconds, default)
  AIDER_MODEL=ollama/qwen2.5-coder:7b   (default)

Sending a task from any Claude Code session
-------------------------------------------
  /bridge send --to task-executor '{"type":"shell","command":"npm test","cwd":"/project"}'
  /bridge send --to task-executor '{"type":"git_commit","message":"fix: ...","cwd":"/project"}'
  /bridge send --to task-executor '{"type":"git_pr","title":"fix: ...","body":"...","cwd":"/project"}'
  /bridge send --to task-executor '{"type":"aider","prompt":"refactor X","files":["src/x.ts"],"cwd":"/project"}'
  /bridge send --to task-executor '{"type":"test","command":"pytest -x","cwd":"/project"}'
"""

import json
import os
import subprocess
import sys
import threading
import time

import requests

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
BRIDGE = os.environ.get("AGENTBRIDGE_URL", "http://localhost:7890")
AGENT_NAME = os.environ.get("EXECUTOR_NAME", "task-executor")
POLL_INTERVAL = int(os.environ.get("EXECUTOR_POLL_INTERVAL", "3"))
AIDER_MODEL = os.environ.get("AIDER_MODEL", "ollama/qwen2.5-coder:7b")
HEARTBEAT_INTERVAL = 30  # seconds — well within the 4h agent TTL

# Maximum bytes captured from stdout / stderr to avoid flooding the bridge
STDOUT_CAP = 4000
STDERR_CAP = 1000
SHELL_TIMEOUT = 120  # seconds

CAPABILITIES = ["shell", "git_commit", "git_pr", "aider", "test"]

# Cursor: ID of the last message we processed (persisted in memory only).
# AgentBridge's delivery_cursors table tracks this server-side via since_id.
_last_message_id: str | None = None


# ---------------------------------------------------------------------------
# Agent lifecycle
# ---------------------------------------------------------------------------

def register() -> None:
    """Register this executor as an agent on AgentBridge."""
    try:
        r = requests.post(
            f"{BRIDGE}/agents",
            json={"name": AGENT_NAME, "role": "executor", "capabilities": CAPABILITIES},
            timeout=5,
        )
        r.raise_for_status()
        print(f"[executor] Registered as '{AGENT_NAME}' on {BRIDGE}")
    except requests.RequestException as exc:
        print(f"[executor] ERROR: could not register — is AgentBridge running? ({exc})")
        sys.exit(1)


def _heartbeat_loop() -> None:
    """Background thread: send heartbeat every HEARTBEAT_INTERVAL seconds."""
    while True:
        time.sleep(HEARTBEAT_INTERVAL)
        try:
            requests.post(
                f"{BRIDGE}/agents/{AGENT_NAME}/heartbeat",
                json={"status": "online", "working_on": ""},
                timeout=5,
            )
        except requests.RequestException:
            pass  # server may be momentarily unavailable; will retry


def start_heartbeat() -> None:
    t = threading.Thread(target=_heartbeat_loop, daemon=True)
    t.start()


def poll_messages() -> list[dict]:
    """Return only *new* messages using the since_id cursor to prevent reprocessing."""
    global _last_message_id
    params: dict = {"as_agent": AGENT_NAME, "limit": 20}
    if _last_message_id:
        params["since_id"] = _last_message_id
    try:
        r = requests.get(f"{BRIDGE}/messages", params=params, timeout=5)
        r.raise_for_status()
        # Response is a list directly (not wrapped in {"messages": ...})
        raw = r.json()
        messages = raw if isinstance(raw, list) else raw.get("messages", [])
        if messages:
            _last_message_id = messages[-1]["id"]
        return messages
    except requests.RequestException:
        return []


def reply(sender: str, result: dict, correlation_id: str | None = None) -> None:
    """Post the task result back to the requesting agent.

    Forwards correlation_id and sets msg_type='response' so that synchronous
    callers using AgentBridge's request-response pattern (ab request / MCP
    request tool) get unblocked immediately instead of timing out.
    """
    payload: dict = {
        "sender": AGENT_NAME,
        "recipient": sender,
        "content": json.dumps(result, ensure_ascii=False),
        "msg_type": "response",
    }
    if correlation_id:
        payload["correlation_id"] = correlation_id
    try:
        requests.post(f"{BRIDGE}/messages", json=payload, timeout=5)
    except requests.RequestException as exc:
        print(f"[executor] WARNING: could not send reply to '{sender}': {exc}")


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------

def handle(msg: dict) -> None:
    # Only process chat/request messages — skip status, alert, response, etc.
    msg_type = msg.get("msg_type", "chat")
    if msg_type not in ("chat", "request"):
        return

    sender = msg.get("sender", "unknown")
    correlation_id = msg.get("correlation_id")
    raw = msg.get("content", "")

    # Must be a directed message (recipient set) — ignore broadcasts/channel chat
    if msg.get("recipient") != AGENT_NAME:
        return

    try:
        task = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        # Only reply with error if it looks intentional (has a recipient set)
        reply(sender, {"success": False, "error": "Payload must be valid JSON"},
              correlation_id=correlation_id)
        return

    # Must have a "type" key — otherwise it's not a task payload
    if "type" not in task:
        return

    task_type = task.get("type", "")
    print(f"[executor] Task '{task_type}' from '{sender}'" +
          (f" [corr={correlation_id[:8]}]" if correlation_id else ""))

    result = dispatch(task_type, task)
    reply(sender, result, correlation_id=correlation_id)


def dispatch(task_type: str, task: dict) -> dict:
    handlers = {
        "shell": _task_shell,
        "git_commit": _task_git_commit,
        "git_pr": _task_git_pr,
        "aider": _task_aider,
        "test": _task_test,
    }
    handler = handlers.get(task_type)
    if handler is None:
        return {
            "success": False,
            "error": f"Unknown task type '{task_type}'. Supported: {list(handlers)}",
        }
    try:
        return handler(task)
    except Exception as exc:  # noqa: BLE001
        return {"success": False, "error": f"Unhandled exception: {exc}"}


# ---------------------------------------------------------------------------
# Task handlers
# ---------------------------------------------------------------------------

def _run(cmd: str, cwd: str) -> dict:
    """Run a shell command and return a structured result."""
    try:
        proc = subprocess.run(
            cmd,
            shell=True,
            cwd=cwd or ".",
            capture_output=True,
            text=True,
            timeout=SHELL_TIMEOUT,
        )
        return {
            "success": proc.returncode == 0,
            "exit_code": proc.returncode,
            "stdout": proc.stdout[-STDOUT_CAP:],
            "stderr": proc.stderr[-STDERR_CAP:],
        }
    except subprocess.TimeoutExpired:
        return {"success": False, "error": f"Command timed out after {SHELL_TIMEOUT}s"}
    except FileNotFoundError as exc:
        return {"success": False, "error": str(exc)}


def _task_shell(task: dict) -> dict:
    command = task.get("command")
    if not command:
        return {"success": False, "error": "'command' is required for type 'shell'"}
    return _run(command, task.get("cwd", "."))


def _task_git_commit(task: dict) -> dict:
    message = task.get("message")
    if not message:
        return {"success": False, "error": "'message' is required for type 'git_commit'"}
    # Escape double-quotes in the commit message
    safe_msg = message.replace('"', '\\"')
    return _run(f'git add -A && git commit -m "{safe_msg}"', task.get("cwd", "."))


def _task_git_pr(task: dict) -> dict:
    title = task.get("title")
    body = task.get("body", "")
    if not title:
        return {"success": False, "error": "'title' is required for type 'git_pr'"}
    safe_title = title.replace('"', '\\"')
    safe_body = body.replace('"', '\\"')
    cmd = f'gh pr create --title "{safe_title}" --body "{safe_body}"'
    if task.get("base"):
        cmd += f' --base {task["base"]}'
    return _run(cmd, task.get("cwd", "."))


def _task_aider(task: dict) -> dict:
    prompt = task.get("prompt")
    if not prompt:
        return {"success": False, "error": "'prompt' is required for type 'aider'"}
    files = task.get("files", [])
    model = task.get("model", AIDER_MODEL)
    safe_prompt = prompt.replace('"', '\\"')
    files_arg = " ".join(files)
    cmd = f'aider --model {model} --message "{safe_prompt}" {files_arg} --yes --no-git'
    return _run(cmd, task.get("cwd", "."))


def _task_test(task: dict) -> dict:
    command = task.get("command", "npm test")
    return _run(command, task.get("cwd", "."))


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def main() -> None:
    register()
    start_heartbeat()
    print(f"[executor] Polling every {POLL_INTERVAL}s — send tasks via AgentBridge to '{AGENT_NAME}'")
    print(f"[executor] Heartbeat every {HEARTBEAT_INTERVAL}s")
    print("[executor] Ctrl-C to stop\n")

    while True:
        messages = poll_messages()
        for msg in messages:
            handle(msg)
        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n[executor] Stopped.")
