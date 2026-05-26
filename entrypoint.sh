#!/bin/bash
set -e

echo "[entrypoint] Initialising Upstox token..."
python /app/init_upstox_token.py

echo "[entrypoint] Starting service: $*"
exec "$@"
