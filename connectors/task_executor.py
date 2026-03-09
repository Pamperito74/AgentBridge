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
  EXECUTOR_MAX_WORKERS=4                 (parallel tasks, default)
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
from concurrent.futures import ThreadPoolExecutor

import requests

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
BRIDGE = os.environ.get("AGENTBRIDGE_URL", "http://localhost:7890")
AGENT_NAME = os.environ.get("EXECUTOR_NAME", "task-executor")
POLL_INTERVAL = int(os.environ.get("EXECUTOR_POLL_INTERVAL", "3"))
AIDER_MODEL = os.environ.get("AIDER_MODEL", "ollama/qwen2.5-coder:7b")
HEARTBEAT_INTERVAL = 30  # seconds — well within the 4h agent TTL
MAX_WORKERS = int(os.environ.get("EXECUTOR_MAX_WORKERS", "4"))

# Maximum bytes captured from stdout / stderr to avoid flooding the bridge
STDOUT_CAP = 4000
STDERR_CAP = 1000
SHELL_TIMEOUT = 120  # seconds

CAPABILITIES = ["shell", "git_commit", "git_pr", "aider", "gemini", "test"]

# Cursor: ID of the last message we processed (persisted in memory only).
# AgentBridge's delivery_cursors table tracks this server-side via since_id.
_last_message_id: str | None = None

# Thread pool for parallel task execution.
_pool = ThreadPoolExecutor(max_workers=MAX_WORKERS)

