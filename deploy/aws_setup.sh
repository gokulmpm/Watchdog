#!/bin/bash
# ─────────────────────────────────────────────────────────
# SandMan AI Watchdog — AWS EC2 staging setup script
# Run once on a fresh Amazon Linux 2 / Ubuntu 22.04 instance
# ─────────────────────────────────────────────────────────

set -e

echo "=== 1. System packages ==="
sudo apt-get update -y
sudo apt-get install -y python3.11 python3.11-venv python3-pip nginx git

echo "=== 2. App directory ==="
sudo mkdir -p /opt/sandman
sudo chown ubuntu:ubuntu /opt/sandman

echo "=== 3. Copy app (run from your machine via scp, or git clone) ==="
# Example: scp -r . ubuntu@<EC2_IP>:/opt/sandman/
# Or:      git clone <your-repo> /opt/sandman

echo "=== 4. Python virtual environment ==="
cd /opt/sandman
python3.11 -m venv venv
source venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt

echo "=== 5. Environment variables ==="
cat > /opt/sandman/.env << 'EOF'
# Database — update with your AWS RDS endpoint
DB_HOST=your-rds-endpoint.rds.amazonaws.com
DB_PORT=3306
DB_USER=admin
DB_PASS=your-password

# Registry DB
REGISTRY_DB=sandman_dev

# Flask
FLASK_ENV=production
PORT=5055
EOF

echo "=== 6. Systemd service — Dashboard (Flask/Gunicorn) ==="
sudo tee /etc/systemd/system/sandman-web.service > /dev/null << 'EOF'
[Unit]
Description=SandMan AI Watchdog — Dashboard
After=network.target

[Service]
User=ubuntu
WorkingDirectory=/opt/sandman
EnvironmentFile=/opt/sandman/.env
ExecStart=/opt/sandman/venv/bin/gunicorn wsgi:application \
          --bind 0.0.0.0:5055 \
          --workers 2 \
          --timeout 120 \
          --log-level info \
          --access-logfile /opt/sandman/logs/access.log \
          --error-logfile /opt/sandman/logs/error.log
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

echo "=== 7. Systemd service — Background monitor ==="
sudo tee /etc/systemd/system/sandman-monitor.service > /dev/null << 'EOF'
[Unit]
Description=SandMan AI Watchdog — Background Monitor
After=network.target sandman-web.service

[Service]
User=ubuntu
WorkingDirectory=/opt/sandman
EnvironmentFile=/opt/sandman/.env
ExecStart=/opt/sandman/venv/bin/python -m watchdog.run_alert_monitor
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF

echo "=== 8. Nginx reverse proxy ==="
sudo tee /etc/nginx/sites-available/sandman > /dev/null << 'EOF'
server {
    listen 80;
    server_name _;          # Replace with your domain / EC2 public DNS

    # Allow large uploads (PDF, PPT)
    client_max_body_size 50M;

    location / {
        proxy_pass         http://127.0.0.1:5055;
        proxy_set_header   Host $host;
        proxy_set_header   X-Real-IP $remote_addr;
        proxy_set_header   X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_read_timeout 120s;

        # CORS — allow the host application to embed this UI
        add_header 'Access-Control-Allow-Origin'  '*' always;
        add_header 'Access-Control-Allow-Methods' 'GET, POST, OPTIONS' always;
        add_header 'Access-Control-Allow-Headers' 'Authorization, Content-Type' always;
    }

    location /static/ {
        alias /opt/sandman/watchdog/static/;
        expires 7d;
        add_header Cache-Control "public, immutable";
    }
}
EOF

sudo ln -sf /etc/nginx/sites-available/sandman /etc/nginx/sites-enabled/
sudo rm -f /etc/nginx/sites-enabled/default
sudo nginx -t
sudo systemctl enable nginx
sudo systemctl reload nginx

echo "=== 9. Start services ==="
mkdir -p /opt/sandman/logs
sudo systemctl daemon-reload
sudo systemctl enable sandman-web sandman-monitor
sudo systemctl start  sandman-web sandman-monitor

echo ""
echo "=== DONE ==="
echo "Dashboard: http://$(curl -s ifconfig.me):80/?user=<username>"
echo "Check logs: journalctl -u sandman-web -f"
echo "Check logs: journalctl -u sandman-monitor -f"
