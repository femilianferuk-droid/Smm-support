import asyncio
import logging
import os
import sys
from datetime import datetime
from typing import Optional, List, Dict, Any

from aiogram import Bot, Dispatcher, types, F
from aiogram.enums import ParseMode
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.client.default import DefaultBotProperties
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardMarkup, 
    InlineKeyboardButton, ReplyKeyboardMarkup, KeyboardButton
)
from aiogram.utils.keyboard import InlineKeyboardBuilder
import asyncpg
from asyncpg import Pool

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger(__name__)

# Конфигурация
BOT_TOKEN = os.getenv('BOT_TOKEN')
DATABASE_URL = os.getenv('DATABASE_URL')
ADMIN_ID = 7973988177

if not BOT_TOKEN or not DATABASE_URL:
    logger.error("BOT_TOKEN и DATABASE_URL должны быть установлены в переменных окружения")
    sys.exit(1)

# Premium Emoji IDs
EMOJI = {
    'settings': '5870982283724328568',
    'profile': '5870994129244131212',
    'people': '5870772616305839506',
    'file': '5870528606328852614',
    'stats': '5870921681735781843',
    'lock': '6037249452824072506',
    'megaphone': '6039422865189638057',
    'check': '5870633910337015697',
    'cross': '5870657884844462243',
    'pencil': '5870676941614354370',
    'paperclip': '6039451237743595514',
    'link': '5769289093221454192',
    'send': '5963103826075456248',
    'notification': '6039486778597970865',
    'gift': '6032644646587338669',
    'clock': '5983150113483134607',
    'box': '5884479287171485878',
    'tag': '5886285355279193209',
    'code': '5940433880585605708',
}

# Инициализация бота и диспетчера (исправленная версия)
bot = Bot(
    token=BOT_TOKEN,
    default=DefaultBotProperties(parse_mode=ParseMode.HTML)
)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)
db_pool: Optional[Pool] = None

# Состояния FSM
class TicketCreation(StatesGroup):
    waiting_for_problem_type = State()
    waiting_for_description = State()

class TicketReply(StatesGroup):
    waiting_for_message = State()

class SupportReply(StatesGroup):
    waiting_for_reply = State()

class BroadcastMessage(StatesGroup):
    waiting_for_message = State()

class SetDisplayName(StatesGroup):
    waiting_for_name = State()

