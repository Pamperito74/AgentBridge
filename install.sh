#!/usr/bin/env bash
set -euo pipefail

# AgentBridge — one-command installer
# Usage: curl -fsSL https://raw.githubusercontent.com/Pamperito74/AgentBridge/main/install.sh | bash

REPO="Pamperito74/AgentBridge"
COMPOSE_FILE="docker-compose.yml"
ENV_FILE=".env"

echo "==> AgentBridge installer"

# Check dependencies
command -v docker >/dev/null 2>&1 || { echo "Error: docker is required but not installed."; exit 1; }
command -v docker compose >/dev/null 2>&1 || docker compose version >/dev/null 2>&1 || \
  { echo "Error: docker compose (v2) is required."; exit 1; }

# Create working directory
INSTALL_DIR="${AGENTBRIDGE_DIR:-./agentbridge}"
mkdir -p "$INSTALL_DIR"
cd "$INSTALL_DIR"

echo "==> Downloading docker-compose.yml..."
curl -fsSL "https://raw.githubusercontent.com/$REPO/main/$COMPOSE_FILE" -o "$COMPOSE_FILE"

# Generate .env if it doesn't exist
if [ ! -f "$ENV_FILE" ]; then
  echo "==> Generating .env..."
  TOKEN=$(LC_ALL=C tr -dc 'A-Za-z0-9' </dev/urandom | head -c 48 || true)
  cat > "$ENV_FILE" <<EOF
# AgentBridge configuration
AGENTBRIDGE_TOKEN=${TOKEN}
PORT=4000
NODE_ENV=production
EOF
  echo "   Token saved to $ENV_FILE — keep it secret!"
else
  echo "==> .env already exists, skipping generation."
fi

echo "==> Pulling and starting AgentBridge..."
docker compose pull 2>/dev/null || true
docker compose up -d

echo ""
echo "AgentBridge is running at http://localhost:4000"
echo ""
echo "Your API token:"
grep AGENTBRIDGE_TOKEN "$ENV_FILE" | cut -d= -f2
echo ""
echo "To stop: docker compose down"
echo "To view logs: docker compose logs -f"
