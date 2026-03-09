import click
import requests
import sys
import os
from datetime import datetime
from pathlib import Path
import sqlite3
import json

BASE_URL = "http://localhost:7890"
AUTH_TOKEN = os.environ.get("AGENTBRIDGE_TOKEN", "")


def _headers() -> dict:
    if not AUTH_TOKEN:
        return {}
    return {"X-AgentBridge-Token": AUTH_TOKEN}


def _check_log_dir() -> tuple[bool, str]:
    configured = os.environ.get("AGENTBRIDGE_LOG_DIR")
    candidates = []
    if configured:
        candidates.append(Path(configured).expanduser())
    candidates.extend([
        Path.home() / ".agentbridge" / "logs",
        Path.cwd() / ".agentbridge" / "logs",
        Path("/tmp") / "agentbridge-logs",
    ])
    for candidate in candidates:
        try:
            candidate.mkdir(parents=True, exist_ok=True)
            probe = candidate / ".write-probe"
            probe.write_text("ok")
            probe.unlink(missing_ok=True)
            return True, str(candidate)
        except OSError:
            continue
    return False, "no writable log directory found"


def _post(path: str, data: dict) -> dict:
    try:
        r = requests.post(f"{BASE_URL}{path}", json=data, timeout=5, headers=_headers())
        r.raise_for_status()
        return r.json()
    except requests.ConnectionError:
        click.echo("Error: AgentBridge server not running. Start it with: ab serve", err=True)
        sys.exit(1)
    except requests.HTTPError as e:
        detail = e.response.text if e.response is not None else str(e)
        click.echo(f"HTTP error: {detail}", err=True)
        sys.exit(1)
    except requests.RequestException as e:
        click.echo(f"Request failed: {e}", err=True)
        sys.exit(1)


def _get(path: str, params: dict | None = None) -> dict | list:
    try:
        r = requests.get(f"{BASE_URL}{path}", params=params, timeout=5, headers=_headers())
        r.raise_for_status()
        return r.json()
    except requests.ConnectionError:
        click.echo("Error: AgentBridge server not running. Start it with: ab serve", err=True)
        sys.exit(1)
    except requests.HTTPError as e:
        detail = e.response.text if e.response is not None else str(e)
        click.echo(f"HTTP error: {detail}", err=True)
        sys.exit(1)
    except requests.RequestException as e:
        click.echo(f"Request failed: {e}", err=True)
        sys.exit(1)


@click.group()
def cli():
    """AgentBridge — Inter-agent communication for AI agents."""
    pass


@cli.command()
@click.argument("name")
@click.option("--role", "-r", default="", help="Agent role description")
def register(name: str, role: str):
    """Register this agent with the bridge."""
    result = _post("/agents", {"name": name, "role": role})
    click.echo(f"Registered: {result['name']} ({result.get('role', '')})")


@cli.command()
@click.argument("content")
@click.option("--to", "recipient", default=None, help="Recipient agent name")
@click.option("--thread", "-t", default="general", help="Thread name")
@click.option("--sender", "-s", default="cli", help="Sender name")
@click.option("--type", "msg_type", default="chat", type=click.Choice(["chat", "request", "status", "alert"]), help="Message type")
def send(content: str, recipient: str | None, thread: str, sender: str, msg_type: str):
    """Send a message."""
    data = {"sender": sender, "content": content, "thread": thread, "msg_type": msg_type}
    if recipient:
        data["recipient"] = recipient
    result = _post("/messages", data)
    type_tag = f"[{result['msg_type']}] " if result.get("msg_type", "chat") != "chat" else ""
    click.echo(f"{type_tag}[{result['thread']}] {result['sender']}: {result['content']}")


