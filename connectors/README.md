# AgentBridge Connectors

Drop-in agents that extend AgentBridge with local execution capabilities.

---

## `task_executor.py` — Local Task Executor

Registers as an agent on AgentBridge and executes tasks dispatched by any other agent (Claude Code sessions, scripts, etc.).

### Start

```bash
# From the AgentBridge root — starts server + executor together
python start.py
```

Optional environment variables:

| Variable | Default | Description |
|---|---|---|
| `AGENTBRIDGE_URL` | `http://localhost:7890` | AgentBridge server URL |
| `EXECUTOR_NAME` | `task-executor` | Agent name on the bridge |
| `EXECUTOR_POLL_INTERVAL` | `3` | Polling interval in seconds |
| `AIDER_MODEL` | `ollama/qwen2.5-coder:7b` | Model used for `aider` tasks |

### Supported Task Types

Send a JSON payload to `task-executor` from any agent:

#### `shell` — Run a shell command
```json
{
  "type": "shell",
  "command": "npm test -- --testPathPattern=auth",
  "cwd": "/Users/you/project"
}
```

#### `git_commit` — Stage all and commit
```json
{
  "type": "git_commit",
  "message": "fix(auth): handle null session edge case",
  "cwd": "/Users/you/project"
}
```

#### `git_pr` — Create a pull request via `gh` CLI
```json
{
  "type": "git_pr",
  "title": "fix(auth): handle null session",
  "body": "Closes #123\n\nFixed by Claude agent.",
  "base": "develop",
  "cwd": "/Users/you/project"
}
```

#### `aider` — Run aider against files with a prompt
```json
{
  "type": "aider",
  "prompt": "Refactor this function to use async/await",
  "files": ["src/auth/auth.service.ts"],
  "model": "ollama/qwen2.5-coder:7b",
  "cwd": "/Users/you/project"
}
```

#### `test` — Run a test command
```json
{
  "type": "test",
  "command": "pytest -x tests/",
  "cwd": "/Users/you/project"
}
```

### Sending Tasks from Claude Code

```
/bridge send --to task-executor '{"type":"shell","command":"npm test","cwd":"/project"}'
```

Or via HTTP:
```bash
curl -X POST http://localhost:7890/messages \
  -H "Content-Type: application/json" \
  -d '{
    "sender": "me",
    "recipient": "task-executor",
    "content": "{\"type\":\"shell\",\"command\":\"npm test\",\"cwd\":\"/project\"}"
  }'
```

### Response Shape

The executor replies to the sender with:
```json
{
  "success": true,
  "exit_code": 0,
  "stdout": "...",
  "stderr": ""
}
```

On failure:
```json
{
  "success": false,
  "exit_code": 1,
  "stderr": "Error message...",
  "stdout": ""
}
```

---

## Multi-Agent Workflow Example

```
Terminal 1: python start.py                  # AgentBridge + executor
Terminal 2: claude (Session 1 — Planner)
Terminal 3: claude (Session 2 — Implementer)
```

**Planner posts a task:**
```
/bridge send --thread feature-42 "Implement null check in auth.service.ts line 84"
```

**Implementer reads and acts:**
```
/bridge read --thread feature-42
# ... writes the fix ...
/bridge send --to task-executor '{"type":"test","command":"npm test","cwd":"/project"}'
```

**Executor runs tests and replies — Implementer reads results:**
```
/bridge read --from task-executor
# {"success":true,"exit_code":0,"stdout":"All tests passed"}
```

**Implementer commits and opens PR:**
```
/bridge send --to task-executor '{"type":"git_commit","message":"fix(auth): null check","cwd":"/project"}'
/bridge send --to task-executor '{"type":"git_pr","title":"fix(auth): null check","body":"Closes #42","cwd":"/project"}'
```
