"""Tests for AgentBridge core functionality."""

import pytest
from pathlib import Path
import asyncio
import threading
import time

from agentbridge.store import MessageStore


@pytest.fixture
def store(tmp_path):
    """Create a fresh store with a temp database."""
    db = tmp_path / "test.db"
    s = MessageStore(db_path=db)
    yield s
    s.close()


class TestAgents:
    def test_register_agent(self, store):
        agent = store.register_agent("agent-1", role="testing")
        assert agent.name == "agent-1"
        assert agent.role == "testing"
        assert agent.status == "online"

    def test_list_agents(self, store):
        store.register_agent("a1")
        store.register_agent("a2", role="helper")
        agents = store.list_agents()
        names = [a.name for a in agents]
        assert "a1" in names
        assert "a2" in names

    def test_re_register_updates(self, store):
        store.register_agent("a1", role="old")
        store.register_agent("a1", role="new")
        agents = store.list_agents()
        assert len(agents) == 1
        assert agents[0].role == "new"


class TestHeartbeat:
    def test_heartbeat_updates_status(self, store):
        store.register_agent("a1")
        agent = store.heartbeat("a1", status="busy", working_on="fixing bugs")
        assert agent.status == "busy"
        assert agent.working_on == "fixing bugs"

    def test_heartbeat_unknown_agent(self, store):
        result = store.heartbeat("unknown")
        assert result is None

    def test_heartbeat_updates_last_seen(self, store):
        store.register_agent("a1")
        original = store.list_agents()[0].last_seen
        store.heartbeat("a1", status="idle")
        updated = store.list_agents()[0].last_seen
        assert updated >= original

    def test_status_shown_in_list(self, store):
        store.register_agent("a1")
        store.heartbeat("a1", status="busy", working_on="reviewing PR")
        agents = store.list_agents()
        assert agents[0].status == "busy"
        assert agents[0].working_on == "reviewing PR"


class TestMessages:
    def test_send_and_read(self, store):
        store.register_agent("sender")
        store.add_message(sender="sender", content="hello world")
        msgs = store.read_messages()
        assert len(msgs) == 1
        assert msgs[0].content == "hello world"
        assert msgs[0].sender == "sender"

    def test_thread_filter(self, store):
        store.register_agent("s1")
        store.add_message(sender="s1", content="general msg")
        store.add_message(sender="s1", content="debug msg", thread="debug")
        all_msgs = store.read_messages()
        assert len(all_msgs) == 2
        debug_msgs = store.read_messages(thread="debug")
        assert len(debug_msgs) == 1
        assert debug_msgs[0].content == "debug msg"

    def test_sender_filter(self, store):
        store.register_agent("a1")
        store.register_agent("a2")
        store.add_message(sender="a1", content="from a1")
        store.add_message(sender="a2", content="from a2")
        msgs = store.read_messages(sender="a1")
        assert len(msgs) == 1
        assert msgs[0].sender == "a1"

    def test_recipient(self, store):
        store.register_agent("a1")
        msg = store.add_message(sender="a1", content="private", recipient="a2")
        assert msg.recipient == "a2"

    def test_limit(self, store):
        store.register_agent("s")
        for i in range(10):
            store.add_message(sender="s", content=f"msg {i}")
        msgs = store.read_messages(limit=3)
        assert len(msgs) == 3

    def test_messages_ordered_chronologically(self, store):
        store.register_agent("s")
        store.add_message(sender="s", content="first")
        store.add_message(sender="s", content="second")
        store.add_message(sender="s", content="third")
        msgs = store.read_messages()
        assert msgs[0].content == "first"
        assert msgs[-1].content == "third"

    def test_before_cursor(self, store):
        store.register_agent("s")
        store.add_message(sender="s", content="one")
        store.add_message(sender="s", content="two")
        store.add_message(sender="s", content="three")
        all_msgs = store.read_messages(limit=10)
        cursor = all_msgs[-1].timestamp.isoformat()
        older = store.read_messages(before=cursor, limit=10)
        assert [m.content for m in older] == ["one", "two"]