@cli.command()
@click.option("--thread", "-t", default=None, help="Filter by thread")
@click.option("--sender", "-s", default=None, help="Filter by sender")
@click.option("--inbox", "-i", default=None, help="Show only messages to this agent (inbox mode)")
@click.option("--before", default=None, help="Only messages before this ISO timestamp (pagination)")
@click.option("--since-id", default=None, help="Only messages after this message ID")
@click.option("--cursor-agent", default=None, help="Agent name to store cursor under (--save-cursor required)")
@click.option("--cursor-thread", default=None, help="Thread name when storing cursor; falls back to --thread or message thread")
@click.option("--save-cursor/--no-save-cursor", default=False, help="Persist cursor after reading (requires --cursor-agent)")
@click.option("--limit", "-n", default=20, help="Number of messages")
def read(thread: str | None, sender: str | None, inbox: str | None, before: str | None, since_id: str | None, cursor_agent: str | None, cursor_thread: str | None, save_cursor: bool, limit: int):
    """Read recent messages. Use --inbox NAME to see only your messages."""
    params = {"limit": limit}
    if thread:
        params["thread"] = thread
    if sender:
        params["sender"] = sender
    if inbox:
        params["as_agent"] = inbox
    if before:
        params["before"] = before
    if since_id:
        params["since_id"] = since_id
    messages = _get("/messages", params)
    if not messages:
        click.echo("No messages.")
        return
    for msg in messages:
        prefix = f"[{msg['thread']}] " if msg["thread"] != "general" else ""
        to = f" -> {msg['recipient']}" if msg.get("recipient") else ""
        type_tag = f"[{msg.get('msg_type', 'chat')}] " if msg.get("msg_type", "chat") != "chat" else ""
        artifacts = f" [{len(msg.get('artifacts', []))} artifacts]" if msg.get("artifacts") else ""
        click.echo(f"{type_tag}{prefix}{msg['sender']}{to}: {msg['content']}{artifacts}")
    if save_cursor:
        if not cursor_agent:
            click.echo("Cannot save cursor: --cursor-agent is required when --save-cursor is set.", err=True)
            raise click.exceptions.Exit(1)
        last_msg = messages[-1]
        target_thread = cursor_thread or thread or last_msg.get("thread") or "general"
        payload = {
            "agent_name": cursor_agent,
            "thread": target_thread,
            "last_message_id": last_msg["id"],
        }
        ts = last_msg.get("timestamp")
        if ts:
            payload["last_timestamp"] = ts
        _post("/cursors", payload)
        click.echo(f"Cursor saved for {cursor_agent} on thread {target_thread}.")


@cli.group()
def cursor():
    """Manage persisted delivery cursors."""
    pass


@cursor.command("list")
@click.option("--agent", default=None, help="Only list cursors for this agent")
def cursor_list(agent: str | None):
    params = {"agent": agent} if agent else None
    result = _get("/cursors", params)
    if not result:
        click.echo("No saved cursors.")
        return
    for cursor in result:
        thread = cursor.get("thread", "general")
        msg_id = cursor.get("last_message_id") or "<none>"
        ts = cursor.get("last_timestamp") or "-"
        click.echo(f"{cursor['agent_name']}#{thread}: last={msg_id} @ {ts}")


@cursor.command("set")
@click.argument("agent")
@click.argument("message_id")
@click.option("--thread", "-t", default="general", help="Thread name")
@click.option("--timestamp", "-ts", default=None, help="Optional ISO timestamp")
def cursor_set(agent: str, message_id: str, thread: str, timestamp: str | None):
    payload = {
        "agent_name": agent,
        "thread": thread,
        "last_message_id": message_id,
    }
    if timestamp:
        payload["last_timestamp"] = timestamp
    _post("/cursors", payload)
    click.echo(f"Cursor stored for {agent} on thread {thread}.")


@cli.command()
def agents():
    """List connected agents."""
    result = _get("/agents")
    if not result:
        click.echo("No agents connected.")
        return
    for a in result:
        role = f" ({a['role']})" if a.get("role") else ""
        status = a.get("status", "online")
        working = f" — {a['working_on']}" if a.get("working_on") else ""
        click.echo(f"  {a['name']}{role} [{status}]{working}")


@cli.command()
def actors():
    """List active actors (generic alias for agents)."""
    result = _get("/actors")
    if not result:
        click.echo("No actors connected.")
        return
    for a in result:
        role = f" ({a.get('role', '')})" if a.get("role") else ""
        working = f" — {a['working_on']}" if a.get("working_on") else ""
        click.echo(f"  {a['id']}<{a.get('type', 'agent')}>{role} [{a.get('status', 'online')}]{working}")


