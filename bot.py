import asyncio
import logging
import os
import sys
from datetime import datetime, time
from typing import Optional, List, Dict, Any
import json

from aiogram import Bot, Dispatcher, types, F
from aiogram.enums import ParseMode
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.client.default import DefaultBotProperties
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardMarkup, 
    InlineKeyboardButton, ReplyKeyboardMarkup, KeyboardButton,
    InputMediaPhoto, InputMediaVideo, InputMediaDocument
)
from aiogram.utils.keyboard import InlineKeyboardBuilder
import asyncpg
from asyncpg import Pool
import pytz

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
MOSCOW_TZ = pytz.timezone('Europe/Moscow')

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
    'photo': '5870724626963171315',
    'video': '5870763177832223236',
    'document': '5870803390662381087',
    'star': '5870842899083105787',
    'fire': '5870862934020661505',
    'warning': '5870882280778067046',
    'low_priority': '6037359615987670571',
    'high_priority': '6037389475940541834',
    'critical_priority': '6037409316043757123',
    'history': '6039507426561303522',
}

# Приоритеты
PRIORITY_LEVELS = {
    'low': {'name': 'Низкий', 'emoji': 'low_priority', 'color': '🟢'},
    'medium': {'name': 'Средний', 'emoji': 'star', 'color': '🟡'},
    'high': {'name': 'Высокий', 'emoji': 'high_priority', 'color': '🟠'},
    'critical': {'name': 'Критический', 'emoji': 'critical_priority', 'color': '🔴'}
}

# Инициализация бота и диспетчера
bot = Bot(
    token=BOT_TOKEN,
    default=DefaultBotProperties(parse_mode=ParseMode.HTML)
)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)
db_pool: Optional[Pool] = None

# Состояния FSM
class TicketCreation(StatesGroup):
    waiting_for_category = State()
    waiting_for_priority = State()
    waiting_for_description = State()
    waiting_for_media = State()

class TicketReply(StatesGroup):
    waiting_for_message = State()
    waiting_for_media = State()

class SupportReply(StatesGroup):
    waiting_for_reply = State()
    waiting_for_media = State()

class BroadcastMessage(StatesGroup):
    waiting_for_message = State()
    waiting_for_media = State()

class SetDisplayName(StatesGroup):
    waiting_for_name = State()

