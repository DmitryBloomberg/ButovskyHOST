#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

if ! command -v python3 >/dev/null 2>&1; then
  echo "Ошибка: Python 3 не найден. Установите Python 3.10 или новее." >&2
  exit 1
fi

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

echo "Запускаю бота. Остановить можно сочетанием Ctrl+C."
exec "$VENV_PYTHON" main.py