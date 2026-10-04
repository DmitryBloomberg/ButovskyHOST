from __future__ import annotations

import asyncio
import html
import logging
import secrets
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from aiogram import BaseMiddleware, Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

from config import BASE_DIR, load_settings
from keyboards import admin_menu, back_to_menu, page_buttons, user_menu
from storage import JsonStorage, utc_now


logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("butovskyhost")
settings = load_settings()
store = JsonStorage(BASE_DIR / "data", settings.admin_ids)
router = Router()

ACTIVE_ORDER_STATES = {
    "requested",
    "in_work",
    "awaiting_payment",
    "receipt_submitted",
    "collecting_server",
}
RECEIPT_STATES = {"awaiting_payment"}
PAGE_SIZE = 8


def esc(value: Any) -> str:
    return html.escape(str(value if value is not None else ""), quote=True)


def display_name(user: dict[str, Any]) -> str:
    full = " ".join(part for part in [user.get("first_name", ""), user.get("last_name", "")] if part)
    return full or "Без имени"


def tariff_text(tariff: dict[str, Any]) -> str:
    return (
        f"<b>{esc(tariff.get('name'))}</b>\n"
        f"CPU: {esc(tariff.get('cpu'))} vCPU · RAM: {esc(tariff.get('ram'))} ГБ · "
        f"Диск: {esc(tariff.get('disk'))} ГБ\n"
        f"Цена: <b>{esc(tariff.get('price'))} ₽ / месяц</b>\n"
        f"{esc(tariff.get('description'))}"
    )


def user_details(user: dict[str, Any]) -> str:
    username = f"@{esc(user.get('username'))}" if user.get("username") else "не указан"
    status = {"A": "Администратор", "U": "Пользователь", "B": "Заблокирован"}.get(
        user.get("status"), "Пользователь"
    )
    return (
        f"<b>Пользователь</b>\n"
        f"ID: <code>{esc(user.get('telegram_id'))}</code>\n"
        f"Имя: {esc(display_name(user))}\n"
        f"Тег: {username}\n"
        f"Статус: {status}\n"
        f"Баланс: {esc(user.get('balance', 0))} ₽\n"
        f"Регистрация: {esc(user.get('created_at', 'неизвестно')[:10])}"
    )


def order_details(order: dict[str, Any], user: dict[str, Any] | None = None) -> str:
    user = user or {}
    tariff = order.get("tariff", {})
    username = f"@{esc(user.get('username'))}" if user.get("username") else "не указан"
    text = (
        f"<b>Заказ #{esc(order.get('id'))}</b>\n"
        f"Статус: <code>{esc(order.get('status'))}</code>\n"
        f"Создан: {esc(order.get('created_at', '')[:19].replace('T', ' '))} UTC\n\n"
        f"<b>Покупатель</b>\n"
        f"ID: <code>{esc(order.get('user_id'))}</code>\n"
        f"Имя: {esc(display_name(user))}\n"
        f"Тег: {username}\n"
        f"Статус: {esc({'A': 'администратор', 'U': 'пользователь', 'B': 'заблокирован'}.get(user.get('status'), 'пользователь'))}\n"
        f"Баланс: {esc(user.get('balance', 0))} ₽\n\n"
        f"<b>Тариф</b>\n"
        f"{esc(tariff.get('name'))} — {esc(tariff.get('price'))} ₽ / месяц\n"
        f"CPU {esc(tariff.get('cpu'))} vCPU · RAM {esc(tariff.get('ram'))} ГБ · "
        f"диск {esc(tariff.get('disk'))} ГБ\n"
        f"Описание: {esc(tariff.get('description'))}\n"
        f"Разрешение на использование 20% мощности: "
        f"{'да' if order.get('consent_20_percent') else 'нет'}"
    )
    server = order.get("server")
    if server:
        text += (
            f"\n\n<b>Данные сервера</b>\n"
            f"IP: <code>{esc(server.get('ip'))}</code>\n"
            f"Логин: <code>{esc(server.get('username'))}</code>\n"
            f"Пароль: зашифрован при хранении; доступен пользователю через раздел «Сервера»"
        )
    return text


def order_admin_keyboard(order: dict[str, Any]):
    order_id = order["id"]
    status = order.get("status")
    rows = []
    if status == "requested":
        rows.append(
            [
                {"text": "✅ Взять в работу", "callback_data": f"a:take:{order_id}"},
                {"text": "❌ Отменить", "callback_data": f"a:cancel:{order_id}"},
            ]
        )
    elif status == "in_work":
        rows.append([{"text": "💳 Запросить оплату", "callback_data": f"a:pay:{order_id}"}])
    elif status == "receipt_submitted":
        rows.append(
            [
                {"text": "✅ Подтвердить и начать", "callback_data": f"a:start:{order_id}"},
                {"text": "↩️ Отклонить чек", "callback_data": f"a:reject:{order_id}"},
            ]
        )
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=item["text"], callback_data=item["callback_data"]) for item in row]
            for row in rows
        ]
    )