# Функции работы с БД
async def init_db():
    """Инициализация базы данных"""
    global db_pool
    
    if not DATABASE_URL:
        logger.error("DATABASE_URL не установлен")
        sys.exit(1)
    
    try:
        db_pool = await asyncpg.create_pool(DATABASE_URL)
        
        async with db_pool.acquire() as conn:
            # Создаем таблицы
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    id SERIAL PRIMARY KEY,
                    telegram_id BIGINT UNIQUE NOT NULL,
                    username VARCHAR(255),
                    full_name VARCHAR(255),
                    is_admin BOOLEAN DEFAULT FALSE,
                    is_support BOOLEAN DEFAULT FALSE,
                    support_display_name VARCHAR(255),
                    is_vip BOOLEAN DEFAULT FALSE,
                    created_at TIMESTAMP DEFAULT NOW()
                )
            """)
            
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS tickets (
                    id SERIAL PRIMARY KEY,
                    user_id INTEGER REFERENCES users(id),
                    ticket_number VARCHAR(50) UNIQUE NOT NULL,
                    category VARCHAR(255),
                    problem_type VARCHAR(255),
                    priority VARCHAR(20) DEFAULT 'medium',
                    status VARCHAR(20) DEFAULT 'open',
                    is_collaboration BOOLEAN DEFAULT FALSE,
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
                    media_type VARCHAR(50),
                    media_file_id TEXT,
                    is_from_user BOOLEAN DEFAULT TRUE,
                    created_at TIMESTAMP DEFAULT NOW()
                )
            """)
            
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS support_logs (
                    id SERIAL PRIMARY KEY,
                    support_id INTEGER REFERENCES users(id),
                    action_type VARCHAR(50),
                    ticket_id INTEGER REFERENCES tickets(id),
                    details TEXT,
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
    except Exception as e:
        logger.error(f"Failed to initialize database: {e}")
        sys.exit(1)

async def add_support_log(support_id: int, action_type: str, ticket_id: int = None, details: str = ""):
    """Добавление лога действий поддержки"""
    try:
        async with db_pool.acquire() as conn:
            await conn.execute(
                """INSERT INTO support_logs (support_id, action_type, ticket_id, details) 
                   VALUES ($1, $2, $3, $4)""",
                support_id, action_type, ticket_id, details
            )
    except Exception as e:
        logger.error(f"Error adding support log: {e}")

async def get_support_logs(limit: int = 50) -> List[Dict[str, Any]]:
    """Получение логов поддержки"""
    try:
        async with db_pool.acquire() as conn:
            logs = await conn.fetch(
                """SELECT sl.*, u.full_name, u.username 
                   FROM support_logs sl 
                   JOIN users u ON sl.support_id = u.id 
                   ORDER BY sl.created_at DESC LIMIT $1""",
                limit
            )
            return [dict(log) for log in logs]
    except Exception as e:
        logger.error(f"Error getting support logs: {e}")
        return []

def is_working_hours() -> bool:
    """Проверка рабочего времени (9:00-22:00 МСК)"""
    now = datetime.now(MOSCOW_TZ)
    return time(9, 0) <= now.time() <= time(22, 0)

async def get_or_create_user(telegram_id: int, username: str, full_name: str) -> Dict[str, Any]:
    """Получить или создать пользователя"""
    try:
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
                logger.info(f"New user created: {telegram_id} - {full_name}")
            
            return dict(user)
    except Exception as e:
        logger.error(f"Error in get_or_create_user: {e}")
        raise

async def generate_ticket_number() -> str:
    """Генерация номера тикета"""
    try:
        async with db_pool.acquire() as conn:
            count = await conn.fetchval("SELECT COUNT(*) FROM tickets")
            return f"TKT-{count + 1:04d}"
    except Exception as e:
        logger.error(f"Error generating ticket number: {e}")
        return "TKT-0001"

async def create_ticket(user_id: int, category: str, problem_type: str, 
                       description: str, priority: str = 'medium', 
                       is_collaboration: bool = False,
                       media_type: str = None, media_file_id: str = None) -> Optional[int]:
    """Создание нового тикета"""
    try:
        ticket_number = await generate_ticket_number()
        
        async with db_pool.acquire() as conn:
            # Создаем тикет
            ticket = await conn.fetchrow(
                """INSERT INTO tickets (user_id, ticket_number, category, problem_type, 
                   priority, is_collaboration) 
                   VALUES ($1, $2, $3, $4, $5, $6) 
                   RETURNING id, ticket_number""",
                user_id, ticket_number, category, problem_type, priority, is_collaboration
            )
            
            ticket_id = ticket['id']
            
            # Сохраняем первое сообщение
            await conn.execute(
                """INSERT INTO ticket_messages (ticket_id, sender_id, message_text, 
                   media_type, media_file_id, is_from_user) 
                   VALUES ($1, $2, $3, $4, $5, TRUE)""",
                ticket_id, user_id, description, media_type, media_file_id
            )
            
            logger.info(f"Ticket created: {ticket_number} (ID: {ticket_id}) by user {user_id}")
            return ticket_id
    except Exception as e:
        logger.error(f"Error creating ticket: {e}")
        return None

async def get_user_tickets(user_id: int) -> List[Dict[str, Any]]:
    """Получить тикеты пользователя"""
    try:
        async with db_pool.acquire() as conn:
            tickets = await conn.fetch(
                """SELECT * FROM tickets 
                   WHERE user_id = $1 
                   ORDER BY created_at DESC""",
                user_id
            )
            return [dict(t) for t in tickets]
    except Exception as e:
        logger.error(f"Error getting user tickets: {e}")
        return []

async def get_all_open_tickets(support_id: int = None) -> List[Dict[str, Any]]:
    """Получить все открытые тикеты"""
    try:
        async with db_pool.acquire() as conn:
            if support_id:
                # Для обычной поддержки - без collaboration
                tickets = await conn.fetch(
                    """SELECT t.*, u.full_name, u.username 
                       FROM tickets t 
                       JOIN users u ON t.user_id = u.id 
                       WHERE t.status = 'open' AND t.is_collaboration = FALSE
                       ORDER BY 
                         CASE t.priority 
                           WHEN 'critical' THEN 0 
                           WHEN 'high' THEN 1 
                           WHEN 'medium' THEN 2 
                           WHEN 'low' THEN 3 
                         END,
                         t.created_at ASC"""
                )
            else:
                # Для админа - все тикеты
                tickets = await conn.fetch(
                    """SELECT t.*, u.full_name, u.username 
                       FROM tickets t 
                       JOIN users u ON t.user_id = u.id 
                       WHERE t.status = 'open'
                       ORDER BY 
                         CASE t.priority 
                           WHEN 'critical' THEN 0 
                           WHEN 'high' THEN 1 
                           WHEN 'medium' THEN 2 
                           WHEN 'low' THEN 3 
                         END,
                         t.created_at ASC"""
                )
            return [dict(t) for t in tickets]
    except Exception as e:
        logger.error(f"Error getting open tickets: {e}")
        return []

async def get_ticket_messages(ticket_id: int) -> List[Dict[str, Any]]:
    """Получить сообщения тикета"""
    try:
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
    except Exception as e:
        logger.error(f"Error getting ticket messages: {e}")
        return []

async def close_ticket(ticket_id: int):
    """Закрыть тикет"""
    try:
        async with db_pool.acquire() as conn:
            await conn.execute(
                """UPDATE tickets 
                   SET status = 'closed', updated_at = NOW() 
                   WHERE id = $1""",
                ticket_id
            )
            logger.info(f"Ticket {ticket_id} closed")
    except Exception as e:
        logger.error(f"Error closing ticket: {e}")

async def get_support_users() -> List[Dict[str, Any]]:
    """Получить список поддержки"""
    try:
        async with db_pool.acquire() as conn:
            users = await conn.fetch(
                "SELECT * FROM users WHERE is_support = TRUE"
            )
            return [dict(u) for u in users]
    except Exception as e:
        logger.error(f"Error getting support users: {e}")
        return []

async def get_admin_user() -> Optional[Dict[str, Any]]:
    """Получить админа"""
    try:
        async with db_pool.acquire() as conn:
            user = await conn.fetchrow(
                "SELECT * FROM users WHERE telegram_id = $1", ADMIN_ID
            )
            return dict(user) if user else None
    except Exception as e:
        logger.error(f"Error getting admin user: {e}")
        return None

async def get_statistics() -> Dict[str, Any]:
    """Получить статистику"""
    try:
        async with db_pool.acquire() as conn:
            total_users = await conn.fetchval("SELECT COUNT(*) FROM users")
            open_tickets = await conn.fetchval("SELECT COUNT(*) FROM tickets WHERE status = 'open'")
            closed_tickets = await conn.fetchval("SELECT COUNT(*) FROM tickets WHERE status = 'closed'")
            total_messages = await conn.fetchval("SELECT COUNT(*) FROM ticket_messages")
            
            # Статистика по приоритетам
            priority_stats = await conn.fetch(
                """SELECT priority, COUNT(*) as count 
                   FROM tickets 
                   WHERE status = 'open' 
                   GROUP BY priority"""
            )
            
            return {
                'total_users': total_users,
                'open_tickets': open_tickets,
                'closed_tickets': closed_tickets,
                'total_messages': total_messages,
                'priority_stats': {p['priority']: p['count'] for p in priority_stats}
            }
    except Exception as e:
        logger.error(f"Error getting statistics: {e}")
        return {
            'total_users': 0,
            'open_tickets': 0,
            'closed_tickets': 0,
            'total_messages': 0,
            'priority_stats': {}
        }

async def get_all_users() -> List[Dict[str, Any]]:
    """Получить всех пользователей для рассылки"""
    try:
        async with db_pool.acquire() as conn:
            users = await conn.fetch("SELECT * FROM users")
            return [dict(u) for u in users]
    except Exception as e:
        logger.error(f"Error getting all users: {e}")
        return []

# Клавиатуры
def main_menu_keyboard(is_admin: bool = False):
    """Главное меню"""
    buttons = [
        [
            KeyboardButton(text="Создать тикет", icon_custom_emoji_id=EMOJI['box']),
            KeyboardButton(text="Мои тикеты", icon_custom_emoji_id=EMOJI['tag'])
        ]
    ]
    
    return ReplyKeyboardMarkup(keyboard=buttons, resize_keyboard=True)

def category_keyboard():
    """Клавиатура выбора категории"""
    builder = InlineKeyboardBuilder()
    
    categories = [
        ("Техническая проблема", "category_tech", EMOJI['code']),
        ("Проблема с заказом", "category_order", EMOJI['box']),
        ("Проблема с оплатой", "category_payment", EMOJI['link']),
        ("По сотрудничеству", "category_collaboration", EMOJI['star']),
        ("Другое", "category_other", EMOJI['paperclip'])
    ]
    
    for name, callback, emoji in categories:
        builder.button(text=name, callback_data=callback, icon_custom_emoji_id=emoji)
    
    builder.button(text="◁ Назад", callback_data="back_to_main")
    builder.adjust(1)
    return builder.as_markup()

def priority_keyboard():
    """Клавиатура выбора приоритета"""
    builder = InlineKeyboardBuilder()
    
    for key, value in PRIORITY_LEVELS.items():
        builder.button(
            text=f"{value['color']} {value['name']}",
            callback_data=f"priority_{key}",
            icon_custom_emoji_id=EMOJI[value['emoji']]
        )
    
    builder.button(text="◁ Назад", callback_data="back_to_categories")
    builder.adjust(1)
    return builder.as_markup()

def admin_panel_keyboard():
    """Клавиатура админ-панели"""
    builder = InlineKeyboardBuilder()
    
    builder.button(text="Статистика", callback_data="admin_stats", icon_custom_emoji_id=EMOJI['stats'])
    builder.button(text="Рассылка", callback_data="admin_broadcast", icon_custom_emoji_id=EMOJI['megaphone'])
    builder.button(text="Назначение поддержки", callback_data="admin_manage_support", icon_custom_emoji_id=EMOJI['people'])
    builder.button(text="Имя поддержки", callback_data="admin_display_names", icon_custom_emoji_id=EMOJI['pencil'])
    builder.button(text="Логи поддержки", callback_data="admin_logs", icon_custom_emoji_id=EMOJI['history'])
    builder.button(text="Тикеты по сотрудничеству", callback_data="admin_collaboration", icon_custom_emoji_id=EMOJI['star'])
    builder.button(text="◁ Закрыть", callback_data="close_admin")
    
    builder.adjust(1)
    return builder.as_markup()

def support_panel_keyboard():
    """Клавиатура панели поддержки"""
    builder = InlineKeyboardBuilder()
    
    builder.button(text="Открытые тикеты", callback_data="support_open_tickets", icon_custom_emoji_id=EMOJI['box'])
    
    if not is_working_hours():
        builder.button(
            text="⚠️ Нерабочее время (9:00-22:00 МСК)", 
            callback_data="working_hours_info",
            icon_custom_emoji_id=EMOJI['clock']
        )
    
    builder.button(text="◁ Закрыть", callback_data="close_support")
    builder.adjust(1)
    return builder.as_markup()

def tickets_list_keyboard(tickets: List[Dict[str, Any]], prefix: str = "ticket"):
    """Клавиатура со списком тикетов"""
    builder = InlineKeyboardBuilder()
    
    for ticket in tickets:
        priority_info = PRIORITY_LEVELS.get(ticket.get('priority', 'medium'), PRIORITY_LEVELS['medium'])
        status_emoji = EMOJI['check'] if ticket['status'] == 'open' else EMOJI['lock']
        
        label = f"{priority_info['color']} №{ticket['ticket_number']}"
        if ticket.get('is_collaboration'):
            label += " 🤝"
        
        builder.button(
            text=f"{label} - {ticket['category'][:20]}",
            callback_data=f"{prefix}_{ticket['id']}",
            icon_custom_emoji_id=status_emoji
        )
    
    builder.button(text="◁ Назад", callback_data="back_to_main")
    builder.adjust(1)
    return builder.as_markup()

def ticket_actions_keyboard(ticket_id: int, is_support: bool = False, has_media: bool = True):
    """Клавиатура действий с тикетом"""
    builder = InlineKeyboardBuilder()
    
    if not is_support:
        builder.button(text="Отправить новое сообщение", callback_data=f"new_msg_{ticket_id}", icon_custom_emoji_id=EMOJI['send'])
        if has_media:
            builder.button(text="Отправить фото/видео/файл", callback_data=f"new_media_{ticket_id}", icon_custom_emoji_id=EMOJI['photo'])
    
    if is_support:
        builder.button(text="Ответить текстом", callback_data=f"reply_{ticket_id}", icon_custom_emoji_id=EMOJI['pencil'])
        builder.button(text="Ответить с медиа", callback_data=f"reply_media_{ticket_id}", icon_custom_emoji_id=EMOJI['photo'])
        builder.button(text="Закрыть тикет", callback_data=f"close_{ticket_id}", icon_custom_emoji_id=EMOJI['lock'])
    
    builder.button(text="◁ Назад", callback_data="back_to_tickets" if not is_support else "back_to_support")
    builder.adjust(1)
    return builder.as_markup()

# Обработчики команд
@dp.message(Command("start"))
async def cmd_start(message: Message):
    """Обработчик команды /start"""
    try:
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
            f'Отслеживайте статус ваших обращений в разделе "Мои тикеты".\n'
            f'<tg-emoji emoji-id="{EMOJI["clock"]}">⏰</tg-emoji> '
            f'Поддержка работает с 9:00 до 22:00 по МСК.'
        )
        
        await message.answer(welcome_text, reply_markup=main_menu_keyboard())
    except Exception as e:
        logger.error(f"Error in start command: {e}")
        await message.answer("Произошла ошибка. Попробуйте позже.")

