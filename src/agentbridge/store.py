import asyncio
import sqlite3
import json
import os
import threading
import time
import hashlib
import secrets
import binascii
import uuid
from datetime import datetime, timezone, timedelta
from pathlib import Path

from .models import Agent, Artifact, Message, Thread, ThreadSummary

DB_DIR = Path.home() / ".agentbridge"
DB_PATH = DB_DIR / "messages.db"

AGENT_TTL_HOURS = 4
MESSAGE_TTL_HOURS = 24
PRUNE_INTERVAL_SEC = 300  # 5 minutes
SESSION_TTL_DAYS = 30


def _hash_password(password: str) -> str:
    salt = os.urandom(32)
    key = hashlib.pbkdf2_hmac('sha256', password.encode('utf-8'), salt, 200_000)
    return binascii.hexlify(salt).decode() + ':' + binascii.hexlify(key).decode()


def _verify_password(password: str, stored_hash: str) -> bool:
    try:
        salt_hex, key_hex = stored_hash.split(':', 1)
        salt = binascii.unhexlify(salt_hex)
        key = hashlib.pbkdf2_hmac('sha256', password.encode('utf-8'), salt, 200_000)
        return secrets.compare_digest(binascii.hexlify(key).decode(), key_hex)
    except Exception:
        return False


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
               CREATE TABLE IF NOT EXISTS delivery_cursors (
                   agent_name TEXT NOT NULL,
                   thread TEXT NOT NULL DEFAULT 'general',
                   last_message_id TEXT,
                   last_timestamp TEXT,
                   updated_at TEXT NOT NULL,
                   PRIMARY KEY (agent_name, thread)
               );
                CREATE TABLE IF NOT EXISTS users (
                    id TEXT PRIMARY KEY,
                    username TEXT UNIQUE NOT NULL,
                    password_hash TEXT NOT NULL,
                    display_name TEXT DEFAULT '',
                    role TEXT DEFAULT 'member',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS user_sessions (
                    token TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    last_used TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at TEXT NOT NULL
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
                CREATE INDEX IF NOT EXISTS idx_delivery_cursors_agent_thread
                    ON delivery_cursors(agent_name, thread);
                CREATE INDEX IF NOT EXISTS idx_agent_requests_to_status
                    ON agent_requests(to_agent, status);
                CREATE INDEX IF NOT EXISTS idx_agent_requests_correlation
                    ON agent_requests(correlation_id);
                CREATE INDEX IF NOT EXISTS idx_agent_requests_expires
                    ON agent_requests(expires_at, status);
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
                ("agent_type", "ALTER TABLE agents ADD COLUMN agent_type TEXT DEFAULT 'bot'"),
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
                ("claimed_by", "ALTER TABLE messages ADD COLUMN claimed_by TEXT"),
            ]:
                if col not in msg_cols:
                    self._conn.execute(ddl)
            # Create agent_requests table if missing (migration-safe)
            self._conn.execute("""
                CREATE TABLE IF NOT EXISTS agent_requests (
                    id TEXT PRIMARY KEY,
                    from_agent TEXT NOT NULL,
                    to_agent TEXT NOT NULL,
                    content TEXT NOT NULL,
                    status TEXT DEFAULT 'pending',
                    correlation_id TEXT NOT NULL,
                    thread TEXT DEFAULT 'general',
                    created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    response TEXT,
                    responded_at TEXT
                )
            """)
            self._conn.commit()

    def _prune(self):
        with self._lock:
            cutoff = (datetime.now(timezone.utc) - timedelta(hours=MESSAGE_TTL_HOURS)).isoformat()
            self._conn.execute("DELETE FROM messages WHERE timestamp < ?", (cutoff,))
            agent_cutoff = (datetime.now(timezone.utc) - timedelta(hours=AGENT_TTL_HOURS)).isoformat()
            self._conn.execute("DELETE FROM agents WHERE last_seen < ?", (agent_cutoff,))
            self._conn.execute("DELETE FROM user_sessions WHERE expires_at < ?", (datetime.now(timezone.utc).isoformat(),))
            # Expire pending requests past their deadline
            now_iso = datetime.now(timezone.utc).isoformat()
            self._conn.execute(
                "UPDATE agent_requests SET status = 'timeout' WHERE status = 'pending' AND expires_at < ?",
                (now_iso,)
            )
            self._conn.commit()
        return None

    async def _run_in_thread(self, func, *args, **kwargs):
        return await asyncio.to_thread(func, *args, **kwargs)

    async def register_agent_async(self, name: str, role: str = "", capabilities: list[str] | None = None, agent_type: str = "bot") -> Agent:
        return await self._run_in_thread(self.register_agent, name, role, capabilities, agent_type)

    async def list_agents_async(self) -> list[Agent]:
        return await self._run_in_thread(self.list_agents)

    async def heartbeat_async(self, name: str, status: str = "online", working_on: str = "") -> Agent | None:
        return await self._run_in_thread(self.heartbeat, name, status, working_on)

    async def add_message_async(
        self, sender: str, content: str,
        recipient: str | None = None,
        thread: str = "general", msg_type: str = "chat",
        artifacts: list[dict] | None = None,
        actor_id: str | None = None, actor_type: str = "agent",
        target_id: str | None = None, target_type: str | None = None,
        event_type: str = "note.text", metadata: dict | None = None,
        labels: list[str] | None = None,
        correlation_id: str | None = None,
    ) -> Message:
        return await self._run_in_thread(
            self.add_message,
            sender, content, recipient, thread, msg_type, artifacts, actor_id, actor_type,
            target_id, target_type, event_type, metadata, labels, correlation_id,
        )

    async def read_messages_async(
        self, thread: str | None = None, since: str | None = None,
        limit: int = 50, sender: str | None = None, as_agent: str | None = None,
        before: str | None = None,
        actor_id: str | None = None, event_type: str | None = None,
        target_id: str | None = None,
        correlation_id: str | None = None,
        since_id: str | None = None,
    ) -> list[Message]:
        return await self._run_in_thread(
            self.read_messages,
            thread, since, limit, sender, as_agent, before, actor_id, event_type, target_id, correlation_id, since_id,
        )

    async def message_timestamp_async(self, message_id: str) -> str | None:
        return await self._run_in_thread(self.message_timestamp, message_id)

    async def claim_message_async(self, message_id: str, agent_name: str) -> bool:
        return await self._run_in_thread(self.claim_message, message_id, agent_name)

    async def set_delivery_cursor_async(
        self,
        agent_name: str,
        thread: str,
        last_message_id: str,
        last_timestamp: str,
    ):
        await self._run_in_thread(
            self.set_delivery_cursor,
            agent_name, thread, last_message_id, last_timestamp,
        )

    async def get_delivery_cursor_async(self, agent_name: str, thread: str = "general") -> dict | None:
        return await self._run_in_thread(self.get_delivery_cursor, agent_name, thread)

    async def list_delivery_cursors_async(self, agent_name: str | None = None) -> list[dict]:
        return await self._run_in_thread(self.list_delivery_cursors, agent_name)

    async def create_thread_async(self, name: str, created_by: str) -> Thread:
        return await self._run_in_thread(self.create_thread, name, created_by)

    async def list_threads_async(self) -> list[Thread]:
        return await self._run_in_thread(self.list_threads)

    async def thread_summary_async(self, name: str) -> ThreadSummary:
        return await self._run_in_thread(self.thread_summary, name)

    def remove_agent(self, name: str):
        with self._lock:
            self._conn.execute("DELETE FROM agents WHERE name = ?", (name,))
            self._conn.commit()

    async def remove_agent_async(self, name: str):
        await self._run_in_thread(self.remove_agent, name)

    # --- Agents ---

    def get_agent(self, name: str) -> Agent | None:
        """Look up a single agent by name. Returns None if not found."""
        with self._lock:
            row = self._conn.execute(
                "SELECT name, role, status, working_on, capabilities, connected_at, last_seen FROM agents WHERE name = ?",
                (name,)
            ).fetchone()
        if not row:
            return None
        return Agent(
            name=row[0], role=row[1] or "", status=row[2] or "online",
            working_on=row[3] or "",
            capabilities=json.loads(row[4]) if row[4] else [],
            connected_at=datetime.fromisoformat(row[5]),
            last_seen=datetime.fromisoformat(row[6]),
        )

    async def get_agent_async(self, name: str) -> Agent | None:
        return await self._run_in_thread(self.get_agent, name)

    def register_agent(self, name: str, role: str = "", capabilities: list[str] | None = None, agent_type: str = "bot") -> Agent:
        now = datetime.now(timezone.utc)
        caps = capabilities or []
        agent = Agent(name=name, role=role, capabilities=caps, agent_type=agent_type, connected_at=now, last_seen=now)
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO agents (name, role, status, working_on, capabilities, agent_type, connected_at, last_seen) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (agent.name, agent.role, agent.status, agent.working_on, json.dumps(agent.capabilities), agent.agent_type, agent.connected_at.isoformat(), agent.last_seen.isoformat()),
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
                "SELECT name, role, status, working_on, capabilities, agent_type, connected_at, last_seen FROM agents WHERE name = ?", (name,)
            ).fetchone()
        if not row:
            return None
        return Agent(
            name=row[0], role=row[1], status=row[2], working_on=row[3],
            capabilities=json.loads(row[4]) if row[4] else [],
            agent_type=row[5] or "bot",
            connected_at=datetime.fromisoformat(row[6]), last_seen=datetime.fromisoformat(row[7]),
        )

    def list_agents(self) -> list[Agent]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT name, role, status, working_on, capabilities, agent_type, connected_at, last_seen FROM agents ORDER BY last_seen DESC"
            ).fetchall()
        return [
            Agent(
                name=r[0], role=r[1], status=r[2] or "online", working_on=r[3] or "",
                capabilities=json.loads(r[4]) if r[4] else [],
                agent_type=r[5] or "bot",
                connected_at=datetime.fromisoformat(r[6]), last_seen=datetime.fromisoformat(r[7]),
            )
            for r in rows
        ]

    def find_agents_by_capability(self, capability: str) -> list[Agent]:
        """Return agents that advertise a given capability."""
        return [a for a in self.list_agents() if capability in a.capabilities]

    # --- Users ---

    def has_any_users(self) -> bool:
        with self._lock:
            row = self._conn.execute("SELECT COUNT(*) FROM users").fetchone()
        return (row[0] if row else 0) > 0

    def bootstrap_admin(self, username: str, password: str) -> dict | None:
        """Create the first admin account if no users exist. Returns user dict or None if users already exist."""
        if self.has_any_users():
            return None
        return self.create_user(username, password, display_name="Admin", role="admin")

    def create_user(self, username: str, password: str, display_name: str = "", role: str = "member") -> dict:
        now = datetime.now(timezone.utc).isoformat()
        user_id = str(uuid.uuid4())
        password_hash = _hash_password(password)
        with self._lock:
            self._conn.execute(
                "INSERT INTO users (id, username, password_hash, display_name, role, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (user_id, username, password_hash, display_name or "", role, now, now),
            )
            self._conn.commit()
        return {"id": user_id, "username": username, "display_name": display_name or "", "role": role, "created_at": now, "updated_at": now}

    def get_user_by_id(self, user_id: str) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT id, username, display_name, role, created_at, updated_at FROM users WHERE id = ?", (user_id,)
            ).fetchone()
        if not row:
            return None
        return {"id": row[0], "username": row[1], "display_name": row[2], "role": row[3], "created_at": row[4], "updated_at": row[5]}

    def authenticate_user(self, username: str, password: str) -> dict | None:
        """Verify username+password. Returns user dict (no password_hash) or None."""
        with self._lock:
            row = self._conn.execute(
                "SELECT id, username, password_hash, display_name, role, created_at, updated_at FROM users WHERE username = ?", (username,)
            ).fetchone()
        if not row:
            return None
        if not _verify_password(password, row[2]):
            return None
        return {"id": row[0], "username": row[1], "display_name": row[3], "role": row[4], "created_at": row[5], "updated_at": row[6]}

    def list_users(self) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, username, display_name, role, created_at, updated_at FROM users ORDER BY created_at ASC"
            ).fetchall()
        return [{"id": r[0], "username": r[1], "display_name": r[2], "role": r[3], "created_at": r[4], "updated_at": r[5]} for r in rows]

    def update_user(self, user_id: str, display_name: str | None = None, role: str | None = None, password: str | None = None) -> dict | None:
        now = datetime.now(timezone.utc).isoformat()
        updates = ["updated_at = ?"]
        params: list = [now]
        if display_name is not None:
            updates.append("display_name = ?")
            params.append(display_name)
        if role is not None:
            updates.append("role = ?")
            params.append(role)
        if password is not None:
            updates.append("password_hash = ?")
            params.append(_hash_password(password))
        params.append(user_id)
        with self._lock:
            self._conn.execute(f"UPDATE users SET {', '.join(updates)} WHERE id = ?", params)
            self._conn.commit()
        return self.get_user_by_id(user_id)

    def delete_user(self, user_id: str) -> bool:
        with self._lock:
            cursor = self._conn.execute("DELETE FROM users WHERE id = ?", (user_id,))
            self._conn.execute("DELETE FROM user_sessions WHERE user_id = ?", (user_id,))
            self._conn.commit()
        return cursor.rowcount > 0

    # --- Sessions ---

    def create_session(self, user_id: str) -> str:
        token = secrets.token_urlsafe(32)
        now = datetime.now(timezone.utc)
        expires_at = (now + timedelta(days=SESSION_TTL_DAYS)).isoformat()
        with self._lock:
            self._conn.execute(
                "INSERT INTO user_sessions (token, user_id, created_at, expires_at, last_used) VALUES (?, ?, ?, ?, ?)",
                (token, user_id, now.isoformat(), expires_at, now.isoformat()),
            )
            self._conn.commit()
        return token

    def get_session_user(self, token: str) -> dict | None:
        """Validate session token. Returns user dict or None if invalid/expired."""
        now = datetime.now(timezone.utc).isoformat()
        with self._lock:
            row = self._conn.execute(
                "SELECT user_id, expires_at FROM user_sessions WHERE token = ?", (token,)
            ).fetchone()
            if not row or row[1] < now:
                return None
            # Update last_used
            self._conn.execute("UPDATE user_sessions SET last_used = ? WHERE token = ?", (now, token))
            self._conn.commit()
        return self.get_user_by_id(row[0])

    def delete_session(self, token: str):
        with self._lock:
            self._conn.execute("DELETE FROM user_sessions WHERE token = ?", (token,))
            self._conn.commit()

    # --- Agent API Key (persistent, never expires) ---

    def get_agent_key(self) -> str | None:
        """Return the stored agent API key, or None if not set."""
        with self._lock:
            row = self._conn.execute(
                "SELECT value FROM settings WHERE key = 'agent_api_key'"
            ).fetchone()
        return row[0] if row else None

    def set_agent_key(self, token: str) -> str:
        """Store (or replace) the agent API key. Returns the token."""
        now = datetime.now(timezone.utc).isoformat()
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO settings (key, value, updated_at) VALUES ('agent_api_key', ?, ?)",
                (token, now),
            )
            self._conn.commit()
        return token

    def verify_agent_key(self, token: str) -> bool:
        """Return True if token matches the stored agent API key."""
        stored = self.get_agent_key()
        return stored is not None and secrets.compare_digest(stored, token)

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
        since_id: str | None = None,
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
        elif since_id:
            ts = self.message_timestamp(since_id)
            if ts:
                query += " AND timestamp > ?"
                params.append(ts)
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

    def message_timestamp(self, message_id: str) -> str | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT timestamp FROM messages WHERE id = ?", (message_id,)
            ).fetchone()
        return row[0] if row else None

    def claim_message(self, message_id: str, agent_name: str) -> bool:
        """Atomically claim a message for processing.

        Returns True if the claim succeeded (this agent now owns the message).
        Returns False if the message was already claimed or does not exist.
        SQLite's single-writer guarantee makes this atomic under the shared lock.
        """
        with self._lock:
            result = self._conn.execute(
                "UPDATE messages SET claimed_by = ? WHERE id = ? AND claimed_by IS NULL",
                (agent_name, message_id),
            )
            self._conn.commit()
            return result.rowcount > 0

    def set_delivery_cursor(
        self,
        agent_name: str,
        thread: str,
        last_message_id: str,
        last_timestamp: str,
    ):
        updated_at = datetime.now(timezone.utc).isoformat()
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO delivery_cursors (agent_name, thread, last_message_id, last_timestamp, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(agent_name, thread) DO UPDATE SET
                    last_message_id = excluded.last_message_id,
                    last_timestamp = excluded.last_timestamp,
                    updated_at = excluded.updated_at
                """,
                (agent_name, thread, last_message_id, last_timestamp, updated_at),
            )
            self._conn.commit()

    def get_delivery_cursor(self, agent_name: str, thread: str = "general") -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT last_message_id, last_timestamp, updated_at FROM delivery_cursors WHERE agent_name = ? AND thread = ?",
                (agent_name, thread),
            ).fetchone()
        if not row:
            return None
        return {
            "agent_name": agent_name,
            "thread": thread,
            "last_message_id": row[0],
            "last_timestamp": row[1],
            "updated_at": row[2],
        }

    def list_delivery_cursors(self, agent_name: str | None = None) -> list[dict]:
        query = "SELECT agent_name, thread, last_message_id, last_timestamp, updated_at FROM delivery_cursors"
        params: list = []
        if agent_name:
            query += " WHERE agent_name = ?"
            params.append(agent_name)
        query += " ORDER BY updated_at DESC"
        with self._lock:
            rows = self._conn.execute(query, params).fetchall()
        return [
            {
                "agent_name": r[0],
                "thread": r[1],
                "last_message_id": r[2],
                "last_timestamp": r[3],
                "updated_at": r[4],
            }
            for r in rows
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

    # --- Agent Requests (persistent request queue) ---

    def create_agent_request(
        self,
        from_agent: str,
        to_agent: str,
        content: str,
        timeout_sec: float = 60.0,
        thread: str = "general",
        correlation_id: str | None = None,
    ) -> dict:
        req_id = str(uuid.uuid4())
        corr_id = correlation_id or str(uuid.uuid4())
        now = datetime.now(timezone.utc)
        expires_at = (now + timedelta(seconds=timeout_sec)).isoformat()
        with self._lock:
            self._conn.execute(
                "INSERT INTO agent_requests (id, from_agent, to_agent, content, status, correlation_id, thread, created_at, expires_at) VALUES (?, ?, ?, ?, 'pending', ?, ?, ?, ?)",
                (req_id, from_agent, to_agent, content, corr_id, thread, now.isoformat(), expires_at),
            )
            self._conn.commit()
        return {
            "id": req_id, "from_agent": from_agent, "to_agent": to_agent,
            "content": content, "status": "pending", "correlation_id": corr_id,
            "thread": thread, "created_at": now.isoformat(), "expires_at": expires_at,
            "response": None, "responded_at": None,
        }

    def get_agent_request(self, request_id: str) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT id, from_agent, to_agent, content, status, correlation_id, thread, created_at, expires_at, response, responded_at FROM agent_requests WHERE id = ?",
                (request_id,)
            ).fetchone()
        if not row:
            return None
        return {
            "id": row[0], "from_agent": row[1], "to_agent": row[2], "content": row[3],
            "status": row[4], "correlation_id": row[5], "thread": row[6],
            "created_at": row[7], "expires_at": row[8], "response": row[9], "responded_at": row[10],
        }

    def get_agent_request_by_correlation(self, correlation_id: str) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT id, from_agent, to_agent, content, status, correlation_id, thread, created_at, expires_at, response, responded_at FROM agent_requests WHERE correlation_id = ?",
                (correlation_id,)
            ).fetchone()
        if not row:
            return None
        return {
            "id": row[0], "from_agent": row[1], "to_agent": row[2], "content": row[3],
            "status": row[4], "correlation_id": row[5], "thread": row[6],
            "created_at": row[7], "expires_at": row[8], "response": row[9], "responded_at": row[10],
        }

    def get_pending_requests(self, agent_name: str) -> list[dict]:
        """Return all pending (non-expired) requests for an agent."""
        now_iso = datetime.now(timezone.utc).isoformat()
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, from_agent, to_agent, content, status, correlation_id, thread, created_at, expires_at, response, responded_at FROM agent_requests WHERE to_agent = ? AND status = 'pending' AND expires_at > ? ORDER BY created_at ASC",
                (agent_name, now_iso)
            ).fetchall()
        return [
            {
                "id": r[0], "from_agent": r[1], "to_agent": r[2], "content": r[3],
                "status": r[4], "correlation_id": r[5], "thread": r[6],
                "created_at": r[7], "expires_at": r[8], "response": r[9], "responded_at": r[10],
            }
            for r in rows
        ]

    def respond_to_agent_request(self, request_id: str, response: str) -> bool:
        """Mark request as answered. Returns True if it was pending, False if already handled."""
        now_iso = datetime.now(timezone.utc).isoformat()
        with self._lock:
            result = self._conn.execute(
                "UPDATE agent_requests SET status = 'answered', response = ?, responded_at = ? WHERE id = ? AND status = 'pending'",
                (response, now_iso, request_id)
            )
            self._conn.commit()
        return result.rowcount > 0

    def wait_for_agent_response(self, correlation_id: str, timeout_sec: float) -> dict | None:
        """Poll DB until the agent_request with this correlation_id is answered or times out."""
        deadline = time.monotonic() + timeout_sec
        while time.monotonic() < deadline:
            row = self.get_agent_request_by_correlation(correlation_id)
            if row and row["status"] == "answered":
                return row
            if row and row["status"] == "timeout":
                return None
            time.sleep(0.4)
        return None

    async def create_agent_request_async(self, from_agent, to_agent, content, timeout_sec=60.0, thread="general", correlation_id=None):
        return await self._run_in_thread(self.create_agent_request, from_agent, to_agent, content, timeout_sec, thread, correlation_id)

    async def get_pending_requests_async(self, agent_name):
        return await self._run_in_thread(self.get_pending_requests, agent_name)

    async def get_agent_request_async(self, request_id):
        return await self._run_in_thread(self.get_agent_request, request_id)

    async def respond_to_agent_request_async(self, request_id, response):
        return await self._run_in_thread(self.respond_to_agent_request, request_id, response)

    def close(self):
        if hasattr(self, "_prune_timer"):
            self._prune_timer.cancel()
        with self._lock:
            self._conn.close()

    def clear_board(self, include_threads: bool = True):
        """Remove all messages and optionally all threads."""
        with self._lock:
            self._conn.execute("DELETE FROM messages")
            self._conn.execute("DELETE FROM delivery_cursors")
            if include_threads:
                self._conn.execute("DELETE FROM threads")
            self._conn.commit()
