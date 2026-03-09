# Task Executor Connector

The task executor is a local agent that sits alongside AgentBridge and executes
shell commands, git operations, and AI coding tasks on behalf of other agents.
Any Claude Code session (or any script) can send it a task and get the result
back — without needing direct shell access themselves.

---

## What it does

```
Claude Code session
      │
      │  /bridge send --to task-executor '{"type":"shell","command":"npm test","cwd":"/project"}'
      ▼
AgentBridge (localhost:7890)
      │
      │  routes message to task-executor
      ▼
task_executor.py
      │
      │  runs: npm test
      ▼
      result → back through AgentBridge → back to Claude Code session
```

The executor:
- Registers as an agent named `task-executor` on AgentBridge
- Polls for incoming task messages every 3 seconds
- Executes the task locally using the appropriate tool (shell, git, aider, etc.)
- Replies to the sender with structured results (exit code, stdout, stderr)
- Sends a heartbeat every 30 seconds so it stays visible in the agent registry
- Uses a message cursor so it never reprocesses old messages after a restart

---

## Prerequisites

- AgentBridge installed and running (`python run_server.py`)
- Python 3.10+ with the `requests` package available
- For `git_commit` / `git_pr` tasks: `git` and `gh` CLI installed and authenticated
- For `aider` tasks: `aider` installed (`pipx install aider-chat`)

---

## How to run

**Step 1 — Start AgentBridge** (if not already running):

```bash
cd ~/code/TheDarkFactory/AgentBridge
source .venv/bin/activate
python run_server.py
```

**Step 2 — Start the executor** in a separate terminal:

```bash
cd ~/code/TheDarkFactory/AgentBridge
source .venv/bin/activate
python connectors/task_executor.py
```

You should see:

```
[executor] Registered as 'task-executor' on http://localhost:7890
[executor] Polling every 3s — send tasks via AgentBridge to 'task-executor'
[executor] Heartbeat every 30s
[executor] Ctrl-C to stop
```

That's it. The executor is now live and waiting for tasks.

---

## Configuration

All settings are controlled via environment variables. The defaults work for
most setups — only change what you need.

| Variable | Default | Description |
|---|---|---|
| `AGENTBRIDGE_URL` | `http://localhost:7890` | AgentBridge server URL |
| `EXECUTOR_NAME` | `task-executor` | Name this agent registers under |
| `EXECUTOR_POLL_INTERVAL` | `3` | How often (seconds) to check for new tasks |
| `EXECUTOR_MAX_WORKERS` | `4` | Max tasks that can run in parallel |
| `AIDER_MODEL` | `ollama/qwen2.5-coder:7b` | Model used for `aider` task type |

`EXECUTOR_MAX_WORKERS` controls the thread pool size. With the default of 4,
up to 4 tasks run simultaneously — a 120s shell command no longer blocks 3
other tasks from starting. Set it lower (`1`) for strict sequential execution
or higher if your machine can handle more concurrent work.

Example — run two executors on different projects simultaneously:

```bash
EXECUTOR_NAME=executor-api   python connectors/task_executor.py &
EXECUTOR_NAME=executor-web   python connectors/task_executor.py &
```

---

## Sending tasks

Tasks are JSON payloads sent to `task-executor` via AgentBridge. Every task
needs a `type` field and a `cwd` (working directory) field.

### From Claude Code (via `/bridge`)

```
/bridge send --to task-executor '{"type":"shell","command":"npm test","cwd":"/Users/you/project"}'
```

### From the CLI

```bash
curl -s -X POST http://localhost:7890/messages \
  -H "Content-Type: application/json" \
  -d '{
    "sender": "me",
    "recipient": "task-executor",
    "content": "{\"type\":\"shell\",\"command\":\"npm test\",\"cwd\":\"/Users/you/project\"}"
  }'
```

### From another Python script

```python
import requests, json

requests.post("http://localhost:7890/messages", json={
    "sender": "my-script",
    "recipient": "task-executor",
    "content": json.dumps({
        "type": "shell",
        "command": "npm test",
        "cwd": "/Users/you/project"
    })
})
```

---

## Task types

### `shell` — Run any shell command

```json
{
  "type": "shell",
  "command": "npm run build",
  "cwd": "/Users/you/project"
}
```

Use this for anything that doesn't fit the other types: linting, building,
running migrations, custom scripts, etc.

---

### `test` — Run tests

```json
{
  "type": "test",
  "command": "pytest -x tests/",
  "cwd": "/Users/you/project"
}
```

`command` defaults to `npm test` if omitted.

---

### `git_commit` — Stage all changes and commit

```json
{
  "type": "git_commit",
  "message": "fix(auth): handle null session edge case",
  "cwd": "/Users/you/project"
}
```

Equivalent to `git add -A && git commit -m "..."`. The working directory must
be a git repository.

---

### `git_pr` — Open a pull request

