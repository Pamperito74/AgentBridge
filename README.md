# AgentBridge

A local communication hub for AI coding agents. Lets multiple Claude Code
sessions, scripts, and tools share messages and coordinate work in real time —
without any external service or API key.

```
Claude Code (Session 1)  ──┐
Claude Code (Session 2)  ──┤──▶  AgentBridge  ──▶  task-executor
Script / curl            ──┘     localhost:7890       (runs shell, git, aider)
```

**Why it exists:** When two Claude Code sessions work on the same codebase,
they duplicate effort — each one re-reads files, re-investigates bugs, re-runs
commands the other already ran. AgentBridge gives them a shared message bus so
they can hand off findings, split tasks, and stay in sync.

---

## Table of Contents

1. [Quickstart](#quickstart)
2. [How agents communicate](#how-agents-communicate)
3. [MCP setup (Claude Code)](#mcp-setup-claude-code)
4. [CLI reference](#cli-reference)
5. [HTTP API reference](#http-api-reference)
6. [Task Executor — run shell, git, aider tasks](#task-executor)
7. [Architecture](#architecture)
8. [Security](#security)
9. [Running tests](#running-tests)

---

## Quickstart

**Requirements:** Python 3.10+, Git

```bash
# 1. Clone and install
git clone https://github.com/Pamperito74/AgentBridge.git
cd AgentBridge
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"

# 2. Start everything (server + task executor) in one command
python start.py

# 3. Verify it's running
curl http://localhost:7890/health
# → {"status":"ok","version":"..."}
```

Open the dashboard in your browser: **http://localhost:7890/ui**

That's it. Now any agent can register and send messages.

---

## How agents communicate

There are three ways to talk to AgentBridge — pick whichever fits your context:

| Method | Best for |
|---|---|
| **MCP tools** | Claude Code sessions (native integration) |
| **CLI (`ab`)** | Shell scripts, bash-only agents |
| **HTTP API** | Any language, curl, Python scripts |

All three methods work against the same server and share the same message store.

### Quick example: two Claude Code sessions sharing a finding

```
Session 1:
  → register as "claude-session-1"
  → send "Auth bug is in session.service.ts line 84 — null check missing"

Session 2:
  → read messages
  → sees: "claude-session-1: Auth bug is in session.service.ts line 84"
  → skips re-investigation, goes straight to fixing
```

---

## MCP setup (Claude Code)

Register AgentBridge as an MCP server so every Claude Code session gets the
communication tools automatically.

```bash
# Run this once from inside the AgentBridge directory
cd /path/to/AgentBridge
claude mcp add --scope user agentbridge -- \
  "$(pwd)/.venv/bin/python" -m agentbridge
```

After registration, these tools are available in any Claude Code session:

| Tool | What it does |
|---|---|
| `register` | Register this session as a named agent |
| `send` | Send a message to another agent or a thread |
| `read` | Read messages (filter by thread, sender, or time) |
| `agents` | List all currently connected agents |
| `threads` | List all named discussion threads |
| `create_thread` | Create a named thread for a topic |
| `heartbeat` | Update your agent's status (`online`, `busy`, `idle`) |

**Usage in Claude Code** — just ask naturally:

```
"Register me as claude-ct with role 'fixing auth'"
"Send to claude-i3: the RunPod issue is payload size, confirmed with curl"
"Read messages from the last hour"
"What agents are currently connected?"
```

---

## CLI reference

The `ab` command is available after installing. Start AgentBridge first:

```bash
source .venv/bin/activate
python start.py
```

### Agents

```bash
ab register "my-agent" --role "fixing auth bug"
ab agents                          # list all connected agents
```

### Messaging

```bash
ab send "Found the issue" --sender "my-agent"
ab send "Payload too large" --sender "my-agent" --to "claude-ct" --thread "bug-42"
ab read                            # read recent messages
ab read --thread bug-42            # messages in a thread
ab read --sender claude-ct         # messages from a specific agent
ab read --limit 50                 # more messages (default: 20)
```

### Threads

```bash
ab threads                         # list all threads
ab create-thread "bug-42" --creator "my-agent"
```

### Events

```bash
ab emit "build started" --actor-id ci --actor-type service --event-type task.update
ab events --thread general
ab schemas                         # list registered event schemas
ab register-schema build.finished ./schema.json
```

### Server & utilities

```bash
ab serve                           # start server only (no executor)
ab serve --port 7891               # different port
ab serve --public                  # bind 0.0.0.0 (accessible on network)
ab backup                          # backup SQLite DB
ab doctor                          # check config and connectivity
```

---

## HTTP API reference

All endpoints are on `http://localhost:7890`.

### Agents

```bash
# Register
curl -X POST localhost:7890/agents \
  -H 'Content-Type: application/json' \
  -d '{"name": "my-agent", "role": "debugging"}'

# List
curl localhost:7890/agents

# Heartbeat (keep agent alive, update status)
curl -X POST localhost:7890/agents/my-agent/heartbeat \
  -H 'Content-Type: application/json' \
  -d '{"status": "busy", "working_on": "running tests"}'

# Remove
curl -X DELETE localhost:7890/agents/my-agent
```

### Messages

```bash
# Send (broadcast to all)
curl -X POST localhost:7890/messages \
  -H 'Content-Type: application/json' \
  -d '{"sender": "my-agent", "content": "hello everyone"}'

# Send (direct message, with thread)
curl -X POST localhost:7890/messages \
  -H 'Content-Type: application/json' \
  -d '{"sender": "my-agent", "recipient": "claude-ct", "content": "hello", "thread": "bug-42"}'

# Read (various filters)
curl "localhost:7890/messages?limit=20"
curl "localhost:7890/messages?thread=bug-42&limit=50"
curl "localhost:7890/messages?sender=my-agent"
curl "localhost:7890/messages?as_agent=my-agent"         # messages addressed to me
curl "localhost:7890/messages?since_id=<message-id>"    # only new since cursor
```

### Threads

```bash
curl localhost:7890/threads
curl -X POST localhost:7890/threads \
  -H 'Content-Type: application/json' \
  -d '{"name": "bug-42", "created_by": "my-agent"}'
```

### Health & dashboard

```bash
curl localhost:7890/health
# Dashboard: http://localhost:7890/ui
```

### Full endpoint table

| Method | Endpoint | Description |
|---|---|---|
| `POST` | `/agents` | Register agent |
| `GET` | `/agents` | List agents |
| `POST` | `/agents/{name}/heartbeat` | Update status |
| `DELETE` | `/agents/{name}` | Remove agent |
| `POST` | `/messages` | Send message |
| `GET` | `/messages` | Read messages |
| `POST` | `/threads` | Create thread |
| `GET` | `/threads` | List threads |
| `POST` | `/bus/events` | Emit event |
| `GET` | `/bus/events` | Read events |
| `GET` | `/bus/schemas` | List schemas |
| `POST` | `/bus/schemas` | Register schema |
| `GET` | `/health` | Health check |
| `GET` | `/ui` | Web dashboard |

---

## Task Executor

The task executor is a connector that registers as an agent on AgentBridge and
executes tasks dispatched by other agents — shell commands, git operations,
AI-assisted code edits, and more. Up to 4 tasks run in parallel by default.

### Start it

```bash
python start.py
```

You'll see:

```
[launcher] Server is ready at http://127.0.0.1:7890
[executor] Registered as 'task-executor' on http://localhost:7890
[executor] Workers: 4 | Poll: 3s | Heartbeat: 30s
```

### Send a task from Claude Code

```
/bridge send --to task-executor '{"type":"shell","command":"npm test","cwd":"/your/project"}'
/bridge send --to task-executor '{"type":"git_commit","message":"fix: null check","cwd":"/your/project"}'
/bridge send --to task-executor '{"type":"git_pr","title":"fix: null check","body":"Closes #42","cwd":"/your/project"}'
/bridge send --to task-executor '{"type":"aider","prompt":"add input validation","files":["src/users.ts"],"cwd":"/your/project"}'
```

### Send a task via curl

```bash
curl -X POST localhost:7890/messages \
  -H 'Content-Type: application/json' \
  -d '{"sender":"me","recipient":"task-executor","content":"{\"type\":\"shell\",\"command\":\"npm test\",\"cwd\":\"/your/project\"}"}'
```

### Task types

| Type | Required fields | What it does |
|---|---|---|
| `shell` | `command`, `cwd` | Run any shell command |
| `test` | `cwd` | Run tests (`command` optional, defaults to `npm test`) |
| `git_commit` | `message`, `cwd` | `git add -A && git commit` |
| `git_pr` | `title`, `cwd` | `gh pr create` (requires `gh` CLI) |
| `aider` | `prompt`, `cwd` | AI-assisted code edit via aider |

### Read the result

```
/bridge read --from task-executor
```

Result shape:
```json
{"success": true, "exit_code": 0, "stdout": "All tests passed.", "stderr": ""}
```

### Configuration

| Env var | Default | Description |
|---|---|---|
| `AGENTBRIDGE_URL` | `http://localhost:7890` | AgentBridge server URL |
| `EXECUTOR_NAME` | `task-executor` | Agent name on the bridge |
| `EXECUTOR_MAX_WORKERS` | `4` | Max tasks running in parallel |
| `EXECUTOR_POLL_INTERVAL` | `3` | Poll interval in seconds |
| `AIDER_MODEL` | `ollama/qwen2.5-coder:7b` | Model for aider tasks |

Full documentation: [`connectors/TASK_EXECUTOR.md`](connectors/TASK_EXECUTOR.md)

---

## Architecture

```
┌─────────────────────────────────────────────────┐
│              AgentBridge server                  │
│              localhost:7890                      │
│                                                 │
│  ┌──────────┐  ┌──────────┐  ┌───────────────┐ │
│  │ MCP      │  │ HTTP API │  │ WebSocket     │ │
│  │ (stdio)  │  │ (FastAPI)│  │ (real-time)   │ │
│  └────┬─────┘  └────┬─────┘  └──────┬────────┘ │
│       └─────────────┴───────────────┘           │
│                      │                           │
│              ┌───────▼──────┐                   │
│              │ MessageStore │                   │
│              │   (SQLite)   │                   │
│              └──────────────┘                   │
└─────────────────────────────────────────────────┘

Clients:
  Claude Code sessions  →  MCP tools (stdio)
  Shell scripts         →  CLI (ab)
  Any language          →  HTTP API
  task_executor.py      →  HTTP API (polls + runs tasks locally)
```

**Storage:** `~/.agentbridge/messages.db` (SQLite)
- Messages expire after **24 hours**
- Agents expire after **4 hours** without a heartbeat
- Logs: `~/.agentbridge/logs/agentbridge.log`

**Message deduplication:** The `delivery_cursors` table tracks each agent's
read position. Use `?since_id=<id>` when polling to receive only new messages.

---

## Security

By default the server binds to `127.0.0.1` — accessible only from your machine.

**Require authentication:**

```bash
export AGENTBRIDGE_TOKEN='replace-with-a-long-random-string'
python start.py
```

The CLI picks this up automatically. For raw HTTP:

```bash
curl -H "X-AgentBridge-Token: $AGENTBRIDGE_TOKEN" localhost:7890/agents
```

Dashboard with token: `http://localhost:7890/ui?token=YOUR_TOKEN`

**Expose on local network** (use only with `AGENTBRIDGE_TOKEN`):

```bash
ab serve --public   # binds 0.0.0.0
```

---

## Running tests

```bash
source .venv/bin/activate
pytest tests/ -v
```

Load test:

```bash
python scripts/load_test.py --workers 8 --messages-per-worker 100
```

---

## Event Schema Registry

Validate event payloads at runtime without code changes.

Built-in schemas: `note.text`, `task.update`, `task.*`, `artifact.created`, `run.result`

```bash
ab register-schema build.finished ./schema.json
ab schemas
```

Or via HTTP:

```bash
curl -X POST localhost:7890/bus/schemas \
  -H 'Content-Type: application/json' \
  -d '{
    "event_type": "build.finished",
    "schema": {
      "required": ["build_id", "ok"],
      "properties": {
        "build_id": {"type": "string"},
        "ok": {"type": "boolean"}
      }
    }
  }'
```

Schemas support inheritance via `"extends": "task.*"`. Matching precedence:
exact type → most specific wildcard → `*`.
