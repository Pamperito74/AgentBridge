# AgentBridge — Inter-Agent Communication Server

## Context

Two Claude Code sessions were debugging the same Inline-3 + CT issue simultaneously but couldn't share findings. One session proved via curl that RunPod was healthy; the other kept investigating the wrong path. A shared message bus would have resolved this in seconds.

AgentBridge is a standalone MCP server + HTTP API that lets any AI coding agent (Claude Code, Codex, Cursor, Copilot, etc.) send and receive messages in real-time. It's the missing "team chat" for AI agents working on the same codebase.

## Architecture

```
┌─────────────┐     MCP (stdio)      ┌──────────────────┐
│ Claude Code  │◄────────────────────►│                  │
│  Session 1   │                      │                  │
└─────────────┘                       │   AgentBridge    │
┌─────────────┐     MCP (stdio)      │   Server         │
│ Claude Code  │◄────────────────────►│                  │
│  Session 2   │                      │  (Python/FastMCP │
└─────────────┘                       │   + FastAPI)     │
┌─────────────┐     HTTP REST API     │                  │
│ Codex / Any  │◄────────────────────►│                  │
│  Agent       │                      │                  │
└─────────────┘                       └──────┬───────────┘
┌─────────────┐     CLI (bash)               │
│ Any Agent    │─────────────────────►  ab send/read/list
└─────────────┘                       (wraps HTTP calls)
```

**Three access layers, one server:**
1. **MCP** — Native for Claude Code (registered via `claude mcp add`)
2. **HTTP API** — For any agent that can make HTTP requests
3. **CLI** — For agents that can only run bash commands (`ab send`, `ab read`)

## Tech Stack

- **Python 3.10+**
- **FastMCP** (from `mcp` SDK) — MCP server with stdio transport
- **FastAPI + uvicorn** — HTTP API server (runs alongside MCP)
- **SQLite** — Persistent message storage (zero config)
- **Click** — CLI tool

## Project Structure

```
agentbridge/
├── README.md
├── pyproject.toml
├── src/
│   └── agentbridge/
│       ├── __init__.py
│       ├── server.py          # FastMCP server (MCP tools) + FastAPI (HTTP API)
│       ├── store.py           # SQLite message store
│       ├── models.py          # Pydantic models (Agent, Message, Thread)
│       └── cli.py             # CLI wrapper (`ab` command)
└── tests/
    └── test_server.py
```

## Data Models

```python
class Agent(BaseModel):
    name: str              # "claude-inline3", "claude-ct", "codex-1"
    role: str = ""         # "debugging inline-3", "working on CT"
    connected_at: datetime
    last_seen: datetime

class Message(BaseModel):
    id: str                # UUID
    sender: str            # agent name
    recipient: str | None  # None = broadcast to all
    thread: str = "general"
    content: str
    timestamp: datetime

class Thread(BaseModel):
    name: str              # "runpod-400-debug", "general"
    created_by: str
    created_at: datetime
```

## MCP Tools (for Claude Code)

| Tool | Description |
|------|-------------|
| `register` | Register this agent with a name and optional role |
| `send` | Send a message to a specific agent or broadcast |
| `read` | Read recent messages (optionally filtered by thread or sender) |
| `agents` | List all connected agents |
| `threads` | List active threads |
| `create_thread` | Create a named discussion thread |

## HTTP API (for any agent)

| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | `/agents` | Register agent `{name, role}` |
| GET | `/agents` | List connected agents |
| POST | `/messages` | Send message `{sender, recipient?, thread?, content}` |
| GET | `/messages?thread=&since=&limit=` | Read messages |
| POST | `/threads` | Create thread `{name, created_by}` |
| GET | `/threads` | List threads |

## CLI (for any agent via bash)

```bash
ab register "codex-1" --role "fixing auth"
ab send "Found the bug — it's in auth.py line 42"
ab send --to "claude-ct" "Deploy the fix"
ab read                          # last 20 messages
ab read --thread "runpod-debug"  # thread-specific
ab agents                        # who's online
ab threads                       # active threads
```

## Key Implementation Details

### server.py (~150 lines)

- FastMCP server handles MCP tools via stdio
- FastAPI app runs on `localhost:7890` for HTTP/CLI access
- Both share the same `MessageStore` instance
- Server starts both transports: MCP stdio + HTTP in background thread

### store.py (~80 lines)

- SQLite database at `~/.agentbridge/messages.db`
- Tables: `agents`, `messages`, `threads`
- Auto-cleanup: messages older than 24h pruned on startup
- Thread-safe (SQLite WAL mode)

### cli.py (~50 lines)

- Click CLI wrapping HTTP calls to `localhost:7890`
- Installed as `ab` entry point
- Simple `requests.get/post` calls

### Registration Flow

1. Agent calls `register(name="claude-inline3", role="debugging handler.py")`
2. Server stores agent with timestamp
3. Other agents can see it via `agents()` → `["claude-inline3 (debugging handler.py)", "claude-ct (fixing base64 threshold)"]`
4. Agents are auto-removed after 1h of no activity

### Example: The Debug Scenario That Inspired This

```
Session 1 (Inline-3):
  → register("claude-i3", role="debugging RunPod 400")
  → send("Tested RunPod directly with curl — small payload works, returns IN_QUEUE. The 400 is payload size.")

Session 2 (CT):
  → register("claude-ct", role="fixing CT quote-analysis")
  → read() → sees: "claude-i3: Tested RunPod directly with curl..."
  → send("Confirmed — staging still has 100MB threshold, not deployed yet")

Session 1:
  → read() → sees: "claude-ct: Confirmed — staging still has 100MB threshold"
  → Done. No more finger-pointing.
```

## Setup & Registration

```bash
# Install
pip install agentbridge
# or
git clone ... && pip install -e .

# Register with Claude Code (one time)
claude mcp add agentbridge -- python -m agentbridge.server

# Start HTTP server (for non-MCP agents)
ab serve
```

## Files to Create

1. `pyproject.toml` — Package config with `ab` CLI entry point
2. `src/agentbridge/models.py` — Pydantic models (~30 lines)
3. `src/agentbridge/store.py` — SQLite store (~80 lines)
4. `src/agentbridge/server.py` — MCP + HTTP server (~150 lines)
5. `src/agentbridge/cli.py` — CLI tool (~50 lines)
6. `src/agentbridge/__init__.py` — Version
7. `tests/test_server.py` — Core tests (~60 lines)
8. `README.md` — Setup and usage docs

## Verification

1. Start server: `python -m agentbridge.server`
2. In terminal 1: `ab register "agent-1" && ab send "hello from agent 1"`
3. In terminal 2: `ab register "agent-2" && ab read` → should see agent-1's message
4. Register with Claude Code: `claude mcp add agentbridge -- python -m agentbridge.server`
5. In Claude Code: use `register` and `send` tools
6. Run tests: `pytest tests/`
