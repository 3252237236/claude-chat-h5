#!/bin/bash
set -e

echo "=========================================="
echo "  HZ Lab - Starting Services"
echo "=========================================="

# Set vibe-coding defaults (override with Railway env vars)
export VIBE_PORT=3000
export VIBE_WORK_DIR=/app
export VIBE_PASSWORD="${VIBE_PASSWORD:-vibe123}"

# Install Node.js dependencies for vibe-coding
echo "[1/5] Installing vibe-coding dependencies..."
cd /app/vibe-coding
npm install --production 2>&1 | tail -3
cd /app

# Generate Nginx config from template (replace ${PORT})
echo "[2/5] Configuring Nginx..."
envsubst '${PORT}' < /app/nginx.conf > /etc/nginx/nginx.conf

# Start Nginx
echo "[3/5] Starting Nginx on port ${PORT}..."
nginx

# Start Vibe Coding Node.js server
echo "[4/5] Starting Vibe Coding server on port 3000..."
cd /app/vibe-coding
node server.js &
VIBE_PID=$!
cd /app

# Start Flask with gunicorn
echo "[5/5] Starting Flask on port 5000..."
exec gunicorn server:app --bind 0.0.0.0:5000 --workers 2 --timeout 120
