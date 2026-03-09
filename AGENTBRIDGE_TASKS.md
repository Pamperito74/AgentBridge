## Async Store Refactor Follow-up

1. **Install dependencies in a writable environment** – pip is blocked by the host's externally managed environment, so `pytest` can't be run yet. When a virtualenv is available (or pip is allowed), install the project with `[dev]` extras and rerun `pytest tests/test_store.py` (and any other suites that rely on FastAPI/uvicorn).
2. **Expand async coverage** – the HTTP server now awaits store helpers, but the MCP tools still call the sync `MessageStore`. Consider exposing async versions there if the toolchain evolves (e.g., when adding async handling to `mcp` or CLI senders).
3. **Monitor SSE/WS delivery latency** – once dependencies allow testing, validate that the `asyncio.to_thread` wrappers keep event throughput stable under load and that pruning still runs on schedule.

Keep this file updated if new blockers appear; reference it when resuming the async refactor later.

## Cursor & SSE follow-up

1. **Document the new cursor workflow** – describe `/cursors` (GET/POST) plus the `since_id` query parameter on `/messages`, `/bus/events`, and `/events` so agents know how to persist offsets. Mention that SSE accepts `cursor_agent`, `cursor_thread`, and `since_id` plus a `thread` filter. Highlight the new `ab read --since-id/--save-cursor` options and `/cursor` CLI group.
2. **Add integration tests** – once `pytest` is available, write tests covering cursor CRUD, `since_id` filtering, and SSE backlog delivery (including cursor advancement).
3. **Update CLI helpers** – new `ab cursor list/set` commands and MCP `read()` cursor helpers should be extended with auto-saving workflows once agents start tracking offsets.
