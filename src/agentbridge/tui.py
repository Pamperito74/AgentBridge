"""
AgentBridge Terminal UI
========================
Launch with:  ab tui
             AGENTBRIDGE_URL=http://myserver:7890 ab tui

Keyboard shortcuts
------------------
  Tab / Shift+Tab  — cycle panels
  j / k            — navigate within focused panel
  G                — jump to bottom of chat
  Alt+1…9          — switch to thread by index
  @                — @mention autocomplete in compose
  /                — command palette (/task, /thread, /help)
  ?                — show all keybindings
  Enter            — send message / confirm selection
  Shift+Enter      — newline in compose
  Esc              — dismiss overlay / deselect
  q / Ctrl+C       — quit
"""
from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime, timezone
from typing import Any

import requests

try:
    from textual import on, work
    from textual.app import App, ComposeResult
    from textual.binding import Binding
    from textual.containers import Horizontal, ScrollableContainer, Vertical
    from textual.reactive import reactive
    from textual.screen import ModalScreen
    from textual.widgets import (
        Footer,
        Input,
        Label,
        ListItem,
        ListView,
        Markdown,
        Static,
    )
except ImportError as _e:
    raise ImportError(
        "Textual is required for the TUI. Install it with:\n"
        "  pip install 'agentbridge[tui]'"
    ) from _e


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

STATUS_GLYPH = {
    "online":      ("●", "green"),
    "busy":        ("◑", "yellow"),
    "idle":        ("○", "bright_black"),
    "needs_input": ("⚠", "red"),
    "offline":     ("–", "bright_black"),
}

PRIORITY_COLOR = {
    "critical": "red",
    "high":     "yellow",
    "medium":   "cyan",
    "low":      "bright_black",
}

TASK_STATUS_GLYPH = {
    "todo":        "○",
    "in_progress": "▶",
    "in_review":   "◐",
    "done":        "✓",
    "blocked":     "✗",
    "cancelled":   "–",
}


def _fmt_ts(iso: str) -> str:
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        local = dt.astimezone()
        return local.strftime("%H:%M")
    except Exception:
        return ""


def _headers(token: str) -> dict[str, str]:
    h: dict[str, str] = {}
    if token:
        h["X-AgentBridge-Token"] = token
    return h


# ---------------------------------------------------------------------------
# Widgets
# ---------------------------------------------------------------------------


class StatusBar(Static):
    """Top bar: thread name, SSE status, pending approvals."""

    DEFAULT_CSS = """
    StatusBar {
        height: 1;
        background: $primary-darken-3;
        color: $text;
        padding: 0 1;
        content-align: left middle;
    }
    """

    def update_status(
        self, thread: str, connected: bool, agents: int, approvals: int
    ) -> None:
        conn = "[green]● SSE[/]" if connected else "[red]✗ SSE[/]"
        appr = f"  [red]⚠ {approvals} approval{'s' if approvals != 1 else ''}[/]" if approvals else ""
        self.update(
            f" AgentBridge  [bold]#{thread}[/]  {conn}  {agents} agent{'s' if agents != 1 else ''}{appr}"
            f"   [dim]? help[/]"
        )


class AgentItem(ListItem):
    """Single agent row in the agents panel."""

    def __init__(self, agent: dict) -> None:
        super().__init__()
        self._agent = agent

    def compose(self) -> ComposeResult:
        a = self._agent
        status = a.get("status", "offline")
        glyph, color = STATUS_GLYPH.get(status, ("–", "bright_black"))
        name = a.get("name", "?")
        working = a.get("working_on", "")
        working_str = f"\n  [dim]{working[:38]}[/]" if working else ""
        yield Static(f"[{color}]{glyph}[/] {name}{working_str}")

    def refresh_agent(self, agent: dict) -> None:
        self._agent = agent
        self.query_one(Static).update(self._render_agent())

    def _render_agent(self) -> str:
        a = self._agent
        status = a.get("status", "offline")
        glyph, color = STATUS_GLYPH.get(status, ("–", "bright_black"))
        name = a.get("name", "?")
        working = a.get("working_on", "")
        working_str = f"\n  [dim]{working[:38]}[/]" if working else ""
        return f"[{color}]{glyph}[/] {name}{working_str}"