@cli.command()
@click.argument("name")
@click.option("--status", "-s", default="online", type=click.Choice(["online", "busy", "idle"]), help="Agent status")
@click.option("--working-on", "-w", default="", help="Brief description of current task")
def heartbeat(name: str, status: str, working_on: str):
    """Update agent status and heartbeat."""
    result = _post(f"/agents/{name}/heartbeat", {"status": status, "working_on": working_on})
    working = f" — {result.get('working_on', '')}" if result.get("working_on") else ""
    click.echo(f"{result['name']} [{result.get('status', 'online')}]{working}")


@cli.command()
def threads():
    """List active threads."""
    result = _get("/threads")
    if not result:
        click.echo("No threads.")
        return
    for t in result:
        click.echo(f"  #{t['name']} (by {t['created_by']})")


@cli.command()
@click.argument("name")
@click.option("--creator", "-c", default="cli", help="Creator name")
def create_thread(name: str, creator: str):
    """Create a new discussion thread."""
    result = _post("/threads", {"name": name, "created_by": creator})
    click.echo(f"Created thread: #{result['name']}")


@cli.command()
@click.argument("thread_name")
def summary(thread_name: str):
    """Get a summary of a thread."""
    result = _get(f"/threads/{thread_name}/summary")
    click.echo(f"#{result['name']}: {result['message_count']} messages")
    if result["participants"]:
        click.echo(f"  Participants: {', '.join(result['participants'])}")
    if result.get("last_message_at"):
        click.echo(f"  Last activity: {result['last_message_at']}")
    if result.get("last_message_preview"):
        click.echo(f"  Last: {result['last_message_preview']}")


@cli.command()
@click.argument("content")
@click.option("--actor-id", required=True, help="Actor identifier")
@click.option("--actor-type", default="agent", help="Actor type (agent, tool, human, service...)")
@click.option("--target-id", default=None, help="Optional target identifier")
@click.option("--target-type", default=None, help="Optional target type")
@click.option("--thread", "-t", default="general", help="Thread name")
@click.option("--event-type", default="note.text", help="Event type name")
@click.option("--label", "labels", multiple=True, help="Add label (repeatable)")
def emit(content: str, actor_id: str, actor_type: str, target_id: str | None, target_type: str | None, thread: str, event_type: str, labels: tuple[str, ...]):
    """Emit a generic bus event."""
    payload = {
        "actor_id": actor_id,
        "actor_type": actor_type,
        "target_id": target_id,
        "target_type": target_type,
        "thread": thread,
        "event_type": event_type,
        "content": content,
        "labels": list(labels),
        "metadata": {},
    }
    result = _post("/bus/events", payload)
    click.echo(f"[{result['event_type']}] {result['actor_id']} -> {result.get('target_id') or '*'}: {result['content']}")


@cli.command()
@click.option("--thread", "-t", default=None, help="Filter by thread")
@click.option("--actor-id", default=None, help="Filter by actor")
@click.option("--target-id", default=None, help="Filter by target")
@click.option("--event-type", default=None, help="Filter by event type")
@click.option("--before", default=None, help="Only events before this ISO timestamp")
@click.option("--since-id", default=None, help="Only events after this message ID")
@click.option("--limit", "-n", default=20, help="Number of events")
def events(thread: str | None, actor_id: str | None, target_id: str | None, event_type: str | None, before: str | None, since_id: str | None, limit: int):
    """Read generic bus events."""
    params = {"limit": limit}
    if thread:
        params["thread"] = thread
    if actor_id:
        params["actor_id"] = actor_id
    if target_id:
        params["target_id"] = target_id
    if event_type:
        params["event_type"] = event_type
    if before:
        params["before"] = before
    if since_id:
        params["since_id"] = since_id
    if since_id:
        params["since_id"] = since_id
    result = _get("/bus/events", params)
    if not result:
        click.echo("No events.")
        return
    for e in result:
        click.echo(f"[{e.get('event_type', 'note.text')}] {e.get('actor_id', e.get('sender'))} -> {e.get('target_id') or '*'}: {e['content']}")


