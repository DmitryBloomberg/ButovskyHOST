"""ButovskyHOST Telegram bot.

The bot intentionally stores its small amount of state in JSON files.  This
makes the project easy to deploy on a VPS and keeps all customer data in the
repository's data directory (which is ignored by git).
"""

from __future__ import annotations

import asyncio
import html
import json
import logging
import os
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder
from dotenv import load_dotenv


load_dotenv()
logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("butovskyhost")


BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / os.getenv("DATA_DIR", "data")
USERS_DIR = DATA_DIR / "users"
SERVERS_DIR = DATA_DIR / "servers"
ORDERS_DIR = DATA_DIR / "orders"
TARIFFS_DIR = DATA_DIR / "tariffs"
SETTINGS_FILE = DATA_DIR / "settings.json"

REGIONS = {
    "de": "🇩🇪 Германия",
    "fi": "🇫🇮 Финляндия",
    "nl": "🇳🇱 Нидерланды",
}
PERIODS = {1: "1 месяц", 3: "3 месяца", 5: "5 месяцев"}
SOLUTIONS = {
    "amnezia": "Amnezia",
    "xui": "3X-UI",
    "none": "Нет",
}
ORDER_STATUSES = {"Заказан", "Настройка", "Готов", "Отклонён"}


def required_admin_id() -> int:
    value = os.getenv("ADMIN_ID", "").strip()
    if not value.isdigit():
        raise RuntimeError(
            "ADMIN_ID is not configured. Run ./start.sh once and enter the Telegram ID."
        )
    return int(value)


try:
    ADMIN_ID = required_admin_id()
except RuntimeError:
    # Keep importing the module useful for linting and local inspection.  The
    # actual startup fails with the helpful message in main().
    ADMIN_ID = 0


def ensure_storage() -> None:
    for directory in (USERS_DIR, SERVERS_DIR, ORDERS_DIR, TARIFFS_DIR):
        directory.mkdir(parents=True, exist_ok=True)
    if not SETTINGS_FILE.exists():
        atomic_write(
            SETTINGS_FILE,
            {
                "payment_requisites": (
                    "Реквизиты пока не настроены. Обратитесь к администратору."
                )
            },
        )


def atomic_write(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    os.replace(temporary, path)


def load_json(path: Path, default: Any = None) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        logger.exception("Could not read %s", path)
        return default


def list_json(directory: Path) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for path in sorted(directory.glob("*.json")):
        item = load_json(path)
        if isinstance(item, dict):
            result.append(item)
    return result


def get_by_id(directory: Path, item_id: str) -> dict[str, Any] | None:
    return load_json(directory / f"{item_id}.json")


def save_entity(directory: Path, item_id: str, payload: dict[str, Any]) -> None:
    atomic_write(directory / f"{item_id}.json", payload)


def delete_entity(directory: Path, item_id: str) -> None:
    path = directory / f"{item_id}.json"
    if path.exists():
        path.unlink()


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def money(value: Any) -> str:
    try:
        number = float(value)
        if number.is_integer():
            return f"{int(number)} ₽"
        return f"{number:.2f} ₽"
    except (TypeError, ValueError):
        return "—"


def parse_price(value: str) -> float:
    normalized = value.strip().replace(",", ".").replace("₽", "").strip()
    number = float(normalized)
    if number < 0 or number > 10_000_000:
        raise ValueError("price out of range")
    return round(number, 2)


def text(value: Any, fallback: str = "—") -> str:
    return html.escape(str(value)) if value not in (None, "") else fallback


def short_id(value: str) -> str:
    return html.escape(value[:12])


def user_path(user_id: int) -> Path:
    return USERS_DIR / f"{user_id}.json"


def ensure_user(tg_user: Any) -> dict[str, Any]:
    user_id = int(tg_user.id)
    path = user_path(user_id)
    stored = load_json(path, {})
    status = "Администратор" if user_id == ADMIN_ID else stored.get("status", "Пользователь")
    user = {
        "first_name": tg_user.first_name or stored.get("first_name", ""),
        "last_name": tg_user.last_name or stored.get("last_name", ""),
        "telegram_id": user_id,
        "status": status,
        "username": tg_user.username or stored.get("username", ""),
        "created_at": stored.get("created_at", now_iso()),
        "updated_at": now_iso(),
    }
    save_entity(USERS_DIR, str(user_id), user)
    return user


def settings() -> dict[str, Any]:
    return load_json(SETTINGS_FILE, {}) or {}


def tariff_for_order(order: dict[str, Any]) -> dict[str, Any] | None:
    tariff_id = order.get("tariff_id")
    return get_by_id(TARIFFS_DIR, str(tariff_id)) if tariff_id else None


def order_price(order: dict[str, Any]) -> float:
    tariff = tariff_for_order(order)
    if not tariff:
        return 0
    prices = tariff.get("prices", {})
    return float(prices.get(str(order.get("period")), 0))


def tariff_label(tariff: dict[str, Any]) -> str:
    return f"{text(tariff.get('name'), 'Без названия')} — {text(tariff.get('description'), 'характеристики не указаны')}"


def order_summary(order: dict[str, Any], admin: bool = False) -> str:
    tariff = tariff_for_order(order)
    lines = [
        f"🧾 <b>Заказ {short_id(str(order.get('id', '')))}</b>",
        f"Регион: {text(REGIONS.get(order.get('region'), order.get('region')))}",
        f"Тариф: {text(tariff.get('name') if tariff else order.get('tariff_name'))}",
        f"Характеристики: {text(tariff.get('description') if tariff else order.get('tariff_description'))}",
        f"Период: {text(PERIODS.get(int(order.get('period', 0)), order.get('period')))}",
        f"Разрешение на 20%: {'Да' if order.get('use_permission') else 'Нет'}",
        f"Решение: {text(SOLUTIONS.get(order.get('solution'), order.get('solution')))}",
        f"Стоимость: <b>{money(order.get('price', order_price(order)))}</b>",
        f"Статус: <b>{text(order.get('status'))}</b>",
        f"Оплата: <b>{text(order.get('payment_status'))}</b>",
    ]
    if admin:
        lines.extend(
            [
                "",
                f"Пользователь: {text(order.get('user_name'))}",
                f"Telegram ID: <code>{text(order.get('user_id'))}</code>",
                f"Создан: {text(order.get('created_at'))}",
            ]
        )
    return "\n".join(lines)


def main_menu(user_id: int) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="👤 Профиль", callback_data="menu:profile"),
        InlineKeyboardButton(text="🖥 Мои серверы", callback_data="menu:servers"),
    )
    builder.row(
        InlineKeyboardButton(text="🛒 Заказать сервер", callback_data="menu:order"),
        InlineKeyboardButton(text="📦 Активные заказы", callback_data="menu:orders"),
    )
    if user_id == ADMIN_ID:
        builder.row(InlineKeyboardButton(text="⚙️ Админ-панель", callback_data="admin:menu"))
    return builder.as_markup()


