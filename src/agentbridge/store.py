import sqlite3
import json
import threading
from datetime import datetime, timezone, timedelta
from pathlib import Path

from .models import Agent, Artifact, Message, Thread, ThreadSummary

DB_DIR = Path.home() / ".agentbridge"
DB_PATH = DB_DIR / "messages.db"

AGENT_TTL_HOURS = 4
MESSAGE_TTL_HOURS = 24
PRUNE_INTERVAL_SEC = 300  # 5 minutes


class MessageStore:
    def __init__(self, db_path: str | Path = DB_PATH):
        self._lock = threading.RLock()
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._create_tables()
        self._migrate()
        self._ensure_indexes()
        self._prune()
        self._start_prune_timer()

    def _start_prune_timer(self):
        self._prune_timer = threading.Timer(PRUNE_INTERVAL_SEC, self._scheduled_prune)
        self._prune_timer.daemon = True
        self._prune_timer.start()

    def _scheduled_prune(self):
        self._prune()
        self._start_prune_timer()

    def _create_tables(self):
        with self._lock:
            self._conn.executescript("""
                CREATE TABLE IF NOT EXISTS agents (
                    name TEXT PRIMARY KEY,
                    role TEXT DEFAULT '',
                    status TEXT DEFAULT 'online',
                    working_on TEXT DEFAULT '',
                    capabilities TEXT DEFAULT '[]',
                    connected_at TEXT NOT NULL,
                    last_seen TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS messages (
                    id TEXT PRIMARY KEY,
                    actor_id TEXT,
                    actor_type TEXT DEFAULT 'agent',
                    target_id TEXT,
                    target_type TEXT,
                    event_type TEXT DEFAULT 'note.text',
                    metadata TEXT DEFAULT '{}',
                    labels TEXT DEFAULT '[]',
                    sender TEXT NOT NULL,
                    recipient TEXT,
                    thread TEXT DEFAULT 'general',
                    content TEXT NOT NULL,
                    msg_type TEXT DEFAULT 'chat',
                    mentions TEXT DEFAULT '[]',
                    artifacts TEXT DEFAULT '[]',
                    correlation_id TEXT,
                    timestamp TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS threads (
                    name TEXT PRIMARY KEY,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
            """)
            self._conn.commit()

    def _ensure_indexes(self):
        with self._lock:
            self._conn.executescript("""
                CREATE INDEX IF NOT EXISTS idx_agents_last_seen
                    ON agents(last_seen);
                CREATE INDEX IF NOT EXISTS idx_messages_timestamp
                    ON messages(timestamp);
                CREATE INDEX IF NOT EXISTS idx_messages_event_type_timestamp
                    ON messages(event_type, timestamp);
                CREATE INDEX IF NOT EXISTS idx_messages_actor_timestamp
                    ON messages(actor_id, timestamp);
                CREATE INDEX IF NOT EXISTS idx_messages_thread_timestamp
                    ON messages(thread, timestamp);
                CREATE INDEX IF NOT EXISTS idx_messages_recipient_timestamp
                    ON messages(recipient, timestamp);
                CREATE INDEX IF NOT EXISTS idx_messages_correlation_id
                    ON messages(correlation_id);
            """)
            self._conn.commit()

    def _migrate(self):
        """Add columns that may not exist in older databases."""
        with self._lock:
            cursor = self._conn.execute("PRAGMA table_info(agents)")
            agent_cols = {row[1] for row in cursor.fetchall()}
            for col, ddl in [
                ("status", "ALTER TABLE agents ADD COLUMN status TEXT DEFAULT 'online'"),
                ("working_on", "ALTER TABLE agents ADD COLUMN working_on TEXT DEFAULT ''"),
                ("capabilities", "ALTER TABLE agents ADD COLUMN capabilities TEXT DEFAULT '[]'"),
            ]:
                if col not in agent_cols:
                    self._conn.execute(ddl)

            cursor = self._conn.execute("PRAGMA table_info(messages)")
            msg_cols = {row[1] for row in cursor.fetchall()}
            for col, ddl in [
                ("msg_type", "ALTER TABLE messages ADD COLUMN msg_type TEXT DEFAULT 'chat'"),
                ("actor_id", "ALTER TABLE messages ADD COLUMN actor_id TEXT"),
                ("actor_type", "ALTER TABLE messages ADD COLUMN actor_type TEXT DEFAULT 'agent'"),
                ("target_id", "ALTER TABLE messages ADD COLUMN target_id TEXT"),
                ("target_type", "ALTER TABLE messages ADD COLUMN target_type TEXT"),
                ("event_type", "ALTER TABLE messages ADD COLUMN event_type TEXT DEFAULT 'note.text'"),
                ("metadata", "ALTER TABLE messages ADD COLUMN metadata TEXT DEFAULT '{}'"),
                ("labels", "ALTER TABLE messages ADD COLUMN labels TEXT DEFAULT '[]'"),
                ("mentions", "ALTER TABLE messages ADD COLUMN mentions TEXT DEFAULT '[]'"),
                ("artifacts", "ALTER TABLE messages ADD COLUMN artifacts TEXT DEFAULT '[]'"),
                ("correlation_id", "ALTER TABLE messages ADD COLUMN correlation_id TEXT"),
            ]:
                if col not in msg_cols:
                    self._conn.execute(ddl)
            self._conn.commit()

    def _prune(self):
        with self._lock:
            cutoff = (datetime.now(timezone.utc) - timedelta(hours=MESSAGE_TTL_HOURS)).isoformat()
            self._conn.execute("DELETE FROM messages WHERE timestamp < ?", (cutoff,))
            agent_cutoff = (datetime.now(timezone.utc) - timedelta(hours=AGENT_TTL_HOURS)).isoformat()
            self._conn.execute("DELETE FROM agents WHERE last_seen < ?", (agent_cutoff,))
            self._conn.commit()

    # --- Agents ---

    def register_agent(self, name: str, role: str = "", capabilities: list[str] | None = None) -> Agent:
        now = datetime.now(timezone.utc)
        caps = capabilities or []
        agent = Agent(name=name, role=role, capabilities=caps, connected_at=now, last_seen=now)
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO agents (name, role, status, working_on, capabilities, connected_at, last_seen) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (agent.name, agent.role, agent.status, agent.working_on, json.dumps(agent.capabilities), agent.connected_at.isoformat(), agent.last_seen.isoformat()),
            )
            self._conn.commit()
        return agent

    def touch_agent(self, name: str):
        now = datetime.now(timezone.utc).isoformat()
        with self._lock:
            self._conn.execute("UPDATE agents SET last_seen = ? WHERE name = ?", (now, name))
            self._conn.commit()

    def heartbeat(self, name: str, status: str = "online", working_on: str = "") -> Agent | None:
        now = datetime.now(timezone.utc).isoformat()
        with self._lock:
            self._conn.execute(
                "UPDATE agents SET last_seen = ?, status = ?, working_on = ? WHERE name = ?",
                (now, status, working_on, name),
            )
            self._conn.commit()
            row = self._conn.execute(
                "SELECT name, role, status, working_on, capabilities, connected_at, last_seen FROM agents WHERE name = ?", (name,)
            ).fetchone()
        if not row:
            return None
        return Agent(
            name=row[0], role=row[1], status=row[2], working_on=row[3],
            capabilities=json.loads(row[4]) if row[4] else [],
            connected_at=datetime.fromisoformat(row[5]), last_seen=datetime.fromisoformat(row[6]),
        )

    def list_agents(self) -> list[Agent]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT name, role, status, working_on, capabilities, connected_at, last_seen FROM agents ORDER BY last_seen DESC"
            ).fetchall()
        return [
            Agent(
                name=r[0], role=r[1], status=r[2] or "online", working_on=r[3] or "",
                capabilities=json.loads(r[4]) if r[4] else [],
                connected_at=datetime.fromisoformat(r[5]), last_seen=datetime.fromisoformat(r[6]),
            )
            for r in rows
        ]

    def find_agents_by_capability(self, capability: str) -> list[Agent]:
        """Return agents that advertise a given capability."""
        return [a for a in self.list_agents() if capability in a.capabilities]

    # --- Messages ---

    def add_message(
        self, sender: str, content: str, recipient: str | None = None,
        thread: str = "general", msg_type: str = "chat",
        artifacts: list[dict] | None = None,
        actor_id: str | None = None, actor_type: str = "agent",
        target_id: str | None = None, target_type: str | None = None,
        event_type: str = "note.text", metadata: dict | None = None,
        labels: list[str] | None = None,
        correlation_id: str | None = None,
    ) -> Message:
        artifact_objs = [Artifact(**a) for a in (artifacts or [])]
        msg = Message(
            sender=sender, content=content, recipient=recipient,
            thread=thread, msg_type=msg_type, artifacts=artifact_objs,
            actor_id=actor_id, actor_type=actor_type,
            target_id=target_id, target_type=target_type,
            event_type=event_type, metadata=metadata or {},
            labels=labels or [],
            correlation_id=correlation_id,
        )
        with self._lock:
            self._conn.execute(
                "INSERT INTO messages (id, actor_id, actor_type, target_id, target_type, event_type, metadata, labels, sender, recipient, thread, content, msg_type, mentions, artifacts, correlation_id, timestamp) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    msg.id, msg.actor_id, msg.actor_type, msg.target_id, msg.target_type,
                    msg.event_type, json.dumps(msg.metadata), json.dumps(msg.labels),
                    msg.sender, msg.recipient, msg.thread, msg.content,
                    msg.msg_type, json.dumps(msg.mentions),
                    json.dumps([a.model_dump() for a in msg.artifacts]),
                    msg.correlation_id,
                    msg.timestamp.isoformat(),
                ),
            )
            self._conn.commit()
        self.touch_agent(sender)
        return msg

    def read_messages(
        self, thread: str | None = None, since: str | None = None,
        limit: int = 50, sender: str | None = None, as_agent: str | None = None,
        before: str | None = None,
        actor_id: str | None = None, event_type: str | None = None,
        target_id: str | None = None,
        correlation_id: str | None = None,
    ) -> list[Message]:
        def _normalize_ts(value: str) -> str:
            try:
                return datetime.fromisoformat(value.replace("Z", "+00:00")).isoformat()
            except ValueError:
                return value

        query = "SELECT id, actor_id, actor_type, target_id, target_type, event_type, metadata, labels, sender, recipient, thread, content, msg_type, mentions, artifacts, correlation_id, timestamp FROM messages WHERE 1=1"
        params: list = []

        if thread:
            query += " AND thread = ?"
            params.append(thread)
        if sender:
            query += " AND sender = ?"
            params.append(sender)
        if as_agent:
            query += " AND (recipient = ? OR recipient IS NULL)"
            params.append(as_agent)
        if actor_id:
            query += " AND actor_id = ?"
            params.append(actor_id)
        if target_id:
            query += " AND target_id = ?"
            params.append(target_id)
        if event_type:
            query += " AND event_type = ?"
            params.append(event_type)
        if since:
            query += " AND timestamp > ?"
            params.append(_normalize_ts(since))
        if before:
            query += " AND timestamp < ?"
            params.append(_normalize_ts(before))
        if correlation_id:
            query += " AND correlation_id = ?"
            params.append(correlation_id)

        query += " ORDER BY timestamp DESC LIMIT ?"
        params.append(limit)
        with self._lock:
            rows = self._conn.execute(query, params).fetchall()
        return [
            Message(
                id=r[0], actor_id=r[1], actor_type=r[2] or "agent",
                target_id=r[3], target_type=r[4], event_type=r[5] or "note.text",
                metadata=json.loads(r[6]) if r[6] else {},
                labels=json.loads(r[7]) if r[7] else [],
                sender=r[8], recipient=r[9], thread=r[10], content=r[11],
                msg_type=r[12] or "chat",
                mentions=json.loads(r[13]) if r[13] else [],
                artifacts=[Artifact(**a) for a in json.loads(r[14])] if r[14] else [],
                correlation_id=r[15],
                timestamp=datetime.fromisoformat(r[16]),
            )
            for r in reversed(rows)
        ]

    def backup_to(self, output_path: str | Path) -> Path:
        output = Path(output_path).expanduser()
        output.parent.mkdir(parents=True, exist_ok=True)
        with self._lock:
            dest = sqlite3.connect(str(output))
            try:
                self._conn.backup(dest)
            finally:
                dest.close()
        return output

    # --- Threads ---

    def create_thread(self, name: str, created_by: str) -> Thread:
        t = Thread(name=name, created_by=created_by)
        with self._lock:
            self._conn.execute(
                "INSERT OR IGNORE INTO threads (name, created_by, created_at) VALUES (?, ?, ?)",
                (t.name, t.created_by, t.created_at.isoformat()),
            )
            self._conn.commit()
            row = self._conn.execute(
                "SELECT name, created_by, created_at FROM threads WHERE name = ?",
                (name,),
            ).fetchone()
        if row:
            return Thread(name=row[0], created_by=row[1], created_at=datetime.fromisoformat(row[2]))
        return t

    def list_threads(self) -> list[Thread]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT name, created_by, created_at FROM threads ORDER BY created_at ASC"
            ).fetchall()
        return [
            Thread(name=r[0], created_by=r[1], created_at=datetime.fromisoformat(r[2]))
            for r in rows
        ]

    def thread_summary(self, name: str) -> ThreadSummary:
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*), MIN(timestamp), MAX(timestamp) FROM messages WHERE thread = ?", (name,)
            ).fetchone()
        count = row[0] or 0
        first_at = datetime.fromisoformat(row[1]) if row[1] else None
        last_at = datetime.fromisoformat(row[2]) if row[2] else None

        with self._lock:
            participants_rows = self._conn.execute(
                "SELECT DISTINCT sender FROM messages WHERE thread = ?", (name,)
            ).fetchall()
        participants = sorted([r[0] for r in participants_rows])

        preview = ""
        if count > 0:
            with self._lock:
                last_row = self._conn.execute(
                    "SELECT content FROM messages WHERE thread = ? ORDER BY timestamp DESC LIMIT 1", (name,)
                ).fetchone()
            if last_row:
                preview = last_row[0][:120]

        return ThreadSummary(
            name=name, message_count=count, participants=participants,
            first_message_at=first_at, last_message_at=last_at,
            last_message_preview=preview,
        )

    def close(self):
        if hasattr(self, "_prune_timer"):
            self._prune_timer.cancel()
        with self._lock:
            self._conn.close()
