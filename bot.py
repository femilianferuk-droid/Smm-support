import asyncio
import logging
import os
from datetime import datetime

import asyncpg
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)

BOT_TOKEN = os.environ["BOT_TOKEN"]
DATABASE_URL = os.environ["DATABASE_URL"]
ADMIN_ID = 7973988177

# ── Premium emoji IDs ──────────────────────────────────────────────
E_SETTINGS   = "5870982283724328568"
E_PROFILE    = "5870994129244131212"
E_PEOPLE     = "5870772616305839506"
E_FILE       = "5870528606328852614"
E_STATS      = "5870921681735781843"
E_LOCK       = "6037249452824072506"
E_BROADCAST  = "6039422865189638057"
E_CHECK      = "5870633910337015697"
E_CROSS      = "5870657884844462243"
E_PENCIL     = "5870676941614354370"
E_CLIP       = "6039451237743595514"
E_LINK       = "5769289093221454192"
E_SEND       = "5963103826075456248"
E_BELL       = "6039486778597970865"
E_GIFT       = "6032644646587338669"
E_CLOCK      = "5983150113483134607"
E_BOX        = "5884479287171485878"
E_TAG        = "5886285355279193209"
E_CODE       = "5940433880585605708"

def pe(emoji_id: str, fallback: str = "●") -> str:
    """Shorthand for premium emoji HTML tag."""
    return f'<tg-emoji emoji-id="{emoji_id}">{fallback}</tg-emoji>'

# ── FSM States ────────────────────────────────────────────────────
class CreateTicket(StatesGroup):
    choosing_problem = State()
    writing_message  = State()

class NewTicketMessage(StatesGroup):
    writing = State()

class SupportReply(StatesGroup):
    choosing_ticket = State()
    writing_reply   = State()

class AdminBroadcast(StatesGroup):
    writing_message = State()

class AdminAddSupport(StatesGroup):
    waiting_user_id = State()

class AdminRemoveSupport(StatesGroup):
    waiting_user_id = State()

class AdminSetDisplayName(StatesGroup):
    choosing_support = State()
    entering_name    = State()

# ── Database ──────────────────────────────────────────────────────
pool: asyncpg.Pool = None  # type: ignore