def back_keyboard(destination: str = "menu:main") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="⬅️ Назад", callback_data=destination)]
        ]
    )


def admin_menu_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="🧾 Заказы", callback_data="admin:orders"),
        InlineKeyboardButton(text="💳 Тарифы", callback_data="admin:tariffs"),
    )
    builder.row(
        InlineKeyboardButton(text="📊 Статистика", callback_data="admin:stats"),
        InlineKeyboardButton(text="🖥 Все серверы", callback_data="admin:servers"),
    )
    builder.row(
        InlineKeyboardButton(text="💰 Реквизиты", callback_data="admin:settings"),
        InlineKeyboardButton(text="⬅️ Главное меню", callback_data="menu:main"),
    )
    return builder.as_markup()


def admin_order_keyboard(order: dict[str, Any]) -> InlineKeyboardMarkup:
    order_id = str(order["id"])
    builder = InlineKeyboardBuilder()
    if order.get("payment_status") == "Ожидает проверки":
        builder.row(
            InlineKeyboardButton(
                text="✅ Подтвердить оплату", callback_data=f"admin:approve:{order_id}"
            ),
            InlineKeyboardButton(
                text="❌ Отклонить", callback_data=f"admin:reject:{order_id}"
            ),
        )
    if order.get("payment_status") == "Оплачено" and order.get("status") == "Заказан":
        builder.row(
            InlineKeyboardButton(
                text="⚙️ В настройку", callback_data=f"admin:setup:{order_id}"
            )
        )
    if order.get("payment_status") == "Оплачено" and order.get("status") == "Настройка":
        builder.row(
            InlineKeyboardButton(
                text="✅ Готов", callback_data=f"admin:ready:{order_id}"
            )
        )
    builder.row(
        InlineKeyboardButton(
            text="🔄 Обновить", callback_data=f"admin:order:{order_id}"
        ),
        InlineKeyboardButton(text="⬅️ К заказам", callback_data="admin:orders"),
    )
    return builder.as_markup()


def render_home() -> str:
    return (
        "🚀 <b>ButovskyHOST</b>\n\n"
        "Надёжная аренда серверов для VPN, прокси и личных проектов.\n\n"
        "✅ Высокий аптайм и стабильное подключение\n"
        "⚡ Быстрое развёртывание выбранного решения\n"
        "🛡️ Понятные тарифы без скрытых условий\n"
        "💬 Оперативная поддержка по заказу\n\n"
        "Выберите нужный раздел ниже:"
    )


def render_profile(user: dict[str, Any]) -> str:
    username = f"@{text(user.get('username'))}" if user.get("username") else "не указан"
    return (
        "👤 <b>Профиль</b>\n\n"
        f"Имя: {text(user.get('first_name'))}\n"
        f"Фамилия: {text(user.get('last_name'))}\n"
        f"Username: {username}\n"
        f"Telegram ID: <code>{text(user.get('telegram_id'))}</code>\n"
        f"Статус: <b>{text(user.get('status'))}</b>"
    )


def render_order_draft(data: dict[str, Any]) -> str:
    tariff = get_by_id(TARIFFS_DIR, str(data.get("tariff_id", "")))
    tariff_name = tariff.get("name") if tariff else None
    tariff_description = tariff.get("description") if tariff else None
    period = data.get("period")
    permission = data.get("use_permission")
    selected_price = (
        tariff.get("prices", {}).get(str(period))
        if tariff and period
        else None
    )
    return (
        "🛒 <b>Создание сервера</b>\n\n"
        f"Регион: <b>{text(REGIONS.get(data.get('region')))}</b>\n"
        f"Название: <b>{text(tariff_name or data.get('tariff_id'))}</b>\n"
        f"Характеристики: {text(tariff_description)}\n"
        f"Период: <b>{text(PERIODS.get(int(period)) if period else None)}</b>\n"
        "Разрешение: <b>"
        + (
            "Да — можно использовать до 20% мощности"
            if permission is True
            else "Нет"
            if permission is False
            else "не выбрано"
        )
        + "</b>\n"
        f"Решение: <b>{text(SOLUTIONS.get(data.get('solution')))}</b>\n\n"
        f"Стоимость: <b>{money(selected_price) if selected_price is not None else '—'}</b>\n\n"
        "Выберите или измените параметры. Цена появится после выбора тарифа и периода."
    )


