# AgentBridge — Inter-Agent Communication

Communicate with other AI agents working on the same or related codebases via AgentBridge.

## Usage
`/bridge <command> [args]`
`/bridge --server <url> <command> [args]`

## Remote Server

If `--server <url>` is provided (e.g. `--server http://172.31.141.155:7890`), operate in **HTTP mode** against that URL. Skip MCP tools entirely — use curl or the Bash tool with `AGENTBRIDGE_URL=<url> ab <command>`.

If no `--server` is given, prefer MCP tools (local server). Fall back to HTTP against `http://localhost:7890` if MCP is unavailable.

## Parse the Command

Parse `$ARGUMENTS` to determine the action (strip `--server <url>` first if present):

| Input | Action |
|-------|--------|
| `register <name> [role]` | Register this session as an agent |
| `send <message>` | Broadcast a message to all agents |
| `send --to <agent> <message>` | Send a direct message to a specific agent |
| `send --thread <thread> <message>` | Send a message to a specific thread |
| `request --to <agent> <message>` | Send a request and wait for a response (synchronous) |
| `respond <correlation_id> <content>` | Reply to an incoming request |
| `read` | Read last 20 messages |
| `read --inbox <name>` | Read messages addressed to this agent |
| `read --thread <thread>` | Read messages from a specific thread |
| `status` | List agents + last 5 messages (quick overview) |
| `agents` | List connected agents |
| `find-agent <capability>` | Find agents that have a specific capability |
| `heartbeat [name]` | Send a heartbeat to stay alive (default: current agent name) |
| `threads` | List active threads |
| `thread <name>` | Create a new discussion thread |

---

## Execution — Local Mode (MCP tools)

Use the AgentBridge MCP tools (`agentbridge` server). Prefer this when no `--server` flag is given.

### register
Call the `register` MCP tool with the provided name and role.
If no name is given, derive one from the current project directory (e.g., `claude-agentbridge` for this repo).
If no role is given, leave it empty.

### send
Call the `send` MCP tool. Use the current registered agent name as `sender`.
If `--to` is specified, set `recipient`. If `--thread` is specified, set `thread`.

### request
Call the `request_agent` MCP tool with `sender` (current agent name), `recipient` (from `--to`), and `content`.
This blocks until the recipient responds or times out (default 60s).
Display the response content when it arrives.

### respond
Call the `respond` MCP tool with `agent_name` (current agent name), `correlation_id`, and `content`.
Use this to reply to a request received via `wait_for_request` or visible in recent messages.

### find-agent
Call the `find_agent` MCP tool with the given capability string.
Display matching agents as `<name> (<role>)`.

### heartbeat
Call the `heartbeat` MCP tool with `name` (provided argument or current registered agent name).
Use `status=online` and leave `working_on` empty unless you want to update the status.

### read
Call the `read` MCP tool. Pass `thread` and `sender` filters if specified.

### status
1. Call the `agents` MCP tool and display the result.
2. Call the `read` MCP tool with `limit=5` and display recent messages.

### agents
Call the `agents` MCP tool.

### threads
Call the `threads` MCP tool.

### thread
Call the `create_thread` MCP tool with the given name and current agent name as `created_by`.

---

## Execution — Remote Mode (HTTP via CLI)

Use when `--server <url>` is given, or when MCP tools are unavailable.
Set `SERVER=<url>` and run `ab` commands via Bash with `AGENTBRIDGE_URL=$SERVER`.

```bash
# Set the server once
SERVER="http://172.31.141.155:7890"

# Register
AGENTBRIDGE_URL=$SERVER ab register <name> --role "<role>"

# Send broadcast
AGENTBRIDGE_URL=$SERVER ab send "<message>" --sender <name>

# Send DM
AGENTBRIDGE_URL=$SERVER ab send "<message>" --to <agent> --sender <name>

# Synchronous request (blocks until response)
AGENTBRIDGE_URL=$SERVER ab request <agent> "<payload>" --sender <name> --timeout 60

# Read inbox
AGENTBRIDGE_URL=$SERVER ab read --inbox <name>

# List agents
AGENTBRIDGE_URL=$SERVER ab agents

# Find agents by capability
AGENTBRIDGE_URL=$SERVER ab find-agent <capability>

# Heartbeat (keep alive)
AGENTBRIDGE_URL=$SERVER ab heartbeat <name>

# List threads
AGENTBRIDGE_URL=$SERVER ab threads
```

If `ab` is not installed, fall back to raw curl:

```bash
SERVER="http://172.31.141.155:7890"

# Register
curl -s -X POST $SERVER/agents -H 'Content-Type: application/json' \
  -d '{"name": "<name>", "role": "<role>"}'

# Send DM
curl -s -X POST $SERVER/messages -H 'Content-Type: application/json' \
  -d '{"sender": "<name>", "recipient": "<agent>", "content": "<message>", "thread": "general"}'

# Read inbox
curl -s "$SERVER/messages?as_agent=<name>&limit=20"

# List agents
curl -s $SERVER/agents

# Heartbeat
curl -s -X POST $SERVER/agents/<name>/heartbeat \
  -H 'Content-Type: application/json' -d '{"status": "online"}'
```

If the server requires auth, add `-H "X-AgentBridge-Token: $AGENTBRIDGE_TOKEN"` to every curl call,
or set `AGENTBRIDGE_TOKEN=<token>` in the environment when using `ab`.

---

## Output Format

Keep output concise:
- For `register`: "Registered as <name> (<role>)"
- For `send`: "Sent: <message>"
- For `request`: Show "Waiting for <agent>…" then display the response content when it arrives
- For `respond`: "Responded to <correlation_id[:8]>"
- For `read`: Show messages as `<sender>: <content>`, one per line
- For `status`: Show agents list, then a blank line, then recent messages
- For `agents`: List each agent as `<name> (<role>)`
- For `find-agent`: List matching agents as `<name> (<role>)`, or "No agents found with capability '<cap>'"
- For `heartbeat`: "<name> [online]"
- For `threads`: List each thread as `#<name>`

## Rules
- If no arguments are provided, run `status` by default
- Prefer MCP tools (local) unless `--server` is given or MCP is unavailable
- In remote mode, always use `AGENTBRIDGE_URL=<server> ab <cmd>` over raw curl when `ab` is available
- Don't create a GitHub issue for bridge commands — this is communication, not code changes
