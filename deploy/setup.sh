#!/usr/bin/env bash
# =============================================================================
# Bet365 Live Data Stream — Ubuntu VPS Setup
# Contabo VPS: 64GB RAM, 12 CPU cores, 350GB NVMe
# =============================================================================
set -euo pipefail

APP_DIR="/opt/bet365"
APP_USER="bet365"
PYTHON_VERSION="3.12"

echo "=== Bet365 Stream — VPS Setup ==="

# ─── System packages ─────────────────────────────────────────────────────────
echo "[1/7] Installing system dependencies..."
apt-get update -qq
apt-get install -y -qq \
    python${PYTHON_VERSION} python${PYTHON_VERSION}-venv python${PYTHON_VERSION}-dev \
    git curl wget \
    libgtk-3-0 libdbus-glib-1-2 libxt6 libasound2t64 \
    libx11-xcb1 libxcomposite1 libxdamage1 libxrandr2 \
    libpango-1.0-0 libatk1.0-0 libcairo2 libcups2 \
    libxkbcommon0 libgbm1 fonts-liberation \
    xvfb

# ─── System tuning for many browser instances ────────────────────────────────
echo "[2/7] Tuning system limits..."

# Increase open file limits (each browser needs many FDs)
cat > /etc/security/limits.d/bet365.conf << 'LIMITS'
bet365  soft  nofile  65536
bet365  hard  nofile  65536
bet365  soft  nproc   32768
bet365  hard  nproc   32768
LIMITS

# Kernel tweaks for high-concurrency browser workload
cat > /etc/sysctl.d/99-bet365.conf << 'SYSCTL'
# Shared memory for Firefox instances
kernel.shmmax = 4294967296
kernel.shmall = 4194304

# Network buffer tuning for WebSocket connections
net.core.rmem_max = 16777216
net.core.wmem_max = 16777216
net.ipv4.tcp_rmem = 4096 87380 16777216
net.ipv4.tcp_wmem = 4096 65536 16777216

# Allow more connections
net.core.somaxconn = 4096
net.ipv4.tcp_max_syn_backlog = 4096

# Faster connection recycling
net.ipv4.tcp_tw_reuse = 1
net.ipv4.tcp_fin_timeout = 15

# Virtual memory tuning (lots of browser processes)
vm.max_map_count = 262144
vm.swappiness = 10
SYSCTL

sysctl --system > /dev/null 2>&1

# ─── Create app user ─────────────────────────────────────────────────────────
echo "[3/7] Creating app user..."
if ! id "$APP_USER" &>/dev/null; then
    useradd -r -m -s /bin/bash "$APP_USER"
fi

# ─── Clone/copy project ──────────────────────────────────────────────────────
echo "[4/7] Setting up project directory..."
mkdir -p "$APP_DIR"

# Copy project files (run this on the VPS after scp/git clone)
if [ -d "/tmp/bet365_scrape" ]; then
    cp /tmp/bet365_scrape/*.py "$APP_DIR/"
    cp /tmp/bet365_scrape/requirements.txt "$APP_DIR/"
    mkdir -p "$APP_DIR/deploy"
    cp /tmp/bet365_scrape/deploy/* "$APP_DIR/deploy/" 2>/dev/null || true
else
    echo "  NOTE: Copy project files to $APP_DIR manually:"
    echo "    scp *.py requirements.txt root@YOUR_VPS:$APP_DIR/"
fi

chown -R "$APP_USER:$APP_USER" "$APP_DIR"

# ─── Python venv & dependencies ──────────────────────────────────────────────
echo "[5/7] Creating Python venv and installing packages..."
sudo -u "$APP_USER" bash << VENV
cd "$APP_DIR"
python${PYTHON_VERSION} -m venv venv
source venv/bin/activate
pip install --upgrade pip
pip install camoufox[geoip] websockets
VENV

# ─── Fetch Camoufox browser binary ───────────────────────────────────────────
echo "[6/7] Downloading Camoufox browser..."
sudo -u "$APP_USER" bash << FETCH
cd "$APP_DIR"
source venv/bin/activate
python -c "import camoufox; camoufox.sync_api.CamoufoxSync(headless=True).__enter__().__exit__(None,None,None)" 2>/dev/null || true
camoufox fetch 2>/dev/null || python -m camoufox fetch 2>/dev/null || true
FETCH

# ─── Install systemd service ─────────────────────────────────────────────────
echo "[7/7] Installing systemd service..."
cp "$(dirname "$0")/bet365.service" /etc/systemd/system/bet365.service 2>/dev/null || true
cp "$(dirname "$0")/bet365.env" /opt/bet365/.env 2>/dev/null || true
systemctl daemon-reload

echo ""
echo "=== Setup complete ==="
echo ""
echo "Next steps:"
echo "  1. Copy project files to $APP_DIR/ (if not done)"
echo "  2. Edit /opt/bet365/.env with your LTESocks proxy credentials"
echo "  3. systemctl enable --now bet365"
echo "  4. journalctl -u bet365 -f   (watch logs)"
echo "  5. WebSocket: ws://YOUR_VPS_IP:8765?token=YOUR_TOKEN"
echo ""