def order_builder(data: dict[str, Any]) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for code, label in REGIONS.items():
        prefix = "✅ " if data.get("region") == code else ""
        builder.row(
            InlineKeyboardButton(
                text=f"{prefix}{label}", callback_data=f"order:region:{code}"
            )
        )
    tariffs = list_json(TARIFFS_DIR)
    if tariffs:
        for tariff in tariffs:
            selected = data.get("tariff_id") == tariff.get("id")
            builder.row(
                InlineKeyboardButton(
                    text=f"{'✅ ' if selected else ''}💻 {tariff.get('name', 'Тариф')}",
                    callback_data=f"order:tariff:{tariff.get('id')}",
                )
            )
    else:
        builder.row(
            InlineKeyboardButton(
                text="💻 Тарифы ещё не добавлены", callback_data="order:no_tariffs"
            )
        )
    builder.row(
        *[
            InlineKeyboardButton(
                text=f"{'✅ ' if data.get('period') == period else ''}{label}",
                callback_data=f"order:period:{period}",
            )
            for period, label in PERIODS.items()
        ]
    )
    builder.row(
        InlineKeyboardButton(
            text="✅ Разрешить 20%" if data.get("use_permission") is True else "☑️ Разрешение: Да",
            callback_data="order:permission:yes",
        ),
        InlineKeyboardButton(
            text="✅ Не разрешать" if data.get("use_permission") is False else "☐ Разрешение: Нет",
            callback_data="order:permission:no",
        ),
    )
    builder.row(
        *[
            InlineKeyboardButton(
                text=f"{'✅ ' if data.get('solution') == key else ''}{label}",
                callback_data=f"order:solution:{key}",
            )
            for key, label in SOLUTIONS.items()
        ]
    )
    complete = all(
        [
            data.get("region"),
            data.get("tariff_id"),
            data.get("period"),
            data.get("use_permission") is not None,
            data.get("solution"),
        ]
    )
    if complete:
        tariff = get_by_id(TARIFFS_DIR, str(data["tariff_id"]))
        price = tariff.get("prices", {}).get(str(data["period"])) if tariff else 0
        builder.row(
            InlineKeyboardButton(
                text=f"💳 Перейти к оплате · {money(price)}",
                callback_data="order:pay",
            )
        )
    builder.row(InlineKeyboardButton(text="⬅️ В меню", callback_data="menu:main"))
    return builder.as_markup()


def receipt_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="📎 Отправить чек", callback_data="order:receipt"
                )
            ],
            [InlineKeyboardButton(text="⬅️ В меню", callback_data="menu:main")],
        ]
    )


def active_orders_for_user(user_id: int) -> list[dict[str, Any]]:
    return [
        order
        for order in list_json(ORDERS_DIR)
        if int(order.get("user_id", -1)) == user_id
        and order.get("status") != "Отклонён"
    ]


class OrderStates(StatesGroup):
    waiting_receipt = State()


class ReadyStates(StatesGroup):
    ip = State()
    port = State()
    login = State()
    password = State()
    comment = State()


class TariffStates(StatesGroup):
    name = State()
    description = State()
    cpu = State()
    ram = State()
    disk = State()
    bandwidth = State()
    price_1 = State()
    price_3 = State()
    price_5 = State()


class SettingsStates(StatesGroup):
    requisites = State()


router = Router()


def only_admin(user_id: int) -> bool:
    return user_id == ADMIN_ID


async def edit_callback(
    callback: CallbackQuery,
    body: str,
    markup: InlineKeyboardMarkup | None = None,
) -> None:
    try:
        await callback.message.edit_text(body, reply_markup=markup)
    except TelegramBadRequest as error:
        if "message is not modified" not in str(error).lower():
            await callback.message.answer(body, reply_markup=markup)
    await callback.answer()


async def send_home(message: Message) -> None:
    ensure_user(message.from_user)
    await message.answer(render_home(), reply_markup=main_menu(message.from_user.id))


@router.message(Command("start"))
async def start_handler(message: Message, state: FSMContext) -> None:
    await state.clear()
    await send_home(message)


