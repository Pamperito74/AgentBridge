#!/usr/bin/env python3
"""
Executor Agent Connector for AgentBridge
==========================================
Model-agnostic task executor. Receives tasks from AgentBridge and runs them
via a configurable shell command. Completely independent of any specific AI —
use Claude, GPT, a Python script, bash, or anything else.

How it works
------------
1. Registers on AgentBridge with declared capabilities
2. Subscribes to the SSE stream for real-time task notifications (no polling)
3. When a task is assigned to us (or matches auto-claim rules):
   - Claims the task atomically (prevents double-execution)
   - Runs EXECUTOR_COMMAND with the task prompt as stdin
   - Sends heartbeat updates while working
   - Posts the output as a message and marks the task done
4. Falls back to HTTP polling with exponential backoff if SSE drops

Shell injection hardening
-------------------------
EXECUTOR_COMMAND is parsed with shlex.split() — it is NEVER f-string interpolated.
Task content is passed to the process via stdin ONLY, never as a shell argument.
This means `EXECUTOR_COMMAND="rm -rf /"` would run rm with no arguments — the
task content would arrive on stdin, not as an argument.

Usage
-----
  EXECUTOR_COMMAND="claude --print" python connectors/executor_agent.py

Required
  EXECUTOR_COMMAND   the command to run (receives task prompt via stdin)

Optional
  AGENTBRIDGE_URL=http://localhost:7890
  AGENTBRIDGE_TOKEN=...
  AGENT_NAME=executor
  AGENT_ROLE=Task Executor
  AGENT_CAPABILITIES=code,analysis,review    (comma-separated)
  WORK_DIR=.                                  (cwd for the command)
  TASK_TIMEOUT=600                            (seconds — hard kill after this)
  MAX_CONCURRENT=1                            (parallel tasks; 1 = serial)
  AUTO_CLAIM=true                             (claim unassigned matching tasks)
  POLL_INTERVAL=10                            (SSE fallback poll seconds)
  DEBUG=0
"""
from __future__ import annotations

import asyncio
import json
import os
import shlex
import sys
import time
from typing import Any

import requests

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

BRIDGE_URL      = os.environ.get("AGENTBRIDGE_URL", "http://localhost:7890").rstrip("/")
BRIDGE_TOKEN    = os.environ.get("AGENTBRIDGE_TOKEN", "")
AGENT_NAME      = os.environ.get("AGENT_NAME", "executor")
AGENT_ROLE      = os.environ.get("AGENT_ROLE", "Task Executor")
_caps_raw       = os.environ.get("AGENT_CAPABILITIES", "")
CAPABILITIES    = [c.strip() for c in _caps_raw.split(",") if c.strip()] if _caps_raw else []
WORK_DIR        = os.environ.get("WORK_DIR", os.getcwd())
TASK_TIMEOUT    = int(os.environ.get("TASK_TIMEOUT", "600"))
MAX_CONCURRENT  = int(os.environ.get("MAX_CONCURRENT", "1"))
AUTO_CLAIM      = os.environ.get("AUTO_CLAIM", "true").lower() == "true"
POLL_INTERVAL   = float(os.environ.get("POLL_INTERVAL", "10"))
DEBUG           = os.environ.get("DEBUG", "0") == "1"

_EXECUTOR_CMD_RAW = os.environ.get("EXECUTOR_COMMAND", "")
if not _EXECUTOR_CMD_RAW:
    print("ERROR: EXECUTOR_COMMAND is not set.", file=sys.stderr)
    print("  Example: EXECUTOR_COMMAND='claude --print' python connectors/executor_agent.py",
          file=sys.stderr)
    sys.exit(1)

# Parse once — never interpolate task content into this
EXECUTOR_CMD: list[str] = shlex.split(_EXECUTOR_CMD_RAW)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ts() -> str:
    return time.strftime("%H:%M:%S")


def _log(msg: str) -> None:
    print(f"[{_ts()}] {msg}", flush=True)


def _dbg(msg: str) -> None:
    if DEBUG:
        _log(f"[DEBUG] {msg}")


def _headers() -> dict[str, str]:
    h: dict[str, str] = {"Content-Type": "application/json"}
    if BRIDGE_TOKEN:
        h["X-AgentBridge-Token"] = BRIDGE_TOKEN
    return h