class TariffForm(StatesGroup):
    name = State()
    cpu = State()
    ram = State()
    disk = State()
    price = State()
    description = State()


class ServerForm(StatesGroup):
    ip = State()
    username = State()
    password = State()


class AccessMiddleware(BaseMiddleware):
    async def __call__(self, handler, event, data):
        actor = getattr(event, "from_user", None)
        if actor is None or actor.id in settings.admin_ids:
            return await handler(event, data)
        # /start is allowed to show a clear blocked notice.
        if isinstance(event, Message) and event.text and event.text.startswith("/start"):
            return await handler(event, data)
        user = await store.get_user(actor.id)
        if user and user.get("status") == "B":
            if isinstance(event, CallbackQuery):
                await event.answer("Доступ заблокирован администратором.", show_alert=True)
            elif isinstance(event, Message):
                await event.answer("Доступ к боту заблокирован администратором.")
            return None
        return await handler(event, data)


router.message.middleware(AccessMiddleware())
router.callback_query.middleware(AccessMiddleware())


async def send_menu(message: Message, user: dict[str, Any], *, edit: bool = False) -> None:
    text = (
        "<b>ButovskyHOST — игровой и VPS-хостинг</b>\n\n"
        "• Подбор тарифа под вашу задачу\n"
        "• Прозрачная стоимость и характеристики\n"
        "• Помощь администратора на каждом этапе\n"
        "• Данные готового сервера доступны в профиле заказа\n\n"
        "<i>После покупки сервис Butovsky может использовать ровно 20% мощности "
        "вашего хостинга только с вашего согласия.</i>\n\n"
        "Выберите раздел:"
    )
    if edit:
        try:
            await message.edit_text(text, reply_markup=user_menu(user.get("status") == "A"))
        except TelegramBadRequest:
            await message.answer(text, reply_markup=user_menu(user.get("status") == "A"))
    else:
        await message.answer(text, reply_markup=user_menu(user.get("status") == "A"))


async def is_admin(actor_id: int) -> bool:
    if actor_id in settings.admin_ids:
        return True
    user = await store.get_user(actor_id)
    return bool(user and user.get("status") == "A")


async def get_user_or_empty(telegram_id: int) -> dict[str, Any]:
    return (await store.get_user(telegram_id)) or {
        "telegram_id": telegram_id,
        "first_name": "",
        "last_name": "",
        "username": "",
        "status": "U",
        "balance": 0,
    }


async def safe_send(bot: Bot, chat_id: int, text: str, **kwargs) -> bool:
    try:
        await bot.send_message(chat_id, text, **kwargs)
        return True
    except (TelegramForbiddenError, TelegramBadRequest) as exc:
        logger.warning("Could not send bot message to chat %s: %s", chat_id, exc.__class__.__name__)
        return False


async def notify_admins(bot: Bot, text: str, reply_markup=None) -> None:
    for admin_id in settings.admin_ids:
        await safe_send(bot, admin_id, text, reply_markup=reply_markup)


async def notify_order_admins(bot: Bot, order: dict[str, Any]) -> None:
    user = await get_user_or_empty(order["user_id"])
    await notify_admins(bot, order_details(order, user), reply_markup=order_admin_keyboard(order))