class MessageWidget(Static):
    """A single chat message."""

    DEFAULT_CSS = """
    MessageWidget {
        padding: 0 1;
        margin-bottom: 1;
    }
    MessageWidget.agent-msg {
        border-left: tall $primary;
    }
    MessageWidget.human-msg {
        border-left: tall $success;
    }
    """

    def __init__(self, msg: dict) -> None:
        super().__init__()
        sender = msg.get("sender", "?")
        ts = _fmt_ts(msg.get("timestamp", ""))
        content = msg.get("content", "")
        is_human = msg.get("actor_type") == "human" or msg.get("msg_type") == "chat"
        sender_color = "green" if is_human else "cyan"
        self.add_class("human-msg" if is_human else "agent-msg")
        self.update(
            f"[bold {sender_color}]{sender}[/]  [dim]{ts}[/]\n{content}"
        )


class LiveStreamBlock(Static):
    """Shows streaming output from an agent in real-time."""

    DEFAULT_CSS = """
    LiveStreamBlock {
        border: double yellow;
        padding: 0 1;
        margin: 1 0;
        max-height: 10;
        overflow-y: auto;
    }
    LiveStreamBlock.ended {
        border: solid bright_black;
    }
    """

    def __init__(self, agent_name: str, stream_id: str) -> None:
        super().__init__()
        self._agent = agent_name
        self._stream_id = stream_id
        self._lines: list[str] = []
        self._start = time.monotonic()
        self._ended = False
        self._render()

    def _render(self) -> None:
        elapsed = f"{time.monotonic() - self._start:.0f}s"
        status = "LIVE" if not self._ended else f"ended {elapsed}"
        header = f"[yellow]══ {self._agent} ● {status} ══[/]"
        body = "\n".join(self._lines[-8:]) if self._lines else "[dim]waiting…[/]"
        self.update(f"{header}\n{body}")

    def add_chunk(self, text: str) -> None:
        self._lines.extend(text.splitlines())
        self._render()

    def end_stream(self) -> None:
        self._ended = True
        self.add_class("ended")
        self._render()


class TaskItem(ListItem):
    """Single task row."""

    def __init__(self, task: dict) -> None:
        super().__init__()
        self._task = task
        self._render()

    def _render(self) -> None:
        t = self._task
        status = t.get("status", "todo")
        glyph = TASK_STATUS_GLYPH.get(status, "○")
        priority = t.get("priority", "medium")
        p_color = PRIORITY_COLOR.get(priority, "white")
        assignee = t.get("assignee") or ""
        assignee_str = f" [dim]{assignee[:12]}[/]" if assignee else ""
        title = t.get("title", "")[:42]
        done_style = "dim strike" if status == "done" else ""
        self.query_one(Static, default=None)  # avoid double-render on first call
        content = (
            f"[{p_color}]{glyph}[/] [{done_style}]{title}[/]{assignee_str}"
        )
        if self.children:
            self.query_one(Static).update(content)

    def compose(self) -> ComposeResult:
        t = self._task
        status = t.get("status", "todo")
        glyph = TASK_STATUS_GLYPH.get(status, "○")
        priority = t.get("priority", "medium")
        p_color = PRIORITY_COLOR.get(priority, "white")
        assignee = t.get("assignee") or ""
        assignee_str = f" [dim]{assignee[:12]}[/]" if assignee else ""
        title = t.get("title", "")[:42]
        done_style = "dim strike" if status == "done" else ""
        yield Static(
            f"[{p_color}]{glyph}[/] [{done_style}]{title}[/]{assignee_str}"
        )

    def update_task(self, task: dict) -> None:
        self._task = task
        self._render()


class HelpScreen(ModalScreen):
    """? overlay showing all keybindings."""

    DEFAULT_CSS = """
    HelpScreen {
        align: center middle;
    }
    HelpScreen > Vertical {
        width: 60;
        height: auto;
        max-height: 35;
        border: thick $primary;
        background: $surface;
        padding: 1 2;
    }
    """

    BINDINGS = [Binding("escape,question_mark", "dismiss", "Close")]

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label("[bold]AgentBridge Keybindings[/]\n", markup=True)
            yield Static(
                "[bold]Navigation[/]\n"
                "  Tab / Shift+Tab   cycle panels\n"
                "  j / k             move up/down in panel\n"
                "  G                 jump to bottom of chat\n"
                "  Alt+1…9           switch thread by index\n\n"
                "[bold]Compose[/]\n"
                "  Enter             send message\n"
                "  Shift+Enter       newline\n"
                "  @                 @mention autocomplete\n"
                "  /task <title>     create a task\n"
                "  /thread <name>    switch thread\n"
                "  /help             show this screen\n\n"
                "[bold]General[/]\n"
                "  ?                 this help screen\n"
                "  Esc               dismiss overlay\n"
                "  q / Ctrl+C        quit"
            )