class TestInboxFiltering:
    def test_inbox_sees_direct_messages(self, store):
        store.register_agent("a1")
        store.register_agent("a2")
        store.add_message(sender="a1", content="hey a2", recipient="a2")
        store.add_message(sender="a1", content="hey a3", recipient="a3")
        msgs = store.read_messages(as_agent="a2")
        assert len(msgs) == 1
        assert msgs[0].content == "hey a2"

    def test_inbox_sees_broadcasts(self, store):
        store.register_agent("a1")
        store.add_message(sender="a1", content="broadcast")
        msgs = store.read_messages(as_agent="a2")
        assert len(msgs) == 1
        assert msgs[0].content == "broadcast"

    def test_inbox_excludes_others_dms(self, store):
        store.register_agent("a1")
        store.add_message(sender="a1", content="to a3", recipient="a3")
        msgs = store.read_messages(as_agent="a2")
        assert len(msgs) == 0

    def test_inbox_combined_with_thread(self, store):
        store.register_agent("a1")
        store.add_message(sender="a1", content="dm in debug", recipient="a2", thread="debug")
        store.add_message(sender="a1", content="dm in general", recipient="a2")
        msgs = store.read_messages(as_agent="a2", thread="debug")
        assert len(msgs) == 1
        assert msgs[0].thread == "debug"


class TestMessageTypes:
    def test_default_type_is_chat(self, store):
        store.register_agent("s")
        msg = store.add_message(sender="s", content="hello")
        assert msg.msg_type == "chat"

    def test_custom_type(self, store):
        store.register_agent("s")
        msg = store.add_message(sender="s", content="need help", msg_type="request")
        assert msg.msg_type == "request"

    def test_type_persisted(self, store):
        store.register_agent("s")
        store.add_message(sender="s", content="alert!", msg_type="alert")
        msgs = store.read_messages()
        assert msgs[0].msg_type == "alert"


class TestMentions:
    def test_auto_extract_mentions(self, store):
        store.register_agent("s")
        msg = store.add_message(sender="s", content="hey @alice and @bob")
        assert "alice" in msg.mentions
        assert "bob" in msg.mentions

    def test_no_mentions(self, store):
        store.register_agent("s")
        msg = store.add_message(sender="s", content="no mentions here")
        assert msg.mentions == []

    def test_mentions_persisted(self, store):
        store.register_agent("s")
        store.add_message(sender="s", content="cc @dev-team")
        msgs = store.read_messages()
        assert "dev-team" in msgs[0].mentions


class TestArtifacts:
    def test_message_with_artifacts(self, store):
        store.register_agent("s")
        msg = store.add_message(
            sender="s", content="check this file",
            artifacts=[{"type": "file", "content": "/src/main.py"}],
        )
        assert len(msg.artifacts) == 1
        assert msg.artifacts[0].type == "file"
        assert msg.artifacts[0].content == "/src/main.py"

    def test_artifacts_persisted(self, store):
        store.register_agent("s")
        store.add_message(
            sender="s", content="code review",
            artifacts=[
                {"type": "code", "content": "def hello(): pass"},
                {"type": "url", "content": "https://github.com/pr/123"},
            ],
        )
        msgs = store.read_messages()
        assert len(msgs[0].artifacts) == 2
        assert msgs[0].artifacts[0].type == "code"
        assert msgs[0].artifacts[1].type == "url"

    def test_no_artifacts_default(self, store):
        store.register_agent("s")
        msg = store.add_message(sender="s", content="plain")
        assert msg.artifacts == []


class TestThreads:
    def test_create_and_list(self, store):
        store.create_thread("debug", "agent-1")
        threads = store.list_threads()
        assert len(threads) == 1
        assert threads[0].name == "debug"
        assert threads[0].created_by == "agent-1"

    def test_duplicate_thread_ignored(self, store):
        store.create_thread("debug", "a1")
        second = store.create_thread("debug", "a2")
        threads = store.list_threads()
        assert len(threads) == 1
        assert threads[0].created_by == "a1"
        assert second.created_by == "a1"

    def test_threads_ordered_by_created_at(self, store):
        store.create_thread("a-thread", "a1")
        store.create_thread("b-thread", "a1")
        threads = store.list_threads()
        assert [t.name for t in threads] == ["a-thread", "b-thread"]


class TestThreadSummary:
    def test_empty_thread_summary(self, store):
        s = store.thread_summary("empty")
        assert s.message_count == 0
        assert s.participants == []
        assert s.first_message_at is None

    def test_summary_with_messages(self, store):
        store.register_agent("a1")
        store.register_agent("a2")
        store.add_message(sender="a1", content="first msg", thread="work")
        store.add_message(sender="a2", content="second msg", thread="work")
        store.add_message(sender="a1", content="last message here", thread="work")
        s = store.thread_summary("work")
        assert s.message_count == 3
        assert sorted(s.participants) == ["a1", "a2"]
        assert s.first_message_at is not None
        assert s.last_message_at is not None
        assert "last message here" in s.last_message_preview

    def test_summary_preview_truncated(self, store):
        store.register_agent("s")
        long_msg = "x" * 200
        store.add_message(sender="s", content=long_msg, thread="t")
        s = store.thread_summary("t")
        assert len(s.last_message_preview) <= 120