```json
{
  "type": "git_pr",
  "title": "fix(auth): handle null session",
  "body": "Closes #123\n\nFixed null check in session middleware.",
  "base": "develop",
  "cwd": "/Users/you/project"
}
```

Requires `gh` CLI authenticated (`gh auth login`). `base` is optional —
defaults to whatever `gh` considers the default branch.

---

### `aider` — AI-assisted code edits

```json
{
  "type": "aider",
  "prompt": "Add input validation to the createUser function",
  "files": ["src/users/users.service.ts"],
  "model": "ollama/qwen2.5-coder:7b",
  "cwd": "/Users/you/project"
}
```

Runs `aider` with `--yes --no-git` so it applies edits without prompting and
without making its own commits. `model` is optional — falls back to the
`AIDER_MODEL` env var (`ollama/qwen2.5-coder:7b` by default). `files` is
optional — omit to let aider decide which files to touch.

---

## Reading the result

The executor replies to the sender automatically. Read it with:

```
/bridge read --from task-executor
```

Or via HTTP:

```bash
curl "http://localhost:7890/messages?sender=task-executor&limit=5"
```

### Success response

```json
{
  "success": true,
  "exit_code": 0,
  "stdout": "All 42 tests passed.",
  "stderr": ""
}
```

### Failure response

```json
{
  "success": false,
  "exit_code": 1,
  "stdout": "",
  "stderr": "Error: Cannot find module './auth'"
}
```

### Error response (bad payload)

```json
{
  "success": false,
  "error": "Payload must be valid JSON"
}
```

---

## Synchronous usage (wait for result)

If you need to block and wait for the result rather than polling, use
AgentBridge's request-response pattern. The executor supports this natively —
it forwards `correlation_id` and sets `msg_type: response` on every reply.

```bash
# CLI — blocks until executor replies (default 300s timeout)
ab request task-executor '{"type":"test","command":"npm test","cwd":"/project"}'
```

From Claude Code:

```
/bridge send --to task-executor '{"type":"test","command":"npm test","cwd":"/project"}'
# then check reply
/bridge read --from task-executor
```

---

## Multi-agent workflow example

Four terminals. This is the full setup for two Claude Code agents coordinating
through the executor.

```
Terminal 1  →  python run_server.py                  # AgentBridge hub
Terminal 2  →  python connectors/task_executor.py    # executor
Terminal 3  →  claude   (Session 1 — Planner)
Terminal 4  →  claude   (Session 2 — Implementer)
```

**Planner (Session 1) posts a task to a shared thread:**

```
/bridge send --thread feature-42 "Need null check in auth.service.ts around line 84"
```

**Implementer (Session 2) reads and implements:**

```
/bridge read --thread feature-42
# ... writes the fix in the editor ...
/bridge send --to task-executor '{"type":"test","command":"npm test -- --testPathPattern=auth","cwd":"/project"}'
```

**Implementer reads the test result:**

```
/bridge read --from task-executor
# → {"success":true,"exit_code":0,"stdout":"All tests passed."}
```

**Implementer commits and opens a PR:**

```
/bridge send --to task-executor '{"type":"git_commit","message":"fix(auth): null check in session middleware","cwd":"/project"}'
/bridge send --to task-executor '{"type":"git_pr","title":"fix(auth): null check","body":"Closes #42","base":"develop","cwd":"/project"}'
```

**Planner reads the PR URL from the result and reviews.**

---

## Troubleshooting

**"could not register — is AgentBridge running?"**
Start AgentBridge first: `python run_server.py`. Verify with `curl http://localhost:7890/health`.

**Tasks sent but no reply appears**
Check the executor terminal for error output. Make sure `cwd` exists and the
command is valid. The executor logs every task it receives.

**Same task executed multiple times**
The cursor (`since_id`) is in-memory. If the executor was restarted while there
were unprocessed messages in the last 24h, it may re-execute them once on
startup. Safe workaround: restart the executor before sending new tasks rather
than while tasks are in flight.

**`gh` not found for `git_pr` tasks**
Install and authenticate: `brew install gh && gh auth login`.

**`aider` not found**
Install with pipx and Python 3.12: `pipx install aider-chat --python python3.12`.

---

## Quick reference

```bash
# Start everything
python run_server.py &                    # AgentBridge
python connectors/task_executor.py &      # executor

# Verify executor is registered
curl http://localhost:7890/agents | python3 -m json.tool

# Send a task
curl -s -X POST http://localhost:7890/messages \
  -H "Content-Type: application/json" \
  -d '{"sender":"me","recipient":"task-executor","content":"{\"type\":\"shell\",\"command\":\"echo hello\",\"cwd\":\".\"}"}'

# Read the reply
curl "http://localhost:7890/messages?sender=task-executor&limit=1"

# Stop the executor
Ctrl-C in Terminal 2
```