@dp.message(Command("admin"))
async def admin_command(message: Message):
    """Админ-панель по команде"""
    try:
        user_data = await get_or_create_user(
            message.from_user.id,
            message.from_user.username,
            message.from_user.full_name
        )
        
        if not user_data['is_admin']:
            await message.answer(f'<tg-emoji emoji-id="{EMOJI["lock"]}">🔒</tg-emoji> У вас нет доступа.')
            return
        
        await message.answer(
            f'<b><tg-emoji emoji-id="{EMOJI["settings"]}">⚙</tg-emoji> Админ-панель</b>\n\nВыберите действие:',
            reply_markup=admin_panel_keyboard()
        )
    except Exception as e:
        logger.error(f"Error in admin panel: {e}")

@dp.message(Command("supp"))
async def support_command(message: Message):
    """Панель поддержки по команде"""
    try:
        user_data = await get_or_create_user(
            message.from_user.id,
            message.from_user.username,
            message.from_user.full_name
        )
        
        if not user_data['is_support']:
            await message.answer(f'<tg-emoji emoji-id="{EMOJI["lock"]}">🔒</tg-emoji> У вас нет доступа.')
            return
        
        await message.answer(
            f'<b><tg-emoji emoji-id="{EMOJI["people"]}">👥</tg-emoji> Панель поддержки</b>\n\nВыберите действие:',
            reply_markup=support_panel_keyboard()
        )
    except Exception as e:
        logger.error(f"Error in support panel: {e}")

@dp.message(F.text == "Создать тикет")
async def create_ticket_start(message: Message, state: FSMContext):
    """Начало создания тикета"""
    try:
        await message.answer(
            f'<b><tg-emoji emoji-id="{EMOJI["box"]}">📦</tg-emoji> Выберите категорию:</b>',
            reply_markup=category_keyboard()
        )
        await state.set_state(TicketCreation.waiting_for_category)
    except Exception as e:
        logger.error(f"Error in create ticket start: {e}")

@dp.message(F.text == "Мои тикеты")
async def my_tickets(message: Message):
    """Просмотр тикетов пользователя"""
    try:
        user_data = await get_or_create_user(
            message.from_user.id,
            message.from_user.username,
            message.from_user.full_name
        )
        
        tickets = await get_user_tickets(user_data['id'])
        
        if not tickets:
            await message.answer(f'<tg-emoji emoji-id="{EMOJI["tag"]}">🏷</tg-emoji> У вас пока нет тикетов.')
            return
        
        await message.answer(
            f'<b><tg-emoji emoji-id="{EMOJI["tag"]}">🏷</tg-emoji> Ваши тикеты:</b>',
            reply_markup=tickets_list_keyboard(tickets, "my_ticket")
        )
    except Exception as e:
        logger.error(f"Error in my tickets: {e}")

@dp.callback_query(F.data.startswith("category_"))
async def select_category(callback: CallbackQuery, state: FSMContext):
    """Выбор категории"""
    try:
        categories = {
            "category_tech": "Техническая проблема",
            "category_order": "Проблема с заказом",
            "category_payment": "Проблема с оплатой",
            "category_collaboration": "По сотрудничеству",
            "category_other": "Другое"
        }
        
        category = categories.get(callback.data)
        is_collaboration = callback.data == "category_collaboration"
        
        await state.update_data(category=category, is_collaboration=is_collaboration)
        
        await callback.message.edit_text(
            f'<b><tg-emoji emoji-id="{EMOJI["warning"]}">⚠️</tg-emoji> Выберите приоритет:</b>\n\n'
            f'Категория: {category}',
            reply_markup=priority_keyboard()
        )
        await state.set_state(TicketCreation.waiting_for_priority)
        await callback.answer()
    except Exception as e:
        logger.error(f"Error in select category: {e}")
        await callback.answer("Произошла ошибка")

