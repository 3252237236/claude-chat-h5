#!/bin/bash
set -e

echo "=========================================="
echo "  HZ Lab - Starting Services"
echo "=========================================="

# Set vibe-coding defaults
export VIBE_PORT=3000
export VIBE_WORK_DIR=/app
export VIBE_PASSWORD="${VIBE_PASSWORD:-vibe123}"

# Get PORT from environment (Railway sets this)
PORT="${PORT:-8080}"
echo "Using PORT: ${PORT}"

# Generate Nginx config - replace __PORT__ with actual port
echo "[1/5] Configuring Nginx..."
sed "s/__PORT__/${PORT}/g" /app/nginx.conf > /etc/nginx/nginx.conf
echo "Nginx config generated:"
cat /etc/nginx/nginx.conf | head -30

# Test Nginx config
echo "[2/5] Testing Nginx config..."
nginx -t

# Start Nginx
echo "[3/5] Starting Nginx on port ${PORT}..."
nginx
echo "Nginx started successfully"

# Start Vibe Coding Node.js server
echo "[4/5] Starting Vibe Coding server on port 3000..."
cd /app/vibe-coding
node server.js &
VIBE_PID=$!
echo "Vibe Coding server started (PID: ${VIBE_PID})"
cd /app

# Start Flask with gunicorn
echo "[5/5] Starting Flask on port 5000..."
exec gunicorn server:app --bind 0.0.0.0:5000 --workers 2 --timeout 120