class TestSchemaMigration:
    def test_migration_adds_new_columns(self, tmp_path):
        """Test that opening an old-format DB gets migrated."""
        import sqlite3
        db = tmp_path / "old.db"
        conn = sqlite3.connect(str(db))
        # Create old schema without new columns
        conn.executescript("""
            CREATE TABLE agents (
                name TEXT PRIMARY KEY,
                role TEXT DEFAULT '',
                connected_at TEXT NOT NULL,
                last_seen TEXT NOT NULL
            );
            CREATE TABLE messages (
                id TEXT PRIMARY KEY,
                sender TEXT NOT NULL,
                recipient TEXT,
                thread TEXT DEFAULT 'general',
                content TEXT NOT NULL,
                timestamp TEXT NOT NULL
            );
            CREATE TABLE threads (
                name TEXT PRIMARY KEY,
                created_by TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
        """)
        conn.commit()
        conn.close()

        # Open with MessageStore — migration should run
        s = MessageStore(db_path=db)
        # New columns should work
        s.register_agent("test", role="r")
        s.heartbeat("test", status="busy", working_on="migrating")
        agents = s.list_agents()
        assert agents[0].status == "busy"

        s.add_message(sender="test", content="@hello", msg_type="alert",
                      artifacts=[{"type": "url", "content": "https://x.com"}])
        msgs = s.read_messages()
        assert msgs[0].msg_type == "alert"
        assert len(msgs[0].artifacts) == 1
        s.close()


