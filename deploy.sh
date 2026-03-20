#!/usr/bin/env bash
# Deploy the PipelinesService.
# Run this script from the repo directory.
#
# Usage:
#   bash deploy.sh                 # set up venv + run directly (test mode)
#   sudo bash deploy.sh --install  # install as systemd service
#   sudo bash deploy.sh --update   # git pull + restart service

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVICE_NAME="PipelinesService"

# ── Python venv + deps ──────────────────────────────────────────────────────

setup_venv() {
    if [ ! -d "$SCRIPT_DIR/venv" ]; then
        echo "==> Creating Python virtual environment..."
        python3 -m venv "$SCRIPT_DIR/venv"
    fi

    echo "==> Installing/upgrading dependencies..."
    "$SCRIPT_DIR/venv/bin/pip" install --upgrade pip -q
    "$SCRIPT_DIR/venv/bin/pip" install -r "$SCRIPT_DIR/requirements.txt" -q
}

# ── Commands ────────────────────────────────────────────────────────────────

cmd_test() {
    setup_venv
    echo "==> Starting service directly (Ctrl+C to stop)..."
    echo ""
    cd "$SCRIPT_DIR"
    "$SCRIPT_DIR/venv/bin/python" main.py
}

cmd_install() {
    setup_venv

    echo "==> Installing systemd service..."
    sed "s|WorkingDirectory=.*|WorkingDirectory=${SCRIPT_DIR}|; s|ExecStart=.*|ExecStart=${SCRIPT_DIR}/venv/bin/python main.py|" \
        "$SCRIPT_DIR/PipelinesService.service" > /etc/systemd/system/"${SERVICE_NAME}.service"

    systemctl daemon-reload
    systemctl enable "$SERVICE_NAME"
    systemctl start "$SERVICE_NAME"

    echo "==> Done. Status:"
    systemctl status "$SERVICE_NAME" --no-pager
    echo ""
    echo "Logs: journalctl -u ${SERVICE_NAME} -f"
}

cmd_update() {
    echo "==> Pulling latest code..."
    git -C "$SCRIPT_DIR" pull

    setup_venv

    echo "==> Restarting ${SERVICE_NAME}..."
    systemctl restart "$SERVICE_NAME"

    echo "==> Done. Status:"
    systemctl status "$SERVICE_NAME" --no-pager
}

# ── Entrypoint ──────────────────────────────────────────────────────────────

case "${1:-}" in
    --install)
        cmd_install
        ;;
    --update)
        cmd_update
        ;;
    *)
        cmd_test
        ;;
esac
