import sqlite3
import json
from datetime import datetime, timezone, timedelta
from pathlib import Path

from .models import Agent, Message, Thread

DB_DIR = Path.home() / ".agentbridge"
DB_PATH = DB_DIR / "messages.db"

AGENT_TTL_HOURS = 1
MESSAGE_TTL_HOURS = 24


class MessageStore:
    def __init__(self, db_path: str | Path = DB_PATH):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._create_tables()
        self._prune()

    def _create_tables(self):
        self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS agents (
                name TEXT PRIMARY KEY,
                role TEXT DEFAULT '',
                connected_at TEXT NOT NULL,
                last_seen TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS messages (
                id TEXT PRIMARY KEY,
                sender TEXT NOT NULL,
                recipient TEXT,
                thread TEXT DEFAULT 'general',
                content TEXT NOT NULL,
                timestamp TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS threads (
                name TEXT PRIMARY KEY,
                created_by TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
        """)
        self._conn.commit()

    def _prune(self):
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=MESSAGE_TTL_HOURS)).isoformat()
        self._conn.execute("DELETE FROM messages WHERE timestamp < ?", (cutoff,))
        agent_cutoff = (datetime.now(timezone.utc) - timedelta(hours=AGENT_TTL_HOURS)).isoformat()
        self._conn.execute("DELETE FROM agents WHERE last_seen < ?", (agent_cutoff,))
        self._conn.commit()

    # --- Agents ---

    def register_agent(self, name: str, role: str = "") -> Agent:
        now = datetime.now(timezone.utc)
        agent = Agent(name=name, role=role, connected_at=now, last_seen=now)
        self._conn.execute(
            "INSERT OR REPLACE INTO agents (name, role, connected_at, last_seen) VALUES (?, ?, ?, ?)",
            (agent.name, agent.role, agent.connected_at.isoformat(), agent.last_seen.isoformat()),
        )
        self._conn.commit()
        return agent

    def touch_agent(self, name: str):
        now = datetime.now(timezone.utc).isoformat()
        self._conn.execute("UPDATE agents SET last_seen = ? WHERE name = ?", (now, name))
        self._conn.commit()

    def list_agents(self) -> list[Agent]:
        self._prune()
        rows = self._conn.execute("SELECT name, role, connected_at, last_seen FROM agents").fetchall()
        return [
            Agent(
                name=r[0],
                role=r[1],
                connected_at=datetime.fromisoformat(r[2]),
                last_seen=datetime.fromisoformat(r[3]),
            )
            for r in rows
        ]

    # --- Messages ---

    def add_message(self, sender: str, content: str, recipient: str | None = None, thread: str = "general") -> Message:
        msg = Message(sender=sender, content=content, recipient=recipient, thread=thread)
        self._conn.execute(
            "INSERT INTO messages (id, sender, recipient, thread, content, timestamp) VALUES (?, ?, ?, ?, ?, ?)",
            (msg.id, msg.sender, msg.recipient, msg.thread, msg.content, msg.timestamp.isoformat()),
        )
        self._conn.commit()
        self.touch_agent(sender)
        return msg

    def read_messages(
        self, thread: str | None = None, since: str | None = None, limit: int = 50, sender: str | None = None
    ) -> list[Message]:
        query = "SELECT id, sender, recipient, thread, content, timestamp FROM messages WHERE 1=1"
        params: list = []
        if thread:
            query += " AND thread = ?"
            params.append(thread)
        if sender:
            query += " AND sender = ?"
            params.append(sender)
        if since:
            query += " AND timestamp > ?"
            params.append(since)
        query += " ORDER BY timestamp DESC LIMIT ?"
        params.append(limit)
        rows = self._conn.execute(query, params).fetchall()
        return [
            Message(
                id=r[0],
                sender=r[1],
                recipient=r[2],
                thread=r[3],
                content=r[4],
                timestamp=datetime.fromisoformat(r[5]),
            )
            for r in reversed(rows)
        ]

    # --- Threads ---

    def create_thread(self, name: str, created_by: str) -> Thread:
        t = Thread(name=name, created_by=created_by)
        self._conn.execute(
            "INSERT OR IGNORE INTO threads (name, created_by, created_at) VALUES (?, ?, ?)",
            (t.name, t.created_by, t.created_at.isoformat()),
        )
        self._conn.commit()
        return t

    def list_threads(self) -> list[Thread]:
        rows = self._conn.execute("SELECT name, created_by, created_at FROM threads").fetchall()
        return [
            Thread(name=r[0], created_by=r[1], created_at=datetime.fromisoformat(r[2]))
            for r in rows
        ]

    def close(self):
        self._conn.close()