class TestHTTPAPI:
    """Test HTTP API endpoints using FastAPI test client."""

    @pytest.fixture
    def client(self, store, monkeypatch):
        from agentbridge import server
        monkeypatch.setattr(server, "store", store)
        monkeypatch.setattr(server, "_auth_token", "")
        from fastapi.testclient import TestClient
        return TestClient(server.http_app)

    def test_register_and_list_agents(self, client):
        resp = client.post("/agents", json={"name": "test-agent", "role": "tester"})
        assert resp.status_code == 200
        assert resp.json()["name"] == "test-agent"

        resp = client.get("/agents")
        assert resp.status_code == 200
        names = [a["name"] for a in resp.json()]
        assert "test-agent" in names

    def test_actors_alias(self, client):
        client.post("/agents", json={"name": "actor-agent", "role": "tester"})
        resp = client.get("/actors")
        assert resp.status_code == 200
        assert any(a["id"] == "actor-agent" and a["type"] == "agent" for a in resp.json())

    def test_send_and_read_messages(self, client):
        client.post("/agents", json={"name": "s1"})
        resp = client.post("/messages", json={"sender": "s1", "content": "hello"})
        assert resp.status_code == 200
        assert resp.json()["content"] == "hello"

        resp = client.get("/messages")
        assert resp.status_code == 200
        assert len(resp.json()) == 1

    def test_threads(self, client):
        resp = client.post("/threads", json={"name": "test-thread", "created_by": "t1"})
        assert resp.status_code == 200

        resp = client.get("/threads")
        assert len(resp.json()) == 1
        assert resp.json()[0]["name"] == "test-thread"

    def test_health(self, client):
        resp = client.get("/health")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"

    def test_heartbeat_endpoint(self, client):
        client.post("/agents", json={"name": "h1"})
        resp = client.post("/agents/h1/heartbeat", json={"status": "busy", "working_on": "tests"})
        assert resp.status_code == 200
        assert resp.json()["status"] == "busy"

    def test_heartbeat_unknown_agent_returns_404(self, client):
        resp = client.post("/agents/missing/heartbeat", json={"status": "online", "working_on": ""})
        assert resp.status_code == 404
        assert resp.json()["detail"] == "Agent not found"

    def test_inbox_endpoint(self, client):
        client.post("/agents", json={"name": "s1"})
        client.post("/messages", json={"sender": "s1", "content": "to a2", "recipient": "a2"})
        client.post("/messages", json={"sender": "s1", "content": "to a3", "recipient": "a3"})
        client.post("/messages", json={"sender": "s1", "content": "broadcast"})

        resp = client.get("/messages", params={"as_agent": "a2"})
        msgs = resp.json()
        assert len(msgs) == 2  # direct + broadcast
        contents = [m["content"] for m in msgs]
        assert "to a2" in contents
        assert "broadcast" in contents
        assert "to a3" not in contents

    def test_message_types_endpoint(self, client):
        client.post("/agents", json={"name": "s1"})
        resp = client.post("/messages", json={"sender": "s1", "content": "help!", "msg_type": "request"})
        assert resp.json()["msg_type"] == "request"

    def test_invalid_message_type_rejected(self, client):
        client.post("/agents", json={"name": "s1"})
        resp = client.post("/messages", json={"sender": "s1", "content": "bad", "msg_type": "nope"})
        assert resp.status_code == 422

    def test_artifacts_endpoint(self, client):
        client.post("/agents", json={"name": "s1"})
        resp = client.post("/messages", json={
            "sender": "s1",
            "content": "check this",
            "artifacts": [{"type": "file", "content": "/src/app.py"}],
        })
        assert len(resp.json()["artifacts"]) == 1

    def test_thread_summary_endpoint(self, client):
        client.post("/agents", json={"name": "s1"})
        client.post("/messages", json={"sender": "s1", "content": "msg1", "thread": "work"})
        client.post("/messages", json={"sender": "s1", "content": "msg2", "thread": "work"})
        resp = client.get("/threads/work/summary")
        assert resp.status_code == 200
        assert resp.json()["message_count"] == 2

    def test_messages_before_filter(self, client):
        client.post("/agents", json={"name": "s1"})
        client.post("/messages", json={"sender": "s1", "content": "m1"})
        time.sleep(0.002)
        client.post("/messages", json={"sender": "s1", "content": "m2"})
        time.sleep(0.002)
        client.post("/messages", json={"sender": "s1", "content": "m3"})
        all_msgs = client.get("/messages", params={"limit": 10}).json()
        cursor = all_msgs[0]["timestamp"]
        resp = client.get("/messages", params={"before": cursor, "limit": 10})
        assert resp.status_code == 200
        assert resp.json() == []

    def test_bus_events_write_read_and_filter(self, client):
        client.post("/bus/events", json={
            "actor_id": "svc-1",
            "actor_type": "service",
            "target_id": "queue-1",
            "target_type": "queue",
            "thread": "ops",
            "event_type": "task.update",
            "content": "job started",
            "metadata": {"job_id": "j1"},
            "labels": ["ops", "start"],
        })
        client.post("/bus/events", json={
            "actor_id": "svc-2",
            "actor_type": "service",
            "thread": "ops",
            "event_type": "task.update",
            "content": "job done",
            "metadata": {"job_id": "j2"},
            "labels": ["ops", "done"],
        })

        resp = client.get("/bus/events", params={"actor_id": "svc-1"})
        assert resp.status_code == 200
        events = resp.json()
        assert len(events) == 1
        assert events[0]["content"] == "job started"
        assert events[0]["actor_type"] == "service"

    def test_bus_event_schema_validation(self, client):
        # task.update requires metadata.job_id by default schema
        resp = client.post("/bus/events", json={
            "actor_id": "svc-1",
            "actor_type": "service",
            "thread": "ops",
            "event_type": "task.update",
            "content": "missing metadata",
            "metadata": {},
            "labels": [],
        })
        assert resp.status_code == 422
        assert "job_id" in resp.json()["detail"]

    def test_register_custom_schema_and_validate(self, client):
        resp = client.post("/bus/schemas", json={
            "event_type": "build.finished",
            "schema": {
                "required": ["build_id", "ok"],
                "properties": {
                    "build_id": {"type": "string"},
                    "ok": {"type": "boolean"},
                },
            },
        })
        assert resp.status_code == 200
        assert resp.json()["ok"] is True

        bad = client.post("/bus/events", json={
            "actor_id": "ci",
            "actor_type": "service",
            "event_type": "build.finished",
            "content": "done",
            "metadata": {"build_id": "b1", "ok": "yes"},
            "labels": [],
        })
        assert bad.status_code == 422

        good = client.post("/bus/events", json={
            "actor_id": "ci",
            "actor_type": "service",
            "event_type": "build.finished",
            "content": "done",
            "metadata": {"build_id": "b1", "ok": True},
            "labels": [],
        })
        assert good.status_code == 200

    def test_wildcard_schema_applies(self, client):
        # task.* requires job_id by default
        bad = client.post("/bus/events", json={
            "actor_id": "svc-1",
            "actor_type": "service",
            "event_type": "task.created",
            "content": "created",
            "metadata": {},
            "labels": [],
        })
        assert bad.status_code == 422
        assert "job_id" in bad.json()["detail"]

        good = client.post("/bus/events", json={
            "actor_id": "svc-1",
            "actor_type": "service",
            "event_type": "task.created",
            "content": "created",
            "metadata": {"job_id": "j-1"},
            "labels": [],
        })
        assert good.status_code == 200

    def test_schema_extends_parent(self, client):
        reg = client.post("/bus/schemas", json={
            "event_type": "task.reviewed",
            "schema": {
                "extends": "task.*",
                "required": ["reviewer"],
                "properties": {
                    "reviewer": {"type": "string"},
                },
            },
        })
        assert reg.status_code == 200

        # Missing inherited job_id and own reviewer -> reject
        bad = client.post("/bus/events", json={
            "actor_id": "svc-1",
            "actor_type": "service",
            "event_type": "task.reviewed",
            "content": "reviewed",
            "metadata": {},
            "labels": [],
        })
        assert bad.status_code == 422

        ok = client.post("/bus/events", json={
            "actor_id": "svc-1",
            "actor_type": "service",
            "event_type": "task.reviewed",
            "content": "reviewed",
            "metadata": {"job_id": "j-2", "reviewer": "alice"},
            "labels": [],
        })
        assert ok.status_code == 200

    def test_dashboard_served(self, client):
        resp = client.get("/ui")
        assert resp.status_code == 200
        assert "AgentBridge" in resp.text


