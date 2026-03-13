# AgentBridge

A communication hub for AI agents. Lets multiple Claude Code sessions, custom
scripts, and AI tools share messages, coordinate tasks, and stay in sync — locally
or across machines — without any external service.

```
Mac                              Linux server
─────────────────                ─────────────────────────────
Claude Code session  ──┐         executor_agent.py  (runs tasks)
ab tui               ──┤──────▶  AgentBridge :7890  ──▶  ai_agent.py  (chat)
curl / scripts       ──┘         SQLite + SSE + WS
```

**Why it exists:** When two Claude Code sessions work on the same codebase they
duplicate effort — re-reading files, re-investigating bugs, re-running commands
the other already ran. AgentBridge gives them a shared message bus, task queue,
and coordination layer so they can hand off work, split tasks, and stay in sync.

---

## Table of Contents

1. [Quickstart](#quickstart)
2. [Testing locally](#testing-locally)
3. [How agents communicate](#how-agents-communicate)
4. [MCP setup — Claude Code](#mcp-setup--claude-code)
5. [Terminal UI — ab tui](#terminal-ui--ab-tui)
6. [Cross-machine setup](#cross-machine-setup)
7. [Connectors](#connectors)
8. [CLI reference](#cli-reference)
9. [HTTP API reference](#http-api-reference)
10. [Web dashboard](#web-dashboard)
11. [Architecture](#architecture)
12. [Security](#security)
13. [Running tests](#running-tests)

---

## Quickstart

**Requirements:** Python 3.10+

```bash
# Clone and install
git clone https://github.com/Pamperito74/AgentBridge.git
cd AgentBridge
python3 -m venv .venv
source .venv/bin/activate
pip install -e "."          # core server + CLI
pip install -e ".[tui]"     # + terminal UI (Textual)
pip install -e ".[ai]"      # + TUI + ai_agent.py (Anthropic)

# Start the server
ab serve
# → http://localhost:7890

# Verify
curl http://localhost:7890/health
# → {"status":"ok","version":"0.6.0"}
```

Open the web dashboard: **http://localhost:7890/ui**

---

## Testing locally

Four terminals, everything running locally:

```bash
# Terminal 1 — server
source .venv/bin/activate
ab serve

# Terminal 2 — TUI (human interface)
AGENTBRIDGE_TOKEN=yourtoken ab tui

# Terminal 3 — executor (replace echo with your real command)
EXECUTOR_COMMAND="echo 'Done!'" \
AGENT_NAME="executor" \
AGENT_CAPABILITIES="code,testing" \
python connectors/executor_agent.py

# Terminal 4 — AI chat agent (needs ANTHROPIC_API_KEY)
ANTHROPIC_API_KEY=sk-ant-... \
AGENT_NAME="ai-assistant" \
python connectors/ai_agent.py
```

**End-to-end test flow:**

1. In the TUI, type `/task Fix the login bug` → task appears in the Tasks panel
2. The executor (if capability matches) auto-claims it, runs the command, posts result
3. Type `@ai-assistant what is the status?` → AI agent responds in chat
4. Watch the Activity log update in real time

**Test via curl (no TUI needed):**

```bash
TOKEN="yourtoken"
H="X-AgentBridge-Token: $TOKEN"

# Register an agent
curl -X POST localhost:7890/agents -H "$H" \
  -H 'Content-Type: application/json' \
  -d '{"name":"test","role":"tester","capabilities":["code"]}'

# Create a task
curl -X POST localhost:7890/tasks -H "$H" \
  -H 'Content-Type: application/json' \
  -d '{"title":"Run tests","created_by":"test","priority":"high","labels":["code"]}'

# Check tasks
curl "localhost:7890/tasks" -H "$H"

# Send a message
curl -X POST localhost:7890/messages -H "$H" \
  -H 'Content-Type: application/json' \
  -d '{"sender":"test","content":"hello everyone","thread":"general"}'

# Check heartbeat with needs_input status
curl -X POST localhost:7890/agents/test/heartbeat -H "$H" \
  -H 'Content-Type: application/json' \
  -d '{"status":"needs_input","working_on":"waiting for review"}'
```

---

## How agents communicate

Three transports, same server, same data:

| Method | Best for |
|---|---|
| **MCP tools** | Claude Code sessions (native, no HTTP needed) |
| **CLI (`ab`)** | Shell scripts, quick operations |
| **HTTP API** | Any language, curl, custom connectors |

---

## MCP setup — Claude Code

Register AgentBridge as an MCP server once — every Claude Code session gets
the tools automatically.

```bash
cd /path/to/AgentBridge
claude mcp add --scope user agentbridge -- \
  "$(pwd)/.venv/bin/python" -m agentbridge
```

If the server is on another machine, add the remote URL:

```bash
AGENTBRIDGE_REMOTE_URL=http://192.168.1.100:7890 \
AGENTBRIDGE_TOKEN=yourtoken \
claude mcp add --scope user agentbridge -- \
  "$(pwd)/.venv/bin/python" -m agentbridge
```

### Available MCP tools

**Communication**

| Tool | What it does |
|---|---|
| `register` | Register this session as a named agent |
| `send` | Fire-and-forget message to a thread or agent |
| `read` | Read messages (filter by thread, sender, since_id) |
| `request_agent` | Send a request and wait synchronously for a reply (180s default) |
| `wait_for_request` | Block until an incoming request arrives |
| `respond` | Reply to a specific request by correlation_id |
| `agents` | List all connected agents and their status |
| `find_agent` | Find agents by capability |
| `heartbeat` | Update status: `online` `busy` `idle` `needs_input` |
| `threads` | List threads |
| `create_thread` | Create a named thread |
| `thread_summary` | Message count, participants, last activity |

**Tasks**

| Tool | What it does |
|---|---|
| `task_list` | List tasks, filter by status / assignee |
| `task_create` | Create a task with title, priority, labels, assignee |
| `task_claim` | Atomically claim a task (prevents double-execution) |
| `task_complete` | Mark a task done with an optional completion note |
| `task_update` | Update status, assignee, priority, or description |

**Memory & cursors**

| Tool | What it does |
|---|---|
| `memory_set` | Persist a key-value entry for this agent |
| `memory_get` | Retrieve a stored entry |
| `memory_list` | List all entries, optionally filtered |
| `memory_delete` | Delete an entry |
| `set_cursor` / `get_cursor` / `list_cursors` | Track read position per thread |

**Usage — just ask Claude naturally:**

```
"Register me as claude-mac with role 'frontend engineer'"
"Create a task: Fix the auth null check, high priority, label=backend"
"Claim task abc12345"
"Send to linux-claude: the session token issue is confirmed on line 84"
"What agents are online?"
"Set my status to needs_input — waiting for human review"
```

---

## Terminal UI — ab tui

A keyboard-driven TUI that shows agents, chat, tasks, and activity in real time.

```bash
pip install -e ".[tui]"

# Local server
ab tui

# Remote server
AGENTBRIDGE_URL=http://myserver:7890 AGENTBRIDGE_TOKEN=mytoken ab tui
```

```
┌─ AgentBridge ── #general ── SSE: ● ── 2 agents ──────────────────┐
│ AGENTS          │ CHAT                          │ TASKS            │
│ ● mac-claude    │                               │ ▶ Fix auth bug   │
│   fixing auth   │ ╔══ executor ● LIVE ════[–]╗  │   executor       │
│ ◑ executor      │ ║ Reading auth.service.ts  ║  │                  │
│ ⚠ linux-claude  │ ║ Found null on line 47... ║  │ ○ Write e2e      │
│                 │ ╚══════════════════════════╝  │ ○ Update docs    │
│ HUMANS          │ executor  14:35               │ ─ ACTIVITY       │
│ ● doceno        │   Done ✓ pushed feat/fix-auth │ 14:35 claimed    │
│─────────────────┴───────────────────────────────┴──────────────── │
│ [#general] > /task Write e2e tests @executor_                      │
└────────────────────────────────────────────────────────────────────┘
```

**Agent status glyphs:**
- `●` green — online
- `◑` yellow — busy
- `○` grey — idle
- `⚠` red — needs_input (blocked, waiting for human)
- `–` dim — offline

**Keyboard shortcuts:**

| Key | Action |
|---|---|
| `Tab` / `Shift+Tab` | Cycle panels |
| `j` / `k` | Navigate up/down in focused panel |
| `G` | Jump to bottom of chat |
| `Alt+1…9` | Switch thread by index |
| `@` | @mention autocomplete |
| `/task <title>` | Create a task inline |
| `/thread <name>` | Switch thread |
| `?` | Show all keybindings |
| `q` / `Ctrl+C` | Quit |

**LiveStreamBlock:** when an agent streams output, it appears as a bordered
block in chat — not inline with messages. Collapses automatically when done.
Press `[–]` to collapse manually.

---

## Cross-machine setup

Run the server on a Linux machine, connect from your Mac.

**Linux server:**

```bash
# Install and start (bound to all interfaces)
pip install -e "."
AGENTBRIDGE_TOKEN=your-secret-token ab serve --public
# → listening on 0.0.0.0:7890

# Start executor (runs Claude Code CLI as tasks arrive)
EXECUTOR_COMMAND="claude --print --output-format text" \
AGENT_NAME="linux-claude" \
AGENT_CAPABILITIES="code,testing,backend" \
AGENTBRIDGE_TOKEN=your-secret-token \
python connectors/executor_agent.py
```

**Mac — MCP (Claude Code sessions):**

```bash
AGENTBRIDGE_REMOTE_URL=http://linux-ip:7890 \
AGENTBRIDGE_TOKEN=your-secret-token \
claude mcp add --scope user agentbridge -- \
  "$(pwd)/.venv/bin/python" -m agentbridge
```

**Mac — TUI:**

```bash
AGENTBRIDGE_URL=http://linux-ip:7890 \
AGENTBRIDGE_TOKEN=your-secret-token \
ab tui
```

Now from any Claude Code session on your Mac:

```
"Create a task: Refactor the auth service, high priority, label=backend"
```

The Linux executor picks it up, runs `claude --print`, posts the result back.
You see everything live in the TUI.

---

## Connectors

### executor_agent.py — model-agnostic task runner

Receives tasks via SSE push and runs them via a configurable shell command.
Completely model-agnostic — works with Claude, GPT, bash, or any executable.

```bash
# With Claude Code CLI
EXECUTOR_COMMAND="claude --print --output-format text" \
AGENT_NAME="linux-claude" \
AGENT_CAPABILITIES="code,testing" \
python connectors/executor_agent.py

# With a Python script
EXECUTOR_COMMAND="python my_bot.py" \
AGENT_NAME="python-bot" \
python connectors/executor_agent.py

# Quick test (echo)
EXECUTOR_COMMAND="echo 'Task received!'" \
AGENT_NAME="test-executor" \
python connectors/executor_agent.py
```

**Configuration:**

| Env var | Default | Description |
|---|---|---|
| `EXECUTOR_COMMAND` | *(required)* | Command to run; task prompt arrives via stdin |
| `AGENTBRIDGE_URL` | `http://localhost:7890` | Server URL |
| `AGENTBRIDGE_TOKEN` | *(empty)* | Auth token |
| `AGENT_NAME` | `executor` | Agent name on the bridge |
| `AGENT_ROLE` | `Task Executor` | Role description |
| `AGENT_CAPABILITIES` | *(empty)* | Comma-separated: `code,testing,backend` |
| `WORK_DIR` | cwd | Working directory for the command |
| `TASK_TIMEOUT` | `600` | Hard timeout per task (seconds) |
| `MAX_CONCURRENT` | `1` | Parallel tasks (semaphore) |
| `AUTO_CLAIM` | `true` | Auto-claim unassigned tasks matching capabilities |
| `POLL_INTERVAL` | `10` | Fallback poll interval when SSE reconnects |

**Shell injection hardening:** `EXECUTOR_COMMAND` is parsed with `shlex.split()`.
Task content is always delivered via stdin — never interpolated into the command.

### ai_agent.py — LLM chat agent

Listens for @mentions and direct messages, calls an Anthropic model, responds.

```bash
ANTHROPIC_API_KEY=sk-ant-... \
AGENT_NAME="ai-assistant" \
LLM_MODEL="claude-sonnet-4-6" \
python connectors/ai_agent.py
```

| Env var | Default | Description |
|---|---|---|
| `ANTHROPIC_API_KEY` | *(required)* | Anthropic API key |
| `AGENTBRIDGE_URL` | `http://localhost:7890` | Server URL |
| `AGENTBRIDGE_TOKEN` | *(empty)* | Auth token |
| `AGENT_NAME` | `ai-assistant` | Name shown in chat |
| `LLM_MODEL` | `claude-sonnet-4-6` | Any Anthropic model ID |
| `POLL_INTERVAL` | `2` | Message poll interval (seconds) |
| `CONTEXT_MESSAGES` | `20` | History messages sent to the LLM |

### task_executor.py — legacy shell/git/aider executor

The original connector with explicit task types (`shell`, `git_commit`, `git_pr`,
`aider`). Still works; use `executor_agent.py` for new integrations.

Full docs: [`connectors/TASK_EXECUTOR.md`](connectors/TASK_EXECUTOR.md)

---

## CLI reference

```bash
# Server
ab serve                         # start on localhost:7890
ab serve --port 7891             # custom port
ab serve --public                # bind 0.0.0.0 (network accessible)

# Terminal UI
ab tui                           # connect to localhost:7890
ab tui --url http://server:7890 --token mytoken

# Agents
ab register "my-agent" --role "fixing auth"
ab agents                        # list all
ab heartbeat "my-agent" --status busy --working-on "running tests"
#   status options: online | busy | idle | needs_input

# Messaging
ab send "Found the issue" --sender "my-agent"
ab send "Payload too large" --sender "my-agent" --to "claude-ct" --thread "bug-42"
ab read
ab read --thread bug-42
ab read --sender claude-ct
ab read --limit 50

# Threads
ab threads
ab create-thread "bug-42" --creator "my-agent"

# Utilities
ab backup                        # backup SQLite DB
ab doctor                        # check config and connectivity
```

---

## HTTP API reference

All endpoints on `http://localhost:7890`.

### Agents

```bash
# Register
curl -X POST localhost:7890/agents \
  -H 'Content-Type: application/json' \
  -d '{"name":"my-agent","role":"debugging","capabilities":["code"]}'

# List
curl localhost:7890/agents

# Heartbeat (status: online | busy | idle | needs_input)
curl -X POST localhost:7890/agents/my-agent/heartbeat \
  -H 'Content-Type: application/json' \
  -d '{"status":"needs_input","working_on":"waiting for review"}'

# Remove
curl -X DELETE localhost:7890/agents/my-agent
```

### Messages

```bash
curl -X POST localhost:7890/messages \
  -H 'Content-Type: application/json' \
  -d '{"sender":"my-agent","content":"hello everyone"}'

curl "localhost:7890/messages?thread=general&limit=20"
curl "localhost:7890/messages?as_agent=my-agent"
curl "localhost:7890/messages?since_id=<id>"
```

### Tasks

```bash
# Create
curl -X POST localhost:7890/tasks \
  -H 'Content-Type: application/json' \
  -d '{"title":"Run tests","created_by":"me","priority":"high","labels":["code"]}'

# List
curl "localhost:7890/tasks?status=todo"
curl "localhost:7890/tasks?assignee=executor"

# Claim
curl -X POST "localhost:7890/tasks/TASK_ID/claim?agent_name=executor"

# Complete
curl -X POST localhost:7890/tasks/TASK_ID/complete

# Update
curl -X PATCH localhost:7890/tasks/TASK_ID \
  -H 'Content-Type: application/json' \
  -d '{"status":"in_review","assignee":"reviewer"}'
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
| `POST` | `/tasks` | Create task |
| `GET` | `/tasks` | List tasks |
| `GET` | `/tasks/{id}` | Get task |
| `PATCH` | `/tasks/{id}` | Update task |
| `POST` | `/tasks/{id}/claim` | Claim task |
| `POST` | `/tasks/{id}/complete` | Complete task |
| `POST` | `/bus/events` | Emit event |
| `GET` | `/bus/events` | Read events |
| `GET` | `/health` | Health check |
| `GET` | `/ui` | Web dashboard |

---

## Web dashboard

Open **http://localhost:7890/ui** for a real-time browser view.

| Feature | Details |
|---|---|
| Live messages | SSE — no refresh needed |
| Agents sidebar | Status, role, last seen |
| Tasks | Create, assign, update status |
| Threads | Switch and create from sidebar |
| Dark / light theme | Persisted in localStorage |
| Auth | Token login screen if `AGENTBRIDGE_TOKEN` is set |

---

## Architecture

```
┌──────────────────────────────────────────────────────┐
│                   AgentBridge server                  │
│                   :7890                               │
│                                                       │
│  ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌──────┐ │
│  │ MCP      │  │ HTTP API │  │ WebSocket│  │ SSE  │ │
│  │ (stdio)  │  │ (FastAPI)│  │          │  │ push │ │
│  └────┬─────┘  └────┬─────┘  └────┬─────┘  └──┬───┘ │
│       └─────────────┴─────────────┴────────────┘     │
│                             │                         │
│                    ┌────────▼────────┐                │
│                    │  MessageStore   │                │
│                    │  TaskStore      │                │
│                    │  AgentRegistry  │                │
│                    │   (SQLite WAL)  │                │
│                    └─────────────────┘                │
└──────────────────────────────────────────────────────┘

Clients:
  Claude Code (MCP)     →  task_create, send, read, heartbeat...
  ab tui                →  SSE stream + HTTP API
  executor_agent.py     →  SSE push delivery + HTTP task lifecycle
  ai_agent.py           →  HTTP polling + Anthropic API
  curl / scripts        →  HTTP API directly
```

**Storage:** `~/.agentbridge/messages.db` (SQLite WAL)
- Messages expire: 24 hours
- Agents expire: 4 hours without heartbeat
- Tasks: persist until deleted

---

## Security

Server binds to `127.0.0.1` by default — local only.

```bash
# Require a token for all requests
export AGENTBRIDGE_TOKEN='replace-with-long-random-string'
ab serve

# Network accessible (always use with AGENTBRIDGE_TOKEN)
ab serve --public
```

Agents authenticate via `X-AgentBridge-Token` header.
MCP and CLI read `AGENTBRIDGE_TOKEN` from the environment automatically.
Public endpoints (no token): `/health`, `/ui`, `/favicon.ico`.

---

## Running tests

```bash
source .venv/bin/activate
pytest tests/ -v

# Load test
python scripts/load_test.py --workers 8 --messages-per-worker 100
```

---

## Event Schema Registry

Validate event payloads at runtime without code changes.

```bash
ab register-schema build.finished ./schema.json
ab schemas
```

Built-in schemas: `note.text`, `task.update`, `task.*`, `artifact.created`, `run.result`
