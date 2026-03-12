"""Configuration constants for AgentBridge.

All module-level constants extracted from server.py.
"""
from pathlib import Path

# Agent / message TTLs (also defined in store.py — kept here for convenience)
AGENT_TTL_HOURS = 4
MESSAGE_TTL_HOURS = 24
PRUNE_INTERVAL_SEC = 300  # 5 minutes
SESSION_TTL_DAYS = 30

# Active-window default (overridable via AGENTBRIDGE_ACTIVE_WINDOW_SEC env var)
ACTIVE_WINDOW_SEC = 300

# Storage paths
BASE_DIR = Path.home() / ".agentbridge"
DB_PATH = BASE_DIR / "messages.db"
LOG_DIR = BASE_DIR / "logs"