class TestConcurrency:
    def test_concurrent_message_writes(self, store):
        store.register_agent("sender")
        total = 200
        workers = 8
        per_worker = total // workers

        def worker(worker_id: int):
            for i in range(per_worker):
                store.add_message(sender="sender", content=f"w{worker_id}-{i}")

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(workers)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        msgs = store.read_messages(limit=500)
        assert len(msgs) == total
        ids = {m.id for m in msgs}
        assert len(ids) == total

    def test_concurrent_read_write_stability(self, store):
        store.register_agent("sender")
        writer_done = threading.Event()
        errors: list[Exception] = []

        def writer():
            try:
                for i in range(150):
                    store.add_message(sender="sender", content=f"m{i}")
                    if i % 25 == 0:
                        time.sleep(0.002)
            except Exception as e:  # pragma: no cover - failure capture path
                errors.append(e)
            finally:
                writer_done.set()

        def reader():
            try:
                while not writer_done.is_set():
                    _ = store.read_messages(limit=50)
                    time.sleep(0.001)
                _ = store.read_messages(limit=200)
            except Exception as e:  # pragma: no cover - failure capture path
                errors.append(e)

        wt = threading.Thread(target=writer)
        rt = threading.Thread(target=reader)
        wt.start()
        rt.start()
        wt.join()
        rt.join()

        assert not errors
        assert len(store.read_messages(limit=500)) == 150


class TestSSE:
    def test_broadcast_dispatches_to_subscribers(self):
        from agentbridge import server

        class ImmediateLoop:
            def call_soon_threadsafe(self, callback, *args):
                callback(*args)

        q1: asyncio.Queue[str] = asyncio.Queue(maxsize=10)
        q2: asyncio.Queue[str] = asyncio.Queue(maxsize=10)
        old = list(server._sse_subscribers)
        server._sse_subscribers[:] = [(ImmediateLoop(), q1), (ImmediateLoop(), q2)]
        try:
            server._broadcast_sse("message", {"content": "hello-sse"})
            m1 = q1.get_nowait()
            m2 = q2.get_nowait()
            assert "event: message" in m1
            assert "hello-sse" in m1
            assert "event: message" in m2
        finally:
            server._sse_subscribers[:] = old


class TestAuth:
    @pytest.fixture
    def client(self, store, monkeypatch):
        from agentbridge import server
        monkeypatch.setattr(server, "store", store)
        monkeypatch.setattr(server, "_auth_token", "secret-token")
        from fastapi.testclient import TestClient
        return TestClient(server.http_app)

    def test_health_is_public(self, client):
        resp = client.get("/health")
        assert resp.status_code == 200

    def test_other_endpoints_require_token(self, client):
        resp = client.get("/agents")
        assert resp.status_code == 401

    def test_token_header_allows_access(self, client):
        resp = client.post("/agents", json={"name": "a1"}, headers={"X-AgentBridge-Token": "secret-token"})
        assert resp.status_code == 200
        assert resp.json()["name"] == "a1"


class TestBackup:
    def test_backup_writes_db_file(self, store, tmp_path):
        store.register_agent("a1")
        store.add_message(sender="a1", content="hello")
        out = tmp_path / "backup.db"
        written = store.backup_to(out)
        assert written.exists()
        assert written.stat().st_size > 0
