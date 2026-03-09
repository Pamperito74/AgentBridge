#!/usr/bin/env python3
"""
AgentBridge — All-in-One Launcher
===================================
Starts the AgentBridge server in a background thread and the task executor
in the foreground. One command, one Ctrl-C to stop everything.

Usage
-----
  python start.py

  # Optional env vars (same as running each component separately)
  AGENTBRIDGE_URL=http://localhost:7890   (default)
  EXECUTOR_NAME=task-executor            (default)
  EXECUTOR_MAX_WORKERS=4                 (default)
  EXECUTOR_POLL_INTERVAL=3               (default)
  AIDER_MODEL=ollama/qwen2.5-coder:7b   (default)
"""

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

HOST = "127.0.0.1"
PORT = int(os.environ.get("AGENTBRIDGE_PORT", "7890"))
HEALTH_URL = f"http://localhost:{PORT}/health"
READY_TIMEOUT = 15  # seconds to wait for server to become healthy

_server_proc: subprocess.Popen | None = None


# ---------------------------------------------------------------------------
# Server
# ---------------------------------------------------------------------------

def _start_server() -> subprocess.Popen:
    """Launch `python -m agentbridge serve` and stream its output."""
    cmd = [
        sys.executable, "-m", "agentbridge", "serve",
        "--host", HOST, "--port", str(PORT),
    ]
    print(f"[launcher] Starting server: {' '.join(cmd)}")
    return subprocess.Popen(cmd, cwd=str(Path(__file__).parent))


def _wait_for_server(timeout: int = READY_TIMEOUT) -> bool:
    """Poll /health until the server responds or timeout expires."""
    import urllib.request
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            urllib.request.urlopen(HEALTH_URL, timeout=2)
            return True
        except Exception:
            time.sleep(0.5)
    return False


# ---------------------------------------------------------------------------
# Executor (imported directly — runs in this process)
# ---------------------------------------------------------------------------

def _run_executor() -> None:
    """Import and run the task executor main loop in this process."""
    # Make connectors/ importable
    connectors_dir = str(Path(__file__).parent / "connectors")
    if connectors_dir not in sys.path:
        sys.path.insert(0, connectors_dir)

    import task_executor  # noqa: PLC0415
    task_executor.main()


# ---------------------------------------------------------------------------
# Shutdown
# ---------------------------------------------------------------------------

def _shutdown(signum=None, frame=None) -> None:
    global _server_proc
    if _server_proc and _server_proc.poll() is None:
        print("\n[launcher] Stopping server...")
        _server_proc.terminate()
        try:
            _server_proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            _server_proc.kill()
    print("[launcher] Done.")
    sys.exit(0)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    global _server_proc

    print("=" * 56)
    print("  AgentBridge + Task Executor")
    print(f"  Server:    http://{HOST}:{PORT}")
    print(f"  Dashboard: http://{HOST}:{PORT}/ui")
    print("  Ctrl-C to stop everything")
    print("=" * 56 + "\n")

    # Graceful shutdown on SIGINT / SIGTERM
    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    # Start the AgentBridge server
    _server_proc = _start_server()

    # Wait until it's healthy
    print(f"[launcher] Waiting for server to be ready (up to {READY_TIMEOUT}s)...")
    if not _wait_for_server():
        print(f"[launcher] ERROR: server did not become healthy within {READY_TIMEOUT}s")
        _server_proc.terminate()
        sys.exit(1)
    print(f"[launcher] Server is ready at http://{HOST}:{PORT}\n")

    # Run the executor in the foreground (blocks until Ctrl-C)
    try:
        _run_executor()
    except KeyboardInterrupt:
        pass
    finally:
        _shutdown()


if __name__ == "__main__":
    main()
