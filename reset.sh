#!/bin/bash
# ==============================================================================
#
#         Seraph — Full Environment Reset
#
# ==============================================================================
#
# Stops all containers, prunes Docker resources, removes persistent data
# (database, memories, soul file, logs), and restarts fresh.
# Use this to test onboarding from scratch.
#
# Usage:
#   ./reset.sh          # Reset dev environment (default)
#   ./reset.sh prod     # Reset prod environment
#   ./reset.sh --help   # Show this help
#
# What gets deleted:
#   - Docker containers, images, volumes, and networks (project-scoped prune)
#   - SQLite database (seraph.db)
#   - LanceDB vector store (lance/)
#   - Soul file (soul.md)
#   - Google OAuth tokens
#   - Backend logs
#
# What is NOT deleted:
#   - Source code and configuration
#   - .env files
#   - Daemon venv (daemon/.venv)
#   - Docker resources from other projects
#   - Browser localStorage (clear manually: localStorage.clear() in DevTools)
#
# ==============================================================================

set -euo pipefail

ENV="${1:-dev}"

if [[ "$ENV" == "--help" || "$ENV" == "-h" ]]; then
    head -33 "$0" | grep '^#' | sed 's/^# \?//'
    exit 0
fi

if [[ "$ENV" != "dev" && "$ENV" != "prod" ]]; then
    echo "Error: Invalid environment '$ENV'. Use 'dev' or 'prod'."
    exit 1
fi

DATA_DIR="./docker-data/$ENV/backend/data"
LOGS_DIR="./docker-data/$ENV/backend/logs"
COMPOSE_FILE="docker-compose.$ENV.yaml"
ENV_FILE=".env.$ENV"

echo "=== Seraph Full Reset ($ENV) ==="
echo ""
echo "This will destroy ALL persistent data:"
echo "  - Docker containers, images, and volumes for this project"
echo "  - Database (sessions, messages, goals, user profile)"
echo "  - Memory store (embeddings, consolidated memories)"
echo "  - Soul file (personality/identity)"
echo "  - OAuth tokens (Google Calendar/Gmail)"
echo "  - Logs"
echo ""

read -p "Are you sure? (y/N) " -n 1 -r
echo ""

if [[ ! $REPLY =~ ^[Yy]$ ]]; then
    echo "Aborted."
    exit 0
fi

echo ""

# 1. Stop containers and remove volumes
echo "Stopping containers and removing volumes..."
docker compose --env-file "$ENV_FILE" -f "$COMPOSE_FILE" down --volumes --remove-orphans 2>/dev/null || true

# 2. Prune project images (rebuild from scratch)
echo "Pruning project Docker images..."
docker compose --env-file "$ENV_FILE" -f "$COMPOSE_FILE" down --rmi local 2>/dev/null || true

# 3. Remove dangling Docker resources
echo "Removing dangling Docker resources..."
docker system prune -f 2>/dev/null || true

# 4. Remove persistent data on host
if [ -d "$DATA_DIR" ]; then
    echo "Removing data: $DATA_DIR"
    rm -rf "$DATA_DIR"
else
    echo "No data directory found at $DATA_DIR (already clean)"
fi

if [ -d "$LOGS_DIR" ]; then
    echo "Removing logs: $LOGS_DIR"
    rm -rf "$LOGS_DIR"
else
    echo "No logs directory found at $LOGS_DIR (already clean)"
fi

# 5. Rebuild and restart
echo ""
echo "Rebuilding and starting fresh containers..."
./manage.sh -e "$ENV" up -d --build

echo ""
echo "=== Reset complete ==="
echo ""
echo "Backend will initialize a fresh database on startup."
echo ""
echo "Remember to also clear browser localStorage if needed:"
echo "  Open DevTools console → localStorage.clear()"