@dp.callback_query(F.data == "back_to_categories")
async def back_to_categories(callback: CallbackQuery, state: FSMContext):
    """Возврат к выбору категории"""
    await state.set_state(TicketCreation.waiting_for_category)
    await callback.message.edit_text(
        f'<b><tg-emoji emoji-id="{EMOJI["box"]}">📦</tg-emoji> Выберите категорию:</b>',
        reply_markup=category_keyboard()
    )
    await callback.answer()

@dp.callback_query(F.data.startswith("priority_"))
async def select_priority(callback: CallbackQuery, state: FSMContext):
    """Выбор приоритета"""
    try:
        priority = callback.data.split("_")[1]
        await state.update_data(priority=priority)
        
        priority_info = PRIORITY_LEVELS[priority]
        data = await state.get_data()
        
        await callback.message.edit_text(
            f'<b><tg-emoji emoji-id="{EMOJI["pencil"]}">🖋</tg-emoji> Опишите проблему:</b>\n\n'
            f'Категория: {data["category"]}\n'
            f'Приоритет: {priority_info["color"]} {priority_info["name"]}\n\n'
            f'<i>Отправьте текстовое сообщение с описанием.\n'
            f'Вы также можете прикрепить фото, видео или файл.</i>',
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="◁ Назад", callback_data="back_to_priorities")]
            ])
        )
        await state.set_state(TicketCreation.waiting_for_description)
        await callback.answer()
    except Exception as e:
        logger.error(f"Error in select priority: {e}")
        await callback.answer("Произошла ошибка")

@dp.callback_query(F.data == "back_to_priorities")
async def back_to_priorities(callback: CallbackQuery, state: FSMContext):
    """Возврат к выбору приоритета"""
    await state.set_state(TicketCreation.waiting_for_priority)
    await callback.message.edit_text(
        f'<b><tg-emoji emoji-id="{EMOJI["warning"]}">⚠️</tg-emoji> Выберите приоритет:</b>',
        reply_markup=priority_keyboard()
    )
    await callback.answer()

@dp.message(StateFilter(TicketCreation.waiting_for_description))
async def process_ticket_description(message: Message, state: FSMContext):
    """Обработка описания тикета с медиа"""
    try:
        data = await state.get_data()
        category = data.get('category')
        priority = data.get('priority', 'medium')
        is_collaboration = data.get('is_collaboration', False)
        
        if not category:
            await message.answer("Ошибка: категория не выбрана. Начните создание тикета заново.")
            await state.clear()
            return
        
        user_data = await get_or_create_user(
            message.from_user.id,
            message.from_user.username,
            message.from_user.full_name
        )
        
        # Определяем тип медиа
        media_type = None
        media_file_id = None
        
        if message.photo:
            media_type = 'photo'
            media_file_id = message.photo[-1].file_id
        elif message.video:
            media_type = 'video'
            media_file_id = message.video.file_id
        elif message.document:
            media_type = 'document'
            media_file_id = message.document.file_id
        elif message.voice:
            media_type = 'voice'
            media_file_id = message.voice.file_id
        
        description = message.text or message.caption or "Без описания"
        
        # Создаем тикет
        ticket_id = await create_ticket(
            user_data['id'], category, "Обращение в поддержку",
            description, priority, is_collaboration,
            media_type, media_file_id
        )
        
        if not ticket_id:
            await message.answer("Ошибка при создании тикета. Попробуйте позже.")
            await state.clear()
            return
        
        async with db_pool.acquire() as conn:
            ticket = await conn.fetchrow("SELECT * FROM tickets WHERE id = $1", ticket_id)
        
        priority_info = PRIORITY_LEVELS[priority]
        
        await message.answer(
            f'<b><tg-emoji emoji-id="{EMOJI["check"]}">✅</tg-emoji> Тикет создан!</b>\n\n'
            f'<tg-emoji emoji-id="{EMOJI["tag"]}">🏷</tg-emoji> Номер: {ticket["ticket_number"]}\n'
            f'<tg-emoji emoji-id="{EMOJI["file"]}">📁</tg-emoji> Категория: {category}\n'
            f'Приоритет: {priority_info["color"]} {priority_info["name"]}\n'
            f'<tg-emoji emoji-id="{EMOJI["file"]}">📁</tg-emoji> Статус: Открыт\n\n'
            f'<i>Ожидайте ответа.</i>',
            reply_markup=main_menu_keyboard()
        )
        
        # Уведомляем поддержку
        if is_collaboration:
            # Только админу
            admin = await get_admin_user()
            if admin:
                try:
                    await notify_new_ticket(admin, ticket, user_data, category, priority_info, description, media_type, media_file_id)
                except Exception as e:
                    logger.error(f"Failed to notify admin: {e}")
        else:
            # Всем сотрудникам поддержки
            support_users = await get_support_users()
            for support_user in support_users:
                try:
                    await notify_new_ticket(support_user, ticket, user_data, category, priority_info, description, media_type, media_file_id)
                except Exception as e:
                    logger.error(f"Failed to notify support: {e}")
        
        await state.clear()
    except Exception as e:
        logger.error(f"Error in process ticket description: {e}")
        await message.answer("Произошла ошибка при создании тикета.")
        await state.clear()

async def notify_new_ticket(user: Dict, ticket: Dict, user_data: Dict, 
                           category: str, priority_info: Dict, description: str,
                           media_type: str = None, media_file_id: str = None):
    """Отправка уведомления о новом тикете"""
    text = (
        f'<b><tg-emoji emoji-id="{EMOJI["notification"]}">🔔</tg-emoji> Новый тикет!</b>\n\n'
        f'<tg-emoji emoji-id="{EMOJI["tag"]}">🏷</tg-emoji> Номер: {ticket["ticket_number"]}\n'
        f'<tg-emoji emoji-id="{EMOJI["people"]}">👥</tg-emoji> От: {user_data["full_name"]}'
    )
    
    if user_data.get('username'):
        text += f' (@{user_data["username"]})'
    
    text += (
        f'\n<tg-emoji emoji-id="{EMOJI["file"]}">📁</tg-emoji> Категория: {category}\n'
        f'Приоритет: {priority_info["color"]} {priority_info["name"]}\n\n'
        f'<tg-emoji emoji-id="{EMOJI["file"]}">📁</tg-emoji> {description[:200]}'
    )
    
    if media_type:
        try:
            if media_type == 'photo':
                await bot.send_photo(user['telegram_id'], media_file_id, caption=text)
            elif media_type == 'video':
                await bot.send_video(user['telegram_id'], media_file_id, caption=text)
            elif media_type == 'document':
                await bot.send_document(user['telegram_id'], media_file_id, caption=text)
            elif media_type == 'voice':
                await bot.send_voice(user['telegram_id'], media_file_id, caption=text)
        except:
            await bot.send_message(user['telegram_id'], text)
    else:
        await bot.send_message(user['telegram_id'], text)

