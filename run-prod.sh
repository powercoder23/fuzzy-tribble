#!/bin/bash

echo "============================"
echo "Stopping Existing Containers"
echo "============================"
docker compose -f docker-compose.prod.yml down

echo ""
echo "============================"
echo "Building Docker Image"
echo "============================"
docker compose -f docker-compose.prod.yml build --no-cache

echo ""
echo "============================"
echo "Starting Containers"
echo "============================"
docker compose -f docker-compose.prod.yml up -d

echo ""
echo "============================"
echo "Showing Logs"
echo "============================"
docker logs -f discount-scanner