# Функции работы с БД
async def init_db():
    """Инициализация базы данных"""
    global db_pool
    db_pool = await asyncpg.create_pool(DATABASE_URL)
    
    async with db_pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id SERIAL PRIMARY KEY,
                telegram_id BIGINT UNIQUE NOT NULL,
                username VARCHAR(255),
                full_name VARCHAR(255),
                is_admin BOOLEAN DEFAULT FALSE,
                is_support BOOLEAN DEFAULT FALSE,
                support_display_name VARCHAR(255),
                created_at TIMESTAMP DEFAULT NOW()
            )
        """)
        
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS tickets (
                id SERIAL PRIMARY KEY,
                user_id INTEGER REFERENCES users(id),
                ticket_number VARCHAR(50) UNIQUE NOT NULL,
                problem_type VARCHAR(255),
                status VARCHAR(20) DEFAULT 'open',
                created_at TIMESTAMP DEFAULT NOW(),
                updated_at TIMESTAMP DEFAULT NOW()
            )
        """)
        
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS ticket_messages (
                id SERIAL PRIMARY KEY,
                ticket_id INTEGER REFERENCES tickets(id),
                sender_id INTEGER REFERENCES users(id),
                message_text TEXT,
                is_from_user BOOLEAN DEFAULT TRUE,
                created_at TIMESTAMP DEFAULT NOW()
            )
        """)
        
        # Создаем админа при первом запуске
        await conn.execute("""
            INSERT INTO users (telegram_id, full_name, is_admin, is_support)
            VALUES ($1, 'Admin', TRUE, TRUE)
            ON CONFLICT (telegram_id) DO UPDATE 
            SET is_admin = TRUE, is_support = TRUE
        """, ADMIN_ID)
        
        logger.info("Database initialized successfully")

async def get_or_create_user(telegram_id: int, username: str, full_name: str) -> Dict[str, Any]:
    """Получить или создать пользователя"""
    async with db_pool.acquire() as conn:
        user = await conn.fetchrow(
            "SELECT * FROM users WHERE telegram_id = $1", telegram_id
        )
        
        if not user:
            user = await conn.fetchrow(
                """INSERT INTO users (telegram_id, username, full_name) 
                   VALUES ($1, $2, $3) RETURNING *""",
                telegram_id, username, full_name
            )
        
        return dict(user)

async def generate_ticket_number() -> str:
    """Генерация номера тикета"""
    async with db_pool.acquire() as conn:
        count = await conn.fetchval("SELECT COUNT(*) FROM tickets")
        return f"TKT-{count + 1:04d}"

async def create_ticket(user_id: int, problem_type: str, description: str) -> int:
    """Создание нового тикета"""
    ticket_number = await generate_ticket_number()
    
    async with db_pool.acquire() as conn:
        ticket = await conn.fetchrow(
            """INSERT INTO tickets (user_id, ticket_number, problem_type) 
               VALUES ($1, $2, $3) RETURNING id""",
            user_id, ticket_number, problem_type
        )
        
        ticket_id = ticket['id']
        
        # Сохраняем первое сообщение
        await conn.execute(
            """INSERT INTO ticket_messages (ticket_id, sender_id, message_text, is_from_user) 
               VALUES ($1, $2, $3, TRUE)""",
            ticket_id, user_id, description
        )
        
        logger.info(f"Ticket created: {ticket_number} by user {user_id}")
        return ticket_id

async def get_user_tickets(user_id: int) -> List[Dict[str, Any]]:
    """Получить тикеты пользователя"""
    async with db_pool.acquire() as conn:
        tickets = await conn.fetch(
            """SELECT * FROM tickets 
               WHERE user_id = $1 
               ORDER BY created_at DESC""",
            user_id
        )
        return [dict(t) for t in tickets]

async def get_all_open_tickets() -> List[Dict[str, Any]]:
    """Получить все открытые тикеты"""
    async with db_pool.acquire() as conn:
        tickets = await conn.fetch(
            """SELECT t.*, u.full_name, u.username 
               FROM tickets t 
               JOIN users u ON t.user_id = u.id 
               WHERE t.status = 'open' 
               ORDER BY t.created_at ASC"""
        )
        return [dict(t) for t in tickets]

async def get_ticket_messages(ticket_id: int) -> List[Dict[str, Any]]:
    """Получить сообщения тикета"""
    async with db_pool.acquire() as conn:
        messages = await conn.fetch(
            """SELECT tm.*, u.full_name, u.username, u.support_display_name
               FROM ticket_messages tm
               JOIN users u ON tm.sender_id = u.id
               WHERE tm.ticket_id = $1
               ORDER BY tm.created_at ASC""",
            ticket_id
        )
        return [dict(m) for m in messages]

async def close_ticket(ticket_id: int):
    """Закрыть тикет"""
    async with db_pool.acquire() as conn:
        await conn.execute(
            """UPDATE tickets 
               SET status = 'closed', updated_at = NOW() 
               WHERE id = $1""",
            ticket_id
        )
        logger.info(f"Ticket {ticket_id} closed")

async def get_support_users() -> List[Dict[str, Any]]:
    """Получить список поддержки"""
    async with db_pool.acquire() as conn:
        users = await conn.fetch(
            "SELECT * FROM users WHERE is_support = TRUE"
        )
        return [dict(u) for u in users]

async def get_statistics() -> Dict[str, Any]:
    """Получить статистику"""
    async with db_pool.acquire() as conn:
        total_users = await conn.fetchval("SELECT COUNT(*) FROM users")
        open_tickets = await conn.fetchval("SELECT COUNT(*) FROM tickets WHERE status = 'open'")
        closed_tickets = await conn.fetchval("SELECT COUNT(*) FROM tickets WHERE status = 'closed'")
        total_messages = await conn.fetchval("SELECT COUNT(*) FROM ticket_messages")
        
        return {
            'total_users': total_users,
            'open_tickets': open_tickets,
            'closed_tickets': closed_tickets,
            'total_messages': total_messages
        }

async def get_all_users() -> List[Dict[str, Any]]:
    """Получить всех пользователей для рассылки"""
    async with db_pool.acquire() as conn:
        users = await conn.fetch("SELECT * FROM users")
        return [dict(u) for u in users]

# Клавиатуры
def main_menu_keyboard(is_admin: bool = False):
    """Главное меню"""
    buttons = [
        [
            KeyboardButton(
                text="Создать тикет",
                icon_custom_emoji_id=EMOJI['box']
            ),
            KeyboardButton(
                text="Мои тикеты",
                icon_custom_emoji_id=EMOJI['tag']
            )
        ]
    ]
    
    if is_admin:
        buttons.append([
            KeyboardButton(
                text="Админ панель",
                icon_custom_emoji_id=EMOJI['settings']
            )
        ])
    
    return ReplyKeyboardMarkup(
        keyboard=buttons,
        resize_keyboard=True
    )

def problem_type_keyboard():
    """Клавиатура выбора типа проблемы"""
    builder = InlineKeyboardBuilder()
    
    builder.button(
        text="Заказ не выполняется очень долго",
        callback_data="problem_delay",
        icon_custom_emoji_id=EMOJI['clock']
    )
    builder.button(
        text="Ошибка в создание заказа",
        callback_data="problem_order_error",
        icon_custom_emoji_id=EMOJI['code']
    )
    builder.button(
        text="Ошибка пополнения баланса",
        callback_data="problem_balance_error",
        icon_custom_emoji_id=EMOJI['cross']
    )
    builder.button(
        text="Пополнение баланса ЮМАНИ",
        callback_data="problem_yumani",
        icon_custom_emoji_id=EMOJI['link']
    )
    builder.button(
        text="Другое",
        callback_data="problem_other",
        icon_custom_emoji_id=EMOJI['paperclip']
    )
    builder.button(
        text="◁ Назад",
        callback_data="back_to_main"
    )
    
    builder.adjust(1)
    return builder.as_markup()

def admin_panel_keyboard():
    """Клавиатура админ-панели"""
    builder = InlineKeyboardBuilder()
    
    builder.button(
        text="Статистика",
        callback_data="admin_stats",
        icon_custom_emoji_id=EMOJI['stats']
    )
    builder.button(
        text="Рассылка",
        callback_data="admin_broadcast",
        icon_custom_emoji_id=EMOJI['megaphone']
    )
    builder.button(
        text="Назначение поддержки",
        callback_data="admin_manage_support",
        icon_custom_emoji_id=EMOJI['people']
    )
    builder.button(
        text="Имя поддержки",
        callback_data="admin_display_names",
        icon_custom_emoji_id=EMOJI['pencil']
    )
    builder.button(
        text="◁ Назад",
        callback_data="back_to_main"
    )
    
    builder.adjust(1)
    return builder.as_markup()

def support_panel_keyboard():
    """Клавиатура панели поддержки"""
    builder = InlineKeyboardBuilder()
    
    builder.button(
        text="Открытые тикеты",
        callback_data="support_open_tickets",
        icon_custom_emoji_id=EMOJI['box']
    )
    builder.button(
        text="◁ Назад",
        callback_data="back_to_main"
    )
    
    builder.adjust(1)
    return builder.as_markup()

def tickets_list_keyboard(tickets: List[Dict[str, Any]], prefix: str = "ticket"):
    """Клавиатура со списком тикетов"""
    builder = InlineKeyboardBuilder()
    
    for ticket in tickets:
        status_emoji = EMOJI['check'] if ticket['status'] == 'open' else EMOJI['lock']
        builder.button(
            text=f"№{ticket['ticket_number']} - {ticket['problem_type'][:30]}",
            callback_data=f"{prefix}_{ticket['id']}",
            icon_custom_emoji_id=status_emoji
        )
    
    builder.button(
        text="◁ Назад",
        callback_data="back_to_main"
    )
    
    builder.adjust(1)
    return builder.as_markup()

def ticket_actions_keyboard(ticket_id: int, is_support: bool = False):
    """Клавиатура действий с тикетом"""
    builder = InlineKeyboardBuilder()
    
    if not is_support:
        builder.button(
            text="Отправить новое сообщение",
            callback_data=f"new_msg_{ticket_id}",
            icon_custom_emoji_id=EMOJI['send']
        )
    
    if is_support:
        builder.button(
            text="Ответить",
            callback_data=f"reply_{ticket_id}",
            icon_custom_emoji_id=EMOJI['pencil']
        )
        builder.button(
            text="Закрыть тикет",
            callback_data=f"close_{ticket_id}",
            icon_custom_emoji_id=EMOJI['lock']
        )
    
    builder.button(
        text="◁ Назад",
        callback_data="back_to_tickets" if not is_support else "back_to_support"
    )
    
    builder.adjust(1)
    return builder.as_markup()

# Обработчики команд
@dp.message(Command("start"))
async def cmd_start(message: Message):
    """Обработчик команды /start"""
    user_data = await get_or_create_user(
        message.from_user.id,
        message.from_user.username,
        message.from_user.full_name
    )
    
    welcome_text = (
        f'<b><tg-emoji emoji-id="{EMOJI["gift"]}">🎁</tg-emoji> '
        f'Добро пожаловать в техподдержку Vest Smm!</b>\n\n'
        f'<tg-emoji emoji-id="{EMOJI["box"]}">📦</tg-emoji> '
        f'Здесь вы можете создать тикет для решения вашей проблемы.\n'
        f'<tg-emoji emoji-id="{EMOJI["tag"]}">🏷</tg-emoji> '
        f'Отслеживайте статус ваших обращений в разделе "Мои тикеты".'
    )
    
    await message.answer(
        welcome_text,
        reply_markup=main_menu_keyboard(user_data['is_admin'])
    )

@dp.message(Command("admin"))
@dp.message(F.text.contains("Админ панель"))
async def admin_panel(message: Message):
    """Админ-панель"""
    user_data = await get_or_create_user(
        message.from_user.id,
        message.from_user.username,
        message.from_user.full_name
    )
    
    if not user_data['is_admin']:
        await message.answer(
            f'<tg-emoji emoji-id="{EMOJI["lock"]}">🔒</tg-emoji> '
            f'У вас нет доступа к админ-панели.'
        )
        return
    
    await message.answer(
        f'<b><tg-emoji emoji-id="{EMOJI["settings"]}">⚙</tg-emoji> '
        f'Админ-панель</b>\n\n'
        f'Выберите действие:',
        reply_markup=admin_panel_keyboard()
    )

@dp.message(Command("supp"))
async def support_panel(message: Message):
    """Панель поддержки"""
    user_data = await get_or_create_user(
        message.from_user.id,
        message.from_user.username,
        message.from_user.full_name
    )
    
    if not user_data['is_support']:
        await message.answer(
            f'<tg-emoji emoji-id="{EMOJI["lock"]}">🔒</tg-emoji> '
            f'У вас нет доступа к панели поддержки.'
        )
        return
    
    await message.answer(
        f'<b><tg-emoji emoji-id="{EMOJI["people"]}">👥</tg-emoji> '
        f'Панель поддержки</b>\n\n'
        f'Выберите действие:',
        reply_markup=support_panel_keyboard()
    )

@dp.message(F.text == "Создать тикет")
async def create_ticket_start(message: Message, state: FSMContext):
    """Начало создания тикета"""
    await message.answer(
        f'<b><tg-emoji emoji-id="{EMOJI["box"]}">📦</tg-emoji> '
        f'Выберите тип проблемы:</b>',
        reply_markup=problem_type_keyboard()
    )
    await state.set_state(TicketCreation.waiting_for_problem_type)

@dp.message(F.text == "Мои тикеты")
async def my_tickets(message: Message):
    """Просмотр тикетов пользователя"""
    user_data = await get_or_create_user(
        message.from_user.id,
        message.from_user.username,
        message.from_user.full_name
    )
    
    tickets = await get_user_tickets(user_data['id'])
    
    if not tickets:
        await message.answer(
            f'<tg-emoji emoji-id="{EMOJI["tag"]}">🏷</tg-emoji> '
            f'У вас пока нет созданных тикетов.'
        )
        return
    
    await message.answer(
        f'<b><tg-emoji emoji-id="{EMOJI["tag"]}">🏷</tg-emoji> '
        f'Ваши тикеты:</b>',
        reply_markup=tickets_list_keyboard(tickets, "my_ticket")
    )

# Обработчики callback-запросов
@dp.callback_query(F.data == "back_to_main")
async def back_to_main(callback: CallbackQuery, state: FSMContext):
    """Возврат в главное меню"""
    await state.clear()
    user_data = await get_or_create_user(
        callback.from_user.id,
        callback.from_user.username,
        callback.from_user.full_name
    )
    
    await callback.message.delete()
    await callback.message.answer(
        'Выберите действие:',
        reply_markup=main_menu_keyboard(user_data['is_admin'])
    )
    await callback.answer()

@dp.callback_query(F.data.startswith("problem_"))
async def select_problem_type(callback: CallbackQuery, state: FSMContext):
    """Выбор типа проблемы"""
    problem_types = {
        "problem_delay": "Заказ не выполняется очень долго",
        "problem_order_error": "Ошибка в создание заказа",
        "problem_balance_error": "Ошибка пополнения баланса",
        "problem_yumani": "Пополнение баланса ЮМАНИ",
        "problem_other": "Другое"
    }
    
    problem_type = problem_types.get(callback.data)
    await state.update_data(problem_type=problem_type)
    
    await callback.message.edit_text(
        f'<b><tg-emoji emoji-id="{EMOJI["pencil"]}">🖋</tg-emoji> '
        f'Опишите вашу проблему:</b>\n\n'
        f'<i>Тип проблемы: {problem_type}</i>\n\n'
        f'Отправьте текстовое сообщение с описанием.'
    )
    await state.set_state(TicketCreation.waiting_for_description)
    await callback.answer()

@dp.message(StateFilter(TicketCreation.waiting_for_description))
async def process_ticket_description(message: Message, state: FSMContext):
    """Обработка описания тикета"""
    data = await state.get_data()
    problem_type = data.get('problem_type')
    
    user_data = await get_or_create_user(
        message.from_user.id,
        message.from_user.username,
        message.from_user.full_name
    )
    
    ticket_id = await create_ticket(
        user_data['id'],
        problem_type,
        message.text
    )
    
    # Получаем созданный тикет для получения номера
    async with db_pool.acquire() as conn:
        ticket = await conn.fetchrow(
            "SELECT * FROM tickets WHERE id = $1", ticket_id
        )
    
    await message.answer(
        f'<b><tg-emoji emoji-id="{EMOJI["check"]}">✅</tg-emoji> '
        f'Тикет создан!</b>\n\n'
        f'<tg-emoji emoji-id="{EMOJI["tag"]}">🏷</tg-emoji> '
        f'Номер тикета: {ticket["ticket_number"]}\n'
        f'<tg-emoji emoji-id="{EMOJI["clock"]}">⏰</tg-emoji> '
        f'Тип проблемы: {problem_type}\n'
        f'<tg-emoji emoji-id="{EMOJI["file"]}">📁</tg-emoji> '
        f'Статус: Открыт\n\n'
        f'<i>Ожидайте ответа от службы поддержки.</i>',
        reply_markup=main_menu_keyboard(user_data['is_admin'])
    )
    
    # Уведомляем поддержку и админов
    support_users = await get_support_users()
    for support_user in support_users:
        try:
            await bot.send_message(
                support_user['telegram_id'],
                f'<b><tg-emoji emoji-id="{EMOJI["notification"]}">🔔</tg-emoji> '
                f'Новый тикет!</b>\n\n'
                f'<tg-emoji emoji-id="{EMOJI["tag"]}">🏷</tg-emoji> '
                f'Номер: {ticket["ticket_number"]}\n'
                f'<tg-emoji emoji-id="{EMOJI["people"]}">👥</tg-emoji> '
                f'Пользователь: {user_data["full_name"]} (@{user_data.get("username", "нет")})\n'
                f'<tg-emoji emoji-id="{EMOJI["clock"]}">⏰</tg-emoji> '
                f'Тип: {problem_type}\n'
                f'<tg-emoji emoji-id="{EMOJI["file"]}">📁</tg-emoji> '
                f'Текст: {message.text[:200]}...'
            )
        except Exception as e:
            logger.error(f"Failed to notify support user {support_user['telegram_id']}: {e}")
    
    await state.clear()

@dp.callback_query(F.data.startswith("my_ticket_"))
async def view_my_ticket(callback: CallbackQuery):
    """Просмотр тикета пользователем"""
    ticket_id = int(callback.data.split("_")[2])
    
    async with db_pool.acquire() as conn:
        ticket = await conn.fetchrow("SELECT * FROM tickets WHERE id = $1", ticket_id)
    
    if not ticket:
        await callback.answer("Тикет не найден")
        return
    
    messages = await get_ticket_messages(ticket_id)
    
    text = (
        f'<b><tg-emoji emoji-id="{EMOJI["tag"]}">🏷</tg-emoji> '
        f'Тикет {ticket["ticket_number"]}</b>\n\n'
        f'<tg-emoji emoji-id="{EMOJI["clock"]}">⏰</tg-emoji> '
        f'Тип: {ticket["problem_type"]}\n'
        f'<tg-emoji emoji-id="{EMOJI["file"]}">📁</tg-emoji> '
        f'Статус: {"Открыт" if ticket["status"] == "open" else "Закрыт"}\n'
        f'<tg-emoji emoji-id="{EMOJI["clock"]}">⏰</tg-emoji> '
        f'Создан: {ticket["created_at"].strftime("%d.%m.%Y %H:%M")}\n\n'
        f'<b>История сообщений:</b>\n'
    )
    
    for msg in messages:
        sender = "Вы" if msg['is_from_user'] else (msg.get('support_display_name') or msg['full_name'])
        text += f"\n<b>{sender}:</b>\n{msg['message_text']}\n"
        text += f"<i>{msg['created_at'].strftime('%d.%m.%Y %H:%M')}</i>\n"
    
    if len(text) > 4000:
        text = text[:4000] + "\n\n<i>...сообщение обрезано</i>"
    
    await callback.message.edit_text(
        text,
        reply_markup=ticket_actions_keyboard(ticket_id, is_support=False)
    )
    await callback.answer()

@dp.callback_query(F.data.startswith("new_msg_"))
async def new_message_to_ticket(callback: CallbackQuery, state: FSMContext):
    """Отправка нового сообщения в тикет"""
    ticket_id = int(callback.data.split("_")[2])
    await state.update_data(ticket_id=ticket_id)
    
    await callback.message.edit_text(
        f'<b><tg-emoji emoji-id="{EMOJI["send"]}">⬆</tg-emoji> '
        f'Отправьте новое сообщение:</b>\n\n'
        f'<i>Введите текст сообщения, который будет добавлен в тикет.</i>'
    )
    await state.set_state(TicketReply.waiting_for_message)
    await callback.answer()

@dp.message(StateFilter(TicketReply.waiting_for_message))
async def process_ticket_reply(message: Message, state: FSMContext):
    """Обработка нового сообщения в тикет"""
    data = await state.get_data()
    ticket_id = data.get('ticket_id')
    
    user_data = await get_or_create_user(
        message.from_user.id,
        message.from_user.username,
        message.from_user.full_name
    )
    
    async with db_pool.acquire() as conn:
        await conn.execute(
            """INSERT INTO ticket_messages (ticket_id, sender_id, message_text, is_from_user) 
               VALUES ($1, $2, $3, TRUE)""",
            ticket_id, user_data['id'], message.text
        )
        
        ticket = await conn.fetchrow("SELECT * FROM tickets WHERE id = $1", ticket_id)
    
    await message.answer(
        f'<b><tg-emoji emoji-id="{EMOJI["check"]}">✅</tg-emoji> '
        f'Сообщение отправлено!</b>\n\n'
        f'<tg-emoji emoji-id="{EMOJI["tag"]}">🏷</tg-emoji> '
        f'Тикет: {ticket["ticket_number"]}',
        reply_markup=main_menu_keyboard(user_data['is_admin'])
    )
    
    # Уведомляем поддержку
    support_users = await get_support_users()
    for support_user in support_users:
        try:
            await bot.send_message(
                support_user['telegram_id'],
                f'<b><tg-emoji emoji-id="{EMOJI["notification"]}">🔔</tg-emoji> '
                f'Новое сообщение от пользователя!</b>\n\n'
                f'<tg-emoji emoji-id="{EMOJI["tag"]}">🏷</tg-emoji> '
                f'Тикет: {ticket["ticket_number"]}\n'
                f'<tg-emoji emoji-id="{EMOJI["people"]}">👥</tg-emoji> '
                f'От: {user_data["full_name"]}\n'
                f'<tg-emoji emoji-id="{EMOJI["file"]}">📁</tg-emoji> '
                f'Сообщение: {message.text}'
            )
        except Exception as e:
            logger.error(f"Failed to notify support: {e}")
    
    await state.clear()

@dp.callback_query(F.data == "support_open_tickets")
async def support_view_tickets(callback: CallbackQuery):
    """Просмотр открытых тикетов поддержкой"""
    user_data = await get_or_create_user(
        callback.from_user.id,
        callback.from_user.username,
        callback.from_user.full_name
    )
    
    if not user_data['is_support']:
        await callback.answer("Нет доступа")
        return
    
    tickets = await get_all_open_tickets()
    
    if not tickets:
        await callback.message.edit_text(
            f'<tg-emoji emoji-id="{EMOJI["check"]}">✅</tg-emoji> '
            f'Нет открытых тикетов.',
            reply_markup=support_panel_keyboard()
        )
        await callback.answer()
        return
    
    await callback.message.edit_text(
        f'<b><tg-emoji emoji-id="{EMOJI["box"]}">📦</tg-emoji> '
        f'Открытые тикеты:</b>',
        reply_markup=tickets_list_keyboard(tickets, "support_ticket")
    )
    await callback.answer()

@dp.callback_query(F.data.startswith("support_ticket_"))
async def support_view_ticket(callback: CallbackQuery):
    """Просмотр тикета поддержкой"""
    ticket_id = int(callback.data.split("_")[2])
    
    user_data = await get_or_create_user(
        callback.from_user.id,
        callback.from_user.username,
        callback.from_user.full_name
    )
    
    if not user_data['is_support']:
        await callback.answer("Нет доступа")
        return
    
    async with db_pool.acquire() as conn:
        ticket = await conn.fetchrow(
            """SELECT t.*, u.full_name, u.username 
               FROM tickets t 
               JOIN users u ON t.user_id = u.id 
               WHERE t.id = $1""", 
            ticket_id
        )
    
    if not ticket:
        await callback.answer("Тикет не найден")
        return
    
    messages = await get_ticket_messages(ticket_id)
    
    text = (
        f'<b><tg-emoji emoji-id="{EMOJI["tag"]}">🏷</tg-emoji> '
        f'Тикет {ticket["ticket_number"]}</b>\n\n'
        f'<tg-emoji emoji-id="{EMOJI["people"]}">👥</tg-emoji> '
        f'Пользователь: {ticket["full_name"]}'
    )
    
    if ticket['username']:
        text += f' (@{ticket["username"]})'
    
    text += (
        f'\n<tg-emoji emoji-id="{EMOJI["clock"]}">⏰</tg-emoji> '
        f'Тип: {ticket["problem_type"]}\n'
        f'<tg-emoji emoji-id="{EMOJI["file"]}">📁</tg-emoji> '
        f'Статус: {"Открыт" if ticket["status"] == "open" else "Закрыт"}\n\n'
        f'<b>История сообщений:</b>\n'
    )
    
    for msg in messages:
        sender = "Пользователь" if msg['is_from_user'] else (msg.get('support_display_name') or msg['full_name'])
        text += f"\n<b>{sender}:</b>\n{msg['message_text']}\n"
        text += f"<i>{msg['created_at'].strftime('%d.%m.%Y %H:%M')}</i>\n"
    
    if len(text) > 4000:
        text = text[:4000] + "\n\n<i>...сообщение обрезано</i>"
    
    await callback.message.edit_text(
        text,
        reply_markup=ticket_actions_keyboard(ticket_id, is_support=True)
    )
    await callback.answer()

@dp.callback_query(F.data.startswith("reply_"))
async def support_reply_start(callback: CallbackQuery, state: FSMContext):
    """Начало ответа поддержки"""
    ticket_id = int(callback.data.split("_")[1])
    await state.update_data(reply_ticket_id=ticket_id)
    
    await callback.message.edit_text(
        f'<b><tg-emoji emoji-id="{EMOJI["pencil"]}">🖋</tg-emoji> '
        f'Введите ответ пользователю:</b>\n\n'
        f'<i>Текст сообщения будет отправлен пользователю.</i>'
    )
    await state.set_state(SupportReply.waiting_for_reply)
    await callback.answer()

@dp.message(StateFilter(SupportReply.waiting_for_reply))
async def process_support_reply(message: Message, state: FSMContext):
    """Обработка ответа поддержки"""
    data = await state.get_data()
    ticket_id = data.get('reply_ticket_id')
    
    user_data = await get_or_create_user(
        message.from_user.id,
        message.from_user.username,
        message.from_user.full_name
    )
    
    if not user_data['is_support']:
        await message.answer(
            f'<tg-emoji emoji-id="{EMOJI["lock"]}">🔒</tg-emoji> Нет доступа.'
        )
        await state.clear()
        return
    
    async with db_pool.acquire() as conn:
        ticket = await conn.fetchrow("SELECT * FROM tickets WHERE id = $1", ticket_id)
        
        # Сохраняем ответ
        await conn.execute(
            """INSERT INTO ticket_messages (ticket_id, sender_id, message_text, is_from_user) 
               VALUES ($1, $2, $3, FALSE)""",
            ticket_id, user_data['id'], message.text
        )
        
        # Получаем данные пользователя
        ticket_user = await conn.fetchrow(
            "SELECT * FROM users WHERE id = $1", ticket['user_id']
        )
    
    # Отправляем ответ пользователю
    display_name = user_data.get('support_display_name') or user_data['full_name']
    
    try:
        await bot.send_message(
            ticket_user['telegram_id'],
            f'<b><tg-emoji emoji-id="{EMOJI["notification"]}">🔔</tg-emoji> '
            f'Ответ от поддержки</b>\n\n'
            f'<tg-emoji emoji-id="{EMOJI["tag"]}">🏷</tg-emoji> '
            f'Тикет: {ticket["ticket_number"]}\n\n'
            f'<b>{display_name}:</b>\n{message.text}'
        )
    except Exception as e:
        logger.error(f"Failed to send reply to user {ticket_user['telegram_id']}: {e}")
    
    await message.answer(
        f'<tg-emoji emoji-id="{EMOJI["check"]}">✅</tg-emoji> '
        f'Ответ отправлен пользователю!'
    )
    
    await state.clear()

@dp.callback_query(F.data.startswith("close_"))
async def close_ticket_handler(callback: CallbackQuery):
    """Закрытие тикета"""
    ticket_id = int(callback.data.split("_")[1])
    
    user_data = await get_or_create_user(
        callback.from_user.id,
        callback.from_user.username,
        callback.from_user.full_name
    )
    
    if not user_data['is_support']:
        await callback.answer("Нет доступа")
        return
    
    await close_ticket(ticket_id)
    
    async with db_pool.acquire() as conn:
        ticket = await conn.fetchrow("SELECT * FROM tickets WHERE id = $1", ticket_id)
        ticket_user = await conn.fetchrow(
            "SELECT * FROM users WHERE id = $1", ticket['user_id']
        )
    
    # Уведомляем пользователя
    try:
        await bot.send_message(
            ticket_user['telegram_id'],
            f'<b><tg-emoji emoji-id="{EMOJI["lock"]}">🔒</tg-emoji> '
            f'Тикет закрыт</b>\n\n'
            f'<tg-emoji emoji-id="{EMOJI["tag"]}">🏷</tg-emoji> '
            f'Тикет {ticket["ticket_number"]} был закрыт.\n'
            f'Если проблема не решена, создайте новый тикет.'
        )
    except Exception as e:
        logger.error(f"Failed to notify user about ticket closure: {e}")
    
    await callback.message.edit_text(
        f'<tg-emoji emoji-id="{EMOJI["check"]}">✅</tg-emoji> '
        f'Тикет {ticket["ticket_number"]} закрыт.',
        reply_markup=support_panel_keyboard()
    )
    await callback.answer()

@dp.callback_query(F.data == "admin_stats")
async def admin_statistics(callback: CallbackQuery):
    """Статистика для админа"""
    user_data = await get_or_create_user(
        callback.from_user.id,
        callback.from_user.username,
        callback.from_user.full_name
    )
    
    if not user_data['is_admin']:
        await callback.answer("Нет доступа")
        return
    
    stats = await get_statistics()
    
    text = (
        f'<b><tg-emoji emoji-id="{EMOJI["stats"]}">📊</tg-emoji> '
        f'Статистика</b>\n\n'
        f'<tg-emoji emoji-id="{EMOJI["people"]}">👥</tg-emoji> '
        f'Всего пользователей: {stats["total_users"]}\n'
        f'<tg-emoji emoji-id="{EMOJI["box"]}">📦</tg-emoji> '
        f'Открытых тикетов: {stats["open_tickets"]}\n'
        f'<tg-emoji emoji-id="{EMOJI["lock"]}">🔒</tg-emoji> '
        f'Закрытых тикетов: {stats["closed_tickets"]}\n'
        f'<tg-emoji emoji-id="{EMOJI["file"]}">📁</tg-emoji> '
        f'Всего сообщений: {stats["total_messages"]}'
    )
    
    await callback.message.edit_text(
        text,
        reply_markup=admin_panel_keyboard()
    )
    await callback.answer()

@dp.callback_query(F.data == "admin_broadcast")
async def admin_broadcast_start(callback: CallbackQuery, state: FSMContext):
    """Начало рассылки"""
    user_data = await get_or_create_user(
        callback.from_user.id,
        callback.from_user.username,
        callback.from_user.full_name
    )
    
    if not user_data['is_admin']:
        await callback.answer("Нет доступа")
        return
    
    await callback.message.edit_text(
        f'<b><tg-emoji emoji-id="{EMOJI["megaphone"]}">📣</tg-emoji> '
        f'Создание рассылки</b>\n\n'
        f'Отправьте текст сообщения для рассылки.'
    )
    await state.set_state(BroadcastMessage.waiting_for_message)
    await callback.answer()

@dp.message(StateFilter(BroadcastMessage.waiting_for_message))
async def broadcast_send(message: Message, state: FSMContext):
    """Отправка рассылки"""
    user_data = await get_or_create_user(
        message.from_user.id,
        message.from_user.username,
        message.from_user.full_name
    )
    
    if not user_data['is_admin']:
        await message.answer("Нет доступа")
        await state.clear()
        return
    
    users = await get_all_users()
    success_count = 0
    fail_count = 0
    
    await message.answer(
        f'<tg-emoji emoji-id="{EMOJI["send"]}">⬆</tg-emoji> '
        f'Начинаю рассылку...'
    )
    
    for user in users:
        try:
            await bot.send_message(
                user['telegram_id'],
                f'<b><tg-emoji emoji-id="{EMOJI["megaphone"]}">📣</tg-emoji> '
                f'Рассылка от Vest Smm</b>\n\n{message.text}'
            )
            success_count += 1
            await asyncio.sleep(0.05)  # Защита от флуда
        except Exception as e:
            fail_count += 1
            logger.error(f"Failed to send broadcast to {user['telegram_id']}: {e}")
    
    await message.answer(
        f'<b><tg-emoji emoji-id="{EMOJI["check"]}">✅</tg-emoji> '
        f'Рассылка завершена!</b>\n\n'
        f'<tg-emoji emoji-id="{EMOJI["check"]}">✅</tg-emoji> '
        f'Успешно: {success_count}\n'
        f'<tg-emoji emoji-id="{EMOJI["cross"]}">❌</tg-emoji> '
        f'Не удалось: {fail_count}',
        reply_markup=main_menu_keyboard(user_data['is_admin'])
    )
    await state.clear()

@dp.callback_query(F.data == "admin_manage_support")
async def admin_manage_support(callback: CallbackQuery):
    """Управление поддержкой"""
    user_data = await get_or_create_user(
        callback.from_user.id,
        callback.from_user.username,
        callback.from_user.full_name
    )
    
    if not user_data['is_admin']:
        await callback.answer("Нет доступа")
        return
    
    support_users = await get_support_users()
    
    text = (
        f'<b><tg-emoji emoji-id="{EMOJI["people"]}">👥</tg-emoji> '
        f'Управление поддержкой</b>\n\n'
        f'<i>Для добавления сотрудника используйте команду:\n'
        f'/add_support ID_пользователя\n'
        f'Для удаления:\n'
        f'/remove_support ID_пользователя</i>\n\n'
        f'<b>Текущие сотрудники поддержки:</b>\n'
    )
    
    for user in support_users:
        text += (
            f'\n<tg-emoji emoji-id="{EMOJI["profile"]}">👤</tg-emoji> '
            f'{user["full_name"]}'
        )
        if user['username']:
            text += f' (@{user["username"]})'
        text += f' - ID: {user["telegram_id"]}'
    
    await callback.message.edit_text(text, reply_markup=admin_panel_keyboard())
    await callback.answer()

@dp.message(Command("add_support"))
async def add_support_user(message: Message):
    """Добавление сотрудника поддержки"""
    user_data = await get_or_create_user(
        message.from_user.id,
        message.from_user.username,
        message.from_user.full_name
    )
    
    if not user_data['is_admin']:
        await message.answer("Нет доступа")
        return
    
    try:
        target_id = int(message.text.split()[1])
    except (IndexError, ValueError):
        await message.answer(
            f'<tg-emoji emoji-id="{EMOJI["cross"]}">❌</tg-emoji> '
            f'Используйте: /add_support ID_пользователя'
        )
        return
    
    async with db_pool.acquire() as conn:
        await conn.execute(
            "UPDATE users SET is_support = TRUE WHERE telegram_id = $1",
            target_id
        )
    
    await message.answer(
        f'<tg-emoji emoji-id="{EMOJI["check"]}">✅</tg-emoji> '
        f'Пользователь {target_id} добавлен в поддержку!'
    )

@dp.message(Command("remove_support"))
async def remove_support_user(message: Message):
    """Удаление сотрудника поддержки"""
    user_data = await get_or_create_user(
        message.from_user.id,
        message.from_user.username,
        message.from_user.full_name
    )
    
    if not user_data['is_admin']:
        await message.answer("Нет доступа")
        return
    
    try:
        target_id = int(message.text.split()[1])
    except (IndexError, ValueError):
        await message.answer(
            f'<tg-emoji emoji-id="{EMOJI["cross"]}">❌</tg-emoji> '
            f'Используйте: /remove_support ID_пользователя'
        )
        return
    
    async with db_pool.acquire() as conn:
        await conn.execute(
            "UPDATE users SET is_support = FALSE WHERE telegram_id = $1",
            target_id
        )
    
    await message.answer(
        f'<tg-emoji emoji-id="{EMOJI["check"]}">✅</tg-emoji> '
        f'Пользователь {target_id} удален из поддержки!'
    )

@dp.callback_query(F.data == "admin_display_names")
async def admin_display_names(callback: CallbackQuery):
    """Управление отображаемыми именами"""
    user_data = await get_or_create_user(
        callback.from_user.id,
        callback.from_user.username,
        callback.from_user.full_name
    )
    
    if not user_data['is_admin']:
        await callback.answer("Нет доступа")
        return
    
    support_users = await get_support_users()
    
    if not support_users:
        await callback.message.edit_text(
            f'<tg-emoji emoji-id="{EMOJI["cross"]}">❌</tg-emoji> '
            f'Нет сотрудников поддержки.',
            reply_markup=admin_panel_keyboard()
        )
        await callback.answer()
        return
    
    builder = InlineKeyboardBuilder()
    
    for user in support_users:
        current_name = user.get('support_display_name') or user['full_name']
        builder.button(
            text=f"{current_name}",
            callback_data=f"set_name_{user['telegram_id']}",
            icon_custom_emoji_id=EMOJI['pencil']
        )
    
    builder.button(text="◁ Назад", callback_data="back_to_admin")
    builder.adjust(1)
    
    await callback.message.edit_text(
        f'<b><tg-emoji emoji-id="{EMOJI["pencil"]}">🖋</tg-emoji> '
        f'Выберите сотрудника для изменения имени:</b>',
        reply_markup=builder.as_markup()
    )
    await callback.answer()

@dp.callback_query(F.data == "back_to_admin")
async def back_to_admin(callback: CallbackQuery):
    """Возврат в админ-панель"""
    await callback.message.edit_text(
        f'<b><tg-emoji emoji-id="{EMOJI["settings"]}">⚙</tg-emoji> '
        f'Админ-панель</b>',
        reply_markup=admin_panel_keyboard()
    )
    await callback.answer()

@dp.callback_query(F.data.startswith("set_name_"))
async def set_display_name_start(callback: CallbackQuery, state: FSMContext):
    """Начало установки отображаемого имени"""
    target_id = int(callback.data.split("_")[2])
    await state.update_data(name_target_id=target_id)
    
    await callback.message.edit_text(
        f'<b><tg-emoji emoji-id="{EMOJI["pencil"]}">🖋</tg-emoji> '
        f'Введите новое отображаемое имя:</b>\n\n'
        f'<i>Это имя будет отображаться пользователям при ответах поддержки.</i>'
    )
    await state.set_state(SetDisplayName.waiting_for_name)
    await callback.answer()

@dp.message(StateFilter(SetDisplayName.waiting_for_name))
async def process_display_name(message: Message, state: FSMContext):
    """Обработка установки отображаемого имени"""
    data = await state.get_data()
    target_id = data.get('name_target_id')
    
    user_data = await get_or_create_user(
        message.from_user.id,
        message.from_user.username,
        message.from_user.full_name
    )
    
    if not user_data['is_admin']:
        await message.answer("Нет доступа")
        await state.clear()
        return
    
    async with db_pool.acquire() as conn:
        await conn.execute(
            "UPDATE users SET support_display_name = $1 WHERE telegram_id = $2",
            message.text, target_id
        )
    
    await message.answer(
        f'<tg-emoji emoji-id="{EMOJI["check"]}">✅</tg-emoji> '
        f'Отображаемое имя установлено: {message.text}',
        reply_markup=main_menu_keyboard(user_data['is_admin'])
    )
    await state.clear()

@dp.callback_query(F.data == "back_to_support")
async def back_to_support(callback: CallbackQuery):
    """Возврат в панель поддержки"""
    await callback.message.edit_text(
        f'<b><tg-emoji emoji-id="{EMOJI["people"]}">👥</tg-emoji> '
        f'Панель поддержки</b>',
        reply_markup=support_panel_keyboard()
    )
    await callback.answer()

@dp.callback_query(F.data == "back_to_tickets")
async def back_to_tickets(callback: CallbackQuery):
    """Возврат к списку тикетов"""
    await my_tickets(callback.message)

async def main():
    """Основная функция запуска бота"""
    await init_db()
    
    logger.info("Bot started successfully")
    
    try:
        await dp.start_polling(bot)
    finally:
        if db_pool:
            await db_pool.close()

if __name__ == "__main__":
    asyncio.run(main())