# ---------------------------------------------------------------------------
# HTTP helpers (sync — called from thread executor)
# ---------------------------------------------------------------------------


def _get(path: str, params: dict | None = None) -> Any:
    r = requests.get(f"{BRIDGE_URL}{path}", params=params, headers=_headers(), timeout=10)
    r.raise_for_status()
    return r.json()


def _post(path: str, body: dict | None = None, params: dict | None = None) -> Any:
    r = requests.post(
        f"{BRIDGE_URL}{path}", json=body, params=params, headers=_headers(), timeout=10
    )
    r.raise_for_status()
    return r.json()


def _patch(path: str, body: dict) -> Any:
    r = requests.patch(f"{BRIDGE_URL}{path}", json=body, headers=_headers(), timeout=10)
    r.raise_for_status()
    return r.json()


# ---------------------------------------------------------------------------
# AgentBridge operations (async wrappers — delegate blocking I/O to executor)
# ---------------------------------------------------------------------------


async def register() -> None:
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, lambda: _post("/agents", {
        "name": AGENT_NAME,
        "role": AGENT_ROLE,
        "capabilities": CAPABILITIES,
        "agent_type": "bot",
    }))
    cap_str = f" caps=[{', '.join(CAPABILITIES)}]" if CAPABILITIES else ""
    _log(f"Registered as '{AGENT_NAME}'{cap_str} on {BRIDGE_URL}")


async def send_heartbeat(status: str = "online", working_on: str = "") -> None:
    loop = asyncio.get_running_loop()
    try:
        await loop.run_in_executor(None, lambda: _post(
            f"/agents/{AGENT_NAME}/heartbeat",
            {"status": status, "working_on": working_on},
        ))
    except Exception as exc:
        _dbg(f"Heartbeat error: {exc}")


async def post_message(content: str, thread: str = "general", recipient: str | None = None) -> None:
    loop = asyncio.get_running_loop()
    try:
        body: dict = {"sender": AGENT_NAME, "content": content, "thread": thread}
        if recipient:
            body["recipient"] = recipient
        await loop.run_in_executor(None, lambda: _post("/messages", body))
    except Exception as exc:
        _dbg(f"Post message error: {exc}")


async def claim_task(task_id: str) -> dict | None:
    """Atomically claim a task. Returns the task dict or None if already taken."""
    loop = asyncio.get_running_loop()
    try:
        task = await loop.run_in_executor(None, lambda: _post(
            f"/tasks/{task_id}/claim", params={"agent_name": AGENT_NAME}
        ))
        return task
    except Exception as exc:
        _dbg(f"Claim failed for {task_id}: {exc}")
        return None


async def complete_task(task_id: str, note: str = "") -> None:
    loop = asyncio.get_running_loop()
    try:
        if note:
            await loop.run_in_executor(None, lambda: _patch(
                f"/tasks/{task_id}", {"description": note[:4096]}
            ))
        await loop.run_in_executor(None, lambda: _post(f"/tasks/{task_id}/complete"))
    except Exception as exc:
        _log(f"Warning: could not mark task {task_id} complete: {exc}")


async def fail_task(task_id: str, reason: str) -> None:
    loop = asyncio.get_running_loop()
    try:
        await loop.run_in_executor(None, lambda: _patch(
            f"/tasks/{task_id}", {"status": "blocked", "description": f"FAILED: {reason}"[:4096]}
        ))
    except Exception as exc:
        _dbg(f"Fail-task error for {task_id}: {exc}")


async def recover_stale_claims() -> None:
    """On startup: find tasks we claimed but never finished and reset them to todo."""
    loop = asyncio.get_running_loop()
    try:
        tasks = await loop.run_in_executor(None, lambda: _get(
            "/tasks", {"assignee": AGENT_NAME, "status": "in_progress"}
        ))
        for t in tasks if isinstance(tasks, list) else []:
            _log(f"Recovering stale claim: {t['id'][:8]} — {t['title']}")
            await loop.run_in_executor(None, lambda tid=t["id"]: _patch(
                f"/tasks/{tid}", {"status": "todo", "assignee": None}
            ))
    except Exception as exc:
        _dbg(f"Stale claim recovery error: {exc}")


# ---------------------------------------------------------------------------
# Task execution — the core loop
# ---------------------------------------------------------------------------


