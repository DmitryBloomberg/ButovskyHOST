from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken
from dotenv import load_dotenv


BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")


def _admin_ids() -> frozenset[int]:
    raw = os.getenv("ADMIN_IDS", "")
    try:
        return frozenset(int(item.strip()) for item in raw.split(",") if item.strip())
    except ValueError as exc:
        raise RuntimeError("ADMIN_IDS должен содержать Telegram ID через запятую.") from exc


@dataclass(frozen=True)
class Settings:
    bot_token: str
    admin_ids: frozenset[int]
    payment_instructions: str
    fernet: Fernet


def load_settings() -> Settings:
    token = os.getenv("BOT_TOKEN", "").strip()
    if not token:
        raise RuntimeError("Не задан BOT_TOKEN. Скопируйте .env.example в .env и заполните настройки.")

    admins = _admin_ids()
    if not admins:
        raise RuntimeError("Укажите хотя бы один Telegram ID в ADMIN_IDS.")

    key = os.getenv("FERNET_KEY", "").strip()
    if not key:
        raise RuntimeError(
            "Не задан FERNET_KEY. Сгенерируйте его командой из README и добавьте в .env."
        )
    try:
        fernet = Fernet(key.encode("ascii"))
        # Check that the configured key can actually encrypt and decrypt.
        fernet.decrypt(fernet.encrypt(b"startup-check"))
    except (ValueError, UnicodeEncodeError, InvalidToken) as exc:
        raise RuntimeError("FERNET_KEY некорректен. Сгенерируйте новый ключ по инструкции README.") from exc

    return Settings(
        bot_token=token,
        admin_ids=admins,
        payment_instructions=os.getenv(
            "PAYMENT_INSTRUCTIONS",
            "Свяжитесь с администратором для получения реквизитов оплаты.",
        ).strip(),
        fernet=fernet,
    )