@router.message(Command("cancel"))
async def cancel_handler(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("Действие отменено.", reply_markup=main_menu(message.from_user.id))


@router.callback_query(F.data == "menu:main")
async def menu_main(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    ensure_user(callback.from_user)
    await edit_callback(
        callback, render_home(), main_menu(callback.from_user.id)
    )


@router.callback_query(F.data == "menu:profile")
async def menu_profile(callback: CallbackQuery) -> None:
    user = ensure_user(callback.from_user)
    await edit_callback(callback, render_profile(user), back_keyboard())


@router.callback_query(F.data == "menu:servers")
async def menu_servers(callback: CallbackQuery) -> None:
    user_id = callback.from_user.id
    servers = [
        server
        for server in list_json(SERVERS_DIR)
        if int(server.get("user_id", -1)) == user_id
    ]
    if not servers:
        body = (
            "🖥 <b>Мои серверы</b>\n\n"
            "У вас пока нет готовых серверов. После подтверждения оплаты "
            "и настройки данные появятся здесь."
        )
    else:
        chunks = ["🖥 <b>Мои серверы</b>\n"]
        for server in servers:
            chunks.append(
                "\n".join(
                    [
                        f"<b>{text(server.get('name', server.get('id')))}</b>",
                        f"Регион: {text(REGIONS.get(server.get('region')))}",
                        f"IP: <code>{text(server.get('ip'))}</code>",
                        f"Порт: <code>{text(server.get('port'))}</code>",
                        f"Логин: <code>{text(server.get('login'))}</code>",
                        f"Пароль: <code>{text(server.get('password'))}</code>",
                        f"Решение: {text(SOLUTIONS.get(server.get('solution')))}",
                        f"Разрешение 20%: {'Да' if server.get('use_permission') else 'Нет'}",
                        f"Комментарий: {text(server.get('comment'))}",
                    ]
                )
            )
        body = "\n\n".join(chunks)
    await edit_callback(callback, body, back_keyboard())


@router.callback_query(F.data == "menu:orders")
async def menu_orders(callback: CallbackQuery) -> None:
    orders = active_orders_for_user(callback.from_user.id)
    if not orders:
        body = "📦 <b>Активные заказы</b>\n\nАктивных заказов нет."
    else:
        body = "📦 <b>Активные заказы</b>\n\n" + "\n\n".join(
            order_summary(order) for order in orders
        )
    await edit_callback(callback, body, back_keyboard())


@router.callback_query(F.data == "menu:order")
async def order_start(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await edit_callback(
        callback,
        render_order_draft({}),
        order_builder({}),
    )


@router.callback_query(F.data == "order:no_tariffs")
async def no_tariffs(callback: CallbackQuery) -> None:
    await callback.answer(
        "Администратор ещё не добавил тарифы.", show_alert=True
    )


@router.callback_query(F.data.startswith("order:region:"))
async def choose_region(callback: CallbackQuery, state: FSMContext) -> None:
    code = callback.data.rsplit(":", 1)[1]
    data = await state.get_data()
    await state.update_data(region=code)
    data["region"] = code
    await edit_callback(callback, render_order_draft(data), order_builder(data))


@router.callback_query(F.data.startswith("order:tariff:"))
async def choose_tariff(callback: CallbackQuery, state: FSMContext) -> None:
    tariff_id = callback.data.rsplit(":", 1)[1]
    if not get_by_id(TARIFFS_DIR, tariff_id):
        await callback.answer("Тариф больше недоступен.", show_alert=True)
        return
    data = await state.get_data()
    await state.update_data(tariff_id=tariff_id)
    data["tariff_id"] = tariff_id
    await edit_callback(callback, render_order_draft(data), order_builder(data))


@router.callback_query(F.data.startswith("order:period:"))
async def choose_period(callback: CallbackQuery, state: FSMContext) -> None:
    period = int(callback.data.rsplit(":", 1)[1])
    data = await state.get_data()
    await state.update_data(period=period)
    data["period"] = period
    await edit_callback(callback, render_order_draft(data), order_builder(data))


@router.callback_query(F.data.startswith("order:permission:"))
async def choose_permission(callback: CallbackQuery, state: FSMContext) -> None:
    value = callback.data.rsplit(":", 1)[1] == "yes"
    data = await state.get_data()
    await state.update_data(use_permission=value)
    data["use_permission"] = value
    await edit_callback(callback, render_order_draft(data), order_builder(data))


@router.callback_query(F.data.startswith("order:solution:"))
async def choose_solution(callback: CallbackQuery, state: FSMContext) -> None:
    solution = callback.data.rsplit(":", 1)[1]
    data = await state.get_data()
    await state.update_data(solution=solution)
    data["solution"] = solution
    await edit_callback(callback, render_order_draft(data), order_builder(data))


@router.callback_query(F.data == "order:pay")
async def order_pay(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    required = ("region", "tariff_id", "period", "use_permission", "solution")
    if not all(key in data for key in required):
        await callback.answer("Сначала выберите все параметры.", show_alert=True)
        return
    tariff = get_by_id(TARIFFS_DIR, str(data["tariff_id"]))
    if not tariff:
        await callback.answer("Тариф больше недоступен.", show_alert=True)
        return
    price = tariff.get("prices", {}).get(str(data["period"]), 0)
    body = (
        "💳 <b>Оплата заказа</b>\n\n"
        f"К оплате: <b>{money(price)}</b>\n\n"
        f"<b>Реквизиты:</b>\n{text(settings().get('payment_requisites'), 'Реквизиты не настроены')}\n\n"
        "После оплаты нажмите кнопку ниже и отправьте фото или файл чека. "
        "Администратор проверит платёж и начнёт настройку."
    )
    await edit_callback(callback, body, receipt_keyboard())


@router.callback_query(F.data == "order:receipt")
async def receipt_start(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(OrderStates.waiting_receipt)
    await callback.message.answer(
        "📎 Отправьте одним сообщением фотографию или файл чека.\n"
        "Для отмены используйте /cancel."
    )
    await callback.answer()


@router.message(OrderStates.waiting_receipt, F.photo)
async def receive_photo_receipt(message: Message, state: FSMContext, bot: Bot) -> None:
    photo = message.photo[-1]
    await create_order_from_receipt(
        message,
        state,
        bot,
        {"type": "photo", "file_id": photo.file_id},
    )


@router.message(OrderStates.waiting_receipt, F.document)
async def receive_document_receipt(message: Message, state: FSMContext, bot: Bot) -> None:
    document = message.document
    await create_order_from_receipt(
        message,
        state,
        bot,
        {
            "type": "document",
            "file_id": document.file_id,
            "file_name": document.file_name or "receipt",
        },
    )


async def create_order_from_receipt(
    message: Message,
    state: FSMContext,
    bot: Bot,
    receipt: dict[str, Any],
) -> None:
    data = await state.get_data()
    tariff = get_by_id(TARIFFS_DIR, str(data.get("tariff_id", "")))
    if not tariff:
        await state.clear()
        await message.answer(
            "Тариф больше недоступен. Начните заказ заново.",
            reply_markup=main_menu(message.from_user.id),
        )
        return
    order_id = f"ORD-{uuid.uuid4().hex[:10].upper()}"
    order = {
        "id": order_id,
        "user_id": message.from_user.id,
        "user_name": " ".join(
            part for part in [message.from_user.first_name, message.from_user.last_name] if part
        ),
        "region": data["region"],
        "tariff_id": tariff["id"],
        "tariff_name": tariff.get("name"),
        "tariff_description": tariff.get("description"),
        "period": int(data["period"]),
        "use_permission": bool(data["use_permission"]),
        "solution": data["solution"],
        "price": order_price({**data, "tariff_id": tariff["id"]}),
        "status": "Заказан",
        "payment_status": "Ожидает проверки",
        "receipt": receipt,
        "created_at": now_iso(),
        "updated_at": now_iso(),
    }
    save_entity(ORDERS_DIR, order_id, order)
    await state.clear()
    await message.answer(
        f"✅ Заказ <code>{order_id}</code> создан.\n\n"
        "Чек отправлен администратору на проверку. После подтверждения "
        "статус заказа появится в разделе «Активные заказы».",
        reply_markup=main_menu(message.from_user.id),
    )
    try:
        await bot.send_message(
            ADMIN_ID,
            "🔔 <b>Новая заявка на оплату</b>\n\n"
            + order_summary(order, admin=True),
            reply_markup=admin_order_keyboard(order),
        )
        if receipt["type"] == "photo":
            await bot.send_photo(
                ADMIN_ID,
                receipt["file_id"],
                caption=f"Чек по заказу <code>{order_id}</code>",
            )
        else:
            await bot.send_document(
                ADMIN_ID,
                receipt["file_id"],
                caption=f"Чек по заказу <code>{order_id}</code>",
            )
    except (TelegramForbiddenError, TelegramBadRequest):
        logger.exception("Could not notify admin about %s", order_id)


@router.callback_query(F.data == "admin:menu")
async def admin_menu(callback: CallbackQuery) -> None:
    if not only_admin(callback.from_user.id):
        await callback.answer("Доступ запрещён.", show_alert=True)
        return
    await edit_callback(
        callback,
        "⚙️ <b>Админ-панель</b>\n\nУправление заказами, тарифами, оплатами и серверами.",
        admin_menu_keyboard(),
    )


@router.callback_query(F.data == "admin:orders")
async def admin_orders(callback: CallbackQuery) -> None:
    if not only_admin(callback.from_user.id):
        await callback.answer("Доступ запрещён.", show_alert=True)
        return
    orders = sorted(
        list_json(ORDERS_DIR),
        key=lambda item: item.get("created_at", ""),
        reverse=True,
    )
    if not orders:
        body = "🧾 <b>Заказы</b>\n\nЗаказов пока нет."
        markup = back_keyboard("admin:menu")
    else:
        builder = InlineKeyboardBuilder()
        for order in orders[:30]:
            label = (
                f"{order.get('id')} · {order.get('status')} · "
                f"{money(order.get('price'))}"
            )
            builder.row(
                InlineKeyboardButton(
                    text=label[:60],
                    callback_data=f"admin:order:{order.get('id')}",
                )
            )
        builder.row(InlineKeyboardButton(text="⬅️ В админ-панель", callback_data="admin:menu"))
        body = f"🧾 <b>Заказы</b>\n\nВсего: {len(orders)}"
        markup = builder.as_markup()
    await edit_callback(callback, body, markup)


@router.callback_query(F.data.startswith("admin:order:"))
async def admin_order(callback: CallbackQuery) -> None:
    if not only_admin(callback.from_user.id):
        await callback.answer("Доступ запрещён.", show_alert=True)
        return
    order_id = callback.data.split(":", 2)[2]
    order = get_by_id(ORDERS_DIR, order_id)
    if not order:
        await callback.answer("Заказ не найден.", show_alert=True)
        return
    await edit_callback(callback, order_summary(order, admin=True), admin_order_keyboard(order))


async def update_order_and_notify(
    callback: CallbackQuery,
    order: dict[str, Any],
    *,
    status: str | None = None,
    payment_status: str | None = None,
    notice: str,
) -> None:
    if status:
        order["status"] = status
    if payment_status:
        order["payment_status"] = payment_status
    order["updated_at"] = now_iso()
    save_entity(ORDERS_DIR, order["id"], order)
    await edit_callback(callback, order_summary(order, admin=True), admin_order_keyboard(order))
    try:
        await callback.bot.send_message(
            int(order["user_id"]),
            f"📦 Заказ <code>{order['id']}</code>\n\n{notice}\n"
            f"Текущий статус: <b>{text(order.get('status'))}</b>",
        )
    except (TelegramForbiddenError, TelegramBadRequest):
        logger.info("User %s cannot be notified", order.get("user_id"))


@router.callback_query(F.data.startswith("admin:approve:"))
async def admin_approve(callback: CallbackQuery) -> None:
    if not only_admin(callback.from_user.id):
        await callback.answer("Доступ запрещён.", show_alert=True)
        return
    order = get_by_id(ORDERS_DIR, callback.data.split(":", 2)[2])
    if not order:
        await callback.answer("Заказ не найден.", show_alert=True)
        return
    await update_order_and_notify(
        callback,
        order,
        payment_status="Оплачено",
        notice="Оплата подтверждена администратором. Заказ можно взять в настройку.",
    )


@router.callback_query(F.data.startswith("admin:reject:"))
async def admin_reject(callback: CallbackQuery) -> None:
    if not only_admin(callback.from_user.id):
        await callback.answer("Доступ запрещён.", show_alert=True)
        return
    order = get_by_id(ORDERS_DIR, callback.data.split(":", 2)[2])
    if not order:
        await callback.answer("Заказ не найден.", show_alert=True)
        return
    await update_order_and_notify(
        callback,
        order,
        status="Отклонён",
        payment_status="Отклонено",
        notice="Оплата отклонена. Если это ошибка, свяжитесь с поддержкой.",
    )


@router.callback_query(F.data.startswith("admin:setup:"))
async def admin_setup(callback: CallbackQuery) -> None:
    if not only_admin(callback.from_user.id):
        await callback.answer("Доступ запрещён.", show_alert=True)
        return
    order = get_by_id(ORDERS_DIR, callback.data.split(":", 2)[2])
    if not order or order.get("payment_status") != "Оплачено":
        await callback.answer("Сначала подтвердите оплату.", show_alert=True)
        return
    await update_order_and_notify(
        callback,
        order,
        status="Настройка",
        notice="Заказ передан в настройку сервера.",
    )


@router.callback_query(F.data.startswith("admin:ready:"))
async def admin_ready_start(callback: CallbackQuery, state: FSMContext) -> None:
    if not only_admin(callback.from_user.id):
        await callback.answer("Доступ запрещён.", show_alert=True)
        return
    order_id = callback.data.split(":", 2)[2]
    order = get_by_id(ORDERS_DIR, order_id)
    if not order or order.get("status") != "Настройка":
        await callback.answer("Сначала переведите заказ в настройку.", show_alert=True)
        return
    await state.clear()
    await state.update_data(order_id=order_id)
    await state.set_state(ReadyStates.ip)
    await callback.message.answer(
        f"✅ Выдача заказа <code>{order_id}</code>\n\n"
        "Введите IP-адрес сервера.\nДля отмены используйте /cancel."
    )
    await callback.answer()


@router.message(ReadyStates.ip, F.text)
async def ready_ip(message: Message, state: FSMContext) -> None:
    if not only_admin(message.from_user.id):
        return
    await state.update_data(ip=message.text.strip())
    await state.set_state(ReadyStates.port)
    await message.answer("Введите порт сервера (например, 443):")


@router.message(ReadyStates.port, F.text)
async def ready_port(message: Message, state: FSMContext) -> None:
    if not only_admin(message.from_user.id):
        return
    port = message.text.strip()
    if not port.isdigit() or not 1 <= int(port) <= 65535:
        await message.answer("Порт должен быть числом от 1 до 65535. Попробуйте ещё раз:")
        return
    await state.update_data(port=int(port))
    await state.set_state(ReadyStates.login)
    await message.answer("Введите логин сервера:")


@router.message(ReadyStates.login, F.text)
async def ready_login(message: Message, state: FSMContext) -> None:
    if not only_admin(message.from_user.id):
        return
    await state.update_data(login=message.text.strip())
    await state.set_state(ReadyStates.password)
    await message.answer("Введите пароль сервера:")


@router.message(ReadyStates.password, F.text)
async def ready_password(message: Message, state: FSMContext) -> None:
    if not only_admin(message.from_user.id):
        return
    await state.update_data(password=message.text.strip())
    await state.set_state(ReadyStates.comment)
    await message.answer("Введите комментарий к заказу (или отправьте «-»):")


@router.message(ReadyStates.comment, F.text)
async def ready_comment(message: Message, state: FSMContext) -> None:
    if not only_admin(message.from_user.id):
        return
    data = await state.get_data()
    order = get_by_id(ORDERS_DIR, str(data.get("order_id")))
    if not order:
        await state.clear()
        await message.answer("Заказ не найден.", reply_markup=admin_menu_keyboard())
        return
    comment = "" if message.text.strip() == "-" else message.text.strip()
    server_id = f"SRV-{uuid.uuid4().hex[:10].upper()}"
    server = {
        "id": server_id,
        "order_id": order["id"],
        "user_id": order["user_id"],
        "name": order.get("tariff_name") or server_id,
        "region": order.get("region"),
        "tariff_id": order.get("tariff_id"),
        "period": order.get("period"),
        "solution": order.get("solution"),
        "use_permission": order.get("use_permission"),
        "ip": data["ip"],
        "port": data["port"],
        "login": data["login"],
        "password": data["password"],
        "comment": comment,
        "created_at": now_iso(),
    }
    order.update(
        {
            "status": "Готов",
            "server_id": server_id,
            "server": {
                "ip": data["ip"],
                "port": data["port"],
                "login": data["login"],
                "password": data["password"],
                "comment": comment,
            },
            "updated_at": now_iso(),
        }
    )
    save_entity(ORDERS_DIR, order["id"], order)
    save_entity(SERVERS_DIR, server_id, server)
    await state.clear()
    await message.answer(
        f"✅ Заказ <code>{order['id']}</code> отмечен как готовый.\n"
        f"Сервер <code>{server_id}</code> сохранён.",
        reply_markup=admin_menu_keyboard(),
    )
    try:
        await message.bot.send_message(
            int(order["user_id"]),
            "🎉 <b>Сервер готов!</b>\n\n"
            f"Заказ: <code>{order['id']}</code>\n"
            f"IP: <code>{text(data['ip'])}</code>\n"
            f"Порт: <code>{text(data['port'])}</code>\n"
            f"Логин: <code>{text(data['login'])}</code>\n"
            f"Пароль: <code>{text(data['password'])}</code>\n"
            f"Комментарий: {text(comment)}",
        )
    except (TelegramForbiddenError, TelegramBadRequest):
        logger.info("User %s cannot be notified", order.get("user_id"))


@router.callback_query(F.data == "admin:tariffs")
async def admin_tariffs(callback: CallbackQuery) -> None:
    if not only_admin(callback.from_user.id):
        await callback.answer("Доступ запрещён.", show_alert=True)
        return
    tariffs = list_json(TARIFFS_DIR)
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="➕ Добавить тариф", callback_data="admin:tariff_add"))
    for tariff in tariffs:
        builder.row(
            InlineKeyboardButton(
                text=f"✏️ {tariff.get('name', tariff.get('id'))}"[:60],
                callback_data=f"admin:tariff_edit:{tariff.get('id')}",
            ),
            InlineKeyboardButton(
                text="🗑",
                callback_data=f"admin:tariff_delete:{tariff.get('id')}",
            ),
        )
    builder.row(InlineKeyboardButton(text="⬅️ В админ-панель", callback_data="admin:menu"))
    if tariffs:
        details = "\n\n".join(
            f"<b>{text(t.get('name'))}</b>\n{text(t.get('description'))}\n"
            f"1 мес: {money(t.get('prices', {}).get('1'))} · "
            f"3 мес: {money(t.get('prices', {}).get('3'))} · "
            f"5 мес: {money(t.get('prices', {}).get('5'))}"
            for t in tariffs
        )
        body = "💳 <b>Тарифы</b>\n\n" + details
    else:
        body = "💳 <b>Тарифы</b>\n\nТарифов пока нет. Добавьте первый тариф."
    await edit_callback(callback, body, builder.as_markup())


async def begin_tariff_wizard(
    callback: CallbackQuery, state: FSMContext, tariff: dict[str, Any] | None = None
) -> None:
    await state.clear()
    if tariff:
        await state.update_data(mode="edit", tariff_id=tariff["id"])
    else:
        await state.update_data(mode="add")
    await state.set_state(TariffStates.name)
    prompt = (
        "✏️ Редактирование тарифа\n\nВведите название:"
        if tariff
        else "➕ Добавление тарифа\n\nВведите название:"
    )
    await callback.message.answer(prompt + "\nДля отмены используйте /cancel.")
    await callback.answer()


@router.callback_query(F.data == "admin:tariff_add")
async def tariff_add(callback: CallbackQuery, state: FSMContext) -> None:
    if not only_admin(callback.from_user.id):
        await callback.answer("Доступ запрещён.", show_alert=True)
        return
    await begin_tariff_wizard(callback, state)


@router.callback_query(F.data.startswith("admin:tariff_edit:"))
async def tariff_edit(callback: CallbackQuery, state: FSMContext) -> None:
    if not only_admin(callback.from_user.id):
        await callback.answer("Доступ запрещён.", show_alert=True)
        return
    tariff = get_by_id(TARIFFS_DIR, callback.data.split(":", 2)[2])
    if not tariff:
        await callback.answer("Тариф не найден.", show_alert=True)
        return
    await begin_tariff_wizard(callback, state, tariff)


@router.message(TariffStates.name, F.text)
async def tariff_name(message: Message, state: FSMContext) -> None:
    if not only_admin(message.from_user.id):
        return
    await state.update_data(name=message.text.strip())
    await state.set_state(TariffStates.description)
    await message.answer("Введите краткие характеристики тарифа (CPU, RAM, диск, трафик):")


@router.message(TariffStates.description, F.text)
async def tariff_description(message: Message, state: FSMContext) -> None:
    if not only_admin(message.from_user.id):
        return
    await state.update_data(description=message.text.strip())
    await state.set_state(TariffStates.cpu)
    await message.answer("Введите CPU (например, 2 vCPU):")


@router.message(TariffStates.cpu, F.text)
async def tariff_cpu(message: Message, state: FSMContext) -> None:
    if not only_admin(message.from_user.id):
        return
    await state.update_data(cpu=message.text.strip())
    await state.set_state(TariffStates.ram)
    await message.answer("Введите RAM (например, 4 GB):")


@router.message(TariffStates.ram, F.text)
async def tariff_ram(message: Message, state: FSMContext) -> None:
    if not only_admin(message.from_user.id):
        return
    await state.update_data(ram=message.text.strip())
    await state.set_state(TariffStates.disk)
    await message.answer("Введите объём диска (например, 50 GB SSD):")


@router.message(TariffStates.disk, F.text)
async def tariff_disk(message: Message, state: FSMContext) -> None:
    if not only_admin(message.from_user.id):
        return
    await state.update_data(disk=message.text.strip())
    await state.set_state(TariffStates.bandwidth)
    await message.answer("Введите канал/трафик (например, 1 Gbps / безлимит):")


@router.message(TariffStates.bandwidth, F.text)
async def tariff_bandwidth(message: Message, state: FSMContext) -> None:
    if not only_admin(message.from_user.id):
        return
    await state.update_data(bandwidth=message.text.strip())
    await state.set_state(TariffStates.price_1)
    await message.answer("Введите цену за 1 месяц в рублях:")


async def store_tariff_price(
    message: Message, state: FSMContext, period: int, next_state: State | None
) -> None:
    try:
        value = parse_price(message.text)
    except ValueError:
        await message.answer("Введите корректную неотрицательную цену, например 499 или 499.90:")
        return
    await state.update_data(**{f"price_{period}": value})
    if next_state:
        await state.set_state(next_state)
        next_period = 3 if period == 1 else 5
        await message.answer(f"Введите цену за {next_period} месяца(ев) в рублях:")
    else:
        await finish_tariff(message, state)


@router.message(TariffStates.price_1, F.text)
async def tariff_price_1(message: Message, state: FSMContext) -> None:
    if not only_admin(message.from_user.id):
        return
    await store_tariff_price(message, state, 1, TariffStates.price_3)


@router.message(TariffStates.price_3, F.text)
async def tariff_price_3(message: Message, state: FSMContext) -> None:
    if not only_admin(message.from_user.id):
        return
    await store_tariff_price(message, state, 3, TariffStates.price_5)


@router.message(TariffStates.price_5, F.text)
async def tariff_price_5(message: Message, state: FSMContext) -> None:
    if not only_admin(message.from_user.id):
        return
    await store_tariff_price(message, state, 5, None)


async def finish_tariff(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    tariff_id = data.get("tariff_id") or f"TAR-{uuid.uuid4().hex[:8].upper()}"
    tariff = {
        "id": tariff_id,
        "name": data["name"],
        "description": data["description"],
        "cpu": data["cpu"],
        "ram": data["ram"],
        "disk": data["disk"],
        "bandwidth": data["bandwidth"],
        "prices": {
            "1": data["price_1"],
            "3": data["price_3"],
            "5": data["price_5"],
        },
        "updated_at": now_iso(),
    }
    save_entity(TARIFFS_DIR, tariff_id, tariff)
    await state.clear()
    await message.answer(
        f"✅ Тариф «{html.escape(tariff['name'])}» сохранён.",
        reply_markup=admin_menu_keyboard(),
    )


@router.callback_query(F.data.startswith("admin:tariff_delete:"))
async def tariff_delete_prompt(callback: CallbackQuery) -> None:
    if not only_admin(callback.from_user.id):
        await callback.answer("Доступ запрещён.", show_alert=True)
        return
    tariff_id = callback.data.split(":", 2)[2]
    tariff = get_by_id(TARIFFS_DIR, tariff_id)
    if not tariff:
        await callback.answer("Тариф не найден.", show_alert=True)
        return
    markup = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Да, удалить",
                    callback_data=f"admin:tariff_delete_yes:{tariff_id}",
                ),
                InlineKeyboardButton(
                    text="Отмена", callback_data="admin:tariffs"
                ),
            ]
        ]
    )
    await edit_callback(
        callback,
        f"⚠️ Удалить тариф «{text(tariff.get('name'))}»?\n"
        "Новые заказы больше не смогут его выбрать. Старые заказы не изменятся.",
        markup,
    )


