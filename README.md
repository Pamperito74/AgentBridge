# AgentBridge

**Production-ready multi-agent coordination layer.** AgentBridge provides real-time A2A (Agent-to-Agent) communication, room-based orchestration, task management, and MCP (Model Context Protocol) server support — all in a single deployable service.

## Quick Start (Docker)

```bash
# One-command install
curl -fsSL https://raw.githubusercontent.com/Pamperito74/AgentBridge/main/install.sh | bash
```

This generates a secure API token, downloads `docker-compose.yml`, and starts the service at **http://localhost:4000**.

Or manually:

```bash
cp .env.example .env
# Edit .env — set AGENTBRIDGE_TOKEN to a secure random string
docker compose up -d
```

## Configuration

All configuration is via environment variables. Copy `.env.example` to `.env` and set:

| Variable | Required | Description |
|----------|----------|-------------|
| `AGENTBRIDGE_TOKEN` | **Yes** | Bearer token for API and WebSocket auth |
| `PORT` | No | HTTP port (default: `4000`) |
| `NODE_ENV` | No | `production` or `development` |
| `PAPERCLIP_API_TOKEN` | No | Token for Paperclip integration |
| `PAPERCLIP_API_URL` | No | Paperclip API base URL |

The server **refuses to start** if `AGENTBRIDGE_TOKEN` is not set.

## Authentication

All `/api/*` endpoints require a Bearer token:

```bash
curl -H "Authorization: Bearer $AGENTBRIDGE_TOKEN" http://localhost:4000/api/agents
```

WebSocket connections require a `token` query parameter:

```
ws://localhost:4000/a2a?agent_id=my-agent&token=$AGENTBRIDGE_TOKEN
```

The `/health` endpoint is unauthenticated (for liveness probes).

## REST API

### Agents

| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET` | `/api/agents` | List all agents |
| `POST` | `/api/agents/register` | Register or update an agent |

**Register agent:**
```json
POST /api/agents/register
{ "id": "cto-001", "name": "CTO Agent", "role": "cto" }
```

### Rooms

| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET` | `/api/rooms` | List all rooms |
| `POST` | `/api/rooms` | Create a room |
| `GET` | `/api/rooms/:id` | Get room by ID |
| `POST` | `/api/rooms/:id/message` | Post a message to a room |

### Tasks

| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET` | `/api/tasks` | List tasks (optional `?room_id=`) |
| `POST` | `/api/tasks` | Create a task |
| `POST` | `/api/pm/plan` | Auto-plan tasks for a goal |

### Tools

| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET` | `/api/tools` | List available tools |
| `POST` | `/api/tools/execute` | Execute a tool |

### Other

| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET` | `/api/threads/:id` | Get thread message history |
| `GET` | `/api/topology` | Agent network graph |
| `GET` | `/api/paperclip/agents` | Paperclip-sourced agents |
| `GET` | `/api/paperclip/tasks` | Paperclip-sourced tasks |
| `GET` | `/health` | Liveness check (unauthenticated) |

## WebSocket (A2A Protocol)

Connect at `/a2a?agent_id=<id>&token=<AGENTBRIDGE_TOKEN>`.

Send messages in A2A/1.0 format:

```json
{
  "protocol": "A2A/1.0",
  "sender": "cto-001",
  "receiver": "devops-001",
  "message_type": "task",
  "content": "Deploy v1.2 to staging",
  "thread_id": "deploy-thread-01"
}
```

- Set `receiver` to route directly to an agent.
- Set `room_id` to broadcast to all agents in a room.
- Omit both to broadcast to all connected agents.

If the receiver is offline, you get a `warning` response with the message echoed back.

## MCP Server

For AI client integration via the Model Context Protocol:

```bash
npm run start:mcp
```

Starts an MCP stdio server. Connect Claude Desktop or other MCP-compatible clients via this transport.

## Development

```bash
npm install
cp .env.example .env   # Set AGENTBRIDGE_TOKEN
npm run dev            # Server with hot reload on :4000
npm run dev:mcp        # MCP stdio server
npm test               # Run tests
npm run seed           # Seed sample agents and rooms
```

## Deployment

### Docker Compose (recommended)

```bash
docker compose up -d
docker compose logs -f agentbridge
docker compose down
```

Data is persisted in the `agentbridge_data` Docker volume.

### Manual Docker

```bash
docker build -t agentbridge .
docker run -d \
  -p 4000:4000 \
  -e AGENTBRIDGE_TOKEN=your-secret-token \
  -v agentbridge_data:/app/data \
  agentbridge
```

## Architecture

```
┌─────────────────────────────────────────────┐
│               AgentBridge                   │
│                                             │
│  ┌──────────┐  ┌──────────┐  ┌──────────┐  │
│  │  Express │  │   WS     │  │   MCP    │  │
│  │  REST    │  │  A2A     │  │  stdio   │  │
│  │   API    │  │ Router   │  │  server  │  │
│  └────┬─────┘  └────┬─────┘  └──────────┘  │
│       │             │                       │
│  ┌────▼─────────────▼──────────────────┐   │
│  │        AgentBridgeStore              │   │
│  │  (agents, rooms, tasks, messages)    │   │
│  └─────────────────────────────────────┘   │
│                    │                        │
│             [data/state.json]               │
└─────────────────────────────────────────────┘
```

- **REST API**: CRUD for agents, rooms, tasks, tools — all auth-gated
- **WebSocket A2A**: Real-time agent-to-agent routing with token auth
- **Store**: In-memory state with JSON file persistence
- **MCP Server**: Stdio transport for AI client integration

## Health & Monitoring

```bash
curl http://localhost:4000/health
# {"ok":true,"time":"2026-03-14T09:00:00.000Z"}
```

Docker Compose includes a health check that polls `/health` every 30s with automatic restart.

## Paperclip Adapter

Set environment variables to connect to the Paperclip orchestration platform:

- `PAPERCLIP_API_URL`
- `PAPERCLIP_API_TOKEN`

If unset, the adapter returns mock data (useful for local development).

## License

MIT