@dp.callback_query(F.data.startswith("my_ticket_"))
async def view_my_ticket(callback: CallbackQuery):
    """Просмотр тикета пользователем с медиа"""
    try:
        ticket_id = int(callback.data.split("_")[2])
        
        async with db_pool.acquire() as conn:
            ticket = await conn.fetchrow("SELECT * FROM tickets WHERE id = $1", ticket_id)
        
        if not ticket:
            await callback.answer("Тикет не найден")
            return
        
        messages = await get_ticket_messages(ticket_id)
        priority_info = PRIORITY_LEVELS.get(ticket['priority'], PRIORITY_LEVELS['medium'])
        
        text = (
            f'<b><tg-emoji emoji-id="{EMOJI["tag"]}">🏷</tg-emoji> Тикет {ticket["ticket_number"]}</b>\n\n'
            f'<tg-emoji emoji-id="{EMOJI["file"]}">📁</tg-emoji> Категория: {ticket["category"]}\n'
            f'Приоритет: {priority_info["color"]} {priority_info["name"]}\n'
            f'<tg-emoji emoji-id="{EMOJI["file"]}">📁</tg-emoji> Статус: {"Открыт" if ticket["status"] == "open" else "Закрыт"}\n\n'
            f'<b>История сообщений:</b>'
        )
        
        await callback.message.edit_text(text)
        
        # Отправляем медиа сообщения отдельно
        for msg in messages:
            sender = "Вы" if msg['is_from_user'] else (msg.get('support_display_name') or msg['full_name'])
            msg_text = f"<b>{sender}:</b>\n{msg['message_text']}\n<i>{msg['created_at'].strftime('%d.%m.%Y %H:%M')}</i>"
            
            if msg.get('media_type'):
                try:
                    if msg['media_type'] == 'photo':
                        await callback.message.answer_photo(msg['media_file_id'], caption=msg_text)
                    elif msg['media_type'] == 'video':
                        await callback.message.answer_video(msg['media_file_id'], caption=msg_text)
                    elif msg['media_type'] == 'document':
                        await callback.message.answer_document(msg['media_file_id'], caption=msg_text)
                    elif msg['media_type'] == 'voice':
                        await callback.message.answer_voice(msg['media_file_id'], caption=msg_text)
                except:
                    await callback.message.answer(msg_text)
            else:
                if len(msg_text) > 100:
                    await callback.message.answer(msg_text)
        
        await callback.message.answer(
            "Действия с тикетом:",
            reply_markup=ticket_actions_keyboard(ticket_id, is_support=False)
        )
        await callback.answer()
    except Exception as e:
        logger.error(f"Error viewing ticket: {e}")
        await callback.answer("Произошла ошибка")

@dp.callback_query(F.data.startswith("new_media_"))
async def new_media_to_ticket(callback: CallbackQuery, state: FSMContext):
    """Отправка медиа в тикет"""
    ticket_id = int(callback.data.split("_")[2])
    await state.update_data(media_ticket_id=ticket_id)
    
    await callback.message.edit_text(
        f'<b><tg-emoji emoji-id="{EMOJI["photo"]}">📷</tg-emoji> Отправьте медиа:</b>\n\n'
        f'<i>Вы можете отправить фото, видео, документ или голосовое сообщение.</i>'
    )
    await state.set_state(TicketReply.waiting_for_media)
    await callback.answer()

@dp.message(StateFilter(TicketReply.waiting_for_media))
async def process_ticket_media(message: Message, state: FSMContext):
    """Обработка медиа в тикет"""
    try:
        data = await state.get_data()
        ticket_id = data.get('media_ticket_id')
        
        user_data = await get_or_create_user(
            message.from_user.id,
            message.from_user.username,
            message.from_user.full_name
        )
        
        media_type = None
        media_file_id = None
        
        if message.photo:
            media_type = 'photo'
            media_file_id = message.photo[-1].file_id
        elif message.video:
            media_type = 'video'
            media_file_id = message.video.file_id
        elif message.document:
            media_type = 'document'
            media_file_id = message.document.file_id
        elif message.voice:
            media_type = 'voice'
            media_file_id = message.voice.file_id
        
        if not media_type:
            await message.answer("Пожалуйста, отправьте медиа-файл.")
            return
        
        async with db_pool.acquire() as conn:
            await conn.execute(
                """INSERT INTO ticket_messages (ticket_id, sender_id, message_text, 
                   media_type, media_file_id, is_from_user) 
                   VALUES ($1, $2, $3, $4, $5, TRUE)""",
                ticket_id, user_data['id'], message.caption or "Медиа-файл", 
                media_type, media_file_id
            )
            
            ticket = await conn.fetchrow("SELECT * FROM tickets WHERE id = $1", ticket_id)
        
        await message.answer(
            f'<tg-emoji emoji-id="{EMOJI["check"]}">✅</tg-emoji> Медиа отправлено!',
            reply_markup=main_menu_keyboard()
        )
        
        await state.clear()
    except Exception as e:
        logger.error(f"Error in process ticket media: {e}")
        await message.answer("Произошла ошибка.")
        await state.clear()

@dp.callback_query(F.data.startswith("new_msg_"))
async def new_message_to_ticket(callback: CallbackQuery, state: FSMContext):
    """Отправка нового сообщения в тикет"""
    try:
        ticket_id = int(callback.data.split("_")[2])
        await state.update_data(ticket_id=ticket_id)
        
        await callback.message.edit_text(
            f'<b><tg-emoji emoji-id="{EMOJI["send"]}">⬆</tg-emoji> Отправьте сообщение:</b>'
        )
        await state.set_state(TicketReply.waiting_for_message)
        await callback.answer()
    except Exception as e:
        logger.error(f"Error in new message: {e}")
        await callback.answer("Произошла ошибка")

@dp.message(StateFilter(TicketReply.waiting_for_message))
async def process_ticket_reply(message: Message, state: FSMContext):
    """Обработка нового сообщения в тикет"""
    try:
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
            f'<tg-emoji emoji-id="{EMOJI["check"]}">✅</tg-emoji> Сообщение отправлено!',
            reply_markup=main_menu_keyboard()
        )
        
        # Уведомляем поддержку
        support_users = await get_support_users()
        for support_user in support_users:
            try:
                await bot.send_message(
                    support_user['telegram_id'],
                    f'<b><tg-emoji emoji-id="{EMOJI["notification"]}">🔔</tg-emoji> Новое сообщение!</b>\n\n'
                    f'<tg-emoji emoji-id="{EMOJI["tag"]}">🏷</tg-emoji> Тикет: {ticket["ticket_number"]}\n'
                    f'От: {user_data["full_name"]}\n\n{message.text}'
                )
            except Exception as e:
                logger.error(f"Failed to notify support: {e}")
        
        await state.clear()
    except Exception as e:
        logger.error(f"Error in process ticket reply: {e}")
        await message.answer("Произошла ошибка.")
        await state.clear()

@dp.callback_query(F.data == "support_open_tickets")
async def support_view_tickets(callback: CallbackQuery):
    """Просмотр открытых тикетов поддержкой"""
    try:
        user_data = await get_or_create_user(
            callback.from_user.id,
            callback.from_user.username,
            callback.from_user.full_name
        )
        
        if not user_data['is_support']:
            await callback.answer("Нет доступа")
            return
        
        # Админ видит все тикеты, обычная поддержка - без collaboration
        tickets = await get_all_open_tickets(
            None if user_data['is_admin'] else user_data['id']
        )
        
        if not tickets:
            await callback.message.edit_text(
                f'<tg-emoji emoji-id="{EMOJI["check"]}">✅</tg-emoji> Нет открытых тикетов.',
                reply_markup=support_panel_keyboard()
            )
            await callback.answer()
            return
        
        await callback.message.edit_text(
            f'<b><tg-emoji emoji-id="{EMOJI["box"]}">📦</tg-emoji> Открытые тикеты:</b>',
            reply_markup=tickets_list_keyboard(tickets, "support_ticket")
        )
        await callback.answer()
    except Exception as e:
        logger.error(f"Error viewing support tickets: {e}")
        await callback.answer("Произошла ошибка")