@router.callback_query(F.data.startswith("admin:tariff_delete_yes:"))
async def tariff_delete_confirm(callback: CallbackQuery) -> None:
    if not only_admin(callback.from_user.id):
        await callback.answer("Доступ запрещён.", show_alert=True)
        return
    tariff_id = callback.data.split(":", 2)[2]
    delete_entity(TARIFFS_DIR, tariff_id)
    await callback.answer("Тариф удалён.")
    await admin_tariffs(callback)


@router.callback_query(F.data == "admin:settings")
async def admin_settings(callback: CallbackQuery) -> None:
    if not only_admin(callback.from_user.id):
        await callback.answer("Доступ запрещён.", show_alert=True)
        return
    current = settings().get("payment_requisites", "не настроены")
    markup = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✏️ Изменить реквизиты", callback_data="admin:set_requisites")],
            [InlineKeyboardButton(text="⬅️ В админ-панель", callback_data="admin:menu")],
        ]
    )
    await edit_callback(
        callback,
        f"💰 <b>Реквизиты для оплаты</b>\n\n{text(current)}",
        markup,
    )


@router.callback_query(F.data == "admin:set_requisites")
async def admin_set_requisites(callback: CallbackQuery, state: FSMContext) -> None:
    if not only_admin(callback.from_user.id):
        await callback.answer("Доступ запрещён.", show_alert=True)
        return
    await state.set_state(SettingsStates.requisites)
    await callback.message.answer(
        "Введите реквизиты для оплаты одним сообщением.\n"
        "Переносы строк сохранятся. Для отмены используйте /cancel."
    )
    await callback.answer()