async def send_tariff_page(message: Message, page: int) -> None:
    tariffs = await store.list_tariffs(active_only=True)
    if not tariffs:
        await message.answer("Пока нет доступных тарифов. Загляните позже.", reply_markup=back_to_menu())
        return
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

    pages = max(1, (len(tariffs) + PAGE_SIZE - 1) // PAGE_SIZE)
    page = min(max(page, 0), pages - 1)
    shown = tariffs[page * PAGE_SIZE : (page + 1) * PAGE_SIZE]
    buttons = [
        [
            InlineKeyboardButton(
                text=f"{str(t['name'])[:38]} · {t['price']} ₽/мес",
                callback_data=f"u:tariff:{t['id']}",
            )
        ]
        for t in shown
    ]
    nav = page_buttons("u:tariffs", page, len(tariffs), PAGE_SIZE)
    if nav:
        buttons.append(nav)
    buttons.append([InlineKeyboardButton(text="⬅️ Главное меню", callback_data="u:menu")])
    await message.answer(
        f"Выберите тариф (страница {page + 1} из {pages}):",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
    )


async def reveal_server(order: dict[str, Any]) -> str:
    server = order.get("server", {})
    try:
        password = settings.fernet.decrypt(server.get("password_enc", "").encode()).decode()
    except Exception:
        password = "Не удалось расшифровать пароль. Проверьте FERNET_KEY из первоначальной установки."
    return (
        f"<b>Сервер по заказу #{esc(order.get('id'))}</b>\n"
        f"Тариф: {esc(order.get('tariff', {}).get('name'))}\n"
        f"IP: <code>{esc(server.get('ip'))}</code>\n"
        f"Логин: <code>{esc(server.get('username'))}</code>\n"
        f"Пароль: <code>{esc(password)}</code>\n\n"
        "Не пересылайте эти данные посторонним."
    )


@router.message(CommandStart())
async def start(message: Message, state: FSMContext) -> None:
    await state.clear()
    user = await store.get_or_create_user(message.from_user)
    if user.get("status") == "B":
        await message.answer("Доступ к боту заблокирован администратором.")
        return
    await send_menu(message, user)


@router.callback_query(F.data == "u:menu")
async def show_menu(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    user = await store.get_or_create_user(callback.from_user)
    await callback.answer()
    await send_menu(callback.message, user, edit=True)


@router.callback_query(F.data == "u:order")
async def show_tariffs(callback: CallbackQuery) -> None:
    await callback.answer()
    await send_tariff_page(callback.message, 0)


@router.callback_query(F.data.startswith("u:tariffs:"))
async def show_tariff_page(callback: CallbackQuery) -> None:
    try:
        page = max(0, int(callback.data.split(":")[-1]))
    except ValueError:
        await callback.answer("Некорректная страница.", show_alert=True)
        return
    await callback.answer()
    await send_tariff_page(callback.message, page)


@router.callback_query(F.data.startswith("u:tariff:"))
async def choose_tariff(callback: CallbackQuery, state: FSMContext) -> None:
    tariff_id = callback.data.split(":")[-1]
    tariff = await store.get_tariff(tariff_id)
    await callback.answer()
    if not tariff or not tariff.get("active", True):
        await callback.message.answer("Этот тариф больше не доступен.", reply_markup=back_to_menu())
        return
    await state.update_data(tariff_id=tariff_id)
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Да", callback_data=f"u:consent:yes:{tariff_id}"),
                InlineKeyboardButton(text="❌ Нет", callback_data=f"u:consent:no:{tariff_id}"),
            ]
        ]
    )
    await callback.message.answer(tariff_text(tariff))
    await callback.message.answer(
        "После приобретения хостинга вы разрешаете сервису Butovsky подключаться "
        "к вашему хостингу и использовать ровно 20% его мощности?",
        reply_markup=keyboard,
    )


@router.callback_query(F.data.startswith("u:consent:"))
async def consent(callback: CallbackQuery) -> None:
    _, _, answer, tariff_id = callback.data.split(":")
    await callback.answer()
    if answer != "yes":
        await callback.message.answer(
            "Без этого разрешения оформить заказ нельзя. Ничего не заказано.",
            reply_markup=back_to_menu(),
        )
        return
    tariff = await store.get_tariff(tariff_id)
    if not tariff or not tariff.get("active", True):
        await callback.message.answer("Тариф больше не доступен.", reply_markup=back_to_menu())
        return
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="🛒 Заказать", callback_data=f"u:confirm:{tariff_id}"),
                InlineKeyboardButton(text="Отмена", callback_data="u:menu"),
            ]
        ]
    )
    await callback.message.answer(
        "<b>Проверьте настройки заказа</b>\n\n"
        f"{tariff_text(tariff)}\n\n"
        "Разрешение на использование 20% мощности: <b>дано</b>\n"
        "После подтверждения заказ поступит администратору. Оплата проводится вручную.",
        reply_markup=keyboard,
    )


@router.callback_query(F.data.startswith("u:confirm:"))
async def confirm_order(callback: CallbackQuery, bot: Bot) -> None:
    tariff_id = callback.data.split(":")[-1]
    tariff = await store.get_tariff(tariff_id)
    await callback.answer()
    if not tariff or not tariff.get("active", True):
        await callback.message.answer("Тариф больше не доступен.", reply_markup=back_to_menu())
        return
    order_id = secrets.token_hex(5).upper()
    order = {
        "id": order_id,
        "user_id": callback.from_user.id,
        "tariff": {key: tariff.get(key) for key in ("id", "name", "cpu", "ram", "disk", "price", "description")},
        "consent_20_percent": True,
        "status": "requested",
        "created_at": utc_now(),
        "updated_at": utc_now(),
    }
    await store.create_order(order)
    await callback.message.answer(
        f"Заказ <b>#{esc(order_id)}</b> отправлен администратору. "
        "Вы получите сообщение, когда его возьмут в работу.",
        reply_markup=back_to_menu(),
    )
    await notify_order_admins(bot, order)


