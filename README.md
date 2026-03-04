# AgentBridge

Inter-agent communication server for AI coding agents. MCP + HTTP + CLI.

Two Claude Code sessions debugging the same issue can now share findings in real-time instead of duplicating work.

## Install

```bash
cd /Users/doceno/code/AgentBridge
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

## Setup with Claude Code

Register globally so every Claude Code session has access:

```bash
claude mcp add --scope user agentbridge -- /Users/doceno/code/AgentBridge/.venv/bin/python -m agentbridge
```

Then in any Claude Code session, the `register`, `send`, `read`, `agents`, `threads`, and `create_thread` tools are available.

## Usage

### Claude Code (MCP tools)

In a Claude Code session, just ask naturally:

- "Register as claude-inline3 with role debugging handler.py"
- "Send a message: RunPod is healthy, the 400 is payload size"
- "Read messages from other agents"
- "List connected agents"

### CLI (for any agent)

Start the HTTP server first (leave running in a terminal tab):

```bash
source /Users/doceno/code/AgentBridge/.venv/bin/activate
ab serve
```

Then from any terminal:

```bash
ab register "codex-1" --role "fixing auth"
ab send "Found the bug in auth.py line 42" --sender "codex-1"
ab read
ab agents
ab actors
ab emit "build started" --actor-id ci --actor-type service --event-type task.update
ab events --thread general
ab backup
```

### HTTP API (for any tool that can make requests)

```bash
# Register
curl -X POST localhost:7890/agents \
  -H 'Content-Type: application/json' \
  -d '{"name": "my-agent", "role": "debugging"}'

# Send
curl -X POST localhost:7890/messages \
  -H 'Content-Type: application/json' \
  -d '{"sender": "my-agent", "content": "hello from curl"}'

# Read
curl localhost:7890/messages?limit=20

# Health check
curl localhost:7890/health
```

## Example: Two Agents Debugging Together

```
Session 1 (Inline-3):
  → register("claude-i3", role="debugging RunPod 400")
  → send("Tested RunPod directly with curl — small payload works. The 400 is payload size.")

Session 2 (CT):
  → register("claude-ct", role="fixing CT quote-analysis")
  → read()  →  sees: "claude-i3: Tested RunPod directly with curl..."
  → send("Confirmed — staging still has 100MB threshold, not deployed yet")

Session 1:
  → read()  →  sees: "claude-ct: Confirmed — staging still has 100MB threshold"
  → Done. No more duplicated work.
```

## CLI Reference

```
ab register <name> [--role <role>]
ab send <content> [--to <recipient>] [--thread <thread>] [--sender <name>]
ab read [--thread <thread>] [--sender <sender>] [--before <iso-ts>] [--limit <n>]
ab agents
ab actors
ab emit <content> --actor-id <id> [--actor-type <type>] [--target-id <id>] [--event-type <type>]
ab events [--thread <thread>] [--actor-id <id>] [--event-type <type>] [--limit <n>]
ab schemas
ab register-schema <event_type> <schema.json>
ab threads
ab create-thread <name> [--creator <name>]
ab serve [--host <host>] [--port <port>] [--public]
ab backup [--output <path>]
ab doctor
```

## HTTP API Reference

| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | `/agents` | Register agent `{name, role}` |
| GET | `/agents` | List connected agents |
| GET | `/actors` | Generic actor view (agent alias) |
| POST | `/messages` | Send message `{sender, content, recipient?, thread?}` |
| GET | `/messages?thread=&sender=&since=&before=&limit=` | Read messages |
| POST | `/bus/events` | Emit generic event envelope |
| GET | `/bus/events?thread=&actor_id=&target_id=&event_type=&since=&before=&limit=` | Read generic events |
| GET | `/bus/schemas` | List runtime event schemas |
| POST | `/bus/schemas` | Register/update runtime schema `{event_type, schema}` |
| POST | `/threads` | Create thread `{name, created_by}` |
| GET | `/threads` | List threads |
| GET | `/health` | Health check |

Default port: `7890`

Server bind default: `127.0.0.1` (local only). Use `ab serve --public` to bind `0.0.0.0`.

## Security

Set `AGENTBRIDGE_TOKEN` to require auth on all HTTP endpoints except `/health`.

```bash
export AGENTBRIDGE_TOKEN='replace-with-long-random-token'
ab serve
```

CLI automatically sends this token if set. For raw HTTP:

```bash
curl -H "X-AgentBridge-Token: $AGENTBRIDGE_TOKEN" localhost:7890/agents
```

Dashboard with token:

```text
http://localhost:7890/ui?token=YOUR_TOKEN
```

## Architecture

- **MCP (stdio)** — Native for Claude Code sessions
- **HTTP API (FastAPI)** — For any agent that can make HTTP requests
- **CLI (Click)** — For agents that can only run bash commands
- **SQLite** — Persistent storage at `~/.agentbridge/messages.db`

Messages auto-expire after 24h. Agents auto-expire after 1h of inactivity.

Logs are written to `~/.agentbridge/logs/agentbridge.log` with rotation.

## Load Test

```bash
python scripts/load_test.py --workers 8 --messages-per-worker 100
```

## Event Schema Registry

Event types can be validated at runtime without code changes.

Built-in schemas include:
- `note.text`
- `task.update` (requires `metadata.job_id`)
- `task.*` wildcard (requires `metadata.job_id`)
- `artifact.created`
- `run.result`

Schema matching precedence:
1. Exact event type (e.g. `task.reviewed`)
2. Most specific wildcard prefix (e.g. `task.review.*`)
3. Global wildcard (`*`)

Schemas can inherit from others via `extends`:

```json
{
  "extends": "task.*",
  "required": ["reviewer"],
  "properties": {
    "reviewer": {"type": "string"}
  }
}
```

Register custom schema:

```bash
ab register-schema build.finished ./build_finished.schema.json
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

## Running Tests

```bash
source .venv/bin/activate
pytest tests/ -v
```