@dp.callback_query(F.data.startswith("support_ticket_"))
async def support_view_ticket(callback: CallbackQuery):
    """Просмотр тикета поддержкой с медиа"""
    try:
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
        priority_info = PRIORITY_LEVELS.get(ticket['priority'], PRIORITY_LEVELS['medium'])
        
        text = (
            f'<b><tg-emoji emoji-id="{EMOJI["tag"]}">🏷</tg-emoji> Тикет {ticket["ticket_number"]}</b>\n\n'
            f'<tg-emoji emoji-id="{EMOJI["people"]}">👥</tg-emoji> Пользователь: {ticket["full_name"]}'
        )
        
        if ticket['username']:
            text += f' (@{ticket["username"]})'
        
        text += (
            f'\n<tg-emoji emoji-id="{EMOJI["file"]}">📁</tg-emoji> Категория: {ticket["category"]}\n'
            f'Приоритет: {priority_info["color"]} {priority_info["name"]}\n'
            f'<tg-emoji emoji-id="{EMOJI["file"]}">📁</tg-emoji> Статус: {"Открыт" if ticket["status"] == "open" else "Закрыт"}\n\n'
        )
        
        await callback.message.edit_text(text)
        
        # Отправляем историю сообщений с медиа
        for msg in messages:
            sender = "Пользователь" if msg['is_from_user'] else (msg.get('support_display_name') or msg['full_name'])
            msg_text = f"<b>{sender}:</b>\n{msg['message_text']}\n<i>{msg['created_at'].strftime('%d.%m.%Y %H:%M')}</i>"
            
            if msg.get('media_type'):
                try:
                    if msg['media_type'] == 'photo':
                        await callback.message.answer_photo(msg['media_file_id'], caption=msg_text)
                    elif msg['media_type'] == 'video':
                        await callback.message.answer_video(msg['media_file_id'], caption=msg_text)
                    elif msg['media_type'] == 'document':
                        await callback.message.answer_document(msg['media_file_id'], caption=msg_text)
                    elif msg['media_type'] == 'voice':
                        await callback.message.answer_voice(msg['media_file_id'], caption=msg_text)
                except:
                    await callback.message.answer(msg_text)
            else:
                await callback.message.answer(msg_text)
        
        await callback.message.answer(
            "Действия:",
            reply_markup=ticket_actions_keyboard(ticket_id, is_support=True)
        )
        await callback.answer()
        
        # Логируем просмотр тикета
        await add_support_log(user_data['id'], 'view_ticket', ticket_id, f"Просмотр тикета {ticket['ticket_number']}")
    except Exception as e:
        logger.error(f"Error viewing support ticket: {e}")
        await callback.answer("Произошла ошибка")

@dp.callback_query(F.data.startswith("reply_media_"))
async def support_reply_media_start(callback: CallbackQuery, state: FSMContext):
    """Начало ответа с медиа от поддержки"""
    ticket_id = int(callback.data.split("_")[2])
    await state.update_data(reply_media_ticket_id=ticket_id)
    
    await callback.message.edit_text(
        f'<b><tg-emoji emoji-id="{EMOJI["photo"]}">📷</tg-emoji> Отправьте медиа с подписью:</b>'
    )
    await state.set_state(SupportReply.waiting_for_media)
    await callback.answer()

@dp.message(StateFilter(SupportReply.waiting_for_media))
async def process_support_reply_media(message: Message, state: FSMContext):
    """Обработка медиа-ответа поддержки"""
    try:
        data = await state.get_data()
        ticket_id = data.get('reply_media_ticket_id')
        
        user_data = await get_or_create_user(
            message.from_user.id,
            message.from_user.username,
            message.from_user.full_name
        )
        
        if not user_data['is_support']:
            await message.answer("Нет доступа.")
            await state.clear()
            return
        
        media_type = None
        media_file_id = None
        
        if message.photo:
            media_type = 'photo'
            media_file_id = message.photo[-1].file_id
        elif message.video:
            media_type = 'video'
            media_file_id = message.video.file_id
        elif message.document:
            media_type = 'document'
            media_file_id = message.document.file_id
        
        if not media_type:
            await message.answer("Пожалуйста, отправьте медиа-файл.")
            return
        
        async with db_pool.acquire() as conn:
            ticket = await conn.fetchrow("SELECT * FROM tickets WHERE id = $1", ticket_id)
            
            await conn.execute(
                """INSERT INTO ticket_messages (ticket_id, sender_id, message_text, 
                   media_type, media_file_id, is_from_user) 
                   VALUES ($1, $2, $3, $4, $5, FALSE)""",
                ticket_id, user_data['id'], message.caption or "Медиа-файл",
                media_type, media_file_id
            )
            
            ticket_user = await conn.fetchrow("SELECT * FROM users WHERE id = $1", ticket['user_id'])
        
        display_name = user_data.get('support_display_name') or user_data['full_name']
        
        # Отправляем пользователю
        try:
            caption = f'<b>Ответ от поддержки</b>\n<tg-emoji emoji-id="{EMOJI["tag"]}">🏷</tg-emoji> Тикет: {ticket["ticket_number"]}\n\n<b>{display_name}:</b>\n{message.caption or ""}'
            
            if media_type == 'photo':
                await bot.send_photo(ticket_user['telegram_id'], media_file_id, caption=caption)
            elif media_type == 'video':
                await bot.send_video(ticket_user['telegram_id'], media_file_id, caption=caption)
            elif media_type == 'document':
                await bot.send_document(ticket_user['telegram_id'], media_file_id, caption=caption)
        except Exception as e:
            logger.error(f"Failed to send media reply: {e}")
        
        await message.answer(f'<tg-emoji emoji-id="{EMOJI["check"]}">✅</tg-emoji> Ответ отправлен!')
        
        # Логируем
        await add_support_log(user_data['id'], 'reply_media', ticket_id, f"Медиа-ответ в тикет {ticket['ticket_number']}")
        
        await state.clear()
    except Exception as e:
        logger.error(f"Error in support media reply: {e}")
        await message.answer("Произошла ошибка.")
        await state.clear()

@dp.callback_query(F.data.startswith("reply_"))
async def support_reply_start(callback: CallbackQuery, state: FSMContext):
    """Начало ответа поддержки"""
    try:
        ticket_id = int(callback.data.split("_")[1])
        await state.update_data(reply_ticket_id=ticket_id)
        
        await callback.message.edit_text(
            f'<b><tg-emoji emoji-id="{EMOJI["pencil"]}">🖋</tg-emoji> Введите ответ:</b>'
        )
        await state.set_state(SupportReply.waiting_for_reply)
        await callback.answer()
    except Exception as e:
        logger.error(f"Error in support reply start: {e}")
        await callback.answer("Произошла ошибка")