@router.callback_query(F.data == "u:profile")
async def profile(callback: CallbackQuery) -> None:
    user = await store.get_or_create_user(callback.from_user)
    await callback.answer()
    await callback.message.answer(user_details(user), reply_markup=back_to_menu())


@router.callback_query(F.data == "u:orders")
@router.callback_query(F.data.startswith("u:orders:"))
async def user_orders(callback: CallbackQuery) -> None:
    parts = callback.data.split(":")
    try:
        page = max(0, int(parts[-1])) if len(parts) > 2 else 0
    except ValueError:
        await callback.answer("Некорректная страница.", show_alert=True)
        return
    orders = [
        order
        for order in await store.list_orders()
        if order.get("user_id") == callback.from_user.id and order.get("status") in ACTIVE_ORDER_STATES
    ]
    await callback.answer()
    if not orders:
        await callback.message.answer("Активных заказов пока нет.", reply_markup=back_to_menu())
        return
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

    pages = max(1, (len(orders) + PAGE_SIZE - 1) // PAGE_SIZE)
    page = min(page, pages - 1)
    shown = orders[page * PAGE_SIZE : (page + 1) * PAGE_SIZE]
    buttons = [
        [
            InlineKeyboardButton(
                text=f"#{order['id']} · {order.get('tariff', {}).get('name', '')[:24]} · {order.get('status')}",
                callback_data=f"u:orderdetail:{order['id']}",
            )
        ]
        for order in shown
    ]
    nav = page_buttons("u:orders", page, len(orders), PAGE_SIZE)
    if nav:
        buttons.append(nav)
    buttons.append([InlineKeyboardButton(text="⬅️ Главное меню", callback_data="u:menu")])
    await callback.message.answer(
        f"<b>Ваши активные заказы</b> · страница {page + 1} из {pages}",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
    )


@router.callback_query(F.data.startswith("u:orderdetail:"))
async def user_order_detail(callback: CallbackQuery) -> None:
    order = await store.get_order(callback.data.split(":")[-1])
    await callback.answer()
    if not order or order.get("user_id") != callback.from_user.id:
        await callback.message.answer("Заказ не найден.", reply_markup=back_to_menu())
        return
    user = await get_user_or_empty(callback.from_user.id)
    await callback.message.answer(order_details(order, user), reply_markup=back_to_menu())


@router.callback_query(F.data.startswith("u:servers:"))
async def user_servers(callback: CallbackQuery) -> None:
    page = max(0, int(callback.data.split(":")[-1]))
    orders = [
        order
        for order in await store.list_orders()
        if order.get("user_id") == callback.from_user.id and order.get("status") == "completed" and order.get("server")
    ]
    await callback.answer()
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

    buttons = []
    if orders:
        current = min(page, len(orders) - 1)
        buttons.append(
            [InlineKeyboardButton(text=f"🖥 Сервер {current + 1} из {len(orders)}", callback_data=f"u:server:{current}")]
        )
        buttons.extend([page_buttons("u:servers", page, len(orders), 1)] if len(orders) > 1 else [])
    buttons.append([InlineKeyboardButton(text="⬅️ Главное меню", callback_data="u:menu")])
    if not orders:
        await callback.message.answer("Готовых серверов пока нет.", reply_markup=back_to_menu())
        return
    await callback.message.answer(
        f"У вас {len(orders)} сервер(ов). Нажмите кнопку, чтобы открыть данные.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
    )


@router.callback_query(F.data.startswith("u:server:"))
async def show_user_server(callback: CallbackQuery) -> None:
    try:
        index = int(callback.data.split(":")[-1])
    except ValueError:
        await callback.answer("Некорректная страница.", show_alert=True)
        return
    orders = [
        order
        for order in await store.list_orders()
        if order.get("user_id") == callback.from_user.id and order.get("status") == "completed" and order.get("server")
    ]
    await callback.answer()
    if index < 0 or index >= len(orders):
        await callback.message.answer("Сервер не найден.", reply_markup=back_to_menu())
        return
    await callback.message.answer(await reveal_server(orders[index]), reply_markup=back_to_menu())


@router.callback_query(F.data == "a:panel")
async def admin_panel(callback: CallbackQuery, state: FSMContext) -> None:
    if not await is_admin(callback.from_user.id):
        await callback.answer("Нет доступа.", show_alert=True)
        return
    await state.clear()
    await callback.answer()
    await callback.message.answer("<b>Панель администратора</b>", reply_markup=admin_menu())


@router.callback_query(F.data.startswith("a:users:"))
async def admin_users(callback: CallbackQuery) -> None:
    if not await is_admin(callback.from_user.id):
        await callback.answer("Нет доступа.", show_alert=True)
        return
    page = max(0, int(callback.data.split(":")[-1]))
    users = await store.list_users()
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

    start_index = page * PAGE_SIZE
    shown = users[start_index : start_index + PAGE_SIZE]
    buttons = [
        [
            InlineKeyboardButton(
                text=f"{display_name(user)} · {user.get('telegram_id')} · {user.get('status')}",
                callback_data=f"a:user:{user['telegram_id']}",
            )
        ]
        for user in shown
    ]
    nav = page_buttons("a:users", page, len(users), PAGE_SIZE)
    if nav:
        buttons.append(nav)
    buttons.append([InlineKeyboardButton(text="⬅️ Админ-меню", callback_data="a:panel")])
    await callback.answer()
    await callback.message.answer(
        f"<b>Пользователи</b> · всего: {len(users)}",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
    )


@router.callback_query(F.data.startswith("a:user:"))
async def admin_user_detail(callback: CallbackQuery) -> None:
    if not await is_admin(callback.from_user.id):
        await callback.answer("Нет доступа.", show_alert=True)
        return
    try:
        telegram_id = int(callback.data.split(":")[-1])
    except ValueError:
        await callback.answer("Некорректный пользователь.", show_alert=True)
        return
    user = await store.get_user(telegram_id)
    await callback.answer()
    if not user:
        await callback.message.answer("Пользователь не найден.")
        return
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

    rows = []
    if telegram_id not in settings.admin_ids:
        if user.get("status") == "B":
            rows.append([InlineKeyboardButton(text="✅ Разблокировать", callback_data=f"a:unblock:{telegram_id}")])
        else:
            rows.append([InlineKeyboardButton(text="🚫 Заблокировать", callback_data=f"a:block:{telegram_id}")])
    rows.extend(
        [
            [InlineKeyboardButton(text="⬅️ К пользователям", callback_data="a:users:0")],
            [InlineKeyboardButton(text="⬅️ Админ-меню", callback_data="a:panel")],
        ]
    )
    await callback.message.answer(user_details(user), reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


@router.callback_query(F.data.startswith("a:block:"))
@router.callback_query(F.data.startswith("a:unblock:"))
async def set_user_block(callback: CallbackQuery, bot: Bot) -> None:
    if not await is_admin(callback.from_user.id):
        await callback.answer("Нет доступа.", show_alert=True)
        return
    action, raw_id = callback.data.split(":")[1:]
    telegram_id = int(raw_id)
    status = "B" if action == "block" else "U"
    user = await store.set_user_status(telegram_id, status)
    await callback.answer("Статус обновлён." if user else "Пользователь не найден.")
    if user:
        await safe_send(
            bot,
            telegram_id,
            "Доступ к боту заблокирован администратором."
            if status == "B"
            else "Доступ к боту восстановлен. Нажмите /start.",
        )
        await callback.message.answer(user_details(user))


@router.callback_query(F.data == "a:tariffs")
async def admin_tariffs(callback: CallbackQuery, state: FSMContext) -> None:
    if not await is_admin(callback.from_user.id):
        await callback.answer("Нет доступа.", show_alert=True)
        return
    await state.clear()
    tariffs = await store.list_tariffs()
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

    rows = [
        [
            InlineKeyboardButton(
                text=f"{t.get('name')} · {'активен' if t.get('active', True) else 'скрыт'}",
                callback_data=f"a:toggle_tariff:{t['id']}",
            )
        ]
        for t in tariffs
    ]
    rows.extend(
        [
            [InlineKeyboardButton(text="➕ Создать тариф", callback_data="a:new_tariff")],
            [InlineKeyboardButton(text="⬅️ Админ-меню", callback_data="a:panel")],
        ]
    )
    await callback.answer()
    await callback.message.answer(
        "Нажмите тариф, чтобы включить/скрыть его. Скрытый тариф не доступен для новых заказов.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )
    if not tariffs:
        await callback.message.answer("Тарифов пока нет.")


@router.callback_query(F.data.startswith("a:toggle_tariff:"))
async def toggle_tariff(callback: CallbackQuery) -> None:
    if not await is_admin(callback.from_user.id):
        await callback.answer("Нет доступа.", show_alert=True)
        return
    tariff = await store.toggle_tariff(callback.data.split(":")[-1])
    await callback.answer("Статус тарифа изменён." if tariff else "Тариф не найден.")
    if tariff:
        await callback.message.answer(
            f"Тариф «{esc(tariff.get('name'))}» теперь "
            f"{'активен' if tariff.get('active') else 'скрыт'}.",
            reply_markup=back_to_menu(),
        )


@router.callback_query(F.data == "a:new_tariff")
async def new_tariff(callback: CallbackQuery, state: FSMContext) -> None:
    if not await is_admin(callback.from_user.id):
        await callback.answer("Нет доступа.", show_alert=True)
        return
    await state.clear()
    await state.set_state(TariffForm.name)
    await callback.answer()
    await callback.message.answer("Введите название тарифа (до 60 символов):")


@router.message(TariffForm.name)
async def tariff_name(message: Message, state: FSMContext) -> None:
    value = (message.text or "").strip()
    if not value or len(value) > 60:
        await message.answer("Название должно содержать от 1 до 60 символов.")
        return
    await state.update_data(name=value)
    await state.set_state(TariffForm.cpu)
    await message.answer("Укажите количество vCPU (целое число, например 2):")


@router.message(TariffForm.cpu)
async def tariff_cpu(message: Message, state: FSMContext) -> None:
    value = (message.text or "").strip()
    if not value.isdigit() or not 1 <= int(value) <= 256:
        await message.answer("Введите целое число vCPU от 1 до 256.")
        return
    await state.update_data(cpu=int(value))
    await state.set_state(TariffForm.ram)
    await message.answer("Укажите RAM в ГБ (целое число):")


@router.message(TariffForm.ram)
async def tariff_ram(message: Message, state: FSMContext) -> None:
    value = (message.text or "").strip()
    if not value.isdigit() or not 1 <= int(value) <= 4096:
        await message.answer("Введите RAM целым числом от 1 до 4096 ГБ.")
        return
    await state.update_data(ram=int(value))
    await state.set_state(TariffForm.disk)
    await message.answer("Укажите размер диска в ГБ (целое число):")


@router.message(TariffForm.disk)
async def tariff_disk(message: Message, state: FSMContext) -> None:
    value = (message.text or "").strip()
    if not value.isdigit() or not 1 <= int(value) <= 100000:
        await message.answer("Введите размер диска целым числом от 1 до 100000 ГБ.")
        return
    await state.update_data(disk=int(value))
    await state.set_state(TariffForm.price)
    await message.answer("Укажите цену в рублях за месяц (целое число):")


@router.message(TariffForm.price)
async def tariff_price(message: Message, state: FSMContext) -> None:
    value = (message.text or "").strip()
    if not value.isdigit() or not 1 <= int(value) <= 100000000:
        await message.answer("Введите цену целым числом от 1 до 100000000 ₽.")
        return
    await state.update_data(price=int(value))
    await state.set_state(TariffForm.description)
    await message.answer("Кратко опишите тариф (до 500 символов):")


@router.message(TariffForm.description)
async def tariff_description(message: Message, state: FSMContext) -> None:
    description = (message.text or "").strip()
    if not description or len(description) > 500:
        await message.answer("Описание должно содержать от 1 до 500 символов.")
        return
    data = await state.get_data()
    tariff = {
        **data,
        "id": secrets.token_hex(4),
        "description": description,
        "active": True,
        "created_at": utc_now(),
    }
    await store.add_tariff(tariff)
    await state.clear()
    await message.answer(
        f"Тариф создан:\n\n{tariff_text(tariff)}",
        reply_markup=admin_menu(),
    )


@router.callback_query(F.data.startswith("a:orders:"))
async def admin_orders(callback: CallbackQuery) -> None:
    if not await is_admin(callback.from_user.id):
        await callback.answer("Нет доступа.", show_alert=True)
        return
    page = max(0, int(callback.data.split(":")[-1]))
    orders = await store.list_orders()
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

    start_index = page * PAGE_SIZE
    shown = orders[start_index : start_index + PAGE_SIZE]
    buttons = [
        [
            InlineKeyboardButton(
                text=f"#{order['id']} · {order.get('status')} · {order.get('tariff', {}).get('name', '')}",
                callback_data=f"a:order:{order['id']}",
            )
        ]
        for order in shown
    ]
    nav = page_buttons("a:orders", page, len(orders), PAGE_SIZE)
    if nav:
        buttons.append(nav)
    buttons.append([InlineKeyboardButton(text="⬅️ Админ-меню", callback_data="a:panel")])
    await callback.answer()
    if not orders:
        await callback.message.answer("Заказов пока нет.", reply_markup=admin_menu())
        return
    await callback.message.answer(
        f"<b>Заказы</b> · всего: {len(orders)}",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
    )


@router.callback_query(F.data.startswith("a:order:"))
async def admin_order_detail(callback: CallbackQuery) -> None:
    if not await is_admin(callback.from_user.id):
        await callback.answer("Нет доступа.", show_alert=True)
        return
    order = await store.get_order(callback.data.split(":")[-1])
    await callback.answer()
    if not order:
        await callback.message.answer("Заказ не найден.")
        return
    user = await get_user_or_empty(order["user_id"])
    await callback.message.answer(order_details(order, user), reply_markup=order_admin_keyboard(order))
    if order.get("server"):
        await callback.message.answer(await reveal_server(order))


@router.callback_query(F.data.startswith("a:take:"))
async def take_order(callback: CallbackQuery, bot: Bot) -> None:
    if not await is_admin(callback.from_user.id):
        await callback.answer("Нет доступа.", show_alert=True)
        return
    order_id = callback.data.split(":")[-1]
    order = await store.update_order(
        order_id,
        allowed_statuses={"requested"},
        changes={"status": "in_work", "assigned_admin": callback.from_user.id},
    )
    await callback.answer("Заказ взят в работу." if order else "Заказ уже обработан.")
    if not order:
        return
    user = await get_user_or_empty(order["user_id"])
    await safe_send(
        bot,
        order["user_id"],
        f"Заказ #{esc(order_id)} взят в работу. Администратор подготовит запрос на оплату.",
    )
    await callback.message.answer(
        "Заказ закреплён за вами.\n\n" + order_details(order, user),
        reply_markup=order_admin_keyboard(order),
    )


@router.callback_query(F.data.startswith("a:cancel:"))
async def cancel_order(callback: CallbackQuery, bot: Bot) -> None:
    if not await is_admin(callback.from_user.id):
        await callback.answer("Нет доступа.", show_alert=True)
        return
    order_id = callback.data.split(":")[-1]
    order = await store.update_order(
        order_id,
        allowed_statuses={"requested"},
        changes={"status": "cancelled", "cancelled_by": callback.from_user.id},
    )
    await callback.answer("Заказ отменён." if order else "Заказ уже обработан.")
    if not order:
        return
    await safe_send(bot, order["user_id"], f"Заказ #{esc(order_id)} отменён администратором.")
    await callback.message.answer("Заказ отменён и пользователь уведомлён.")


@router.callback_query(F.data.startswith("a:pay:"))
async def request_payment(callback: CallbackQuery, bot: Bot) -> None:
    if not await is_admin(callback.from_user.id):
        await callback.answer("Нет доступа.", show_alert=True)
        return
    order_id = callback.data.split(":")[-1]
    order = await store.update_order(
        order_id,
        allowed_statuses={"in_work"},
        changes={"status": "awaiting_payment", "payment_requested_at": utc_now()},
    )
    await callback.answer("Запрос оплаты отправлен." if order else "Заказ уже обработан.")
    if not order:
        return
    sent = await safe_send(
        bot,
        order["user_id"],
        f"<b>Запрос оплаты по заказу #{esc(order_id)}</b>\n"
        f"Тариф: {esc(order.get('tariff', {}).get('name'))} — "
        f"{esc(order.get('tariff', {}).get('price'))} ₽ / месяц\n\n"
        f"{esc(settings.payment_instructions)}\n\n"
        "После оплаты пришлите сюда фото или PDF/файл чека. Не отправляйте данные банковской карты.",
    )
    if not sent:
        await store.update_order(
            order_id,
            allowed_statuses={"awaiting_payment"},
            changes={"status": "in_work", "payment_delivery_failed": True},
        )
        await callback.message.answer("Не удалось доставить запрос оплаты: пользователь ещё не запустил бота.")
        return
    await callback.message.answer(f"Запрос оплаты по заказу #{esc(order_id)} отправлен пользователю.")


@router.message(F.photo | F.document)
async def payment_receipt(message: Message, bot: Bot) -> None:
    order = await store.latest_order_for_user(message.from_user.id, RECEIPT_STATES)
    if not order:
        await message.answer(
            "Сейчас нет заказа, ожидающего чек. Откройте активный заказ или напишите администратору."
        )
        return
    updated = await store.update_order(
        order["id"],
        allowed_statuses={"awaiting_payment"},
        changes={
            "status": "receipt_submitted",
            "receipt_message_id": message.message_id,
            "receipt_submitted_at": utc_now(),
        },
    )
    if not updated:
        await message.answer("Этот чек уже отправлен на проверку.")
        return
    await message.answer(
        f"Чек по заказу #{esc(order['id'])} отправлен администратору на проверку. "
        "После проверки вы получите сообщение."
    )
    user = await get_user_or_empty(message.from_user.id)
    text = "<b>Получен чек об оплате</b>\n\n" + order_details(updated, user)
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Подтвердить и начать", callback_data=f"a:start:{order['id']}"),
                InlineKeyboardButton(text="↩️ Отклонить чек", callback_data=f"a:reject:{order['id']}"),
            ]
        ]
    )
    for admin_id in settings.admin_ids:
        try:
            await bot.copy_message(admin_id, message.chat.id, message.message_id)
            await safe_send(bot, admin_id, text, reply_markup=keyboard)
        except (TelegramForbiddenError, TelegramBadRequest) as exc:
            logger.warning("Could not copy receipt to admin %s: %s", admin_id, exc.__class__.__name__)


