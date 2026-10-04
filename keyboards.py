from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup


def user_menu(is_admin: bool = False) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(text="🛒 Заказать хост", callback_data="u:order"),
            InlineKeyboardButton(text="👤 Профиль", callback_data="u:profile"),
        ],
        [
            InlineKeyboardButton(text="📦 Заказы", callback_data="u:orders"),
            InlineKeyboardButton(text="🖥 Сервера", callback_data="u:servers:0"),
        ],
    ]
    if is_admin:
        rows.append([InlineKeyboardButton(text="⚙️ Панель администратора", callback_data="a:panel")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def back_to_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="⬅️ Главное меню", callback_data="u:menu")]]
    )


def admin_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="👥 Пользователи", callback_data="a:users:0")],
            [InlineKeyboardButton(text="💳 Тарифы", callback_data="a:tariffs")],
            [InlineKeyboardButton(text="🧾 Все заказы и сервера", callback_data="a:orders:0")],
            [InlineKeyboardButton(text="⬅️ Главное меню", callback_data="u:menu")],
        ]
    )


def page_buttons(prefix: str, page: int, total: int, page_size: int = 8) -> list[InlineKeyboardButton]:
    pages = max(1, (total + page_size - 1) // page_size)
    result = []
    if page > 0:
        result.append(InlineKeyboardButton(text="◀️ Назад", callback_data=f"{prefix}:{page - 1}"))
    if page + 1 < pages:
        result.append(InlineKeyboardButton(text="Далее ▶️", callback_data=f"{prefix}:{page + 1}"))
    return result