@dp.message(StateFilter(SupportReply.waiting_for_reply))
async def process_support_reply(message: Message, state: FSMContext):
    """Обработка ответа поддержки"""
    try:
        data = await state.get_data()
        ticket_id = data.get('reply_ticket_id')
        
        user_data = await get_or_create_user(
            message.from_user.id,
            message.from_user.username,
            message.from_user.full_name
        )
        
        if not user_data['is_support']:
            await message.answer("Нет доступа.")
            await state.clear()
            return
        
        async with db_pool.acquire() as conn:
            ticket = await conn.fetchrow("SELECT * FROM tickets WHERE id = $1", ticket_id)
            
            await conn.execute(
                """INSERT INTO ticket_messages (ticket_id, sender_id, message_text, is_from_user) 
                   VALUES ($1, $2, $3, FALSE)""",
                ticket_id, user_data['id'], message.text
            )
            
            ticket_user = await conn.fetchrow("SELECT * FROM users WHERE id = $1", ticket['user_id'])
        
        display_name = user_data.get('support_display_name') or user_data['full_name']
        
        # Отправляем ответ пользователю
        try:
            await bot.send_message(
                ticket_user['telegram_id'],
                f'<b><tg-emoji emoji-id="{EMOJI["notification"]}">🔔</tg-emoji> Ответ от поддержки</b>\n\n'
                f'<tg-emoji emoji-id="{EMOJI["tag"]}">🏷</tg-emoji> Тикет: {ticket["ticket_number"]}\n\n'
                f'<b>{display_name}:</b>\n{message.text}'
            )
        except Exception as e:
            logger.error(f"Failed to send reply to user: {e}")
        
        await message.answer(f'<tg-emoji emoji-id="{EMOJI["check"]}">✅</tg-emoji> Ответ отправлен!')
        
        # Логируем
        await add_support_log(user_data['id'], 'reply', ticket_id, f"Ответ в тикет {ticket['ticket_number']}")
        
        await state.clear()
    except Exception as e:
        logger.error(f"Error in process support reply: {e}")
        await message.answer("Произошла ошибка.")
        await state.clear()

@dp.callback_query(F.data.startswith("close_"))
async def close_ticket_handler(callback: CallbackQuery):
    """Закрытие тикета"""
    try:
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
            ticket_user = await conn.fetchrow("SELECT * FROM users WHERE id = $1", ticket['user_id'])
        
        try:
            await bot.send_message(
                ticket_user['telegram_id'],
                f'<b><tg-emoji emoji-id="{EMOJI["lock"]}">🔒</tg-emoji> Тикет закрыт</b>\n\n'
                f'<tg-emoji emoji-id="{EMOJI["tag"]}">🏷</tg-emoji> Тикет {ticket["ticket_number"]} был закрыт.\n'
                f'Если проблема не решена, создайте новый тикет.'
            )
        except Exception as e:
            logger.error(f"Failed to notify user about ticket closure: {e}")
        
        await callback.message.edit_text(
            f'<tg-emoji emoji-id="{EMOJI["check"]}">✅</tg-emoji> Тикет {ticket["ticket_number"]} закрыт.',
            reply_markup=support_panel_keyboard()
        )
        
        # Логируем
        await add_support_log(user_data['id'], 'close_ticket', ticket_id, f"Закрытие тикета {ticket['ticket_number']}")
        
        await callback.answer()
    except Exception as e:
        logger.error(f"Error closing ticket: {e}")
        await callback.answer("Произошла ошибка")

@dp.callback_query(F.data == "admin_logs")
async def admin_view_logs(callback: CallbackQuery):
    """Просмотр логов поддержки админом"""
    try:
        user_data = await get_or_create_user(
            callback.from_user.id,
            callback.from_user.username,
            callback.from_user.full_name
        )
        
        if not user_data['is_admin']:
            await callback.answer("Нет доступа")
            return
        
        logs = await get_support_logs(20)
        
        if not logs:
            await callback.message.edit_text(
                f'<tg-emoji emoji-id="{EMOJI["history"]}">📋</tg-emoji> Логи пусты.',
                reply_markup=admin_panel_keyboard()
            )
            await callback.answer()
            return
        
        text = f'<b><tg-emoji emoji-id="{EMOJI["history"]}">📋</tg-emoji> Последние действия поддержки:</b>\n\n'
        
        for log in logs[:10]:  # Показываем последние 10
            action_emoji = {
                'view_ticket': '👁',
                'reply': '💬',
                'reply_media': '📎',
                'close_ticket': '🔒'
            }.get(log['action_type'], '📝')
            
            text += (
                f'{action_emoji} <b>{log["full_name"]}</b> - {log["action_type"]}\n'
                f'<i>{log["created_at"].strftime("%d.%m.%Y %H:%M")}</i>\n'
            )
            if log.get('details'):
                text += f'└ {log["details"]}\n'
            text += '\n'
        
        await callback.message.edit_text(text, reply_markup=admin_panel_keyboard())
        await callback.answer()
    except Exception as e:
        logger.error(f"Error viewing logs: {e}")
        await callback.answer("Произошла ошибка")

@dp.callback_query(F.data == "admin_collaboration")
async def admin_collaboration_tickets(callback: CallbackQuery):
    """Просмотр тикетов по сотрудничеству"""
    try:
        user_data = await get_or_create_user(
            callback.from_user.id,
            callback.from_user.username,
            callback.from_user.full_name
        )
        
        if not user_data['is_admin']:
            await callback.answer("Нет доступа")
            return
        
        async with db_pool.acquire() as conn:
            tickets = await conn.fetch(
                """SELECT t.*, u.full_name, u.username 
                   FROM tickets t 
                   JOIN users u ON t.user_id = u.id 
                   WHERE t.is_collaboration = TRUE AND t.status = 'open'
                   ORDER BY t.created_at DESC"""
            )
        
        if not tickets:
            await callback.message.edit_text(
                f'<tg-emoji emoji-id="{EMOJI["star"]}">⭐</tg-emoji> Нет открытых тикетов по сотрудничеству.',
                reply_markup=admin_panel_keyboard()
            )
            await callback.answer()
            return
        
        await callback.message.edit_text(
            f'<b><tg-emoji emoji-id="{EMOJI["star"]}">⭐</tg-emoji> Тикеты по сотрудничеству:</b>',
            reply_markup=tickets_list_keyboard([dict(t) for t in tickets], "support_ticket")
        )
        await callback.answer()
    except Exception as e:
        logger.error(f"Error viewing collaboration tickets: {e}")
        await callback.answer("Произошла ошибка")

@dp.callback_query(F.data == "working_hours_info")
async def working_hours_info(callback: CallbackQuery):
    """Информация о рабочем времени"""
    await callback.answer("Поддержка работает с 9:00 до 22:00 по МСК. Ваш запрос будет обработан в рабочее время.", show_alert=True)

@dp.callback_query(F.data == "close_admin")
async def close_admin(callback: CallbackQuery):
    """Закрытие админ-панели"""
    await callback.message.delete()
    await callback.answer()

@dp.callback_query(F.data == "close_support")
async def close_support(callback: CallbackQuery):
    """Закрытие панели поддержки"""
    await callback.message.delete()
    await callback.answer()

@dp.callback_query(F.data == "back_to_main")
async def back_to_main(callback: CallbackQuery, state: FSMContext):
    """Возврат в главное меню"""
    await state.clear()
    await callback.message.delete()
    await callback.message.answer('Выберите действие:', reply_markup=main_menu_keyboard())
    await callback.answer()

@dp.callback_query(F.data == "back_to_support")
async def back_to_support(callback: CallbackQuery):
    """Возврат в панель поддержки"""
    await callback.message.edit_text(
        f'<b><tg-emoji emoji-id="{EMOJI["people"]}">👥</tg-emoji> Панель поддержки</b>',
        reply_markup=support_panel_keyboard()
    )
    await callback.answer()

