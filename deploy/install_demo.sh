#!/usr/bin/env bash
set -euo pipefail

if [[ "${EUID}" -ne 0 ]]; then
    echo "Please run with sudo: sudo bash deploy/install_demo.sh ..." >&2
    exit 1
fi

SERVER_NAME=""
PUBLIC_ORIGIN=""
LETSENCRYPT_EMAIL=""
APP_DIR="/opt/agent-platform"

while [[ $# -gt 0 ]]; do
    case "$1" in
        --server-name) SERVER_NAME="${2:-}"; shift 2 ;;
        --public-origin) PUBLIC_ORIGIN="${2:-}"; shift 2 ;;
        --letsencrypt-email) LETSENCRYPT_EMAIL="${2:-}"; shift 2 ;;
        --app-dir) APP_DIR="${2:-}"; shift 2 ;;
        *) echo "Unknown argument: $1" >&2; exit 2 ;;
    esac
done

if [[ ! "$SERVER_NAME" =~ ^[A-Za-z0-9.-]+$ ]]; then
    echo "--server-name must be an IP address or domain name" >&2
    exit 2
fi
if [[ ! "$PUBLIC_ORIGIN" =~ ^https?://[A-Za-z0-9.:-]+$ ]]; then
    echo "--public-origin must look like http://IP or https://domain" >&2
    exit 2
fi
if [[ ! -f "./compose.yaml" || ! -f "./pyproject.toml" ]]; then
    echo "Run this script from the project root directory" >&2
    exit 2
fi
if [[ "$PUBLIC_ORIGIN" == https://* && -z "$LETSENCRYPT_EMAIL" ]]; then
    echo "HTTPS deployment requires --letsencrypt-email" >&2
    exit 2
fi

export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y ca-certificates curl gnupg nginx sqlite3 rsync openssl

if ! command -v docker >/dev/null 2>&1; then
    install -m 0755 -d /etc/apt/keyrings
    curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
    chmod a+r /etc/apt/keyrings/docker.asc
    . /etc/os-release
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu ${VERSION_CODENAME} stable" > /etc/apt/sources.list.d/docker.list
    apt-get update
    apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
fi

SOURCE_DIR="$(pwd)"
if [[ "$SOURCE_DIR" != "$APP_DIR" ]]; then
    install -d -m 0755 "$APP_DIR"
    rsync -a --delete \
        --exclude '.git/' --exclude '.venv*/' --exclude '.env' \
        --exclude '.env.production' --exclude '.pytest_artifacts/' \
        --exclude 'data/*.sqlite3*' --exclude 'data/*.db*' \
        "$SOURCE_DIR/" "$APP_DIR/"
fi

cd "$APP_DIR"
install -d -m 0750 data
chown -R 10001:10001 data

if [[ ! -f .env.production ]]; then
    AUTH_SECRET="$(openssl rand -hex 32)"
    cat > .env.production <<EOF
APP_ENV=production
PYTHON_BASE_IMAGE=docker.m.daocloud.io/library/python:3.11.9-slim-bookworm
PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple
APT_MIRROR_HOST=mirrors.aliyun.com
AUTH_ENABLED=true
AUTH_SECRET=$AUTH_SECRET
AUTH_TOKEN_TTL_S=28800
AUTH_REGISTRATION_ENABLED=true
ALLOWED_ORIGINS=$PUBLIC_ORIGIN
TRUSTED_HOSTS=$SERVER_NAME,127.0.0.1,localhost
SQLITE_PATH=data/app.sqlite3
LANGGRAPH_USE_MEMORY_SAVER=false
LLM_PROVIDER=mock
DEEPSEEK_API_KEY=
DEEPSEEK_MODEL=deepseek-chat
MARKET_DATA_PROVIDER=akshare
PAPER_MONITOR_ENABLED=false
PAPER_MONITOR_POLL_INTERVAL_S=30
EOF
    chmod 0600 .env.production
fi

sed "s/__SERVER_NAME__/$SERVER_NAME/g" deploy/nginx-agent-platform.conf.template \
    > /etc/nginx/sites-available/agent-platform
ln -sfn /etc/nginx/sites-available/agent-platform /etc/nginx/sites-enabled/agent-platform
rm -f /etc/nginx/sites-enabled/default
nginx -t
systemctl enable --now nginx docker

docker compose --env-file .env.production up -d --build

for _ in $(seq 1 30); do
    if curl -fsS http://127.0.0.1:8003/ready >/dev/null; then
        break
    fi
    sleep 2
done
curl -fsS http://127.0.0.1:8003/ready >/dev/null
systemctl reload nginx

install -m 0750 deploy/backup_sqlite.sh /usr/local/sbin/agent-platform-backup
cat > /etc/cron.d/agent-platform-backup <<EOF
17 3 * * * root APP_DIR=$APP_DIR /usr/local/sbin/agent-platform-backup >> /var/log/agent-platform-backup.log 2>&1
EOF
chmod 0644 /etc/cron.d/agent-platform-backup

if [[ "$PUBLIC_ORIGIN" == https://* ]]; then
    apt-get install -y certbot python3-certbot-nginx
    certbot --nginx --non-interactive --agree-tos \
        --redirect --email "$LETSENCRYPT_EMAIL" -d "$SERVER_NAME"
fi

echo "Deployment completed: $PUBLIC_ORIGIN"
echo "Check: curl -fsS $PUBLIC_ORIGIN/ready"