@router.callback_query(F.data.startswith("a:reject:"))
async def reject_receipt(callback: CallbackQuery, bot: Bot) -> None:
    if not await is_admin(callback.from_user.id):
        await callback.answer("Нет доступа.", show_alert=True)
        return
    order_id = callback.data.split(":")[-1]
    order = await store.update_order(
        order_id,
        allowed_statuses={"receipt_submitted"},
        changes={"status": "awaiting_payment", "receipt_rejected_at": utc_now()},
    )
    await callback.answer("Чек отклонён." if order else "Чек уже обработан.")
    if order:
        await safe_send(
            bot,
            order["user_id"],
            f"Чек по заказу #{esc(order_id)} не принят. Проверьте оплату и пришлите новый чек.",
        )
        await callback.message.answer(f"Чек по заказу #{esc(order_id)} отклонён; пользователь уведомлён.")


@router.callback_query(F.data.startswith("a:start:"))
async def start_provisioning(callback: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    if not await is_admin(callback.from_user.id):
        await callback.answer("Нет доступа.", show_alert=True)
        return
    order_id = callback.data.split(":")[-1]
    order = await store.update_order(
        order_id,
        allowed_statuses={"receipt_submitted"},
        changes={"status": "collecting_server", "payment_verified_by": callback.from_user.id},
    )
    await callback.answer("Оплата подтверждена." if order else "Чек уже обработан.")
    if not order:
        return
    await state.clear()
    await state.update_data(order_id=order_id)
    await state.set_state(ServerForm.ip)
    await safe_send(
        bot,
        order["user_id"],
        f"Оплата по заказу #{esc(order_id)} подтверждена. Администратор настраивает сервер.",
    )
    await callback.message.answer(
        f"Оплата подтверждена для заказа #{esc(order_id)}.\n"
        "Введите IP-адрес сервера (пароль будет зашифрован при хранении):"
    )


@router.message(ServerForm.ip)
async def server_ip(message: Message, state: FSMContext) -> None:
    value = (message.text or "").strip()
    if not value or len(value) > 255:
        await message.answer("Введите корректный IP или hostname (до 255 символов).")
        return
    await state.update_data(server_ip=value)
    await state.set_state(ServerForm.username)
    await message.answer("Введите логин сервера:")


@router.message(ServerForm.username)
async def server_username(message: Message, state: FSMContext) -> None:
    value = (message.text or "").strip()
    if not value or len(value) > 128:
        await message.answer("Логин должен содержать от 1 до 128 символов.")
        return
    await state.update_data(server_username=value)
    await state.set_state(ServerForm.password)
    await message.answer(
        "Введите пароль сервера одним сообщением. Он не будет записан в логи и сохранится в JSON только в зашифрованном виде."
    )


@router.message(ServerForm.password)
async def server_password(message: Message, state: FSMContext, bot: Bot) -> None:
    password = (message.text or "").strip()
    if not password or len(password) > 512:
        await message.answer("Пароль должен содержать от 1 до 512 символов. Отправьте его текстом.")
        return
    data = await state.get_data()
    order_id = data.get("order_id")
    order = await store.get_order(order_id) if order_id else None
    if not order or order.get("status") != "collecting_server":
        await state.clear()
        await message.answer("Заказ больше не ожидает настройки сервера. Откройте его в панели заказов.")
        return
    server = {
        "ip": data.get("server_ip", ""),
        "username": data.get("server_username", ""),
        "password_enc": settings.fernet.encrypt(password.encode()).decode(),
        "created_at": utc_now(),
    }
    updated = await store.update_order(
        order_id,
        allowed_statuses={"collecting_server"},
        changes={"status": "completed", "server": server, "completed_at": utc_now()},
    )
    # Best effort: remove the sensitive message from the admin's private chat.
    try:
        await message.delete()
    except TelegramBadRequest:
        pass
    await state.clear()
    if not updated:
        await message.answer("Заказ изменился; откройте его в админ-панели.")
        return
    await safe_send(bot, updated["user_id"], await reveal_server(updated))
    await message.answer(
        f"Заказ #{esc(order_id)} завершён. Данные отправлены пользователю. "
        "Пароль в этой переписке не отображён.",
        reply_markup=admin_menu(),
    )


@router.message()
async def fallback(message: Message) -> None:
    user = await store.get_or_create_user(message.from_user)
    await message.answer(
        "Используйте кнопки меню. Для отправки чека по ожидающему оплату заказу пришлите фото или файл.",
        reply_markup=user_menu(user.get("status") == "A"),
    )


async def main() -> None:
    bot = Bot(
        token=settings.bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dispatcher = Dispatcher()
    dispatcher.include_router(router)
    try:
        await dispatcher.start_polling(bot, allowed_updates=dispatcher.resolve_used_update_types())
    finally:
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())