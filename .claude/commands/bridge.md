# AgentBridge — Inter-Agent Communication

Communicate with other AI agents working on the same or related codebases via AgentBridge.

## Usage
`/bridge <command> [args]`

## Parse the Command

Parse `$ARGUMENTS` to determine the action:

| Input | Action |
|-------|--------|
| `register <name> [role]` | Register this session as an agent |
| `send <message>` | Broadcast a message to all agents |
| `send --to <agent> <message>` | Send a direct message to a specific agent |
| `send --thread <thread> <message>` | Send a message to a specific thread |
| `request --to <agent> <message>` | Send a request and wait for a response (synchronous) |
| `respond <correlation_id> <content>` | Reply to an incoming request |
| `read` | Read last 20 messages |
| `read --thread <thread>` | Read messages from a specific thread |
| `status` | List agents + last 5 messages (quick overview) |
| `agents` | List connected agents |
| `find-agent <capability>` | Find agents that have a specific capability |
| `heartbeat [name]` | Send a heartbeat to stay alive (default: current agent name) |
| `threads` | List active threads |
| `thread <name>` | Create a new discussion thread |

## Execution

Use the AgentBridge MCP tools to execute the action. The MCP server is registered as `agentbridge`.

### register
Call the `register` MCP tool with the provided name and role.
If no name is given, derive one from the current project directory (e.g., `claude-inline3` for Inline-3, `claude-ct` for CirrusTranslate).
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

## Fallback: HTTP Mode

If MCP tools are not available (e.g., the agentbridge MCP server isn't registered), fall back to HTTP calls:

```bash
# Register
curl -s -X POST http://localhost:7890/agents -H 'Content-Type: application/json' -d '{"name": "<name>", "role": "<role>"}'

# Send
curl -s -X POST http://localhost:7890/messages -H 'Content-Type: application/json' -d '{"sender": "<name>", "content": "<message>", "thread": "<thread>"}'

# Read
curl -s http://localhost:7890/messages?limit=20

# Agents
curl -s http://localhost:7890/agents

# Threads
curl -s http://localhost:7890/threads
```

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
- Always prefer MCP tools over HTTP fallback
- Don't create a GitHub issue for bridge commands — this is communication, not code changes