async def execute_task(task: dict, semaphore: asyncio.Semaphore) -> None:
    """Claim, run, and complete a single task."""
    async with semaphore:
        task_id: str = task["id"]
        title: str = task.get("title", "")
        description: str = task.get("description", "")
        thread: str = task.get("thread", "general")

        # Build the prompt that goes to the executor via stdin
        prompt = title
        if description and description != title:
            prompt = f"{title}\n\n{description}"

        claimed = await claim_task(task_id)
        if not claimed:
            _dbg(f"Skipping {task_id[:8]} — could not claim (already taken?)")
            return

        _log(f"Executing: [{task_id[:8]}] {title}")
        await send_heartbeat("busy", working_on=title[:120])
        await post_message(
            f"Starting task: {title}",
            thread=thread,
        )

        try:
            proc = await asyncio.create_subprocess_exec(
                *EXECUTOR_CMD,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                cwd=WORK_DIR,
            )

            # Stream stdout lines; send periodic heartbeat updates
            output_lines: list[str] = []
            heartbeat_every = 15  # seconds
            last_hb = asyncio.get_event_loop().time()

            async def _read_output() -> None:
                nonlocal last_hb
                assert proc.stdout is not None
                while True:
                    try:
                        line = await asyncio.wait_for(proc.stdout.readline(), timeout=1.0)
                    except asyncio.TimeoutError:
                        line = b""
                    if line:
                        decoded = line.decode(errors="replace").rstrip()
                        output_lines.append(decoded)
                        _dbg(f"  out: {decoded}")
                    now = asyncio.get_event_loop().time()
                    if now - last_hb >= heartbeat_every:
                        snippet = output_lines[-1][:80] if output_lines else "working..."
                        await send_heartbeat("busy", working_on=f"{title[:60]}: {snippet}")
                        last_hb = now
                    if proc.stdout.at_eof():
                        break

            # Feed prompt to stdin then close it, read output concurrently
            assert proc.stdin is not None
            proc.stdin.write(prompt.encode())
            await proc.stdin.drain()
            proc.stdin.close()

            try:
                await asyncio.wait_for(
                    asyncio.gather(_read_output(), proc.wait()),
                    timeout=TASK_TIMEOUT,
                )
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
                reason = f"Task timed out after {TASK_TIMEOUT}s"
                _log(f"TIMEOUT: {task_id[:8]} — {title}")
                await fail_task(task_id, reason)
                await post_message(f"Task timed out after {TASK_TIMEOUT}s: {title}", thread=thread)
                await send_heartbeat("idle")
                return

            returncode = proc.returncode
            output = "\n".join(output_lines).strip()

            if returncode == 0:
                result_preview = output[:200] + ("…" if len(output) > 200 else "")
                _log(f"Done: [{task_id[:8]}] {title} (exit 0, {len(output)} chars)")
                await complete_task(task_id, note=output[:4096])
                await post_message(
                    f"Completed: **{title}**\n\n{result_preview}" if output else f"Completed: **{title}**",
                    thread=thread,
                )
            else:
                reason = f"exit {returncode}: {output[-500:]}" if output else f"exit {returncode}"
                _log(f"FAILED: [{task_id[:8]}] {title} — {reason[:100]}")
                await fail_task(task_id, reason)
                await post_message(
                    f"Task failed (exit {returncode}): {title}\n\n{output[-300:]}".strip(),
                    thread=thread,
                )

        except Exception as exc:
            _log(f"ERROR executing [{task_id[:8]}]: {exc}")
            await fail_task(task_id, str(exc))
            await post_message(f"Unexpected error on task '{title}': {exc}", thread=thread)

        finally:
            await send_heartbeat("idle")


# ---------------------------------------------------------------------------
# SSE subscriber — primary task delivery mechanism
# ---------------------------------------------------------------------------


