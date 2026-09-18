#!/usr/bin/env bash
set -Eeuo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$APP_DIR"

if [[ "${1:-}" == "--install-service" ]]; then
    if [[ "$(id -u)" -ne 0 ]]; then
        echo "Для установки systemd-сервиса запустите: sudo ./start.sh --install-service"
        exit 1
    fi

    if [[ ! -f .env ]]; then
        read -r -p "Введите Telegram ID администратора: " ADMIN_ID_INPUT
        read -r -s -p "Введите токен Telegram-бота: " BOT_TOKEN_INPUT
        echo
        umask 077
        cat > .env <<EOF
BOT_TOKEN=${BOT_TOKEN_INPUT}
ADMIN_ID=${ADMIN_ID_INPUT}
LOG_LEVEL=INFO
EOF
    fi

    SERVICE_FILE="/etc/systemd/system/butovskyhost.service"
    sed \
        -e "s|__APP_DIR__|$APP_DIR|g" \
        -e "s|__APP_USER__|${SUDO_USER:-$USER}|g" \
        systemd/butovskyhost.service > "$SERVICE_FILE"
    chmod 600 .env
    systemctl daemon-reload
    systemctl enable --now butovskyhost.service
    echo "Сервис установлен и запущен."
    echo "Логи: sudo journalctl -u butovskyhost -f"
    exit 0
fi

if [[ "${1:-}" == "--run" ]]; then
    RUN_MODE=1
else
    RUN_MODE=0
fi

if [[ ! -f .env ]]; then
    read -r -p "Введите Telegram ID администратора: " ADMIN_ID_INPUT
    if [[ ! "$ADMIN_ID_INPUT" =~ ^[0-9]+$ ]]; then
        echo "ADMIN_ID должен состоять только из цифр."
        exit 1
    fi
    read -r -s -p "Введите токен Telegram-бота: " BOT_TOKEN_INPUT
    echo
    if [[ -z "$BOT_TOKEN_INPUT" ]]; then
        echo "Токен не может быть пустым."
        exit 1
    fi
    umask 077
    cat > .env <<EOF
BOT_TOKEN=${BOT_TOKEN_INPUT}
ADMIN_ID=${ADMIN_ID_INPUT}
LOG_LEVEL=INFO
EOF
    chmod 600 .env
fi

if [[ ! -x .venv/bin/python ]]; then
    python3 -m venv .venv
fi
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -r requirements.txt

if [[ "$RUN_MODE" -eq 0 ]]; then
    echo "Бот запускается. Для автоматического запуска после перезагрузки выполните:"
    echo "  sudo ./start.sh --install-service"
fi

exec .venv/bin/python bot.py