@router.message(SettingsStates.requisites, F.text)
async def save_requisites(message: Message, state: FSMContext) -> None:
    if not only_admin(message.from_user.id):
        return
    current = settings()
    current["payment_requisites"] = message.text.strip()
    current["updated_at"] = now_iso()
    atomic_write(SETTINGS_FILE, current)
    await state.clear()
    await message.answer("✅ Реквизиты обновлены.", reply_markup=admin_menu_keyboard())


@router.callback_query(F.data == "admin:stats")
async def admin_stats(callback: CallbackQuery) -> None:
    if not only_admin(callback.from_user.id):
        await callback.answer("Доступ запрещён.", show_alert=True)
        return
    users = list_json(USERS_DIR)
    orders = list_json(ORDERS_DIR)
    servers = list_json(SERVERS_DIR)
    paid = [o for o in orders if o.get("payment_status") == "Оплачено"]
    income = sum(float(o.get("price", 0) or 0) for o in paid)
    active = [o for o in orders if o.get("status") in {"Заказан", "Настройка"}]
    body = (
        "📊 <b>Статистика</b>\n\n"
        f"Пользователей: <b>{len(users)}</b>\n"
        f"Всего заказов: <b>{len(orders)}</b>\n"
        f"Активных заказов: <b>{len(active)}</b>\n"
        f"Оплаченных заказов: <b>{len(paid)}</b>\n"
        f"Доход по подтверждённым оплатам: <b>{money(income)}</b>\n"
        f"Готовых серверов: <b>{len(servers)}</b>"
    )
    await edit_callback(callback, body, back_keyboard("admin:menu"))