@dp.callback_query(F.data == "back_to_tickets")
async def back_to_tickets(callback: CallbackQuery):
    """Возврат к списку тикетов"""
    user_data = await get_or_create_user(
        callback.from_user.id,
        callback.from_user.username,
        callback.from_user.full_name
    )
    
    tickets = await get_user_tickets(user_data['id'])
    
    if not tickets:
        await callback.message.edit_text(f'<tg-emoji emoji-id="{EMOJI["tag"]}">🏷</tg-emoji> У вас пока нет тикетов.')
        await callback.answer()
        return
    
    await callback.message.edit_text(
        f'<b><tg-emoji emoji-id="{EMOJI["tag"]}">🏷</tg-emoji> Ваши тикеты:</b>',
        reply_markup=tickets_list_keyboard(tickets, "my_ticket")
    )
    await callback.answer()

# Админские функции
@dp.callback_query(F.data == "admin_stats")
async def admin_statistics(callback: CallbackQuery):
    """Статистика для админа"""
    try:
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
            f'<b><tg-emoji emoji-id="{EMOJI["stats"]}">📊</tg-emoji> Статистика</b>\n\n'
            f'<tg-emoji emoji-id="{EMOJI["people"]}">👥</tg-emoji> Всего пользователей: {stats["total_users"]}\n'
            f'<tg-emoji emoji-id="{EMOJI["box"]}">📦</tg-emoji> Открытых тикетов: {stats["open_tickets"]}\n'
            f'<tg-emoji emoji-id="{EMOJI["lock"]}">🔒</tg-emoji> Закрытых тикетов: {stats["closed_tickets"]}\n'
            f'<tg-emoji emoji-id="{EMOJI["file"]}">📁</tg-emoji> Всего сообщений: {stats["total_messages"]}\n\n'
            f'<b>По приоритетам:</b>\n'
        )
        
        for priority, count in stats.get('priority_stats', {}).items():
            priority_info = PRIORITY_LEVELS.get(priority, PRIORITY_LEVELS['medium'])
            text += f'{priority_info["color"]} {priority_info["name"]}: {count}\n'
        
        await callback.message.edit_text(text, reply_markup=admin_panel_keyboard())
        await callback.answer()
    except Exception as e:
        logger.error(f"Error in admin statistics: {e}")
        await callback.answer("Произошла ошибка")

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
        f'<b><tg-emoji emoji-id="{EMOJI["megaphone"]}">📣</tg-emoji> Создание рассылки</b>\n\n'
        f'Отправьте текст или медиа для рассылки.'
    )
    await state.set_state(BroadcastMessage.waiting_for_message)
    await callback.answer()

@dp.message(StateFilter(BroadcastMessage.waiting_for_message))
async def broadcast_send(message: Message, state: FSMContext):
    """Отправка рассылки"""
    try:
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
        
        await message.answer(f'<tg-emoji emoji-id="{EMOJI["send"]}">⬆</tg-emoji> Начинаю рассылку...')
        
        for user in users:
            try:
                if message.photo:
                    await bot.send_photo(
                        user['telegram_id'],
                        message.photo[-1].file_id,
                        caption=f'<b><tg-emoji emoji-id="{EMOJI["megaphone"]}">📣</tg-emoji> Рассылка от Vest Smm</b>\n\n{message.caption or ""}'
                    )
                elif message.video:
                    await bot.send_video(
                        user['telegram_id'],
                        message.video.file_id,
                        caption=f'<b><tg-emoji emoji-id="{EMOJI["megaphone"]}">📣</tg-emoji> Рассылка от Vest Smm</b>\n\n{message.caption or ""}'
                    )
                else:
                    await bot.send_message(
                        user['telegram_id'],
                        f'<b><tg-emoji emoji-id="{EMOJI["megaphone"]}">📣</tg-emoji> Рассылка от Vest Smm</b>\n\n{message.text}'
                    )
                success_count += 1
                await asyncio.sleep(0.05)
            except Exception as e:
                fail_count += 1
                logger.error(f"Failed to send broadcast to {user['telegram_id']}: {e}")
        
        await message.answer(
            f'<b><tg-emoji emoji-id="{EMOJI["check"]}">✅</tg-emoji> Рассылка завершена!</b>\n\n'
            f'Успешно: {success_count}\nНе удалось: {fail_count}',
            reply_markup=main_menu_keyboard()
        )
        await state.clear()
    except Exception as e:
        logger.error(f"Error in broadcast: {e}")
        await message.answer("Произошла ошибка.")
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
        f'<b><tg-emoji emoji-id="{EMOJI["people"]}">👥</tg-emoji> Управление поддержкой</b>\n\n'
        f'<i>/add_support ID - добавить\n/remove_support ID - удалить</i>\n\n'
        f'<b>Сотрудники поддержки:</b>\n'
    )
    
    for user in support_users:
        text += f'\n<tg-emoji emoji-id="{EMOJI["profile"]}">👤</tg-emoji> {user["full_name"]}'
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
        await message.answer("Используйте: /add_support ID_пользователя")
        return
    
    async with db_pool.acquire() as conn:
        await conn.execute(
            "UPDATE users SET is_support = TRUE WHERE telegram_id = $1",
            target_id
        )
    
    await message.answer(f'✅ Пользователь {target_id} добавлен в поддержку!')

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
        await message.answer("Используйте: /remove_support ID_пользователя")
        return
    
    async with db_pool.acquire() as conn:
        await conn.execute(
            "UPDATE users SET is_support = FALSE WHERE telegram_id = $1",
            target_id
        )
    
    await message.answer(f'✅ Пользователь {target_id} удален из поддержки!')

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
            "Нет сотрудников поддержки.",
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
        f'<b><tg-emoji emoji-id="{EMOJI["pencil"]}">🖋</tg-emoji> Выберите сотрудника:</b>',
        reply_markup=builder.as_markup()
    )
    await callback.answer()

@dp.callback_query(F.data == "back_to_admin")
async def back_to_admin(callback: CallbackQuery):
    """Возврат в админ-панель"""
    await callback.message.edit_text(
        f'<b><tg-emoji emoji-id="{EMOJI["settings"]}">⚙</tg-emoji> Админ-панель</b>',
        reply_markup=admin_panel_keyboard()
    )
    await callback.answer()

@dp.callback_query(F.data.startswith("set_name_"))
async def set_display_name_start(callback: CallbackQuery, state: FSMContext):
    """Начало установки отображаемого имени"""
    target_id = int(callback.data.split("_")[2])
    await state.update_data(name_target_id=target_id)
    
    await callback.message.edit_text(
        f'<b><tg-emoji emoji-id="{EMOJI["pencil"]}">🖋</tg-emoji> Введите новое имя:</b>'
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
    
    await message.answer(f'✅ Отображаемое имя установлено: {message.text}')
    await state.clear()

async def main():
    """Основная функция запуска бота"""
    logger.info("Starting bot...")
    
    await init_db()
    
    logger.info("Bot started successfully")
    
    try:
        await dp.start_polling(bot)
    except Exception as e:
        logger.error(f"Error in main loop: {e}")
    finally:
        if db_pool:
            await db_pool.close()
            logger.info("Database connection closed")

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Bot stopped by user")
    except Exception as e:
        logger.error(f"Fatal error: {e}")
