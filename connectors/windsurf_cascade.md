# Windsurf Cascade — AgentBridge Agent Instructions

Paste this into a Cascade chat session to activate it as an AgentBridge agent.
Cascade will register itself, poll for tasks from Claude, implement them using
its free AI models, and post results back — saving Claude API tokens for
planning and review only.

---

## Activation prompt (paste into Cascade)

```
You are now operating as an AgentBridge agent named "cascade".

AgentBridge is running at http://localhost:7890. You have access to it via
the agentbridge MCP tools (register, send, read, agents, heartbeat).

Follow this protocol for the rest of this session:

1. REGISTER — Call: register(name="cascade", role="implementer")

2. POLL — Every time I ask you to check for work, call:
   read(as_agent="cascade", limit=10)
   Look for messages with a JSON payload containing a "type" field.

3. IMPLEMENT — For each task:
   - "code_edit": Edit the files specified in the task using your own
     judgment. Apply the change. Reply with what you changed and why.
   - "review": Read the files listed and post your findings back.
   - "explain": Explain the code at the given file/line to the sender.
   - Any task with no recognized type: reply asking for clarification.

4. REPLY — After completing a task, call:
   send(recipient=<sender>, content=<result_as_json>, thread=<thread>)
   Always include: {"success": true/false, "summary": "...", "details": "..."}

5. HEARTBEAT — When asked to check for work, also call:
   heartbeat(name="cascade", status="online")

Never stop the session. Keep checking for work whenever I prompt you.
You are the implementation agent — Claude plans, you build, Claude reviews.
This saves Claude API tokens for high-level decisions only.
```

---

## How Claude sends tasks to Cascade

From any Claude Code session:

```
/bridge send --to cascade '{
  "type": "code_edit",
  "description": "Add null check before user.id access in auth.service.ts line 84",
  "files": ["apps/api/src/auth/auth.service.ts"],
  "cwd": "/Users/you/project",
  "thread": "feature-42"
}'
```

```
/bridge send --to cascade '{
  "type": "review",
  "description": "Review the auth service for security issues",
  "files": ["apps/api/src/auth/auth.service.ts"],
  "thread": "security-review"
}'
```

```
/bridge send --to cascade '{
  "type": "explain",
  "description": "Explain what verifyHashChain does and its complexity",
  "files": ["apps/api/src/devops/devops.service.ts"],
  "line": 431,
  "thread": "code-qa"
}'
```

---

## Full workflow example

**Terminal 1:** AgentBridge running (`python run_server.py`)
**Windsurf:** Cascade active with the activation prompt above
**Claude Code:** Planner session

```
Claude → /bridge send --to cascade '{"type":"code_edit","description":"Extract the
         pricing calculation into a separate pure function","files":["src/quote.ts"],
         "thread":"refactor-7"}'

Cascade → reads task → edits src/quote.ts → replies:
          {"success":true,"summary":"Extracted calculatePrice() — 23 lines moved",
           "details":"New function at line 45, original call site updated at line 112"}

Claude → /bridge read --from cascade --thread refactor-7
       → reviews result → approves or requests changes
       → /bridge send --to task-executor '{"type":"test","command":"npm test","cwd":"/project"}'
       → tests pass → /bridge send --to task-executor '{"type":"git_commit",...}'
```

**Token split:**
- Claude: planning, reviewing results, git/test dispatch (~10% of tokens)
- Cascade: reading files, editing code, writing summaries (~90% of work, free)

---

## Cascade limitations to work around

| Limitation | Workaround |
|---|---|
| Cascade can't run shell commands | Use `task-executor` for shell/git/test tasks |
| Cascade sessions don't auto-poll | Prompt Cascade periodically: "check for new tasks" |
| Cascade can't write files headlessly | It edits in the Windsurf editor — files save to disk automatically |
| No persistent state between sessions | Re-run the activation prompt at the start of each Windsurf session |

---

## Token cost comparison

| Operation | Without integration | With Cascade |
|---|---|---|
| Implement a 50-line feature | Claude: ~2,000 tokens | Claude: ~200 tokens (plan+review only) |
| Code review of a service | Claude: ~1,500 tokens | Claude: ~100 tokens (read Cascade's summary) |
| Refactor a module | Claude: ~3,000 tokens | Claude: ~300 tokens |
| Explain complex code | Claude: ~800 tokens | Claude: ~50 tokens |

**Rough saving: ~85–90% of implementation tokens move to Cascade (free).**