@router.callback_query(F.data == "admin:servers")
async def admin_servers(callback: CallbackQuery) -> None:
    if not only_admin(callback.from_user.id):
        await callback.answer("Доступ запрещён.", show_alert=True)
        return
    servers = list_json(SERVERS_DIR)
    if not servers:
        body = "🖥 <b>Все серверы</b>\n\nГотовых серверов пока нет."
    else:
        blocks = []
        for server in servers:
            blocks.append(
                "\n".join(
                    [
                        f"<b>{text(server.get('id'))}</b> · пользователь <code>{text(server.get('user_id'))}</code>",
                        f"Регион: {text(REGIONS.get(server.get('region')))}",
                        f"IP: <code>{text(server.get('ip'))}</code> · порт: <code>{text(server.get('port'))}</code>",
                        f"Логин: <code>{text(server.get('login'))}</code>",
                        f"Пароль: <code>{text(server.get('password'))}</code>",
                        f"Решение: {text(SOLUTIONS.get(server.get('solution')))}",
                        f"Разрешение 20%: {'Да' if server.get('use_permission') else 'Нет'}",
                        f"Комментарий: {text(server.get('comment'))}",
                    ]
                )
            )
        body = "🖥 <b>Все арендованные серверы</b>\n\n" + "\n\n".join(blocks)
    await edit_callback(callback, body, back_keyboard("admin:menu"))


async def main() -> None:
    global ADMIN_ID
    if not os.getenv("BOT_TOKEN", "").strip():
        raise RuntimeError("BOT_TOKEN is not configured. Run ./start.sh first.")
    ADMIN_ID = required_admin_id()
    ensure_storage()
    bot = Bot(
        token=os.environ["BOT_TOKEN"],
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dispatcher = Dispatcher()
    dispatcher.include_router(router)
    logger.info("ButovskyHOST bot started; admin id: %s", ADMIN_ID)
    try:
        await bot.delete_webhook(drop_pending_updates=False)
        await dispatcher.start_polling(bot)
    finally:
        await bot.session.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (RuntimeError, KeyboardInterrupt) as error:
        logger.error("%s", error)