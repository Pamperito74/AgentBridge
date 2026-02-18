import click
import requests
import json
import sys

BASE_URL = "http://localhost:7890"


def _post(path: str, data: dict) -> dict:
    try:
        r = requests.post(f"{BASE_URL}{path}", json=data, timeout=5)
        r.raise_for_status()
        return r.json()
    except requests.ConnectionError:
        click.echo("Error: AgentBridge server not running. Start it with: ab serve", err=True)
        sys.exit(1)


def _get(path: str, params: dict | None = None) -> dict | list:
    try:
        r = requests.get(f"{BASE_URL}{path}", params=params, timeout=5)
        r.raise_for_status()
        return r.json()
    except requests.ConnectionError:
        click.echo("Error: AgentBridge server not running. Start it with: ab serve", err=True)
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
def send(content: str, recipient: str | None, thread: str, sender: str):
    """Send a message."""
    data = {"sender": sender, "content": content, "thread": thread}
    if recipient:
        data["recipient"] = recipient
    result = _post("/messages", data)
    click.echo(f"[{result['thread']}] {result['sender']}: {result['content']}")


@cli.command()
@click.option("--thread", "-t", default=None, help="Filter by thread")
@click.option("--sender", "-s", default=None, help="Filter by sender")
@click.option("--limit", "-n", default=20, help="Number of messages")
def read(thread: str | None, sender: str | None, limit: int):
    """Read recent messages."""
    params = {"limit": limit}
    if thread:
        params["thread"] = thread
    if sender:
        params["sender"] = sender
    messages = _get("/messages", params)
    if not messages:
        click.echo("No messages.")
        return
    for msg in messages:
        prefix = f"[{msg['thread']}] " if msg["thread"] != "general" else ""
        to = f" → {msg['recipient']}" if msg.get("recipient") else ""
        click.echo(f"{prefix}{msg['sender']}{to}: {msg['content']}")


@cli.command()
def agents():
    """List connected agents."""
    result = _get("/agents")
    if not result:
        click.echo("No agents connected.")
        return
    for a in result:
        role = f" ({a['role']})" if a.get("role") else ""
        click.echo(f"  {a['name']}{role}")


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
@click.option("--host", default="0.0.0.0", help="Bind host")
@click.option("--port", "-p", default=7890, help="Bind port")
def serve(host: str, port: int):
    """Start the AgentBridge HTTP server."""
    click.echo(f"Starting AgentBridge on {host}:{port}")
    import uvicorn
    from agentbridge.server import http_app
    uvicorn.run(http_app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    cli()