class MentionOverlay(ModalScreen):
    """@mention autocomplete overlay above compose."""

    DEFAULT_CSS = """
    MentionOverlay {
        align: center bottom;
    }
    MentionOverlay > Vertical {
        width: 40;
        height: auto;
        max-height: 12;
        border: solid $primary;
        background: $surface;
        offset-y: -3;
    }
    """

    def __init__(self, agents: list[str], prefix: str) -> None:
        super().__init__()
        self._agents = [a for a in agents if a.lower().startswith(prefix.lower())] or agents
        self._chosen: str | None = None

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label("[dim]@mention — Enter to select, Esc to cancel[/]")
            yield ListView(
                *[ListItem(Static(name)) for name in self._agents[:10]],
                id="mention-list",
            )

    @on(ListView.Selected)
    def _selected(self, event: ListView.Selected) -> None:
        idx = self._agents.index(
            event.item.query_one(Static).renderable  # type: ignore[arg-type]
        ) if event.item.query_one(Static).renderable in self._agents else 0
        self._chosen = self._agents[idx] if self._agents else None
        self.dismiss(self._chosen)


# ---------------------------------------------------------------------------
# Main App
# ---------------------------------------------------------------------------


class AgentBridgeApp(App):
    """AgentBridge Terminal UI."""

    TITLE = "AgentBridge"
    CSS = """
    Screen {
        background: #0d0d0d;
    }

    /* ── Layout ── */
    #main-row {
        height: 1fr;
    }
    #left-panel {
        width: 24;
        border-right: solid $primary-darken-2;
        padding: 0;
    }
    #chat-panel {
        width: 1fr;
        border-right: solid $primary-darken-2;
    }
    #right-panel {
        width: 30;
    }

    /* ── Section labels ── */
    .section-label {
        background: $primary-darken-3;
        color: $text-muted;
        padding: 0 1;
        height: 1;
        text-style: bold;
    }

    /* ── Agent list ── */
    #agents-list, #humans-list, #tasks-list, #activity-list {
        background: transparent;
        border: none;
        padding: 0;
    }
    #agents-list > ListItem, #humans-list > ListItem {
        background: transparent;
        padding: 0 1;
        height: auto;
    }
    #agents-list > ListItem.--highlight, #humans-list > ListItem.--highlight {
        background: $primary-darken-2;
    }

    /* ── Chat messages ── */
    #chat-messages {
        height: 1fr;
        padding: 0 1;
    }
    #thread-label {
        background: $primary-darken-2;
        height: 1;
        padding: 0 1;
        color: $text-muted;
    }

    /* ── Compose ── */
    #compose-row {
        height: 3;
        border-top: solid $primary-darken-2;
        padding: 0 1;
    }
    #compose-input {
        background: $surface;
        border: solid $primary-darken-1;
        height: 3;
        width: 1fr;
    }

    /* ── Tasks ── */
    #tasks-list > ListItem {
        background: transparent;
        padding: 0 1;
        height: 1;
    }
    #tasks-list > ListItem.--highlight {
        background: $primary-darken-2;
    }
    #tasks-divider {
        height: 1;
        background: $primary-darken-3;
        color: $text-muted;
        padding: 0 1;
    }
    #activity-list {
        height: 1fr;
        overflow-y: auto;
    }
    #activity-list > ListItem {
        background: transparent;
        padding: 0 1;
        height: 1;
        color: $text-muted;
    }
    """

    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("question_mark", "help", "Help", key_display="?"),
        Binding("tab", "focus_next", "Next panel", show=False),
        Binding("shift+tab", "focus_previous", "Prev panel", show=False),
        Binding("g,G", "scroll_bottom", "Bottom", show=False),
    ]

    # Reactive state
    _sse_connected: reactive[bool] = reactive(False)
    _thread: reactive[str] = reactive("general")
    _agents: dict[str, dict] = {}
    _threads: list[str] = []
    _streams: dict[str, LiveStreamBlock] = {}  # correlation_id → widget
    _task_items: dict[str, TaskItem] = {}       # task_id → widget
    _pending_approvals: int = 0

    def __init__(self, server_url: str, token: str, agent_name: str = "") -> None:
        super().__init__()
        self._server_url = server_url.rstrip("/")
        self._token = token
        self._agent_name = agent_name

    # ── HTTP helpers ──────────────────────────────────────────────────────

    def _get(self, path: str, params: dict | None = None) -> Any:
        r = requests.get(
            f"{self._server_url}{path}",
            params=params,
            headers=_headers(self._token),
            timeout=8,
        )
        r.raise_for_status()
        return r.json()

    def _post(self, path: str, body: dict | None = None) -> Any:
        r = requests.post(
            f"{self._server_url}{path}",
            json=body,
            headers={**_headers(self._token), "Content-Type": "application/json"},
            timeout=8,
        )
        r.raise_for_status()
        return r.json()

    # ── Compose UI ───────────────────────────────────────────────────────

    def compose(self) -> ComposeResult:
        yield StatusBar(id="statusbar")
        with Horizontal(id="main-row"):
            # Left: agents + humans
            with Vertical(id="left-panel"):
                yield Label("AGENTS", classes="section-label")
                yield ListView(id="agents-list")
                yield Label("HUMANS", classes="section-label")
                yield ListView(id="humans-list")

            # Center: chat
            with Vertical(id="chat-panel"):
                yield Label(f"  #{self._thread}", id="thread-label")
                yield ScrollableContainer(id="chat-messages")

            # Right: tasks + activity
            with Vertical(id="right-panel"):
                yield Label("TASKS", classes="section-label")
                yield ListView(id="tasks-list")
                yield Label("─ ACTIVITY", id="tasks-divider")
                yield ListView(id="activity-list")

        # Bottom compose bar
        with Horizontal(id="compose-row"):
            yield Input(
                placeholder=f"  Message #{self._thread} — @ mention  / command  ? help",
                id="compose-input",
            )

    # ── Startup ──────────────────────────────────────────────────────────

    def on_mount(self) -> None:
        self._start_sse_worker()
        self._load_initial_data()

    @work(thread=True)
    def _start_sse_worker(self) -> None:
        """Background thread: subscribe to SSE and dispatch events to UI."""
        backoff = 1.0
        while True:
            try:
                url = f"{self._server_url}/events"
                with requests.get(
                    url,
                    headers=_headers(self._token),
                    stream=True,
                    timeout=None,
                ) as resp:
                    resp.raise_for_status()
                    backoff = 1.0
                    self.call_from_thread(self._set_sse_connected, True)
                    event_name: str | None = None
                    for raw in resp.iter_lines(decode_unicode=True):
                        if not raw:
                            event_name = None
                            continue
                        if raw.startswith("event:"):
                            event_name = raw[len("event:"):].strip()
                        elif raw.startswith("data:") and event_name:
                            try:
                                payload = json.loads(raw[len("data:"):].strip())
                                self.call_from_thread(
                                    self._handle_sse_event, event_name, payload
                                )
                            except json.JSONDecodeError:
                                pass
            except Exception:
                self.call_from_thread(self._set_sse_connected, False)
                time.sleep(backoff)
                backoff = min(backoff * 2, 60.0)

    @work(thread=True)
    def _load_initial_data(self) -> None:
        """Load agents, messages, tasks, threads on startup."""
        try:
            agents = self._get("/agents")
            for a in agents if isinstance(agents, list) else []:
                self.call_from_thread(self._upsert_agent, a)
        except Exception:
            pass

        try:
            messages = self._get("/messages", {"thread": self._thread, "limit": 50})
            for m in reversed(messages) if isinstance(messages, list) else []:
                self.call_from_thread(self._add_message, m)
        except Exception:
            pass

        try:
            tasks = self._get("/tasks", {"limit": 50})
            for t in tasks if isinstance(tasks, list) else []:
                self.call_from_thread(self._upsert_task, t)
        except Exception:
            pass

        try:
            threads = self._get("/threads")
            if isinstance(threads, list):
                self._threads = [t.get("name", "") for t in threads]
        except Exception:
            pass

    # ── SSE event dispatch ────────────────────────────────────────────────

    def _set_sse_connected(self, connected: bool) -> None:
        self._sse_connected = connected
        self._refresh_statusbar()

    def _handle_sse_event(self, event_type: str, payload: dict) -> None:
        match event_type:
            case "agent_joined" | "agent_status":
                self._upsert_agent(payload)
            case "agent_kicked":
                self._remove_agent(payload.get("name", ""))
            case "message":
                thread = payload.get("thread", "general")
                if thread == self._thread:
                    self._add_message(payload)
            case "task_created" | "task_updated" | "task_completed":
                self._upsert_task(payload)
            case "task_deleted":
                self._remove_task(payload.get("id", ""))
            case "stream_start":
                self._stream_start(payload)
            case "stream_chunk":
                self._stream_chunk(payload)
            case "stream_end":
                self._stream_end(payload)

    # ── Agent panel ──────────────────────────────────────────────────────

    def _upsert_agent(self, agent: dict) -> None:
        name = agent.get("name", "")
        agent_type = agent.get("agent_type", "bot")
        self._agents[name] = agent
        list_id = "#humans-list" if agent_type == "human" else "#agents-list"
        lv = self.query_one(list_id, ListView)
        # Try to find existing item
        for item in lv.children:
            if isinstance(item, AgentItem) and item._agent.get("name") == name:
                item.refresh_agent(agent)
                self._refresh_statusbar()
                return
        lv.append(AgentItem(agent))
        self._refresh_statusbar()

    def _remove_agent(self, name: str) -> None:
        self._agents.pop(name, None)
        for list_id in ("#agents-list", "#humans-list"):
            lv = self.query_one(list_id, ListView)
            for item in list(lv.children):
                if isinstance(item, AgentItem) and item._agent.get("name") == name:
                    item.remove()
        self._refresh_statusbar()

    # ── Chat panel ───────────────────────────────────────────────────────

    def _add_message(self, msg: dict) -> None:
        container = self.query_one("#chat-messages", ScrollableContainer)
        container.mount(MessageWidget(msg))
        container.scroll_end(animate=False)

    def _stream_start(self, payload: dict) -> None:
        stream_id = payload.get("correlation_id") or payload.get("id", "")
        agent = payload.get("sender") or payload.get("actor_id", "")
        block = LiveStreamBlock(agent, stream_id)
        self._streams[stream_id] = block
        container = self.query_one("#chat-messages", ScrollableContainer)
        container.mount(block)
        container.scroll_end(animate=False)

    def _stream_chunk(self, payload: dict) -> None:
        stream_id = payload.get("correlation_id") or payload.get("id", "")
        block = self._streams.get(stream_id)
        if block:
            block.add_chunk(payload.get("content", ""))

    def _stream_end(self, payload: dict) -> None:
        stream_id = payload.get("correlation_id") or payload.get("id", "")
        block = self._streams.pop(stream_id, None)
        if block:
            block.end_stream()

    # ── Task panel ───────────────────────────────────────────────────────

    def _upsert_task(self, task: dict) -> None:
        task_id = task.get("id", "")
        lv = self.query_one("#tasks-list", ListView)
        if task_id in self._task_items:
            self._task_items[task_id].update_task(task)
        else:
            item = TaskItem(task)
            self._task_items[task_id] = item
            lv.append(item)

        # Activity log entry
        self._add_activity(task)

    def _remove_task(self, task_id: str) -> None:
        item = self._task_items.pop(task_id, None)
        if item:
            item.remove()

    def _add_activity(self, task: dict) -> None:
        status = task.get("status", "")
        assignee = task.get("assignee") or ""
        title = task.get("title", "")[:28]
        ts = _fmt_ts(task.get("updated_at") or task.get("created_at") or "")
        action = {
            "in_progress": "claimed",
            "done":        "completed",
            "blocked":     "blocked",
            "todo":        "created",
        }.get(status, status)
        actor = assignee or "system"
        entry = ListItem(Static(f"[dim]{ts}[/] {actor} {action}: {title}"))
        lv = self.query_one("#activity-list", ListView)
        lv.append(entry)
        # Keep last 100 entries
        while len(lv.children) > 100:
            lv.children[0].remove()

    # ── Status bar ───────────────────────────────────────────────────────

    def _refresh_statusbar(self) -> None:
        bar = self.query_one("#statusbar", StatusBar)
        bar.update_status(
            thread=self._thread,
            connected=self._sse_connected,
            agents=len(self._agents),
            approvals=self._pending_approvals,
        )

    # ── Compose / send ───────────────────────────────────────────────────

    @on(Input.Submitted, "#compose-input")
    def _on_submit(self, event: Input.Submitted) -> None:
        text = event.value.strip()
        if not text:
            return
        event.input.clear()

        # Slash commands
        if text.startswith("/task "):
            title = text[6:].strip()
            if title:
                self._create_task(title)
            return
        if text.startswith("/thread "):
            name = text[8:].strip()
            if name:
                self._switch_thread(name)
            return
        if text in ("/help", "/?"):
            self.action_help()
            return

        # Regular message
        self._send_message(text)

    @on(Input.Changed, "#compose-input")
    def _on_input_change(self, event: Input.Changed) -> None:
        val = event.value
        # Trigger @mention autocomplete when user types @
        if "@" in val:
            prefix = val.split("@")[-1]
            agent_names = [
                a.get("name", "") for a in self._agents.values()
                if a.get("agent_type") != "human"
            ]
            if agent_names:
                self._show_mention_overlay(agent_names, prefix, event.input)

    def _show_mention_overlay(
        self, agents: list[str], prefix: str, input_widget: Input
    ) -> None:
        def _on_dismiss(name: str | None) -> None:
            if name:
                val = input_widget.value
                at_idx = val.rfind("@")
                input_widget.value = val[: at_idx + 1] + name + " "
                input_widget.cursor_position = len(input_widget.value)

        self.push_screen(MentionOverlay(agents, prefix), _on_dismiss)

    @work(thread=True)
    def _send_message(self, text: str) -> None:
        try:
            sender = self._agent_name or "human"
            self._post("/messages", {
                "sender": sender,
                "content": text,
                "thread": self._thread,
            })
        except Exception as exc:
            self.call_from_thread(
                self.notify, f"Send failed: {exc}", severity="error"
            )

    @work(thread=True)
    def _create_task(self, title: str) -> None:
        try:
            creator = self._agent_name or "human"
            self._post("/tasks", {
                "title": title,
                "created_by": creator,
                "thread": self._thread,
            })
            self.call_from_thread(self.notify, f"Task created: {title}")
        except Exception as exc:
            self.call_from_thread(
                self.notify, f"Task create failed: {exc}", severity="error"
            )

    def _switch_thread(self, name: str) -> None:
        self._thread = name
        self.query_one("#thread-label", Label).update(f"  #{name}")
        compose = self.query_one("#compose-input", Input)
        compose.placeholder = f"  Message #{name} — @ mention  / command  ? help"
        # Reload chat
        self.query_one("#chat-messages", ScrollableContainer).remove_children()
        self._load_thread_messages(name)
        self._refresh_statusbar()

    @work(thread=True)
    def _load_thread_messages(self, thread: str) -> None:
        try:
            messages = self._get("/messages", {"thread": thread, "limit": 50})
            for m in reversed(messages) if isinstance(messages, list) else []:
                self.call_from_thread(self._add_message, m)
        except Exception:
            pass

    # ── Keyboard actions ─────────────────────────────────────────────────

    def action_help(self) -> None:
        self.push_screen(HelpScreen())

    def action_scroll_bottom(self) -> None:
        container = self.query_one("#chat-messages", ScrollableContainer)
        container.scroll_end(animate=True)

    def on_key(self, event: Any) -> None:
        # Alt+1…9 to switch threads by index
        if event.key.startswith("alt+") and event.key[-1].isdigit():
            idx = int(event.key[-1]) - 1
            if 0 <= idx < len(self._threads):
                self._switch_thread(self._threads[idx])
                event.prevent_default()
            return

        # j/k navigation in focused panel
        focused = self.focused
        if event.key == "j" and isinstance(focused, ListView):
            focused.action_cursor_down()
            event.prevent_default()
        elif event.key == "k" and isinstance(focused, ListView):
            focused.action_cursor_up()
            event.prevent_default()


# ---------------------------------------------------------------------------
# Entry point (also callable directly)
# ---------------------------------------------------------------------------


def main() -> None:
    import sys
    url = os.environ.get("AGENTBRIDGE_URL", "http://localhost:7890")
    token = os.environ.get("AGENTBRIDGE_TOKEN", "")
    name = os.environ.get("AGENT_NAME", "")
    app = AgentBridgeApp(server_url=url, token=token, agent_name=name)
    app.run()


if __name__ == "__main__":
    main()
