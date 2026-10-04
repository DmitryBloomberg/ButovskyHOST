#!/usr/bin/env bash
set -Eeuo pipefail

PROJECT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="$PROJECT_DIR/.venv"
SERVICE_NAME="butovskyhost.service"
SERVICE_FILE="/etc/systemd/system/$SERVICE_NAME"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m'

fail() {
  printf '%b\n' "${RED}[✗]${NC} $1" >&2
  exit 1
}

run_as_root() {
  if [[ "$(id -u)" -eq 0 ]]; then
    "$@"
  else
    command -v sudo >/dev/null 2>&1 || fail "Нужны права root или установленная команда sudo."
    sudo "$@"
  fi
}

printf '%b\n' "${CYAN}========================================${NC}"
printf '%b\n' "${CYAN}       ButovskyHOST — установка${NC}"
printf '%b\n\n' "${CYAN}========================================${NC}"

command -v python3 >/dev/null 2>&1 || fail "Не найден Python 3. Установите Python 3.10+ и python3-venv."
command -v systemctl >/dev/null 2>&1 || fail "Не найден systemd/systemctl — для фоновой работы 24/7 нужен Linux-сервер с systemd."
if [[ "$(id -u)" -ne 0 ]] && ! command -v sudo >/dev/null 2>&1; then
  fail "Для создания systemd-службы нужны права root или установленная команда sudo."
fi

while true; do
  read -r -s -p "Токен Telegram-бота (ввод скрыт): " BOT_TOKEN
  printf '\n'
  if [[ "$BOT_TOKEN" =~ ^[0-9]+:[A-Za-z0-9_-]{20,}$ ]]; then
    break
  fi
  printf '%b\n' "${RED}[✗]${NC} Проверьте формат токена Telegram-бота."
done

while true; do
  read -r -p "Telegram ID администратора (несколько ID через запятую): " ADMIN_IDS
  ADMIN_IDS="${ADMIN_IDS//[[:space:]]/}"
  if [[ "$ADMIN_IDS" =~ ^[0-9]+(,[0-9]+)*$ ]]; then
    break
  fi
  printf '%b\n' "${RED}[✗]${NC} ID должен содержать только цифры; несколько ID разделяйте запятыми."
done

FERNET_KEY=""
PAYMENT_INSTRUCTIONS=""
if [[ -f "$PROJECT_DIR/.env" ]]; then
  FERNET_KEY="$(sed -n 's/^FERNET_KEY=//p' "$PROJECT_DIR/.env" | head -n 1 || true)"
  PAYMENT_INSTRUCTIONS="$(sed -n 's/^PAYMENT_INSTRUCTIONS=//p' "$PROJECT_DIR/.env" | head -n 1 || true)"
fi

# Keep the existing encryption key when updating the service.
if [[ -z "$FERNET_KEY" ]]; then
  FERNET_KEY="$(python3 -c 'import base64, secrets; print(base64.urlsafe_b64encode(secrets.token_bytes(32)).decode())')"
fi
if [[ -z "$PAYMENT_INSTRUCTIONS" ]]; then
  PAYMENT_INSTRUCTIONS="Переведите оплату по реквизитам, которые вам сообщит администратор."
fi

umask 077
{
  printf 'BOT_TOKEN=%s\n' "$BOT_TOKEN"
  printf 'ADMIN_IDS=%s\n' "$ADMIN_IDS"
  printf 'FERNET_KEY=%s\n' "$FERNET_KEY"
  printf 'PAYMENT_INSTRUCTIONS=%s\n' "$PAYMENT_INSTRUCTIONS"
} > "$PROJECT_DIR/.env"
chmod 600 "$PROJECT_DIR/.env"
unset BOT_TOKEN
printf '%b\n' "${GREEN}[✓]${NC} Настройки сохранены в .env"

if [[ ! -x "$VENV_DIR/bin/python" ]]; then
  python3 -m venv "$VENV_DIR" || fail "Не удалось создать виртуальное окружение. Установите python3-venv."
fi

"$VENV_DIR/bin/python" -m pip --version >/dev/null 2>&1 || fail "В виртуальном окружении недоступен pip. Установите python3-venv."
echo "Устанавливаю зависимости..."
"$VENV_DIR/bin/python" -m pip install --disable-pip-version-check -r "$PROJECT_DIR/requirements.txt"

SERVICE_TMP="$(mktemp)"
trap 'rm -f "$SERVICE_TMP"' EXIT
SERVICE_USER="$(id -un)"
SERVICE_GROUP="$(id -gn)"

cat > "$SERVICE_TMP" <<EOF
[Unit]
Description=ButovskyHOST Telegram bot
Wants=network-online.target
After=network-online.target

[Service]
Type=simple
User=$SERVICE_USER
Group=$SERVICE_GROUP
WorkingDirectory=$PROJECT_DIR
Environment=PYTHONUNBUFFERED=1
ExecStart=$VENV_DIR/bin/python $PROJECT_DIR/main.py
Restart=always
RestartSec=5
TimeoutStopSec=30

[Install]
WantedBy=multi-user.target
EOF

run_as_root install -o root -g root -m 0644 "$SERVICE_TMP" "$SERVICE_FILE"
if command -v systemd-analyze >/dev/null 2>&1; then
  if ! run_as_root systemd-analyze verify "$SERVICE_FILE"; then
    fail "systemd отклонил файл службы. Исправьте указанную выше ошибку и запустите bash start.sh ещё раз."
  fi
fi
run_as_root systemctl daemon-reload
run_as_root systemctl enable "$SERVICE_NAME" >/dev/null
run_as_root systemctl reset-failed "$SERVICE_NAME" >/dev/null 2>&1 || true
if ! run_as_root systemctl restart "$SERVICE_NAME"; then
  printf '%b\n' "${RED}[✗]${NC} Не удалось запустить systemd-службу."
  run_as_root systemctl status "$SERVICE_NAME" --no-pager -l || true
  run_as_root journalctl -u "$SERVICE_NAME" -n 50 --no-pager || true
  exit 1
fi
sleep 2

if ! run_as_root systemctl is-active --quiet "$SERVICE_NAME"; then
  printf '%b\n' "${RED}[✗]${NC} Служба ButovskyHOST не запустилась."
  printf '%b\n' "${YELLOW}Последние логи:${NC}"
  run_as_root journalctl -u "$SERVICE_NAME" -n 50 --no-pager || true
  exit 1
fi

trap - EXIT
rm -f "$SERVICE_TMP"
printf '\n%b\n' "${GREEN}[✓] ButovskyHOST работает в фоне и настроен на автозапуск 24/7.${NC}"
printf '%b\n' "Статус:       sudo systemctl status $SERVICE_NAME"
printf '%b\n' "Логи:         sudo journalctl -u $SERVICE_NAME -f"
printf '%b\n' "Остановить:   sudo systemctl stop $SERVICE_NAME"
printf '%b\n' "Перезапустить: sudo systemctl restart $SERVICE_NAME"