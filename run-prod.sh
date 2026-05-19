#!/bin/bash
set -e

echo "============================"
echo "Stopping Existing Containers"
echo "============================"
docker compose -f docker-compose.prod.yml down

echo ""
echo "============================"
echo "Relocating IV History DB"
echo "============================"
# The IV DB now lives in the shared data/ volume (data/iv_history.db) instead of
# the repo root. Migrate any legacy root copy once — skip if already migrated.
mkdir -p data
if [ -f iv_history.db ]; then
    if [ -f data/iv_history.db ]; then
        echo "data/iv_history.db already exists — leaving legacy root copy untouched"
    else
        mv iv_history.db data/iv_history.db
        echo "Moved iv_history.db -> data/iv_history.db"
    fi
else
    echo "No legacy root iv_history.db — nothing to migrate"
fi

echo ""
echo "============================"
echo "Building Docker Image"
echo "============================"
docker compose -f docker-compose.prod.yml build

echo ""
echo "============================"
echo "Starting Containers"
echo "============================"
docker compose -f docker-compose.prod.yml up -d

echo ""
echo "============================"
echo "Showing Logs"
echo "============================"
docker logs -f discount-strategy
