#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

if [[ "${EUID}" -eq 0 ]]; then
  echo "Запускайте start.sh от обычного пользователя с правом sudo, не через sudo bash." >&2
  exit 1
fi

if ! command -v python3 >/dev/null 2>&1; then
  echo "Ошибка: Python 3 не найден. Установите Python 3.10 или новее." >&2
  exit 1
fi

if ! command -v systemctl >/dev/null 2>&1 || [[ ! -d /run/systemd/system ]]; then
  echo "Ошибка: systemd не обнаружен. Этот скрипт устанавливает постоянную службу systemd и требует Linux-сервер с systemd." >&2
  exit 1
fi

if ! command -v sudo >/dev/null 2>&1; then
  echo "Ошибка: sudo не найден. Нужен обычный пользователь с правом устанавливать systemd-службу." >&2
  exit 1
fi
echo "Для установки и запуска systemd-службы потребуется пароль sudo."
sudo -v

read -r -s -p "Введите токен Telegram-бота (ввод скрыт): " BOT_TOKEN
printf '\n'
BOT_TOKEN="${BOT_TOKEN//[[:space:]]/}"
if [[ -z "$BOT_TOKEN" ]]; then
  echo "Ошибка: токен не может быть пустым." >&2
  exit 1
fi

read -r -p "Введите Telegram ID администратора (несколько ID через запятую): " ADMIN_IDS
ADMIN_IDS="${ADMIN_IDS//[[:space:]]/}"
if [[ ! "$ADMIN_IDS" =~ ^[0-9]+(,[0-9]+)*$ ]]; then
  echo "Ошибка: укажите числовой Telegram ID; несколько ID разделяйте запятыми." >&2
  exit 1
fi

FERNET_KEY=""
PAYMENT_INSTRUCTIONS=""
if [[ -f ".env" ]]; then
  FERNET_KEY="$(sed -n 's/^FERNET_KEY=//p' .env | head -n 1 || true)"
  PAYMENT_INSTRUCTIONS="$(sed -n 's/^PAYMENT_INSTRUCTIONS=//p' .env | head -n 1 || true)"
fi

# Keep the original Fernet key on later runs: changing it would make saved
# server passwords impossible to decrypt.
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
} > .env
chmod 600 .env
unset BOT_TOKEN

if [[ ! -x ".venv/bin/python" ]]; then
  python3 -m venv .venv
fi

VENV_PYTHON=".venv/bin/python"
if ! "$VENV_PYTHON" -m pip --version >/dev/null 2>&1; then
  echo "Ошибка: pip недоступен в виртуальном окружении. Установите пакет venv для Python 3 и повторите запуск." >&2
  exit 1
fi

echo "Устанавливаю зависимости из requirements.txt..."
"$VENV_PYTHON" -m pip install -r requirements.txt

PROJECT_DIR="$(pwd -P)"
SERVICE_NAME="butovskyhost.service"
SERVICE_TMP="$(mktemp)"
trap 'rm -f "$SERVICE_TMP"' EXIT

cat > "$SERVICE_TMP" <<EOF
[Unit]
Description=ButovskyHOST Telegram bot
Wants=network-online.target
After=network-online.target

[Service]
Type=simple
User=$(id -un)
Group=$(id -gn)
WorkingDirectory="$PROJECT_DIR"
EnvironmentFile="$PROJECT_DIR/.env"
Environment=PYTHONUNBUFFERED=1
Environment=PYTHONDONTWRITEBYTECODE=1
UMask=0077
ExecStart="$PROJECT_DIR/.venv/bin/python" "$PROJECT_DIR/main.py"
Restart=always
RestartSec=5
NoNewPrivileges=true
PrivateTmp=true

[Install]
WantedBy=multi-user.target
EOF

echo "Устанавливаю systemd-службу $SERVICE_NAME..."
sudo install -o root -g root -m 0644 "$SERVICE_TMP" "/etc/systemd/system/$SERVICE_NAME"
sudo systemctl daemon-reload
sudo systemctl enable "$SERVICE_NAME"
if sudo systemctl is-active --quiet "$SERVICE_NAME"; then
  sudo systemctl restart "$SERVICE_NAME"
else
  sudo systemctl start "$SERVICE_NAME"
fi

trap - EXIT
rm -f "$SERVICE_TMP"
echo "Служба включена и будет автоматически запускаться после перезагрузки сервера."
echo "Статус:  sudo systemctl status $SERVICE_NAME"
echo "Логи:    sudo journalctl -u $SERVICE_NAME -f"
echo "Остановка: sudo systemctl stop $SERVICE_NAME"