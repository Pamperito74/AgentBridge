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

from .models import Agent, Approval, Artifact, ActivityEntry, AutomationRule, ContentBlock, CostEvent, Message, Task, Thread, ThreadSummary

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
                CREATE TABLE IF NOT EXISTS agent_memory (
                    agent_name TEXT NOT NULL,
                    key TEXT NOT NULL,
                    value TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (agent_name, key)
                );
                CREATE TABLE IF NOT EXISTS tasks (
                    id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    description TEXT DEFAULT '',
                    status TEXT DEFAULT 'todo',
                    priority TEXT DEFAULT 'medium',
                    assignee TEXT,
                    created_by TEXT NOT NULL,
                    thread TEXT DEFAULT 'general',
                    parent_id TEXT,
                    labels TEXT DEFAULT '[]',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    completed_at TEXT
                );
                CREATE TABLE IF NOT EXISTS cost_events (
                    id TEXT PRIMARY KEY,
                    agent_name TEXT NOT NULL,
                    provider TEXT DEFAULT 'anthropic',
                    model TEXT NOT NULL,
                    input_tokens INTEGER DEFAULT 0,
                    output_tokens INTEGER DEFAULT 0,
                    cost_cents INTEGER DEFAULT 0,
                    task_id TEXT,
                    thread TEXT,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS activity_log (
                    id TEXT PRIMARY KEY,
                    actor_type TEXT DEFAULT 'system',
                    actor_id TEXT,
                    action TEXT NOT NULL,
                    entity_type TEXT,
                    entity_id TEXT,
                    details TEXT DEFAULT '{}',
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS approvals (
                    id TEXT PRIMARY KEY,
                    type TEXT NOT NULL,
                    requested_by TEXT NOT NULL,
                    status TEXT DEFAULT 'pending',
                    payload TEXT DEFAULT '{}',
                    decision_note TEXT,
                    decided_by TEXT,
                    created_at TEXT NOT NULL,
                    decided_at TEXT
                );
                CREATE TABLE IF NOT EXISTS automation_rules (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    trigger_event TEXT NOT NULL,
                    conditions TEXT DEFAULT '{}',
                    action_type TEXT NOT NULL,
                    action_params TEXT DEFAULT '{}',
                    enabled INTEGER DEFAULT 1,
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
                CREATE INDEX IF NOT EXISTS idx_delivery_cursors_agent_thread
                    ON delivery_cursors(agent_name, thread);
                CREATE INDEX IF NOT EXISTS idx_agent_requests_to_status
                    ON agent_requests(to_agent, status);
                CREATE INDEX IF NOT EXISTS idx_agent_requests_correlation
                    ON agent_requests(correlation_id);
                CREATE INDEX IF NOT EXISTS idx_agent_requests_expires
                    ON agent_requests(expires_at, status);
                CREATE INDEX IF NOT EXISTS idx_agent_memory_agent
                    ON agent_memory(agent_name);
                CREATE INDEX IF NOT EXISTS idx_tasks_status
                    ON tasks(status);
                CREATE INDEX IF NOT EXISTS idx_tasks_assignee
                    ON tasks(assignee);
                CREATE INDEX IF NOT EXISTS idx_tasks_created_at
                    ON tasks(created_at);
                CREATE INDEX IF NOT EXISTS idx_cost_events_agent
                    ON cost_events(agent_name, created_at);
                CREATE INDEX IF NOT EXISTS idx_activity_log_created_at
                    ON activity_log(created_at);
                CREATE INDEX IF NOT EXISTS idx_activity_log_actor
                    ON activity_log(actor_id);
                CREATE INDEX IF NOT EXISTS idx_approvals_status
                    ON approvals(status);
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
                ("reports_to", "ALTER TABLE agents ADD COLUMN reports_to TEXT"),
                ("budget_monthly_cents", "ALTER TABLE agents ADD COLUMN budget_monthly_cents INTEGER DEFAULT 0"),
                ("spent_monthly_cents", "ALTER TABLE agents ADD COLUMN spent_monthly_cents INTEGER DEFAULT 0"),
                ("is_orchestrator", "ALTER TABLE agents ADD COLUMN is_orchestrator INTEGER DEFAULT 0"),
                ("autonomous", "ALTER TABLE agents ADD COLUMN autonomous INTEGER DEFAULT 0"),
                ("autonomous_mode", "ALTER TABLE agents ADD COLUMN autonomous_mode TEXT DEFAULT 'decide'"),
                ("watch_threads", "ALTER TABLE agents ADD COLUMN watch_threads TEXT DEFAULT '[]'"),
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
                ("blocks", "ALTER TABLE messages ADD COLUMN blocks TEXT DEFAULT '[]'"),
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
            # Create agent_memory table if missing (migration-safe)
            self._conn.execute("""
                CREATE TABLE IF NOT EXISTS agent_memory (
                    agent_name TEXT NOT NULL,
                    key TEXT NOT NULL,
                    value TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (agent_name, key)
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

    async def register_agent_async(self, name: str, role: str = "", capabilities: list[str] | None = None,
                                    agent_type: str = "bot", is_orchestrator: bool = False,
                                    autonomous: bool = False, autonomous_mode: str = "decide",
                                    watch_threads: list[str] | None = None) -> Agent:
        return await self._run_in_thread(self.register_agent, name, role, capabilities, agent_type,
                                         is_orchestrator, autonomous, autonomous_mode, watch_threads)

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
        blocks: list[dict] | None = None,
    ) -> Message:
        return await self._run_in_thread(
            self.add_message,
            sender, content, recipient, thread, msg_type, artifacts, actor_id, actor_type,
            target_id, target_type, event_type, metadata, labels, correlation_id, blocks,
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

    _AGENT_COLS = ("name, role, status, working_on, capabilities, agent_type, connected_at, last_seen, "
                   "reports_to, budget_monthly_cents, spent_monthly_cents, "
                   "is_orchestrator, autonomous, autonomous_mode, watch_threads")

    def _agent_from_row(self, row) -> Agent:
        return Agent(
            name=row[0], role=row[1] or "", status=row[2] or "online",
            working_on=row[3] or "",
            capabilities=json.loads(row[4]) if row[4] else [],
            agent_type=row[5] or "bot",
            connected_at=datetime.fromisoformat(row[6]),
            last_seen=datetime.fromisoformat(row[7]),
            reports_to=row[8],
            budget_monthly_cents=row[9] or 0,
            spent_monthly_cents=row[10] or 0,
            is_orchestrator=bool(row[11]) if len(row) > 11 else False,
            autonomous=bool(row[12]) if len(row) > 12 else False,
            autonomous_mode=row[13] or "decide" if len(row) > 13 else "decide",
            watch_threads=json.loads(row[14]) if len(row) > 14 and row[14] else [],
        )

    def get_agent(self, name: str) -> Agent | None:
        """Look up a single agent by name. Returns None if not found."""
        with self._lock:
            row = self._conn.execute(
                f"SELECT {self._AGENT_COLS} FROM agents WHERE name = ?", (name,)
            ).fetchone()
        if not row:
            return None
        return self._agent_from_row(row)

    async def get_agent_async(self, name: str) -> Agent | None:
        return await self._run_in_thread(self.get_agent, name)

    def register_agent(self, name: str, role: str = "", capabilities: list[str] | None = None,
                        agent_type: str = "bot", is_orchestrator: bool = False,
                        autonomous: bool = False, autonomous_mode: str = "decide",
                        watch_threads: list[str] | None = None) -> Agent:
        now = datetime.now(timezone.utc)
        caps = capabilities or []
        wt = watch_threads or []
        agent = Agent(name=name, role=role, capabilities=caps, agent_type=agent_type,
                      connected_at=now, last_seen=now, is_orchestrator=is_orchestrator,
                      autonomous=autonomous, autonomous_mode=autonomous_mode, watch_threads=wt)
        with self._lock:
            self._conn.execute(
                """INSERT OR REPLACE INTO agents
                   (name, role, status, working_on, capabilities, agent_type, connected_at, last_seen,
                    is_orchestrator, autonomous, autonomous_mode, watch_threads)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (agent.name, agent.role, agent.status, agent.working_on,
                 json.dumps(agent.capabilities), agent.agent_type,
                 agent.connected_at.isoformat(), agent.last_seen.isoformat(),
                 int(agent.is_orchestrator), int(agent.autonomous),
                 agent.autonomous_mode, json.dumps(agent.watch_threads)),
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
                f"SELECT {self._AGENT_COLS} FROM agents WHERE name = ?", (name,)
            ).fetchone()
        if not row:
            return None
        return self._agent_from_row(row)

    def list_agents(self) -> list[Agent]:
        with self._lock:
            rows = self._conn.execute(
                f"SELECT {self._AGENT_COLS} FROM agents ORDER BY last_seen DESC"
            ).fetchall()
        return [self._agent_from_row(r) for r in rows]

    def get_orchestrators(self) -> list[Agent]:
        """Return all agents that are marked as orchestrators."""
        with self._lock:
            rows = self._conn.execute(
                f"SELECT {self._AGENT_COLS} FROM agents WHERE is_orchestrator = 1"
            ).fetchall()
        return [self._agent_from_row(r) for r in rows]

    def list_autonomous_agents_for_thread(self, thread: str) -> list[Agent]:
        """Return autonomous agents that watch a given thread."""
        with self._lock:
            rows = self._conn.execute(
                f"SELECT {self._AGENT_COLS} FROM agents WHERE autonomous = 1"
            ).fetchall()
        return [
            self._agent_from_row(r) for r in rows
            if thread in (json.loads(r[14]) if r[14] else [])
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
        blocks: list[dict] | None = None,
    ) -> Message:
        artifact_objs = [Artifact(**a) for a in (artifacts or [])]
        block_objs = [ContentBlock(**b) for b in (blocks or [])]
        msg = Message(
            sender=sender, content=content, recipient=recipient,
            thread=thread, msg_type=msg_type, artifacts=artifact_objs,
            blocks=block_objs,
            actor_id=actor_id, actor_type=actor_type,
            target_id=target_id, target_type=target_type,
            event_type=event_type, metadata=metadata or {},
            labels=labels or [],
            correlation_id=correlation_id,
        )
        with self._lock:
            self._conn.execute(
                "INSERT INTO messages (id, actor_id, actor_type, target_id, target_type, event_type, metadata, labels, sender, recipient, thread, content, msg_type, mentions, artifacts, blocks, correlation_id, timestamp) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    msg.id, msg.actor_id, msg.actor_type, msg.target_id, msg.target_type,
                    msg.event_type, json.dumps(msg.metadata), json.dumps(msg.labels),
                    msg.sender, msg.recipient, msg.thread, msg.content,
                    msg.msg_type, json.dumps(msg.mentions),
                    json.dumps([a.model_dump() for a in msg.artifacts]),
                    json.dumps([b.model_dump() for b in msg.blocks]),
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

        query = "SELECT id, actor_id, actor_type, target_id, target_type, event_type, metadata, labels, sender, recipient, thread, content, msg_type, mentions, artifacts, blocks, correlation_id, timestamp FROM messages WHERE 1=1"
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
                blocks=[ContentBlock(**b) for b in json.loads(r[15])] if r[15] else [],
                correlation_id=r[16],
                timestamp=datetime.fromisoformat(r[17]),
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

    # --- Agent Memory ---

    def memory_set(self, agent_name: str, key: str, value: str) -> dict:
        """Upsert a memory entry for an agent."""
        now = datetime.now(timezone.utc).isoformat()
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO agent_memory (agent_name, key, value, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(agent_name, key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
                """,
                (agent_name, key, value, now, now),
            )
            self._conn.commit()
        return {"agent_name": agent_name, "key": key, "value": value, "updated_at": now}

    def memory_get(self, agent_name: str, key: str) -> dict | None:
        """Retrieve a single memory entry."""
        with self._lock:
            row = self._conn.execute(
                "SELECT value, created_at, updated_at FROM agent_memory WHERE agent_name = ? AND key = ?",
                (agent_name, key),
            ).fetchone()
        if not row:
            return None
        return {"agent_name": agent_name, "key": key, "value": row[0], "created_at": row[1], "updated_at": row[2]}

    def memory_list(self, agent_name: str) -> list[dict]:
        """List all memory entries for an agent."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT key, value, created_at, updated_at FROM agent_memory WHERE agent_name = ? ORDER BY key ASC",
                (agent_name,),
            ).fetchall()
        return [
            {"agent_name": agent_name, "key": r[0], "value": r[1], "created_at": r[2], "updated_at": r[3]}
            for r in rows
        ]

    def memory_delete(self, agent_name: str, key: str) -> bool:
        """Delete a single memory entry. Returns True if deleted."""
        with self._lock:
            result = self._conn.execute(
                "DELETE FROM agent_memory WHERE agent_name = ? AND key = ?",
                (agent_name, key),
            )
            self._conn.commit()
        return result.rowcount > 0

    def memory_search(self, agent_name: str, query: str) -> list[dict]:
        """Case-insensitive substring search across keys and values."""
        pattern = f"%{query}%"
        with self._lock:
            rows = self._conn.execute(
                "SELECT key, value, created_at, updated_at FROM agent_memory WHERE agent_name = ? AND (key LIKE ? OR value LIKE ?) ORDER BY key ASC",
                (agent_name, pattern, pattern),
            ).fetchall()
        return [
            {"agent_name": agent_name, "key": r[0], "value": r[1], "created_at": r[2], "updated_at": r[3]}
            for r in rows
        ]

    async def memory_set_async(self, agent_name: str, key: str, value: str) -> dict:
        return await self._run_in_thread(self.memory_set, agent_name, key, value)

    async def memory_get_async(self, agent_name: str, key: str) -> dict | None:
        return await self._run_in_thread(self.memory_get, agent_name, key)

    async def memory_list_async(self, agent_name: str) -> list[dict]:
        return await self._run_in_thread(self.memory_list, agent_name)

    async def memory_delete_async(self, agent_name: str, key: str) -> bool:
        return await self._run_in_thread(self.memory_delete, agent_name, key)

    async def memory_search_async(self, agent_name: str, query: str) -> list[dict]:
        return await self._run_in_thread(self.memory_search, agent_name, query)

    # --- Tasks ---

    def create_task(self, title: str, created_by: str, description: str = "", status: str = "todo",
                    priority: str = "medium", assignee: str | None = None, thread: str = "general",
                    parent_id: str | None = None, labels: list[str] | None = None) -> dict:
        task = Task(title=title, created_by=created_by, description=description, status=status,
                    priority=priority, assignee=assignee, thread=thread, parent_id=parent_id,
                    labels=labels or [])
        with self._lock:
            self._conn.execute(
                "INSERT INTO tasks (id, title, description, status, priority, assignee, created_by, thread, parent_id, labels, created_at, updated_at, completed_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (task.id, task.title, task.description, task.status, task.priority, task.assignee,
                 task.created_by, task.thread, task.parent_id, json.dumps(task.labels),
                 task.created_at.isoformat(), task.updated_at.isoformat(), None),
            )
            self._conn.commit()
        return self._task_row_to_dict(task.id)

    def get_task(self, task_id: str) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT id, title, description, status, priority, assignee, created_by, thread, parent_id, labels, created_at, updated_at, completed_at FROM tasks WHERE id = ?",
                (task_id,)
            ).fetchone()
        if not row:
            return None
        return self._task_row_to_dict_from_row(row)

    def _task_row_to_dict(self, task_id: str) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT id, title, description, status, priority, assignee, created_by, thread, parent_id, labels, created_at, updated_at, completed_at FROM tasks WHERE id = ?",
                (task_id,)
            ).fetchone()
        if not row:
            return None
        return self._task_row_to_dict_from_row(row)

    def _task_row_to_dict_from_row(self, row) -> dict:
        return {
            "id": row[0], "title": row[1], "description": row[2] or "", "status": row[3],
            "priority": row[4], "assignee": row[5], "created_by": row[6], "thread": row[7] or "general",
            "parent_id": row[8], "labels": json.loads(row[9]) if row[9] else [],
            "created_at": row[10], "updated_at": row[11], "completed_at": row[12],
        }

    def list_tasks(self, status: str | None = None, assignee: str | None = None,
                   thread: str | None = None, priority: str | None = None, limit: int = 50) -> list[dict]:
        query = "SELECT id, title, description, status, priority, assignee, created_by, thread, parent_id, labels, created_at, updated_at, completed_at FROM tasks WHERE 1=1"
        params: list = []
        if status:
            query += " AND status = ?"
            params.append(status)
        if assignee:
            query += " AND assignee = ?"
            params.append(assignee)
        if thread:
            query += " AND thread = ?"
            params.append(thread)
        if priority:
            query += " AND priority = ?"
            params.append(priority)
        query += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        with self._lock:
            rows = self._conn.execute(query, params).fetchall()
        return [self._task_row_to_dict_from_row(r) for r in rows]

    def update_task(self, task_id: str, **kwargs) -> dict | None:
        allowed = {"title", "description", "status", "priority", "assignee", "thread", "parent_id", "labels"}
        updates = ["updated_at = ?"]
        params: list = [datetime.now(timezone.utc).isoformat()]
        for k, v in kwargs.items():
            if k in allowed and v is not None:
                updates.append(f"{k} = ?")
                params.append(json.dumps(v) if k == "labels" else v)
        if kwargs.get("status") in ("done",) and "completed_at" not in kwargs:
            updates.append("completed_at = ?")
            params.append(datetime.now(timezone.utc).isoformat())
        params.append(task_id)
        with self._lock:
            self._conn.execute(f"UPDATE tasks SET {', '.join(updates)} WHERE id = ?", params)
            self._conn.commit()
        return self._task_row_to_dict(task_id)

    def delete_task(self, task_id: str) -> bool:
        with self._lock:
            result = self._conn.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
            self._conn.commit()
        return result.rowcount > 0

    async def create_task_async(self, *args, **kwargs) -> dict:
        return await self._run_in_thread(self.create_task, *args, **kwargs)

    async def get_task_async(self, task_id: str) -> dict | None:
        return await self._run_in_thread(self.get_task, task_id)

    async def list_tasks_async(self, **kwargs) -> list[dict]:
        return await self._run_in_thread(self.list_tasks, **kwargs)

    async def update_task_async(self, task_id: str, **kwargs) -> dict | None:
        return await self._run_in_thread(self.update_task, task_id, **kwargs)

    async def delete_task_async(self, task_id: str) -> bool:
        return await self._run_in_thread(self.delete_task, task_id)

    # --- Cost Events ---

    def record_cost_event(self, agent_name: str, model: str, input_tokens: int = 0,
                          output_tokens: int = 0, cost_cents: int = 0, provider: str = "anthropic",
                          task_id: str | None = None, thread: str | None = None) -> dict:
        ev = CostEvent(agent_name=agent_name, model=model, input_tokens=input_tokens,
                       output_tokens=output_tokens, cost_cents=cost_cents, provider=provider,
                       task_id=task_id, thread=thread)
        with self._lock:
            self._conn.execute(
                "INSERT INTO cost_events (id, agent_name, provider, model, input_tokens, output_tokens, cost_cents, task_id, thread, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (ev.id, ev.agent_name, ev.provider, ev.model, ev.input_tokens, ev.output_tokens,
                 ev.cost_cents, ev.task_id, ev.thread, ev.created_at.isoformat()),
            )
            # Update agent's monthly spent
            self._conn.execute(
                "UPDATE agents SET spent_monthly_cents = spent_monthly_cents + ? WHERE name = ?",
                (cost_cents, agent_name)
            )
            self._conn.commit()
        return {
            "id": ev.id, "agent_name": ev.agent_name, "provider": ev.provider, "model": ev.model,
            "input_tokens": ev.input_tokens, "output_tokens": ev.output_tokens,
            "cost_cents": ev.cost_cents, "task_id": ev.task_id, "thread": ev.thread,
            "created_at": ev.created_at.isoformat(),
        }

    def list_cost_events(self, agent_name: str | None = None, since: str | None = None,
                         limit: int = 50) -> list[dict]:
        query = "SELECT id, agent_name, provider, model, input_tokens, output_tokens, cost_cents, task_id, thread, created_at FROM cost_events WHERE 1=1"
        params: list = []
        if agent_name:
            query += " AND agent_name = ?"
            params.append(agent_name)
        if since:
            query += " AND created_at > ?"
            params.append(since)
        query += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        with self._lock:
            rows = self._conn.execute(query, params).fetchall()
        return [
            {"id": r[0], "agent_name": r[1], "provider": r[2], "model": r[3],
             "input_tokens": r[4], "output_tokens": r[5], "cost_cents": r[6],
             "task_id": r[7], "thread": r[8], "created_at": r[9]}
            for r in rows
        ]

    def cost_summary_for_agent(self, agent_name: str) -> dict:
        with self._lock:
            row = self._conn.execute(
                "SELECT SUM(input_tokens), SUM(output_tokens), SUM(cost_cents), COUNT(*) FROM cost_events WHERE agent_name = ?",
                (agent_name,)
            ).fetchone()
        return {
            "agent_name": agent_name,
            "total_input_tokens": row[0] or 0,
            "total_output_tokens": row[1] or 0,
            "total_cost_cents": row[2] or 0,
            "event_count": row[3] or 0,
        }

    def cost_summary_platform(self) -> dict:
        with self._lock:
            row = self._conn.execute(
                "SELECT SUM(input_tokens), SUM(output_tokens), SUM(cost_cents), COUNT(*) FROM cost_events"
            ).fetchone()
            agents_row = self._conn.execute(
                "SELECT agent_name, SUM(input_tokens), SUM(output_tokens), SUM(cost_cents) FROM cost_events GROUP BY agent_name ORDER BY SUM(cost_cents) DESC"
            ).fetchall()
        return {
            "total_input_tokens": row[0] or 0,
            "total_output_tokens": row[1] or 0,
            "total_cost_cents": row[2] or 0,
            "event_count": row[3] or 0,
            "by_agent": [
                {"agent_name": r[0], "input_tokens": r[1] or 0, "output_tokens": r[2] or 0, "cost_cents": r[3] or 0}
                for r in agents_row
            ],
        }

    def set_agent_budget(self, agent_name: str, budget_monthly_cents: int) -> bool:
        with self._lock:
            result = self._conn.execute(
                "UPDATE agents SET budget_monthly_cents = ? WHERE name = ?",
                (budget_monthly_cents, agent_name)
            )
            self._conn.commit()
        return result.rowcount > 0

    async def record_cost_event_async(self, *args, **kwargs) -> dict:
        return await self._run_in_thread(self.record_cost_event, *args, **kwargs)

    async def list_cost_events_async(self, **kwargs) -> list[dict]:
        return await self._run_in_thread(self.list_cost_events, **kwargs)

    async def cost_summary_for_agent_async(self, agent_name: str) -> dict:
        return await self._run_in_thread(self.cost_summary_for_agent, agent_name)

    async def cost_summary_platform_async(self) -> dict:
        return await self._run_in_thread(self.cost_summary_platform)

    async def set_agent_budget_async(self, agent_name: str, budget_monthly_cents: int) -> bool:
        return await self._run_in_thread(self.set_agent_budget, agent_name, budget_monthly_cents)

    # --- Activity Log ---

    def log_activity(self, action: str, actor_type: str = "system", actor_id: str | None = None,
                     entity_type: str | None = None, entity_id: str | None = None,
                     details: dict | None = None) -> dict:
        entry = ActivityEntry(action=action, actor_type=actor_type, actor_id=actor_id,
                              entity_type=entity_type, entity_id=entity_id, details=details or {})
        with self._lock:
            self._conn.execute(
                "INSERT INTO activity_log (id, actor_type, actor_id, action, entity_type, entity_id, details, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (entry.id, entry.actor_type, entry.actor_id, entry.action, entry.entity_type,
                 entry.entity_id, json.dumps(entry.details), entry.created_at.isoformat()),
            )
            self._conn.commit()
        return {
            "id": entry.id, "actor_type": entry.actor_type, "actor_id": entry.actor_id,
            "action": entry.action, "entity_type": entry.entity_type, "entity_id": entry.entity_id,
            "details": entry.details, "created_at": entry.created_at.isoformat(),
        }

    def list_activity(self, actor_id: str | None = None, action: str | None = None,
                      entity_type: str | None = None, since: str | None = None,
                      limit: int = 100) -> list[dict]:
        query = "SELECT id, actor_type, actor_id, action, entity_type, entity_id, details, created_at FROM activity_log WHERE 1=1"
        params: list = []
        if actor_id:
            query += " AND actor_id = ?"
            params.append(actor_id)
        if action:
            query += " AND action = ?"
            params.append(action)
        if entity_type:
            query += " AND entity_type = ?"
            params.append(entity_type)
        if since:
            query += " AND created_at > ?"
            params.append(since)
        query += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        with self._lock:
            rows = self._conn.execute(query, params).fetchall()
        return [
            {"id": r[0], "actor_type": r[1], "actor_id": r[2], "action": r[3],
             "entity_type": r[4], "entity_id": r[5],
             "details": json.loads(r[6]) if r[6] else {},
             "created_at": r[7]}
            for r in rows
        ]

    async def log_activity_async(self, *args, **kwargs) -> dict:
        return await self._run_in_thread(self.log_activity, *args, **kwargs)

    async def list_activity_async(self, **kwargs) -> list[dict]:
        return await self._run_in_thread(self.list_activity, **kwargs)

    # --- Approvals ---

    def create_approval(self, type: str, requested_by: str, payload: dict | None = None) -> dict:
        approval = Approval(type=type, requested_by=requested_by, payload=payload or {})
        with self._lock:
            self._conn.execute(
                "INSERT INTO approvals (id, type, requested_by, status, payload, decision_note, decided_by, created_at, decided_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (approval.id, approval.type, approval.requested_by, approval.status,
                 json.dumps(approval.payload), None, None, approval.created_at.isoformat(), None),
            )
            self._conn.commit()
        return self._approval_row_to_dict(approval.id)

    def get_approval(self, approval_id: str) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT id, type, requested_by, status, payload, decision_note, decided_by, created_at, decided_at FROM approvals WHERE id = ?",
                (approval_id,)
            ).fetchone()
        if not row:
            return None
        return self._approval_from_row(row)

    def _approval_row_to_dict(self, approval_id: str) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT id, type, requested_by, status, payload, decision_note, decided_by, created_at, decided_at FROM approvals WHERE id = ?",
                (approval_id,)
            ).fetchone()
        if not row:
            return None
        return self._approval_from_row(row)

    def _approval_from_row(self, row) -> dict:
        return {
            "id": row[0], "type": row[1], "requested_by": row[2], "status": row[3],
            "payload": json.loads(row[4]) if row[4] else {},
            "decision_note": row[5], "decided_by": row[6],
            "created_at": row[7], "decided_at": row[8],
        }

    def list_approvals(self, status: str | None = None, type: str | None = None,
                       limit: int = 50) -> list[dict]:
        query = "SELECT id, type, requested_by, status, payload, decision_note, decided_by, created_at, decided_at FROM approvals WHERE 1=1"
        params: list = []
        if status:
            query += " AND status = ?"
            params.append(status)
        if type:
            query += " AND type = ?"
            params.append(type)
        query += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        with self._lock:
            rows = self._conn.execute(query, params).fetchall()
        return [self._approval_from_row(r) for r in rows]

    def decide_approval(self, approval_id: str, decision: str, decided_by: str,
                        decision_note: str | None = None) -> dict | None:
        """Set approval status to 'approved' or 'rejected'."""
        now = datetime.now(timezone.utc).isoformat()
        with self._lock:
            result = self._conn.execute(
                "UPDATE approvals SET status = ?, decided_by = ?, decision_note = ?, decided_at = ? WHERE id = ? AND status = 'pending'",
                (decision, decided_by, decision_note, now, approval_id)
            )
            self._conn.commit()
        if result.rowcount == 0:
            return None
        return self._approval_row_to_dict(approval_id)

    def cancel_approval(self, approval_id: str) -> bool:
        with self._lock:
            result = self._conn.execute(
                "UPDATE approvals SET status = 'cancelled' WHERE id = ? AND status = 'pending'",
                (approval_id,)
            )
            self._conn.commit()
        return result.rowcount > 0

    async def create_approval_async(self, *args, **kwargs) -> dict:
        return await self._run_in_thread(self.create_approval, *args, **kwargs)

    async def get_approval_async(self, approval_id: str) -> dict | None:
        return await self._run_in_thread(self.get_approval, approval_id)

    async def list_approvals_async(self, **kwargs) -> list[dict]:
        return await self._run_in_thread(self.list_approvals, **kwargs)

    async def decide_approval_async(self, *args, **kwargs) -> dict | None:
        return await self._run_in_thread(self.decide_approval, *args, **kwargs)

    async def cancel_approval_async(self, approval_id: str) -> bool:
        return await self._run_in_thread(self.cancel_approval, approval_id)

    # --- Automation Rules ---

    def create_automation_rule(self, name: str, trigger_event: str, action_type: str,
                                conditions: dict | None = None,
                                action_params: dict | None = None) -> dict:
        rule = AutomationRule(name=name, trigger_event=trigger_event, action_type=action_type,
                              conditions=conditions or {}, action_params=action_params or {})
        with self._lock:
            self._conn.execute(
                "INSERT INTO automation_rules (id, name, trigger_event, conditions, action_type, action_params, enabled, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (rule.id, rule.name, rule.trigger_event, json.dumps(rule.conditions),
                 rule.action_type, json.dumps(rule.action_params), 1, rule.created_at.isoformat()),
            )
            self._conn.commit()
        return self._rule_from_id(rule.id)

    def _rule_from_row(self, row) -> dict:
        return {
            "id": row[0], "name": row[1], "trigger_event": row[2],
            "conditions": json.loads(row[3]) if row[3] else {},
            "action_type": row[4],
            "action_params": json.loads(row[5]) if row[5] else {},
            "enabled": bool(row[6]), "created_at": row[7],
        }

    def _rule_from_id(self, rule_id: str) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT id, name, trigger_event, conditions, action_type, action_params, enabled, created_at FROM automation_rules WHERE id = ?",
                (rule_id,)
            ).fetchone()
        return self._rule_from_row(row) if row else None

    def list_automation_rules(self, trigger_event: str | None = None, enabled_only: bool = False) -> list[dict]:
        query = "SELECT id, name, trigger_event, conditions, action_type, action_params, enabled, created_at FROM automation_rules WHERE 1=1"
        params: list = []
        if trigger_event:
            query += " AND trigger_event = ?"
            params.append(trigger_event)
        if enabled_only:
            query += " AND enabled = 1"
        query += " ORDER BY created_at ASC"
        with self._lock:
            rows = self._conn.execute(query, params).fetchall()
        return [self._rule_from_row(r) for r in rows]

    def set_automation_rule_enabled(self, rule_id: str, enabled: bool) -> bool:
        with self._lock:
            result = self._conn.execute(
                "UPDATE automation_rules SET enabled = ? WHERE id = ?", (int(enabled), rule_id)
            )
            self._conn.commit()
        return result.rowcount > 0

    def delete_automation_rule(self, rule_id: str) -> bool:
        with self._lock:
            result = self._conn.execute("DELETE FROM automation_rules WHERE id = ?", (rule_id,))
            self._conn.commit()
        return result.rowcount > 0

    async def create_automation_rule_async(self, *args, **kwargs) -> dict:
        return await self._run_in_thread(self.create_automation_rule, *args, **kwargs)

    async def list_automation_rules_async(self, **kwargs) -> list[dict]:
        return await self._run_in_thread(self.list_automation_rules, **kwargs)

    async def set_automation_rule_enabled_async(self, rule_id: str, enabled: bool) -> bool:
        return await self._run_in_thread(self.set_automation_rule_enabled, rule_id, enabled)

    async def delete_automation_rule_async(self, rule_id: str) -> bool:
        return await self._run_in_thread(self.delete_automation_rule, rule_id)