# Active task counter — used to report "busy" status in heartbeats.
_active_tasks = 0
_active_lock = threading.Lock()


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
    """Background thread: send heartbeat every HEARTBEAT_INTERVAL seconds.

    Reports 'busy' with a task count when work is in flight so other agents
    (and the dashboard) can see the executor is actively running tasks.
    """
    while True:
        time.sleep(HEARTBEAT_INTERVAL)
        with _active_lock:
            count = _active_tasks
        status = "busy" if count > 0 else "online"
        working_on = f"{count}/{MAX_WORKERS} tasks running" if count > 0 else ""
        try:
            requests.post(
                f"{BRIDGE}/agents/{AGENT_NAME}/heartbeat",
                json={"status": status, "working_on": working_on},
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
    """Validate the message and dispatch to the thread pool.

    Runs in the main poll loop — must return quickly. Heavy work happens
    inside _execute(), which the pool runs in a worker thread.
    """
    # Only process chat/request messages — skip status, alert, response, etc.
    msg_type = msg.get("msg_type", "chat")
    if msg_type not in ("chat", "request"):
        return

    # Must be a directed message — ignore broadcasts/channel chat
    if msg.get("recipient") != AGENT_NAME:
        return

    sender = msg.get("sender", "unknown")
    correlation_id = msg.get("correlation_id")
    raw = msg.get("content", "")

    try:
        task = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        reply(sender, {"success": False, "error": "Payload must be valid JSON"},
              correlation_id=correlation_id)
        return

    # Must have a "type" key — otherwise it's not a task payload
    if "type" not in task:
        return

    task_type = task.get("type", "")
    print(f"[executor] Queuing task '{task_type}' from '{sender}'" +
          (f" [corr={correlation_id[:8]}]" if correlation_id else "") +
          f" (pool: {_active_tasks}/{MAX_WORKERS} active)")

    _pool.submit(_execute, sender, task_type, task, correlation_id)


def _execute(sender: str, task_type: str, task: dict, correlation_id: str | None) -> None:
    """Run a task in a worker thread, track active count, reply when done."""
    global _active_tasks
    with _active_lock:
        _active_tasks += 1
    try:
        result = dispatch(task_type, task)
        print(f"[executor] Done '{task_type}' from '{sender}' — "
              f"{'ok' if result.get('success') else 'FAILED'}")
    except Exception as exc:  # noqa: BLE001
        result = {"success": False, "error": f"Unhandled exception: {exc}"}
    finally:
        with _active_lock:
            _active_tasks -= 1
    reply(sender, result, correlation_id=correlation_id)


def dispatch(task_type: str, task: dict) -> dict:
    handlers = {
        "shell": _task_shell,
        "git_commit": _task_git_commit,
        "git_pr": _task_git_pr,
        "aider": _task_aider,
        "gemini": _task_gemini,
        "test": _task_test,
    }
    handler = handlers.get(task_type)
    if handler is None:
        return {
            "success": False,
            "error": f"Unknown task type '{task_type}'. Supported: {list(handlers)}",
        }
    return handler(task)


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


def _task_gemini(task: dict) -> dict:
    """Run a prompt through the Gemini CLI (free tier: 1,500 req/day, 1M context).

    If 'files' is provided, each file's content is injected into the prompt
    before the question — Gemini's 1M-token context handles large codebases.

    Prerequisites:
      npm install -g @google/generative-ai-cli   (or: pip install gemini-cli)
      gemini auth login

    Optional fields:
      files   — list of file paths to include as context (relative to cwd)
      model   — Gemini model name (default: gemini-2.0-flash)
    """
    prompt = task.get("prompt")
    if not prompt:
        return {"success": False, "error": "'prompt' is required for type 'gemini'"}

    files = task.get("files", [])
    model = task.get("model", "gemini-2.0-flash")
    cwd = task.get("cwd", ".")

    # Build a single stdin payload: file contents (labelled) + the prompt.
    # This avoids shell-quoting issues with large file contents.
    if files:
        file_blocks = []
        for path in files:
            # Paths are relative to cwd
            abs_path = path if os.path.isabs(path) else os.path.join(cwd, path)
            try:
                with open(abs_path, encoding="utf-8", errors="replace") as fh:
                    content = fh.read()
                file_blocks.append(f"=== {path} ===\n{content}")
            except OSError as exc:
                return {"success": False, "error": f"Cannot read file '{path}': {exc}"}
        stdin_payload = "\n\n".join(file_blocks) + f"\n\n{prompt}"
    else:
        stdin_payload = prompt

    # Pipe the payload into `gemini` via stdin so we never hit shell quoting limits.
    try:
        proc = subprocess.run(
            ["gemini", f"--model={model}", "-p", "-"],
            input=stdin_payload,
            capture_output=True,
            text=True,
            cwd=cwd or ".",
            timeout=SHELL_TIMEOUT,
        )
        return {
            "success": proc.returncode == 0,
            "exit_code": proc.returncode,
            "stdout": proc.stdout[-STDOUT_CAP:],
            "stderr": proc.stderr[-STDERR_CAP:],
        }
    except subprocess.TimeoutExpired:
        return {"success": False, "error": f"gemini timed out after {SHELL_TIMEOUT}s"}
    except FileNotFoundError:
        return {
            "success": False,
            "error": "gemini CLI not found. Install: npm install -g @google/generative-ai-cli",
        }


def _task_test(task: dict) -> dict:
    command = task.get("command", "npm test")
    return _run(command, task.get("cwd", "."))


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def main() -> None:
    register()
    start_heartbeat()
    print(f"[executor] Registered as '{AGENT_NAME}' — parallel executor")
    print(f"[executor] Workers: {MAX_WORKERS} | Poll: {POLL_INTERVAL}s | Heartbeat: {HEARTBEAT_INTERVAL}s")
    print(f"[executor] Set EXECUTOR_MAX_WORKERS to change concurrency (current: {MAX_WORKERS})")
    print("[executor] Ctrl-C to stop\n")

    try:
        while True:
            messages = poll_messages()
            for msg in messages:
                handle(msg)
            time.sleep(POLL_INTERVAL)
    except KeyboardInterrupt:
        pass
    finally:
        print("\n[executor] Shutting down — waiting for in-flight tasks to complete...")
        _pool.shutdown(wait=True)
        print("[executor] All tasks done. Stopped.")


if __name__ == "__main__":
    main()
