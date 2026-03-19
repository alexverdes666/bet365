# Deployment Guide — Contabo VPS (64GB / 12 CPU / 350GB NVMe)

## 1. Get a UK Proxy from LTESocks

You need a UK mobile proxy so bet365 serves English content.

```bash
# 1. Get your API token from https://ltesocks.io
# 2. List available UK plans:
curl -H "Authorization: YOUR_API_TOKEN" https://api.ltesocks.io/v2/plans | jq '.plans[] | select(.countryCode=="GB")'

# 3. Order a UK port:
curl -X POST -H "Authorization: YOUR_API_TOKEN" \
     -H "Content-Type: application/json" \
     -d '{"plan": "PLAN_ID_FROM_STEP_2", "tarification": {"time": 2592000, "traffic": 51200}}' \
     https://api.ltesocks.io/v2/ports/order

# 4. Set credentials on your port (password auth):
curl -X POST -H "Authorization: YOUR_API_TOKEN" \
     -H "Content-Type: application/json" \
     -d '{"password": [{"login": "myuser", "password": "mypass"}]}' \
     https://api.ltesocks.io/v2/ports/PORT_ID/credentials

# 5. Get proxy servers:
curl -H "Authorization: YOUR_API_TOKEN" https://api.ltesocks.io/v2/servers
# Use a proxyServer host (e.g., ap1.ltesocks.io)

# Your proxy URL will be:
# socks5://myuser:mypass@ap1.ltesocks.io:PORT_NUMBER
```

## 2. Upload Code to VPS

```bash
# From your local machine:
scp -r *.py requirements.txt deploy/ root@YOUR_VPS_IP:/tmp/bet365_scrape/
```

## 3. Run Setup

```bash
# SSH into your VPS:
ssh root@YOUR_VPS_IP

# Run setup script:
chmod +x /tmp/bet365_scrape/deploy/setup.sh
bash /tmp/bet365_scrape/deploy/setup.sh
```

## 4. Configure

```bash
# Edit the environment file:
nano /opt/bet365/.env

# Set your proxy URL:
# PROXY_URL=socks5://myuser:mypass@ap1.ltesocks.io:12345

# Set a secure WebSocket token:
# WS_TOKEN=your_random_secure_token_here
```

## 5. Start

```bash
# Create log dir
mkdir -p /opt/bet365/logs && chown bet365:bet365 /opt/bet365/logs

# Start service
systemctl enable --now bet365

# Watch logs
journalctl -u bet365 -f

# Check JSONL output
tail -f /opt/bet365/logs/stream.jsonl
```

## 6. Connect Clients

```
WebSocket URL: ws://YOUR_VPS_IP:8765?token=YOUR_TOKEN
```

## Resource Estimates (64GB RAM)

| Events | Tabs/Browser | Browsers | Est. RAM  | Status |
|--------|-------------|----------|-----------|--------|
| 50     | 8           | ~7       | ~4 GB     | Easy   |
| 100    | 8           | ~13      | ~8 GB     | Easy   |
| 150    | 8           | ~19      | ~12 GB    | Fine   |
| 200    | 8           | ~25      | ~16 GB    | Fine   |
| 300    | 8           | ~38      | ~24 GB    | OK     |

With 64GB you have massive headroom. The defaults (8 tabs/browser, 10 concurrency) are optimized for your hardware.

## Useful Commands

```bash
# Service management
systemctl status bet365
systemctl restart bet365
systemctl stop bet365

# Live logs
journalctl -u bet365 -f
journalctl -u bet365 --since "10 min ago"

# Monitor resource usage
htop
watch -n 5 'ps aux --sort=-%mem | head -20'

# Check WebSocket server
curl -i -N -H "Connection: Upgrade" -H "Upgrade: websocket" \
     http://localhost:8765/

# Log rotation (install once)
sudo cp /opt/bet365/deploy/logrotate.conf /etc/logrotate.d/bet365
```

## Firewall

```bash
# Allow WebSocket port from specific IPs only
ufw allow 22/tcp
ufw allow 8765/tcp
ufw enable
```
