#!/bin/bash
# AgentBridge Server Launcher with Auto-Restart

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_PATH="$SCRIPT_DIR/.venv"
LOG_FILE="$SCRIPT_DIR/.agentbridge/logs/agentbridge_server.log"
PID_FILE="/tmp/agentbridge.pid"

# Colors
GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

echo -e "${GREEN}🚀 AgentBridge Server Launcher${NC}"
echo "Log: $LOG_FILE"
echo "PID: $PID_FILE"

# Create log directory
mkdir -p "$(dirname "$LOG_FILE")"

# Activate venv
source "$VENV_PATH/bin/activate"

# Function to start server
start_server() {
    echo -e "${GREEN}Starting AgentBridge server...${NC}"
    python -m agentbridge serve \
        --host 127.0.0.1 \
        --port 7890 \
        2>&1 | tee -a "$LOG_FILE" &

    SERVER_PID=$!
    echo $SERVER_PID > "$PID_FILE"

    echo -e "${GREEN}✅ Server started (PID: $SERVER_PID)${NC}"
    echo "   HTTP:      http://localhost:7890"
    echo "   WebSocket: ws://localhost:7890/ws"
    echo "   Dashboard: http://localhost:7890/ui"
    echo ""

    # Wait for process
    wait $SERVER_PID

    # If we get here, process exited
    echo -e "${YELLOW}⚠️  Server stopped (PID: $SERVER_PID)${NC}"
    return $?
}

# Clean up on exit
cleanup() {
    echo -e "${YELLOW}Shutting down...${NC}"
    if [ -f "$PID_FILE" ]; then
        PID=$(cat "$PID_FILE")
        if kill -0 $PID 2>/dev/null; then
            kill $PID 2>/dev/null || true
        fi
        rm "$PID_FILE"
    fi
    echo -e "${RED}Server stopped${NC}"
}

trap cleanup EXIT INT TERM

# Start server with auto-restart
RESTART_COUNT=0
MAX_RESTARTS=100

while [ $RESTART_COUNT -lt $MAX_RESTARTS ]; do
    start_server
    EXIT_CODE=$?

    RESTART_COUNT=$((RESTART_COUNT + 1))

    if [ $EXIT_CODE -eq 0 ]; then
        echo -e "${RED}Server exited cleanly${NC}"
        break
    else
        echo -e "${YELLOW}Server crashed (exit code: $EXIT_CODE, restart #$RESTART_COUNT)${NC}"
        echo "Restarting in 3 seconds..."
        sleep 3
    fi
done

if [ $RESTART_COUNT -ge $MAX_RESTARTS ]; then
    echo -e "${RED}❌ Max restart attempts reached ($MAX_RESTARTS)${NC}"
    exit 1
fi