async def init_db():
    global pool
    pool = await asyncpg.create_pool(DATABASE_URL)
    async with pool.acquire() as conn:
        await conn.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id SERIAL PRIMARY KEY,
            telegram_id BIGINT UNIQUE NOT NULL,
            username TEXT,
            full_name TEXT,
            is_admin BOOLEAN DEFAULT FALSE,
            is_support BOOLEAN DEFAULT FALSE,
            support_display_name TEXT,
            created_at TIMESTAMPTZ DEFAULT NOW()
        );
        CREATE TABLE IF NOT EXISTS tickets (
            id SERIAL PRIMARY KEY,
            user_id INTEGER REFERENCES users(id),
            ticket_number SERIAL,
            problem_type TEXT NOT NULL,
            status TEXT DEFAULT 'open',
            created_at TIMESTAMPTZ DEFAULT NOW(),
            updated_at TIMESTAMPTZ DEFAULT NOW()
        );
        CREATE TABLE IF NOT EXISTS ticket_messages (
            id SERIAL PRIMARY KEY,
            ticket_id INTEGER REFERENCES tickets(id),
            sender_id INTEGER REFERENCES users(id),
            message_text TEXT NOT NULL,
            is_from_user BOOLEAN DEFAULT TRUE,
            created_at TIMESTAMPTZ DEFAULT NOW()
        );
        """)
    logger.info("Database initialized")

async def get_or_create_user(telegram_id: int, username: str | None, full_name: str) -> asyncpg.Record:
    async with pool.acquire() as conn:
        user = await conn.fetchrow("SELECT * FROM users WHERE telegram_id=$1", telegram_id)
        if not user:
            is_admin = telegram_id == ADMIN_ID
            user = await conn.fetchrow(
                "INSERT INTO users(telegram_id,username,full_name,is_admin) VALUES($1,$2,$3,$4) RETURNING *",
                telegram_id, username, full_name, is_admin
            )
            logger.info(f"New user registered: {telegram_id} ({full_name})")
        else:
            await conn.execute(
                "UPDATE users SET username=$1, full_name=$2 WHERE telegram_id=$3",
                username, full_name, telegram_id
            )
        return await conn.fetchrow("SELECT * FROM users WHERE telegram_id=$1", telegram_id)

async def get_user_by_telegram_id(tid: int) -> asyncpg.Record | None:
    async with pool.acquire() as conn:
        return await conn.fetchrow("SELECT * FROM users WHERE telegram_id=$1", tid)

async def get_user_by_db_id(uid: int) -> asyncpg.Record | None:
    async with pool.acquire() as conn:
        return await conn.fetchrow("SELECT * FROM users WHERE id=$1", uid)

async def get_support_staff() -> list:
    async with pool.acquire() as conn:
        return await conn.fetch("SELECT * FROM users WHERE is_support=TRUE OR is_admin=TRUE")

async def create_ticket(user_db_id: int, problem_type: str, message_text: str) -> asyncpg.Record:
    async with pool.acquire() as conn:
        ticket = await conn.fetchrow(
            "INSERT INTO tickets(user_id, problem_type) VALUES($1,$2) RETURNING *",
            user_db_id, problem_type
        )
        await conn.execute(
            "INSERT INTO ticket_messages(ticket_id,sender_id,message_text,is_from_user) VALUES($1,$2,$3,TRUE)",
            ticket["id"], user_db_id, message_text
        )
        return ticket

async def get_user_tickets(user_db_id: int) -> list:
    async with pool.acquire() as conn:
        return await conn.fetch(
            "SELECT * FROM tickets WHERE user_id=$1 ORDER BY created_at DESC",
            user_db_id
        )

async def get_ticket_by_id(ticket_id: int) -> asyncpg.Record | None:
    async with pool.acquire() as conn:
        return await conn.fetchrow("SELECT * FROM tickets WHERE id=$1", ticket_id)

async def get_ticket_messages(ticket_id: int) -> list:
    async with pool.acquire() as conn:
        return await conn.fetch(
            "SELECT tm.*, u.full_name, u.username FROM ticket_messages tm "
            "JOIN users u ON tm.sender_id=u.id WHERE tm.ticket_id=$1 ORDER BY tm.created_at",
            ticket_id
        )

async def add_ticket_message(ticket_id: int, sender_id: int, text: str, is_from_user: bool):
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO ticket_messages(ticket_id,sender_id,message_text,is_from_user) VALUES($1,$2,$3,$4)",
            ticket_id, sender_id, text, is_from_user
        )
        await conn.execute(
            "UPDATE tickets SET updated_at=NOW() WHERE id=$1", ticket_id
        )

async def close_ticket(ticket_id: int):
    async with pool.acquire() as conn:
        await conn.execute("UPDATE tickets SET status='closed', updated_at=NOW() WHERE id=$1", ticket_id)

async def get_open_tickets() -> list:
    async with pool.acquire() as conn:
        return await conn.fetch(
            "SELECT t.*, u.full_name, u.telegram_id FROM tickets t "
            "JOIN users u ON t.user_id=u.id WHERE t.status='open' ORDER BY t.created_at DESC"
        )

async def get_all_users() -> list:
    async with pool.acquire() as conn:
        return await conn.fetch("SELECT * FROM users")

async def get_stats() -> dict:
    async with pool.acquire() as conn:
        users_count = await conn.fetchval("SELECT COUNT(*) FROM users")
        open_tickets = await conn.fetchval("SELECT COUNT(*) FROM tickets WHERE status='open'")
        closed_tickets = await conn.fetchval("SELECT COUNT(*) FROM tickets WHERE status='closed'")
        messages_count = await conn.fetchval("SELECT COUNT(*) FROM ticket_messages")
        return {
            "users": users_count,
            "open": open_tickets,
            "closed": closed_tickets,
            "messages": messages_count
        }

# ── Keyboards ─────────────────────────────────────────────────────
def main_menu_keyboard(is_admin: bool = False, is_support: bool = False) -> ReplyKeyboardMarkup:
    rows = [
        [
            KeyboardButton(text="Создать тикет"),
            KeyboardButton(text="Мои тикеты"),
        ]
    ]
    if is_support and not is_admin:
        rows.append([KeyboardButton(text="Панель поддержки")])
    if is_admin:
        rows.append([KeyboardButton(text="Панель поддержки")])
        rows.append([KeyboardButton(text="Админ панель")])
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True)

def problem_type_keyboard() -> InlineKeyboardMarkup:
    problems = [
        ("Заказ не выполняется очень долго", "prob_delay",       E_CLOCK),
        ("Ошибка в создание заказа",          "prob_order_err",   E_CODE),
        ("Ошибка пополнения баланса",          "prob_balance_err", E_CROSS),
        ("Пополнение баланса ЮМАНИ",           "prob_ymoney",      E_GIFT),
        ("Другое",                             "prob_other",       E_CLIP),
    ]
    buttons = [
        [InlineKeyboardButton(text=name, callback_data=cd, icon_custom_emoji_id=eid)]
        for name, cd, eid in problems
    ]
    buttons.append([InlineKeyboardButton(text="◁ Назад", callback_data="back_main")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def ticket_list_keyboard(tickets: list, prefix: str = "ticket") -> InlineKeyboardMarkup:
    status_map = {"open": "🟢", "closed": "🔴"}
    buttons = []
    for t in tickets:
        status_icon = status_map.get(t["status"], "⚪")
        created = t["created_at"].strftime("%d.%m %H:%M")
        buttons.append([InlineKeyboardButton(
            text=f"{status_icon} #{t['ticket_number']} • {t['problem_type'][:25]} • {created}",
            callback_data=f"{prefix}:{t['id']}"
        )])
    buttons.append([InlineKeyboardButton(text="◁ Назад", callback_data="back_main")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def ticket_actions_keyboard(ticket_id: int, status: str) -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton(
            text="Отправить новое сообщение",
            callback_data=f"new_msg:{ticket_id}",
            icon_custom_emoji_id=E_SEND
        )],
    ]
    if status == "open":
        pass  # user can only message; closing is support-side
    buttons.append([InlineKeyboardButton(text="◁ Назад к тикетам", callback_data="my_tickets")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def support_ticket_keyboard(ticket_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text="Ответить пользователю",
            callback_data=f"supp_reply:{ticket_id}",
            icon_custom_emoji_id=E_SEND
        )],
        [InlineKeyboardButton(
            text="Закрыть тикет",
            callback_data=f"supp_close:{ticket_id}",
            icon_custom_emoji_id=E_LOCK
        )],
        [InlineKeyboardButton(text="◁ Назад", callback_data="supp_list")],
    ])

def admin_panel_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text="Статистика",
            callback_data="adm_stats",
            icon_custom_emoji_id=E_STATS
        )],
        [InlineKeyboardButton(
            text="Рассылка",
            callback_data="adm_broadcast",
            icon_custom_emoji_id=E_BROADCAST
        )],
        [InlineKeyboardButton(
            text="Назначить поддержку",
            callback_data="adm_add_support",
            icon_custom_emoji_id=E_PEOPLE
        )],
        [InlineKeyboardButton(
            text="Удалить из поддержки",
            callback_data="adm_remove_support",
            icon_custom_emoji_id=E_CROSS
        )],
        [InlineKeyboardButton(
            text="Имя поддержки",
            callback_data="adm_display_name",
            icon_custom_emoji_id=E_PENCIL
        )],
        [InlineKeyboardButton(text="◁ Назад", callback_data="back_main")],
    ])

# ── Routers ───────────────────────────────────────────────────────
router = Router()

# ── /start ────────────────────────────────────────────────────────
@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    user = await get_or_create_user(
        message.from_user.id,
        message.from_user.username,
        message.from_user.full_name
    )
    kb = main_menu_keyboard(is_admin=user["is_admin"], is_support=user["is_support"])
    await message.answer(
        f'{pe(E_BOX)} <b>Добро пожаловать в Тех Поддержку Vest SMM!</b>\n\n'
        f'Выберите действие в меню ниже {pe(E_CHECK)}',
        parse_mode=ParseMode.HTML,
        reply_markup=kb
    )

# ── Main menu buttons ─────────────────────────────────────────────
@router.message(F.text == "Создать тикет")
async def btn_create_ticket(message: Message, state: FSMContext):
    await state.clear()
    await state.set_state(CreateTicket.choosing_problem)
    await message.answer(
        f'{pe(E_BOX)} <b>Выберите тип проблемы:</b>',
        parse_mode=ParseMode.HTML,
        reply_markup=problem_type_keyboard()
    )

@router.message(F.text == "Мои тикеты")
async def btn_my_tickets(message: Message, state: FSMContext):
    await state.clear()
    user = await get_or_create_user(
        message.from_user.id, message.from_user.username, message.from_user.full_name
    )
    tickets = await get_user_tickets(user["id"])
    if not tickets:
        await message.answer(
            f'{pe(E_TAG)} <b>У вас пока нет тикетов.</b>\n\nСоздайте первый обращение!',
            parse_mode=ParseMode.HTML
        )
        return
    await message.answer(
        f'{pe(E_TAG)} <b>Ваши тикеты:</b>',
        parse_mode=ParseMode.HTML,
        reply_markup=ticket_list_keyboard(tickets, prefix="myticket")
    )

@router.message(F.text == "Панель поддержки")
async def btn_support_panel(message: Message, state: FSMContext):
    await state.clear()
    user = await get_or_create_user(
        message.from_user.id, message.from_user.username, message.from_user.full_name
    )
    if not (user["is_support"] or user["is_admin"]):
        await message.answer(
            f'{pe(E_LOCK)} <b>Доступ запрещён.</b>',
            parse_mode=ParseMode.HTML
        )
        return
    await show_support_panel(message)

@router.message(F.text == "Админ панель")
async def btn_admin_panel(message: Message, state: FSMContext):
    await state.clear()
    if message.from_user.id != ADMIN_ID:
        await message.answer(f'{pe(E_LOCK)} <b>Доступ запрещён.</b>', parse_mode=ParseMode.HTML)
        return
    await message.answer(
        f'{pe(E_SETTINGS)} <b>Админ-панель</b>\n\nВыберите раздел:',
        parse_mode=ParseMode.HTML,
        reply_markup=admin_panel_keyboard()
    )

@router.message(Command("admin"))
async def cmd_admin(message: Message, state: FSMContext):
    await state.clear()
    if message.from_user.id != ADMIN_ID:
        await message.answer(f'{pe(E_LOCK)} <b>Доступ запрещён.</b>', parse_mode=ParseMode.HTML)
        return
    await message.answer(
        f'{pe(E_SETTINGS)} <b>Админ-панель</b>\n\nВыберите раздел:',
        parse_mode=ParseMode.HTML,
        reply_markup=admin_panel_keyboard()
    )

@router.message(Command("supp"))
async def cmd_supp(message: Message, state: FSMContext):
    await state.clear()
    user = await get_or_create_user(
        message.from_user.id, message.from_user.username, message.from_user.full_name
    )
    if not (user["is_support"] or user["is_admin"]):
        await message.answer(f'{pe(E_LOCK)} <b>Доступ запрещён.</b>', parse_mode=ParseMode.HTML)
        return
    await show_support_panel(message)

# ── Create ticket flow ────────────────────────────────────────────
PROBLEM_LABELS = {
    "prob_delay":      "Заказ не выполняется очень долго",
    "prob_order_err":  "Ошибка в создание заказа",
    "prob_balance_err":"Ошибка пополнения баланса",
    "prob_ymoney":     "Пополнение баланса ЮМАНИ",
    "prob_other":      "Другое",
}

@router.callback_query(F.data.in_(PROBLEM_LABELS.keys()), CreateTicket.choosing_problem)
async def cb_problem_chosen(callback: CallbackQuery, state: FSMContext):
    label = PROBLEM_LABELS[callback.data]
    await state.update_data(problem_type=label)
    await state.set_state(CreateTicket.writing_message)
    await callback.message.edit_text(
        f'{pe(E_PENCIL)} <b>Тип проблемы:</b> {label}\n\n'
        f'Опишите вашу проблему подробно одним сообщением:',
        parse_mode=ParseMode.HTML
    )
    await callback.answer()

@router.callback_query(F.data == "back_main")
async def cb_back_main(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    user = await get_user_by_telegram_id(callback.from_user.id)
    await callback.message.delete()
    await callback.message.answer(
        f'{pe(E_BOX)} <b>Главное меню</b>',
        parse_mode=ParseMode.HTML,
        reply_markup=main_menu_keyboard(
            is_admin=user["is_admin"] if user else False,
            is_support=user["is_support"] if user else False
        )
    )
    await callback.answer()

@router.message(CreateTicket.writing_message)
async def ticket_message_received(message: Message, state: FSMContext):
    data = await state.get_data()
    problem_type = data.get("problem_type", "Другое")
    user = await get_or_create_user(
        message.from_user.id, message.from_user.username, message.from_user.full_name
    )
    ticket = await create_ticket(user["id"], problem_type, message.text)
    await state.clear()
    logger.info(f"Ticket #{ticket['ticket_number']} created by user {message.from_user.id}")

    kb = main_menu_keyboard(is_admin=user["is_admin"], is_support=user["is_support"])
    await message.answer(
        f'{pe(E_CHECK)} <b>Тикет #{ticket["ticket_number"]} создан!</b>\n\n'
        f'{pe(E_TAG)} Тип: {problem_type}\n'
        f'{pe(E_CLOCK)} Статус: <b>Открыт</b>\n\n'
        f'Мы ответим вам в ближайшее время.',
        parse_mode=ParseMode.HTML,
        reply_markup=kb
    )

    # Notify support staff
    support_staff = await get_support_staff()
    bot = message.bot
    for staff in support_staff:
        if staff["telegram_id"] == message.from_user.id:
            continue
        try:
            await bot.send_message(
                staff["telegram_id"],
                f'{pe(E_BELL)} <b>Новый тикет #{ticket["ticket_number"]}!</b>\n\n'
                f'{pe(E_PROFILE)} Пользователь: {user["full_name"]} (@{user["username"] or "—"})\n'
                f'{pe(E_TAG)} Тип: {problem_type}\n\n'
                f'{pe(E_FILE)} <b>Сообщение:</b>\n{message.text}',
                parse_mode=ParseMode.HTML
            )
        except Exception as e:
            logger.warning(f"Could not notify staff {staff['telegram_id']}: {e}")

# ── My tickets flow ───────────────────────────────────────────────
@router.callback_query(F.data == "my_tickets")
async def cb_my_tickets(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    user = await get_user_by_telegram_id(callback.from_user.id)
    tickets = await get_user_tickets(user["id"])
    if not tickets:
        await callback.message.edit_text(
            f'{pe(E_TAG)} <b>У вас пока нет тикетов.</b>',
            parse_mode=ParseMode.HTML
        )
        await callback.answer()
        return
    await callback.message.edit_text(
        f'{pe(E_TAG)} <b>Ваши тикеты:</b>',
        parse_mode=ParseMode.HTML,
        reply_markup=ticket_list_keyboard(tickets, prefix="myticket")
    )
    await callback.answer()

@router.callback_query(F.data.startswith("myticket:"))
async def cb_view_my_ticket(callback: CallbackQuery, state: FSMContext):
    ticket_id = int(callback.data.split(":")[1])
    ticket = await get_ticket_by_id(ticket_id)
    if not ticket:
        await callback.answer("Тикет не найден", show_alert=True)
        return
    messages = await get_ticket_messages(ticket_id)
    history = ""
    for m in messages[-10:]:
        role = f'{pe(E_PROFILE)} Вы' if m["is_from_user"] else f'{pe(E_PEOPLE)} Поддержка'
        ts = m["created_at"].strftime("%d.%m %H:%M")
        history += f"\n<b>{role}</b> [{ts}]:\n{m['message_text']}\n"
    status_text = "🟢 Открыт" if ticket["status"] == "open" else "🔴 Закрыт"
    text = (
        f'{pe(E_BOX)} <b>Тикет #{ticket["ticket_number"]}</b>\n'
        f'{pe(E_TAG)} Тип: {ticket["problem_type"]}\n'
        f'{pe(E_CLOCK)} Статус: {status_text}\n\n'
        f'<b>История ({len(messages)} сообщений):</b>{history}'
    )
    await callback.message.edit_text(
        text,
        parse_mode=ParseMode.HTML,
        reply_markup=ticket_actions_keyboard(ticket_id, ticket["status"])
    )
    await callback.answer()

@router.callback_query(F.data.startswith("new_msg:"))
async def cb_new_message(callback: CallbackQuery, state: FSMContext):
    ticket_id = int(callback.data.split(":")[1])
    ticket = await get_ticket_by_id(ticket_id)
    if not ticket or ticket["status"] == "closed":
        await callback.answer(
            f'Тикет закрыт — нельзя отправить сообщение.', show_alert=True
        )
        return
    await state.set_state(NewTicketMessage.writing)
    await state.update_data(ticket_id=ticket_id)
    await callback.message.edit_text(
        f'{pe(E_SEND)} <b>Напишите ваше сообщение</b> для тикета #{ticket["ticket_number"]}:',
        parse_mode=ParseMode.HTML
    )
    await callback.answer()

@router.message(NewTicketMessage.writing)
async def user_new_message_sent(message: Message, state: FSMContext):
    data = await state.get_data()
    ticket_id = data.get("ticket_id")
    user = await get_or_create_user(
        message.from_user.id, message.from_user.username, message.from_user.full_name
    )
    ticket = await get_ticket_by_id(ticket_id)
    await add_ticket_message(ticket_id, user["id"], message.text, is_from_user=True)
    await state.clear()
    logger.info(f"User {message.from_user.id} sent new message to ticket #{ticket['ticket_number']}")

    await message.answer(
        f'{pe(E_CHECK)} <b>Сообщение отправлено</b> в тикет #{ticket["ticket_number"]}!',
        parse_mode=ParseMode.HTML,
        reply_markup=main_menu_keyboard(is_admin=user["is_admin"], is_support=user["is_support"])
    )

    # Notify support
    support_staff = await get_support_staff()
    for staff in support_staff:
        if staff["telegram_id"] == message.from_user.id:
            continue
        try:
            await message.bot.send_message(
                staff["telegram_id"],
                f'{pe(E_BELL)} <b>Новое сообщение в тикете #{ticket["ticket_number"]}!</b>\n\n'
                f'{pe(E_PROFILE)} От: {user["full_name"]} (@{user["username"] or "—"})\n\n'
                f'{pe(E_FILE)} <b>Сообщение:</b>\n{message.text}',
                parse_mode=ParseMode.HTML
            )
        except Exception as e:
            logger.warning(f"Could not notify staff {staff['telegram_id']}: {e}")

# ── Support panel ─────────────────────────────────────────────────
async def show_support_panel(message: Message):
    tickets = await get_open_tickets()
    if not tickets:
        await message.answer(
            f'{pe(E_CHECK)} <b>Открытых тикетов нет.</b> Всё обработано!',
            parse_mode=ParseMode.HTML
        )
        return
    buttons = []
    for t in tickets:
        created = t["created_at"].strftime("%d.%m %H:%M")
        buttons.append([InlineKeyboardButton(
            text=f"#{t['ticket_number']} • {t['full_name'][:15]} • {t['problem_type'][:20]} • {created}",
            callback_data=f"suppticket:{t['id']}"
        )])
    kb = InlineKeyboardMarkup(inline_keyboard=buttons)
    await message.answer(
        f'{pe(E_BOX)} <b>Открытые тикеты ({len(tickets)}):</b>',
        parse_mode=ParseMode.HTML,
        reply_markup=kb
    )

@router.callback_query(F.data == "supp_list")
async def cb_supp_list(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    user = await get_user_by_telegram_id(callback.from_user.id)
    if not (user and (user["is_support"] or user["is_admin"])):
        await callback.answer("Доступ запрещён", show_alert=True)
        return
    tickets = await get_open_tickets()
    if not tickets:
        await callback.message.edit_text(
            f'{pe(E_CHECK)} <b>Открытых тикетов нет.</b>',
            parse_mode=ParseMode.HTML
        )
        await callback.answer()
        return
    buttons = []
    for t in tickets:
        created = t["created_at"].strftime("%d.%m %H:%M")
        buttons.append([InlineKeyboardButton(
            text=f"#{t['ticket_number']} • {t['full_name'][:15]} • {t['problem_type'][:20]} • {created}",
            callback_data=f"suppticket:{t['id']}"
        )])
    kb = InlineKeyboardMarkup(inline_keyboard=buttons)
    await callback.message.edit_text(
        f'{pe(E_BOX)} <b>Открытые тикеты ({len(tickets)}):</b>',
        parse_mode=ParseMode.HTML,
        reply_markup=kb
    )
    await callback.answer()

@router.callback_query(F.data.startswith("suppticket:"))
async def cb_supp_view_ticket(callback: CallbackQuery, state: FSMContext):
    user = await get_user_by_telegram_id(callback.from_user.id)
    if not (user and (user["is_support"] or user["is_admin"])):
        await callback.answer("Доступ запрещён", show_alert=True)
        return
    ticket_id = int(callback.data.split(":")[1])
    ticket = await get_ticket_by_id(ticket_id)
    if not ticket:
        await callback.answer("Тикет не найден", show_alert=True)
        return
    ticket_user = await get_user_by_db_id(ticket["user_id"])
    msgs = await get_ticket_messages(ticket_id)
    history = ""
    for m in msgs[-15:]:
        role = f'{pe(E_PROFILE)} Пользователь' if m["is_from_user"] else f'{pe(E_PEOPLE)} Поддержка'
        ts = m["created_at"].strftime("%d.%m %H:%M")
        history += f"\n<b>{role}</b> [{ts}]:\n{m['message_text']}\n"
    status_text = "🟢 Открыт" if ticket["status"] == "open" else "🔴 Закрыт"
    text = (
        f'{pe(E_BOX)} <b>Тикет #{ticket["ticket_number"]}</b>\n'
        f'{pe(E_PROFILE)} Пользователь: {ticket_user["full_name"] if ticket_user else "—"}\n'
        f'{pe(E_TAG)} Тип: {ticket["problem_type"]}\n'
        f'{pe(E_CLOCK)} Статус: {status_text}\n\n'
        f'<b>История ({len(msgs)} сообщений):</b>{history}'
    )
    await callback.message.edit_text(
        text,
        parse_mode=ParseMode.HTML,
        reply_markup=support_ticket_keyboard(ticket_id)
    )
    await callback.answer()

@router.callback_query(F.data.startswith("supp_reply:"))
async def cb_supp_reply_start(callback: CallbackQuery, state: FSMContext):
    user = await get_user_by_telegram_id(callback.from_user.id)
    if not (user and (user["is_support"] or user["is_admin"])):
        await callback.answer("Доступ запрещён", show_alert=True)
        return
    ticket_id = int(callback.data.split(":")[1])
    ticket = await get_ticket_by_id(ticket_id)
    await state.set_state(SupportReply.writing_reply)
    await state.update_data(ticket_id=ticket_id)
    await callback.message.edit_text(
        f'{pe(E_SEND)} <b>Введите ответ</b> для тикета #{ticket["ticket_number"]}:',
        parse_mode=ParseMode.HTML
    )
    await callback.answer()

@router.message(SupportReply.writing_reply)
async def supp_reply_sent(message: Message, state: FSMContext):
    data = await state.get_data()
    ticket_id = data.get("ticket_id")
    support_user = await get_or_create_user(
        message.from_user.id, message.from_user.username, message.from_user.full_name
    )
    ticket = await get_ticket_by_id(ticket_id)
    if not ticket:
        await state.clear()
        await message.answer(f'{pe(E_CROSS)} Тикет не найден.', parse_mode=ParseMode.HTML)
        return

    await add_ticket_message(ticket_id, support_user["id"], message.text, is_from_user=False)
    await state.clear()
    logger.info(f"Support {message.from_user.id} replied to ticket #{ticket['ticket_number']}")

    display_name = support_user["support_display_name"] or support_user["full_name"]
    ticket_owner = await get_user_by_db_id(ticket["user_id"])

    await message.answer(
        f'{pe(E_CHECK)} <b>Ответ отправлен</b> в тикет #{ticket["ticket_number"]}!',
        parse_mode=ParseMode.HTML,
        reply_markup=main_menu_keyboard(
            is_admin=support_user["is_admin"],
            is_support=support_user["is_support"]
        )
    )

    if ticket_owner:
        try:
            await message.bot.send_message(
                ticket_owner["telegram_id"],
                f'{pe(E_BELL)} <b>{display_name}</b> (#{ticket["ticket_number"]}):\n\n{message.text}',
                parse_mode=ParseMode.HTML
            )
        except Exception as e:
            logger.warning(f"Could not send reply to user {ticket_owner['telegram_id']}: {e}")

@router.callback_query(F.data.startswith("supp_close:"))
async def cb_supp_close_ticket(callback: CallbackQuery, state: FSMContext):
    user = await get_user_by_telegram_id(callback.from_user.id)
    if not (user and (user["is_support"] or user["is_admin"])):
        await callback.answer("Доступ запрещён", show_alert=True)
        return
    ticket_id = int(callback.data.split(":")[1])
    ticket = await get_ticket_by_id(ticket_id)
    await close_ticket(ticket_id)
    logger.info(f"Ticket #{ticket['ticket_number']} closed by {callback.from_user.id}")

    ticket_owner = await get_user_by_db_id(ticket["user_id"])
    if ticket_owner:
        try:
            await callback.bot.send_message(
                ticket_owner["telegram_id"],
                f'{pe(E_LOCK)} <b>Тикет #{ticket["ticket_number"]} закрыт.</b>\n\n'
                f'Если у вас остались вопросы — создайте новый тикет.',
                parse_mode=ParseMode.HTML
            )
        except Exception as e:
            logger.warning(f"Could not notify user about ticket close: {e}")

    await callback.message.edit_text(
        f'{pe(E_CHECK)} <b>Тикет #{ticket["ticket_number"]} закрыт!</b>',
        parse_mode=ParseMode.HTML
    )
    await callback.answer("Тикет закрыт!")

# ── Admin panel callbacks ─────────────────────────────────────────
@router.callback_query(F.data == "adm_stats")
async def cb_adm_stats(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("Доступ запрещён", show_alert=True)
        return
    stats = await get_stats()
    await callback.message.edit_text(
        f'{pe(E_STATS)} <b>Статистика</b>\n\n'
        f'{pe(E_PROFILE)} Пользователей: <b>{stats["users"]}</b>\n'
        f'{pe(E_BOX)} Открытых тикетов: <b>{stats["open"]}</b>\n'
        f'{pe(E_LOCK)} Закрытых тикетов: <b>{stats["closed"]}</b>\n'
        f'{pe(E_FILE)} Сообщений в поддержке: <b>{stats["messages"]}</b>',
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="◁ Назад", callback_data="adm_back")]
        ])
    )
    await callback.answer()

@router.callback_query(F.data == "adm_back")
async def cb_adm_back(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.edit_text(
        f'{pe(E_SETTINGS)} <b>Админ-панель</b>\n\nВыберите раздел:',
        parse_mode=ParseMode.HTML,
        reply_markup=admin_panel_keyboard()
    )
    await callback.answer()

@router.callback_query(F.data == "adm_broadcast")
async def cb_adm_broadcast(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("Доступ запрещён", show_alert=True)
        return
    await state.set_state(AdminBroadcast.writing_message)
    await callback.message.edit_text(
        f'{pe(E_BROADCAST)} <b>Рассылка</b>\n\nОтправьте текст или фото для рассылки всем пользователям.\n'
        f'Для отмены напишите /cancel',
        parse_mode=ParseMode.HTML
    )
    await callback.answer()

@router.message(AdminBroadcast.writing_message)
async def adm_broadcast_message(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    users = await get_all_users()
    sent, failed = 0, 0
    for u in users:
        try:
            if message.photo:
                await message.bot.send_photo(
                    u["telegram_id"],
                    message.photo[-1].file_id,
                    caption=message.caption or ""
                )
            else:
                await message.bot.send_message(u["telegram_id"], message.text or "")
            sent += 1
        except Exception:
            failed += 1
    await state.clear()
    logger.info(f"Broadcast sent: {sent} success, {failed} failed")
    admin_user = await get_user_by_telegram_id(ADMIN_ID)
    await message.answer(
        f'{pe(E_CHECK)} <b>Рассылка завершена!</b>\n\n'
        f'{pe(E_SEND)} Отправлено: {sent}\n'
        f'{pe(E_CROSS)} Не доставлено: {failed}',
        parse_mode=ParseMode.HTML,
        reply_markup=main_menu_keyboard(is_admin=True)
    )

@router.callback_query(F.data == "adm_add_support")
async def cb_adm_add_support(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("Доступ запрещён", show_alert=True)
        return
    await state.set_state(AdminAddSupport.waiting_user_id)
    await callback.message.edit_text(
        f'{pe(E_PEOPLE)} <b>Назначить поддержку</b>\n\n'
        f'Введите Telegram ID или @username пользователя:',
        parse_mode=ParseMode.HTML
    )
    await callback.answer()

@router.message(AdminAddSupport.waiting_user_id)
async def adm_add_support_input(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    text = message.text.strip()
    async with pool.acquire() as conn:
        if text.startswith("@"):
            user = await conn.fetchrow("SELECT * FROM users WHERE username=$1", text[1:])
        else:
            try:
                tid = int(text)
                user = await conn.fetchrow("SELECT * FROM users WHERE telegram_id=$1", tid)
            except ValueError:
                user = None

        if not user:
            await message.answer(
                f'{pe(E_CROSS)} Пользователь не найден. Убедитесь, что он запускал бота.',
                parse_mode=ParseMode.HTML
            )
            await state.clear()
            return
        await conn.execute("UPDATE users SET is_support=TRUE WHERE id=$1", user["id"])

    await state.clear()
    logger.info(f"User {user['telegram_id']} added to support by admin")
    await message.answer(
        f'{pe(E_CHECK)} <b>{user["full_name"]}</b> добавлен(а) в поддержку!',
        parse_mode=ParseMode.HTML,
        reply_markup=main_menu_keyboard(is_admin=True)
    )
    try:
        await message.bot.send_message(
            user["telegram_id"],
            f'{pe(E_BELL)} <b>Вам предоставлен доступ к панели поддержки!</b>\n\n'
            f'Используйте команду /supp или кнопку «Панель поддержки».',
            parse_mode=ParseMode.HTML
        )
    except Exception:
        pass

@router.callback_query(F.data == "adm_remove_support")
async def cb_adm_remove_support(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("Доступ запрещён", show_alert=True)
        return
    await state.set_state(AdminRemoveSupport.waiting_user_id)
    await callback.message.edit_text(
        f'{pe(E_CROSS)} <b>Удалить из поддержки</b>\n\n'
        f'Введите Telegram ID или @username пользователя:',
        parse_mode=ParseMode.HTML
    )
    await callback.answer()

@router.message(AdminRemoveSupport.waiting_user_id)
async def adm_remove_support_input(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    text = message.text.strip()
    async with pool.acquire() as conn:
        if text.startswith("@"):
            user = await conn.fetchrow("SELECT * FROM users WHERE username=$1", text[1:])
        else:
            try:
                tid = int(text)
                user = await conn.fetchrow("SELECT * FROM users WHERE telegram_id=$1", tid)
            except ValueError:
                user = None

        if not user:
            await message.answer(
                f'{pe(E_CROSS)} Пользователь не найден.',
                parse_mode=ParseMode.HTML
            )
            await state.clear()
            return
        await conn.execute("UPDATE users SET is_support=FALSE WHERE id=$1", user["id"])

    await state.clear()
    logger.info(f"User {user['telegram_id']} removed from support by admin")
    await message.answer(
        f'{pe(E_CHECK)} <b>{user["full_name"]}</b> удалён(а) из поддержки.',
        parse_mode=ParseMode.HTML,
        reply_markup=main_menu_keyboard(is_admin=True)
    )

@router.callback_query(F.data == "adm_display_name")
async def cb_adm_display_name(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("Доступ запрещён", show_alert=True)
        return
    async with pool.acquire() as conn:
        support_list = await conn.fetch("SELECT * FROM users WHERE is_support=TRUE")
    if not support_list:
        await callback.message.edit_text(
            f'{pe(E_CROSS)} Список поддержки пуст.',
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="◁ Назад", callback_data="adm_back")]
            ])
        )
        await callback.answer()
        return
    buttons = [
        [InlineKeyboardButton(
            text=f'{u["full_name"]} — {u["support_display_name"] or "не задано"}',
            callback_data=f"adm_setname:{u['id']}"
        )]
        for u in support_list
    ]
    buttons.append([InlineKeyboardButton(text="◁ Назад", callback_data="adm_back")])
    await callback.message.edit_text(
        f'{pe(E_PENCIL)} <b>Имя поддержки</b>\n\nВыберите сотрудника:',
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons)
    )
    await callback.answer()

@router.callback_query(F.data.startswith("adm_setname:"))
async def cb_adm_setname(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("Доступ запрещён", show_alert=True)
        return
    user_db_id = int(callback.data.split(":")[1])
    await state.set_state(AdminSetDisplayName.entering_name)
    await state.update_data(target_user_id=user_db_id)
    target = await get_user_by_db_id(user_db_id)
    await callback.message.edit_text(
        f'{pe(E_PENCIL)} Введите отображаемое имя для <b>{target["full_name"]}</b>:\n\n'
        f'(Например: «Анна ТехПоддержка»)',
        parse_mode=ParseMode.HTML
    )
    await callback.answer()

@router.message(AdminSetDisplayName.entering_name)
async def adm_set_display_name_input(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    data = await state.get_data()
    target_id = data.get("target_user_id")
    display_name = message.text.strip()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE users SET support_display_name=$1 WHERE id=$2",
            display_name, target_id
        )
    target = await get_user_by_db_id(target_id)
    await state.clear()
    logger.info(f"Display name for user {target_id} set to '{display_name}'")
    await message.answer(
        f'{pe(E_CHECK)} Имя <b>{target["full_name"]}</b> установлено: <b>{display_name}</b>',
        parse_mode=ParseMode.HTML,
        reply_markup=main_menu_keyboard(is_admin=True)
    )

# ── /cancel ───────────────────────────────────────────────────────
@router.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext):
    await state.clear()
    user = await get_user_by_telegram_id(message.from_user.id)
    await message.answer(
        f'{pe(E_CHECK)} <b>Действие отменено.</b>',
        parse_mode=ParseMode.HTML,
        reply_markup=main_menu_keyboard(
            is_admin=user["is_admin"] if user else False,
            is_support=user["is_support"] if user else False
        )
    )

# ── Fallback ──────────────────────────────────────────────────────
@router.message()
async def fallback(message: Message, state: FSMContext):
    current = await state.get_state()
    if current:
        return  # FSM is active, ignore
    user = await get_or_create_user(
        message.from_user.id, message.from_user.username, message.from_user.full_name
    )
    await message.answer(
        f'{pe(E_BOX)} Используйте кнопки меню для навигации.',
        parse_mode=ParseMode.HTML,
        reply_markup=main_menu_keyboard(is_admin=user["is_admin"], is_support=user["is_support"])
    )

# ── Entry point ───────────────────────────────────────────────────
async def main():
    await init_db()
    bot = Bot(
        token=BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML)
    )
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)
    logger.info("Bot starting...")
    await dp.start_polling(bot, allowed_updates=["message", "callback_query"])

if __name__ == "__main__":
    asyncio.run(main())
