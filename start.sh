#!/bin/bash
set -e

echo "=========================================="
echo "  HZ Lab - Starting Services"
echo "=========================================="

# Set vibe-coding defaults (override with Railway env vars)
export VIBE_PORT=3000
export VIBE_WORK_DIR=/app
export VIBE_PASSWORD="${VIBE_PASSWORD:-vibe123}"

# Get PORT from environment (Railway sets this)
PORT="${PORT:-8080}"

# Install Node.js dependencies for vibe-coding
echo "[1/5] Installing vibe-coding dependencies..."
cd /app/vibe-coding
npm install --production 2>&1 | tail -3
cd /app

# Generate Nginx config - replace __PORT__ with actual port
echo "[2/5] Configuring Nginx (port ${PORT})..."
sed "s/__PORT__/${PORT}/g" /app/nginx.conf > /etc/nginx/nginx.conf

# Verify config
cat /etc/nginx/nginx.conf | head -30

# Start Nginx
echo "[3/5] Starting Nginx..."
nginx -t && nginx

# Start Vibe Coding Node.js server
echo "[4/5] Starting Vibe Coding server on port 3000..."
cd /app/vibe-coding
node server.js &
VIBE_PID=$!
cd /app

echo "[5/5] Starting Flask on port 5000..."
exec gunicorn server:app --bind 0.0.0.0:5000 --workers 2 --timeout 120
