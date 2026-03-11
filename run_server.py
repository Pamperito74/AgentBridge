#!/usr/bin/env python3
"""
AgentBridge Server Runner with Auto-Restart
Keeps the WebSocket + HTTP server running with automatic restart on crash
"""

import os
import re
import subprocess
import sys
import time
import signal
from pathlib import Path

# Configuration
HOST = "127.0.0.1"
PORT = 7890
MAX_RESTARTS = 100
RESTART_DELAY = 3

def get_script_dir():
    """Get the directory of this script"""
    return Path(__file__).parent.absolute()


def load_dotenv(script_dir: Path) -> dict:
    """Load .env file and shell profile files into a copy of the environment."""
    env = os.environ.copy()

    def _parse_file(path: Path):
        if not path.exists():
            return
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                m = re.match(r'^export\s+(\w+)=["\']?([^"\']*)["\']?$', line) or \
                    re.match(r'^(\w+)=["\']?([^"\']*)["\']?$', line)
                if m:
                    env[m.group(1)] = m.group(2)

    # Load shell profiles first (lowest priority)
    home = Path.home()
    for profile in [home / ".zprofile", home / ".bash_profile", home / ".profile", home / ".zshrc", home / ".bashrc"]:
        _parse_file(profile)

    # .env overrides shell profiles
    _parse_file(script_dir / ".env")

    return env

def get_log_file():
    """Get log file path"""
    log_dir = get_script_dir() / ".agentbridge" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    return log_dir / "agentbridge_server.log"

def print_header():
    """Print startup header"""
    print("\n" + "=" * 60)
    print("🚀 AgentBridge Server (with Auto-Restart)")
    print("=" * 60)
    print(f"Host:      {HOST}")
    print(f"Port:      {PORT}")
    print(f"HTTP:      http://{HOST}:{PORT}")
    print(f"WebSocket: ws://{HOST}:{PORT}/ws")
    print(f"Dashboard: http://{HOST}:{PORT}/ui")
    print(f"Log:       {get_log_file()}")
    print("=" * 60 + "\n")

def run_server():
    """Start the AgentBridge server"""
    script_dir = get_script_dir()
    log_file = get_log_file()
    env = load_dotenv(script_dir)

    # Append to log file
    with open(log_file, 'a') as f:
        f.write(f"\n{'='*60}\n")
        f.write(f"Server starting at {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
        f.write(f"{'='*60}\n")

    # Build command
    cmd = [
        sys.executable,
        "-m", "agentbridge",
        "serve",
        "--host", HOST,
        "--port", str(PORT),
    ]

    print(f"▶️  Starting: {' '.join(cmd)}")

    try:
        # Run server process
        with open(log_file, 'a') as f:
            process = subprocess.Popen(
                cmd,
                cwd=str(script_dir),
                stdout=f,
                stderr=subprocess.STDOUT,
                text=True,
                env=env,
            )

        # Wait for it
        return_code = process.wait()

        print(f"⚠️  Server exited with code {return_code}")
        return return_code

    except KeyboardInterrupt:
        print("\n⏹️  Interrupted by user")
        return 0
    except Exception as e:
        print(f"❌ Error: {e}")
        return 1

def main():
    """Main entry point"""
    print_header()

    restart_count = 0

    while restart_count < MAX_RESTARTS:
        try:
            exit_code = run_server()

            if exit_code == 0:
                print("✅ Server exited cleanly")
                break

            # Server crashed, restart
            restart_count += 1
            print(f"\n⏳ Restarting server (attempt {restart_count}/{MAX_RESTARTS})...")
            print(f"   Waiting {RESTART_DELAY} seconds...\n")

            time.sleep(RESTART_DELAY)

        except KeyboardInterrupt:
            print("\n\n✋ Shutting down...")
            break

    if restart_count >= MAX_RESTARTS:
        print(f"\n❌ Max restart attempts reached ({MAX_RESTARTS})")
        sys.exit(1)

if __name__ == "__main__":
    main()