@cli.command("schemas")
def list_schemas():
    """List event schemas currently registered in the bus."""
    result = _get("/bus/schemas")
    if not result:
        click.echo("No schemas.")
        return
    for event_type in sorted(result.keys()):
        schema = result[event_type]
        required = ", ".join(schema.get("required", [])) or "-"
        click.echo(f"{event_type}: required=[{required}]")


@cli.command("register-schema")
@click.argument("event_type")
@click.argument("schema_file")
def register_schema(event_type: str, schema_file: str):
    """Register or update an event schema from a JSON file."""
    path = Path(schema_file).expanduser()
    if not path.exists():
        click.echo(f"Schema file not found: {path}", err=True)
        raise click.exceptions.Exit(1)
    try:
        schema = json.loads(path.read_text())
    except json.JSONDecodeError as e:
        click.echo(f"Invalid JSON: {e}", err=True)
        raise click.exceptions.Exit(1)
    result = _post("/bus/schemas", {"event_type": event_type, "schema": schema})
    click.echo(f"Registered schema: {result['event_type']}")


@cli.command()
@click.option("--host", default="127.0.0.1", help="Bind host")
@click.option("--port", "-p", default=7890, help="Bind port")
@click.option("--public", is_flag=True, help="Bind to all interfaces (same as --host 0.0.0.0)")
def serve(host: str, port: int, public: bool):
    """Start the AgentBridge HTTP server."""
    if public:
        host = "0.0.0.0"
    elif host == "0.0.0.0":
        host = "127.0.0.1"
    click.echo(f"Starting AgentBridge on {host}:{port}")
    click.echo(f"Dashboard: http://localhost:{port}/ui")
    if AUTH_TOKEN:
        click.echo("Auth token enabled via AGENTBRIDGE_TOKEN")
    import uvicorn
    from agentbridge.server import http_app
    uvicorn.run(http_app, host=host, port=port, log_level="info")


@cli.command()
@click.option("--output", "-o", default=None, help="Backup file path (.db). Defaults to ~/.agentbridge/backups/")
def backup(output: str | None):
    """Create a SQLite backup of AgentBridge data."""
    from agentbridge.store import MessageStore
    if output:
        out_path = Path(output).expanduser()
    else:
        stamp = datetime.utcnow().strftime("%Y%m%d-%H%M%S")
        out_path = Path.home() / ".agentbridge" / "backups" / f"messages-{stamp}.db"
    store = MessageStore()
    try:
        saved = store.backup_to(out_path)
        click.echo(f"Backup written: {saved}")
    finally:
        store.close()


@cli.command()
def doctor():
    """Run local diagnostics for AgentBridge setup and runtime."""
    checks: list[tuple[str, bool, str]] = []

    try:
        health = requests.get(f"{BASE_URL}/health", timeout=3).json()
        checks.append(("http_health", True, f"status={health.get('status', 'unknown')}"))
    except Exception as e:
        checks.append(("http_health", False, str(e)))

    if AUTH_TOKEN:
        try:
            r = requests.get(f"{BASE_URL}/agents", timeout=3, headers=_headers())
            ok = r.status_code == 200
            checks.append(("auth_token", ok, f"http_status={r.status_code}"))
        except Exception as e:
            checks.append(("auth_token", False, str(e)))
    else:
        checks.append(("auth_token", True, "not configured (optional)"))

    from agentbridge.store import DB_PATH
    db_path = Path(DB_PATH).expanduser()
    if db_path.exists():
        try:
            conn = sqlite3.connect(str(db_path))
            result = conn.execute("PRAGMA quick_check").fetchone()
            conn.close()
            ok = bool(result and result[0] == "ok")
            checks.append(("db_integrity", ok, f"path={db_path}"))
        except Exception as e:
            checks.append(("db_integrity", False, str(e)))
    else:
        checks.append(("db_integrity", True, f"path={db_path} (will be created on first use)"))

    log_ok, log_msg = _check_log_dir()
    checks.append(("log_dir", log_ok, log_msg))

    failed = 0
    for name, ok, msg in checks:
        mark = "OK" if ok else "FAIL"
        click.echo(f"{mark:4} {name:12} {msg}")
        if not ok:
            failed += 1

    if failed:
        raise click.exceptions.Exit(1)


if __name__ == "__main__":
    cli()
