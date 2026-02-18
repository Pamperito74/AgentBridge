"""Tests for AgentBridge core functionality."""

import tempfile
import pytest
from pathlib import Path

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


class TestThreads:
    def test_create_and_list(self, store):
        store.create_thread("debug", "agent-1")
        threads = store.list_threads()
        assert len(threads) == 1
        assert threads[0].name == "debug"
        assert threads[0].created_by == "agent-1"

    def test_duplicate_thread_ignored(self, store):
        store.create_thread("debug", "a1")
        store.create_thread("debug", "a2")
        threads = store.list_threads()
        assert len(threads) == 1


class TestHTTPAPI:
    """Test HTTP API endpoints using FastAPI test client."""

    @pytest.fixture
    def client(self, store, monkeypatch):
        from agentbridge import server
        monkeypatch.setattr(server, "store", store)
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