def _sse_reader(queue: asyncio.Queue, loop: asyncio.AbstractEventLoop) -> None:
    """
    Blocking SSE reader — runs in a background thread.
    Parses task_created and task_updated events and puts them on the queue.
    Reconnects automatically with exponential backoff on disconnect.
    """
    backoff = 1.0
    while True:
        try:
            url = f"{BRIDGE_URL}/events"
            _dbg(f"SSE connecting to {url}")
            with requests.get(url, headers=_headers(), stream=True, timeout=None) as resp:
                resp.raise_for_status()
                backoff = 1.0  # reset on successful connect
                _dbg("SSE connected")
                event_name: str | None = None
                for raw_line in resp.iter_lines(decode_unicode=True):
                    if not raw_line:
                        event_name = None
                        continue
                    if raw_line.startswith("event:"):
                        event_name = raw_line[len("event:"):].strip()
                    elif raw_line.startswith("data:") and event_name in (
                        "task_created", "task_updated"
                    ):
                        try:
                            payload = json.loads(raw_line[len("data:"):].strip())
                            asyncio.run_coroutine_threadsafe(queue.put(payload), loop)
                        except json.JSONDecodeError:
                            pass
        except Exception as exc:
            _dbg(f"SSE error: {exc} — reconnecting in {backoff:.0f}s")
            time.sleep(backoff)
            backoff = min(backoff * 2, 60.0)  # exponential backoff, cap at 60s


# ---------------------------------------------------------------------------
# Auto-claim poller — catches tasks we missed while SSE was down
# ---------------------------------------------------------------------------


async def _poll_for_tasks(queue: asyncio.Queue) -> None:
    """Periodically poll for tasks assigned to us or unassigned matching our caps."""
    while True:
        await asyncio.sleep(POLL_INTERVAL)
        try:
            loop = asyncio.get_running_loop()
            # Tasks explicitly assigned to us
            assigned = await loop.run_in_executor(None, lambda: _get(
                "/tasks", {"assignee": AGENT_NAME, "status": "todo"}
            ))
            for t in assigned if isinstance(assigned, list) else []:
                await queue.put(t)

            # Unassigned tasks we could claim (if auto-claim enabled)
            if AUTO_CLAIM and CAPABILITIES:
                unassigned = await loop.run_in_executor(None, lambda: _get(
                    "/tasks", {"status": "todo", "limit": 20}
                ))
                for t in unassigned if isinstance(unassigned, list) else []:
                    if t.get("assignee"):
                        continue
                    task_labels: list[str] = t.get("labels") or []
                    if any(cap in task_labels for cap in CAPABILITIES):
                        await queue.put(t)
        except Exception as exc:
            _dbg(f"Poll error: {exc}")


# ---------------------------------------------------------------------------
# Heartbeat loop
# ---------------------------------------------------------------------------


async def _heartbeat_loop() -> None:
    while True:
        await asyncio.sleep(30)
        await send_heartbeat("online")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


async def main() -> None:
    _log(f"Executor agent starting — command: {' '.join(EXECUTOR_CMD)}")

    await register()
    await recover_stale_claims()

    semaphore = asyncio.Semaphore(MAX_CONCURRENT)
    task_queue: asyncio.Queue[dict] = asyncio.Queue()

    # Start SSE reader in a background thread
    loop = asyncio.get_running_loop()
    loop.run_in_executor(None, _sse_reader, task_queue, loop)

    # Start fallback poller and heartbeat
    asyncio.create_task(_poll_for_tasks(task_queue))
    asyncio.create_task(_heartbeat_loop())

    _log(f"Listening for tasks (MAX_CONCURRENT={MAX_CONCURRENT}, AUTO_CLAIM={AUTO_CLAIM})…")
    await send_heartbeat("online")

    seen_task_ids: set[str] = set()  # deduplicate (SSE + poll can both surface the same task)

    while True:
        task = await task_queue.get()
        task_id = task.get("id")
        if not task_id:
            continue

        # Only process tasks assigned to us or unassigned (if auto-claim)
        assignee = task.get("assignee")
        status = task.get("status", "")

        if status not in ("todo",):
            _dbg(f"Skipping {task_id[:8]} — status={status}")
            continue

        is_ours = assignee == AGENT_NAME
        is_claimable = (
            AUTO_CLAIM
            and not assignee
            and any(cap in (task.get("labels") or []) for cap in CAPABILITIES)
        )

        if not is_ours and not is_claimable:
            _dbg(f"Skipping {task_id[:8]} — not ours (assignee={assignee})")
            continue

        if task_id in seen_task_ids:
            _dbg(f"Skipping {task_id[:8]} — already seen")
            continue
        seen_task_ids.add(task_id)

        # Execute in background so we can pick up more tasks concurrently
        asyncio.create_task(execute_task(task, semaphore))


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        _log("Shutting down.")
