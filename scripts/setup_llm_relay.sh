#!/bin/bash
# =============================================================================
# LM Studio Relay Setup
# =============================================================================
#
# What this does:
#   Your LM Studio runs on your Windows PC (via Tailscale at 100.104.116.6:1234).
#   Docker containers on the VPS can't reach Tailscale IPs directly.
#   This script sets up socat to forward VPS port 11234 -> your PC's LM Studio.
#
# How it works:
#   Docker container -> 172.17.0.1:11234 -> socat -> Tailscale -> your PC:1234
#
# Usage:
#   sudo bash scripts/setup_llm_relay.sh
#
# After running this:
#   - socat runs as a systemd service (starts on boot)
#   - Docker containers can reach LM Studio at http://172.17.0.1:11234/v1
#
# =============================================================================

set -e

# --- Configuration ---
TAILSCALE_IP="100.104.116.6"   # Your PC's Tailscale IP
LM_STUDIO_PORT="1234"          # LM Studio's port on your PC
RELAY_PORT="11234"             # Port socat listens on (on the VPS)

echo "=== LM Studio Relay Setup ==="
echo ""
echo "  Your PC (Tailscale):  $TAILSCALE_IP:$LM_STUDIO_PORT"
echo "  VPS relay port:       $RELAY_PORT"
echo ""

# --- Step 1: Install socat if needed ---
if ! command -v socat &> /dev/null; then
    echo "[1/3] Installing socat..."
    apt-get update -qq && apt-get install -y -qq socat
else
    echo "[1/3] socat already installed"
fi

# --- Step 2: Create systemd service ---
echo "[2/3] Creating systemd service..."

cat > /etc/systemd/system/llm-relay.service << EOF
[Unit]
Description=LM Studio Relay (socat: port $RELAY_PORT -> Tailscale $TAILSCALE_IP:$LM_STUDIO_PORT)
After=network.target tailscaled.service
Wants=tailscaled.service

[Service]
Type=simple
ExecStart=/usr/bin/socat TCP-LISTEN:$RELAY_PORT,bind=0.0.0.0,fork,reuseaddr TCP:$TAILSCALE_IP:$LM_STUDIO_PORT
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

# --- Step 3: Enable and start the service ---
echo "[3/3] Starting relay service..."
systemctl daemon-reload
systemctl enable llm-relay.service
systemctl restart llm-relay.service

echo ""
echo "=== Done! ==="
echo ""
echo "  Service status:  sudo systemctl status llm-relay"
echo "  Test it:         curl http://localhost:$RELAY_PORT/v1/models"
echo "  View logs:       sudo journalctl -u llm-relay -f"
echo ""
